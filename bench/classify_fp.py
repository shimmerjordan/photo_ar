"""把 `photoar eval` 报出的库外假阳性逐条归类：漏掉的近重复，还是真实误识别。

为什么需要它：库外假阳性率是 0d 的验收闸门之一，但这个数字有两种完全不同的
成因，处置方式相反——

  A) **漏掉的近似重复**：留出图其实是某张库内照片的另一份编码/裁切。识别器
     "认出"它是正确行为，错在语料卫生。dedup 的 O(N·K) 扫描只验证粗排
     Top-K 候选对，没进对方 Top-K 的近重复就会漏过去。处置：扩大 K 或按
     分组做补扫，不动判定阈值。
  B) **真实误识别**：两张原图根本不匹配，是合成扰动在不相关的照片之间造出了
     足够多的伪内点。处置：这才是该动 MIN_INLIERS / RATIO 的证据。

把两者混在一个百分比里，会让人对着 A 去调阈值（伤召回、且治不好），或者对着
B 去清语料（清不掉）。

归类只看**原图**互查（不经合成扰动），因为"这两张照片是不是同一张的另一份
编码"是照片本身的属性，与查询时的扰动无关。判据与 photoar.dedup 完全一致：

    m = max(verify_pair(a,b), verify_pair(b,a)) 的内点数   # verify_pair 不对称
    s = min(self_score(a), self_score(b))                  # 现实自匹配分
    近重复  <=>  m >= MIN_INLIERS 且 s < RATIO * m

用法：
    python bench/classify_fp.py --corpus <语料目录> --eval-log <eval 的日志>
        [--self-samples 3] [--seed 1] [--out <report.json>]

`--eval-log` 直接吃 `photoar eval` 的输出（stdout+stderr 混在一个文件里也行），
从中抽 "  <留出图路径> -> <photo_id>" 那些行。同一对只算一次。

只读输入，不修改语料也不改任何阈值。
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import cv2

from photoar import dedup, synth
from photoar.features import extract
from photoar.verify import DET_MAX, DET_MIN, MIN_INLIERS, RATIO, verify_pair

# eval 打的那一行长这样（注意行首两个空格）：
#   /path/to/holdout.jpg -> 7a9e84fb41f19245
_FP_LINE = re.compile(r"^\s+(?P<qid>\S.*?)\s+->\s+(?P<pid>\S+)\s*$")


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_fp_lines(text: str) -> list[tuple[str, str]]:
    """抽出 (留出图路径, 命中的 photo_id) 列表，保序去重。

    只认"箭头行"，不认它上面那句中文说明——说明文字里没有箭头，天然被过滤掉。
    保序而不是排序：让报告里的顺序与 eval 日志一致，便于对照原始日志。
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        m = _FP_LINE.match(line)
        if not m:
            continue
        key = (m.group("qid"), m.group("pid"))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def mutual_inliers(fa, fb) -> tuple[int, float]:
    """两个方向各跑一次取内点数较大的那个，返回 (内点数, 对应的 det)。

    verify_pair 不对称：findHomography 的方向、BFMatcher 的 query/train 角色
    都不同，同一对照片两个方向的内点数实测能差一两成。取 max 与 photoar.dedup
    的 scan_pairs 一致。det 超出 [DET_MIN, DET_MAX] 的方向按 0 内点计（镜像/
    退化变换本来就不该算匹配）。
    """
    best_inliers, best_det = 0, 0.0
    for q, r in ((fa, fb), (fb, fa)):
        res = verify_pair(q, r, photo_id="x")
        n = res.inliers if DET_MIN <= res.det <= DET_MAX else 0
        if n > best_inliers:
            best_inliers, best_det = n, res.det
    return best_inliers, best_det


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", required=True, help="photoar build 产出的语料目录")
    ap.add_argument("--eval-log", required=True, help="photoar eval 的输出文件")
    ap.add_argument("--self-samples", type=int, default=3,
                    help="每张照片算自匹配分用几张扰动查询图（默认 3，取中位数）")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=None, help="报告 JSON 路径（默认只打印）")
    args = ap.parse_args(argv)

    if args.self_samples < 1:
        print(f"--self-samples 必须 >= 1，收到 {args.self_samples!r}", file=sys.stderr)
        return 2

    manifest_path = Path(args.corpus) / "manifest.json"
    if not manifest_path.exists():
        print(f"找不到 {manifest_path}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text())
    ref_of = {p["photo_id"]: p["ref_path"] for p in manifest["photos"]}

    fps = parse_fp_lines(Path(args.eval_log).read_text())
    if not fps:
        log("eval 日志里没有库外假阳性行——库外误识别为 0，无需归类")
        return 0
    log(f"[fp] {len(fps)} 个不同的 (留出图 -> 库内照片) 对待归类")

    t0 = time.time()
    feat_cache: dict[str, object] = {}
    self_cache: dict[str, int] = {}

    def feats(path: str):
        if path not in feat_cache:
            img = cv2.imread(path, cv2.IMREAD_COLOR)
            if img is None:
                raise FileNotFoundError(f"读不出图片：{path}")
            feat_cache[path] = (extract(img), img)
        return feat_cache[path]

    def self_of(path: str) -> int:
        # 自匹配分只与照片本身有关，缓存复用：同一张库内照片常被多张留出图命中
        # （先导 0d 里 5 张留出图命中 4 张库内照片，其中两张撞同一个）。
        if path not in self_cache:
            f, img = feats(path)
            qs = [extract(q) for q, _ in synth.generate(img, args.self_samples, args.seed)]
            self_cache[path] = dedup.self_score(f, qs)
        return self_cache[path]

    rows = []
    kinds: Counter[str] = Counter()
    for i, (qid, pid) in enumerate(fps, 1):
        ref = ref_of.get(pid)
        if ref is None:
            # photo_id 不在 manifest 里 = 语料与日志不是同一次跑的，不能猜。
            kinds["语料与日志不匹配"] += 1
            rows.append({"holdout": qid, "photo_id": pid, "kind": "语料与日志不匹配"})
            continue
        fa, _ = feats(qid)
        fb, _ = feats(ref)
        m, det = mutual_inliers(fa, fb)
        s = min(self_of(qid), self_of(ref))
        if m >= MIN_INLIERS and s < RATIO * m:
            kind = "漏掉的近重复"
        elif m >= MIN_INLIERS:
            kind = "能几何对上但不该混淆"
        else:
            kind = "真实误识别"
        kinds[kind] += 1
        rows.append({
            "holdout": qid, "photo_id": pid, "ref_path": ref,
            "mutual_inliers": m, "det": round(det, 3),
            "self_score_min": s, "ratio_x_m": round(RATIO * m, 1),
            "kind": kind,
        })
        if i % 20 == 0:
            log(f"[fp]   {i}/{len(fps)}（{time.time() - t0:.0f}s）")

    log(f"[fp] 归类完成（{time.time() - t0:.0f}s），判据 MIN_INLIERS={MIN_INLIERS} "
        f"RATIO={RATIO}：")
    for kind, cnt in kinds.most_common():
        log(f"[fp]   {kind}: {cnt}（{cnt / len(fps):.1%}）")

    genuine = [r for r in rows if r.get("kind") == "真实误识别"]
    if genuine:
        # 真实误识别的内点数分布决定了"要不要抬 MIN_INLIERS、抬到多少"。
        # 只报个数没用——如果它们都刚过阈值，抬 5 就能清掉；如果远超阈值，
        # 抬阈值的代价会落到召回率上，那就得换别的手段。
        vals = sorted(r["mutual_inliers"] for r in genuine)
        log(f"[fp] 真实误识别的原图互查内点数：最小 {vals[0]} 中位 "
            f"{vals[len(vals) // 2]} 最大 {vals[-1]}（阈值 {MIN_INLIERS}）")
        log("[fp] 注意：这是**原图**互查的内点数，不是查询时的。假阳性发生在"
            "合成扰动后的查询图上，扰动会造出额外的伪内点，实测同一对能从原图"
            "的 21 涨到查询时的 33。所以这一行只能说明两张原图有多不像，"
            "不能直接拿来定 MIN_INLIERS——要定阈值得量查询时的内点数分布")
        for r in genuine[:10]:
            log(f"[fp]   {Path(r['holdout']).name} -> {Path(r['ref_path']).name}"
                f"  原图内点 {r['mutual_inliers']}  自匹配分 {r['self_score_min']}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "min_inliers": MIN_INLIERS,
            "ratio": RATIO,
            "self_samples": args.self_samples,
            "n_pairs": len(fps),
            "counts": dict(kinds),
            "rows": rows,
        }, ensure_ascii=False, indent=2))
        log(f"[fp] 报告 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
