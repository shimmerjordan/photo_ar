"""参考图预处理档位 —— **bench 专用，没有接进入库路径**。

这个模块存在的价值是记录一组**否定结论**：给参考图做增强并不能把 `quality_too_low`
那道墙推开。别再重新发明一遍。本文件先前的 docstring 论证过「CLAHE 是域对齐」，
那套说法被下面的实测推翻了，已删除。

## 结论一：CLAHE 有害，不要用

真机帧（17 帧，拍的是手机屏幕上显示的 demo-a，门槛 MIN_INLIERS=40）：

    档位              过门槛    中位内点
    none              5/17      32
    clahe2            6/17      31
    clahe4            4/17       9
    clahe8            2/17       8
    clahe4_unsharp    3/17      17

机理很清楚：入库侧的特征预算是**固定的** 300 个（`features.N_FEATURES`），
CLAHE 把噪点也放大成角点，这些假角点会把真正稳定的结构**挤出**那 300 个名额。
不是"多提了一些不好的"，是"好的被顶掉了"。

## 结论二：轻锐化在真机帧上有收益，但在真正被卡住的那张照片上没有

同一批 17 帧，`amount=0.4 / sigma=0.8`：过门槛 10/17（vs 5/17）、中位内点 45
（vs 32）、最高 69（vs 59）。amount 从 0.4 到 2.0 收益一样，3.0 崩掉。锐化施加
的尺度无关（原生 1600 上锐化再抽特征 ≈ 缩到 640 再锐化）。

两个必须一起说的限定：

1. **过门槛数 5→10 有一半是门槛假象**。baseline 的内点排序是
   `… 32 36 39 39 42 45 …`，MIN_INLIERS=40 正好落在这个密集簇里。稳健读数是
   中位数（32→45）。结构上是：低尾更糟（11 11 15 16 → 7 10 13），中高段变好。
2. **这 17 帧拍的是手机屏幕，不是印刷件**。屏幕自带摩尔纹、且已经被屏幕侧锐化
   过一遍，和婚礼现场的印刷件不是同一个成像域。所以这个收益的适用范围只覆盖
   「另一台手机显示照片」这一种情况。

而在**真正被门槛卡住的那张婚礼照**上，合成帧口径（4 seed × 8 repeat = 96 次
边缘查询，`bench/simcam.py`）：none 38/96 vs mild 40/96 —— **没有效果**。
（我先前只看 seed 7 就报过"3/5 vs 1/5，明显更好"，那是 seed 运气。）

代价是零：配对假阳测试（200 张从未入库的真实照片 × 5 视角 = 1000 次查询，两个
臂判同一批）raw 0/1000、sharp 0/1000；真机交叉负样本（17 帧真机帧 vs 婚礼照
参考）两臂最高内点都是 9（门槛 40）。零代价、但在目标照片上零收益，所以不上线。

## 结论三：arcoreimg 的分数不能当判据 —— 它不是像素的函数

先前这里贴过一张"CLAHE 把分数从 55 抬到 95"的表，并据此以为找到了机理。那张表
不可信，因为这个分数本身就不稳：

* **不是像素的函数。** 用 cv2 解出 `wedding-01.jpg` 的像素、原样写成**无损** PNG
  （已逐字节校验解码结果一致）→ 35 分；原 JPEG → 55 分。改文件名不变（仍 55）。
* **确定的。** 同一个文件跑 6 次，分数逐字节一致（35×6 / 85×6 / 55×6）。粒度 5 分。
* **不是逐像素混沌。** 单个像素 B 通道 ±1，换三个位置：35 / 35 / 35 / 35，不动。
  但**全图**统一 +1 → 45。
* **对 JPEG 质量非单调。** 同一批像素 q90…q100 重编码：55 35 50 25 55 45 30 55 45。
  q95(25) 比 q90(55) 还低。所以"分数 ≈ 高频能量"那套解释是错的。
* **判决落在它自己的测量抖动里。** 21 个视觉无差别变换（JPEG q85–q100、无损 PNG、
  全图 ±1/±2）：
      婚礼照   名义 55 → 25–55，均值 40.7，中位 40，**0/21 能过 75**
      demo-a   名义 75 → 40–75，均值 57.1，中位 55，**只有 2/21 = 10% 能过 75**
  demo-a 是**已经入库**的那张。它的中位数（55）正好等于婚礼照的名义分。也就是说
  它当初能过，是因为恰好落在自己零分布的上边缘。

所以不能拿"把分数抬过 75"当验收判据 —— 那是在拟合一个锯齿状的目标（metric
gaming）；也不能拿它当**硬拒**的依据，因为同一张照片换个存法就会翻面。

## 为什么不放大

入库侧无论如何会缩回 `features.LONG_EDGE`(640)，放大只是先插值再缩回去，白走
一趟还多一次重采样损失。真正能增加信息量的旋钮在查询侧
（`backend.QUERY_LONG_EDGE`，实测 1280 是拐点），不在这里。
"""

