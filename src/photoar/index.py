"""TF-IDF 倒排索引，用于两阶段检索的粗排。

不用 scipy.sparse：build() 时把倒排表压成两个扁平数组 + 偏移量，
query() 在一个 float32 累加器上做散射加法。万级文档下这已经足够快，
且零额外依赖。

评分：文档与查询都用 L2 归一化的 tf-idf 向量，分数即余弦相似度。
idf = log(n_docs / df)，df 为 0 的词权重记为 0。
"""

from collections import Counter
from pathlib import Path

import numpy as np


class InvertedIndexBuilder:
    def __init__(self, n_words: int) -> None:
        self._n_words = int(n_words)
        self._docs: list[Counter] = []

    def add(self, words: np.ndarray) -> int:
        if words.size and (int(words.min()) < 0 or int(words.max()) >= self._n_words):
            raise ValueError(
                f"词 id 超出范围 [0, {self._n_words})："
                f"min={int(words.min())} max={int(words.max())}"
            )
        self._docs.append(Counter(int(w) for w in words))
        return len(self._docs) - 1

    def build(self) -> "InvertedIndex":
        n_docs = len(self._docs)
        n_words = self._n_words

        df = np.zeros(n_words, np.int64)
        for tf in self._docs:
            for w in tf:
                df[w] += 1

        idf = np.zeros(n_words, np.float32)
        nonzero = df > 0
        if n_docs:
            idf[nonzero] = np.log(n_docs / df[nonzero]).astype(np.float32)

        # 每篇文档的 L2 归一化 tf-idf 权重
        per_word: list[list[tuple[int, float]]] = [[] for _ in range(n_words)]
        for doc_idx, tf in enumerate(self._docs):
            weights = {w: c * idf[w] for w, c in tf.items()}
            norm = float(np.sqrt(sum(v * v for v in weights.values())))
            if norm == 0.0:
                continue
            for w, v in weights.items():
                per_word[w].append((doc_idx, v / norm))

        offsets = np.zeros(n_words + 1, np.int64)
        for w in range(n_words):
            offsets[w + 1] = offsets[w] + len(per_word[w])
        total = int(offsets[-1])

        doc_ids = np.zeros(total, np.int32)
        weights_flat = np.zeros(total, np.float32)
        for w in range(n_words):
            start = int(offsets[w])
            for i, (doc_idx, weight) in enumerate(per_word[w]):
                doc_ids[start + i] = doc_idx
                weights_flat[start + i] = weight

        return InvertedIndex(n_docs, idf, offsets, doc_ids, weights_flat)


class InvertedIndex:
    def __init__(
        self,
        n_docs: int,
        idf: np.ndarray,
        offsets: np.ndarray,
        doc_ids: np.ndarray,
        weights: np.ndarray,
    ) -> None:
        self._n_docs = int(n_docs)
        self._idf = idf
        self._offsets = offsets
        self._doc_ids = doc_ids
        self._weights = weights

    @property
    def n_docs(self) -> int:
        return self._n_docs

    def query(self, words: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self._n_docs == 0 or words.size == 0 or top_k <= 0:
            return []

        qtf = Counter(int(w) for w in words)
        qw = {w: c * float(self._idf[w]) for w, c in qtf.items()}
        qnorm = float(np.sqrt(sum(v * v for v in qw.values())))
        if qnorm == 0.0:
            return []

        scores = np.zeros(self._n_docs, np.float32)
        for w, v in qw.items():
            start, end = int(self._offsets[w]), int(self._offsets[w + 1])
            if start == end:
                continue
            np.add.at(scores, self._doc_ids[start:end], self._weights[start:end] * (v / qnorm))

        k = min(top_k, self._n_docs)
        cand = np.argpartition(-scores, k - 1)[:k]
        cand = cand[np.argsort(-scores[cand], kind="stable")]
        return [(int(d), float(scores[d])) for d in cand]

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            n_docs=np.array([self._n_docs], np.int64),
            idf=self._idf,
            offsets=self._offsets,
            doc_ids=self._doc_ids,
            weights=self._weights,
        )

    @classmethod
    def load(cls, path: str | Path) -> "InvertedIndex":
        z = np.load(Path(path))
        return cls(
            n_docs=int(z["n_docs"][0]),
            idf=z["idf"],
            offsets=z["offsets"],
            doc_ids=z["doc_ids"],
            weights=z["weights"],
        )
