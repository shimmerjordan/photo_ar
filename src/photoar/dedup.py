"""近似重复检测。

存在的理由（里程碑 0d 的第一条硬结论）：`corpus.build_corpus` 的 `_photo_id`
是内容哈希，只挡得住**字节完全相同**的重复。重新编码 / 裁切 / 不同分辨率
导出的近似重复哈希不相等，会两份都入库，然后互相触发 `verify.RATIO=1.5`
判 `ambiguous` —— **两份都永久漏检**，而且用户看到的现象是"识别器坏了"。
0d 先导语料里这一项造成 6.25% 库内漏检 + 32.7% 库外假阳性；剔除后 1.04% / 0%。

三条设计约束，动这个模块之前先读：

1. **判定必须复用 `verify.verify_pair`，不能另立一套"相似"的定义。** 另立
   一套（感知哈希、直方图距离……）就必然与识别器的实际行为分叉，而分叉的
   最坏方向是"扫描说干净、识别器却认为重复"——那时清理过的语料仍然漏检，
   却没有任何地方能查出原因。
2. **必须用原图互查，不经 `synth` 扰动。** 这里问的是"这两张照片本身是不是
   同一张内容"，不是"扰动后还认不认得出"。
3. **近重复是传递的**：A≈B、B≈C 时三者必须落进同一簇。否则"每簇留一张"
   会留下两张仍然互为重复的照片，清理等于没做。

本模块不做 IO、不解码图片、不建词表：输入是已经提好的 `Features` 和已经
算好的候选下标列表。这样它能被单元测试直接驱动，也能同时服务两个调用方
（Phase 0 的 `bench/dedup_scan.py`，以及 Phase 1 的入库管线）。

刻意**不做** O(N²) 全对比：1 万张要 5000 万次 `verify_pair`，按 0d 实测的
2.66 ms/次是 37 小时。候选由调用方用倒排索引粗排给出，代价降到 O(N·K)。
代价是召回不完全——粗排没召回的重复对查不出来，这是明确的已知取舍，
`DedupReport.top_k` 把当时用的 K 记进产物，便于事后判断覆盖面。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .features import Features
from .verify import DET_MAX, DET_MIN, MIN_INLIERS, verify_pair


@dataclass(frozen=True)
class DedupReport:
    """一次扫描的完整结果。

    pair_scores 包含**全部被校验过的候选对**（不只是判为重复的那些），
    值是"几何校验通过 det 范围时的内点数，否则 0"。保留未达标的对是刻意的：
    0d 那条关键结论（"同主题不同照片 6-8 内点 vs 阈值 25，中间有空档"）
    只能从这个完整分布上读出来。换一批语料就必须重新看一眼这个分布，
    不能照抄上一轮的结论。
    """

    n_docs: int
    top_k: int
    min_inliers: int
    n_verify_pair: int
    pair_scores: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def dup_pairs(self) -> list[tuple[int, int]]:
        """判为近重复的对，按内点数从高到低（同分时按下标，保证确定性）。"""
        return sorted(
            (k for k, v in self.pair_scores.items() if v >= self.min_inliers),
            key=lambda k: (-self.pair_scores[k], k),
        )


def scan_pairs(
    features: Sequence[Features],
    candidates: Sequence[Sequence[int]],
    min_inliers: int = MIN_INLIERS,
    top_k: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> DedupReport:
    """对每个 i 的候选逐个几何校验，返回成对内点数表。

    candidates[i] 必须已经排除 i 自己（自己对自己必然满分，会把每张照片都
    并进一个假簇）。同一对可能在两个方向上都被测到（i 的候选里有 j，j 的
    候选里也有 i）：`verify_pair` 并不完全对称（`findHomography` 的方向不同、
    `BFMatcher` 的 query/train 角色不同），取两次里内点数较大的那个——任一
    方向达到阈值就足以说明这两张是同一内容。

    det 落在 [DET_MIN, DET_MAX] 之外时记 0 分而不是记原始内点数：det 出界
    意味着这个单应矩阵本身不可信（镜像或极端缩放），它的内点数没有意义。
    这与 `verify_pair` 里 `ok` 的算法保持同一口径。
    """
    if len(features) != len(candidates):
        raise ValueError(
            f"features 与 candidates 长度必须一致：{len(features)} vs {len(candidates)}"
        )
    if min_inliers < 1:
        # 0 或负数会把每一个被校验过的候选对都判成重复（未通过 det 的对记 0
        # 分，也会 >= 0），整个语料并成一个簇、只留一张照片。这是用法错误，
        # 不能静默执行。
        raise ValueError(f"min_inliers 必须为正整数，收到 {min_inliers!r}")

    n = len(features)
    scores: dict[tuple[int, int], int] = {}
    n_verify = 0
    for i in range(n):
        for d in candidates[i]:
            if d == i:
                raise ValueError(
                    f"candidates[{i}] 含有 {i} 自己：自比必然满分，会把每张照片"
                    f"都并进同一个簇。调用方必须先排除自己。"
                )
            if not (0 <= d < n):
                raise ValueError(f"candidates[{i}] 含越界下标 {d}（n={n}）")
            r = verify_pair(features[i], features[d], photo_id=str(d))
            n_verify += 1
            score = r.inliers if DET_MIN <= r.det <= DET_MAX else 0
            key = (min(i, d), max(i, d))
            if score > scores.get(key, -1):
                scores[key] = score
        if on_progress is not None:
            on_progress(i + 1, n)

    return DedupReport(
        n_docs=n,
        top_k=top_k if top_k is not None else max((len(c) for c in candidates), default=0),
        min_inliers=min_inliers,
        n_verify_pair=n_verify,
        pair_scores=scores,
    )


def cluster(pairs: Sequence[tuple[int, int]], n: int) -> list[list[int]]:
    """把近重复对并成簇，返回**全部** n 个下标的划分（含单例簇）。

    并查集而不是"逐对标记删除"：近重复是传递的，A≈B、B≈C 时三者必须同簇。
    逐对处理会在 A≈B 时删掉 B、随后 B≈C 这条边失效，留下 A 和 C 两张仍然
    互为重复的照片。

    返回值按每簇最小下标排序，簇内也排序——完全确定性，同样的输入永远得到
    逐元素相同的输出（下游 select_keep 的确定性依赖于此）。
    """
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]  # 路径压缩
            a = parent[a]
        return a

    for a, b in pairs:
        if not (0 <= a < n and 0 <= b < n):
            raise ValueError(f"下标越界：({a}, {b})，n={n}")
        ra, rb = find(a), find(b)
        if ra != rb:
            # 总是把大的根挂到小的根上：根恒为簇内最小下标，与输入对的顺序无关。
            parent[max(ra, rb)] = min(ra, rb)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [sorted(m) for _, m in sorted(groups.items())]


def select_keep(clusters: Sequence[Sequence[int]], keys: Sequence[Any]) -> list[int]:
    """每簇留一个代表：keys 最小的那个。返回排序后的保留下标。

    用外部提供的 keys（通常是文件路径）而不是簇内最小下标来选代表：下标
    取决于目录遍历顺序，而 keys 由调用方给出稳定的排序依据，换一次扫描
    范围（比如加了 --limit）不会改变同一批照片里留下的是哪一张。
    """
    keep = []
    for members in clusters:
        if not members:
            raise ValueError("簇不能为空")
        keep.append(min(members, key=lambda i: keys[i]))
    return sorted(keep)