from __future__ import annotations

import cv2
import numpy as np

#: CLAHE 的 clipLimit。**这一档是负面结论，留着只为可复现**（见模块 docstring
#: 结论一：clip=4 时真机帧中位内点从 32 掉到 9）。默认值取 4.0 是因为当初的对比表
#: 是在这个值上跑的，不是因为它好。
CLAHE_CLIP = 4.0

#: CLAHE 的分块数。8×8 在 708×468 上约等于每块 88×58 像素 —— 块再大就退化成
#: 全局直方图均衡（局部对比度那一半的收益没了），再小则每块内样本不足，
#: 直方图噪声会被当成信号放大。
CLAHE_GRID = 8

#: 轻锐化的量与半径。在**真机帧**上扫出来的（见模块 docstring 结论二）：amount
#: 从 0.4 一路到 2.0 收益都一样（过门槛 10/17、中位内点 45），再往上到 3.0 崩掉。
#: 既然 0.4 就吃到了全部收益，就取最轻的那一档 —— 锐化量越大，低尾那几张本来就糊
#: 的帧掉得越多（实测 amount=1.2 时最差的三帧 11/11/15 → 7/10/13）。
#:
#: 注意：这一档**没有接进入库路径**。它在婚礼照的合成帧口径上零收益（40/96 vs
#: 38/96），唯一的正面证据来自拍手机屏幕的帧，覆盖不到印刷件场景。
MILD_AMOUNT = 0.4
MILD_SIGMA = 0.8


def clahe(img_bgr: np.ndarray, clip: float = CLAHE_CLIP, grid: int = CLAHE_GRID) -> np.ndarray:
    """只在 L 通道上做 CLAHE，色度不动。

    在 LAB 的 L 上做而不是分别对 BGR 三通道做：后者会改变色度比例，让红金色调
    的照片整体偏色。而且 arcoreimg 与 ORB 都只看灰度（实测灰度化后 arcoreimg
    分数与原图完全相同），改色度对识别毫无收益、只有副作用。
    """
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def unsharp(img_bgr: np.ndarray, amount: float, sigma: float = 1.0) -> np.ndarray:
    """非锐化掩模。`amount` 过大（实测 ≥2.0）会产生振铃，反而掉分。"""
    blur = cv2.GaussianBlur(img_bgr, (0, 0), sigma)
    return cv2.addWeighted(img_bgr, 1 + amount, blur, -amount, 0)


#: 供 `bench/simcam.py --ref-pre` 与入库路径共用的具名档位。**同一份实现**，
#: 否则 bench 量的是一个东西、上线跑的是另一个。
VARIANTS: dict[str, object] = {
    "none": lambda im: im,
    "clahe2": lambda im: clahe(im, 2.0),
    "clahe4": lambda im: clahe(im, 4.0),
    "clahe8": lambda im: clahe(im, 8.0),
    "mild": lambda im: unsharp(im, MILD_AMOUNT, MILD_SIGMA),
    "unsharp": lambda im: unsharp(im, 1.2),
    "clahe4_unsharp": lambda im: unsharp(clahe(im, 4.0), 1.0),
    "clahe2_unsharp": lambda im: unsharp(clahe(im, 2.0), 1.0),
}


def apply(img_bgr: np.ndarray, name: str) -> np.ndarray:
    fn = VARIANTS.get(name)
    if fn is None:
        raise ValueError(f"未知的参考图预处理档位：{name!r}，可选 {tuple(VARIANTS)}")
    return fn(img_bgr)  # type: ignore[operator]
