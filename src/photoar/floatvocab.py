"""浮点描述子的层次词汇树（球面 k-means）。

`photoar.vocab` 是给 ORB 的二进制描述子用的（Hamming 分配 + 逐 bit 多数表决）。
XFeat 的描述子是已 L2 归一化的 64 维 float32，Hamming 无从下手，对应的算法是**球面
k-means**：余弦相似度分配、簇内向量求和再归一化作新中心。

**为什么粗排还要词汇树，而不是换成一个全局描述子。** 本来打算用 DINOv2 ViT-S/14 出
384 维全局向量做暴力余弦检索（它 Apache-2.0、有官方非 HF 直链、CPU 43ms、1 万张暴力
检索只要 0.17ms，很诱人）。放弃的理由是**这个场景的查询长什么样**：相机帧里是「一张
打印照片 + 一大片桌面/墙面/人手」，照片可能只占画面三成。整帧的全局向量会被背景主导，
同一张照片在不同背景下拿到的向量彼此不像 —— 检索直接退化。

局部描述子 + 倒排索引对杂乱天然鲁棒（背景的词只是投票里的噪声，照片自己的词照样把
正确文档顶上去），而且这条路在本项目这个场景里**已经实测过**：ORB + 二进制词汇树在
真实语料上做到命中 95.70%、真实误识别 0.000%。换掉特征提取器是有依据的改进，换掉
整个检索结构不是。

与 `vocab.Vocab` 的接口刻意保持一致（`n_words` / `words_of` / `save` / `load` /
`train`），这样 `InvertedIndex`、`PhotoLibrary`、`TwoStageRecognizer` 一行都不用改就能
换后端。两个类**没有**共同基类：它们的内部表示（uint8 中心 vs float32 中心）与距离
函数完全不同，抽一个基类出来只会多一层间接而不省任何代码。

`words_of` 这一版是**向量化**的（按节点分组批量算内积），而不是像二进制版那样逐描述子
Python 循环。二进制版那个循环是 `library.py` 里"重建索引要 30 分钟"的主要来源；浮点版
没有理由重复这个错误。
"""

from pathlib import Path

import numpy as np

# 分支数与深度。沿用二进制版扇扫出来的结论：**要提召回先提分支数，别提深度**
# （10/5 与 16/4 词数几乎相同但 R@20 差 1.6pp，因为每一层都是一次走错分支的机会）。
# 所以这里直接取二进制版的峰值配置 16/4，没有另做一次扇扫。
#
# ⚠️ 这是**移植过来的结论，不是在 XFeat 描述子上重新量过的**。要调的话用
# bench/measure_0b_vocab_sweep.py 同样的方法在 XFeat 描述子上重扫一遍。
BRANCHING = 16
DEPTH = 4
KMEANS_ITERS = 8
MIN_DESC_PER_NODE = 8  # 少于此数不再细分，与二进制版同值


def _normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(n, 1e-12)


