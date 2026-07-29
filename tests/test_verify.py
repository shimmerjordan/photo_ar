import numpy as np

from photoar import features as F
from photoar import synth
from photoar import verify as V


def _res(photo_id, inliers, ok=None, det=1.0):
    if ok is None:
        ok = inliers >= V.MIN_INLIERS and V.DET_MIN <= det <= V.DET_MAX
    return V.PairResult(photo_id=photo_id, inliers=inliers, det=det, ok=ok)


def test_ransac_max_iters_is_capped_at_200():
    """Pin the RANSAC iteration cap: 200 vs default 2000. The cost is only on non-matches
    (true matches use adaptive termination, ~0.34 ms regardless). Measured with 12 true pairs
    and 12 false pairs: lowering from 2000 to 200 keeps true-match inlier counts byte-identical,
    nudges false-match counts slightly down (safer), and cuts false-match cost from 20.56 ms
    to 2.19 ms — a 10× improvement for Top-20 refinement without changing verdicts.

    This cap limits effort on hard cases. RANSAC needs roughly log(1-p)/log(1-w^4) iterations
    for a 4-point model at inlier ratio w; 200 covers w ≥ 0.37, while 500 covers w ≥ 0.29.
    Badly distorted real photos could land near 0.3, so a marginal true match may become a miss
    — the acceptable direction, since misses cost an order of magnitude less than false positives.
    Milestone 0d on real photos will reveal whether 200 is too tight; raising this knob costs
    time only on non-matches.
    """
    assert V.RANSAC_MAX_ITERS == 200


def test_verify_pair_matches_image_against_its_own_synthetic_query(textured_image):
    img = textured_image(seed=1, w=1000, h=700)
    ref = F.extract(img)
    query_img, _ = synth.generate(img, count=1, seed=5)[0]
    r = V.verify_pair(F.extract(query_img), ref, "p1")
    assert r.ok
    assert r.inliers >= V.MIN_INLIERS


def test_verify_pair_rejects_unrelated_images(textured_image):
    q = F.extract(textured_image(seed=1))
    ref = F.extract(textured_image(seed=999))
    r = V.verify_pair(q, ref, "p2")
    assert not r.ok


def test_verify_pair_handles_empty_features():
    empty = F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8))
    r = V.verify_pair(empty, empty, "p3")
    assert r.inliers == 0
    assert not r.ok


def test_verify_pair_rejects_mirrored_match(textured_image):
    """镜像的单应矩阵行列式为负。实体照片经相机成像永远不会镜像，
    因此负行列式必须判否——这比 spec 写的 abs(det) 更严格且更正确。
    """
    import cv2

    img = textured_image(seed=2, w=800, h=600)
    ref = F.extract(img)
    mirrored = F.extract(cv2.flip(img, 1))
    r = V.verify_pair(mirrored, ref, "p4")
    assert not r.ok or r.det > 0


def test_mirrored_match_is_rejected_by_determinant_sign_not_inlier_count(textured_image):
    """Isolate and directly test the signed-determinant rejection mechanism.

    This test constructs a case where:
    - Identical descriptors → BFMatcher matches every keypoint 1:1 → all become inliers
    - Horizontally mirrored points → homography has negative determinant
    - Inlier count is well above MIN_INLIERS

    The three assertions below are ALL ESSENTIAL to prove the determinant sign is what
    decides rejection. If any assertion is dropped, the test becomes vacuous:
    1. r.inliers >= V.MIN_INLIERS  — proves the inlier count check did NOT reject it
    2. r.det < 0  — proves we are genuinely in the mirrored (reflection) case
    3. not r.ok  — proves the overall result is rejection

    Dropping assertion 1 would let a low-inlier match pass through; dropping assertion 2
    loses the mirror-specificity; dropping assertion 3 inverts the test logic.
    Without all three, the test could pass for the wrong reason and fail to catch a
    regression to abs(det).
    """
    img = textured_image(seed=3, w=800, h=600)
    query = F.extract(img)

    # Get the resized image dimensions (extract() uses LONG_EDGE=640)
    resized = F.resize_to_long_edge(img)
    resized_w = resized.shape[1]

    # Create reference with identical descriptors but points mirrored horizontally
    # (mirrored in the 640-long-edge coordinate space, not the original)
    mirrored_pts = query.pts.copy()
    mirrored_pts[:, 0] = (resized_w - 1) - mirrored_pts[:, 0]
    ref = F.Features(pts=mirrored_pts, desc=query.desc)

    r = V.verify_pair(query, ref, "test_mirror")

    # All three assertions are critical — see docstring
    assert r.inliers >= V.MIN_INLIERS, f"Expected >= {V.MIN_INLIERS} inliers, got {r.inliers}"
    assert r.det < 0, f"Expected negative determinant, got {r.det}"
    assert not r.ok, "Expected rejection by determinant sign"


def test_decide_returns_no_match_on_empty_results():
    d = V.decide([])
    assert not d.matched
    assert d.photo_id is None


def test_decide_rejects_when_best_fails_inlier_threshold():
    d = V.decide([_res("a", V.MIN_INLIERS - 1)])
    assert not d.matched
    assert d.reason == "weak"


def test_decide_accepts_clear_single_winner():
    d = V.decide([_res("a", 60), _res("b", 5)])
    assert d.matched
    assert d.photo_id == "a"
    assert d.inliers == 60


