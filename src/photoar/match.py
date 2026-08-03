"""浮点描述子的互近邻匹配。

ORB 那侧用 `cv2.BFMatcher(NORM_HAMMING, crossCheck=True)`；XFeat 的描述子是已 L2
归一化的 64 维 float32，对应的匹配就是内积取最大 + 互为最近邻。

**为什么不用 LightGlue / LighterGlue。** 它们确实更准，但 CPU 上完全跑不动：
LightGlue 官方数字是 0.31 pairs/sec @1200px（i7-6700K）≈ **3.2 秒一对**，而精排要对
Top-K 个候选各算一次。LighterGlue 按作者"快约 3 倍"外推也还在 1 秒量级。本项目在
N5095 上的单次查询预算是几百毫秒，差两个数量级，不是优化能补的差距。
互近邻在同一台机器上是 **0.46ms/对**（512 点，i9-11900K 限 3 线程实测）。

**关键点数是这里的硬约束。** 互近邻的成本随关键点数超线性增长（N×N 相似度矩阵的
argmax 是内存瓶颈）：256 点 0.09ms、512 点 0.46ms、1024 点 1.89ms、2048 点 10.7ms、
4096 点 116.7ms。Top-20 精排在 512 点是 9.2ms，在 4096 点是 2334ms —— 后者一个查询
就把预算烧光。所以 `xfeat.TOP_K` 钉在 512，且是烘进 ONNX 图里的。
"""

from __future__ import annotations

import numpy as np

# 互近邻的余弦下限，取 XFeat 官方 `match()` 的默认值 0.82。
#
# 它和 RANSAC 的内点数阈值是两道**不同**的闸门，都需要：余弦下限挡掉"描述子根本不
# 像"的匹配（降低 RANSAC 的外点比例，让它更容易找到真单应矩阵），内点数阈值挡掉
# "几何上对不上"的候选。只留后者的话，RANSAC 要在一堆随机匹配里找结构，200 次迭代
# 常常会在噪声里凑出一个看似合理的单应矩阵。
MIN_COSSIM = 0.82


def mnn_matches(
    query_desc: np.ndarray,
    ref_desc: np.ndarray,
    min_cossim: float = MIN_COSSIM,
) -> tuple[np.ndarray, np.ndarray]:
    """互为最近邻的匹配对下标。

    两个入参都必须是 (N, D) float32 且**已经 L2 归一化**（XFeat 的 ONNX 图里已经归
    一化过了）。没归一化的话内积不是余弦，`min_cossim` 这道闸门就失去意义 —— 而它
    不会报错，只会静默失效。

    @return (query 下标, ref 下标)，长度相同，一一对应。
    """
    if query_desc.size == 0 or ref_desc.size == 0:
        empty = np.zeros(0, np.int64)
        return empty, empty

    sim = query_desc.astype(np.float32, copy=False) @ ref_desc.astype(
        np.float32, copy=False
    ).T
    best_for_query = sim.argmax(axis=1)
    best_for_ref = sim.argmax(axis=0)
    qi = np.arange(sim.shape[0])
    mutual = best_for_ref[best_for_query] == qi
    if min_cossim > 0:
        mutual &= sim[qi, best_for_query] >= min_cossim
    return qi[mutual].astype(np.int64), best_for_query[mutual].astype(np.int64)
