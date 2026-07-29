"""几何校验与命中判定。

本模块是全项目判定阈值的唯一来源。三条判定（spec §8.3）缺一不可：
  1. 内点数 >= MIN_INLIERS
  2. 单应矩阵行列式落在 [DET_MIN, DET_MAX]
  3. 第一名内点数 >= RATIO * 第二名内点数

第 3 条的比值检验在**全部候选**之间进行，不只在通过前两条的候选之间。
理由：若第二名 24 分（未过阈值）而第一名 26 分，二者其实无法区分，
只在通过者之间比会把它当作"唯一通过者"直接放行，制造误识别。

行列式用带符号值而非绝对值：负行列式意味着镜像变换，而实体照片经
相机成像永远不会镜像，因此负值必须判否。这比 spec 的 abs(det) 更严。
"""

from dataclasses import dataclass

import cv2
import numpy as np

from .features import Features

MIN_INLIERS = 25
DET_MIN = 0.05
DET_MAX = 20.0
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
    photo_id: str
    inliers: int
    det: float
    ok: bool  # 是否通过前两条判定（比值检验是 decide 的职责）


@dataclass(frozen=True)
class Decision:
    matched: bool
    photo_id: str | None
    inliers: int
    reason: str  # 'ok' | 'empty' | 'weak' | 'ambiguous'


def _fail(photo_id: str) -> PairResult:
    return PairResult(photo_id=photo_id, inliers=0, det=0.0, ok=False)


def verify_pair(query: Features, ref: Features, photo_id: str) -> PairResult:
    if len(query) < MIN_MATCHES_FOR_HOMOGRAPHY or len(ref) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(query.desc, ref.desc)
    if len(matches) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)

    src = query.pts[[m.queryIdx for m in matches]]
    dst = ref.pts[[m.trainIdx for m in matches]]
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, RANSAC_REPROJ, maxIters=RANSAC_MAX_ITERS)
    if H is None or mask is None:
        return _fail(photo_id)

    inliers = int(mask.sum())
    det = float(np.linalg.det(H))
    ok = inliers >= MIN_INLIERS and DET_MIN <= det <= DET_MAX
    return PairResult(photo_id=photo_id, inliers=inliers, det=det, ok=ok)


def decide(results: list[PairResult]) -> Decision:
    if not results:
        return Decision(matched=False, photo_id=None, inliers=0, reason="empty")

    ranked = sorted(results, key=lambda r: -r.inliers)
    top1 = ranked[0]
    if not top1.ok:
        return Decision(matched=False, photo_id=None, inliers=top1.inliers, reason="weak")

    runner_up = ranked[1].inliers if len(ranked) > 1 else 0
    if top1.inliers < RATIO * runner_up:
        return Decision(
            matched=False, photo_id=None, inliers=top1.inliers, reason="ambiguous"
        )

    return Decision(
        matched=True, photo_id=top1.photo_id, inliers=top1.inliers, reason="ok"
    )
