"""可增量入库的识别库。

Phase 0 的 `corpus.build_corpus` 是**全量**构建：一次扫完所有照片，训练词汇树，
再把倒排索引压成扁平数组。服务端不能这样 —— `POST /v1/photo` 一次只加一张。
三个约束决定了这里的设计：

**1. 词汇树必须固定，不能随库增长重训。**
`vocab` 决定描述子量化成哪个词，重训会让已存的全部词序列失效（要重新量化全库
描述子，10k 张约 3M 次树遍历）。spec §8.2 本来也要求用预训练词表。所以服务
启动时加载一份只读 vocab，入库只量化不训练。换 vocab = 全库重建索引，用
`reindex(rebuild_words=True)` 显式做，不会偷偷发生。

**2. idf 随 n_docs 变，所以倒排表必须整体重建，不能只追加 postings。**
`idf = log(n_docs/df)`，加一张照片会改变**每一个词**的 idf，从而改变**每一篇
文档**已归一化的 tf-idf 权重。只把新文档的 postings 追加进去、不动旧权重，
粗排排序会静默漂移 —— 没有任何报错，只表现为召回率慢慢变差。这正是本项目
反复在防的那类缺陷，所以这里选"每次入库重建一次扁平索引"。

代价是入库路径上的一次 O(全库总词数) 重建（实测数字见 `bench/library_scan.py`
的输出与 Phase 1 计划文档），换来的是查询路径上与 Phase 0 完全相同的语义。
入库本来就要跑 `arcoreimg` + `ffmpeg`（秒级到分钟级），重建不是瓶颈；批量
入库用 `add(..., defer_reindex=True)` 加一次 `reindex()`，只付一次。

**3. 重建需要每张照片的词序列，而 `InvertedIndex` 不保留它。**
所以词序列自己持久化成定长文件 `words.bin`（4 + 300×4 = 1204 字节/张，1 万张
12MB）。不从 `desc.bin` 现算的理由：`vocab.words_of` 是 Python 循环，10k 张
要 3M 次树遍历，比读 12MB 慢几个数量级。

## 小库与"检索不到的文档"

`InvertedIndex` 有一个真实的退化：`idf[w] == 0` 当且仅当 `df[w] == n_docs`，
此时该词对所有文档无区分度。**n_docs == 1 时每个词的 df 都等于 1 == n_docs，
唯一那篇文档的 tf-idf 范数为 0，永远检索不到。** 刚部署完入库第一张照片，
粗排会返回空，识别必然失败。

不通过给 idf 加平滑来解决 —— 那会改变 Phase 0 实测过的排序语义。做法是：
`n_docs <= TOP_K` 时**跳过粗排，直接校验全部照片**。此时两者本来就等价（粗排
存在的意义是从上万张里选 20 张，库里不到 20 张时它什么也没筛掉），而且更快。
库更大时若仍有 `unretrievable_docs`，把它们并进候选集 —— 多给候选只会让 ratio
判据更保守，不会放宽判定。

`n_docs > TOP_K` 且无退化文档时，候选集与 `TwoStageRecognizer` **逐位相同**，
Phase 0 的实测数字直接适用。`tests/server/test_library.py` 钉住了这一点。
"""

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import descstore
from .. import backend as backend_mod
from ..backend import Backend, VocabLike
from ..descstore import DescStore
from ..features import N_FEATURES, Features
from ..index import InvertedIndex, InvertedIndexBuilder
from ..recognizer import TOP_K
from ..verify import (
    DET_MAX,
    DET_MIN,
    RATIO,
    Decision,
    PairResult,
    decide,
)

SLOTS_VERSION = 1

# 词序列文件的定长布局：uint32 count + uint32[N_FEATURES] words（本机字节序，
# 与 descstore 同一前提，理由见那个模块的 Minor #8 说明）。
_WORDS_SLOTS = 1 + N_FEATURES
_WORDS_STRIDE = _WORDS_SLOTS * 4

