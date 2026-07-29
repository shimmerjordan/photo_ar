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
