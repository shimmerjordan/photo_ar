"""近似重复检测的测试。

这个模块决定"干净语料"到底干净不干净，所以它自己错了不会有任何报错——
只会让 0d 的数字悄悄失真。因此除了常规的参数校验，重点测三件事：

1. 真近重复被抓到、真不同的照片不被抓到（**同一个测试里两条都断言**，
   否则一个恒返回"全是重复"的实现也能过第一条）。
2. 传递性：A≈B、B≈C 必须并成一簇。
3. det 出界时高内点数不算重复（隔离测试，用替身把 verify_pair 的返回值
   固定住——真实图片很难稳定构造出"内点数高但 det 出界"的样本，而这条
   分支恰恰是防镜像/极端缩放的那道闸）。
"""

import cv2
import numpy as np
import pytest

from photoar import dedup
from photoar.features import extract
from photoar.verify import MIN_INLIERS, PairResult


def _all_others(n: int) -> list[list[int]]:
    """小语料下的候选表：每张的候选是其余全部（已排除自己）。

    单元测试刻意不经词汇树/倒排索引拿候选：那是粗排的职责、有自己的测试，
    混进来会让本模块的失败原因变得不可归因。"""
    return [[j for j in range(n) if j != i] for i in range(n)]


def _reencode(img: np.ndarray, quality: int = 70, scale: float = 0.95) -> np.ndarray:
    """造一个真实意义上的近似重复：重新编码 + 轻微缩放。

    这正是内容哈希挡不住的那一类——字节完全不同，内容是同一张。"""
    h, w = img.shape[:2]
    small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


class TestScanPairs:
    def test_finds_reencoded_duplicate_and_leaves_distinct_photos_alone(
        self, textured_image
    ):
        """一次断言两侧：重复对被抓到，且不相似的对一个都没被抓到。

        只断言前者的话，一个"把所有候选都判成重复"的实现也能过——那正是
        本项目反复撞见的假校验模式。"""
        base = textured_image(0)
        imgs = [base, _reencode(base), textured_image(1), textured_image(2)]
        feats = [extract(im) for im in imgs]

        report = dedup.scan_pairs(feats, _all_others(len(feats)))

        assert report.dup_pairs == [(0, 1)], (
            f"期望只有 (0,1) 这一对近重复，实际 {report.dup_pairs}；"
            f"全部候选对得分 {report.pair_scores}"
        )
        # 顺带把"空档"这条 0d 的关键证据在测试里也钉住：重复对的内点数应当
        # 远高于阈值，不相似对应当远低于阈值。二者贴着阈值才是危险状态。
        assert report.pair_scores[(0, 1)] >= MIN_INLIERS * 2
        distinct = [v for k, v in report.pair_scores.items() if k != (0, 1)]
        assert distinct and max(distinct) < MIN_INLIERS

    def test_counts_every_verification(self, textured_image):
        feats = [extract(textured_image(s)) for s in range(4)]
        report = dedup.scan_pairs(feats, _all_others(4))
        assert report.n_verify_pair == 4 * 3          # 每张 3 个候选，双向都测
        assert len(report.pair_scores) == 4 * 3 // 2  # 但只有 6 个不同的对

    def test_keeps_the_higher_score_across_both_directions(self, monkeypatch):
        """verify_pair 不完全对称，(i,j) 与 (j,i) 可能给出不同内点数；
        任一方向达标就应判为重复，所以取较大者。

        分数刻意让**先**被测到的那个方向更高（99 然后 10）。反过来写
        （10 然后 99）这条测试就是假的：一个"后写覆盖前写"的错误实现也会
        得到 99 而通过。"""
        per_call = [99, 10]
        calls = []

        def fake(query, ref, photo_id):
            # query/ref 是同一个替身对象，方向只能靠调用顺序区分：第一次是
            # 0->1，第二次是 1->0。
            calls.append(photo_id)
            return PairResult(
                photo_id=photo_id, inliers=per_call[len(calls) - 1], det=1.0, ok=True
            )

        monkeypatch.setattr(dedup, "verify_pair", fake)
        report = dedup.scan_pairs([object(), object()], [[1], [0]], min_inliers=50)
        assert calls == ["1", "0"], "候选表决定了调用顺序，先 0->1 再 1->0"
        assert report.pair_scores[(0, 1)] == 99
        assert report.dup_pairs == [(0, 1)]

    def test_det_out_of_range_scores_zero_even_with_many_inliers(self, monkeypatch):
        """镜像 / 极端缩放：内点数再高也不可信，必须记 0 分。

        这条分支在真实图片上很难稳定复现，所以用替身隔离——与 verify 那边
        拒绝镜像的回归测试同一个理由。"""
        monkeypatch.setattr(
            dedup,
            "verify_pair",
            lambda query, ref, photo_id: PairResult(
                photo_id=photo_id, inliers=300, det=-1.0, ok=False
            ),
        )
        report = dedup.scan_pairs([object(), object()], [[1], [0]])
        assert report.pair_scores[(0, 1)] == 0
        assert report.dup_pairs == []

    def test_dup_pairs_order_is_deterministic_on_ties(self, monkeypatch):
        monkeypatch.setattr(
            dedup,
            "verify_pair",
            lambda query, ref, photo_id: PairResult(
                photo_id=photo_id, inliers=100, det=1.0, ok=True
            ),
        )
        report = dedup.scan_pairs([object()] * 4, _all_others(4))
        assert report.dup_pairs == [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]

    def test_reports_progress(self, textured_image):
        seen = []
        feats = [extract(textured_image(s)) for s in range(3)]
        dedup.scan_pairs(feats, _all_others(3), on_progress=lambda i, n: seen.append((i, n)))
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_rejects_self_in_candidates(self, textured_image):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="自己"):
            dedup.scan_pairs(feats, [[0, 1], [0]])

    def test_rejects_out_of_range_candidate(self, textured_image):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="越界"):
            dedup.scan_pairs(feats, [[5], [0]])

    def test_rejects_length_mismatch(self, textured_image):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="长度必须一致"):
            dedup.scan_pairs(feats, [[1]])

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_nonpositive_min_inliers(self, textured_image, bad):
        """阈值 <= 0 会把每一个被校验过的对都判成重复（未过 det 的记 0 分也
        满足 >= 0），整个语料并成一簇只留一张。必须报错，不能静默执行。"""
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="min_inliers"):
            dedup.scan_pairs(feats, _all_others(2), min_inliers=bad)


