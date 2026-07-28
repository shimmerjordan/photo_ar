import numpy as np

from photoar import evaluate as E
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


def test_zero_samples_does_not_divide_by_zero():
    m = E.evaluate(_FakeRecognizer([_miss()]), {}, samples_per_ref=0, seed=1)
    assert m.total == 0
    assert m.correct_rate == 0.0
    assert not m.meets_baseline
