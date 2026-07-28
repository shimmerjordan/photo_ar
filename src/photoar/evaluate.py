"""指标计算与报告。

三分类互斥且穷尽（spec §14.2）：
  正确命中  matched 且 photo_id 等于来源
  误识别    matched 但 photo_id 不等于来源
  漏检      not matched

误识别率比漏检率重要一个数量级——漏检只是让用户多举一秒手机，
播错视频是在家人面前的事故。
"""

import time
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from . import synth
from .verify import Decision

BASELINE_CORRECT_RATE = 0.95
BASELINE_WRONG_RATE = 0.001
BASELINE_P95_LATENCY_MS = 80.0


class Recognizer(Protocol):
    def recognize(self, img_bgr: np.ndarray) -> Decision: ...


@dataclass(frozen=True)
class Metrics:
    total: int
    correct: int
    wrong: int
    missed: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def correct_rate(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def wrong_rate(self) -> float:
        return self.wrong / self.total if self.total else 0.0

    @property
    def missed_rate(self) -> float:
        return self.missed / self.total if self.total else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return float(np.percentile(np.asarray(self.latencies_ms), 95))

    @property
    def meets_baseline(self) -> bool:
        return (
            self.total > 0
            and self.correct_rate >= BASELINE_CORRECT_RATE
            and self.wrong_rate <= BASELINE_WRONG_RATE
        )

    def as_report(self) -> str:
        verdict = "达标" if self.meets_baseline else "未达标"
        latency_note = (
            "达标"
            if self.p95_latency_ms <= BASELINE_P95_LATENCY_MS
            else f"超出目标 {BASELINE_P95_LATENCY_MS:.0f}ms"
        )
        return "\n".join(
            [
                f"样本总数    {self.total}",
                f"正确命中    {self.correct:6d}  {self.correct_rate:7.2%}  "
                f"（目标 >= {BASELINE_CORRECT_RATE:.0%}）",
                f"误识别      {self.wrong:6d}  {self.wrong_rate:7.3%}  "
                f"（目标 <= {BASELINE_WRONG_RATE:.1%}）",
                f"漏检        {self.missed:6d}  {self.missed_rate:7.2%}",
                f"P95 延迟    {self.p95_latency_ms:.1f} ms  （{latency_note}）",
                f"结论        {verdict}",
            ]
        )


def evaluate(
    recognizer: Recognizer,
    refs: dict[str, np.ndarray],
    samples_per_ref: int,
    seed: int,
) -> Metrics:
    correct = wrong = missed = 0
    latencies: list[float] = []

    for offset, (photo_id, img) in enumerate(sorted(refs.items())):
        for query_img, _ in synth.generate(img, samples_per_ref, seed + offset):
            t0 = time.perf_counter()
            d = recognizer.recognize(query_img)
            latencies.append((time.perf_counter() - t0) * 1000.0)

            if not d.matched:
                missed += 1
            elif d.photo_id == photo_id:
                correct += 1
            else:
                wrong += 1

    return Metrics(
        total=correct + wrong + missed,
        correct=correct,
        wrong=wrong,
        missed=missed,
        latencies_ms=latencies,
    )
