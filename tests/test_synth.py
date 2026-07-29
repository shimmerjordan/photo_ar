import os
import subprocess
import sys

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


# ---------------------------------------------------------------------------
# M15（最终审阅追加）：_glare 用 rng.integers(w // 8, w // 3) 取高光半径，
# 图片小于约 8px 宽/高时 w//8 == w//3 == 0，low==high 让 rng.integers 直接
# 抛 ValueError；3~7px 之间 low<high 但抽到的半径可能是 0，导致下游
# cv2.GaussianBlur(..., sigmaX=0) 因核大小非正而断言失败——两种崩溃、同一个
# 根因（半径计算没考虑极小图）。真实入库照片不会这么小，但 synth 是"随手
# 构造一张图就能跑"的测试基础设施，边界档不住会让写测试的人猝不及防。
# ---------------------------------------------------------------------------


def test_glare_does_not_crash_on_tiny_images():
    for size in (1, 2, 3, 4, 5, 7, 8):
        img = np.zeros((size, size, 3), np.uint8)
        p = synth.SynthParams(
            corner_jitter=0.0, blur_sigma=0.0, brightness=1.0,
            warm_shift=0.0, glare=True, jpeg_quality=85,
        )
        out = synth.apply(img, p)  # 不应该抛异常
        assert out.shape == img.shape


# ---------------------------------------------------------------------------
# Minor #4（最终审阅追加）：apply() 原来用内置 hash((四个 float, 一个 bool,
# 一个 int) 的 tuple) 派生内部 rng 的 seed。审阅者实测确认这在当前 CPython
# 下跨进程稳定——PYTHONHASHSEED 的随机加盐只作用于 str/bytes/datetime，不
# 作用于 float/bool/int 组成的 tuple——所以今天没有活 bug。但这份稳定性
# 完全没有被测试钉住：谁都可能在未来给 SynthParams 加一个 str 字段（比如
# 一个可读的"profile"标签）而不觉得这有什么问题，届时 hash() 就会因为
# tuple 里混进了 str 而开始跨进程变化，静默摧毁 phase0-results.md 里记录
# 的每一个准确率数字的可复现性——没有任何测试会变红，因为改的是加了新
# 字段这件事本身，不是这个函数。换成 struct.pack + zlib.crc32（都是标准
# 库、显式小端字节布局）之后不再有这个隐患。
# ---------------------------------------------------------------------------


def test_params_seed_is_pinned_for_a_known_synthparams():
    """钉住一组已知 SynthParams 派生出的具体 seed 数值。以后不管是换派生
    算法，还是不小心往 struct.pack 的格式串/参数列表里漏加或多加一个
    字段，都会被这条断言的失败立刻捕捉到——这正是旧的 hash() 实现缺的
    那道防线：换派生方式那次改动本身没有任何测试会变红。"""
    p = synth.SynthParams(
        corner_jitter=0.1, blur_sigma=0.5, brightness=1.1,
        warm_shift=-0.05, glare=True, jpeg_quality=70,
    )
    assert synth._params_seed(p) == 1180488566


def test_params_seed_is_independent_of_pythonhashseed():
    """Minor #4 的核心担忧不是"今天会不会崩"，而是"这份稳定性有没有被
    强制"。换成 struct.pack+crc32 后，即使故意用不同的 PYTHONHASHSEED 起
    三个独立子进程，派生出的 seed 也必须完全一致——不再依赖"SynthParams
    里从来没有 str 字段"这条从未被代码检查过的隐藏前提。"""
    code = (
        "from photoar.synth import SynthParams, _params_seed\n"
        "p = SynthParams(0.1, 0.5, 1.1, -0.05, True, 70)\n"
        "print(_params_seed(p))\n"
    )
    seeds = set()
    for hashseed in ("0", "1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed)
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, env=env, check=True,
        )
        seeds.add(out.stdout.strip())
    assert len(seeds) == 1, f"PYTHONHASHSEED 不应该影响派生结果，实际得到 {seeds}"
