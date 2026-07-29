"""语料构建与加载。

Phase 0 的产物是纯文件，不引入 SQLite —— 数据库是 Phase 1 随服务
一起引入的。产物布局：
    <root>/desc.bin        定长描述子库
    <root>/vocab.npz       词汇树
    <root>/index.npz       倒排索引
    <root>/manifest.json   photo_id 顺序与元数据（顺序即 slot/doc 下标）
    <root>/imgdb/<id>.imgdb  单目标库（仅在提供 arcoreimg 时生成）
    <root>/holdout.json    库外查询测试集（仅在 build 时给了 --holdout-frac
                           才会生成，见 select_holdout/finding I8）——这些
                           路径对应的照片从未进入上面任何一个产物文件

manifest 里 photos 的顺序就是描述子库 slot 下标与倒排索引 doc 下标，
三者必须始终一致——这是 load_corpus 里两项完整性校验（描述子指纹、
倒排索引自查）要守护的不变量。build_corpus 本身靠单循环里"追加一个
就索引一个"天然维持这个不变量；一旦 Phase 1 从持久化语料加载，顺序
就不再由代码保证，而是由文件保证，所以加载侧必须自己验证。
"""

import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from . import quality as Q
from . import vocab as V
from .descstore import DescStore, DescStoreWriter, truncate_count
from .features import extract
from .index import InvertedIndexBuilder, InvertedIndex
from .recognizer import TOP_K, TwoStageRecognizer

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

# arcoreimg build-db 的清单行要求一个正的打印物理宽度（米）。Phase 0 只做
# 识别、不做 AR 放置，语料构建时并不天然知道每张照片真实的打印尺寸，因此
# 用这个值作为 build_corpus 的 print_width_m 默认值（0.152m ≈ 6 寸照片
# 宽边）。调用方（CLI 的 --print-width-mm，或直接调用 build_corpus 的
# 代码）可以覆盖它；如果 0d 的照片是别的尺寸打印的，必须显式传入真实值，
# 否则烘进 .imgdb 的物理宽度会是错的。
DEFAULT_PRINT_WIDTH_M = 0.152

# 词汇树训练用的描述子数量上限。vocab.train 的 k-majority 在根层每次迭代
# 都要物化 (N, BRANCHING, 32) 的中间数组，实测端到端峰值 RSS 随训练描述子
# 数 N 线性增长、约 1.66 KB/描述子（50k -> 142.6MB，150k -> 308.7MB，
# 300k -> 554.9MB，见 final-fix-wave1-report.md 的 C2 测量）。1 万张照片 x
# N_FEATURES(300) = 300 万描述子若不设上限会把这一步撑到约 5GB，是 0d 第一次
# 真实入库最容易撞上的 OOM 点。这个数值故意与 measure-0b.py 独立定义的同名
# TRAIN_DESC_CAP 一致——那不是巧合，是同一个真实内存约束，此前只在测量脚本
# 里躲开、产品代码里没有，是"0b 的数字不是产品这棵树量出来的"这条已知缺口；
# 这里把上限搬进 build_corpus，缺口即告闭合。超过上限时用 default_rng(seed)
# 做不放回抽样：词袋检索的词表本来就只需要能代表描述子的分布，不需要看到
# 每一个描述子，抽样训练是标准做法。
TRAIN_DESC_CAP = 120_000

# manifest.json 的 schema 版本。v1 -> v2：PhotoEntry 新增 desc_sha256 字段
# （描述子指纹，供 load_corpus 的 _verify_desc_fingerprints 校验描述子库
# slot 顺序）。v1 的 manifest 没有这个字段，如果直接当 v2 读，会在构造
# PhotoEntry(**e) 时因缺字段抛不知所云的 TypeError；load_corpus 改为先比
# 对版本号，版本不对就给出清晰错误。
MANIFEST_VERSION = 2

# finding I8：holdout.json 记录 build 时被留出、从未进入语料的照片路径。
# 这是独立于 manifest 的一份小文件，不参与 MANIFEST_VERSION 的版本号
# （它不影响 desc/vocab/index 三者的顺序不变量，load_corpus 完全不需要
# 它就能正常加载语料），但同样加个自己的版本号，为以后演进格式留余地。
HOLDOUT_MANIFEST_VERSION = 1


