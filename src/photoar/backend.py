"""识别后端：把「用哪套特征」收成一个可切换的对象。

一个后端要同时决定四件事，它们必须成套换，换错一个就静默失配：

  1. **提特征**（ORB 的 256bit 二值 / XFeat 的 64 维 float32）
  2. **配对**（Hamming + crossCheck / 余弦互近邻）
  3. **描述子存储布局**（`descstore.SlotLayout`，步长与 dtype 都不同）
  4. **词表种类与内点数阈值**（二进制 k-majority / 浮点球面 k-means；40 / 60）

所以它们不是四个独立开关，而是一个后端对象上的四个属性。库文件（desc.bin /
words.bin / index.npz）**与后端一一绑定**：换后端等于全库描述子作废，必须重建，
所以两个后端落在不同的库目录下（见 `ServerConfig.library_dir_for`）。

为什么 ORB 那条路完整保留、而不是删掉：它是**已经通过出口条件的基线**（真实语料上
命中 95.70%、真实误识别 0.000%、P95 67.7ms）。XFeat 在纸质翻拍上应当更强，但那是
论文与间接证据支撑的预期，不是在这个项目的语料上量过的结论。留着 ORB，是为了
(a) 任何时候能一条配置退回已知可用的状态；(b) 让"换特征"这一步的收益与代价能被
单独量出来，而不是把两个变量混在一起。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from . import descstore, features, floatvocab, nullvocab, verify, vocab, xfeat
from .features import Features
from .verify import PairResult

# 后端名。写进配置与 API 响应，别改字面量 —— 改了等于让已有部署的配置失效。
ORB = "orb"
XFEAT = "xfeat"
NAMES = (ORB, XFEAT)


class VocabLike(Protocol):
    """两种词表的公共形状。刻意用 Protocol 而不是基类：两者内部表示与距离函数完全
    不同，抽基类只会多一层间接而不省任何代码。"""

    @property
    def n_words(self) -> int: ...

    def words_of(self, desc: np.ndarray) -> np.ndarray: ...

    def save(self, path: str | Path) -> None: ...


@dataclass(frozen=True)
class Backend:
    name: str
    layout: descstore.SlotLayout
    min_inliers: int
    dedup_min_inliers: int
    vocab_file: str  # 词表文件名，两种后端不能同名（内容格式不兼容）
    # 用来在 PhotoLibrary 构造时当场拒绝配错的词表。
    #
    # 是**元组**而不是单个类，因为除了本后端自己那种词表，还必须放行
    # `nullvocab.NullVocab` —— 全新部署时词表文件根本不存在（它是用用户自己的照片
    # 训的），此时库里装的是空词表，行为退化成全量扫描但结果正确（推理见
    # nullvocab.py 的模块 docstring）。不放行的话服务在第一次启动时就 TypeError，
    # 而用户没有任何办法先训出词表来 —— 训词表需要库里已有描述子，入库需要服务
    # 起来。那是一个真死锁。
    vocab_cls: tuple[type, ...]
    _extract: Any
    _verify: Any
    _train_vocab: Any
    _load_vocab: Any
    # 查询帧专用的提特征。`None` 表示"和入库那边用同一个"（XFeat 走这条：它的
    # 提取器把 TOP_K 写死在模型输出里，没有可调的预算）。为什么查询侧要能和入库侧
    # 不一样，见 `QUERY_N_FEATURES` 的注释。
    _extract_query: Any = None

    def extract(self, img_bgr: np.ndarray) -> Features:
        """**入库**用的提特征。它的特征数决定 `descstore` 的槽位宽度，动它等于全库作废。"""
        return self._extract(img_bgr)

    def extract_query(self, img_bgr: np.ndarray) -> Features:
        """**查询帧**用的提特征。预算与入库侧解耦，见 `QUERY_N_FEATURES`。"""
        return (self._extract_query or self._extract)(img_bgr)

    def verify(self, query: Features, ref: Features, photo_id: str) -> PairResult:
        return self._verify(query, ref, photo_id)

    def train_vocab(self, descriptors: np.ndarray) -> VocabLike:
        return self._train_vocab(descriptors)

    def load_vocab(self, path: str | Path) -> VocabLike:
        return self._load_vocab(path)


# 查询帧提多少个 ORB 特征。入库侧仍然是 `features.N_FEATURES`(300) —— 两边**故意
# 不一样**，这是识别率的主导变量，不是笔误。
#
# 为什么必须不一样：入库时照片是**铺满**画面的，300 个特征全落在照片上。手持扫描时
# 照片只占画面的一部分（实测自然举手距离约 0.4~0.5），同样的 300 个特征要摊在整个
# 画面上 —— 桌面、墙面、旁边的杂物都在抢预算，落到照片上的可能只剩几十个。
#
# 在用户的真实素材（708×468 婚礼照，真实桌面背景，5 个随机视角）上实测，
# 判据取"5 个视角**全部**过门槛"（只有一个过等于让用户靠运气）：
#
# ⚠️ **下表那个"全过"是几个视角抽出来的，不足以支撑"这一档稳了"。** 同一张图把
# 视角数抬到 20 之后，占比 0.40 那一档的分数是 39~150（跨度 4 倍），而门槛是 40 ——
# 有 1/20 落在 39，差 1 分。也就是说"0.4 全过"是抽到的那几个视角恰好都过。
# 这与 `verify.MIN_INLIERS` 那段写的真阳性 p1=9／p5=53 一致（门槛 40 天生吃掉
# 1%~5% 的真阳性）。要照着这张表往下调任何数之前，先读 bench/README.md 里
# 「repeat=3 是虚假的精度」那一节，并用 `--repeat 20` 以上重量一遍。
#
# 下表里"长边 X"是**发帧与处理同为 X**（两者可以不同，见 `QUERY_LONG_EDGE`）：
#
#   配置                              全过的最小占比
#   长边 640 + 300 特征（原状态）      —— 一档都不全过，0.6 才偶尔过
#   长边 640 + 800 特征                0.8
#   长边 1280 + 300 特征               —— 长边变大但预算没变，几乎没变化
#   长边 1280 + 2000 特征              0.5
#   长边 1280 + 4000 特征              0.4   ← 这里
#   长边 1280 + 8000 特征              —— 反而退化（弱特征稀释了 ratio test）
#   长边 1600 + 2000 特征              0.5（长边与预算不配比，白付流量）
#
# 代价都量过了，三项都在预算内（120 张纹理库、952 个错配对、占比 0.4）：
#   * 误识别：错配内点上限 12 → 11，跨过门槛 40 的**仍然是 0 个**，没有恶化。
#   * 提特征：3.3ms → 13.8ms；单个候选精排 2.63ms → 4.54ms（参考侧仍 300，所以
#     BFMatcher 是 4000×300 而不是 4000×4000）。对着 10s 的识别容忍度可以忽略。
#   * 粗排（这一项是**最大**的赢家，也是原来真机全扫不出来的主因）：正确答案落进
#     Top-20 候选集的比例 5/20 → 20/20。粗排漏了，精排再强也看不到那张照片。
#
# 客户端发帧长边也同步抬到 1280（见 `Frames.kt`）：光靠服务端放大能到 0.5，配上
# 真实的 1280 像素才到 0.4。
QUERY_N_FEATURES = 4000

# 服务端处理长边。`features.extract` 默认会先把帧缩到 `features.LONG_EDGE`(640)
# 再提特征，所以这个值必须显式传 —— 而且它是**比发帧长边更主导**的那一个变量。
# 同一张真实婚礼照 + 真实桌面场景，5 个随机视角取"全部过门槛"：
#
#   发帧    处理    全过的最小占比
#   640     640     一档都不全过        ← 原状态
#   1280    640     一档都不全过        ← 只改客户端发帧，等于完全没改
#   640     1280    0.5
#   1280    1280    0.4                 ← 现在这一档
#   1280    1600    0.4（持平，白花 CPU）
#   1280    1920    退化回一档都不全过
#
# 注意第三行：帧比这个值**小的时候会被放大**（`resize_to_long_edge` 对 scale > 1
# 走 INTER_LINEAR），而且实测放大是**有收益**的 —— 不是凭空造信息，是把查询侧的
# 尺度对回入库侧（入库时照片铺满 640，手持时照片只占画面的一小块）。所以这里
# **故意不加"禁止放大"的保护**：老客户端发 640 的帧，升到 1280 照样能识别到 0.5。
QUERY_LONG_EDGE = 1280


def _extract_orb_query(img_bgr: np.ndarray) -> Features:
    return features.extract(
        img_bgr, long_edge=QUERY_LONG_EDGE, n_features=QUERY_N_FEATURES
    )


def orb_backend() -> Backend:
    return Backend(
        name=ORB,
        layout=descstore.ORB_LAYOUT,
        min_inliers=verify.MIN_INLIERS,
        dedup_min_inliers=verify.DEDUP_MIN_INLIERS,
        vocab_file="vocab.npz",
        vocab_cls=(vocab.Vocab, nullvocab.NullVocab),
        _extract=features.extract,
        _verify=verify.verify_pair,
        _train_vocab=vocab.train,
        _load_vocab=vocab.Vocab.load,
        _extract_query=_extract_orb_query,
    )


# XFeat 的存储布局：512 个 64 维 float32 描述子 + 512 个 xy。
# 每张 8 + 512*8 + 512*64*4 = 135,176 字节，是 ORB（12,008）的 11.3 倍。
# 1 万张约 1.35GB —— 仍然只在精排时按 slot 随机读，不常驻内存（mmap），所以这个体积
# 影响的是磁盘而不是内存。写进文档，因为它足以改变 NAS 上的容量规划。
XFEAT_LAYOUT = descstore.SlotLayout(
    n_features=xfeat.TOP_K, desc_dim=xfeat.DESC_DIM, desc_dtype=np.float32
)


def xfeat_backend(model_path: str | Path | None = None) -> Backend:
    """构造 XFeat 后端。会**立刻**加载 ONNX 会话。

    立刻加载而不是懒加载，是为了让"模型不在"这个错误在服务启动时就暴露，而不是等到
    第一次入库或第一次识别 —— 后者会让用户以为是照片的问题。
    """
    extractor = xfeat.XFeatExtractor(model_path)
    return Backend(
        name=XFEAT,
        layout=XFEAT_LAYOUT,
        min_inliers=verify.XFEAT_MIN_INLIERS,
        dedup_min_inliers=verify.XFEAT_DEDUP_MIN_INLIERS,
        vocab_file="vocab_xfeat.npz",
        vocab_cls=(floatvocab.FloatVocab, nullvocab.NullVocab),
        _extract=extractor.extract,
        _verify=verify.verify_pair_xfeat,
        _train_vocab=floatvocab.train,
        _load_vocab=floatvocab.FloatVocab.load,
    )


def make(name: str, *, model_path: str | Path | None = None) -> Backend:
    if name == ORB:
        return orb_backend()
    if name == XFEAT:
        return xfeat_backend(model_path)
    raise ValueError(f"未知识别后端：{name!r}，可选 {NAMES}")
