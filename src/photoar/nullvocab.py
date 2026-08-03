"""没有词表时用的占位词表。**它让"全新部署"成为一个合法状态。**

词表是用**用户自己的照片**离线训出来的（`photoar build` / `photoar-server
build-vocab`），所以第一次 `docker compose up` 的那一刻它必然还不存在：库是空的，
没有描述子可训。此前 `ServerConfig.from_dict` 强制要求 `vocab_path`、
`Server.create` 又要求那个文件存在，于是一键部署根本起不来 —— 而用户在服务起来
之前也没有任何办法把照片入库来训词表。这是一个真正的死锁，不是配置麻烦。

## 为什么"全零词表"就等于"全量扫描"，而不是"检索不到"

这条推理是本模块存在的全部依据，所以完整写下来（`tests/test_nullvocab.py` 与
`tests/server/test_library_nullvocab.py` 把每一步都钉住了）：

1. `words_of` 恒返回全 0，`n_words == 1`，所以**每一篇**文档的词序列都是 `[0]*k`。
2. `InvertedIndexBuilder.build()` 里 `df[0] == n_docs`，于是
   `idf[0] = log(n_docs/df[0]) = log(1) = 0`。
3. 每篇文档的 tf-idf 权重全为 0，`norm == 0.0`，于是那一篇被整体 `continue`
   跳过 —— 倒排表里**一条 posting 都没有**。
4. 因此 `InvertedIndex.unretrievable_docs()` 返回**全部** doc 下标（它的判据正是
   "从未出现在任何 postings 里"）。
5. `PhotoLibrary._make_snapshot` 把这份名单存进 `_Snapshot.extra_slots`，而
   `_candidate_slots` 会把它**无条件**并进候选集。
6. 查询侧 `index.query()` 也返回空（查询向量的 `qnorm == 0`），所以候选集恰好
   就是 extra_slots = 全库。

也就是说行为是**全量扫描**：每个候选照常走一次几何校验，判定逻辑一个字都没变，
**结果正确**。代价是 O(库大小) —— 库大了每次识别都要跑 N 次 RANSAC。

而且在小库上它与有词表**完全等价**：`_candidate_slots` 本来就有一条
`n_docs <= top_k` 时跳过粗排直接全查的分支（理由见 `library.py` 的"小库与检索不到
的文档"那节），此时两条路走到同一个候选集。

## 为什么不用"给 idf 加平滑"或"让 n_words 大一点"来绕开

- 加平滑会改变 Phase 0 实测过的排序语义（命中 95.70% / 真实误识别 0.000% 那组
  数字是在 `idf = log(n_docs/df)` 上量出来的），为了一个临时状态动它不值得。
- 让 `words_of` 返回随机词 id 看起来"更像一个词表"，实际更糟：那样倒排表里会有
  postings，`unretrievable_docs()` 变成空，粗排会**真的筛掉**候选 —— 按随机词筛，
  等于随机漏检。而它不会报错，只表现为识别率莫名其妙地低。宁可慢，不可错。

## save 为什么必须拒绝

`NullVocab` 不是一份可持久化的词表，它是"词表还不存在"这件事的表示。允许它写盘
的后果是磁盘上出现一个 `vocab.npz`，下次启动被当成一份**真**词表加载 —— 从此
`unretrievable_docs()` 由文件内容决定（一棵只有一个词的树），而"要不要提醒用户去
训词表"的判据（文件在不在）永久失效。用户会看到一个"已经有词表了"的部署，却始终
是全量扫描的性能。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

# 全零词序列里那个唯一的词 id。写成常量是为了让测试能引用它而不是复述一个 0。
ONLY_WORD = 0


class NullVocabNotPersistable(RuntimeError):
    """有人试图把 `NullVocab` 存盘。理由见模块 docstring 最后一节。"""


class NullVocab:
    """`backend.VocabLike` 的一个实现：任何描述子都量化成同一个词。

    刻意不继承 `vocab.Vocab` / `floatvocab.FloatVocab`：它对**两种**后端都要能用
    （ORB 的 uint8 描述子与 XFeat 的 float32 描述子），而那两个类的内部表示互不
    兼容。它靠 `Backend.vocab_cls` 里那个元组被放行，见 `backend.py`。
    """

    @property
    def n_words(self) -> int:
        # 必须是 1 而不是 0：`InvertedIndexBuilder` 会建一个长度 n_words 的 idf
        # 数组，而 `InvertedIndex.query` 用 `words.max() >= n_words` 校验词 id。
        # n_words==0 时词 id 0 就越界了，表现是每次识别抛 ValueError→500。
        return 1

    def words_of(self, desc: np.ndarray) -> np.ndarray:
        """恒返回长度等于描述子条数的全 0 数组。

        长度必须跟着描述子条数走，不能返回一个固定长度或空数组：
        `library._encode_words` 拿 `words.size` 当那一条记录的 count 写进
        `words.bin`，而 `words.bin` 是 `reindex()` 重建倒排索引的唯一输入。返回空
        数组的话每篇文档的词序列都是空的 —— `df` 全 0、`idf` 全 0，看起来结果一样，
        但那是"这张照片没有任何词"，与"这张照片的词都没有区分度"是两件事：前者在
        以后换成真词表 `reindex(rebuild_words=True)` 之前，连 `n_docs` 之外的任何
        信息都没有留下。

        dtype 用 int32，与 `vocab.Vocab.words_of` / `floatvocab.FloatVocab.words_of`
        一致 —— `_encode_words` 会转成 uint32，但混用 dtype 迟早在别处冒出来。
        """
        n = 0 if desc is None else int(np.asarray(desc).shape[0])
        return np.full(n, ONLY_WORD, np.int32)

    def save(self, path: str | Path) -> None:
        raise NullVocabNotPersistable(
            f"拒绝把空词表写到 {path}：它表示的是「词表还不存在」，"
            f"存盘之后下次启动会把它当成一份真词表加载，"
            f"「该提醒用户去训词表」的判据（文件在不在）就永久失效了。"
            f"要一份真词表用 `photoar-server build-vocab`。"
        )

    def __repr__(self) -> str:  # 日志里要一眼看出跑的是空词表
        return "NullVocab(n_words=1, 全量扫描)"