class CorpusIntegrityError(RuntimeError):
    """语料本身不可信：顺序对不上，或者 manifest 版本不受支持。

    TwoStageRecognizer.__init__ 只检查三者的**长度**相等，这防不住"顺序被
    打乱但长度凑巧一致"的情况——那种情况下 recognize() 会自信地返回一张
    别的、错误的照片，正是本项目权重最高的误识别类别。这个异常由
    load_corpus 的两项顺序完整性校验抛出：描述子指纹校验证明 slot 与
    manifest 对应；倒排索引自查证明 doc 下标与 slot/manifest 对应。同一个
    异常也在 manifest 版本号与 MANIFEST_VERSION 不一致时抛出——版本不对
    同样意味着"这份语料不能直接当当前 schema 用"，调用方（cli.py）只需
    一个 except 分支就能把这些情况统一映射到退出码 2。
    """


@dataclass(frozen=True)
class CorpusPaths:
    root: Path
    desc: Path
    vocab: Path
    index: Path
    manifest: Path
    imgdb_dir: Path
    holdout: Path

    @classmethod
    def at(cls, root: str | Path) -> "CorpusPaths":
        root = Path(root)
        return cls(
            root=root,
            desc=root / "desc.bin",
            vocab=root / "vocab.npz",
            index=root / "index.npz",
            manifest=root / "manifest.json",
            imgdb_dir=root / "imgdb",
            holdout=root / "holdout.json",
        )


@dataclass(frozen=True)
class PhotoEntry:
    photo_id: str
    ref_path: str
    quality_score: int  # -1 = 未评估（未提供 arcoreimg）
    imgdb_bytes: int  # 0 = 未生成
    desc_sha256: str  # 写入描述子库那一份（截断到 N_FEATURES 后）的 SHA-256


def _photo_id(path: Path) -> str:
    """由内容指纹派生 id，同一张图重复入库得到同一个 id。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _desc_fingerprint(desc: np.ndarray) -> str:
    """算出即将写入描述子库那份字节的 SHA-256（截断到 N_FEATURES 之后）。

    必须和 DescStoreWriter.append 实际写入的字节完全一致，否则 load_corpus
    校验时会对着两份不同的东西比较，产生假阳性或假阴性。Minor #23：截断
    规则本身（min(count, N_FEATURES)）以前在这里和 DescStoreWriter.append
    里各自独立写了一份，两处一旦分叉，fingerprint 校验就会系统性地误判——
    现在共用 descstore.truncate_count，确保两处永远同步。
    """
    count = truncate_count(desc.shape[0])
    truncated = np.ascontiguousarray(desc[:count], np.uint8)
    return hashlib.sha256(truncated.tobytes()).hexdigest()


def select_holdout(
    image_paths: list[Path], frac: float, seed: int
) -> tuple[list[Path], list[Path]]:
    """按 frac 从 image_paths 里确定性地切出一部分，永远不参与 build_corpus。

    finding I8："库外查询"指的是识别器的候选库里根本不存在这张照片的任何
    描述子/词/倒排项——不是"库内某张恰好没被抽去当 eval 的参考图"。切分
    必须发生在 build_corpus **之前**：留出的照片一次都不能进 extract /
    DescStoreWriter / 词汇树训练 / 倒排索引，否则它的内容早就以别的形式
    渗进了语料（比如被采样进词汇树训练集），"从未入库"这个前提就不成立了。

    用 default_rng(seed).choice 做不放回抽样：给定同一个 seed，两次对
    同一批 image_paths 调用必须选中同一批留出图（spec 明确要求的确定性），
    这里先按路径排序再抽样，保证结果与调用方传入 image_paths 的原始顺序
    无关（跟 build_corpus 自己对 image_paths 排序的做法一致）。

    返回 (library_paths, holdout_paths)；frac<=0 时 holdout_paths 恒为
    空列表，library_paths 就是排序后的全量——这是默认行为，不留出任何图，
    与这个特性存在之前完全等价。
    """
    ordered = sorted(Path(p) for p in image_paths)
    n = len(ordered)
    if frac <= 0:
        return ordered, []
    if not (0 < frac < 1):
        raise ValueError(f"holdout frac 必须在 (0, 1) 之间，收到 {frac!r}")
    n_holdout = max(1, round(n * frac))
    if n_holdout >= n:
        raise ValueError(
            f"holdout frac={frac!r} 会把 {n} 张图全部留出（算出 {n_holdout} 张）："
            f"库里至少要留 1 张才能建语料，换一个更小的 frac 或提供更多照片"
        )
    rng = np.random.default_rng(seed)
    holdout_idx = set(rng.choice(n, size=n_holdout, replace=False).tolist())
    library = [p for i, p in enumerate(ordered) if i not in holdout_idx]
    holdout = [ordered[i] for i in sorted(holdout_idx)]
    return library, holdout


def write_holdout(out_root: str | Path, holdout_paths: list[Path]) -> None:
    """把 select_holdout 切出的留出图路径写进语料目录，供 eval 侧读取。

    与 manifest.json 分开是故意的：留出图从未进入 desc/vocab/index/
    manifest，不影响 load_corpus 的任何完整性校验，是一份纯附加信息。
    显式 encoding="utf-8"（不依赖进程 locale）——I5 已经证明真实照片目录
    里非 ASCII 路径近乎必然出现。
    """
    paths = CorpusPaths.at(out_root)
    paths.holdout.write_text(
        json.dumps(
            {
                "version": HOLDOUT_MANIFEST_VERSION,
                "paths": [str(p) for p in holdout_paths],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_holdout(root: str | Path) -> list[Path]:
    """读回 write_holdout 写的留出图路径；语料没有 holdout.json（旧语料，
    或 build 时没开 --holdout-frac）时返回空列表——eval 侧据此判断"这次
    没有库外测量"，行为与这个特性存在之前完全一致，不报错。"""
    paths = CorpusPaths.at(root)
    if not paths.holdout.exists():
        return []
    data = json.loads(paths.holdout.read_text(encoding="utf-8"))
    return [Path(p) for p in data.get("paths", [])]


