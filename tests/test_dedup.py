"""近似重复检测的测试。

这个模块决定"干净语料"到底干净不干净，所以它自己错了不会有任何报错——
只会让 0d 的数字悄悄失真，或者（更糟）静默删掉用户的照片。因此除了常规的
参数校验，重点测四件事：

1. 真近重复被抓到、真不同的照片不被抓到（**同一个测试里两条都断言**，
   否则一个恒返回"全是重复"的实现也能过第一条）。
2. **ratio 判据**：内点数够高但识别器根本不会混淆的对，不能判成重复。
   这是 5058 张真实语料上推翻纯绝对阈值的那条结论（见 dedup 模块 docstring），
   必须有测试钉住，否则改回绝对阈值不会有任何测试变红。
3. **链不等于团**：A—B—C 只需剔掉 B，A 与 C 都要留下。旧的"连通分量每簇
   留一张"实现在真实语料上因此删掉了 14.3% 的照片。
4. det 出界时高内点数不算重复（隔离测试，用替身把 verify_pair 的返回值
   固定住——真实图片很难稳定构造出"内点数高但 det 出界"的样本，而这条
   分支恰恰是防镜像/极端缩放的那道闸）。
"""

import cv2
import numpy as np
import pytest

from photoar import dedup, synth
from photoar.features import extract
from photoar.verify import DEDUP_MIN_INLIERS, PairResult


def _all_others(n: int) -> list[list[int]]:
    """小语料下的候选表：每张的候选是其余全部（已排除自己）。

    单元测试刻意不经词汇树/倒排索引拿候选：那是粗排的职责、有自己的测试，
    混进来会让本模块的失败原因变得不可归因。"""
    return [[j for j in range(n) if j != i] for i in range(n)]


def _self_scores(imgs: list[np.ndarray], samples: int = 3) -> list[int]:
    """真实图片的自匹配分：造扰动查询图，走 dedup.self_score。

    与 bench/dedup_scan.py 里生产路径用的是同一个函数，测试不另造一套。"""
    out = []
    for k, img in enumerate(imgs):
        qs = [extract(q) for q, _ in synth.generate(img, samples, 1000 + k)]
        out.append(dedup.self_score(extract(img), qs))
    return out


def _reencode(img: np.ndarray, quality: int = 70, scale: float = 0.95) -> np.ndarray:
    """造一个真实意义上的近似重复：重新编码 + 轻微缩放。

    这正是内容哈希挡不住的那一类——字节完全不同，内容是同一张。"""
    h, w = img.shape[:2]
    small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _fixed(inliers: int):
    """把 verify_pair 固定成给定内点数的替身工厂。"""
    return lambda query, ref, photo_id: PairResult(
        photo_id=photo_id, inliers=inliers, det=1.0, ok=True
    )


