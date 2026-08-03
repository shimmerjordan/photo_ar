"""把参考图变成"手机翻拍打印照片"风格的查询图。

这是 Phase 0 的测试数据来源。真实拍摄测试集是最终验收基线，
但调参迭代必须靠这个——它无需真机、无需网络、完全确定性。

各扰动的现实对应：
  corner_jitter  四角随机位移占图宽/高的比例，模拟斜视角。0.25 约对应 40°
  blur_sigma     高斯模糊，模拟手抖与失焦
  brightness     整体亮度增益，模拟不同光照
  warm_shift     蓝/红通道反向增益，模拟色温偏移
  glare          椭圆高光斑，模拟覆膜反光
  jpeg_quality   JPEG 压缩，模拟客户端上传前的编码损失
"""

import struct
import zlib
from dataclasses import dataclass

import cv2
import numpy as np

from . import features

MAX_CORNER_JITTER = 0.25
MAX_BLUR_SIGMA = 1.5
BRIGHTNESS_RANGE = (0.7, 1.3)
MAX_WARM_SHIFT = 0.15
JPEG_QUALITY_RANGE = (50, 85)
GLARE_PROBABILITY = 0.35

# 合成之前把输入缩到这个长边。
#
# ## 为什么必须有这个上限（实测数字）
#
# `apply` 对每一张样本要做：透视 warp、高斯模糊、**转 float32**、逐通道乘、clip、转回
# uint8、可选眩光、**JPEG 编码 + 解码**。也就是六七遍全图操作加一次 JPEG 往返。这在
# 查询帧尺寸（640–1280）上是几十毫秒，在一张 12MP 的手机原图上是**18 秒一张**
# （那次 float32 转换本身就要分配 144 MB）。
#
# 实测（3 CPU 容器内，一张 3000×4000 的手机照片，6 个样本）：
#
#     全分辨率(4000)   self_score=154   111125 ms   ← 入库直接超时
#     缩到 1920        self_score=150     6385 ms
#     缩到 1280        self_score=144     1371 ms   ← 选它
#     缩到  960        self_score=145      355 ms
#     缩到  640        self_score=130      127 ms
#
# ## 为什么这不是「降低精度」而是去掉浪费
#
# 下游的 `features.extract` **本来就会**把图缩到 `features.LONG_EDGE`(640) 再提特征。
# 也就是说全分辨率 warp 出来的那些像素，97% 在下一步就被扔掉了 —— 111 秒里绝大部分是
# 白算的。
#
# ## 为什么是 1280 而不是 640
#
# 640 会让 12MP 照片的自匹配分掉 24（-15.6%），而 1280 只掉 10（-6.5%），960 与 1280
# 已经在噪声范围内（145 vs 144）。1280 还有一个现成的锚：那正是 App 喂给 ARCore 的
# CPU 图像长边（`Frames.LONG_EDGE`），所以自匹配分是在**相机真正给出的那个尺度**上量的。
#
# ## 混代分数的问题（为什么可以接受）
#
# `self_score` 会存进库，而去重判据 `min(s_new, s_exist) < ratio * m` 要拿新旧两代比。
# 换算法本该配一次全库重算。这里可以不重算，理由是实测的：**已经在库里的照片长边基本都
# ≤1600，在 1280 上限下分数只差 0～1**（demo-a 1600px：109 → 108；wedding-01 708px：
# 不受影响）。真正会变的只有 12MP 手机照片，而它们在改之前**根本入不了库**（超时）。
# 而且变化方向是分数变低 = 去重更保守 = 宁可多报冲突，那是安全的方向。
SYNTH_LONG_EDGE = 1280


@dataclass(frozen=True)
class SynthParams:
    corner_jitter: float
    blur_sigma: float
    brightness: float
    warm_shift: float
    glare: bool
    jpeg_quality: int


def sample_params(rng: np.random.Generator) -> SynthParams:
    return SynthParams(
        corner_jitter=float(rng.uniform(0.0, MAX_CORNER_JITTER)),
        blur_sigma=float(rng.uniform(0.0, MAX_BLUR_SIGMA)),
        brightness=float(rng.uniform(*BRIGHTNESS_RANGE)),
        warm_shift=float(rng.uniform(-MAX_WARM_SHIFT, MAX_WARM_SHIFT)),
        glare=bool(rng.random() < GLARE_PROBABILITY),
        jpeg_quality=int(rng.integers(JPEG_QUALITY_RANGE[0], JPEG_QUALITY_RANGE[1] + 1)),
    )