class TestCluster:
    def test_is_transitive(self):
        """A≈B、B≈C 必须并成一簇。逐对删除的实现会在这里留下两张仍互为
        重复的照片。"""
        assert dedup.cluster([(0, 1), (1, 2)], 5) == [[0, 1, 2], [3], [4]]

    def test_result_is_independent_of_pair_order(self):
        a = dedup.cluster([(0, 1), (1, 2), (3, 4)], 5)
        b = dedup.cluster([(3, 4), (1, 2), (0, 1)], 5)
        assert a == b == [[0, 1, 2], [3, 4]]

    def test_partitions_every_index_including_singletons(self):
        clusters = dedup.cluster([(1, 3)], 5)
        assert sorted(i for m in clusters for i in m) == [0, 1, 2, 3, 4]
        assert clusters == [[0], [1, 3], [2], [4]]

    def test_no_pairs_gives_all_singletons(self):
        assert dedup.cluster([], 3) == [[0], [1], [2]]

    def test_rejects_out_of_range_pair(self):
        with pytest.raises(ValueError, match="越界"):
            dedup.cluster([(0, 3)], 3)


class TestSelectKeep:
    def test_keeps_smallest_key_not_smallest_index(self):
        """keys 的顺序刻意与下标顺序相反：若实现偷懒直接取 min(members)，
        这条会失败。反过来说，keys 与下标同序的测试是测不出区别的。"""
        clusters = [[0, 1, 2], [3]]
        keys = ["d", "c", "b", "a"]
        assert dedup.select_keep(clusters, keys) == [2, 3]

    def test_is_deterministic(self):
        clusters = dedup.cluster([(0, 2), (1, 4)], 5)
        keys = [f"p{i}.jpg" for i in range(5)]
        assert dedup.select_keep(clusters, keys) == [0, 1, 3]

    def test_rejects_empty_cluster(self):
        with pytest.raises(ValueError, match="簇不能为空"):
            dedup.select_keep([[]], ["a"])


def test_end_to_end_removes_exactly_the_duplicates(textured_image):
    """scan -> cluster -> select_keep 串起来：一组含两个重复的照片里，
    保留数必须正好等于不同内容的张数。"""
    base = textured_image(0)
    other = textured_image(1)
    imgs = [base, _reencode(base), _reencode(base, quality=60, scale=0.9),
            other, textured_image(2)]
    feats = [extract(im) for im in imgs]
    names = [f"{i:02d}.jpg" for i in range(len(imgs))]

    report = dedup.scan_pairs(feats, _all_others(len(feats)))
    clusters = dedup.cluster(report.dup_pairs, len(feats))
    keep = dedup.select_keep(clusters, names)

    assert clusters == [[0, 1, 2], [3], [4]]
    assert keep == [0, 3, 4]