class TestSelfScore:
    def test_takes_median_not_max(self, monkeypatch):
        """取最大值会挑出扰动最轻的那一次、高估识别器的余量，使判据偏向
        "不剔"，把该剔的重复留下来——正是本模块要防的双向漏检。
        刻意让最大值(90)与中位(30)差得远，取错就立刻可见。"""
        vals = [10, 30, 90]
        seen = []

        def fake(query, ref, photo_id):
            seen.append(1)
            return PairResult(photo_id=photo_id, inliers=vals[len(seen) - 1],
                              det=1.0, ok=True)

        monkeypatch.setattr(dedup, "verify_pair", fake)
        assert dedup.self_score(object(), [object()] * 3) == 30

    def test_det_out_of_range_counts_as_zero(self, monkeypatch):
        monkeypatch.setattr(
            dedup, "verify_pair",
            lambda query, ref, photo_id: PairResult(
                photo_id=photo_id, inliers=300, det=-1.0, ok=False),
        )
        assert dedup.self_score(object(), [object()] * 3) == 0

    def test_rejects_no_queries(self):
        with pytest.raises(ValueError, match="不能为空"):
            dedup.self_score(object(), [])


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

        report = dedup.scan_pairs(feats, _all_others(len(feats)), _self_scores(imgs))

        assert report.dup_pairs == [(0, 1)], (
            f"期望只有 (0,1) 这一对近重复，实际 {report.dup_pairs}；"
            f"全部候选对得分 {report.pair_scores}，自匹配分 {report.self_scores}"
        )
        # 重复对的内点数应当远高于阈值，不相似对应当远低于阈值。
        assert report.pair_scores[(0, 1)] >= DEDUP_MIN_INLIERS * 2
        distinct = [v for k, v in report.pair_scores.items() if k != (0, 1)]
        assert distinct and max(distinct) < DEDUP_MIN_INLIERS

    def test_ratio_test_survivor_is_not_flagged_but_loser_is(self, monkeypatch):
        """**这条钉住的是 5058 张真实语料上推翻纯绝对阈值的那条结论。**

        两个 case 都在同一条测试里，缺一条就测不出区别：
          m=40, s=100 -> 100 >= 1.5*40=60  -> ratio test 通得过，不该剔
          m=80, s=100 -> 100 <  1.5*80=120 -> 会被挤成 ambiguous，该剔
        两者都远高于绝对阈值 25。纯绝对阈值的实现会把两个都判成重复，
        因此会在第一条断言上失败。"""
        monkeypatch.setattr(dedup, "verify_pair", _fixed(40))
        safe = dedup.scan_pairs([object()] * 2, [[1], [0]], [100, 100])
        assert safe.pair_scores[(0, 1)] == 40 >= DEDUP_MIN_INLIERS, "前提：已过绝对阈值"
        assert safe.dup_pairs == [], (
            "内点数 40 但自匹配 100，ratio test 通得过，识别器不会混淆，"
            "不能判成重复——这正是旧实现剔掉 14.3% 照片的原因"
        )

        monkeypatch.setattr(dedup, "verify_pair", _fixed(80))
        risky = dedup.scan_pairs([object()] * 2, [[1], [0]], [100, 100])
        assert risky.dup_pairs == [(0, 1)], "自匹配 100 < 1.5*80，该剔"

    def test_absolute_floor_still_applies_when_self_score_is_tiny(self, monkeypatch):
        """低纹理照片的自匹配分本来就低，只看比值会把噪声判成重复。
        m=5、s=1 时比值判据成立（1 < 7.5），但 m 低于绝对下限，不能判重复。"""
        monkeypatch.setattr(dedup, "verify_pair", _fixed(5))
        report = dedup.scan_pairs([object()] * 2, [[1], [0]], [1, 1])
        assert report.pair_scores[(0, 1)] == 5
        assert report.dup_pairs == []

    def test_uses_the_weaker_side_of_the_pair(self, monkeypatch):
        """两张里只要有**一张**会被挤成 ambiguous，这两张就不能同时入库
        （那一张永久漏检）。所以用 min(s_i, s_j)，不是 max、也不是要求两边
        都失败。s=[100, 200]、m=80：200 通得过、100 通不过 -> 必须判重复。
        用 max 的实现会漏掉这一对。"""
        monkeypatch.setattr(dedup, "verify_pair", _fixed(80))
        report = dedup.scan_pairs([object()] * 2, [[1], [0]], [100, 200])
        assert report.dup_pairs == [(0, 1)]

    def test_counts_every_verification(self, textured_image):
        imgs = [textured_image(s) for s in range(4)]
        feats = [extract(im) for im in imgs]
        report = dedup.scan_pairs(feats, _all_others(4), _self_scores(imgs))
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
        report = dedup.scan_pairs(
            [object(), object()], [[1], [0]], [100, 100], min_inliers=50
        )
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
        report = dedup.scan_pairs([object(), object()], [[1], [0]], [100, 100])
        assert report.pair_scores[(0, 1)] == 0
        assert report.dup_pairs == []

    def test_dup_pairs_order_is_deterministic_on_ties(self, monkeypatch):
        monkeypatch.setattr(dedup, "verify_pair", _fixed(100))
        report = dedup.scan_pairs([object()] * 4, _all_others(4), [100] * 4)
        assert report.dup_pairs == [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]

    def test_reports_progress(self, textured_image):
        seen = []
        imgs = [textured_image(s) for s in range(3)]
        feats = [extract(im) for im in imgs]
        dedup.scan_pairs(feats, _all_others(3), _self_scores(imgs),
                         on_progress=lambda i, n: seen.append((i, n)))
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_rejects_self_in_candidates(self, textured_image):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="自己"):
            dedup.scan_pairs(feats, [[0, 1], [0]], [100, 100])

    def test_rejects_out_of_range_candidate(self, textured_image):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="越界"):
            dedup.scan_pairs(feats, [[5], [0]], [100, 100])

    def test_rejects_length_mismatch(self, textured_image):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="candidates 长度必须一致"):
            dedup.scan_pairs(feats, [[1]], [100, 100])

    def test_rejects_self_scores_length_mismatch(self, textured_image):
        """长度不齐会让 dup_pairs 在查 self_scores[i] 时 KeyError，或者
        （更糟）错位比较——拿别人的自匹配分判自己这一对。"""
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="self_scores 长度必须一致"):
            dedup.scan_pairs(feats, _all_others(2), [100])

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_nonpositive_min_inliers(self, textured_image, bad):
        """阈值 <= 0 会把每一个被校验过的对都判成重复（未过 det 的记 0 分也
        满足 >= 0），整个语料并成一簇只留一张。必须报错，不能静默执行。"""
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="min_inliers"):
            dedup.scan_pairs(feats, _all_others(2), [100, 100], min_inliers=bad)

    @pytest.mark.parametrize("bad", [0, -1.0])
    def test_rejects_nonpositive_ratio(self, textured_image, bad):
        feats = [extract(textured_image(0)), extract(textured_image(1))]
        with pytest.raises(ValueError, match="ratio"):
            dedup.scan_pairs(feats, _all_others(2), [100, 100], ratio=bad)


