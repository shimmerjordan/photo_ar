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

from dataclasses import dataclass

import cv2
import numpy as np

MAX_CORNER_JITTER = 0.25
MAX_BLUR_SIGMA = 1.5
BRIGHTNESS_RANGE = (0.7, 1.3)
MAX_WARM_SHIFT = 0.15
JPEG_QUALITY_RANGE = (50, 85)
GLARE_PROBABILITY = 0.35


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
    cx = int(rng.integers(w // 4, 3 * w // 4))
    cy = int(rng.integers(h // 4, 3 * h // 4))
    rx = int(rng.integers(w // 8, w // 3))
    ry = int(rng.integers(h // 8, h // 3))

    mask = np.zeros((h, w), np.float32)
    cv2.ellipse(mask, (cx, cy), (rx, ry), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(rx, ry) / 3.0)
    strength = float(rng.uniform(60.0, 140.0))

    out = img.astype(np.float32) + mask[:, :, None] * strength
    return np.clip(out, 0, 255).astype(np.uint8)


def apply(img_bgr: np.ndarray, p: SynthParams) -> np.ndarray:
    # 用参数自身派生 rng，让 apply 对同一 params 也是确定性的
    seed = abs(hash((p.corner_jitter, p.blur_sigma, p.brightness,
                     p.warm_shift, p.glare, p.jpeg_quality))) % (2**32)
    rng = np.random.default_rng(seed)

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
    img_bgr: np.ndarray, count: int, seed: int
) -> list[tuple[np.ndarray, SynthParams]]:
    rng = np.random.default_rng(seed)
    return [(apply(img_bgr, p), p) for p in (sample_params(rng) for _ in range(count))]
