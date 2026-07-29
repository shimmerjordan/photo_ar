"""语料构建与加载。

Phase 0 的产物是纯文件，不引入 SQLite —— 数据库是 Phase 1 随服务
一起引入的。产物布局：
    <root>/desc.bin        定长描述子库
    <root>/vocab.npz       词汇树
    <root>/index.npz       倒排索引
    <root>/manifest.json   photo_id 顺序与元数据（顺序即 slot/doc 下标）
    <root>/imgdb/<id>.imgdb  单目标库（仅在提供 arcoreimg 时生成）

manifest 里 photos 的顺序就是描述子库 slot 下标与倒排索引 doc 下标，
三者必须始终一致——这是 load_corpus 里两项完整性校验（描述子指纹、
倒排索引自查）要守护的不变量。build_corpus 本身靠单循环里"追加一个
就索引一个"天然维持这个不变量；一旦 Phase 1 从持久化语料加载，顺序
就不再由代码保证，而是由文件保证，所以加载侧必须自己验证。
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from . import quality as Q
from . import vocab as V
from .descstore import DescStore, DescStoreWriter
from .features import N_FEATURES, extract
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

# manifest.json 的 schema 版本。v1 -> v2：PhotoEntry 新增 desc_sha256 字段
# （描述子指纹，供 load_corpus 的 _verify_desc_fingerprints 校验描述子库
# slot 顺序）。v1 的 manifest 没有这个字段，如果直接当 v2 读，会在构造
# PhotoEntry(**e) 时因缺字段抛不知所云的 TypeError；load_corpus 改为先比
# 对版本号，版本不对就给出清晰错误。
MANIFEST_VERSION = 2


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
    校验时会对着两份不同的东西比较，产生假阳性或假阴性。
    """
    count = min(desc.shape[0], N_FEATURES)
    truncated = np.ascontiguousarray(desc[:count], np.uint8)
    return hashlib.sha256(truncated.tobytes()).hexdigest()


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
    for path in sorted(Path(p) for p in image_paths):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            continue
        f = extract(img)
        if len(f) == 0:
            continue

        photo_id = _photo_id(path)
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
                continue

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

    if not entries:
        raise ValueError("没有任何图片通过入库（可能全部不可读或质量分不达标）")

    with DescStoreWriter(paths.desc, capacity=len(feats)) as w:
        for f in feats:
            w.append(f)

    voc = V.train(np.vstack([f.desc for f in feats]), seed=seed)
    voc.save(paths.vocab)

    builder = InvertedIndexBuilder(voc.n_words)
    for f in feats:
        builder.add(voc.words_of(f.desc))
    builder.build().save(paths.index)

    paths.manifest.write_text(
        json.dumps(
            {"version": MANIFEST_VERSION, "photos": [asdict(e) for e in entries]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return entries


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
      - n_docs=1 时语料本身没法被"打乱"，恒过是对的。
      - 旧版本 k 恒为 top_k(20)，n_docs<=20 时检测概率恒为 0%。

    这仍然是概率性检测，不是保证；用 5 个均匀分布的采样点是为了把单次
    检测的偶然失败（比如某张照片恰好是识别难例）平均掉，同时保持对
    "整体错位"这种粗暴故障的高检出率。零特征的照片直接跳过，不能既
    没有词又要求命中自己。
    """
    n_docs = len(entries)
    k = max(1, min(top_k, n_docs // 2))
    slots = _self_query_sample_slots(n_docs)
    for slot in slots:
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

    # 顺序完整性校验：manifest 的顺序必须真的等于 slot 顺序（指纹）与
    # doc 顺序（自查），否则宁可在启动时报错，也不要在线上悄悄认错人。
    _verify_desc_fingerprints(store, entries)

    rec = TwoStageRecognizer(
        vocab=voc,
        index=idx,
        store=store,
        photo_ids=[e.photo_id for e in entries],
    )

    _verify_self_query(voc, idx, store, entries)

    return rec, entries
