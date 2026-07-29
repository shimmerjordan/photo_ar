"""阈值重放的测试。

最重要的一条是 TestReplayReproducesTheRealRun：默认阈值下重放出来的计数必须
与 `evaluate()` 真跑的计数**完全相等**。整套"录一次、离线扫网格"的方法论就
架在这条断言上——如果重放和真跑对不上，扫出来的表格全是好看的废数字。
"""

import numpy as np
import pytest

from photoar import features as F
from photoar import thresholds as T
from photoar import verify as V
from photoar import vocab as VOC
from photoar.descstore import DescStore, DescStoreWriter
from photoar.evaluate import evaluate, evaluate_out_of_library
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TwoStageRecognizer


def _row(kind="in", src="p0", top1_id="p0", n1=100, det=1.0, n2=0):
    return T.QueryRow(kind=kind, src=src, top1_id=top1_id, top1_inliers=n1,
                      top1_det=det, top2_inliers=n2)


class TestOutcomeBuckets:
    def test_in_library_hit_on_itself_is_correct(self):
        assert T.outcome(_row(src="p3", top1_id="p3", n1=90)) == "correct"

    def test_in_library_hit_on_another_photo_is_wrong(self):
        assert T.outcome(_row(src="p3", top1_id="p7", n1=90)) == "wrong"

    def test_in_library_rejection_is_missed(self):
        assert T.outcome(_row(src="p3", top1_id="p3", n1=10)) == "missed"

    def test_out_of_library_any_match_is_a_false_positive(self):
        """库外查询的正确答案根本不在库里，所以"认出了谁"无关紧要——报
        matched 就是误识别。这条口径与 evaluate.evaluate_out_of_library 的
        文档一致，写错方向会把最该关注的那个数字变成 0。
        """
        assert T.outcome(_row(kind="oos", src="/tmp/x.jpg", top1_id="p9", n1=90)) \
            == "false_positive"

    def test_out_of_library_rejection_is_correct_rejection(self):
        assert T.outcome(_row(kind="oos", src="/tmp/x.jpg", top1_id="p9", n1=5)) \
            == "correct_rejection"

    def test_no_candidate_at_all_is_not_a_match(self):
        """粗排一个候选都没给出（词全是 idf=0 的共享词）：as_results 返回空表，
        decide_with 判 'empty'，库内算漏检、库外算正确拒绝。
        """
        assert T.outcome(_row(top1_id=None, n1=0)) == "missed"
        assert T.outcome(_row(kind="oos", top1_id=None, n1=0)) == "correct_rejection"

    def test_runner_up_participates_in_the_ratio_test(self):
        """top2 必须参与比值检验，哪怕它自己没过 MIN_INLIERS——这是 verify 的
        既有语义（test_ratio_test_counts_candidates_below_inlier_threshold），
        录制时把 top2 也录下来的全部意义就在这里。
        """
        # 分数相对 MIN_INLIERS 取，不写死：钉的是比值检验的语义，与下限具体
        # 是多少无关。写死数字的话下限一动，两条断言会一起掉到下限以下，
        # 第一条从 correct 变 missed，看着像 outcome 坏了。
        hi, lo = V.MIN_INLIERS + 1, V.MIN_INLIERS - 1
        assert T.outcome(_row(n1=hi, n2=0)) == "correct"
        assert T.outcome(_row(n1=hi, n2=lo)) == "missed"  # hi < 1.5*lo


class TestRowOf:
    def test_picks_the_two_highest_inlier_counts(self):
        results = [
            V.PairResult("a", 30, 1.0, True),
            V.PairResult("b", 90, 1.0, True),
            V.PairResult("c", 50, 1.0, True),
        ]
        row = T.row_of("in", "b", results)
        assert (row.top1_id, row.top1_inliers, row.top2_inliers) == ("b", 90, 50)

    def test_empty_candidate_list_records_no_top1(self):
        row = T.row_of("in", "p0", [])
        assert row.top1_id is None and row.top1_inliers == 0 and row.top2_inliers == 0

    def test_ranking_matches_decide_on_random_candidate_tables(self):
        """row_of 的排序键必须与 decide_with 的一致，否则"top1"指的不是判定
        实际看的那个候选。随机撒候选表，比对 row_of 记下的 top1_id 与
        decide_with 在放宽到必然匹配的阈值下报出的 photo_id。
        """
        rng = np.random.default_rng(7)
        for _ in range(100):
            n = int(rng.integers(1, 8))
            results = [
                V.PairResult(f"p{i}", int(rng.integers(0, 120)), 1.0, False)
                for i in range(n)
            ]
            row = T.row_of("in", "p0", results)
            d = V.decide_with(results, min_inliers=0, ratio=0.0)
            assert row.top1_id == d.photo_id
            assert row.top1_inliers == d.inliers


