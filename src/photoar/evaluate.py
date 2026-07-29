"""指标计算与报告。

三分类互斥且穷尽（spec §14.2）：
  正确命中  matched 且 photo_id 等于来源
  误识别    matched 但 photo_id 不等于来源
  漏检      not matched

误识别率比漏检率重要一个数量级——漏检只是让用户多举一秒手机，
播错视频是在家人面前的事故。
"""

import hashlib
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


def _ref_seed(seed: int, photo_id: str) -> int:
    """把 (seed, photo_id) 派生成这张参考图专用的合成查询 seed。

    旧实现用 `seed + offset`（offset 是 sorted(refs.items()) 里的枚举位置），
    有两个问题（M14）：
      1. 不同 --seed 的两次运行会共享大部分查询组——seed=1 下第 i+1 张与
         seed=2 下第 i 张的 seed+offset 恰好相等，抽到完全相同的合成参数。
      2. 一张参考图用到的查询组依赖于它在批次里排第几、批次里还有哪些别的
         图——这堵死了 C1 需要的"一次只解码一张参考图，流式喂给 evaluate()"：
         如果流式跑出来的结果和一次性全喂进去不一样，streaming 就不只是内存
         优化，还悄悄改变了测量口径。

    改成只由 (seed, photo_id) 决定，与批次位置、批次里其它成员都无关：两次
    运行只要 seed 和 photo_id 相同，查询组就完全一样；cli._cmd_eval 因此
    可以安全地把整库拆成一次只解码一张参考图分别调用 evaluate()（见 C1）。
    用 sha256 而不是内置 hash()：内置 hash() 对字符串按进程加盐，不满足
    "同一 seed 下确定性"这条项目规则。
    """
    digest = hashlib.sha256(f"{seed}:{photo_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (2**32)


def evaluate(
    recognizer: Recognizer,
    refs: dict[str, np.ndarray],
    samples_per_ref: int,
    seed: int,
) -> Metrics:
    correct = wrong = missed = 0
    latencies: list[float] = []

    for photo_id, img in sorted(refs.items()):
        for query_img, _ in synth.generate(img, samples_per_ref, _ref_seed(seed, photo_id)):
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


def combine(metrics: list[Metrics]) -> Metrics:
    """把多次 evaluate() 调用的结果合并成一份聚合 Metrics。

    C1：cli._cmd_eval 现在一次只解码一张参考图、调用一次 evaluate()（而
    不是把整库解码进内存后一次性调用），再用这个函数把各次结果拼起来——
    这是特意留给 wave 2（I8，库外查询测量）的接缝：以后可以再对库外查询图
    调 evaluate()，把结果一并 combine() 进同一份聚合指标，不需要改
    evaluate() 本身的形状。
    """
    total = sum(m.total for m in metrics)
    correct = sum(m.correct for m in metrics)
    wrong = sum(m.wrong for m in metrics)
    missed = sum(m.missed for m in metrics)
    latencies: list[float] = []
    for m in metrics:
        latencies.extend(m.latencies_ms)
    return Metrics(
        total=total, correct=correct, wrong=wrong, missed=missed, latencies_ms=latencies
    )