def test_decide_accepts_when_only_one_candidate():
    d = V.decide([_res("a", 40)])
    assert d.matched
    assert d.photo_id == "a"


def test_decide_rejects_ambiguous_pair():
    """第一名 30、第二名 25：30 < 1.5*25=37.5，判否。
    这一条是压住误识别率的关键，宁可漏检。
    """
    d = V.decide([_res("a", 30), _res("b", 25)])
    assert not d.matched
    assert d.reason == "ambiguous"


def test_ratio_test_counts_candidates_below_inlier_threshold():
    """第二名 24 分（自身未过 MIN_INLIERS）也必须参与比值检验：
    第一名 26 < 1.5*24=36，判否。只在通过者之间比会放过这类歧义。
    """
    d = V.decide([_res("a", 26), _res("b", 24)])
    assert not d.matched
    assert d.reason == "ambiguous"


def test_decide_rejects_out_of_range_determinant():
    d = V.decide([_res("a", 80, ok=False, det=0.001)])
    assert not d.matched


# ---------------------------------------------------------------------------
# decide_with：同一套判定的参数化版本，供阈值扫描重放录好的候选分数。
# 0d 上规模跑出库外误识别 6.951%，要回答"阈值调到多少能关掉、代价多少漏检"，
# 重跑一次 eval 是 54 分钟，一个 5×5 网格 22 小时——所以必须能离线重放。
# ---------------------------------------------------------------------------


class TestDecideWith:
    def test_defaults_are_byte_identical_to_decide(self):
        """默认参数下必须与 decide 完全一致，否则重放口径和产品口径就是两套。

        遍历覆盖四种结局（ok / weak / ambiguous / empty）的候选表，逐个比对
        整个 Decision（不只是 matched）——reason 也不能变，它是判断"这次不匹配
        是因为分不够还是因为歧义"的唯一依据。
        """
        cases = [
            [],
            [_res("a", 0)],
            [_res("a", 24)],
            [_res("a", 100)],
            [_res("a", 100), _res("b", 20)],
            [_res("a", 30), _res("b", 25)],
            [_res("a", 26), _res("b", 24)],
            [_res("a", 80, ok=False, det=0.001)],
            [_res("a", 80, ok=False, det=-1.2)],
            [_res("a", 60), _res("b", 30), _res("c", 10)],
        ]
        for results in cases:
            assert V.decide_with(results) == V.decide(results), f"候选表 {results} 上不一致"

    def test_does_not_read_the_precomputed_ok_flag(self):
        """PairResult.ok 是 verify_pair 用**模块常量**算的，重放时那个值正是
        要被替换掉的东西。这里给一个"分数足够但 ok=False"的候选：如果
        decide_with 读了 ok，它会判 weak；正确行为是按传入阈值重算后判 ok。
        """
        stale = V.PairResult(photo_id="a", inliers=100, det=1.0, ok=False)
        d = V.decide_with([stale])
        assert d.matched and d.reason == "ok", "decide_with 不能相信 ok 字段"

    def test_raising_min_inliers_turns_a_hit_into_weak(self):
        results = [_res("a", 40), _res("b", 10)]
        assert V.decide_with(results, min_inliers=25).matched
        d = V.decide_with(results, min_inliers=50)
        assert not d.matched and d.reason == "weak"

    def test_raising_min_inliers_does_not_promote_the_runner_up(self):
        """提高 min_inliers 后 top1 过不了，不能让 top2 顶上来——这是 decide
        的既有语义（top1 不过直接判 weak），也是"录 top1/top2 就够"这个结论
        成立的前提。top2 是 90 分，足以过任何这里用到的阈值，但仍不该被选中。
        """
        d = V.decide_with([_res("a", 95), _res("b", 90)], min_inliers=94)
        assert not d.matched
        assert d.photo_id is None
        assert d.inliers == 95, "报的应该是 top1 的分数，不是 top2 的"

    def test_raising_ratio_turns_a_hit_into_ambiguous(self):
        results = [_res("a", 100), _res("b", 50)]
        assert V.decide_with(results, ratio=1.5).matched  # 100 >= 75
        d = V.decide_with(results, ratio=2.5)  # 100 < 125
        assert not d.matched and d.reason == "ambiguous"

    def test_det_window_is_also_parameterizable(self):
        """det 区间不是本轮要扫的旋钮，但一并参数化：否则重放时它会悄悄用
        模块常量，而调用方以为自己控制了全部判定条件。
        """
        results = [_res("a", 80, ok=False, det=30.0)]
        assert not V.decide_with(results).matched
        assert V.decide_with(results, det_max=50.0).matched

    def test_passes_is_the_single_source_for_the_first_two_conditions(self):
        """verify_pair 的 ok 与 decide_with 的前两条判定必须来自同一个实现。
        随机撒一批 (inliers, det) 组合，两边算出来的必须一致——两边各写一遍
        表达式的话，改了一边忘了另一边不会有任何报错，重放数字会静默失真。
        """
        rng = np.random.default_rng(0)
        for _ in range(200):
            inliers = int(rng.integers(0, 60))
            det = float(rng.uniform(-2.0, 30.0))
            expected = V.PairResult(photo_id="a", inliers=inliers, det=det, ok=False)
            by_verify_pair = V._passes(inliers, det, V.MIN_INLIERS, V.DET_MIN, V.DET_MAX)
            by_decide = V.decide_with([expected]).reason != "weak"
            assert by_verify_pair == by_decide, f"inliers={inliers} det={det} 两边判定不一致"
