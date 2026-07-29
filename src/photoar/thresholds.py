"""阈值重放：用另一组 (MIN_INLIERS, RATIO) 重算一次真跑的结果。

为什么需要这个模块。0d 上规模跑出**库外误识别 6.951%**（目标 ≤0.1%），
拆开之后 0.349% 是真实误识别，仍超目标 3.5 倍。要回答"把 MIN_INLIERS 或
RATIO 调到多少能关掉这个缺口、代价是多少漏检"，有三条路：

  1. 各调一个值重跑一次 eval —— 一次 54 分钟，一个 5×5 的网格是 22 小时。
  2. 拿 `bench/classify_fp.py` 那些**原图**互查内点数去估 —— 不行。原图
     内点数不是查询时的内点数：实测同一对能从原图 21 涨到查询时 33（查询
     图是合成扰动过的，特征点集不一样）。用原图数字定阈值会定错。
  3. 把一次真跑里每个查询的候选分数录下来，离线重放。

本模块是第 3 条。重放是**精确**的而不是近似的，理由见 `verify.decide_with`
的文档：`verify_pair` 算出的 inliers/det 与阈值无关，候选排序也只按
inliers，所以录下 top1/top2 就足以还原任意 (min_inliers, ratio) 下的判定。

判定一律走 `verify.decide_with`，不在这里另写一遍 if/else —— 扫出来的阈值
是要直接写回产品的，重放口径与产品口径必须是同一份代码。
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from . import synth
from .evaluate import BASELINE_CORRECT_RATE, BASELINE_WRONG_RATE, _ref_seed
from .verify import DET_MAX, DET_MIN, MIN_INLIERS, RATIO, PairResult, decide_with

# 库内查询的三种结局 + 库外查询的两种结局。名字与 evaluate.Metrics /
# OutOfLibraryMetrics 的字段一致，方便把重放结果和真跑报告对着看。
IN_LIBRARY_OUTCOMES = ("correct", "wrong", "missed")
OUT_OF_LIBRARY_OUTCOMES = ("false_positive", "correct_rejection")


@dataclass(frozen=True)
class QueryRow:
    """一次查询录下来的东西：候选表的前两名，加上"正确答案是什么"。

    只录前两名，不录全部 Top-20：`decide_with` 只看 ranked[0]（三条判定的
    前两条）和 ranked[1].inliers（比值检验的分母），再往后的候选对任何
    (min_inliers, ratio) 组合都不产生影响。录 20 个候选会让文件大 10 倍，
    换不到任何多出来的结论。

    kind='in'  时 src 是参考图自己的 photo_id，正确答案就是它。
    kind='oos' 时 src 是留出图的路径：它**从未入库**，所以库里没有任何
               photo_id 是正确答案，报 matched 就是误识别（见
               evaluate.evaluate_out_of_library 的分类规则）。
    """

    kind: str
    src: str
    top1_id: str | None
    top1_inliers: int
    top1_det: float
    top2_inliers: int

    def as_results(self) -> list[PairResult]:
        """还原成 decide_with 吃的候选列表。

        top1_id 为 None 表示粗排一个候选都没给出（词全是 idf=0 的共享词），
        此时候选列表是空的，decide_with 会判 'empty'——不是"内点数 0 的候选"，
        两者在计数上同样是不匹配，但语义不同，不该混。

        top2_inliers=0 同时表示"只有一个候选"和"次优拿了 0 分"。这不会造成
        差别：decide 对"只有一个候选"用的分母正是 0（`ranked[1].inliers if
        len(ranked) > 1 else 0`），两种情况下比值检验的结果完全一样。
        """
        if self.top1_id is None:
            return []
        results = [
            PairResult(
                photo_id=self.top1_id,
                inliers=self.top1_inliers,
                det=self.top1_det,
                ok=False,  # decide_with 会自己按传入阈值重算，这个值不被读
            )
        ]
        if self.top2_inliers:
            # 次优的 det 没录：比值检验只用它的 inliers，det 不参与。给 0.0
            # 而不是编一个"看起来合理"的值，免得日后有人以为它有意义。
            results.append(
                PairResult(photo_id="<runner-up>", inliers=self.top2_inliers, det=0.0, ok=False)
            )
        return results


class CandidateSource(Protocol):
    def verify_candidates(self, img_bgr: np.ndarray) -> list[PairResult]: ...


def row_of(kind: str, src: str, results: list[PairResult]) -> QueryRow:
    """把一次查询的候选表压成一行。

    排序键与 `decide_with` 的必须是同一个（`-inliers`，稳定排序）：如果这里
    按别的方式排，"top1/top2"指的就不是判定实际看的那两个候选。
    """
    ranked = sorted(results, key=lambda r: -r.inliers)
    if not ranked:
        return QueryRow(kind=kind, src=src, top1_id=None, top1_inliers=0,
                        top1_det=0.0, top2_inliers=0)
    return QueryRow(
        kind=kind,
        src=src,
        top1_id=ranked[0].photo_id,
        top1_inliers=ranked[0].inliers,
        top1_det=ranked[0].det,
        top2_inliers=ranked[1].inliers if len(ranked) > 1 else 0,
    )


def record(
    recognizer: CandidateSource,
    sources: dict[str, np.ndarray],
    *,
    kind: str,
    samples_per_ref: int,
    seed: int,
) -> list[QueryRow]:
    """对一批查询源逐样本录制候选分数。

    这个循环放在 src 而不是 bench 脚本里，是因为它必须与
    `evaluate.evaluate` / `evaluate_out_of_library` 的查询生成**逐字节一致**
    ——同样的 `sorted()` 遍历顺序、同样的 `synth.generate` 调用、同样的
    `_ref_seed` 派生。差一点，录下来的行对应的就不是那次真跑，而重放出来的
    数字看起来照样正常。tests/test_thresholds.py 里有一条端到端断言把这件事
    钉住：默认阈值下重放的计数必须与 `evaluate()` 的计数完全相等。
    """
    rows: list[QueryRow] = []
    for src_id, img in sorted(sources.items()):
        for query_img, _ in synth.generate(img, samples_per_ref, _ref_seed(seed, src_id)):
            rows.append(row_of(kind, src_id, recognizer.verify_candidates(query_img)))
    return rows


def outcome(
    row: QueryRow,
    *,
    min_inliers: int = MIN_INLIERS,
    ratio: float = RATIO,
    det_min: float = DET_MIN,
    det_max: float = DET_MAX,
) -> str:
    """这一行在给定阈值下的结局。"""
    d = decide_with(
        row.as_results(),
        min_inliers=min_inliers,
        ratio=ratio,
        det_min=det_min,
        det_max=det_max,
    )
    if row.kind == "oos":
        # 库外：认出任何一张都是误识别，拒绝就是正确拒绝。这里**不**看
        # d.photo_id 是谁——正确答案不在库里，"认对了"这件事不存在。
        return "false_positive" if d.matched else "correct_rejection"
    if not d.matched:
        return "missed"
    return "correct" if d.photo_id == row.src else "wrong"


@dataclass(frozen=True)
class SweepPoint:
    """网格上一个 (min_inliers, ratio) 点的重放结果。"""

    min_inliers: int
    ratio: float
    correct: int = 0
    wrong: int = 0
    missed: int = 0
    false_positive: int = 0
    correct_rejection: int = 0

    @property
    def in_total(self) -> int:
        return self.correct + self.wrong + self.missed

    @property
    def oos_total(self) -> int:
        return self.false_positive + self.correct_rejection

    @property
    def correct_rate(self) -> float:
        return self.correct / self.in_total if self.in_total else 0.0

    @property
    def wrong_rate(self) -> float:
        return self.wrong / self.in_total if self.in_total else 0.0

    @property
    def missed_rate(self) -> float:
        return self.missed / self.in_total if self.in_total else 0.0

    @property
    def oos_false_positive_rate(self) -> float:
        return self.false_positive / self.oos_total if self.oos_total else 0.0

    @property
    def meets_baseline(self) -> bool:
        """spec §14.2 的四条（不含 P95——重放不产生延迟数字）。

        故意与 evaluate.Metrics.meets_baseline 用同一组常量：重放表里"达标"
        这两个字必须和真跑报告里那两个字是同一个意思，否则扫描结论没法直接
        拿来做决定。库外那条只在真的测了库外查询时才参与判定，与
        Metrics.meets_baseline 对 oos=None 的处理一致。
        """
        ok = (
            self.in_total > 0
            and self.correct_rate >= BASELINE_CORRECT_RATE
            and self.wrong_rate <= BASELINE_WRONG_RATE
        )
        if self.oos_total:
            ok = ok and self.oos_false_positive_rate <= BASELINE_WRONG_RATE
        return ok


def sweep(
    rows: list[QueryRow],
    *,
    min_inliers: int = MIN_INLIERS,
    ratio: float = RATIO,
    det_min: float = DET_MIN,
    det_max: float = DET_MAX,
) -> SweepPoint:
    """把 rows 在一个阈值组合下重放，返回计数。"""
    tally = {k: 0 for k in IN_LIBRARY_OUTCOMES + OUT_OF_LIBRARY_OUTCOMES}
    for row in rows:
        tally[
            outcome(row, min_inliers=min_inliers, ratio=ratio, det_min=det_min, det_max=det_max)
        ] += 1
    return SweepPoint(min_inliers=min_inliers, ratio=ratio, **tally)