def build_corpus(
    image_paths: list[Path],
    out_root: str | Path,
    seed: int = 0,
    arcoreimg: str | None = None,
    print_width_m: float = DEFAULT_PRINT_WIDTH_M,
) -> list[PhotoEntry]:
    if not image_paths:
        raise ValueError("build_corpus 需要至少一张图片")
    if print_width_m <= 0:
        raise ValueError(f"print_width_m 必须为正数（米），收到 {print_width_m!r}")

    paths = CorpusPaths.at(out_root)
    paths.root.mkdir(parents=True, exist_ok=True)

    entries: list[PhotoEntry] = []
    feats = []
    seen_ids: set[str] = set()
    # I7：每个跳过原因都要能数出来、报出来——旧代码只有 quality_too_low 这
    # 一种会被"悄悄 continue"，unreadable/zero_feature 更是从来没人数过；
    # CLI 原来只打印"入库 N 张"，用户没法知道 1 万张变成 9800 张是质量分
    # 不够、图片本身读不出来、还是重复照片，0d 的 correct_rate 也会被这种
    # "分母悄悄变小却没人知道"污染。
    skip_counts = {
        "unreadable": 0,
        "zero_feature": 0,
        "duplicate": 0,
        "quality_too_low": 0,
        "invalid_listing": 0,
    }
    for path in sorted(Path(p) for p in image_paths):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            skip_counts["unreadable"] += 1
            continue
        f = extract(img)
        if len(f) == 0:
            skip_counts["zero_feature"] += 1
            continue

        photo_id = _photo_id(path)
        if photo_id in seen_ids:
            # I9：按内容哈希去重。字节完全相同的重复照片如果都入库，desc.bin
            # 会出现两个内容一模一样的 slot：两者互为最近邻，RATIO 判定
            # （第一名内点数 >= RATIO * 第二名）永远无法分出胜负，两份都会
            # 被判 ambiguous——安全方向（不会误识别），但等于两张都永久
            # 漏检，还会让 manifest 出现重复 id、.imgdb 被写两次到同一路径。
            # 只对"已经成功入库的照片"去重（seen_ids 只在成功 append 后才
            # 添加，见下方）：如果第一次出现本身就没通过下面的质量分/清单
            # 格式校验，后续字节相同的重复照片仍会各自重新走一遍这些校验
            # （结果必然相同，因为内容相同——冗余但不是错误），不会被误记成
            # "重复"而掩盖真实的拒绝原因。
            skip_counts["duplicate"] += 1
            continue

        score, imgdb_bytes = -1, 0
        if arcoreimg is not None:
            try:
                score = Q.assert_quality(path, arcoreimg=arcoreimg)
                imgdb_bytes = Q.build_single_target_db(
                    path, name=photo_id,
                    print_width_m=print_width_m,
                    out_path=paths.imgdb_dir / f"{photo_id}.imgdb",
                    arcoreimg=arcoreimg,
                )
            except Q.QualityTooLow:
                skip_counts["quality_too_low"] += 1
                continue
            except Q.InvalidListingField:
                # I5：文件名/路径含 '|' 或换行，与 ASCII 字符集无关（已实测
                # 推翻"只支持 ASCII"这个旧假设）。这类照片单张跳过、记录
                # 原因，而不是让 build_single_target_db 的 ValueError 一路
                # 冒泡出 build_corpus，让整个入库中止在半途——中文文件名在
                # 真实照片目录里近乎必然出现，之前会被这条路径误伤（虽然
                # 中文本身现在已经不再触发这个异常，含字面 '|' 的路径仍然
                # 会）。
                skip_counts["invalid_listing"] += 1
                continue

        seen_ids.add(photo_id)
        feats.append(f)
        entries.append(
            PhotoEntry(
                photo_id=photo_id,
                ref_path=str(path),
                quality_score=score,
                imgdb_bytes=imgdb_bytes,
                desc_sha256=_desc_fingerprint(f.desc),
            )
        )

    _warn_skips(skip_counts)

    if not entries:
        detail = _skip_summary(skip_counts)
        suffix = f"（{detail}）" if detail else "（可能全部不可读或质量分不达标）"
        raise ValueError(f"没有任何图片通过入库{suffix}")

    with DescStoreWriter(paths.desc, capacity=len(feats)) as w:
        for f in feats:
            w.append(f)

    all_desc = np.vstack([f.desc for f in feats])
    train_desc = all_desc
    if train_desc.shape[0] > TRAIN_DESC_CAP:
        rng = np.random.default_rng(seed)
        sample = rng.choice(train_desc.shape[0], size=TRAIN_DESC_CAP, replace=False)
        train_desc = train_desc[sample]
    voc = V.train(train_desc, seed=seed)
    voc.save(paths.vocab)

    builder = InvertedIndexBuilder(voc.n_words)
    for f in feats:
        builder.add(voc.words_of(f.desc))
    idx = builder.build()
    idx.save(paths.index)
    _warn_unretrievable(idx.unretrievable_docs(), entries, action="入库")

    paths.manifest.write_text(
        json.dumps(
            {"version": MANIFEST_VERSION, "photos": [asdict(e) for e in entries]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return entries


_SKIP_REASON_LABELS = {
    "unreadable": "读不出来（不是有效图片）",
    "zero_feature": "提取不到 ORB 特征点",
    "duplicate": "内容与已入库的另一张完全重复（按内容哈希去重）",
    "quality_too_low": "arcoreimg 质量分不达标",
    "invalid_listing": "文件名/路径含清单分隔符 '|' 或换行",
}


def _skip_summary(skip_counts: dict[str, int]) -> str:
    """把逐原因的跳过计数拼成一行人类可读摘要；没有任何跳过时返回空串。"""
    parts = [
        f"{_SKIP_REASON_LABELS[reason]} {count} 张"
        for reason, count in skip_counts.items()
        if count
    ]
    return "；".join(parts)


def _warn_skips(skip_counts: dict[str, int]) -> None:
    """I7：把每张未入库照片的跳过原因报出来，而不是只让"入库 N 张"这一个
    数字掩盖掉分母是怎么变小的——不知道 1 万张变成 9800 张是质量分不够、
    读不出来还是重复照片，0d 的 correct_rate 就没法正确解读。"""
    total = sum(skip_counts.values())
    if not total:
        return
    print(
        f"警告：{total} 张照片未入库 —— {_skip_summary(skip_counts)}",
        file=sys.stderr,
    )


def _warn_unretrievable(
    unretrievable: list[int], entries: list["PhotoEntry"], action: str
) -> None:
    """I3：把"哪些照片的全部特征词都是全局共享词、因而永远无法被检索命中"
    报出来，而不是让它们在 n_docs / len(entries) 里悄悄消失。

    这不是错误（语料本身没有损坏，_verify_self_query 会正确地跳过对它们
    的自查），只是一个用户应该知道的退化信号：常见原因是语料规模太小
    （n_docs=1 时唯一那篇文档必然触发）或者内容高度重复/雷同。
    """
    if not unretrievable:
        return
    ids_preview = "、".join(entries[d].photo_id for d in unretrievable[:5])
    more = f" 等共 {len(unretrievable)} 张" if len(unretrievable) > 5 else ""
    print(
        f"警告：{action}的照片里有 {len(unretrievable)} 张的全部特征词都是"
        f"全局共享词（idf=0），无法被检索命中，但仍计入图库规模：{ids_preview}"
        f"{more}。常见原因是语料规模太小（比如只有 1 张）或内容高度重复/雷同。",
        file=sys.stderr,
    )


def _verify_desc_fingerprints(store: DescStore, entries: list["PhotoEntry"]) -> None:
    """逐 slot 校验描述子库内容与 manifest 记录的指纹一致。

    证明的是"描述子库 slot i 确实是 entries[i] 这张照片"——查不出倒排
    索引被打乱的情况（索引不存内容，无从对比），那部分交给
    _verify_self_query。
    """
    for slot, entry in enumerate(entries):
        features = store.read(slot)
        actual = hashlib.sha256(np.ascontiguousarray(features.desc, np.uint8).tobytes()).hexdigest()
        if actual != entry.desc_sha256:
            raise CorpusIntegrityError(
                f"描述子库 slot {slot}（photo_id={entry.photo_id}）指纹不匹配：manifest "
                f"记录 {entry.desc_sha256}，实际读到 {actual}。这通常意味着描述子库与 "
                f"manifest 的顺序发生了错位（例如两者是从不同顺序分别写入的）。"
            )


def _self_query_sample_slots(n: int, n_samples: int = 5) -> list[int]:
    """在 [0, n) 上确定性地挑最多 n_samples 个下标，按下标均匀分布。"""
    if n <= 0:
        return []
    if n <= n_samples:
        return list(range(n))
    return sorted({round(i * (n - 1) / (n_samples - 1)) for i in range(n_samples)})


def _verify_self_query(
    vocab: "V.Vocab",
    index: InvertedIndex,
    store: DescStore,
    entries: list["PhotoEntry"],
    top_k: int = TOP_K,
    unretrievable: frozenset[int] = frozenset(),
) -> None:
    """抽样验证倒排索引的 doc 下标顺序与 manifest / 描述子库一致。

    描述子指纹查不出倒排索引被整体打乱的情况——索引里不存任何可以拿来
    hash 的原始内容。这里改用行为验证：一张照片自己的词去查索引，理应
    在候选里看到它自己的 doc 下标；如果索引被错位，这条会失败。

    实际查询用的 k 会按语料规模收缩：
        k = max(1, min(top_k, n_docs // 2))
    而不是恒等于 top_k。原因是 InvertedIndex.query 内部本来就会把 k 截到
    min(top_k, n_docs)——如果语料规模不大于 top_k（比如恰好等于
    recognizer.TOP_K=20 的默认语料），query 就会把**全部**文档当 top-K
    候选返回，"自己的下标是否在候选里"这条断言无论顺序有没有被打乱都
    恒为真，检测概率恒为 0%，这正是本项目反复撞见的那种"看着在守护、
    实际什么都没守护"的假校验。收缩 k 之后：
      - 一个被打乱的索引会把照片 i 的词映射到别的文档上，所以照片 i
        自己的 doc 下标在打分排序里近似随机分布；单次采样检测出错位的
        概率约为 1 - k/n_docs。
      - n_docs=12 时 k=6，单次约 50%，5 个采样点合起来 1-0.5^5 ≈ 97%。
      - n_docs=1000 时 k=20（不受收缩影响，因为 20 <= 500），单次约
        98%，几乎必然检测到。
      - n_docs=2 时 k=1，要求排第一；打乱后另一篇文档会排第一，能测到。
      - 旧版本 k 恒为 top_k(20)，n_docs<=20 时检测概率恒为 0%。

    这仍然是概率性检测，不是保证；用 5 个均匀分布的采样点是为了把单次
    检测的偶然失败（比如某张照片恰好是识别难例）平均掉，同时保持对
    "整体错位"这种粗暴故障的高检出率。零特征的照片直接跳过，不能既
    没有词又要求命中自己。

    unretrievable 是 index.unretrievable_docs() 算出的、"全部词都是全局
    共享词（idf=0）、tf-idf 范数为 0、build() 时被整体排除在倒排表之外"
    的文档下标集合（I3）。这类文档天生不会出现在任何候选列表里——
    index.query 对着一个 qnorm=0 的查询直接返回空列表——继续对它们做自查
    只会制造 100% 必然触发的假阳性，而不是检测真实的顺序错位。旧代码这里
    曾经错误地断言"n_docs=1 时语料本身没法被打乱，恒过是对的"：实测结果
    是恒炸，不是恒过（唯一文档的每个词 df 必然等于 n_docs=1，必然触发这
    个情况）。跳过这些下标，而不是跳过"n_docs 很小"这整类语料——n_docs=2
    时如果两篇文档有各自的独有词，顺序打乱依然能被正确测到。
    """
    n_docs = len(entries)
    k = max(1, min(top_k, n_docs // 2))
    slots = _self_query_sample_slots(n_docs)
    for slot in slots:
        if slot in unretrievable:
            continue
        features = store.read(slot)
        if len(features) == 0:
            continue
        words = vocab.words_of(features.desc)
        if words.size == 0:
            continue
        candidates = [doc for doc, _ in index.query(words, k)]
        if slot not in candidates:
            raise CorpusIntegrityError(
                f"倒排索引自查失败：photo_id={entries[slot].photo_id}（slot {slot}）用 "
                f"自己的描述子查询索引，Top-{k} 候选 {candidates} 里却没有它自己。 "
                f"这通常意味着倒排索引与 manifest / 描述子库的顺序发生了错位。"
            )


def load_corpus(root: str | Path) -> tuple[TwoStageRecognizer, list[PhotoEntry]]:
    paths = CorpusPaths.at(root)
    for required in (paths.desc, paths.vocab, paths.index, paths.manifest):
        if not required.exists():
            raise FileNotFoundError(f"语料不完整，缺少 {required}")

    data = json.loads(paths.manifest.read_text())
    version = data.get("version")
    if version != MANIFEST_VERSION:
        raise CorpusIntegrityError(
            f"manifest 版本不受支持：期望 {MANIFEST_VERSION}，实际读到 {version!r}。"
            f"语料可能是用不兼容的旧版本 photoar 构建的（比如缺 desc_sha256 字段），"
            f"需要用当前版本重新 build_corpus。"
        )
    entries = [PhotoEntry(**e) for e in data["photos"]]

    voc = V.Vocab.load(paths.vocab)
    idx = InvertedIndex.load(paths.index)
    store = DescStore(paths.desc)

    # I4：三者数量的前置校验必须在指纹校验循环之前做。desc.bin 被截断
    # （比如少了整数个 slot）时，仍然满足"文件大小是 SLOT_STRIDE 的整数
    # 倍"，DescStore 不会报错，但 len(store) 会比 manifest/index 记的数量
    # 少。_verify_desc_fingerprints 是按 enumerate(entries) 的下标去读
    # store 的，如果不先在这里挡住，读到越界下标时 DescStore.read 会抛出
    # 未捕获的 IndexError（不是 CorpusIntegrityError），让调用方
    # （cli._cmd_eval）的 traceback 以 Python 默认退出码 1 结束——违反了
    # "语料损坏归 2，不是 1"的退出码约定。TwoStageRecognizer.__init__ 本来
    # 就会检查这三者长度相等，但它在这之后才构造，救不了指纹校验这一步。
    if not (len(store) == len(entries) == idx.n_docs):
        raise CorpusIntegrityError(
            f"语料三者数量不一致：manifest {len(entries)} 条、描述子库 "
            f"{len(store)} slot、倒排索引 {idx.n_docs} 个 doc。这通常意味着"
            f"某个产物文件被截断或被部分覆盖，需要用当前版本重新 build_corpus。"
        )

    # 顺序完整性校验：manifest 的顺序必须真的等于 slot 顺序（指纹）与
    # doc 顺序（自查），否则宁可在启动时报错，也不要在线上悄悄认错人。
    _verify_desc_fingerprints(store, entries)

    # I3：先算出"哪些文档的全部词都是全局共享词、天生检索不到"，报出来
    # （不是错误，是退化信号），并把同一份结果喂给 _verify_self_query 当
    # 豁免名单——它们必然不会出现在任何查询候选里，继续对它们做自查只会
    # 制造 100% 必然触发的假阳性，见 _verify_self_query 的文档。
    unretrievable = idx.unretrievable_docs()
    _warn_unretrievable(unretrievable, entries, action="加载")

    rec = TwoStageRecognizer(
        vocab=voc,
        index=idx,
        store=store,
        photo_ids=[e.photo_id for e in entries],
    )

    _verify_self_query(voc, idx, store, entries, unretrievable=frozenset(unretrievable))

    return rec, entries
