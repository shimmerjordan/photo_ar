"""里程碑 0b：两阶段检索（1000 张合成库）。

要回答三个问题，按重要性排序：

1. 误识别率是否仍为 0 —— 粗排会不会把不相似的照片塞进候选反而制造歧义
2. 粗排召回率 —— Top-K 里到底有没有正确答案（这是两阶段的上限）
3. 端到端延迟 —— 两阶段是否真的把 0a 那 565ms/查询压到 80ms 目标内

同时用暴力检索在一个子集上做对照，直接回答「粗排是不是无损的」。

必须当分离后台作业跑，并逐步刷输出。
"""

import os
import pathlib
import sys
import tempfile
import time

import cv2
import numpy as np

from photoar import evaluate as E
from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.bruteforce import BruteForceRecognizer
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TOP_K, TwoStageRecognizer

LIB_SIZE = 1000
EVAL_REFS = 50
SAMPLES_PER_REF = 10
SEED = 1
# 词汇树训练用的描述子上限。全量 1000x300 = 30 万，k-majority 在根层会
# 反复分配 (30万,10,32) 的中间数组（约 192MB/次 x 8 次迭代）。抽样训练是
# 词袋检索的标准做法（词表本来就只需代表描述子分布），但这是一处与
# Task 11 build_corpus（全量训练）的差异，必须记录。
TRAIN_DESC_CAP = 120_000
BRUTE_REFS = 10           # 暴力对照只在小子集上做，否则要跑 20 分钟
BRUTE_SAMPLES = 3


def make(seed, w=900, h=650):
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
log(f"[0b] 库规模={LIB_SIZE} 评估参考图={EVAL_REFS} 每张样本={SAMPLES_PER_REF} "
    f"TOP_K={TOP_K} seed={SEED}")
BRANCHING = int(os.environ.get("PHOTOAR_BRANCHING", V.BRANCHING))
DEPTH = int(os.environ.get("PHOTOAR_DEPTH", V.DEPTH))
_dflt = " (模块默认)" if (BRANCHING, DEPTH) == (V.BRANCHING, V.DEPTH) else " (环境变量覆盖)"
log(f"[0b] 词汇树参数 branching={BRANCHING} depth={DEPTH}{_dflt}")

ids = [f"p{i}" for i in range(LIB_SIZE)]
imgs = {ids[i]: make(i) for i in range(LIB_SIZE)}
log(f"[0b] 生成 {LIB_SIZE} 张合成图，耗时 {time.perf_counter() - t_start:.1f}s")

t = time.perf_counter()
feats = [F.extract(imgs[k]) for k in ids]
log(f"[0b] 提取 ORB 完成 {time.perf_counter() - t:.1f}s，"
    f"平均 {np.mean([len(f) for f in feats]):.1f} 个/张")

d = pathlib.Path(tempfile.mkdtemp(prefix="photoar-0b-"))
with DescStoreWriter(d / "desc.bin", capacity=LIB_SIZE) as w:
    for f in feats:
        w.append(f)
log(f"[0b] 描述子库 {(d / 'desc.bin').stat().st_size / 1024 / 1024:.1f} MB")

all_desc = np.vstack([f.desc for f in feats])
if all_desc.shape[0] > TRAIN_DESC_CAP:
    sub = np.random.default_rng(SEED).choice(
        all_desc.shape[0], size=TRAIN_DESC_CAP, replace=False
    )
    train_desc = all_desc[np.sort(sub)]
    log(f"[0b] 词汇树训练抽样 {TRAIN_DESC_CAP} / {all_desc.shape[0]} 个描述子"
        f"（抽样训练是词袋标准做法，但与 Task 11 全量训练有差异）")
else:
    train_desc = all_desc

t = time.perf_counter()
voc = V.train(train_desc, branching=BRANCHING, depth=DEPTH, seed=SEED)
log(f"[0b] 词汇树训练完成 {time.perf_counter() - t:.1f}s，词数 {voc.n_words}")

t = time.perf_counter()
builder = InvertedIndexBuilder(voc.n_words)
doc_words = []
for i, f in enumerate(feats):
    wz = voc.words_of(f.desc)
    doc_words.append(wz)
    builder.add(wz)
    if (i + 1) % 200 == 0:
        log(f"[0b]   量化并入索引 {i + 1}/{LIB_SIZE}"
            f"（{time.perf_counter() - t:.1f}s）")
index = builder.build()
log(f"[0b] 索引构建完成 {time.perf_counter() - t:.1f}s，n_docs={index.n_docs}")

