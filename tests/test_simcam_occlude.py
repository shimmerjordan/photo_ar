"""`bench/simcam.py` 的遮挡扰动。

为什么这个 bench 里的函数要有单测：它决定的是**结论**。用户报的头号现象是「手指
遮挡四角就识别不出来」，而这个变量在整个 bench 里从来没有过 —— 补上它之后，那张
「全过的最小占比」表会多出一列，而阈值调整会照着那一列走。写歪了（遮挡块反而制造
新角点、或者面积算错一个开方）不会有任何报错，只会让那一列的数字系统性偏乐观或
偏悲观，然后阈值跟着偏。
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "bench"))
# `src` 也要显式插。`simcam` 自己在模块顶层插了一次，所以不插也能跑 —— 但那是靠
# 一个 import 副作用，而这里 import 的顺序一变（或者哪天 simcam 改成用包导入）
# 这个文件就会莫名收集失败。
sys.path.insert(0, str(_ROOT / "src"))

import simcam  # noqa: E402

from photoar import features  # noqa: E402


def _photo(w: int = 400, h: int = 300, seed: int = 7) -> np.ndarray:
    """一块「像照片」的纹理。

    **不能用纯随机噪声。** 噪声图的角点供给处处饱和，远超 ORB 的 `nfeatures` 配额 ——
    每一层金字塔的候选都比配额多，于是遮掉一块面积之后剩余区域照样提满，特征总数
    一个都不少。实测：640×480 噪声图遮掉 1 个角（6% 面积）前后都是 1796 个。那样
    这组测试就永远量不到遮挡的效应，而实现写歪了也发现不了。

    真实照片的特征供给是**有限**的 —— 这正是 `backend.QUERY_N_FEATURES` 那段注释的
    前提（入库时 300 个特征全落在照片上，手持时要摊到整个画面）。所以这里造的是
    低频块状图：噪声降采样再放大，角点只出现在块的边界上，数量由内容决定而不是被
    配额截断。
    """
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (max(2, h // 10), max(2, w // 10), 3), dtype=np.uint8)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def test_遮挡占比为零时一个像素都不改():
    frame = _photo()
    before = frame.copy()
    simcam.occlude_corners(frame, (0, 0, frame.shape[1], frame.shape[0]), 0.0, 4)
    assert np.array_equal(frame, before)


def test_遮几个角为零时一个像素都不改():
    frame = _photo()
    before = frame.copy()
    simcam.occlude_corners(frame, (0, 0, frame.shape[1], frame.shape[0]), 0.1, 0)
    assert np.array_equal(frame, before)


def test_遮挡块边长按面积开方算():
    # 每角遮 4% 面积 → 边长应是 sqrt(0.04*w*h)。忘开方（直接拿 0.04*w 当边长）
    # 会让实际遮挡面积差一个数量级，而表格里那一列会显得「遮挡几乎没影响」。
    w, h = 400, 300
    assert simcam.occlusion_side(w, h, 0.04) == round((0.04 * w * h) ** 0.5)


def test_遮挡块不越出照片矩形():
    frame = np.zeros((300, 400, 3), np.uint8)
    rect = (50, 40, 200, 150)
    simcam.occlude_corners(frame, rect, 0.09, 4)
    x0, y0, w, h = rect
    outside = frame.copy()
    outside[y0 : y0 + h, x0 : x0 + w] = 0
    assert not outside.any(), "遮挡改到了照片矩形之外的像素"


def test_遮四个角时四角都改而中心不动():
    frame = _photo(400, 300)
    before = frame.copy()
    simcam.occlude_corners(frame, (0, 0, 400, 300), 0.04, 4)
    changed = (frame != before).any(axis=2)
    side = simcam.occlusion_side(400, 300, 0.04)
    # 每个角的内侧一小块必须被改到（取角上 side//3 见方，避开羽化带）
    probe = max(1, side // 3)
    assert changed[:probe, :probe].any(), "左上角没被遮"
    assert changed[:probe, -probe:].any(), "右上角没被遮"
    assert changed[-probe:, :probe].any(), "左下角没被遮"
    assert changed[-probe:, -probe:].any(), "右下角没被遮"
    cy, cx = 150, 200
    assert not changed[cy - 10 : cy + 10, cx - 10 : cx + 10].any(), "中心不该被动"


def test_遮两个角时只遮上面那两个():
    # 顺序必须是确定的，否则「遮 2 个角」在两次运行之间不是同一个实验。
    # 上面两角是单手/双手持握时拇指最常压住的位置。
    frame = _photo(400, 300)
    before = frame.copy()
    simcam.occlude_corners(frame, (0, 0, 400, 300), 0.04, 2)
    changed = (frame != before).any(axis=2)
    side = simcam.occlusion_side(400, 300, 0.04)
    probe = max(1, side // 3)
    assert changed[:probe, :probe].any(), "左上角应该被遮"
    assert changed[:probe, -probe:].any(), "右上角应该被遮"
    assert not changed[-probe:, :probe].any(), "左下角不该被遮"
    assert not changed[-probe:, -probe:].any(), "右下角不该被遮"


def test_遮挡只删特征不制造新角点():
    """这是这组测试里最重要的一条。

    遮挡块如果是纯色硬边，它与照片的交界会变成整幅图最强的对比边 —— ORB 会在那四条
    边上找到一堆**新**角点。那样量出来的就不是「手指挡掉了特征」，而是「手指换掉了
    特征」，两者对识别的影响方向完全不同（后者还会污染词袋）。

    判据取「照片区域内提到的特征数必须下降」：真手指是软边、无高频纹理，删信息不加
    信息。硬边实现会让这个数持平甚至上升。
    """
    frame = _photo(640, 480, seed=11)
    n_before = len(features.extract(frame, long_edge=640, n_features=2000))
    simcam.occlude_corners(frame, (0, 0, 640, 480), 0.08, 4)
    n_after = len(features.extract(frame, long_edge=640, n_features=2000))
    assert n_after < n_before, f"遮挡后特征数没降：{n_before} -> {n_after}"


def test_遮挡越重特征掉得越多():
    counts = []
    for frac in (0.0, 0.05, 0.12):
        frame = _photo(640, 480, seed=3)
        simcam.occlude_corners(frame, (0, 0, 640, 480), frac, 4)
        counts.append(len(features.extract(frame, long_edge=640, n_features=2000)))
    assert counts[0] > counts[1] > counts[2], f"应该单调下降，实际 {counts}"


@pytest.mark.parametrize("corners", [1, 2, 3, 4])
def test_遮的角越多特征越少(corners: int):
    base = _photo(640, 480, seed=5)
    n_base = len(features.extract(base, long_edge=640, n_features=2000))
    frame = base.copy()
    simcam.occlude_corners(frame, (0, 0, 640, 480), 0.06, corners)
    n = len(features.extract(frame, long_edge=640, n_features=2000))
    assert n < n_base
