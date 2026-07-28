import cv2
import numpy as np

from photoar import features as F


def test_resize_keeps_aspect_and_hits_long_edge(textured_image):
    img = textured_image(seed=1, w=1200, h=800)
    out = F.resize_to_long_edge(img, 640)
    h, w = out.shape[:2]
    assert max(h, w) == 640
    assert abs((w / h) - (1200 / 800)) < 0.02


def test_resize_upscales_small_images(textured_image):
    img = textured_image(seed=2, w=300, h=200)
    out = F.resize_to_long_edge(img, 640)
    assert max(out.shape[:2]) == 640


def test_extract_shapes_and_dtypes(textured_image):
    f = F.extract(textured_image(seed=3))
    assert f.desc.ndim == 2 and f.desc.shape[1] == 32
    assert f.desc.dtype == np.uint8
    assert f.pts.shape == (f.desc.shape[0], 2)
    assert f.pts.dtype == np.float32
    assert 0 < f.desc.shape[0] <= F.N_FEATURES


def test_extract_is_deterministic(textured_image):
    img = textured_image(seed=4)
    a, b = F.extract(img), F.extract(img)
    assert np.array_equal(a.desc, b.desc)
    assert np.array_equal(a.pts, b.pts)


def test_extract_is_scale_normalized(textured_image):
    """尺度对齐硬约束：同一张图放大 2 倍后提取，描述子应与原图高度一致。

    这个测试锁住的是全项目最容易被违反、后果最严重的约束。
    """
    img = textured_image(seed=5, w=1000, h=700)
    big = cv2.resize(img, (2000, 1400), interpolation=cv2.INTER_LINEAR)

    fa, fb = F.extract(img), F.extract(big)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(fa.desc, fb.desc)
    good = [m for m in matches if m.distance <= 32]
    assert len(good) >= 0.5 * min(len(fa.desc), len(fb.desc))


def test_extract_handles_blank_image():
    blank = np.full((400, 600, 3), 128, np.uint8)
    f = F.extract(blank)
    assert f.desc.shape == (0, 32)
    assert f.pts.shape == (0, 2)
