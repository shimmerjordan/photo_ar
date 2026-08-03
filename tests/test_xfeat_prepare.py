"""`xfeat.prepare()` 的预处理契约。**这份契约有两份实现**，这里是其中一份。

另一份在 Android 侧（`arview/.../feat/XFeatPreprocess.kt`），因为端上提特征那条路
（`POST /v1/recognize/features`）要求手机产出的描述子与服务端自己提的可以互换。

两份实现不一致**不会报错**，只会让描述子对不上、识别率静默变低 —— 在一个家用部署里
那几乎不可能被归因到「补边补错了一边」。所以这个文件里的数字（`GOLDEN_*`）在
`XFeatPreprocessTest.kt` 里逐字重复了一遍：一份改了另一份不改，两边各有一条测试红。

## 为什么用「2×2 同值块」的合成图

golden 值必须两种语言都能精确复现，而 `cv2.INTER_AREA` 在缩放比不是整数、或者块内
像素平均刚好落在 .5 上时，取整走的是 round-half-to-even（`cvRound`），而 JVM 的
`Math.round` 是 round-half-up —— 那种差异会让 golden 值在两边差 1，然后被误当成
「实现不一致」。

把源图造成每个 2×2 块内四像素完全相同、且长边正好是 640 的两倍，就把这两个坑一次
绕开：缩放比恰好 0.5，块平均恰好等于块值（整数），任何正确的面积平均实现都必然给出
同一个结果。这不是在回避难点 —— 真正需要两边一致的是**通道顺序、缩放目标、补边方向
与取值范围**这四件事，重采样核在 ±1 个灰阶内的差异对描述子没有影响。
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from photoar import xfeat

# 横图（16:9，相机帧的形状）：缩到 640×360，补边只补**下方** 280 行。
GOLDEN_LANDSCAPE_SUM = 156_668_672
# 竖图：缩到 360×640，补边只补**右侧** 280 列。
# 两个和不相等，正好说明补边方向真的被算进去了（同一份内容、只是转置，
# 如果实现对两个方向不加区分，这两个数会相等）。
GOLDEN_PORTRAIT_SUM = 156_683_520


def block_image(h: int, w: int) -> np.ndarray:
    """BGR 合成图，每个 2×2 块内四个像素完全相同。三个通道取不同的线性函数，
    所以「BGR 与 RGB 搞反」会立刻表现成数值不对，而不是肉眼看不出的偏色。"""
    v = (np.arange(h) // 2)[:, None]
    u = (np.arange(w) // 2)[None, :]
    b = (17 * u + 5 * v) % 256
    g = (31 * u + 11 * v) % 256
    r = (7 * u + 23 * v) % 256
    return np.stack([b, g, r], axis=-1).astype(np.uint8)


def expect_rgb(u: int, v: int) -> tuple[int, int, int]:
    """缩放后画布上 (v, u) 处应有的 (R, G, B)。"""
    return (
        (7 * u + 23 * v) % 256,
        (31 * u + 11 * v) % 256,
        (17 * u + 5 * v) % 256,
    )


def mirror(i: int, n: int) -> int:
    """BORDER_REFLECT_101：越过边界的下标折回 `2*(n-1) - i`（不重复边界那一行）。"""
    return i if i < n else 2 * (n - 1) - i


# ---- 形状与值域 ----


def test_shape_and_dtype():
    t, size = xfeat.prepare(block_image(720, 1280))
    assert t.shape == (1, 3, xfeat.CANVAS, xfeat.CANVAS)
    assert t.dtype == np.float32
    assert size.dtype == np.int64 and size.tolist() == [360, 640]


def test_画布固定640_学不了ORB查询侧抬到1280那一手():
    """这条测试挡的是一次很自然的"对齐两条路"。

    ORB 路径的查询侧已经与入库侧解耦：入库 640/300 特征，查询
    `backend.QUERY_LONG_EDGE`(1280) / `QUERY_N_FEATURES`(4000)。那一步是 ORB 召回率
    的主导变量（真机扫不出来就是查询侧还在 640 上处理）。看到这个差异的人会想把端上
    提特征那条路也"抬上去"。

    抬不了，而且抬的方式是错的：画布边长是 ONNX 图的固定输入形状 (1,3,640,640)，
    `prepare` 无论收到多大的帧都只会缩到 640 —— 改 `CANVAS` 常量不会改模型，只会让
    onnxruntime 在推理时报形状不匹配（或者更糟，`prepare` 这一侧改了、导出的图没改，
    表现成"描述子对不上、识别率静默变低"）。真要 1280 得重新导出模型，且新描述子与
    全库不可比，那是一次重建全库。

    所以这里锁的是一个比"不超过 640"更强的事实：**有效区长边恒等于 640**。大帧缩下来、
    小帧放大上去，两头都归一到同一个尺度 —— 这条路上"送多大的帧"根本不是一个旋钮，
    改客户端分辨率对它一点影响都没有（ORB 那边恰恰相反）。
    """
    for h, w in ((240, 320), (720, 1280), (1080, 1920), (2160, 3840)):
        t, size = xfeat.prepare(block_image(h, w))
        assert t.shape == (1, 3, xfeat.CANVAS, xfeat.CANVAS)
        # 恒等于而不是"不超过"：320 宽的小帧也会被放大到 640，不是留在 320。
        assert max(size.tolist()) == xfeat.CANVAS, f"{h}x{w} → {size.tolist()}"


def test_values_stay_in_0_255_not_normalized():
    """契约第 4 条：**不**除 255。

    除了会静默让描述子全变 —— InstanceNorm 抹掉全局尺度，所以模型在 0..1 上也照样
    输出「像样」的描述子，只是与库里那批不在同一个空间。
    """
    t, _ = xfeat.prepare(block_image(720, 1280))
    assert float(t.max()) > 1.0
    assert 0.0 <= float(t.min()) and float(t.max()) <= 255.0


def test_channel_order_is_rgb():
    """契约第 1 条：BGR → RGB。第 0 个平面必须是 **R**。"""
    t, _ = xfeat.prepare(block_image(720, 1280))
    r, g, b = expect_rgb(23, 17)
    assert (t[0, 0, 17, 23], t[0, 1, 17, 23], t[0, 2, 17, 23]) == (r, g, b)
    # 反过来一定不成立，否则这条测试对「反了」是瞎的
    assert r != b


# ---- 缩放与补边 ----


@pytest.mark.parametrize(
    "h,w,nh,nw",
    [
        (720, 1280, 360, 640),  # 16:9 横（相机帧）
        (1280, 720, 640, 360),  # 9:16 竖
        (800, 1200, 427, 640),  # 3:2 横（照片），缩放比不是整数
        (640, 640, 640, 640),  # 正方形：一点都不用补
        (400, 500, 512, 640),  # 比画布小：放大而不是缩小
    ],
)
def test_canvas_size_matches_prepare(h, w, nh, nw):
    """`canvas_size` 必须与 `prepare` 真的算出来的有效区一致。

    两者分开之后，`featurebody._check_bounds` 拿 `canvas_size` 去判「关键点有没有落在
    补边区」—— 它算错的话，那道检查会去挡合法请求，或者放过一个补边全错的客户端。
    """
    assert xfeat.canvas_size(h, w) == (nh, nw)
    _, size = xfeat.prepare(block_image(h, w))
    assert size.tolist() == [nh, nw]


def test_padding_is_bottom_only_for_landscape():
    """契约第 3 条：只补右侧与下方。

    横图 nw 恰好等于 CANVAS，所以右侧一列都不该补 —— 也就是说画布最后一列必须是
    真实内容。补成四边居中的话这一列会变成镜像内容，而关键点坐标也会整体平移。
    """
    t, size = xfeat.prepare(block_image(720, 1280))
    nh, nw = size.tolist()
    assert (nh, nw) == (360, 640)
    for x in (0, 313, 639):
        assert tuple(t[0, :, 0, x]) == expect_rgb(x, 0), f"第 0 行 x={x} 不是真实内容"


def test_bottom_padding_mirrors_without_repeating_the_edge():
    t, size = xfeat.prepare(block_image(720, 1280))
    nh, _ = size.tolist()
    # 紧贴边界的第一行补边应当是**倒数第二**行（REFLECT_101 不重复边界行）；
    # 用 BORDER_REFLECT（会重复）的话这一行等于倒数第一行。
    assert np.array_equal(t[0, :, nh, :], t[0, :, nh - 2, :])
    assert not np.array_equal(t[0, :, nh, :], t[0, :, nh - 1, :])
    for y in (nh, nh + 137, xfeat.CANVAS - 1):
        assert np.array_equal(t[0, :, y, :], t[0, :, mirror(y, nh), :])


def test_right_padding_mirrors_for_portrait():
    t, size = xfeat.prepare(block_image(1280, 720))
    _, nw = size.tolist()
    assert nw == 360
    for x in (nw, nw + 91, xfeat.CANVAS - 1):
        assert np.array_equal(t[0, :, :, x], t[0, :, :, mirror(x, nw)])
    # 下方一行都不补：最后一行必须是真实内容
    assert tuple(t[0, :, xfeat.CANVAS - 1, 12]) == expect_rgb(12, xfeat.CANVAS - 1)


def test_extreme_aspect_falls_back_to_replicate():
    """`CANVAS - nh >= nh` 时 REFLECT_101 的下标会算成负数，`prepare` 因此退回
    REPLICATE。Kotlin 侧必须**照抄同一个判据**（而且它是对两个轴一起判的，不是逐轴）。

    这条路真实存在：全景照片 3200×400 缩下来就是 640×80，补边 560 > 80。
    """
    t, size = xfeat.prepare(block_image(400, 3200))
    nh, nw = size.tolist()
    assert (nh, nw) == (80, 640)
    assert xfeat.CANVAS - nh >= nh  # 判据真的成立，测试没跑偏
    # REPLICATE：所有补边行都等于最后一行
    for y in (nh, nh + 200, xfeat.CANVAS - 1):
        assert np.array_equal(t[0, :, y, :], t[0, :, nh - 1, :])


# ---- 跨语言 golden ----


def test_golden_checksums_pin_the_contract_for_kotlin():
    """整张张量的和。**这两个字面量在 Kotlin 侧逐字重复**。

    取和而不是逐值比：640×640×3 个数没法写进两份源码，而和对「通道顺序反了」
    「补边补在了上方」「值域除了 255」这三类错全都敏感（前两者会换掉几十万个值，
    后者直接把和缩小 255 倍）。
    """
    land, _ = xfeat.prepare(block_image(720, 1280))
    port, _ = xfeat.prepare(block_image(1280, 720))
    assert int(land.sum(dtype=np.float64)) == GOLDEN_LANDSCAPE_SUM
    assert int(port.sum(dtype=np.float64)) == GOLDEN_PORTRAIT_SUM
    assert GOLDEN_LANDSCAPE_SUM != GOLDEN_PORTRAIT_SUM


def test_golden_sample_points_pin_the_contract_for_kotlin():
    """几个定点，同样在 Kotlin 侧逐字重复。挑的位置覆盖：真实内容、补边区、
    以及横竖两种补边方向。"""
    land, _ = xfeat.prepare(block_image(720, 1280))
    assert land[0, 0, 17, 23] == 40.0  # R，真实内容
    assert land[0, 1, 359, 639] == 206.0  # G，有效区最后一行最后一列
    assert land[0, 2, 639, 639] == 250.0  # B，下方补边（镜像回第 79 行）
    assert land[0, 0, 500, 300] == 202.0  # R，下方补边（镜像回第 218 行）

    port, _ = xfeat.prepare(block_image(1280, 720))
    assert port[0, 1, 359, 639] == 254.0  # G，右侧补边（镜像回第 79 列）
    assert port[0, 2, 639, 639] == 186.0  # B，右侧补边
    assert port[0, 0, 500, 300] == 32.0  # R，真实内容（x=300 < nw=360）


def test_grayscale_input_is_accepted():
    """单通道输入不该炸。相机帧不会是灰度，但 `cv2.imread` 对某些 PNG 会给单通道。"""
    gray = (np.arange(640 * 480, dtype=np.uint8).reshape(480, 640)) % 251
    t, size = xfeat.prepare(gray)
    assert t.shape == (1, 3, xfeat.CANVAS, xfeat.CANVAS)
    # 灰度展开成三通道，三个平面在有效区内必须相同
    nh, nw = size.tolist()
    assert np.array_equal(t[0, 0, :nh, :nw], t[0, 2, :nh, :nw])


def test_reflect_matches_opencv_directly():
    """镜像下标公式与 OpenCV 自己算的一致。

    自己写一遍 `mirror()` 是因为 Kotlin 侧只能自己写；这条测试保证那个公式不是
    我猜的，而是与 `cv2.copyMakeBorder` 逐像素相同。
    """
    src = block_image(720, 1280)[:360, :640]
    ref = cv2.copyMakeBorder(src, 0, 280, 0, 0, cv2.BORDER_REFLECT_101)
    for y in range(360, 640):
        assert np.array_equal(ref[y], src[mirror(y, 360)])
