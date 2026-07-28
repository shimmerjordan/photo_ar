import cv2
import numpy as np

from photoar import features as F
from photoar import synth


def test_sample_params_in_documented_ranges():
    rng = np.random.default_rng(0)
    for _ in range(200):
        p = synth.sample_params(rng)
        assert 0.0 <= p.corner_jitter <= 0.25
        assert 0.0 <= p.blur_sigma <= 1.5
        assert 0.7 <= p.brightness <= 1.3
        assert -0.15 <= p.warm_shift <= 0.15
        assert 50 <= p.jpeg_quality <= 85
        assert isinstance(p.glare, bool)


def test_apply_preserves_shape_and_dtype(textured_image):
    img = textured_image(seed=1)
    p = synth.sample_params(np.random.default_rng(7))
    out = synth.apply(img, p)
    assert out.shape == img.shape
    assert out.dtype == np.uint8


def test_generate_is_reproducible(textured_image):
    img = textured_image(seed=2)
    a = synth.generate(img, count=5, seed=42)
    b = synth.generate(img, count=5, seed=42)
    assert len(a) == 5
    for (ia, pa), (ib, pb) in zip(a, b):
        assert np.array_equal(ia, ib)
        assert pa == pb


def test_generate_varies_between_seeds(textured_image):
    img = textured_image(seed=3)
    a = synth.generate(img, count=3, seed=1)
    b = synth.generate(img, count=3, seed=2)
    assert not np.array_equal(a[0][0], b[0][0])


def test_synthetic_query_still_matches_its_source(textured_image):
    """核心契约：合成图必须仍然可被匹配回源图，否则生成器过于激进，
    测出来的低召回率是生成器的问题而不是识别管线的问题。
    """
    img = textured_image(seed=4, w=1000, h=700)
    ref = F.extract(img)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    inlier_counts = []
    for query_img, _ in synth.generate(img, count=10, seed=11):
        q = F.extract(query_img)
        if len(q) < 4 or len(ref) < 4:
            inlier_counts.append(0)
            continue
        matches = bf.match(q.desc, ref.desc)
        if len(matches) < 4:
            inlier_counts.append(0)
            continue
        src = q.pts[[m.queryIdx for m in matches]]
        dst = ref.pts[[m.trainIdx for m in matches]]
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
        inlier_counts.append(0 if H is None else int(mask.sum()))

    assert sum(c >= 25 for c in inlier_counts) >= 8


def test_glare_brightens_a_region(textured_image):
    img = np.full((400, 600, 3), 100, np.uint8)
    p = synth.SynthParams(
        corner_jitter=0.0, blur_sigma=0.0, brightness=1.0,
        warm_shift=0.0, glare=True, jpeg_quality=85,
    )
    out = synth.apply(img, p)
    assert int(out.max()) > 130
