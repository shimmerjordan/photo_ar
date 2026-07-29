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
        # Minor #16：这里没有调用 self._coarse(img_bgr) 再另外拿 query，
        # 而是重复了 _coarse 里 extract+words_of+index.query 这三行——是
        # 故意的，不是没注意到重复。recognize() 精排阶段（下面的
        # verify_pair）需要 query 这个 Features 对象本身（关键点+描述子），
        # _coarse() 只返回 candidates 的 doc 下标列表，不保留 query。如果
        # 这里改成先调 self._coarse(img_bgr) 拿候选、再为了拿 query 单独
        # 调一次 extract(img_bgr)，就会在识别热路径上把最贵的一步
        # （ORB 特征提取，见 features.extract）跑两遍。DRY clean-up 的
        # 直觉在这里是错的，动之前先看这条注释。
        query = extract(img_bgr)
        words = self._vocab.words_of(query.desc)
        docs = [doc for doc, _ in self._index.query(words, self._top_k)]
        results = [
            verify_pair(query, self._store.read(doc), self._ids[doc]) for doc in docs
        ]
        return decide(results)