class TestSelectKeep:
    def test_chain_only_drops_the_middle(self):
        """**这条钉住的是"链不等于团"。** A—B—C 里 A 与 C 并不冲突，只需
        剔掉 B。旧的"连通分量每簇留一张"实现会返回 [0]，在 5058 张真实语料
        上因此删掉 14.3% 的照片（最大连通分量 78 张、横跨 4 组不同建筑，
        分量内无边的对内点数中位仅 9）。"""
        keys = [f"p{i}.jpg" for i in range(3)]
        assert dedup.select_keep([(0, 1), (1, 2)], keys, 3) == [0, 2]

    def test_kept_photos_never_conflict_with_each_other(self):
        """这个函数唯一必须做到的事：保留下来的任意两张之间没有冲突边。
        在一个稠密的团上验证——团里只能留一张。"""
        pairs = [(0, 1), (0, 2), (1, 2)]
        keys = [f"p{i}.jpg" for i in range(4)]
        keep = dedup.select_keep(pairs, keys, 4)
        assert keep == [0, 3]
        conflict = {frozenset(p) for p in pairs}
        for a in keep:
            for b in keep:
                assert a == b or frozenset((a, b)) not in conflict

    def test_walks_in_key_order_not_index_order(self):
        """keys 的顺序刻意与下标顺序相反：keys 最小的是下标 2。冲突对
        (0,2) 里应当保留 2 而不是 0。若实现按下标顺序遍历，会保留 0。"""
        keys = ["c", "b", "a"]
        assert dedup.select_keep([(0, 2)], keys, 3) == [1, 2]

    def test_no_pairs_keeps_everything(self):
        assert dedup.select_keep([], ["a", "b", "c"], 3) == [0, 1, 2]

    def test_rejects_out_of_range_pair(self):
        with pytest.raises(ValueError, match="越界"):
            dedup.select_keep([(0, 3)], ["a", "b", "c"], 3)

    def test_rejects_self_pair(self):
        with pytest.raises(ValueError, match="自己与自己"):
            dedup.select_keep([(1, 1)], ["a", "b"], 2)

    def test_rejects_keys_length_mismatch(self):
        with pytest.raises(ValueError, match="keys 长度"):
            dedup.select_keep([], ["a"], 3)


class TestCluster:
    """cluster 现在只用于报告（把相关照片聚起来给人看），不参与剔除决定。
    它本身的连通分量语义没变，测试保留。"""

    def test_is_transitive(self):
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


def test_end_to_end_removes_exactly_the_duplicates(textured_image):
    """scan -> select_keep 串起来：一组含两个重复的照片里，保留数必须正好
    等于不同内容的张数。"""
    base = textured_image(0)
    other = textured_image(1)
    imgs = [base, _reencode(base), _reencode(base, quality=60, scale=0.9),
            other, textured_image(2)]
    feats = [extract(im) for im in imgs]
    names = [f"{i:02d}.jpg" for i in range(len(imgs))]

    report = dedup.scan_pairs(feats, _all_others(len(feats)), _self_scores(imgs))
    keep = dedup.select_keep(report.dup_pairs, names, len(feats))

    assert dedup.cluster(report.dup_pairs, len(feats)) == [[0, 1, 2], [3], [4]]
    assert keep == [0, 3, 4]