# 增量去重时的候选覆盖面，比识别用的 TOP_K(20) 大。0d 上规模实测：库外误识别
# 里有 0.452% 是"dedup 的 Top-20 覆盖面漏掉的近重复"——粗排没把那张近重复排进
# 前 20，于是两份都入了库，然后互相判 ambiguous 双双永久漏检。识别时 K 大要
# 付延迟（每个候选一次 RANSAC，在 400ms 抽帧的热路径上），入库时一次查询付
# 50 个候选的代价完全可以接受，所以这里不跟着 TOP_K。
DEDUP_TOP_K = 50

# 训词表时最多喂进去的描述子条数。理由（为什么必须有上限、以及怎么抽样）写在
# `PhotoLibrary._sample_descriptors` 里。
#
# 200 万条这个数怎么来的：ORB 是 32 字节/条 = 64MB，XFeat 是 256 字节/条 = 512MB
# —— 后者已经是 `mem_limit: 3g` 上能接受的上限附近，而球面 k-means 每轮还要一个
# (N, branching) 的内积矩阵。真要在更小的机器上跑，调小这个数换来的是词表区分度
# 下降，不是失败。
MAX_TRAIN_DESCRIPTORS = 2_000_000


class LibraryCorrupt(RuntimeError):
    """库目录里几个文件互相不一致。宁可拒绝启动，也不要按错位的 slot 识别。"""


class EmptyLibrary(RuntimeError):
    """库里没有可训练的描述子。见 `PhotoLibrary.train_vocab`。"""


@dataclass(frozen=True)
class VocabTrained:
    """一次 `train_vocab` 的结果。给 CLI 与管理接口如实报告用。"""

    path: Path
    n_photos: int
    n_descriptors: int
    n_words: int
    elapsed_ms: int


def _encode_words(words: np.ndarray, slots: int = _WORDS_SLOTS) -> bytes:
    """定长编码一条词序列。

    `slots` 随后端变（ORB 300 个特征 → 301 槽，XFeat 512 个 → 513 槽）。不参数化的话
    XFeat 的词序列会被截到 300，粗排等于只用了 59% 的词 —— 而这不会报错。
    """
    count = min(int(words.size), slots - 1)
    buf = np.zeros(slots, np.uint32)
    buf[0] = count
    if count:
        buf[1 : 1 + count] = np.asarray(words[:count], np.uint32)
    return buf.tobytes()


def _read_words(path: Path, slots: int = _WORDS_SLOTS) -> list[np.ndarray]:
    if not path.exists():
        return []
    stride = slots * 4
    size = path.stat().st_size
    if size % stride:
        raise LibraryCorrupt(
            f"{path} 大小 {size} 不是词序列步长 {stride} 的整数倍"
        )
    flat = np.fromfile(path, dtype=np.uint32).reshape(-1, slots)
    out = []
    for row in flat:
        count = int(row[0])
        out.append(row[1 : 1 + count].astype(np.int32))
    return out


@dataclass(frozen=True)
class _Snapshot:
    """一致的只读视图。整体替换而不是逐字段改，读侧取一次就不会看到半新半旧。"""

    index: InvertedIndex
    store: DescStore
    photo_ids: tuple[str, ...]
    extra_slots: tuple[int, ...]  # 粗排检索不到、必须无条件并入候选的 slot

    @property
    def n_docs(self) -> int:
        return len(self.photo_ids)


@dataclass(frozen=True)
class Conflict:
    """新照片与库内某张会互相挤成 ambiguous。"""

    photo_id: str
    inliers: int  # 两张原图互查的内点数 m（det 出界记 0，与 dedup 同口径）
    self_score: int  # min(新照片自匹配分, 该照片自匹配分)，ratio 判据的分子


