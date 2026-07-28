"""暴力检索：对全库逐个做几何校验。

存在的意义不是上生产（O(N) 太慢），而是提供一个**不含词汇表变量**的
参考基线。它测出的误识别率就是几何校验本身的判别力上限；BoW 粗排的
召回率也以它的结果为 ground truth。
"""

import numpy as np

from .descstore import DescStore
from .features import extract
from .verify import Decision, decide, verify_pair


class BruteForceRecognizer:
    def __init__(self, store: DescStore, photo_ids: list[str]) -> None:
        if len(photo_ids) != len(store):
            raise ValueError(
                f"photo_ids 数量 {len(photo_ids)} 与描述子库 slot 数 {len(store)} 不一致"
            )
        self._store = store
        self._ids = list(photo_ids)

    def recognize(self, img_bgr: np.ndarray) -> Decision:
        query = extract(img_bgr)
        results = [
            verify_pair(query, self._store.read(slot), pid)
            for slot, pid in enumerate(self._ids)
        ]
        return decide(results)
