"""二进制层次词汇树（k-majority）。

ORB 描述子是 256 bit 二进制，普通 k-means（欧氏均值）不适用，需要用
Hamming 距离分配 + 逐 bit 多数表决更新中心，即 k-majority。

分层的目的是把量化代价从 O(n_words) 降到 O(branching * depth)：
10000 词的扁平词表要比 10000 次，4 层 10 分支只要比 40 次。

spec §8.2 原本要求先用 ORB-SLAM 的现成 ORBvoc.txt。本项目改为自训小
词汇表，因为 Task 5 的暴力检索已经提供了一个不含词汇表变量的更硬基线；
ORBvoc 保留为召回不达标时的备选，届时只需另写一个提供 words_of 的类。
"""

from pathlib import Path

import numpy as np

from .features import DESC_BYTES

# 词汇树分支数。0b 实测在 1000 张库 / 500 个共用查询图上扇扫 8 个配置：
#   6/3   216 词  R@20 83.00%      10/4 10000 词 R@20 96.00%
#   8/3   512 词  R@20 88.40%      12/4 20570 词 R@20 95.60%
#   6/4  1296 词  R@20 92.00%      16/4 57850 词 R@20 97.60%  <- 峰值
#                                  10/5 60135 词 R@20 96.00%
# 两条结论：(1) 召回率随词表变细上升，调粗会把 R@20 从 96% 打到 83%；
# (2) 不是词数决定论——10/5 与 16/4 词数几乎相同但 R@20 差 1.6pp，因为每一层
#     都是一次走错分支的机会，层数越少越好。所以要提召回先提 BRANCHING，别提 DEPTH。
# 代价：词表训练 5.3s -> 8.2s。端到端从 94.80% 升到 96.00%（误识别始终为 0）。
BRANCHING = 16
DEPTH = 4
KMAJORITY_ITERS = 8
MIN_DESC_PER_NODE = 8  # 少于此数不再细分

_POPCOUNT = (
    np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1)
    .sum(axis=1)
    .astype(np.uint16)
)


def hamming_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """成对 Hamming 距离。a:(N,32) b:(M,32) -> (N,M) uint16。

    调用方需保证 N*M 不会大到爆内存；本项目里 M 恒等于 branching（<=10）。
    """
    if a.shape[0] == 0 or b.shape[0] == 0:
        return np.zeros((a.shape[0], b.shape[0]), np.uint16)
    xor = np.bitwise_xor(a[:, None, :], b[None, :, :])
    return _POPCOUNT[xor].sum(axis=2).astype(np.uint16)


def _majority(descs: np.ndarray) -> np.ndarray:
    """逐 bit 多数表决，得到一个 (32,) uint8 的中心。"""
    bits = np.unpackbits(descs, axis=1)
    return np.packbits((bits.mean(axis=0) >= 0.5).astype(np.uint8))


def _kmajority(
    descs: np.ndarray, k: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (centers:(k',32) uint8, labels:(N,) int)。k' 可能小于 k。"""
    n = descs.shape[0]
    k = min(k, n)
    centers = descs[rng.choice(n, size=k, replace=False)].copy()

    labels = np.zeros(n, np.int64)
    for _ in range(KMAJORITY_ITERS):
        labels = hamming_matrix(descs, centers).argmin(axis=1)
        moved = False
        for c in range(centers.shape[0]):
            members = descs[labels == c]
            if members.shape[0] == 0:
                # 空簇：重新播种到离自己最远的那个描述子
                far = hamming_matrix(descs, centers[c : c + 1])[:, 0].argmax()
                new_center = descs[far].copy()
            else:
                new_center = _majority(members)
            if not np.array_equal(new_center, centers[c]):
                centers[c] = new_center
                moved = True
        if not moved:
            break
    labels = hamming_matrix(descs, centers).argmin(axis=1)
    return centers, labels


class Vocab:
    """层次词汇树。内部用扁平数组存节点，便于序列化。

    centers[i]   第 i 个节点的中心描述子
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
        self._centers = centers
        self._children = children
        self._leaf_id = leaf_id
        self._root_children = root_children
        self._n_words = int(n_words)

    @property
    def n_words(self) -> int:
        return self._n_words

    def words_of(self, desc: np.ndarray) -> np.ndarray:
        if desc.shape[0] == 0:
            return np.zeros((0,), np.int32)
        out = np.empty(desc.shape[0], np.int32)
        for i in range(desc.shape[0]):
            row = desc[i : i + 1]
            candidates = self._root_children
            node = -1
            while candidates.size:
                d = hamming_matrix(row, self._centers[candidates])[0]
                node = int(candidates[int(d.argmin())])
                candidates = self._children[node]
            out[i] = self._leaf_id[node] if node >= 0 else 0
        return out

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        flat = np.concatenate(self._children) if self._children else np.zeros(0, np.int32)
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
    def load(cls, path: str | Path) -> "Vocab":
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
) -> Vocab:
    if descriptors.shape[0] == 0:
        raise ValueError("训练词汇树需要至少一个描述子")
    if descriptors.shape[1] != DESC_BYTES:
        raise ValueError(f"描述子宽度应为 {DESC_BYTES}，收到 {descriptors.shape[1]}")

    rng = np.random.default_rng(seed)
    centers_list: list[np.ndarray] = []
    children: list[np.ndarray] = []
    leaf_id_list: list[int] = []
    next_word = 0

    def build(subset: np.ndarray, level: int) -> np.ndarray:
        """在 subset 上建一层，返回本层新建节点的下标数组。"""
        nonlocal next_word
        if subset.shape[0] == 0:
            return np.zeros(0, np.int32)

        centers, labels = _kmajority(subset, branching, rng)
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
                kids = build(members, level + 1)
                children[node_id] = kids
            if children[node_id].size == 0:
                leaf_id_list[node_id] = next_word
                next_word += 1
        return np.array(node_ids, np.int32)

    root_children = build(descriptors, 0)
    return Vocab(
        centers=np.array(centers_list, np.uint8).reshape(-1, DESC_BYTES),
        children=children,
        leaf_id=np.array(leaf_id_list, np.int32),
        root_children=root_children,
        n_words=max(1, next_word),
    )
