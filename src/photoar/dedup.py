"""近似重复检测。

存在的理由（里程碑 0d 的第一条硬结论）：`corpus.build_corpus` 的 `_photo_id`
是内容哈希，只挡得住**字节完全相同**的重复。重新编码 / 裁切 / 不同分辨率
导出的近似重复哈希不相等，会两份都入库，然后互相触发 `verify.RATIO=1.5`
判 `ambiguous` —— **两份都永久漏检**，而且用户看到的现象是"识别器坏了"。
0d 先导语料里这一项造成 6.25% 库内漏检 + 32.7% 库外假阳性；剔除后 1.04% / 0%。

判据必须复用识别器**自己的失败机制**，也就是 ratio test：
    识别器判 ambiguous  <=>  top1 内点数 < RATIO x 次优内点数
把"查询图来自照片 i"代进去，就是
    s_i < RATIO x m_ij
其中 s_i 是 i 的**现实自匹配分**（扰动查询图 vs 自己能拿到的内点数），
m_ij 是 i 与 j 原图互查的内点数。满足这个不等式，i 就会被 j 挤成
ambiguous，两张不能同时入库；不满足就不会混淆，**必须都留下**。

## 5058 张真实语料上推翻的两条旧设计（不要改回去）

旧实现用"m >= 25（当时识别侧的内点数下限）"当判据、并用并查集连通分量"每簇留
一张"。
在 5058 张 Oxford5k 上实测这两条都是错的：

1. **绝对内点数阈值差了 2.6 倍。** 实测 m 在 25-40 的对，s/m 中位 **3.03**
   —— ratio test 轻松通过，根本不会混淆，却被判成重复剔掉。25 是"两张图
   能几何对上"的门槛，不是"会互相混淆"的门槛。会混淆的实际起点在 m ≈ 65
   （s 中位约 100，100/1.5）。先导语料上看不出这个差别，因为那里 m 的分布
   是 0-8 与 200+ 的干净二分，中间是空的，阈值放在 25 还是 65 结果一样。
   真实自相似语料上 m 是**连续分布**（21-25:220 对、26-50:784 对），阈值
   落在哪里就决定剔掉多少，必须放对。
2. **"近重复是传递的"只在二分分布下成立。** 一旦 m 连续，"m >= 阈值"这个
   关系就不是等价关系：实测最大的连通分量有 **78 张**、横跨 4 组不同建筑，
   簇内**没有直接边**的对抽样实测内点数中位只有 **9**、89.6% 低于阈值，
   跨建筑的对中位 **0**。也就是说那 78 张是一条**链**（A≈B、B≈C、A≉C），
   不是一个互相混淆的团。"每簇留一张"会删掉 77 张，其中绝大多数是完全
   可区分的好照片。全语料算下来剔除 722/5058 = **14.3%**。
   生产环境里这等于**静默删掉用户 14% 的照片**，而用户只会看到"这张扫不
   出来"，无从追查——它根本没入库。

所以现在：判据用 ratio（`scan_pairs` 的 `self_scores`），选取用**贪心独立集**
（`select_keep`），连通分量（`cluster`）**降级为纯报告用途**。

## 其余设计约束

- **m 必须用原图互查，不经 `synth` 扰动**：问的是"这两张照片本身是不是同一
  内容"。但 s 必须经扰动——它模拟的正是查询侧，用原图自比会得到全部特征
  数那个上界，把判据整体拉向"不剔"。这两者的口径差别是刻意的。
- 本模块不做 IO、不解码图片、不建词表：输入是已经提好的 `Features`、已经
  算好的候选下标、以及调用方算好的 `self_scores`（算它需要解码+synth，属于
  IO）。这样它能被单元测试直接驱动，也能同时服务两个调用方（Phase 0 的
  `bench/dedup_scan.py`，以及 Phase 1 的入库管线）。
- 刻意**不做** O(N²) 全对比：1 万张要 5000 万次 `verify_pair`，按 0d 实测的
  2.66 ms/次是 37 小时。候选由调用方用倒排索引粗排给出，代价降到 O(N·K)。
  代价是召回不完全——粗排没召回的重复对查不出来，这是明确的已知取舍，
  `DedupReport.top_k` 把当时用的 K 记进产物，便于事后判断覆盖面。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .features import Features
from .verify import DEDUP_MIN_INLIERS, DET_MAX, DET_MIN, RATIO, verify_pair


def self_score(
    ref: Features,
    perturbed_queries: Sequence[Features],
    *,
    verify_fn: Callable[..., Any] | None = None,
) -> int:
    """照片的**现实自匹配分**：扰动查询图 vs 自己，取各次内点数的中位数。

    这是 ratio 判据的分子。调用方负责解码原图、用 `synth.generate` 造若干
    张扰动查询图并提特征（那些都是 IO / 图像处理，不属于本模块），这里只做
    校验与取中位。

    取中位而不是最大值：最大值会挑出扰动最轻的那一次，高估识别器的实际
    余量，使判据偏向"不剔"，把该剔的重复留下来——那正是这个模块要防的
    双向漏检。取中位得到的是"典型一次查询"的分数。

    det 出界记 0 分，与 `scan_pairs` 同一口径。

    `verify_fn` 可注入是为了识别后端能换（XFeat 走 `verify.verify_pair_xfeat`，
    余弦互近邻而不是 Hamming）。**必须能换**：自匹配分是去重判据的分子，用另一个后端
    的配对函数算出来的是一个不同量纲的数，闸门会整体失准 —— 而
    `min(s, s') < ratio * m` 里两边都是数字，不会有任何一处报错。

    默认值写成 `None` 再在函数体里落到模块级的 `verify_pair`，而**不是**写成
    `verify_fn=verify_pair` 当默认参数：后者在 `def` 执行的那一刻就把函数对象绑死了，
    于是 `monkeypatch.setattr(dedup, "verify_pair", ...)` 从此无效 ——
    `tests/test_dedup.py::TestSelfScore` 正是这么测"取中位不取最大"和"det 出界记 0"
    的（用假的 PairResult，不跑真 RANSAC）。参数名也刻意不叫 `verify_pair`：那会在
    函数体内遮住模块级的同名函数，下面这行就只能拿到 None。
    """
    if not perturbed_queries:
        raise ValueError("perturbed_queries 不能为空：没有扰动查询图就算不出自匹配分")
    check = verify_fn if verify_fn is not None else verify_pair
    vals = []
    for q in perturbed_queries:
        r = check(q, ref, photo_id="self")
        vals.append(r.inliers if DET_MIN <= r.det <= DET_MAX else 0)
    return int(median(vals))


@dataclass(frozen=True)
class DedupReport:
    """一次扫描的完整结果。

    pair_scores 包含**全部被校验过的候选对**（不只是判为重复的那些），
    值是"几何校验通过 det 范围时的内点数，否则 0"。保留未达标的对是刻意的：
    0d 那条关键结论只能从这个完整分布上读出来——先导语料上它是"6-8 vs 200+
    中间有空档"，5058 张真实语料上它是**连续分布、没有空档**，同一个阈值在
    两者上的含义完全不同。换一批语料就必须重新看一眼这个分布。
    """

    n_docs: int
    top_k: int
    min_inliers: int
    n_verify_pair: int
    pair_scores: dict[tuple[int, int], int] = field(default_factory=dict)
    # ratio 判据的分子，按下标对齐。空 dict 表示这份报告来自旧的纯绝对阈值
    # 扫描（不再产生，只可能来自历史产物）。
    self_scores: dict[int, int] = field(default_factory=dict)
    ratio: float = RATIO

    @property
    def dup_pairs(self) -> list[tuple[int, int]]:
        """判为近重复的对，按内点数从高到低（同分时按下标，保证确定性）。

        两个条件同时成立才算：
        1. `m >= min_inliers` —— 绝对下限，挡住 m 与 s 都极小时的噪声对
           （低纹理照片的 s 本来就低，光看比值会把噪声判成重复）。
        2. `min(s_i, s_j) < ratio * m` —— 识别器的 ratio test 至少在一个
           方向上会失败，即至少有一张会被另一张挤成 ambiguous。用 min 而不是
           两张都要失败：只要有一张永久漏检，这两张就不能同时入库。
        """
        out = []
        for (i, j), m in self.pair_scores.items():
            if m < self.min_inliers:
                continue
            if self.self_scores:
                s = min(self.self_scores[i], self.self_scores[j])
                if s >= self.ratio * m:
                    continue  # ratio test 两个方向都通得过，不会混淆
            out.append((i, j))
        return sorted(out, key=lambda k: (-self.pair_scores[k], k))


def scan_pairs(
    features: Sequence[Features],
    candidates: Sequence[Sequence[int]],
    self_scores: Sequence[int],
    min_inliers: int = DEDUP_MIN_INLIERS,
    ratio: float = RATIO,
    top_k: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> DedupReport:
    """对每个 i 的候选逐个几何校验，返回成对内点数表 + ratio 判据所需的自匹配分。

    candidates[i] 必须已经排除 i 自己（自己对自己必然满分，会把每张照片都
    并进一个假簇）。同一对可能在两个方向上都被测到（i 的候选里有 j，j 的
    候选里也有 i）：`verify_pair` 并不完全对称（`findHomography` 的方向不同、
    `BFMatcher` 的 query/train 角色不同），取两次里内点数较大的那个——任一
    方向达到阈值就足以说明这两张是同一内容。

    det 落在 [DET_MIN, DET_MAX] 之外时记 0 分而不是记原始内点数：det 出界
    意味着这个单应矩阵本身不可信（镜像或极端缩放），它的内点数没有意义。
    这与 `verify_pair` 里 `ok` 的算法保持同一口径。

    self_scores 是必需参数而不是可选：默认它就等于回退到纯绝对阈值判据，
    而那个判据在真实语料上剔掉 14.3% 的照片（见模块 docstring）。让调用方
    忘记传就静默退回错误行为，是这个模块最不该有的失败方式。

    min_inliers 默认取 `verify.DEDUP_MIN_INLIERS`（25）而**不是**识别侧的
    `verify.MIN_INLIERS`（40）。这里的 m 是原图互查的内点数，比查询时系统性
    偏低（实测同一对 21 -> 33），跟着识别侧抬上去会让本该剔的对留在库里，
    换来的是永久漏检。理由的完整版写在 `verify.DEDUP_MIN_INLIERS` 的注释里。
    """
    if len(features) != len(candidates):
        raise ValueError(
            f"features 与 candidates 长度必须一致：{len(features)} vs {len(candidates)}"
        )
    if len(features) != len(self_scores):
        raise ValueError(
            f"features 与 self_scores 长度必须一致：{len(features)} vs {len(self_scores)}"
        )
    if min_inliers < 1:
        # 0 或负数会把每一个被校验过的候选对都判成重复（未通过 det 的对记 0
        # 分，也会 >= 0），整个语料并成一个簇、只留一张照片。这是用法错误，
        # 不能静默执行。
        raise ValueError(f"min_inliers 必须为正整数，收到 {min_inliers!r}")
    if ratio <= 0:
        raise ValueError(f"ratio 必须为正数，收到 {ratio!r}")

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
        self_scores={i: int(s) for i, s in enumerate(self_scores)},
        ratio=ratio,
    )


def select_keep(
    pairs: Sequence[tuple[int, int]], keys: Sequence[Any], n: int
) -> list[int]:
    """贪心独立集：按 keys 顺序保留照片，与已保留者有冲突边的才剔除。

    保证的性质：任意两张**保留下来**的照片之间没有冲突边（否则它们会互相
    判 ambiguous、双双漏检）。这是这个函数唯一必须做到的事。

    为什么不是"连通分量每簇留一张"（旧实现，已在 5058 张上被推翻）：冲突
    关系不是等价关系。链 A—B—C 里 A 与 C 并不冲突，只需剔掉 B；连通分量
    做法会把 A、C 一起删掉只留一张。实测最大连通分量 78 张（横跨 4 组建筑，
    簇内无边的对内点数中位仅 9），旧做法删 77 张、新做法只删真正冲突的那些。

    遍历顺序按 keys（通常是文件路径）而不是下标：下标取决于目录遍历顺序，
    keys 由调用方给出稳定的排序依据，换一次扫描范围（比如加了 --limit）
    不会改变留下的是哪一张。keys 相同时按下标兜底，保证全序、结果确定。
    """
    if len(keys) != n:
        raise ValueError(f"keys 长度必须等于 n：{len(keys)} vs {n}")
    conflict: dict[int, set[int]] = {}
    for a, b in pairs:
        if not (0 <= a < n and 0 <= b < n):
            raise ValueError(f"下标越界：({a}, {b})，n={n}")
        if a == b:
            raise ValueError(f"冲突对不能是自己与自己：({a}, {b})")
        conflict.setdefault(a, set()).add(b)
        conflict.setdefault(b, set()).add(a)

    kept: list[int] = []
    kept_set: set[int] = set()
    for i in sorted(range(n), key=lambda i: (keys[i], i)):
        if conflict.get(i, ()) and kept_set & conflict[i]:
            continue
        kept.append(i)
        kept_set.add(i)
    return sorted(kept)


def cluster(pairs: Sequence[tuple[int, int]], n: int) -> list[list[int]]:
    """把冲突对并成连通分量，返回**全部** n 个下标的划分（含单例分量）。

    ⚠️ **只用于报告，不要用它做剔除决定。** 剔除用 `select_keep`。
    连通分量回答的是"这些照片之间存在某条相似链"，不是"这些照片互相
    混淆"——实测最大分量 78 张、横跨 4 组不同建筑，分量内没有直接边的对
    内点数中位只有 9。拿它"每簇留一张"会删掉 14.3% 的照片（见模块
    docstring）。它仍然有价值：把相关的照片聚在一起给人看，比一长串
    孤立的对更容易看出语料里有哪些自相似的群。

    返回值按每簇最小下标排序，簇内也排序——完全确定性。
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
