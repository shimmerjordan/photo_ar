"""几何校验与命中判定。

本模块是**单帧**判定阈值的唯一来源。

⚠️ 它曾经是「全项目判定阈值的唯一来源」，现在不是了：`photoar.streak` 里还有一组
**跨帧累积**的阈值（软门槛 30／连续 3 帧／比值 2.0）。那条路在单帧判定没过之后再看
一次「连续几帧的第一名是不是同一张」，也就是说**它能放行这里判 weak 的帧** ——
包括本模块的 [MIN_INLIERS] 本来挡住的那一段（真实误识别的内点数最大到 39）。
调这里的任何数之前先看那边，两组阈值是一对。

三条判定（spec §8.3）缺一不可：
  1. 内点数 >= MIN_INLIERS
  2. 单应矩阵行列式落在 [DET_MIN, DET_MAX]
  3. 第一名内点数 >= RATIO * 第二名内点数

第 3 条的比值检验在**全部候选**之间进行，不只在通过前两条的候选之间。
理由：若第二名 24 分（未过阈值）而第一名 26 分，二者其实无法区分，
只在通过者之间比会把它当作"唯一通过者"直接放行，制造误识别。

行列式用带符号值而非绝对值：负行列式意味着镜像变换，而实体照片经
相机成像永远不会镜像，因此负值必须判否。这比 spec 的 abs(det) 更严。

内点数下限有**两个**：识别侧的 `MIN_INLIERS` 与去重侧的 `DEDUP_MIN_INLIERS`。
它们量的不是同一个量，所以数值不同也不该被统一——理由写在两个常量各自的注释里。
本模块只用前者做判定；后者放在这里是为了让两者挨着，改一个时能看见另一个。

产品路径调 `decide(results)`；`decide_with(results, min_inliers=..., ratio=...)`
是同一套判定的参数化版本，供阈值扫描用另一组阈值重放录好的候选分数
（见 decide_with 的文档与 bench/threshold_scan.py）。
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

from .features import Features

# 识别侧的内点数下限。25 -> 40 的依据是 0d 上规模那 29740 次查询的**查询时**分布
# （复现命令见 bench/README.md）：真实误识别
# 34 条 p95=36、**最大 39**，而库内真阳性 19284 条 p5=69 —— 两个分布几乎不重叠，
# 40 正好卡在中间。代价与收益：真实误识别 0.349% -> 0.000%，库内命中
# 96.42% -> 95.70%（仍守住 §14.2 的 >=95%），库外总误识 6.951% -> 3.963%
# （剩下的是 Oxford5k 的语料属性：同一被摄物体的不同照片，不该拿来定阈值）。
#
# 可行窗口是 [40, 47]（48 起命中率跌破 95%），取**下界**：窗口内往上走只能换到
# "对未见语料的余量"，却要拿一条硬基线的余量去换，而 95.70% 距基线只剩 0.70pp。
#
# ⚠️ 这个 40 拟合在 22 张 holdout 的 34 个真实误识别事件上，样本很小。质量门槛
# 放开后、或换一份非 Oxford5k 的语料，必须用 bench/threshold_scan.py 重新量。
MIN_INLIERS = 40

# 去重侧的内点数下限，**故意**比识别侧低。不要"顺手统一"成一个值。
#
# 两边量的不是同一个量：识别侧量「扰动查询图 vs 库内原图」，去重侧
# （`dedup.scan_pairs`、`bench/classify_fp.py`）量「两张原图之间」。后者系统性更
# 低——实测同一对能从原图 21 分涨到查询时 33 分。
#
# 去重的下限必须**低于**识别的下限：它要挡住的是"识别器会混淆的对"，漏放一对就
# 等于让一张照片永久漏检（两份互相挤成 ambiguous）。跟着抬到 40，原图 25-39 分
# 那批对就不再判为近重复，而它们在查询时完全可能越过 40 —— 直接推高「漏掉的近
# 重复」（0d 上规模实测 0.452%）。抬阈值让 dedup 少剔照片，方向看着"更保守"，
# 实际是把风险从"误删照片"换成"永久漏检"，而后者用户无从追查。
#
# 保持 25 不变还有一层好处：语料判定不变，不需要重跑 dedup + build。
DEDUP_MIN_INLIERS = 25

# ---- XFeat 后端的内点数下限 ----
#
# **不能沿用上面那个 40。** 两个后端的内点数分布是两个完全不同的量：ORB 提 300 个二值
# 描述子、用 Hamming + crossCheck 匹配；XFeat 提 512 个 64 维浮点描述子、用余弦互近邻
# 匹配（还先过了一道 0.82 的余弦闸门）。关键点更多、匹配更准，真阳性的内点数系统性
# 更高，把 ORB 的阈值搬过来会让判定实际上变松。
#
# 60 的依据（`bench/xfeat_inlier_dist.py`，Oxford5k 抽 250 张 × 6 个扰动查询 = 1500 次，
# 每次与 25 张别人的图对比取最强者，与当年定 40 时同一份语料、同一个口径）：
#
#                真阳性 p1   真阳性 p5   误识别 p95   误识别 p99   误识别最大
#   ORB   (300点)      9         53           8          11         213
#   XFeat (512点)     71         97          13          66         163
#
# 取 60 的两条理由都是直接读出来的：
#   1. **真阳性 p1 = 71 > 60** —— 门槛卡在 60，真阳性损失不到 1%。对比 ORB 的 40：它的
#      p1 只有 9、p5 是 53，也就是 40 这个门槛会吃掉 1%~5% 的真阳性。XFeat 在漏检这一侧
#      反而更安全，这正是换特征最实在的收益。
#   2. **误识别 p95 = 13**，远在 60 之下。
#
# ⚠️ p99=66 与最大值 163 超过了 60，但这是**语料属性而不是缺陷**：Oxford5k 里有大量
# 「同一建筑的不同照片」，它们在几何上本来就对得上。当年定 40 时遇到的是同一件事
# （原始记录：库外总误识 3.963%，"剩下的是 Oxford5k 的语料属性，不该拿来定阈值"）。
# 真实误识别率要用 `bench/threshold_scan.py` 走完整两阶段管线在留出集上量。
#
# ⚠️ **这仍是一个待复核的值。** 上面量的只有精排那一步（不含词表与倒排粗排），样本
# 1500 次而当年 ORB 那轮是 29740 次。语料换了、`xfeat.TOP_K` 或 `match.MIN_COSSIM`
# 动了，都必须重新量。
XFEAT_MIN_INLIERS = 60

# 去重侧同样比识别侧低，理由与 DEDUP_MIN_INLIERS 完全一样（见上面那段）：去重量的是
# 「两张原图之间」，系统性低于「扰动查询图 vs 原图」；漏放一对近重复的代价是两张照片
# 都永久漏检，而用户无从追查。
#
# 38 是**按 ORB 那一对的比例推的**（25/40 = 0.625，0.625 × 60 = 37.5），**没有独立量过**。
# 直接量它需要「两张真实近重复原图」的内点数分布，而这份语料里没有标注过的近重复对。
# 保守方向是往低调（宁可多剔几张照片，也不要让两张永久漏检），所以真要动，往下动。
XFEAT_DEDUP_MIN_INLIERS = 38

DET_MIN = 0.05
DET_MAX = 20.0

# 比值检验的门槛。同一批重放数据上量过：1.5 -> 2.0 只把「能几何对上但不该混淆」
# 那一类从 3.963% 压到 2.947%（语料属性，不是缺陷），对真实误识别**零边际作用**
# （40/1.5 与 40/2.0 都是 0 条），却要多付 0.41pp 命中率。所以不动。
RATIO = 1.5

RANSAC_REPROJ = 3.0

# RANSAC 的迭代上限。默认 2000 只在**假匹配**上被烧满——真匹配靠自适应终止，
# 恒定约 0.34ms。实测把上限降到 200：12 个真匹配的内点数完全不变、假匹配的内点数
# 略降（更安全的方向）、误判仍为 0，而假匹配耗时从 20.56ms 降到 2.19ms。
# 这个上限只限制"难例上花多少力气"，不改变任何判定条件。
RANSAC_MAX_ITERS = 200

MIN_MATCHES_FOR_HOMOGRAPHY = 4


@dataclass(frozen=True)
class PairResult:
    """一次精排的结果。

    `homography` 是 RANSAC 拟合出来的那个 3×3 单应矩阵，方向 **query → ref**。

    以前它算完就丢掉了，只留下 `inliers`。但它正是「照片在画面里的四个角」的来源：
    取逆之后作用到参考图的四角，就得到照片此刻在查询帧里的四边形 —— 而 RANSAC 的内点
    筛选本身就是「用一堆点拟合出这个平面、并把手指遮挡那些点当离群值剔掉」。也就是说
    这个几何拟合一直在做，只是产物被扔了。

    `compare=False` 是必须的：numpy 数组的 `==` 返回的是数组而不是布尔，带着它做
    dataclass 的相等比较会抛「truth value of an array is ambiguous」—— 而
    `PairResult` 在测试里是要比较的（`decide_with` 的重放用例）。矩阵是附加产物，
    不参与「这次精排结果是否相同」的语义，所以排除在比较之外正好。
    `repr=False` 同理：一个 3×3 数组打进 repr 只会让失败信息难读。
    """

    photo_id: str
    inliers: int
    det: float
    ok: bool  # 是否通过前两条判定（比值检验是 decide 的职责）
    homography: "np.ndarray | None" = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Decision:
    matched: bool
    photo_id: str | None
    inliers: int
    reason: str  # 'ok' | 'empty' | 'weak' | 'ambiguous'


def _fail(photo_id: str) -> PairResult:
    return PairResult(photo_id=photo_id, inliers=0, det=0.0, ok=False)


def _passes(
    inliers: int, det: float, min_inliers: int, det_min: float, det_max: float
) -> bool:
    """前两条判定：内点数下限 + 带符号行列式区间。

    verify_pair 与 decide_with 共用这一个实现，而不是各写一遍同一个表达
    式。理由是 decide_with 支持用**另一组阈值**重放已经录好的候选分数
    （bench/threshold_scan.py）：如果两边各算一次，改了一边忘了另一边，
    重放出来的数字会和真跑不一致，而且不会有任何报错——扫出来的阈值是要
    直接写回产品的，这种静默漂移代价太大。
    """
    return inliers >= min_inliers and det_min <= det <= det_max


def ransac_pair(
    src: np.ndarray,
    dst: np.ndarray,
    photo_id: str,
    *,
    min_inliers: int,
) -> PairResult:
    """已配好的点对 → 单应矩阵 → 内点数与行列式。

    从 `verify_pair` 里抽出来共用：两个识别后端（ORB 的 Hamming crossCheck、XFeat 的
    余弦互近邻）产出点对的方式不同，但**点对之后的每一步必须完全一样** —— RANSAC 的
    重投影阈值、迭代上限、行列式区间、以及 `_passes` 的判定顺序。抄两份的话，改一边
    忘一边不会报错，只会让两个后端的判定口径悄悄分叉，而阈值标定是分别做的，没人会
    发现口径本身变了。
    """
    if len(src) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)
    H, mask = cv2.findHomography(
        src, dst, cv2.RANSAC, RANSAC_REPROJ, maxIters=RANSAC_MAX_ITERS
    )
    if H is None or mask is None:
        return _fail(photo_id)
    inliers = int(mask.sum())
    det = float(np.linalg.det(H))
    ok = _passes(inliers, det, min_inliers, DET_MIN, DET_MAX)
    return PairResult(
        photo_id=photo_id, inliers=inliers, det=det, ok=ok, homography=H
    )


def verify_pair(query: Features, ref: Features, photo_id: str) -> PairResult:
    """ORB 后端：Hamming + crossCheck 配对，再走 `ransac_pair`。"""
    if len(query) < MIN_MATCHES_FOR_HOMOGRAPHY or len(ref) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(query.desc, ref.desc)
    if len(matches) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)

    src = query.pts[[m.queryIdx for m in matches]]
    dst = ref.pts[[m.trainIdx for m in matches]]
    return ransac_pair(src, dst, photo_id, min_inliers=MIN_INLIERS)


def verify_pair_xfeat(
    query: Features,
    ref: Features,
    photo_id: str,
    *,
    min_inliers: int = XFEAT_MIN_INLIERS,
) -> PairResult:
    """XFeat 后端：余弦互近邻配对，再走同一个 `ransac_pair`。

    描述子必须是已 L2 归一化的 float32（`xfeat.XFeatExtractor` 的输出就是），否则
    `match.mnn_matches` 里那道余弦闸门会静默失效。
    """
    from .match import mnn_matches

    if len(query) < MIN_MATCHES_FOR_HOMOGRAPHY or len(ref) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)
    qi, ri = mnn_matches(query.desc, ref.desc)
    if len(qi) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)
    return ransac_pair(query.pts[qi], ref.pts[ri], photo_id, min_inliers=min_inliers)


def decide_with(
    results: list[PairResult],
    *,
    min_inliers: int = MIN_INLIERS,
    ratio: float = RATIO,
    det_min: float = DET_MIN,
    det_max: float = DET_MAX,
) -> Decision:
    """用**指定的**阈值做三条判定，而不是用模块常量。

    存在的理由：0d 上规模跑出库外误识别 6.951%，要回答"把 MIN_INLIERS 或
    RATIO 调到多少能关掉这个缺口、代价是多少漏检"，唯一诚实的做法是把一次
    真跑里每个查询的候选分数录下来，再用不同阈值重放——而不是各调一个值重
    跑一次（一次 54 分钟）、更不是靠原图内点数估（实测同一对能从原图 21 涨
    到查询时 33）。

    重放是**精确**的，不是近似：verify_pair 算出的 inliers/det 与阈值无关
    （阈值只参与判定），候选排序也只按 inliers，所以录下 top1/top2 就足以
    还原任意 (min_inliers, ratio) 下的判定。提高 min_inliers 不会让 top2
    顶上来——top1 过不了就直接判 weak，这是 decide 的既有语义。

    这里重新计算前两条判定，不复用 PairResult.ok：ok 是 verify_pair 用模块
    常量算的，重放时那个值正是要被替换掉的东西。
    """
    if not results:
        return Decision(matched=False, photo_id=None, inliers=0, reason="empty")

    ranked = sorted(results, key=lambda r: -r.inliers)
    top1 = ranked[0]
    if not _passes(top1.inliers, top1.det, min_inliers, det_min, det_max):
        return Decision(matched=False, photo_id=None, inliers=top1.inliers, reason="weak")

    runner_up = ranked[1].inliers if len(ranked) > 1 else 0
    if top1.inliers < ratio * runner_up:
        return Decision(
            matched=False, photo_id=None, inliers=top1.inliers, reason="ambiguous"
        )

    return Decision(
        matched=True, photo_id=top1.photo_id, inliers=top1.inliers, reason="ok"
    )


def decide(results: list[PairResult]) -> Decision:
    """产品路径的判定：decide_with 配 spec §8.3 的那组阈值。"""
    return decide_with(results)
