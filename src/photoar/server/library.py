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
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import descstore
from ..descstore import DescStore
from ..features import N_FEATURES, Features
from ..index import InvertedIndex, InvertedIndexBuilder
from ..recognizer import TOP_K
from ..verify import (
    DEDUP_MIN_INLIERS,
    DET_MAX,
    DET_MIN,
    RATIO,
    Decision,
    PairResult,
    decide,
    verify_pair,
)
from ..vocab import Vocab

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


class LibraryCorrupt(RuntimeError):
    """库目录里几个文件互相不一致。宁可拒绝启动，也不要按错位的 slot 识别。"""


def _encode_words(words: np.ndarray) -> bytes:
    count = min(int(words.size), N_FEATURES)
    buf = np.zeros(_WORDS_SLOTS, np.uint32)
    buf[0] = count
    if count:
        buf[1 : 1 + count] = np.asarray(words[:count], np.uint32)
    return buf.tobytes()


def _read_words(path: Path) -> list[np.ndarray]:
    if not path.exists():
        return []
    size = path.stat().st_size
    if size % _WORDS_STRIDE:
        raise LibraryCorrupt(
            f"{path} 大小 {size} 不是词序列步长 {_WORDS_STRIDE} 的整数倍"
        )
    flat = np.fromfile(path, dtype=np.uint32).reshape(-1, _WORDS_SLOTS)
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

    def __init__(self, root: str | Path, vocab: Vocab) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._vocab = vocab
        self._write_lock = threading.RLock()
        self._snapshot: _Snapshot | None = None
        self._load()

    # ---- 路径 ----

    @property
    def root(self) -> Path:
        return self._root

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
            self.desc_path.stat().st_size // descstore.SLOT_STRIDE
            if self.desc_path.exists()
            else 0
        )
        n_words = (
            self.words_path.stat().st_size // _WORDS_STRIDE
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
        words = _read_words(self.words_path)

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
        extra = tuple(index.unretrievable_docs()) if index.n_docs > TOP_K else ()
        return _Snapshot(
            index=index,
            store=DescStore(self.desc_path),
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
        from ..features import extract  # 局部 import：让 CLI 的 --help 不必加载 cv2

        snap = self._snapshot
        if snap is None:
            return []
        query = extract(img)
        if len(query) == 0:
            return []
        out = []
        for slot in self._candidate_slots(snap, query, top_k):
            ref = snap.store.read(slot)
            out.append(verify_pair(query, ref, snap.photo_ids[slot]))
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
        min_inliers: int = DEDUP_MIN_INLIERS,
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
        snap = self._snapshot
        if snap is None or len(features) == 0:
            return []
        out: list[Conflict] = []
        for slot in self._candidate_slots(snap, features, top_k):
            pid = snap.photo_ids[slot]
            ref = snap.store.read(slot)
            m = 0
            for a, b in ((features, ref), (ref, features)):
                r = verify_pair(a, b, pid)
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
            slot = descstore.append_slot(self.desc_path, features)
            words = self._vocab.words_of(features.desc)
            with open(self.words_path, "ab") as fh:
                fh.write(_encode_words(words))
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
                store = DescStore(self.desc_path)
                try:
                    with open(self.words_path, "wb") as fh:
                        for slot in range(len(store)):
                            fh.write(
                                _encode_words(
                                    self._vocab.words_of(store.read(slot).desc)
                                )
                            )
                finally:
                    store.close()
            self._reindex_locked(photo_ids)

    def _reindex_locked(self, photo_ids: list[str]) -> None:
        index = self._build_index(_read_words(self.words_path))
        index.save(self.index_path)
        # 整体替换快照。旧快照被读侧的局部变量持有着，它的 mmap 不会在读到
        # 一半时失效——这是这里不显式 close() 旧 store 的原因。
        self._snapshot = self._make_snapshot(index, photo_ids)
