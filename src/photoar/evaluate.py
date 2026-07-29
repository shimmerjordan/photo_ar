"""指标计算与报告。

三分类互斥且穷尽（spec §14.2），针对**库内**查询（查询图对应的
photo_id 确实在库里）：
  正确命中  matched 且 photo_id 等于来源
  误识别    matched 但 photo_id 不等于来源
  漏检      not matched

误识别率比漏检率重要一个数量级——漏检只是让用户多举一秒手机，
播错视频是在家人面前的事故。

finding I8：以上三分类穷举的前提是"查询图对应的照片确实在库里"，此前
全项目所有测量（0a/0b/0d）都只用库内照片当查询源，"误识别 0"因此只
覆盖了"库内 A 认成库内 B"这一种混淆，没有覆盖生产环境里更常见的那种
——用户拍一张库里从来没有的东西（没入库的照片、杂志页、一张脸），
系统必须答"不认识"。这种查询天生没有"来源 photo_id"，"正确/误识别/
漏检"三分类里没有一个格子放得下它：报 matched 是误识别（库外误识别，
FALSE_POSITIVE），报 not matched 是**正确拒绝**——这不是漏检的同义词，
是相反的结果，混为一谈就会让指标失去意义（漏检率升高看起来像是坏事，
但库外正确拒绝率升高恰恰是好事）。因此这不是给 Metrics 的三分类再加
一个分支，而是另开一条独立的统计轴：见下面的 OutOfLibraryMetrics /
evaluate_out_of_library。两者的分母（total）互不相干，绝不相加。
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
class OutOfLibraryMetrics:
    """库外查询（finding I8）的测量结果——查询图对应的照片从未入库。

    与 Metrics 故意分开成两个类型，而不是往 Metrics 里加第四个分支：
    "正确/误识别/漏检"三分类的分母是"库内查询"，"误识别/正确拒绝"这
    两分类的分母是"库外查询"，两个分母统计的是完全不同的现实群体
    （spec 术语里"库内混淆"vs"面对陌生物体"），硬塞进同一个 total 只会
    让 correct_rate 之类的比率变得无法解释、也让已经写进
    phase0-results.md 的 0a/0b 数字失去可比性（要求：不能悄悄重新定义
    correct_rate 在库内场景下的含义）。这里没有"correct"、也没有
    "missed"——库外查询根本不存在"该被找到的正确答案"，只有"该不该报
    matched"这一个是非题，报 matched 就是 false_positive，报
    not matched 就是 correct_rejection，二者互斥且穷尽。
    """

    total: int
    false_positive: int
    correct_rejection: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def false_positive_rate(self) -> float:
        return self.false_positive / self.total if self.total else 0.0

    @property
    def correct_rejection_rate(self) -> float:
        return self.correct_rejection / self.total if self.total else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return float(np.percentile(np.asarray(self.latencies_ms), 95))


@dataclass(frozen=True)
class Metrics:
    total: int
    correct: int
    wrong: int
    missed: int
    latencies_ms: list[float] = field(default_factory=list)
    # 库外查询结果（finding I8），默认 None = 本次没有测量。0a/0b 等历史
    # 记录从未产生过这个字段，None 时 meets_baseline/as_report() 的行为
    # 与它们被测量时完全一致，不追溯改写已记录的结论。
    oos: "OutOfLibraryMetrics | None" = None
    # Minor #10：P95 延迟默认不计入 meets_baseline——0a 的暴力检索 P95
    # 534ms 远超 80ms 目标，但已记录的结论是"达标"（当时只看正确率/误
    # 识别率）；把延迟默认折进判定会追溯改写这条已经写进
    # phase0-results.md 的历史结论。只有调用方显式选择更严格的判定
    # （CLI 的 --strict-latency）时才把这个字段设为 True。
    latency_gate: bool = False

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
        ok = (
            self.total > 0
            and self.correct_rate >= BASELINE_CORRECT_RATE
            and self.wrong_rate <= BASELINE_WRONG_RATE
        )
        if self.latency_gate:
            ok = ok and self.p95_latency_ms <= BASELINE_P95_LATENCY_MS
        if self.oos is not None:
            # 库外误识别一旦被测量，就必须计入判定——否则"建了 holdout 却
            # 不影响退出码"会重新制造 I8 想堵上的那种盲区：CI 显示达标，
            # 但库外误识别率其实很高。这不改写 0a/0b 的历史结论（它们的
            # oos 恒为 None），只对新测量生效。
            ok = ok and self.oos.total > 0 and self.oos.false_positive_rate <= BASELINE_WRONG_RATE
        return ok

    def as_report(self) -> str:
        verdict = "达标" if self.meets_baseline else "未达标"
        scope = "正确率+误识别率"
        if self.latency_gate:
            scope += "+P95延迟"
        if self.oos is not None:
            scope += "+库外误识别"
        latency_note = (
            "达标"
            if self.p95_latency_ms <= BASELINE_P95_LATENCY_MS
            else f"超出目标 {BASELINE_P95_LATENCY_MS:.0f}ms"
        )
        lines = [
            f"样本总数    {self.total}",
            f"正确命中    {self.correct:6d}  {self.correct_rate:7.2%}  "
            f"（目标 >= {BASELINE_CORRECT_RATE:.0%}）",
            f"误识别      {self.wrong:6d}  {self.wrong_rate:7.3%}  "
            f"（目标 <= {BASELINE_WRONG_RATE:.1%}）",
            f"漏检        {self.missed:6d}  {self.missed_rate:7.2%}",
        ]
        if self.oos is not None:
            lines.append(
                f"库外误识别  {self.oos.false_positive:6d}  "
                f"{self.oos.false_positive_rate:7.3%}  "
                f"（目标 <= {BASELINE_WRONG_RATE:.1%}；库外样本 {self.oos.total}，"
                f"统计口径与上面的库内数字相互独立，不可相加）"
            )
            lines.append(
                f"库外正确拒绝{self.oos.correct_rejection:6d}  "
                f"{self.oos.correct_rejection_rate:7.2%}"
            )
        lines.append(f"P95 延迟    {self.p95_latency_ms:.1f} ms  （{latency_note}）")
        lines.append(f"结论（{scope}）  {verdict}")
        return "\n".join(lines)


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


def combine(
    metrics: list[Metrics],
    oos: "OutOfLibraryMetrics | None" = None,
    latency_gate: bool = False,
) -> Metrics:
    """把多次 evaluate() 调用的结果合并成一份聚合 Metrics。

    C1：cli._cmd_eval 现在一次只解码一张参考图、调用一次 evaluate()（而
    不是把整库解码进内存后一次性调用），再用这个函数把各次结果拼起来。

    wave 2（I8）在这里接上库外查询测量：oos 是（如果本次有 holdout 图）
    combine_out_of_library() 算出的聚合结果，直接挂到最终 Metrics 上，
    不需要改 evaluate() 本身的形状。latency_gate 透传给 Metrics，只影响
    最终这一份聚合结果的 meets_baseline/as_report()（中间的每份分片
    Metrics 从不单独判定，这个字段是否为 True 对它们无意义）。
    """
    total = sum(m.total for m in metrics)
    correct = sum(m.correct for m in metrics)
    wrong = sum(m.wrong for m in metrics)
    missed = sum(m.missed for m in metrics)
    latencies: list[float] = []
    for m in metrics:
        latencies.extend(m.latencies_ms)
    return Metrics(
        total=total, correct=correct, wrong=wrong, missed=missed, latencies_ms=latencies,
        oos=oos, latency_gate=latency_gate,
    )


def evaluate_out_of_library(
    recognizer: Recognizer,
    queries: dict[str, np.ndarray],
    samples_per_ref: int,
    seed: int,
) -> OutOfLibraryMetrics:
    """用**从未入库**的照片当查询源，测量 finding I8 说的那种生产环境里
    最常见的假阳性：用户拍了一张库里根本没有的东西。

    queries 的 key 只是给 _ref_seed 派生确定性 seed 用的稳定字符串（不是
    photo_id——这些图片压根不在库里，没有 photo_id 这回事），调用方一般传
    留出图片自己的绝对路径。

    分类规则是本函数存在的唯一理由，必须严格遵守（finding I8 的核心
    要求）：
      recognize() 返回 matched=True  -> false_positive（不管它报的
                                          photo_id 是库里哪一张，只要
                                          "认出了"就是误识别，因为正确
                                          答案根本不在库里）
      recognize() 返回 matched=False -> correct_rejection（不是漏检！
                                          "库外的东西被正确地拒绝"是这
                                          条测量存在的意义所在，是好结
                                          果，不能算进任何形式的"错"里）
    """
    false_positive = correct_rejection = 0
    latencies: list[float] = []

    for qid, img in sorted(queries.items()):
        for query_img, _ in synth.generate(img, samples_per_ref, _ref_seed(seed, qid)):
            t0 = time.perf_counter()
            d = recognizer.recognize(query_img)
            latencies.append((time.perf_counter() - t0) * 1000.0)

            if d.matched:
                false_positive += 1
            else:
                correct_rejection += 1

    return OutOfLibraryMetrics(
        total=false_positive + correct_rejection,
        false_positive=false_positive,
        correct_rejection=correct_rejection,
        latencies_ms=latencies,
    )


def combine_out_of_library(metrics: list[OutOfLibraryMetrics]) -> OutOfLibraryMetrics:
    """把多次 evaluate_out_of_library() 调用的结果合并成一份聚合结果。

    与 combine() 分开一份是故意的：C1 的教训是"整库一次性解码进内存"会
    OOM，留出图片同样可能有成千上万张，cli._cmd_eval 需要能一次只解码
    一张留出图调一次 evaluate_out_of_library() 再合并，而不是把它们也
    一次性摊在内存里重新引入同一类问题。
    """
    total = sum(m.total for m in metrics)
    false_positive = sum(m.false_positive for m in metrics)
    correct_rejection = sum(m.correct_rejection for m in metrics)
    latencies: list[float] = []
    for m in metrics:
        latencies.extend(m.latencies_ms)
    return OutOfLibraryMetrics(
        total=total, false_positive=false_positive, correct_rejection=correct_rejection,
        latencies_ms=latencies,
    )