def _warp(img: np.ndarray, jitter: float, rng: np.random.Generator) -> np.ndarray:
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    offsets = rng.uniform(-jitter, jitter, size=(4, 2)) * np.float32([w, h])
    dst = (src + offsets).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(
        img, M, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _glare(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    h, w = img.shape[:2]
    # M15：w、h 小于约 8px 时 w//8 == w//3 == 0（3~7px 时两者虽不相等但
    # w//3 仍可能是 0），rng.integers(low, high) 在 low>=high 时直接抛
    # ValueError；即使 low<high，抽到的半径也可能是 0，让下游
    # cv2.GaussianBlur(..., sigmaX=rx/3.0) 因核大小非正而断言失败。用
    # max(low + 1, high) 保证区间非空，rx/ry 再用 max(1, ...) 保证半径至少
    # 是 1px——真实入库照片不会这么小，但 synth 是测试基础设施，任何调用方
    # 传一张随手构造的极小图都不应该让它崩溃。
    cx = int(rng.integers(w // 4, max(w // 4 + 1, 3 * w // 4)))
    cy = int(rng.integers(h // 4, max(h // 4 + 1, 3 * h // 4)))
    rx = max(1, int(rng.integers(w // 8, max(w // 8 + 1, w // 3))))
    ry = max(1, int(rng.integers(h // 8, max(h // 8 + 1, h // 3))))

    mask = np.zeros((h, w), np.float32)
    cv2.ellipse(mask, (cx, cy), (rx, ry), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(rx, ry) / 3.0)
    strength = float(rng.uniform(60.0, 140.0))

    out = img.astype(np.float32) + mask[:, :, None] * strength
    return np.clip(out, 0, 255).astype(np.uint8)


def _params_seed(p: SynthParams) -> int:
    """把 SynthParams 的字段确定性地派生成一个 [0, 2**32) 的 rng seed。

    Minor #4：旧实现用内置 hash((浮点数/bool/int 组成的 tuple))。一位
    审阅者实测确认这在当前 CPython 下跨进程稳定——PYTHONHASHSEED 的随机
    加盐只作用于 str/bytes/datetime，不作用于 float/bool/int 组成的
    tuple——所以今天没有活 bug。但这份稳定性从未被测试钉住、也从未被
    写进文档，纯属"恰好如此"：SynthParams 每个里程碑都在单独进程里跑
    （0a/0b/0d 各自的 measure 脚本、CLI 的每次 photoar eval 调用），如果
    以后随手给 SynthParams 加一个 str 字段（比如给某种扰动加个可读的
    "profile" 标签），就会在完全不触碰这个函数的情况下，悄悄让
    phase0-results.md 里已经记录的每一个准确率数字失去可复现性——没有
    任何测试会因此变红，因为 hash() 本身没有变，变的是它对 str 的加盐
    行为。换成 struct.pack + zlib.crc32（都是标准库）：只处理定长的
    float64/bool/int32 字段，字节布局用 "<" 显式钉死小端，不掺入任何
    Python 对象哈希，因而不依赖 PYTHONHASHSEED，也不会在未来加了 str
    字段时静默变化（除非真的把 str 字段也塞进这个 pack 里，那是显式改
    动，会被下面 test_synth.py 里钉住具体数值的测试立刻测出来）。

    Verified（wave 2 报告里也记录了这次验证）：换算法必然会让派生出的
    具体 seed 整数变化（hash() 的 tuple 哈希算法与 crc32(struct.pack(...))
    在数学上就是两回事），所以同一组 SynthParams 生成的合成图片字节确实
    会变——但 corner_jitter/blur_sigma 等参数本身的取值范围/分布完全不变，
    变的只是"用哪一组具体随机数来实现这次扰动"，不是扰动的统计特性。
    """
    payload = struct.pack(
        "<dddd?i",
        p.corner_jitter, p.blur_sigma, p.brightness, p.warm_shift,
        p.glare, p.jpeg_quality,
    )
    return zlib.crc32(payload)


def apply(img_bgr: np.ndarray, p: SynthParams) -> np.ndarray:
    # 用参数自身派生 rng，让 apply 对同一 params 也是确定性的
    rng = np.random.default_rng(_params_seed(p))

    out = img_bgr
    if p.corner_jitter > 0:
        out = _warp(out, p.corner_jitter, rng)
    if p.blur_sigma > 0:
        out = cv2.GaussianBlur(out, (0, 0), sigmaX=p.blur_sigma)

    f = out.astype(np.float32) * p.brightness
    if out.ndim == 3:
        # BGR：warm_shift > 0 偏暖（红增蓝减）
        f[:, :, 2] *= 1.0 + p.warm_shift
        f[:, :, 0] *= 1.0 - p.warm_shift
    out = np.clip(f, 0, 255).astype(np.uint8)

    if p.glare:
        out = _glare(out, rng)

    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), p.jpeg_quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def generate(
    img_bgr: np.ndarray,
    count: int,
    seed: int,
    long_edge: int | None = SYNTH_LONG_EDGE,
) -> list[tuple[np.ndarray, SynthParams]]:
    """造 [count] 张「手机翻拍」风格的查询图。

    @param long_edge 先把输入缩到这个长边再合成。理由与实测数字见 [SYNTH_LONG_EDGE]
        那段注释（一句话版：不缩的话一张 12MP 手机照片要 111 秒，而其中 97% 的像素在
        下一步提特征时就被扔掉了）。给 None 表示不缩 —— 只有在**刻意**要复现旧行为
        或者研究分辨率影响时才这么用（`bench/` 里那类脚本）。

    比 [count] 大的图会被缩，小的**不动**（`resize_to_long_edge` 只在尺寸不等时才动，
    放大一张小图不会凭空造出细节，只会让 warp 更慢）。
    """
    if long_edge is not None and max(img_bgr.shape[:2]) > long_edge:
        img_bgr = features.resize_to_long_edge(img_bgr, long_edge)
    rng = np.random.default_rng(seed)
    return [(apply(img_bgr, p), p) for p in (sample_params(rng) for _ in range(count))]