with DescStore(d / "desc.bin") as store:
    rec = TwoStageRecognizer(voc, index, store, ids)
    sample_ids = ids[:EVAL_REFS]

    # --- 问题 2：粗排召回率（Top-K 里有没有正确答案）---
    log("")
    t = time.perf_counter()
    hits = {k: 0 for k in (1, 5, 10, TOP_K)}
    total_probe = 0
    for i, pid in enumerate(sample_ids):
        for q, _ in synth.generate(imgs[pid], SAMPLES_PER_REF, seed=500 + i):
            cands = rec.candidates(q)
            total_probe += 1
            for k in hits:
                if pid in cands[:k]:
                    hits[k] += 1
    log(f"[0b] 粗排召回率（{total_probe} 次探测，耗时 {time.perf_counter() - t:.1f}s）")
    for k in sorted(hits):
        log(f"[0b]   Recall@{k:<3} = {hits[k] / total_probe:6.2%}  ({hits[k]}/{total_probe})")

    # --- 问题 1+3：端到端指标与延迟 ---
    log("")
    t = time.perf_counter()
    metrics = E.evaluate(rec, {k: imgs[k] for k in sample_ids},
                         samples_per_ref=SAMPLES_PER_REF, seed=SEED)
    two_elapsed = time.perf_counter() - t
    log("[0b] 两阶段端到端：")
    log(metrics.as_report())
    log(f"[0b] 评估耗时 {two_elapsed:.1f}s"
        f"（{two_elapsed / max(1, metrics.total) * 1000:.0f} ms/查询）")

    # --- 对照：暴力检索在小子集上（粗排是否无损）---
    log("")
    brute = BruteForceRecognizer(store, ids)
    subset = {k: imgs[k] for k in ids[:BRUTE_REFS]}
    t = time.perf_counter()
    bm = E.evaluate(brute, subset, samples_per_ref=BRUTE_SAMPLES, seed=SEED)
    log(f"[0b] 暴力对照（{BRUTE_REFS} 张参考 x {BRUTE_SAMPLES} 样本，"
        f"耗时 {time.perf_counter() - t:.1f}s）：")
    log(f"[0b]   正确 {bm.correct}/{bm.total}  误识别 {bm.wrong}  漏检 {bm.missed}")
    t = time.perf_counter()
    tm = E.evaluate(rec, subset, samples_per_ref=BRUTE_SAMPLES, seed=SEED)
    log(f"[0b]   同一子集两阶段（耗时 {time.perf_counter() - t:.1f}s）："
        f"正确 {tm.correct}/{tm.total}  误识别 {tm.wrong}  漏检 {tm.missed}")
    if bm.correct == tm.correct and bm.wrong == tm.wrong:
        log("[0b]   -> 粗排在该子集上是无损的")
    else:
        log(f"[0b]   -> 粗排有损：正确 {bm.correct}->{tm.correct}，"
            f"误识别 {bm.wrong}->{tm.wrong}")

log("")
log(f"[0b] 全脚本耗时 {time.perf_counter() - t_start:.1f}s")

# 判读（同 0a 的规则，外加粗排召回率）
if metrics.wrong > 0:
    log(f"[0b] 判读：未通过 —— 误识别 {metrics.wrong} 例（{metrics.wrong_rate:.3%}）。"
        f"粗排引入了新的混淆，先查 TOP_K 是否过大。")
    sys.exit(2)
recall_top_k = hits[TOP_K] / max(1, total_probe)
if recall_top_k < 0.95:
    log(f"[0b] 判读：粗排召回率 Recall@{TOP_K}={recall_top_k:.2%} < 95%，粗排是瓶颈。"
        f"实测方向是往更细走（提高 BRANCHING，优先于提高 DEPTH——层数越少越好，"
        f"每层都是一次走错分支的机会），不是调粗。")
    sys.exit(1)
if metrics.correct_rate < 0.95:
    # 端到端不可能超过 Recall@TOP_K，所以必须分清是天花板低还是精排丢的多
    refine_keep = metrics.correct_rate / recall_top_k if recall_top_k else 0.0
    log(f"[0b] 判读：正确命中率 {metrics.correct_rate:.2%} < 95%，误识别为 0。"
        f"天花板 Recall@{TOP_K}={recall_top_k:.2%}，精排保留率 {refine_keep:.2%}。")
    if 1.0 - recall_top_k > 1.0 - refine_keep:
        log(f"[0b]   主要损失在粗排（未召回 {1 - recall_top_k:.2%} "
            f"vs 精排丢弃 {1 - refine_keep:.2%}）——抬天花板优先。")
    else:
        log(f"[0b]   主要损失在精排（丢弃 {1 - refine_keep:.2%} "
            f"vs 粗排未召回 {1 - recall_top_k:.2%}）。")
    sys.exit(1)
log(f"[0b] 判读：通过 —— 误识别 0，正确命中 {metrics.correct_rate:.2%}，"
    f"Recall@{TOP_K}={recall_top_k:.2%}")
sys.exit(0)
