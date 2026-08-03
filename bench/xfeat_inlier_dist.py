#!/usr/bin/env python3
"""量两个识别后端的内点数分布，用来定 `verify.XFEAT_MIN_INLIERS`。

## 为什么需要这个脚本

ORB 那侧的 `MIN_INLIERS = 40` 是这么定下来的：29740 次真跑里，**真实误识别的内点数
最大只到 39**，而**库内真阳性的 5 分位是 69** —— 两个分布几乎不重叠，40 卡在中间。

换成 XFeat 之后那个 40 **作废**：关键点从 300 涨到 512，描述子从 32 字节二值换成 64 维
浮点，配对从 Hamming crossCheck 换成余弦互近邻（还先过了一道 0.82 的余弦闸门）。真阳性
的内点数会系统性更高，把 40 搬过来等于把判定放松，而这不会报错。

这个脚本量的就是那两个分布，口径与当年完全一致，所以两个后端的数字可以直接对着看：

  真阳性：查询图（`synth.generate` 扰动过的）vs **它自己的**参考图
  误识别：同一张查询图 vs **别人的**参考图里最强的那一个

`bench/threshold_scan.py` 做的是更完整的事（走完整两阶段管线、录候选分数、网格重放出
命中率/漏检率）。这个脚本刻意**只量精排那一步**：它不需要词表、不需要建库，因此能在
几分钟内跑完，而定阈值需要的正是这两个分布。上规模的端到端复核仍然要用那个脚本。

    python bench/xfeat_inlier_dist.py --photos /path/to/photos --refs 400 --queries 6

输出里 `建议下限` 那一行是**机械推导**，不是结论：取「误识别最大值 + 1」与「真阳性
p5」之间；两者交叉（分布重叠）时会明确报告重叠，此时没有干净的阈值，必须去看
threshold_scan 的命中率/误识别率权衡。
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from photoar import backend as backend_mod  # noqa: E402
from photoar import synth  # noqa: E402
from photoar.verify import DET_MAX, DET_MIN  # noqa: E402


def _load(path: Path) -> np.ndarray | None:
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return img if img is not None and min(img.shape[:2]) >= 200 else None


def _score(be, query, ref, pid: str) -> int:
    """一对的内点数。行列式出界记 0 —— 与 `dedup` / `library.conflicts` 同口径：
    镜像变换在实体照片成像里不可能出现，判定上等于没匹配上。"""
    r = be.verify(query, ref, pid)
    return r.inliers if DET_MIN <= r.det <= DET_MAX else 0


def measure(
    be,
    photos: list[Path],
    *,
    queries: int,
    distractors: int,
    seed: int,
) -> dict:
    rng = random.Random(seed)
    t0 = time.perf_counter()

    refs: list[tuple[str, object, np.ndarray]] = []
    for p in photos:
        img = _load(p)
        if img is None:
            continue
        refs.append((p.name, be.extract(img), img))
    if len(refs) < 2:
        raise SystemExit("可用参考图不足 2 张")

    tp: list[int] = []
    fp: list[int] = []
    n_kpts = [len(f) for _, f, _ in refs]

    for i, (name, ref_feat, ref_img) in enumerate(refs):
        for q_img, _ in synth.generate(ref_img, queries, seed + i):
            q_feat = be.extract(q_img)
            tp.append(_score(be, q_feat, ref_feat, name))
            # 误识别侧：随机抽 distractors 张别人的参考图，取**最强**的那个。
            # 取最强而不是取平均，因为判定看的是 top1 —— 平均会系统性低估风险。
            others = rng.sample(range(len(refs)), min(distractors + 1, len(refs)))
            worst = 0
            for j in others:
                if j == i:
                    continue
                worst = max(worst, _score(be, q_feat, refs[j][1], refs[j][0]))
            fp.append(worst)

    return {
        "backend": be.name,
        "refs": len(refs),
        "queries_per_ref": queries,
        "n_queries": len(tp),
        "kpts_median": float(np.median(n_kpts)),
        "tp": np.array(tp),
        "fp": np.array(fp),
        "elapsed_s": time.perf_counter() - t0,
    }


def report(m: dict) -> None:
    tp, fp = m["tp"], m["fp"]
    print(f"\n=== 后端 {m['backend']} ===")
    print(
        f"参考图 {m['refs']} 张，每张 {m['queries_per_ref']} 个扰动查询，"
        f"共 {m['n_queries']} 次；关键点中位数 {m['kpts_median']:.0f}；"
        f"耗时 {m['elapsed_s']:.0f}s"
    )
    print(
        "真阳性内点数：  "
        f"p1={np.percentile(tp, 1):.0f}  p5={np.percentile(tp, 5):.0f}  "
        f"中位={np.median(tp):.0f}  最小={tp.min()}"
    )
    print(
        "误识别内点数：  "
        f"最大={fp.max()}  p99={np.percentile(fp, 99):.0f}  "
        f"p95={np.percentile(fp, 95):.0f}  中位={np.median(fp):.0f}"
    )
    lo, hi = int(fp.max()) + 1, int(np.percentile(tp, 5))
    if lo <= hi:
        print(f"建议下限：[{lo}, {hi}]（误识别最大值+1 到 真阳性 p5），取下界 {lo}")
    else:
        print(
            f"⚠️ 两个分布重叠（误识别最大 {fp.max()} > 真阳性 p5 {hi}）："
            "没有能同时做到零误识别与 95% 命中的干净阈值，"
            "必须去看 bench/threshold_scan.py 的权衡曲线"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--photos", type=Path, required=True)
    ap.add_argument("--refs", type=int, default=300, help="用多少张参考图")
    ap.add_argument("--queries", type=int, default=6, help="每张参考图生成几个扰动查询")
    ap.add_argument(
        "--distractors", type=int, default=25, help="每个查询与多少张别人的图对比"
    )
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--backends", default="orb,xfeat", help="逗号分隔；两个都跑才能直接对比"
    )
    ap.add_argument("--model", type=Path, help="xfeat.onnx 路径")
    args = ap.parse_args()

    files = sorted(p for p in args.photos.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if not files:
        raise SystemExit(f"{args.photos} 里没有图片")
    # 固定步长抽样而不是取前 N 张：Oxford5k 的文件名按地标聚簇，取前 N 张会只取到
    # 一两个地标，误识别侧全是"同一建筑的不同照片"，把难度系统性夸大。
    step = max(1, len(files) // args.refs)
    picked = files[::step][: args.refs]
    print(f"语料 {args.photos}：{len(files)} 张，按步长 {step} 抽 {len(picked)} 张")

    for name in args.backends.split(","):
        name = name.strip()
        if not name:
            continue
        be = backend_mod.make(name, model_path=args.model)
        report(measure(be, picked, queries=args.queries,
                       distractors=args.distractors, seed=args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