def _spherical_kmeans(
    desc: np.ndarray, k: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (centers:(k',D) float32, labels:(N,) int64)。k' 可能小于 k。

    空簇处理与二进制版 `_kmajority` 保持同一策略：重新播种到「离该中心最远」的那个
    描述子。不这么做的话簇会逐轮塌缩，词表实际词数远小于声明值，而这不会报错 ——
    只会让粗排的区分度静默变差。
    """
    n = desc.shape[0]
    k = min(k, n)
    centers = desc[rng.choice(n, size=k, replace=False)].copy()

    labels = np.zeros(n, np.int64)
    for _ in range(KMEANS_ITERS):
        labels = (desc @ centers.T).argmax(axis=1)
        moved = False
        for c in range(centers.shape[0]):
            members = desc[labels == c]
            if members.shape[0] == 0:
                far = int((desc @ centers[c]).argmin())
                new_center = desc[far].copy()
            else:
                new_center = _normalize(members.sum(axis=0))
            if not np.allclose(new_center, centers[c], atol=1e-7):
                centers[c] = new_center
                moved = True
        if not moved:
            break
    labels = (desc @ centers.T).argmax(axis=1)
    return centers.astype(np.float32), labels


class FloatVocab:
    """层次词汇树。内部用扁平数组存节点，便于序列化。

    centers[i]   第 i 个节点的中心（已 L2 归一化）
    children[i]  第 i 个节点的子节点下标数组；空数组表示叶子
    leaf_id[i]   叶子节点的词 id；非叶子为 -1
    """

    def __init__(
        self,
        centers: np.ndarray,
        children: list[np.ndarray],
        leaf_id: np.ndarray,
        root_children: np.ndarray,
        n_words: int,
    ) -> None:
        self._centers = np.ascontiguousarray(centers, np.float32)
        self._children = children
        self._leaf_id = leaf_id
        self._root_children = root_children
        self._n_words = int(n_words)

    @property
    def n_words(self) -> int:
        return self._n_words

    @property
    def desc_dim(self) -> int:
        return int(self._centers.shape[1]) if self._centers.size else 0

    def words_of(self, desc: np.ndarray) -> np.ndarray:
        """把一批描述子量化成词 id。

        按节点分组批量下降：每访问一个节点，就对"落在这个节点上的那批描述子"一次性
        算内积。访问的节点数与树规模同阶，但每次都是一个矩阵乘，而不是每个描述子各走
        一遍 Python 循环。
        """
        if desc.shape[0] == 0:
            return np.zeros((0,), np.int32)
        x = np.ascontiguousarray(desc, np.float32)
        out = np.zeros(x.shape[0], np.int32)

        # (节点下标, 落在该节点的描述子行号)。-1 代表根（根本身不是一个节点）。
        stack: list[tuple[int, np.ndarray]] = [(-1, np.arange(x.shape[0]))]
        while stack:
            node, rows = stack.pop()
            kids = self._root_children if node < 0 else self._children[node]
            if kids.size == 0:
                # 叶子。根就是叶子只会发生在空树上，此时词 id 记 0（与二进制版一致）。
                out[rows] = int(self._leaf_id[node]) if node >= 0 else 0
                continue
            best = (x[rows] @ self._centers[kids].T).argmax(axis=1)
            for c in range(kids.size):
                sel = rows[best == c]
                if sel.size:
                    stack.append((int(kids[c]), sel))
        return out

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        flat = (
            np.concatenate(self._children) if self._children else np.zeros(0, np.int32)
        )
        lengths = np.array([c.size for c in self._children], np.int32)
        np.savez_compressed(
            path,
            centers=self._centers,
            children_flat=flat.astype(np.int32),
            children_len=lengths,
            leaf_id=self._leaf_id,
            root_children=self._root_children.astype(np.int32),
            n_words=np.array([self._n_words], np.int64),
        )

    @classmethod
    def load(cls, path: str | Path) -> "FloatVocab":
        z = np.load(Path(path))
        lengths = z["children_len"]
        flat = z["children_flat"]
        children, cursor = [], 0
        for length in lengths:
            children.append(flat[cursor : cursor + int(length)])
            cursor += int(length)
        return cls(
            centers=z["centers"],
            children=children,
            leaf_id=z["leaf_id"],
            root_children=z["root_children"],
            n_words=int(z["n_words"][0]),
        )


def train(
    descriptors: np.ndarray,
    branching: int = BRANCHING,
    depth: int = DEPTH,
    seed: int = 0,
) -> FloatVocab:
    """在一堆描述子上训词表。

    描述子会先被强制 L2 归一化：球面 k-means 与 `words_of` 都用内积当相似度，没归一化
    的话内积不是余弦，长向量会无条件赢 —— 而这不会报错。XFeat 的输出本来就归一化过，
    这里再做一次是为了让这个函数对手工造的输入也成立。
    """
    if descriptors.shape[0] == 0:
        raise ValueError("训练词汇树需要至少一个描述子")
    if descriptors.ndim != 2:
        raise ValueError(f"描述子必须是二维 (N, D)，收到 {descriptors.shape}")

    x = _normalize(np.ascontiguousarray(descriptors, np.float32))
    dim = x.shape[1]

    rng = np.random.default_rng(seed)
    centers_list: list[np.ndarray] = []
    children: list[np.ndarray] = []
    leaf_id_list: list[int] = []
    next_word = 0

    def build(subset: np.ndarray, level: int) -> np.ndarray:
        nonlocal next_word
        if subset.shape[0] == 0:
            return np.zeros(0, np.int32)

        centers, labels = _spherical_kmeans(subset, branching, rng)
        node_ids = []
        for c in range(centers.shape[0]):
            node_id = len(centers_list)
            centers_list.append(centers[c])
            children.append(np.zeros(0, np.int32))
            leaf_id_list.append(-1)
            node_ids.append(node_id)

            members = subset[labels == c]
            can_split = level + 1 < depth and members.shape[0] >= MIN_DESC_PER_NODE
            if can_split:
                children[node_id] = build(members, level + 1)
            if children[node_id].size == 0:
                leaf_id_list[node_id] = next_word
                next_word += 1
        return np.array(node_ids, np.int32)

    root_children = build(x, 0)
    return FloatVocab(
        centers=np.array(centers_list, np.float32).reshape(-1, dim),
        children=children,
        leaf_id=np.array(leaf_id_list, np.int32),
        root_children=root_children,
        n_words=max(1, next_word),
    )
