"""两阶段检索编排（spec §8.3）。

  粗排：词汇树量化 -> 倒排索引取 Top-K
  精排：对候选逐个 ORB 匹配 + RANSAC，再由 verify.decide 做三条判定

只对 Top-K 候选从 mmap 随机读描述子，所以内存占用与图库大小无关。
"""

import numpy as np

from .descstore import DescStore
from .features import extract
from .index import InvertedIndex
from .verify import Decision, decide, verify_pair
from .vocab import Vocab

TOP_K = 20


class TwoStageRecognizer:
    def __init__(
        self,
        vocab: Vocab,
        index: InvertedIndex,
        store: DescStore,
        photo_ids: list[str],
        top_k: int = TOP_K,
    ) -> None:
        if not (len(photo_ids) == len(store) == index.n_docs):
            raise ValueError(
                f"三者数量必须一致：photo_ids={len(photo_ids)}、"
                f"store={len(store)}、index={index.n_docs}"
            )
        self._vocab = vocab
        self._index = index
        self._store = store
        self._ids = list(photo_ids)
        self._top_k = int(top_k)

    def _coarse(self, img_bgr: np.ndarray) -> list[int]:
        words = self._vocab.words_of(extract(img_bgr).desc)
        return [doc for doc, _ in self._index.query(words, self._top_k)]

    def candidates(self, img_bgr: np.ndarray) -> list[str]:
        return [self._ids[d] for d in self._coarse(img_bgr)]

    def recognize(self, img_bgr: np.ndarray) -> Decision:
        query = extract(img_bgr)
        words = self._vocab.words_of(query.desc)
        docs = [doc for doc, _ in self._index.query(words, self._top_k)]
        results = [
            verify_pair(query, self._store.read(doc), self._ids[doc]) for doc in docs
        ]
        return decide(results)
