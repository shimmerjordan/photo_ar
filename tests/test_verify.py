import numpy as np

from photoar import features as F
from photoar import synth
from photoar import verify as V


def _res(photo_id, inliers, ok=None, det=1.0):
    if ok is None:
        ok = inliers >= V.MIN_INLIERS and V.DET_MIN <= det <= V.DET_MAX
    return V.PairResult(photo_id=photo_id, inliers=inliers, det=det, ok=ok)


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
