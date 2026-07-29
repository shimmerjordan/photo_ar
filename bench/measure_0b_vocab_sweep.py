"""0b 附加实验：词表粒度对粗排召回率的影响（1000 张库）。

动机：0b 主测在 vocab 默认参数（branching=10 depth=4，1 万词）下得到
Recall@20 = 96.00%，而端到端 94.80% 受这个天花板所限。早先测得合成查询词
落在源图词表里的比例：粗词表（216 词）0.743，默认词表 0.269。所以「调粗词表」
是被预测过的那个旋钮，本脚本只取数据，不改任何默认值。

只测粗排召回率，不跑端到端——召回率就是天花板，先看天花板能不能抬起来。
"""

import pathlib
import tempfile
import time

import cv2
import numpy as np

from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TwoStageRecognizer

LIB_SIZE = 1000
EVAL_REFS = 50
SAMPLES_PER_REF = 10
SEED = 1
TRAIN_DESC_CAP = 120_000

# (branching, depth) —— 默认是 (10, 4)
CONFIGS = [(10, 4), (12, 4), (16, 4), (10, 5)]


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


t0 = time.perf_counter()
ids = [f"p{i}" for i in range(LIB_SIZE)]
imgs = {ids[i]: make(i) for i in range(LIB_SIZE)}
feats = [F.extract(imgs[k]) for k in ids]
log(f"[sweep] {LIB_SIZE} 张图 + ORB 完成 {time.perf_counter() - t0:.1f}s")

d = pathlib.Path(tempfile.mkdtemp(prefix="photoar-sweep-"))
with DescStoreWriter(d / "desc.bin", capacity=LIB_SIZE) as w:
    for f in feats:
        w.append(f)

all_desc = np.vstack([f.desc for f in feats])
sub = np.random.default_rng(SEED).choice(
    all_desc.shape[0], size=min(TRAIN_DESC_CAP, all_desc.shape[0]), replace=False
)
train_desc = all_desc[np.sort(sub)]

# 查询图预先生成一次，各配置共用同一批，保证可比
queries = []
for i, pid in enumerate(ids[:EVAL_REFS]):
    for q, _ in synth.generate(imgs[pid], SAMPLES_PER_REF, seed=500 + i):
        queries.append((pid, q))
log(f"[sweep] 预生成 {len(queries)} 个查询图")

log("")
log(f"{'branching':>10} {'depth':>6} {'词数':>7} {'训练s':>7} {'索引s':>7} "
    f"{'R@1':>7} {'R@5':>7} {'R@10':>7} {'R@20':>7} {'探测s':>7}")

with DescStore(d / "desc.bin") as store:
    for br, dep in CONFIGS:
        t = time.perf_counter()
        voc = V.train(train_desc, branching=br, depth=dep, seed=SEED)
        t_train = time.perf_counter() - t

        t = time.perf_counter()
        b = InvertedIndexBuilder(voc.n_words)
        for f in feats:
            b.add(voc.words_of(f.desc))
        index = b.build()
        t_index = time.perf_counter() - t

        rec = TwoStageRecognizer(voc, index, store, ids)
        t = time.perf_counter()
        hits = {1: 0, 5: 0, 10: 0, 20: 0}
        for pid, q in queries:
            cands = rec.candidates(q)
            for k in hits:
                if pid in cands[:k]:
                    hits[k] += 1
        t_probe = time.perf_counter() - t

        n = len(queries)
        marker = "  <- 默认" if (br, dep) == (V.BRANCHING, V.DEPTH) else ""
        log(f"{br:>10} {dep:>6} {voc.n_words:>7} {t_train:>7.1f} {t_index:>7.1f} "
            f"{hits[1]/n:>6.2%} {hits[5]/n:>6.2%} {hits[10]/n:>6.2%} "
            f"{hits[20]/n:>6.2%} {t_probe:>7.1f}{marker}")

log("")
log(f"[sweep] 全脚本耗时 {time.perf_counter() - t0:.1f}s")
