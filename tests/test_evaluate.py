import numpy as np

from photoar import evaluate as E
from photoar import synth
from photoar.verify import Decision


class _FakeRecognizer:
    """按预设脚本返回结果，让指标计算的测试不依赖真实 CV。"""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def recognize(self, img_bgr):
        d = self._script[self._i % len(self._script)]
        self._i += 1
        return d


def _hit(pid):
    return Decision(matched=True, photo_id=pid, inliers=50, reason="ok")


def _miss():
    return Decision(matched=False, photo_id=None, inliers=0, reason="weak")


def test_classifies_correct_wrong_missed(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0"), _hit("pX"), _miss(), _hit("p0")])
    m = E.evaluate(rec, refs, samples_per_ref=4, seed=1)
    assert m.total == 4
    assert (m.correct, m.wrong, m.missed) == (2, 1, 1)


def test_rates_sum_to_one(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0"), _hit("pX"), _miss()])
    m = E.evaluate(rec, refs, samples_per_ref=3, seed=1)
    assert abs(m.correct_rate + m.wrong_rate + m.missed_rate - 1.0) < 1e-9


def test_p95_latency_is_reported(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0")])
    m = E.evaluate(rec, refs, samples_per_ref=5, seed=1)
    assert len(m.latencies_ms) == 5
    assert m.p95_latency_ms >= 0.0


def test_report_contains_all_four_headline_numbers(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0"), _hit("pX"), _miss()])
    text = E.evaluate(rec, refs, samples_per_ref=3, seed=1).as_report()
    for token in ("正确命中", "误识别", "漏检", "P95"):
        assert token in text


def test_report_flags_pass_or_fail_against_baseline(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    good = E.evaluate(_FakeRecognizer([_hit("p0")]), refs, samples_per_ref=20, seed=1)
    assert good.meets_baseline

    bad = E.evaluate(_FakeRecognizer([_hit("pX")]), refs, samples_per_ref=20, seed=1)
    assert not bad.meets_baseline


def test_zero_samples_does_not_divide_by_zero(textured_image):
    """M13：原测试传 {} 当 refs，实际测的是"零个参考图"，samples_per_ref=0
    这条路径从未被真正跑过（循环体对空字典直接不执行，跟 samples_per_ref
    是 0 还是 20 无关）。这里给一个真实的非空 refs，让 samples_per_ref=0
    真正被使用（synth.generate(img, 0, ...) 生成 0 个查询），才实际验证了
    "0 个样本不会除零"。
    """
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    m = E.evaluate(_FakeRecognizer([_miss()]), refs, samples_per_ref=0, seed=1)
    assert m.total == 0
    assert m.correct_rate == 0.0
    assert not m.meets_baseline


# ---------------------------------------------------------------------------
# M14 + C1（最终审阅追加）：旧实现用 `seed + offset`（offset 是
# enumerate(sorted(refs.items())) 里的位置）给每张参考图派生合成查询的
# seed。这有两个后果：
#   1. 不同 --seed 的两次运行会共享大部分查询组：seed=1 下第 i+1 张参考图
#      与 seed=2 下第 i 张参考图用的是同一个 seed+offset，抽到完全相同的
#      合成参数（M14）。
#   2. 一张参考图实际用到的查询组依赖于它在 refs 字典里排第几、以及批次里
#      还有哪些别的图——这堵死了 C1 想要的"一次只解码一张参考图，流式喂
#      给 evaluate()"：如果流式（一次一张）跑出来的结果和一次性全喂进去
#      不一样，streaming 就不是纯粹的内存优化，而是改变了测量口径。
# 修复：每张参考图的 seed 只由 (seed, photo_id) 决定，与批次位置、批次里
# 其它成员都无关。
# ---------------------------------------------------------------------------


def test_per_ref_query_seed_is_independent_of_batch_position(monkeypatch, textured_image):
    """C1 的前提：单独评估一张参考图，与它跟别的参考图一起被评估，用到的
    合成查询组必须完全一样——这样 cli._cmd_eval 才能安全地把整库拆成
    "一次只解码一张"分别调用 evaluate()，而不改变测量结果。"""
    calls: list[int] = []
    real_generate = synth.generate

    def spy(img, count, seed):
        calls.append(seed)
        return real_generate(img, count, seed)

    monkeypatch.setattr(E.synth, "generate", spy)

    img_a = textured_image(seed=10, w=300, h=200)
    img_b = textured_image(seed=11, w=300, h=200)
    rec = _FakeRecognizer([_miss()])

    E.evaluate(rec, {"pB": img_b}, samples_per_ref=1, seed=7)
    assert len(calls) == 1
    seed_alone = calls[0]

    calls.clear()
    # sorted(refs) 顺序是 pA, pB —— pB 在批次里排第二（旧实现 offset=1）
    E.evaluate(rec, {"pA": img_a, "pB": img_b}, samples_per_ref=1, seed=7)
    assert len(calls) == 2
    seed_within_batch = calls[1]  # 对应 pB 的那次调用

    assert seed_alone == seed_within_batch


def test_different_top_level_seeds_do_not_share_query_sets_across_refs():
    """M14 具体场景：旧公式下 seed=1 的第 1 张（offset=1）与 seed=2 的第 0 张
    （offset=0）用的是同一个 seed+offset=2，会抽到完全相同的合成参数。
    修复后不同 (seed, photo_id) 组合派生出的 seed 必须不同。"""
    seed_p1_at_seed1 = E._ref_seed(1, "p1")
    seed_p0_at_seed2 = E._ref_seed(2, "p0")
    assert seed_p1_at_seed1 != seed_p0_at_seed2


# ---------------------------------------------------------------------------
# finding I8（最终整体审阅追加）：evaluate() 的三分类（正确/误识别/漏检）
# 只穷举了"查询图对应的照片确实在库里"这一种情况。库外查询（用户拍了一张
# 库里根本没有的东西）需要一套不同的、同样互斥穷尽的分类——matched 是
# false_positive，not matched 是 correct_rejection，不是 missed。下面的
# 测试直接锁死这条分类规则：如果谁在 evaluate_out_of_library 里手滑把
# matched 算成别的东西，或者把 not matched 并进 missed/漏检，这里必须
# 失败。
# ---------------------------------------------------------------------------


def test_evaluate_out_of_library_counts_every_match_as_false_positive(textured_image):
    """核心断言：库外查询只要被 recognize() 报 matched，不管报的 photo_id
    是库里哪一张，都必须被计入 false_positive，一个都不能漏记、也不能被
    错记成别的类别（比如 correct——库外查询根本不存在"正确答案"）。"""
    img = textured_image(seed=20, w=400, h=300)
    # 全部脚本成脚本都报"命中了库里的 pX"——但这张图根本没入库，
    # 库外查询语境下这必须无条件是 false_positive。
    rec = _FakeRecognizer([_hit("some-library-photo")])
    m = E.evaluate_out_of_library(rec, {"holdout-1": img}, samples_per_ref=6, seed=1)
    assert m.total == 6
    assert m.false_positive == 6
    assert m.correct_rejection == 0
    assert m.false_positive_rate == 1.0


def test_evaluate_out_of_library_counts_every_rejection_as_correct_rejection_not_missed(
    textured_image,
):
    """反向断言：recognize() 报 not matched 时必须计入 correct_rejection，
    绝不能算成任何形式的"错"（漏检、误识别都不行）——这是一个好结果，是
    这条测量存在的意义所在。OutOfLibraryMetrics 结构上就没有 missed 这个
    字段，这里再从数值上钉一遍，防止未来有人往这个类型上加字段时把这条
    语义悄悄改回"漏检"。"""
    img = textured_image(seed=21, w=400, h=300)
    rec = _FakeRecognizer([_miss()])
    m = E.evaluate_out_of_library(rec, {"holdout-2": img}, samples_per_ref=6, seed=1)
    assert m.total == 6
    assert m.correct_rejection == 6
    assert m.false_positive == 0
    assert m.correct_rejection_rate == 1.0
    assert not hasattr(m, "missed")


def test_evaluate_out_of_library_mixed_script_splits_correctly(textured_image):
    img = textured_image(seed=22, w=400, h=300)
    rec = _FakeRecognizer([_hit("pX"), _miss(), _miss()])
    m = E.evaluate_out_of_library(rec, {"holdout-3": img}, samples_per_ref=9, seed=1)
    assert m.total == 9
    assert m.false_positive == 3  # 每 3 次里第 1 次命中脚本
    assert m.correct_rejection == 6


def test_combine_out_of_library_sums_across_holdout_images():
    a = E.OutOfLibraryMetrics(total=4, false_positive=1, correct_rejection=3)
    b = E.OutOfLibraryMetrics(total=6, false_positive=0, correct_rejection=6)
    c = E.combine_out_of_library([a, b])
    assert (c.total, c.false_positive, c.correct_rejection) == (10, 1, 9)


# ---------------------------------------------------------------------------
# 本轮修复追加：select_holdout 按内容哈希整组去留只堵住了字节完全相同的
# 重复跨边界，堵不住"重新编码的近似重复"（哈希本身就不相等，没有哈希能
# 查出来）。作为可追溯性的补救，evaluate_out_of_library 记录每次
# false_positive 到底命中了库里哪个 photo_id，供验收跑之后人工/脚本核对
# 是否其实是同一张照片的另一份编码——不改变 false_positive 的计数口径。
# ---------------------------------------------------------------------------


def test_evaluate_out_of_library_records_matched_photo_id_for_attribution(textured_image):
    img = textured_image(seed=23, w=400, h=300)
    rec = _FakeRecognizer([_hit("pX"), _miss()])
    m = E.evaluate_out_of_library(rec, {"holdout-4": img}, samples_per_ref=4, seed=1)
    assert m.false_positive == 2
    assert m.false_positive_matches == [("holdout-4", "pX"), ("holdout-4", "pX")]


def test_evaluate_out_of_library_records_no_matches_when_all_rejected(textured_image):
    img = textured_image(seed=24, w=400, h=300)
    rec = _FakeRecognizer([_miss()])
    m = E.evaluate_out_of_library(rec, {"holdout-5": img}, samples_per_ref=3, seed=1)
    assert m.false_positive == 0
    assert m.false_positive_matches == []


def test_combine_out_of_library_merges_false_positive_matches():
    a = E.OutOfLibraryMetrics(
        total=2, false_positive=1, correct_rejection=1,
        false_positive_matches=[("qa", "pa")],
    )
    b = E.OutOfLibraryMetrics(
        total=2, false_positive=1, correct_rejection=1,
        false_positive_matches=[("qb", "pb")],
    )
    c = E.combine_out_of_library([a, b])
    assert c.false_positive_matches == [("qa", "pa"), ("qb", "pb")]


def test_metrics_oos_is_none_by_default_and_does_not_change_meets_baseline(textured_image):
    """0a/0b 的历史 Metrics 从未附带 oos——默认 None 时 meets_baseline 的
    行为必须和这个字段被加进来之前完全一样，不能追溯改写已经记录的结论。"""
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    m = E.evaluate(_FakeRecognizer([_hit("p0")]), refs, samples_per_ref=20, seed=1)
    assert m.oos is None
    assert m.meets_baseline


def test_metrics_oos_false_positive_flips_meets_baseline_to_false(textured_image):
    """库外误识别一旦被测量、且超出与库内相同的 0.1% 目标，就必须让整体
    判定翻成不达标——否则"建了 holdout 却不影响退出码"会重新制造 I8 想
    堵上的盲区。"""
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    good = E.evaluate(_FakeRecognizer([_hit("p0")]), refs, samples_per_ref=20, seed=1)
    bad_oos = E.OutOfLibraryMetrics(total=10, false_positive=2, correct_rejection=8)
    combined = E.combine([good], oos=bad_oos)
    assert combined.meets_baseline is False

    clean_oos = E.OutOfLibraryMetrics(total=10, false_positive=0, correct_rejection=10)
    combined_clean = E.combine([good], oos=clean_oos)
    assert combined_clean.meets_baseline is True


def test_as_report_labels_out_of_library_false_positive_rate_next_to_in_library_figures(
    textured_image,
):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    good = E.evaluate(_FakeRecognizer([_hit("p0")]), refs, samples_per_ref=5, seed=1)
    oos = E.OutOfLibraryMetrics(total=10, false_positive=1, correct_rejection=9)
    text = E.combine([good], oos=oos).as_report()
    assert "库外误识别" in text
    assert "库外正确拒绝" in text
    # false_positive_rate = 1/10 = 10.000%，确实是数字本身出现在报告里，
    # 不是只出现标签文字。
    assert "10.000%" in text


# ---------------------------------------------------------------------------
# Minor #10（最终整体审阅追加）：meets_baseline 原来完全忽略 p95_latency_ms，
# as_report() 因此能打出"P95 延迟 900ms（超出目标 80ms）"紧挨着"结论 达标"
# 这种自相矛盾的 CI 契约。修复：延迟默认仍不计入判定（0a 的历史结论
# 不能被追溯改写——0a 暴力检索 P95=534ms > 80ms 目标，但已记录为达标），
# 只有显式选择 latency_gate=True（CLI 的 --strict-latency）才折进去。
# ---------------------------------------------------------------------------


def test_latency_gate_defaults_to_false_and_ignores_slow_p95(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    m = E.evaluate(_FakeRecognizer([_hit("p0")]), refs, samples_per_ref=20, seed=1)
    combined = E.combine([m])  # latency_gate 默认 False
    assert combined.latency_gate is False
    # 手工构造一个正确率/误识别率都达标、但延迟明显超标的分片，模拟 0a
    slow = E.Metrics(total=1, correct=1, wrong=0, missed=0, latencies_ms=[900.0])
    combined_slow = E.combine([slow])
    assert combined_slow.p95_latency_ms > E.BASELINE_P95_LATENCY_MS
    assert combined_slow.meets_baseline is True  # 默认不看延迟
    assert "超出目标" in combined_slow.as_report()
    assert "达标" in combined_slow.as_report()  # 结论仍然是达标——不自相矛盾


def test_latency_gate_true_flips_meets_baseline_when_p95_exceeds_target():
    slow = E.Metrics(total=1, correct=1, wrong=0, missed=0, latencies_ms=[900.0])
    combined = E.combine([slow], latency_gate=True)
    assert combined.latency_gate is True
    assert combined.p95_latency_ms > E.BASELINE_P95_LATENCY_MS
    assert combined.meets_baseline is False
    assert "未达标" in combined.as_report()


def test_latency_gate_true_still_passes_when_p95_is_within_target():
    fast = E.Metrics(total=1, correct=1, wrong=0, missed=0, latencies_ms=[10.0])
    combined = E.combine([fast], latency_gate=True)
    assert combined.meets_baseline is True
