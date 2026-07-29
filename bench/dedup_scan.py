"""近似重复扫描的命令行外壳：读图 -> 建临时索引 -> 调 photoar.dedup -> 出报告。

判定逻辑全在 `photoar.dedup`（有单元测试）。这里只负责 IO 与进度输出，
刻意不含任何"什么算重复"的判断——那种判断散在脚本里就没人测得到。

为什么需要它：`corpus.build_corpus` 的内容哈希只挡得住字节完全相同的重复，
重新编码/裁切的近似重复会两份都入库并互相判 ambiguous，两份都永久漏检。
里程碑 0d 里这一项造成 6.25% 库内漏检 + 32.7% 库外假阳性。

用法：
    python bench/dedup_scan.py --photos <目录> --out <报告目录> \
        [--out-clean <干净子集目录>] [--top-k 20] [--min-inliers 25] [--limit N]

产物：
    <out>/dedup.json   全部候选对得分、近重复簇、内点数直方图
    <out>/keep.txt     每簇留一张后的路径清单
    --out-clean        上述清单的符号链接目录，可直接喂给 photoar build

只读输入照片，不修改也不删除任何原始文件。
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from photoar import dedup
from photoar import vocab as V
from photoar.corpus import IMAGE_SUFFIXES, TRAIN_DESC_CAP
from photoar.features import extract
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TOP_K
from photoar.verify import MIN_INLIERS

_BUCKETS = ((0, 5), (6, 10), (11, 15), (16, 20), (21, 25), (26, 50), (51, 100),
            (101, 200))


def log(msg: str) -> None:
    print(msg, flush=True)


def _bucket(v: int) -> str:
    for lo, hi in _BUCKETS:
        if lo <= v <= hi:
            return f"{lo}-{hi}"
    return "200+"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", required=True, help="照片目录（递归）")
    ap.add_argument("--out", required=True, help="报告输出目录")
    ap.add_argument("--out-clean", default=None,
                    help="另建一个符号链接目录，每簇只链一张，可直接 photoar build")
    ap.add_argument("--top-k", type=int, default=TOP_K,
                    help=f"每张照片检查多少个粗排候选，默认 {TOP_K}（与识别器一致）")
    ap.add_argument("--min-inliers", type=int, default=MIN_INLIERS,
                    help=f"判为近重复的内点阈值，默认 {MIN_INLIERS}"
                         f"（= verify.MIN_INLIERS，与识别器同口径）")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 张，0 = 全部")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.limit < 0:
        print(f"--limit 不能为负数，收到 {args.limit!r}", file=sys.stderr)
        return 2
    if args.top_k < 1:
        print(f"--top-k 必须为正整数，收到 {args.top_k!r}", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    photo_dir = Path(args.photos)
    paths = sorted(p for p in photo_dir.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"在 {photo_dir} 下没有找到图片（支持 {sorted(IMAGE_SUFFIXES)}）",
              file=sys.stderr)
        return 2
    log(f"[dedup] {len(paths)} 张待扫，top_k={args.top_k} min_inliers={args.min_inliers}")

    # --- 提特征。跳过口径与 build_corpus 一致，并把跳过数报出来 ---
    feats, kept_paths = [], []
    skipped = {"unreadable": 0, "zero_feature": 0}
    for i, p in enumerate(paths):
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            skipped["unreadable"] += 1
            continue
        f = extract(img)
        if len(f) == 0:
            skipped["zero_feature"] += 1
            continue
        feats.append(f)
        kept_paths.append(p)
        if (i + 1) % 100 == 0:
            log(f"[dedup]   提特征 {i + 1}/{len(paths)}（{time.perf_counter() - t0:.0f}s）")
    n = len(feats)
    if sum(skipped.values()):
        log(f"[dedup] 跳过 {sum(skipped.values())} 张：读不出 {skipped['unreadable']}、"
            f"无特征 {skipped['zero_feature']}")
    if n < 2:
        print(f"可用照片只有 {n} 张，无法做成对比较", file=sys.stderr)
        return 2

    # --- 临时词表 + 倒排索引，只为拿粗排候选，不落盘成语料 ---
    all_desc = np.vstack([f.desc for f in feats])
    train_desc = all_desc
    if train_desc.shape[0] > TRAIN_DESC_CAP:
        rng = np.random.default_rng(args.seed)
        train_desc = train_desc[rng.choice(train_desc.shape[0], TRAIN_DESC_CAP, False)]
        log(f"[dedup] 词表训练抽样 {TRAIN_DESC_CAP}/{all_desc.shape[0]} 个描述子"
            f"（与 build_corpus 同一上限）")
    t = time.perf_counter()
    voc = V.train(train_desc, seed=args.seed)
    log(f"[dedup] 词表 {voc.n_words} 词，{time.perf_counter() - t:.0f}s")

    t = time.perf_counter()
    builder = InvertedIndexBuilder(voc.n_words)
    doc_words = []
    for f in feats:
        w = voc.words_of(f.desc)
        doc_words.append(w)
        builder.add(w)
    index = builder.build()
    log(f"[dedup] 索引 n_docs={index.n_docs}，{time.perf_counter() - t:.0f}s")

    # 候选：自己的词查自己的索引，去掉自己后取前 top_k。多要一个再截断，
    # 因为 top-1 通常就是自己。
    candidates = [
        [d for d, _ in index.query(doc_words[i], args.top_k + 1) if d != i][: args.top_k]
        for i in range(n)
    ]

    t = time.perf_counter()

    def progress(done: int, total: int) -> None:
        if done % 100 == 0 or done == total:
            log(f"[dedup]   校验 {done}/{total} 张（{time.perf_counter() - t:.0f}s）")

    report = dedup.scan_pairs(
        feats, candidates, min_inliers=args.min_inliers, top_k=args.top_k,
        on_progress=progress,
    )
    log(f"[dedup] 校验完成：{report.n_verify_pair} 次 verify_pair，"
        f"{len(report.pair_scores)} 个不同的对，{time.perf_counter() - t:.0f}s")

    # --- 内点数直方图。0d 的关键证据就是这个分布上的空档，换语料必须重看 ---
    hist = Counter(_bucket(v) for v in report.pair_scores.values())
    log(f"[dedup] 候选对内点数分布（判定阈值 {args.min_inliers}）：")
    for lo, hi in _BUCKETS:
        b = f"{lo}-{hi}"
        if hist.get(b):
            log(f"[dedup]   {b:>8} : {hist[b]}")
    if hist.get("200+"):
        log(f"[dedup]   {'200+':>8} : {hist['200+']}")

    dup_pairs = report.dup_pairs
    clusters = dedup.cluster(dup_pairs, n)
    multi = [m for m in clusters if len(m) > 1]
    involved = sum(len(m) for m in multi)
    log(f"[dedup] 近重复对 {len(dup_pairs)} 个，簇 {len(multi)} 个，卷入 {involved}/{n} 张")
    for m in sorted(multi, key=len, reverse=True)[:10]:
        log(f"[dedup]   簇({len(m)}): {', '.join(kept_paths[i].name for i in m)}")
    if len(multi) > 10:
        log(f"[dedup]   ...另有 {len(multi) - 10} 个簇")

    keep = dedup.select_keep(clusters, kept_paths)
    keep_paths = [kept_paths[i] for i in keep]
    log(f"[dedup] 保留 {len(keep_paths)} 张（剔除 {n - len(keep_paths)} 张近重复）")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "dedup.json").write_text(
        json.dumps(
            {
                "photos_found": len(paths),
                "photos_scanned": n,
                "skipped": skipped,
                "top_k": report.top_k,
                "min_inliers": report.min_inliers,
                "n_verify_pair": report.n_verify_pair,
                "inlier_histogram": dict(hist),
                "pairs": [
                    {"a": str(kept_paths[a]), "b": str(kept_paths[b]),
                     "inliers": report.pair_scores[(a, b)]}
                    for a, b in dup_pairs
                ],
                "clusters": [
                    [str(kept_paths[i]) for i in m]
                    for m in sorted(multi, key=len, reverse=True)
                ],
                "keep": [str(p) for p in keep_paths],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "keep.txt").write_text(
        "\n".join(str(p) for p in keep_paths) + "\n", encoding="utf-8"
    )

    if args.out_clean:
        clean = Path(args.out_clean)
        clean.mkdir(parents=True, exist_ok=True)
        for p in keep_paths:
            link = clean / p.name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(p.resolve())
        log(f"[dedup] 干净子集：{len(keep_paths)} 个符号链接 -> {clean}"
            f"（可直接 photoar build --photos {clean}）")

    log(f"[dedup] 全脚本 {time.perf_counter() - t0:.0f}s，报告 -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