class PhotoLibrary:
    """目录布局：desc.bin / words.bin / index.npz / slots.json。vocab 在目录外。"""

    def __init__(
        self,
        root: str | Path,
        vocab: "VocabLike",
        recog_backend: "Backend | None" = None,
    ) -> None:
        """`recog_backend` 决定提特征/配对/存储布局/阈值这一整套（见 photoar.backend）。

        默认 ORB，与本文件此前的行为逐字节相同 —— 既有调用方与测试不必改一个字。
        `vocab` 必须与后端配套（ORB 配 vocab.Vocab，XFeat 配 floatvocab.FloatVocab），
        配错不会报错，只会让粗排召回崩塌，所以下面立刻校验一次。
        """
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._vocab = vocab
        self._backend = recog_backend or backend_mod.orb_backend()
        expected = self._backend.vocab_cls
        if not isinstance(vocab, expected):
            names = " / ".join(c.__name__ for c in expected)
            raise TypeError(
                f"{self._backend.name} 后端要的词表是 {names}，"
                f"收到 {type(vocab).__name__}。配错不会在别处报错，只会让粗排召回"
                f"崩塌，所以在这里拒绝。"
            )
        self._words_slots = 1 + self._backend.layout.n_features
        self._write_lock = threading.RLock()
        self._snapshot: _Snapshot | None = None
        self._load()

    # ---- 路径 ----

    @property
    def root(self) -> Path:
        return self._root

    @property
    def backend(self) -> "Backend":
        """这个库绑的识别后端。

        暴露出来是给**入库路径**用的（`ingest.ingest_photo`）：它必须用与库同一个
        后端提特征。此前它 `from ..features import extract` 直接用 ORB —— 在 XFeat
        库上，那些 32 字节的 uint8 描述子会被 `descstore.encode_slot` 塞进一个
        512×64 float32 的 slot 里。那一步恰好会因为形状不匹配抛 ValueError（不是
        静默写坏），但报出来的是一句 numpy 广播错误，与"入库用错了后端"毫无关系。
        """
        return self._backend

    @property
    def vocab(self) -> "VocabLike":
        """当前生效的词表。给状态上报用（"跑的是空词表还是训好的词表"）。"""
        return self._vocab

    @property
    def desc_path(self) -> Path:
        return self._root / "desc.bin"

    @property
    def words_path(self) -> Path:
        return self._root / "words.bin"

    @property
    def index_path(self) -> Path:
        return self._root / "index.npz"

    @property
    def slots_path(self) -> Path:
        return self._root / "slots.json"

    # ---- 打开与一致性 ----

    def _read_slots(self) -> list[str]:
        if not self.slots_path.exists():
            return []
        doc = json.loads(self.slots_path.read_text("utf-8"))
        if int(doc.get("version", 0)) != SLOTS_VERSION:
            raise LibraryCorrupt(
                f"{self.slots_path} 的 version={doc.get('version')!r}，"
                f"本程序只认 {SLOTS_VERSION}"
            )
        return [str(p) for p in doc["photo_ids"]]

    def _write_slots(self, photo_ids: list[str]) -> None:
        tmp = self.slots_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"version": SLOTS_VERSION, "photo_ids": photo_ids},
                ensure_ascii=False,
            ),
            "utf-8",
        )
        tmp.replace(self.slots_path)

    def _counts(self) -> tuple[int, int]:
        """(desc.bin 条数, words.bin 条数)。按文件大小算，不解析内容。"""
        n_desc = (
            self.desc_path.stat().st_size // self._backend.layout.stride
            if self.desc_path.exists()
            else 0
        )
        n_words = (
            self.words_path.stat().st_size // (self._words_slots * 4)
            if self.words_path.exists()
            else 0
        )
        return n_desc, n_words

    def _assert_aligned(self, photo_ids: list[str]) -> None:
        """三份记录条数必须相等，否则 photo_id 与 slot 已经错位。

        错位的后果是"照片 A 的描述子挂在照片 B 的 id 上"——识别命中后播的是
        别人的视频，而且没有任何一步会报错。所以这里宁可拒绝继续，也不要在
        错位的库上再追加一张（追加只会把错位固化下来）。
        """
        n_desc, n_words = self._counts()
        if not (len(photo_ids) == n_desc == n_words):
            raise LibraryCorrupt(
                f"库目录三份记录条数不一致：slots.json={len(photo_ids)} "
                f"desc.bin={n_desc} words.bin={n_words}。"
                f"最后一次入库可能被中断，用 photoar-server reindex 修复。"
            )

    def _load(self) -> None:
        photo_ids = self._read_slots()
        if not photo_ids:
            # 空库：不建文件，第一次 add 时才落盘。
            self._snapshot = None
            return

        self._assert_aligned(photo_ids)
        words = _read_words(self.words_path, self._words_slots)

        if self.index_path.exists():
            index = InvertedIndex.load(self.index_path)
            if index.n_docs != len(photo_ids):
                raise LibraryCorrupt(
                    f"index.npz 的 n_docs={index.n_docs} 与 slots.json 的 "
                    f"{len(photo_ids)} 不一致"
                )
        else:
            index = self._build_index(words)
            index.save(self.index_path)

        self._snapshot = self._make_snapshot(index, photo_ids)

    def _make_snapshot(self, index: InvertedIndex, photo_ids: list[str]) -> _Snapshot:
        # unretrievable_docs() 是 O(全部 postings)，绝不能进查询热路径，
        # 所以在这里算一次存进快照。
        #
        # **无条件算**，不再跳过 `n_docs <= TOP_K` 的情形。原来那个跳过是对的：库比
        # TOP_K 小时 `_candidate_slots` 本来就走全查分支，extra_slots 用不上。但
        # `_candidate_slots` 拿到的 `top_k` 是**热配置**里的 `recog.top_k`，而这里
        # 比的是代码常量 TOP_K(20)。把 `recog.top_k` 调到 10、库里有 15 张时，两个
        # 分支就都不成立了：全查分支要 `n_docs <= 10`（不满足），extra_slots 又是空
        # 的（因为 15 <= 20）。有真词表时粗排照样能返回候选，看不出问题；**空词表下
        # `index.query` 恒返回空**（全部 idf 为 0，见 nullvocab.py），于是候选集为空
        # —— 每一次识别都必然未命中，而日志里是一片正常的 200 "未命中"。
        extra = tuple(index.unretrievable_docs())
        return _Snapshot(
            index=index,
            store=DescStore(self.desc_path, self._backend.layout),
            photo_ids=tuple(photo_ids),
            extra_slots=extra,
        )

    def _build_index(self, words: list[np.ndarray]) -> InvertedIndex:
        builder = InvertedIndexBuilder(self._vocab.n_words)
        for w in words:
            builder.add(w)
        return builder.build()

    # ---- 只读查询 ----

    def __len__(self) -> int:
        snap = self._snapshot
        return snap.n_docs if snap else 0

    def photo_ids(self) -> list[str]:
        snap = self._snapshot
        return list(snap.photo_ids) if snap else []

    def slot_of(self, photo_id: str) -> int | None:
        snap = self._snapshot
        if snap is None:
            return None
        try:
            return snap.photo_ids.index(photo_id)
        except ValueError:
            return None

    def features_of(self, photo_id: str) -> Features | None:
        snap = self._snapshot
        if snap is None:
            return None
        slot = self.slot_of(photo_id)
        return None if slot is None else snap.store.read(slot)

    def _candidate_slots(self, snap: _Snapshot, query: Features, top_k: int) -> list[int]:
        if snap.n_docs <= top_k:
            # 库比 Top-K 还小：粗排什么也筛不掉，而 n_docs==1 时它必然返回空
            # （唯一文档的所有词 idf 都是 0，见模块 docstring）。直接全查。
            return list(range(snap.n_docs))
        words = self._vocab.words_of(query.desc)
        slots = [slot for slot, _ in snap.index.query(words, top_k)]
        if snap.extra_slots:
            seen = set(slots)
            slots.extend(s for s in snap.extra_slots if s not in seen)
        return slots

    def verify_candidates(self, img: np.ndarray, top_k: int = TOP_K) -> list[PairResult]:
        """粗排 + 逐候选几何校验，返回全部候选的成对结果（不做 ok 过滤）。

        与 `recognizer.TwoStageRecognizer.verify_candidates` 一样只提一次
        ORB：抽帧频率 400ms，重复提特征是这条路径上最贵的一步。
        """
        return self.verify_features(self._backend.extract_query(img), top_k)

    def verify_features(
        self, query: Features, top_k: int = TOP_K
    ) -> list[PairResult]:
        """特征**已经提好**的那一半（端上提特征的 `POST /v1/recognize/features` 走这条）。

        拆出来而不是让新接口自己抄一遍循环：粗排候选、`extra_slots` 兜底、精排用哪个
        `verify` 全在这几行里，抄一份的后果是两条识别路径的候选集不一样 —— 而两边都
        返回 200、都能命中，只是命中率不同。那种差异要靠上规模跑才看得出来。
        """
        snap = self._snapshot
        if snap is None or len(query) == 0:
            return []
        out = []
        for slot in self._candidate_slots(snap, query, top_k):
            ref = snap.store.read(slot)
            out.append(self._backend.verify(query, ref, snap.photo_ids[slot]))
        return out

    def recognize(self, img: np.ndarray, top_k: int = TOP_K) -> Decision:
        return decide(self.verify_candidates(img, top_k))

    # ---- 增量去重闸门 ----

    def conflicts(
        self,
        features: Features,
        self_score: int,
        known_self_scores: dict[str, int],
        *,
        min_inliers: int | None = None,
        ratio: float = RATIO,
        top_k: int = DEDUP_TOP_K,
    ) -> list[Conflict]:
        """新照片与库内已有照片会不会互相挤成 ambiguous。

        判据与 `dedup.DedupReport.dup_pairs` 逐字相同（那是 5058 张真实语料上
        校准过的版本）：`m >= min_inliers` 且 `min(s_new, s_exist) < ratio * m`。
        `m` 取两个方向的较大值 —— `verify_pair` 并不对称（findHomography 的方向
        与 BFMatcher 的 query/train 角色都不同），任一方向达到阈值就说明这两张
        是同一内容。

        为什么入库必须过这道闸门：`asset.nas_path UNIQUE` 与内容哈希只挡得住
        **字节相同**的重复。重新编码/裁切过的近似重复会两份都入库，然后在识别
        时互相触发 `RATIO=1.5` 判 ambiguous，**两份都永久漏检**，而用户看到的
        现象是"识别器坏了"。小语料实测：不清理 93.75% 命中 / 32.7% 库外假阳性，
        清理后 98.96% / 0%。

        `known_self_scores` 由调用方从 catalog 取（自匹配分要跑 20 次扰动查询
        才能算出，入库时算一次存库）。缺了某张的分数就当它极低 —— 宁可多报一次
        冲突让用户确认，也不要因为查不到分数就放行。
        """
        # 默认阈值跟后端走（ORB 25 / XFeat 38）：两个后端的内点数分布是两个不同的
        # 量，沿用另一边的数会让去重闸门实际变松或变紧，而这不会报错。
        if min_inliers is None:
            min_inliers = self._backend.dedup_min_inliers
        snap = self._snapshot
        if snap is None or len(features) == 0:
            return []
        out: list[Conflict] = []
        for slot in self._candidate_slots(snap, features, top_k):
            pid = snap.photo_ids[slot]
            ref = snap.store.read(slot)
            m = 0
            for a, b in ((features, ref), (ref, features)):
                r = self._backend.verify(a, b, pid)
                score = r.inliers if DET_MIN <= r.det <= DET_MAX else 0
                m = max(m, score)
            if m < min_inliers:
                continue
            s = min(self_score, known_self_scores.get(pid, 0))
            if s >= ratio * m:
                continue  # ratio test 两个方向都通得过，不会混淆
            out.append(Conflict(photo_id=pid, inliers=m, self_score=s))
        return sorted(out, key=lambda c: (-c.inliers, c.photo_id))

    # ---- 写入 ----

    def add(
        self, photo_id: str, features: Features, *, defer_reindex: bool = False
    ) -> int:
        """把一张照片追加进库，返回 slot。默认立即重建倒排索引。

        `defer_reindex=True` 用于批量入库：追加完全部照片再调一次 `reindex()`，
        把 O(全库) 的重建从"每张一次"降到"一次"。**在调用 reindex() 之前，库处于
        新照片查不到的状态**（desc/words 已落盘，index 还是旧的）—— 这是刻意的
        可见状态而不是隐藏的不一致：`_load()` 会检查三份记录条数，重建缺失时启动
        就会报出来。
        """
        with self._write_lock:
            # 以磁盘上的 slots.json 为准，不用 `self.photo_ids()`（那是快照）。
            # defer_reindex=True 时快照故意不更新，拿它当基准会让每次 add 都从
            # 同一个旧列表续写，slots.json 被覆盖成只有一条，而 desc.bin 已经
            # 长了 N 条 —— photo_id 与 slot 从此错位。
            photo_ids = self._read_slots()
            if photo_id in set(photo_ids):
                raise ValueError(f"photo_id 已在库中：{photo_id}")
            self._assert_aligned(photo_ids)
            slot = descstore.append_slot(
                self.desc_path, features, self._backend.layout
            )
            words = self._vocab.words_of(features.desc)
            with open(self.words_path, "ab") as fh:
                fh.write(_encode_words(words, self._words_slots))
            photo_ids.append(photo_id)
            self._write_slots(photo_ids)
            if defer_reindex:
                # 快照先不动：读侧继续用旧索引，看不到这张新照片，但也绝不会
                # 读到错位的 slot（旧快照持有旧的 DescStore mmap）。
                return slot
            self._reindex_locked(photo_ids)
            return slot

    def reindex(self, *, rebuild_words: bool = False) -> None:
        """重建倒排索引。`rebuild_words=True` 时先用当前 vocab 重新量化全库描述子。

        换 vocab 后必须 `rebuild_words=True`：words.bin 里的词 id 属于旧词表，
        拿新词表的 idf 去解释旧词 id 不会报错，只会让粗排召回率静默崩塌。
        """
        with self._write_lock:
            photo_ids = self._read_slots()
            if rebuild_words:
                store = DescStore(self.desc_path, self._backend.layout)
                try:
                    with open(self.words_path, "wb") as fh:
                        for slot in range(len(store)):
                            fh.write(
                                _encode_words(
                                    self._vocab.words_of(store.read(slot).desc),
                                    self._words_slots,
                                )
                            )
                finally:
                    store.close()
            self._reindex_locked(photo_ids)

    # ---- 训词表 ----

    def train_vocab(
        self, out_path: str | Path, *, max_descriptors: int = MAX_TRAIN_DESCRIPTORS
    ) -> "VocabTrained":
        """从**库里已有的**描述子训一份词表，存盘，然后用它重建全库词序列与倒排索引。

        为什么这件事必须在 `PhotoLibrary` 里、而不是在 CLI 或 HTTP 层拼起来：它是
        "训 → 存盘 → 换掉 self._vocab → reindex(rebuild_words=True)"四步，中间任何
        一步之后停下来，库都处于一个**不报错但静默错**的状态：
        - 训完存盘、没换 `self._vocab` → 磁盘上是新词表，进程里还是旧的，下次重启
          才生效，而重启后 `words.bin` 里的词 id 属于旧词表。
        - 换了词表、没 `rebuild_words` → `words.bin` 是旧词 id，新词表的 idf 去解释
          旧词 id，粗排召回**静默崩塌**（`reindex` 的 docstring 里也写着这条）。
        所以四步必须在同一把 `_write_lock` 下走完，而那把锁是本类的私有状态。

        库为空时**报错而不是训一个空词表**：`vocab.train` 拿 0 个描述子会抛，而就算
        它不抛，训出来的也是一棵只有根的树，`words_of` 恒返回 0 —— 那正好等于
        `NullVocab`，但它会**被存成一个文件**，从此"词表文件在不在"这个判据永久失效
        （见 nullvocab.py 里 `save` 为什么拒绝写盘）。

        返回值里带上描述子条数与耗时，是为了让 `POST /v1/admin/rebuild-vocab` 与
        `photoar-server build-vocab` 能如实报告"这份词表是用多少数据训出来的" ——
        库里只有 3 张照片时训出来的词表能用但没什么区分度，而那件事只有这个数字
        能说明。
        """
        out_path = Path(out_path)
        t0 = time.perf_counter()
        with self._write_lock:
            photo_ids = self._read_slots()
            if not photo_ids:
                raise EmptyLibrary(
                    "库里一张照片都没有，训不出词表。先入库几十张（越多越好，"
                    "几百张起就有意义），再训。在此之前服务用的是空词表 —— "
                    "识别结果正确，只是每次都全量扫描。"
                )
            self._assert_aligned(photo_ids)
            desc = self._sample_descriptors(len(photo_ids), max_descriptors)
            new_vocab = self._backend.train_vocab(desc)
            new_vocab.save(out_path)
            # 先存盘再换内存里那份：反过来的话，`save` 失败（磁盘满、目录只读）会留下
            # 一个"进程里用着一份磁盘上不存在的词表"的服务 —— 它能正常识别，直到重启
            # 那一刻突然退回空词表，而没有任何一条日志把两件事联系起来。
            self._vocab = new_vocab
            # rebuild_words=True 是**必须**的，不是保险：`words.bin` 里现在存的是旧
            # 词表（多半是空词表的全 0）的词 id。
            store = DescStore(self.desc_path, self._backend.layout)
            try:
                with open(self.words_path, "wb") as fh:
                    for slot in range(len(store)):
                        fh.write(
                            _encode_words(
                                self._vocab.words_of(store.read(slot).desc),
                                self._words_slots,
                            )
                        )
            finally:
                store.close()
            self._reindex_locked(photo_ids)
        return VocabTrained(
            path=out_path,
            n_photos=len(photo_ids),
            n_descriptors=int(desc.shape[0]),
            n_words=int(new_vocab.n_words),
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    def _sample_descriptors(self, n_photos: int, max_descriptors: int) -> np.ndarray:
        """从全库均匀取最多 `max_descriptors` 条描述子。

        **必须有上限。** 直接把全库描述子 vstack 起来看着更简单，但 XFeat 一张照片是
        512×64 float32 = 131KB，1 万张就是 1.3GB 常驻内存 —— 而这段代码跑在一台
        `mem_limit: 3g` 的 NAS 上、服务同时还在对外提供识别。被 OOM killer 挑走的
        大概率不是它自己而是正在转码的 ffmpeg。

        按**每张照片配额**取，而不是"取前 N 张照片的全部描述子"：后者会让词表只认识
        库里那一部分照片的内容分布（比如按入库时间排序的话，就是最早那几十张）。

        配额内**随机**取行，不取前 quota 条：ORB 的 `extract` 返回的特征是按响应值
        排序的，取前面几条等于系统性地只用最强的那批特征训词表。查询帧里恰恰有大量
        弱特征，它们会被量化到没有中心靠近的叶子上 —— 粗排召回下降，而这不会报错。
        seed 固定，同一个库训两次得到同一份词表（排查时能对照）。
        """
        quota = max(1, max_descriptors // max(1, n_photos))
        rng = np.random.default_rng(0)
        chunks: list[np.ndarray] = []
        store = DescStore(self.desc_path, self._backend.layout)
        try:
            for slot in range(len(store)):
                d = store.read(slot).desc
                if d.shape[0] == 0:
                    continue
                if d.shape[0] > quota:
                    rows = rng.choice(d.shape[0], size=quota, replace=False)
                    d = d[np.sort(rows)]
                chunks.append(np.ascontiguousarray(d))
        finally:
            store.close()
        if not chunks:
            raise EmptyLibrary(
                "库里的照片一条描述子都没有（desc.bin 里全是空 slot）。"
                "这说明入库时提特征失败过，用 photoar-server check 对一下账。"
            )
        return np.vstack(chunks)

    def _reindex_locked(self, photo_ids: list[str]) -> None:
        index = self._build_index(_read_words(self.words_path, self._words_slots))
        index.save(self.index_path)
        # 整体替换快照。旧快照被读侧的局部变量持有着，它的 mmap 不会在读到
        # 一半时失效——这是这里不显式 close() 旧 store 的原因。
        self._snapshot = self._make_snapshot(index, photo_ids)
