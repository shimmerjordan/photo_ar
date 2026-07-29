"""里程碑 0a：几何校验判别力（暴力检索，无词汇表变量）。

暴力检索是 O(N)：每个查询都要对全库做 verify_pair。200 库 × 200 查询 = 4 万次，
所以这个脚本必须当分离后台作业跑，并且逐步刷输出，否则看不到进度也survive不了。
"""

import pathlib
import sys
import tempfile
import time

import cv2
import numpy as np

from photoar import evaluate as E
from photoar import features as F
from photoar.bruteforce import BruteForceRecognizer
from photoar.descstore import DescStore, DescStoreWriter

LIB_SIZE = 200
EVAL_REFS = 20
SAMPLES_PER_REF = 10
SEED = 1


def make(seed, w=1000, h=700):
    """与 tests/conftest.py 的 textured_image 同构，但独立于 pytest。"""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (h // 8, w // 8, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
    for _ in range(40):
        x1, y1 = int(rng.integers(0, w)), int(rng.integers(0, h))
        x2 = min(w - 1, x1 + int(rng.integers(20, 120)))
        y2 = min(h - 1, y1 + int(rng.integers(20, 120)))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, -1)
    return img


def log(msg):
    print(msg, flush=True)


t_start = time.perf_counter()
log(f"[0a] 库规模={LIB_SIZE} 评估参考图={EVAL_REFS} 每张样本={SAMPLES_PER_REF} seed={SEED}")
log(f"[0a] 预期 verify_pair 调用数 = {EVAL_REFS * SAMPLES_PER_REF * LIB_SIZE}")

ids = [f"p{i}" for i in range(LIB_SIZE)]
imgs = {ids[i]: make(i) for i in range(LIB_SIZE)}
log(f"[0a] 生成 {LIB_SIZE} 张合成图，耗时 {time.perf_counter() - t_start:.1f}s")

t = time.perf_counter()
feats = [F.extract(imgs[k]) for k in ids]
log(f"[0a] 提取 ORB 特征完成，耗时 {time.perf_counter() - t:.1f}s，"
    f"平均 {np.mean([len(f) for f in feats]):.1f} 个/张")

d = pathlib.Path(tempfile.mkdtemp(prefix="photoar-0a-"))
with DescStoreWriter(d / "desc.bin", capacity=LIB_SIZE) as w:
    for f in feats:
        w.append(f)
log(f"[0a] 描述子库写入 {d / 'desc.bin'}"
    f"（{(d / 'desc.bin').stat().st_size / 1024 / 1024:.1f} MB）")

t = time.perf_counter()
with DescStore(d / "desc.bin") as store:
    rec = BruteForceRecognizer(store, ids)
    sample = {k: imgs[k] for k in ids[:EVAL_REFS]}
    metrics = E.evaluate(rec, sample, samples_per_ref=SAMPLES_PER_REF, seed=SEED)
elapsed = time.perf_counter() - t

log("")
log(metrics.as_report())
log("")
log(f"[0a] 评估耗时 {elapsed:.1f}s（{elapsed / max(1, metrics.total) * 1000:.0f} ms/查询，"
    f"每次查询含 {LIB_SIZE} 次 verify_pair）")
log(f"[0a] 全脚本耗时 {time.perf_counter() - t_start:.1f}s")
log("")

# 判读规则（计划 Task 5 Step 6）
if metrics.wrong > 0:
    log(f"[0a] 判读：未通过 —— 误识别 {metrics.wrong} 例（{metrics.wrong_rate:.3%}）。"
        f"几何校验判别力不足，须先调严 MIN_INLIERS/RATIO 再继续。")
    sys.exit(2)
if metrics.correct_rate < 0.95:
    log(f"[0a] 判读：正确命中率 {metrics.correct_rate:.2%} < 95%，但误识别为 0。"
        f"优先怀疑合成扰动过强，而非识别器弱。")
    sys.exit(1)
log(f"[0a] 判读：通过 —— 误识别 0 例，正确命中率 {metrics.correct_rate:.2%}。基线干净。")
sys.exit(0)