class TestSweep:
    def test_tallies_every_row_exactly_once(self):
        rows = [_row(n1=90), _row(n1=90, top1_id="p9"), _row(n1=5),
                _row(kind="oos", n1=90), _row(kind="oos", n1=5)]
        p = T.sweep(rows)
        assert (p.correct, p.wrong, p.missed) == (1, 1, 1)
        assert (p.false_positive, p.correct_rejection) == (1, 1)
        assert p.in_total == 3 and p.oos_total == 2

    def test_raising_min_inliers_moves_hits_into_missed_not_into_wrong(self):
        """提高内点数下限只会让命中变漏检，绝不会把它变成误识别——如果扫出来
        的表里 wrong 随 min_inliers 上升而增加，那是重放写错了。
        """
        rows = [_row(n1=n) for n in (30, 40, 50, 60)]
        loose, tight = T.sweep(rows, min_inliers=25), T.sweep(rows, min_inliers=55)
        assert (loose.correct, loose.missed) == (4, 0)
        assert (tight.correct, tight.missed) == (1, 3)
        assert tight.wrong == 0

    def test_rates_use_the_right_denominators(self):
        """库内三分类的分母是库内查询数，库外两分类的分母是库外查询数，两者
        不可相加——这是 evaluate.OutOfLibraryMetrics 存在的理由，重放表也必须
        守住同一条线，否则 6.951% 这种数字会被摊薄成看起来没事。
        """
        rows = [_row(n1=90)] * 9 + [_row(n1=5)] + [_row(kind="oos", n1=90)] * 2
        p = T.sweep(rows)
        assert p.correct_rate == pytest.approx(0.9)
        assert p.missed_rate == pytest.approx(0.1)
        assert p.oos_false_positive_rate == pytest.approx(1.0)

    def test_meets_baseline_requires_the_out_of_library_rate_too(self):
        """spec §14.2：库外误识别一旦被测量就必须计入判定。全库内完美但库外
        全错的那种组合不能显示"达标"。
        """
        clean = [_row(n1=90)] * 100
        assert T.sweep(clean).meets_baseline
        assert not T.sweep(clean + [_row(kind="oos", n1=90)]).meets_baseline
        assert T.sweep(clean + [_row(kind="oos", n1=5)] * 100).meets_baseline


@pytest.fixture
def corpus(tmp_path, textured_image):
    """12 张入库 + 3 张留出的小语料，够跑端到端等价性断言。"""
    n_lib, n_out = 12, 3
    lib_imgs = {f"p{i}": textured_image(seed=i, w=900, h=650) for i in range(n_lib)}
    out_imgs = {
        f"/fake/out{i}.jpg": textured_image(seed=100 + i, w=900, h=650)
        for i in range(n_out)
    }
    feats = [F.extract(img) for img in lib_imgs.values()]

    path = tmp_path / "desc.bin"
    with DescStoreWriter(path, capacity=n_lib) as w:
        for f in feats:
            w.append(f)

    voc = VOC.train(np.vstack([f.desc for f in feats]), branching=6, depth=3, seed=0)
    builder = InvertedIndexBuilder(voc.n_words)
    for f in feats:
        builder.add(voc.words_of(f.desc))

    store = DescStore(path)
    rec = TwoStageRecognizer(voc, builder.build(), store, list(lib_imgs))
    yield rec, lib_imgs, out_imgs
    store.close()


class TestReplayReproducesTheRealRun:
    """整套方法论的地基：默认阈值下重放的计数 == 真跑的计数。

    重放法能省下的时间很可观（一次 54 分钟的跑换来任意阈值组合的答案，而不是
    每个组合重跑一次），代价是引入了一个新的失效模式：录制时的查询图如果和
    真跑不是同一批（遍历顺序、seed 派生、样本数任一处不同），重放出来的数字
    照样"看起来正常"，没有任何东西会报错。这两条断言是唯一能挡住它的东西。
    """

    def test_in_library_counts_match_evaluate(self, corpus):
        rec, lib_imgs, _ = corpus
        m = evaluate(rec, lib_imgs, samples_per_ref=3, seed=1)
        p = T.sweep(T.record(rec, lib_imgs, kind="in", samples_per_ref=3, seed=1))
        assert (p.correct, p.wrong, p.missed) == (m.correct, m.wrong, m.missed)
        assert p.in_total == m.total

    def test_out_of_library_counts_match_evaluate_out_of_library(self, corpus):
        rec, _, out_imgs = corpus
        m = evaluate_out_of_library(rec, out_imgs, samples_per_ref=3, seed=1)
        p = T.sweep(T.record(rec, out_imgs, kind="oos", samples_per_ref=3, seed=1))
        assert p.false_positive == m.false_positive
        assert p.correct_rejection == m.correct_rejection
        assert p.oos_total == m.total

    def test_recorded_rows_are_deterministic_across_runs(self, corpus):
        """同一 seed 两次录制必须给出完全相同的行，否则"扫描结果可复现"这句话
        不成立。这也顺带证明录制没有把 recognizer 的状态搞脏。
        """
        rec, lib_imgs, _ = corpus
        a = T.record(rec, lib_imgs, kind="in", samples_per_ref=2, seed=5)
        b = T.record(rec, lib_imgs, kind="in", samples_per_ref=2, seed=5)
        assert a == b

    def test_a_wrong_seed_would_have_been_caught(self, corpus):
        """反证上面那两条断言不是恒真的：换个 seed 录，行就不一样了。
        如果这条也过不了（两个 seed 录出同样的行），说明 seed 根本没生效，
        等价性断言就成了摆设。
        """
        rec, lib_imgs, _ = corpus
        a = T.record(rec, lib_imgs, kind="in", samples_per_ref=2, seed=5)
        b = T.record(rec, lib_imgs, kind="in", samples_per_ref=2, seed=6)
        assert a != b
