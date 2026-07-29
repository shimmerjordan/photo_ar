"""量出**查询时**的内点数 / ratio 分布，再离线扫 (MIN_INLIERS, RATIO) 网格。

为什么必须单独做这件事。0d 上规模跑出库外误识别 6.951%，拆开之后 0.349% 是
真实误识别，超目标 3.5 倍。下一个动作只能是"动阈值"或"证明不该动阈值"，而
定阈值需要的是**查询时**的内点数分布：

  - `bench/classify_fp.py` 量的是**原图**互查内点数。那不是查询时的数字：查询图
    经过合成扰动，特征点集不一样，实测同一对能从原图 21 涨到查询时 33。拿原图
    数字定阈值会定错方向。
  - 一个一个阈值重跑 eval 也不行：一次 54 分钟，5×5 网格 22 小时。

所以分两步：`record` 把一次真跑里每个查询的候选分数录下来，`analyze` 在录好的
行上离线重放任意阈值组合。重放是**精确**的而不是近似的（理由见
`photoar.verify.decide_with`），并且 tests/test_thresholds.py 里有一条端到端
断言钉住"默认阈值下重放的计数 == evaluate() 真跑的计数"。

判定与计数逻辑全在 `photoar.thresholds` / `photoar.verify`（有测试），本脚本
只做 IO、编排、进度和报表。

用法：

    # 1) 录制。参数要与那次上规模 eval 完全一致，录出来的行才对应同一批查询
    python bench/threshold_scan.py record --corpus ~/photoar-data/corpus \\
        --samples 20 --limit 1000 --seed 1 --out ~/photoar-data/rows.jsonl

    # 2) 分析。--fp-json 吃 classify_fp.py 的报告，用来把库外假阳性拆成
    #    "同一被摄物体的不同照片 / 漏掉的近重复 / 真实误识别"三份
    python bench/threshold_scan.py analyze --rows ~/photoar-data/rows.jsonl \\
        --fp-json ~/photoar-data/fp.json

录制与 eval 同样贵（约 108 ms/查询）；分析是纯 CPU 算术，秒级。
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from photoar import thresholds as T
from photoar.cli import _progress, _progress_every, _strided
from photoar.corpus import load_corpus, load_holdout
from photoar.evaluate import BASELINE_CORRECT_RATE
from photoar.verify import MIN_INLIERS, RATIO

# 扫描网格。MIN_INLIERS 往下也扫两档，不只往上：如果放松阈值几乎不增加误识别，
# 那说明当前 25 这个值卡住的其实是召回率，结论会与"该往上抬"完全相反——只往上
# 扫就永远看不到这半边。
MIN_INLIERS_GRID = (15, 20, 25, 30, 35, 40, 50, 60)
RATIO_GRID = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0)

# JSONL 用短字段名：29740 行 × 6 个字段，长名字会让文件大一倍多，而这个文件
# 只有本脚本读。字段含义见 photoar.thresholds.QueryRow。
_FIELDS = ("kind", "src", "t1", "n1", "d1", "n2")


def log(msg: str) -> None:
    print(msg, flush=True)


def to_json(row: T.QueryRow) -> str:
    return json.dumps({
        "kind": row.kind,
        "src": row.src,
        "t1": row.top1_id,
        "n1": row.top1_inliers,
        "d1": round(row.top1_det, 4),
        "n2": row.top2_inliers,
    }, ensure_ascii=False)


def from_json(line: str) -> T.QueryRow:
    d = json.loads(line)
    return T.QueryRow(
        kind=d["kind"], src=d["src"], top1_id=d["t1"],
        top1_inliers=d["n1"], top1_det=d["d1"], top2_inliers=d["n2"],
    )


def _cmd_record(args: argparse.Namespace) -> int:
    rec, entries = load_corpus(args.corpus)
    chosen = _strided(entries, args.limit)

    # 顺序与 cli._cmd_eval 完全一致：load_holdout 返回的是 holdout.json 里的
    # 原始顺序（**没有**排序），cli 直接在这个顺序上 _strided。这里若多排一次
    # 序，--limit 抽到的就是另一批留出图，录出来的行对应的不是那次 eval——而
    # 两边的行数、报告格式都一模一样，不会有任何迹象提示对错了对象。
    holdout_paths = [str(p) for p in _strided(load_holdout(args.corpus), args.limit)]

    log(f"[scan] 图库 {len(entries)} 张；录制库内参考图 {len(chosen)} 张、"
        f"库外留出图 {len(holdout_paths)} 张，每张 {args.samples} 个样本"
        f"（合计 {(len(chosen) + len(holdout_paths)) * args.samples} 次查询）")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_rows = unreadable = 0

    # 边录边写，不在内存里攒完再写：一次录 3 万行，中途被 Ctrl-C 或 OOM 打断时
    # 已经跑掉的那 40 分钟不该一起消失。
    with out.open("w", encoding="utf-8") as fh:
        for label, kind, items in (
            ("库内参考图", "in", [(e.photo_id, e.ref_path) for e in chosen]),
            ("库外留出图", "oos", [(p, p) for p in holdout_paths]),
        ):
            if not items:
                continue
            t0 = time.time()
            every = _progress_every(len(items))
            for i, (src_id, path) in enumerate(items, 1):
                img = cv2.imread(path, cv2.IMREAD_COLOR)
                if img is None:
                    unreadable += 1
                else:
                    # 一次只解码一张（C1）：全部摊在内存里在万张量级会 OOM。
                    # _ref_seed 只由 (seed, src_id) 决定、与批次位置无关，所以
                    # 逐张调用与整批调用录出来的行完全一样。
                    for row in T.record(rec, {src_id: img}, kind=kind,
                                        samples_per_ref=args.samples, seed=args.seed):
                        fh.write(to_json(row) + "\n")
                        n_rows += 1
                    fh.flush()
                _progress(label, i, len(items), t0, every, tag="scan")

    log(f"[scan] 录了 {n_rows} 行 -> {out}" + (f"；{unreadable} 张读不出来" if unreadable else ""))
    return 0


def _pct(vals: list[int], q: float) -> float:
    return float(np.percentile(np.asarray(vals), q)) if vals else float("nan")


def _describe(name: str, rows: list[T.QueryRow]) -> None:
    """打一个群体的 top1 内点数与 ratio 分位数。

    ratio = top1 内点数 / top2 内点数，正是比值检验用的那个量。top2 为 0 时
    ratio 是无穷（唯一候选），单独报占比而不是塞一个编造的大数进分位数里。
    """
    if not rows:
        log(f"[scan]   {name:22s} 0 条")
        return
    n1 = [r.top1_inliers for r in rows]
    finite = [r.top1_inliers / r.top2_inliers for r in rows if r.top2_inliers]
    lone = sum(1 for r in rows if not r.top2_inliers)
    log(f"[scan]   {name:22s} {len(rows):6d} 条  "
        f"内点数 p5={_pct(n1, 5):.0f} p25={_pct(n1, 25):.0f} 中位={_pct(n1, 50):.0f} "
        f"p75={_pct(n1, 75):.0f} p95={_pct(n1, 95):.0f}  "
        f"ratio 中位={_pct(finite, 50) if finite else float('nan'):.2f} "
        f"p5={_pct(finite, 5) if finite else float('nan'):.2f}  "
        f"无次优={lone / len(rows):.0%}")


def _ratios(rows: list[T.QueryRow]) -> list[float]:
    """top1/top2 内点数之比，也就是比值检验用的那个量。

    top2=0（唯一候选）时给 +inf：这类查询任何 RATIO 都拦不住，把它算成一个
    有限大数会让"抬 RATIO 的代价"被低估。
    """
    return [r.top1_inliers / r.top2_inliers if r.top2_inliers else float("inf")
            for r in rows]


def _overlap(good: list[T.QueryRow], bad: list[T.QueryRow]) -> None:
    """两个群体的重叠度：能不能用一条横线把它们分开。

    这是"该不该动阈值"的直接答案。如果假阳性的上界低于真阳性的下界，存在一个
    阈值能全清假阳性而不伤召回；如果两个分布互相穿插，任何阈值都是在两种错误
    之间做交换，得看交换比——内点数和 ratio 两个维度分别看，因为它们是两个
    独立的旋钮，可能只有一个有分离度。
    """
    if not good or not bad:
        return
    for what, g, b, fmt in (
        ("内点数", sorted(r.top1_inliers for r in good), sorted(r.top1_inliers for r in bad), "d"),
        ("ratio", sorted(_ratios(good)), sorted(_ratios(bad)), ".2f"),
    ):
        # 抬阈值 = 要求这个量 >= 门槛。要清掉全部假阳性，门槛得高过假阳性的
        # 上界；代价是所有低于这个门槛的真阳性变漏检。
        top_bad = b[-1]
        if top_bad == float("inf"):
            log(f"[scan]   {what}：假阳性里存在无次优候选（比值无穷），"
                f"单靠 {what} 阈值**无法**清掉全部假阳性")
            lost = len(good)
        else:
            lost = sum(1 for v in g if v <= top_bad)
            log(f"[scan]   {what}：真阳性下界 {g[0]:{fmt}}（p5={_pct(g, 5):.2f}）"
                f"  vs  假阳性上界 {top_bad:{fmt}}（p95={_pct(b, 95):.2f}）")
            log(f"[scan]     要靠 {what} 单独清掉全部假阳性，门槛需 > {top_bad:{fmt}}，"
                f"代价 {lost}/{len(g)}（{lost / len(g):.1%}）的真阳性变漏检")


def _fine_sweep(
    rows: list[T.QueryRow],
    oos_rows: list[T.QueryRow],
    kind_of: dict[tuple[str, str], str],
) -> None:
    """在当前 RATIO 上按 1 细扫 MIN_INLIERS，定位可行窗口的两端。

    粗网格（步长 5-10）只能说"40 行、50 不行"，说不出窗口到底从哪开始、到哪
    结束。而这两个端点就是决策本身：下界 = 真实误识别归零的最小值，上界 =
    命中率仍守住 §14.2 基线的最大值。窗口有多宽，直接决定"取下界"这个选择
    有多少对未见语料的余量。

    扫描范围是**从数据里算出来的**而不是写死的：先找到这两个端点，再往两侧
    各留几档上下文。写死范围的话换一份语料就可能整个窗口都在范围外，而表格
    照样打得出来，看起来像"没有可行点"。
    """
    def at(mi: int) -> tuple[T.SweepPoint, int]:
        p = T.sweep(rows, min_inliers=mi)
        n_g = sum(
            1 for r in oos_rows
            if T.outcome(r, min_inliers=mi) == "false_positive"
            and kind_of.get((r.src, r.top1_id), "未归类") in ("真实误识别", "未归类")
        )
        return p, n_g

    lo = next((mi for mi in range(1, 201) if at(mi)[1] == 0), None)
    if lo is None:
        log("[scan] 细扫跳过：内点数下限一路扫到 200 都清不掉全部真实误识别，"
            "说明这个旋钮单独不够用")
        return
    hi = max(
        (mi for mi in range(lo, 201) if at(mi)[0].correct_rate >= BASELINE_CORRECT_RATE),
        default=lo - 1,
    )
    if hi < lo:
        log(f"[scan] 细扫：真实误识别归零最早要 MIN_INLIERS={lo}，但那时库内命中已"
            f"跌破 {BASELINE_CORRECT_RATE:.0%}——**没有**可行窗口")
        return

    log(f"\n[scan] RATIO={RATIO} 上按 1 细扫（可行窗口 MIN_INLIERS ∈ [{lo}, {hi}]，"
        f"宽 {hi - lo + 1} 档）：")
    log(f"[scan]  MIN | 真实误识别(条) | 库内命中    漏检 | 库外总误识")
    for mi in range(max(1, lo - 4), min(200, hi + 3) + 1):
        p, n_g = at(mi)
        edge = ""
        if mi == lo:
            edge = "  <- 下界：真实误识别归零"
        elif mi == hi:
            edge = f"  <- 上界：再高一档命中率就跌破 {BASELINE_CORRECT_RATE:.0%}"
        elif p.correct_rate < BASELINE_CORRECT_RATE:
            edge = "  ❌ 命中率不达标"
        log(f"[scan] {mi:>4} | {n_g:>13} | {p.correct_rate:8.2%} {p.missed_rate:7.2%} "
            f"| {p.oos_false_positive_rate:9.3%}{edge}")


def _cmd_analyze(args: argparse.Namespace) -> int:
    rows = [from_json(line) for line in Path(args.rows).read_text().splitlines() if line.strip()]
    if not rows:
        print(f"{args.rows} 里没有行", file=sys.stderr)
        return 2
    in_rows = [r for r in rows if r.kind == "in"]
    oos_rows = [r for r in rows if r.kind == "oos"]
    log(f"[scan] {len(rows)} 行：库内 {len(in_rows)}，库外 {len(oos_rows)}")

    # 归类表：classify_fp.py 的报告按 (留出图, photo_id) 给出每对的成因。
    # 只覆盖 (fp_mi, fp_ra) 下的假阳性对——比它严只会让假阳性变少，所以够用；
    # 比它松会冒出没归类过的对，单独计一类，不假装知道它是什么。
    fp_mi = MIN_INLIERS if args.fp_min_inliers is None else args.fp_min_inliers
    fp_ra = RATIO if args.fp_ratio is None else args.fp_ratio
    kind_of: dict[tuple[str, str], str] = {}
    if args.fp_json:
        report = json.loads(Path(args.fp_json).read_text())
        for r in report.get("rows", []):
            if "kind" in r:
                kind_of[(r["holdout"], r["photo_id"])] = r["kind"]
        log(f"[scan] 归类表 {len(kind_of)} 对（来自 {args.fp_json}，"
            f"覆盖 MIN_INLIERS={fp_mi} RATIO={fp_ra} 下的假阳性对）")

    def fp_kinds(pt_rows: list[T.QueryRow]) -> Counter:
        c: Counter[str] = Counter()
        for r in pt_rows:
            c[kind_of.get((r.src, r.top1_id), "未归类")] += 1
        return c

    # ---- 当前阈值下各群体的分布 ----
    base = T.sweep(rows)
    log(f"\n[scan] 当前阈值 MIN_INLIERS={MIN_INLIERS} RATIO={RATIO} 下的分布"
        f"（库内 {base.correct_rate:.2%} 命中 / {base.missed_rate:.2%} 漏检；"
        f"库外 {base.oos_false_positive_rate:.3%} 误识别）：")
    tp = [r for r in in_rows if T.outcome(r) == "correct"]
    miss = [r for r in in_rows if T.outcome(r) == "missed"]
    in_wrong = [r for r in in_rows if T.outcome(r) == "wrong"]
    fp = [r for r in oos_rows if T.outcome(r) == "false_positive"]
    cr = [r for r in oos_rows if T.outcome(r) == "correct_rejection"]
    _describe("库内真阳性", tp)
    _describe("库内漏检", miss)
    _describe("库内误识别", in_wrong)
    _describe("库外假阳性", fp)
    _describe("库外正确拒绝", cr)

    # 真实误识别取 (fp_mi, fp_ra) 那一帧，而不是当前阈值那一帧：这批事件正是
    # "阈值该定在哪"的**证据**，一旦当前阈值已经把它们清掉了，按当前帧取就成了
    # 空集，报表里那段可分性分析会整段消失——恰好在阈值调对之后失去说明力。
    genuine = [
        r for r in oos_rows
        if T.outcome(r, min_inliers=fp_mi, ratio=fp_ra) == "false_positive"
        and kind_of.get((r.src, r.top1_id)) == "真实误识别"
    ]
    if kind_of:
        log("[scan] 库外假阳性按成因拆开：")
        for kind, cnt in fp_kinds(fp).most_common():
            sub = [r for r in fp if kind_of.get((r.src, r.top1_id), "未归类") == kind]
            _describe(f"  {kind}", sub)

        # 交叉校验：在**产出 fp.json 那次 eval 的阈值**上重放，录出来的假阳性
        # **对**（去重）应当与那次 eval 的对集完全相同。tests 里的等价性断言跑在
        # 12 张合成语料上，这一条是在**真实语料**上唯一能证明"录的就是那次 eval"
        # 的证据——两边参数如果有一处不同，行数和报表格式照样正常，只有这个集合
        # 会对不上。
        recorded = {
            (r.src, r.top1_id) for r in oos_rows
            if T.outcome(r, min_inliers=fp_mi, ratio=fp_ra) == "false_positive"
        }
        expected = set(kind_of)
        at = f"MIN_INLIERS={fp_mi} RATIO={fp_ra}"
        if recorded == expected:
            log(f"[scan] ✅ 交叉校验：{at} 下录到的 {len(recorded)} 对假阳性与 "
                f"{args.fp_json} 的对集逐对相同（录制确实复现了那次 eval）")
        else:
            log(f"[scan] ⚠️  交叉校验**不通过**（在 {at} 上重放）：录到 "
                f"{len(recorded)} 对，归类表 {len(expected)} 对；录到但未归类 "
                f"{len(recorded - expected)} 对，归类表有而没录到 "
                f"{len(expected - recorded)} 对。")
            # 先试试"只是阈值不同"这个解释，它比"不是同一批查询"常见得多，而且
            # 两者的处置完全不同（前者补个参数，后者整批数据作废）。网格上如果
            # 有**唯一**一点能逐对复现，那就是 fp.json 那次 eval 的阈值。
            hits = [
                (mi, ra) for mi in MIN_INLIERS_GRID for ra in RATIO_GRID
                if {(r.src, r.top1_id) for r in oos_rows
                    if T.outcome(r, min_inliers=mi, ratio=ra) == "false_positive"
                    } == expected
            ]
            if len(hits) == 1:
                mi, ra = hits[0]
                log(f"[scan]    但在 MIN_INLIERS={mi} RATIO={ra} 上能逐对复现 —— "
                    f"fp.json 是那组阈值下产出的。重跑时加 --fp-min-inliers {mi} "
                    f"--fp-ratio {ra}，下面的网格数字本身不受影响。")
            else:
                log("[scan]    这说明录制与产出 fp.json 的那次 eval 不是同一批查询"
                    "（语料目录 / --samples / --limit / --seed 至少有一处不同），"
                    "下面的网格数字不能直接拿来做决定。")
                for pair in sorted(recorded - expected)[:5]:
                    log(f"[scan]    录到但未归类：{pair[0]} -> {pair[1]}")
                for pair in sorted(expected - recorded)[:5]:
                    log(f"[scan]    归类表有而没录到：{pair[0]} -> {pair[1]}")

    log("\n[scan] 内点数分布的可分性：")
    _overlap(tp, fp)
    if genuine:
        still = sum(1 for r in genuine if T.outcome(r) == "false_positive")
        log(f"[scan]   只看真实误识别（同一被摄物体的不同照片是语料属性，不该拿来"
            f"定阈值）。这 {len(genuine)} 条是 MIN_INLIERS={fp_mi} RATIO={fp_ra} 下"
            f"归类出来的，当前阈值下还剩 {still} 条：")
        _overlap(tp, genuine)

    # ---- 网格重放 ----
    log(f"\n[scan] 阈值网格重放（{len(MIN_INLIERS_GRID)}×{len(RATIO_GRID)} 个组合，"
        f"每个都是对同一批 {len(rows)} 次查询的精确重算，不是插值）")
    grid = []
    for mi in MIN_INLIERS_GRID:
        for ra in RATIO_GRID:
            p = T.sweep(rows, min_inliers=mi, ratio=ra)
            pt_fp = [r for r in oos_rows
                     if T.outcome(r, min_inliers=mi, ratio=ra) == "false_positive"]
            kinds = fp_kinds(pt_fp) if kind_of else Counter()
            grid.append((p, kinds))

    # 「其中真实」取**保守上界**：真实误识别 + 未归类。未归类只在放松到
    # (fp_mi, fp_ra) 以下时才出现（`fp.json` 是在那组阈值上归类的，比它松就会冒出
    # 没见过的对），这些对可能是真实误识别也可能不是。若按下界（只数已归类的）
    # 算，放松阈值会显得"误识别更少"——那是归类表覆盖不全的假象，恰好会把结论
    # 推向错的方向。
    log("[scan]  MIN RATIO |  库内命中  库内误识    漏检 |  库外误识  其中真实 | 达标")
    for p, kinds in grid:
        n_g = kinds["真实误识别"] + kinds["未归类"]
        gr = n_g / p.oos_total if (kind_of and p.oos_total) else None
        g = f"{gr:8.3%}{'?' if kinds['未归类'] else ' '}" if gr is not None else "        -"
        # 两种"达标"：全部库外假阳性都算（严格，Oxford5k 的语料属性会主导），
        # 和只算真实误识别（把"同一被摄物体的不同照片"剔掉后的样子）。分开标，
        # 因为前者在这份语料上几乎不可能达标，会掩盖后者的差异。
        mark = "全部✅" if p.meets_baseline else ""
        if gr is not None and gr <= 0.001 and p.correct_rate >= 0.95 and p.wrong_rate <= 0.001:
            mark = (mark + " 真实✅").strip()
        star = "  <- 当前" if (p.min_inliers == MIN_INLIERS and p.ratio == RATIO) else ""
        log(f"[scan] {p.min_inliers:>4} {p.ratio:>5.1f} | {p.correct_rate:8.2%} "
            f"{p.wrong_rate:8.3%} {p.missed_rate:7.2%} | "
            f"{p.oos_false_positive_rate:8.3%} {g} | {mark}{star}")
    if kind_of and any(k["未归类"] for _, k in grid):
        log("[scan] `?` = 这一行含未归类的假阳性对（阈值比 fp.json 那次松），"
            "「其中真实」已按最坏情况把它们全算成真实误识别")

    # ---- 结论：能同时满足 §14.2 四条的点里，命中率最高的那个 ----
    if kind_of:
        def genuine_rate(p: T.SweepPoint, k: Counter) -> float:
            return (k["真实误识别"] + k["未归类"]) / p.oos_total if p.oos_total else 0.0

        _fine_sweep(rows, oos_rows, kind_of)

        feasible = [
            (p, k) for p, k in grid
            if p.correct_rate >= 0.95 and p.wrong_rate <= 0.001
            and p.oos_total and genuine_rate(p, k) <= 0.001
        ]
        log("")
        if not feasible:
            best_g = min(grid, key=lambda x: genuine_rate(*x))
            log("[scan] 结论：网格里**没有**任何 (MIN_INLIERS, RATIO) 组合能让真实误识别"
                "降到 ≤0.1% 而库内正确率仍 ≥95%。")
            log(f"[scan]   网格上真实误识别最低的点是 MIN_INLIERS={best_g[0].min_inliers} "
                f"RATIO={best_g[0].ratio}：{genuine_rate(*best_g):.3%}，"
                f"但库内命中只剩 {best_g[0].correct_rate:.2%}（漏检 "
                f"{best_g[0].missed_rate:.2%}）。")
            log("[scan]   也就是说这两个旋钮不够用：缺口得靠别的手段关"
                "（更强的几何约束 / 第三条判定 / 提高查询图质量），"
                "而不是继续在这两个数上找。")
        else:
            # 排序规则直接照抄 spec §14.2：「误识别率比漏检率重要一个数量级……
            # 调参时若两者冲突，一律牺牲漏检率保误识别率」。所以先按真实误识别
            # 升序，再按命中率降序——**不是**先挑命中率最高的那个。两者会给出
            # 不同答案（本轮数据上 30/1.5 命中最高但真实误识别 0.092%，
            # 40/1.5 命中低 0.5pp 而真实误识别 0），照命中率排会选错。
            ranked = sorted(feasible, key=lambda x: (genuine_rate(*x), -x[0].correct_rate))
            p, k = ranked[0]
            log(f"[scan] 结论：可行域 {len(feasible)}/{len(grid)} 个点。按 §14.2"
                "「一律牺牲漏检率保误识别率」排序（真实误识别升序，命中率降序）：")
            for q, kk in ranked:
                log(f"[scan]   MIN_INLIERS={q.min_inliers:<3} RATIO={q.ratio:<4}"
                    f"真实误识别 {genuine_rate(q, kk):7.3%}  库内命中 {q.correct_rate:.2%}"
                    f"  漏检 {q.missed_rate:.2%}  库外总误识 "
                    f"{q.oos_false_positive_rate:.3%}")
            now = next(
                x for x in grid
                if x[0].min_inliers == MIN_INLIERS and x[0].ratio == RATIO
            )
            log(f"[scan] 推荐 MIN_INLIERS={p.min_inliers} RATIO={p.ratio}："
                f"真实误识别 {genuine_rate(p, k):.3%}（当前 {genuine_rate(*now):.3%}）、"
                f"库内命中 {p.correct_rate:.2%}（当前 {base.correct_rate:.2%}，代价 "
                f"{base.correct_rate - p.correct_rate:.2%}）")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "n_rows": len(rows),
            "current": {"min_inliers": MIN_INLIERS, "ratio": RATIO},
            "grid": [
                {
                    "min_inliers": p.min_inliers, "ratio": p.ratio,
                    "correct": p.correct, "wrong": p.wrong, "missed": p.missed,
                    "false_positive": p.false_positive,
                    "correct_rejection": p.correct_rejection,
                    "fp_by_cause": dict(k),
                }
                for p, k in grid
            ],
        }, ensure_ascii=False, indent=2))
        log(f"[scan] 网格 -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    rc = sub.add_parser("record", help="跑一次真查询，把候选分数录成 JSONL")
    rc.add_argument("--corpus", required=True)
    rc.add_argument("--samples", type=int, default=20)
    rc.add_argument("--limit", type=int, default=0, help="等间距抽样，0=全量")
    rc.add_argument("--seed", type=int, default=1)
    rc.add_argument("--out", required=True)
    rc.set_defaults(func=_cmd_record)

    an = sub.add_parser("analyze", help="在录好的行上离线重放阈值网格")
    an.add_argument("--rows", required=True)
    an.add_argument("--fp-json", default=None, help="classify_fp.py 的报告，用于拆成因")
    # 交叉校验要在**产出 fp.json 那次 eval 的阈值**上重放，而不是在当前模块常量
    # 上。两者一开始相同，抬过 MIN_INLIERS 之后就不同了：那时按当前常量重放会得
    # 到更少的假阳性对，交叉校验会以"归类表有而没录到 N 对"的形式假失败——看着
    # 像录制和 eval 不是同一批，其实只是阈值变了。旧产物要显式指定。
    an.add_argument("--fp-min-inliers", type=int, default=None,
                    help=f"产出 fp.json 那次 eval 的 MIN_INLIERS，默认取当前值 "
                         f"{MIN_INLIERS}（阈值改动之前录的产物要显式给旧值）")
    an.add_argument("--fp-ratio", type=float, default=None,
                    help=f"同上，对应 RATIO，默认取当前值 {RATIO}")
    an.add_argument("--out", default=None, help="网格结果 JSON")
    an.set_defaults(func=_cmd_analyze)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
