import numpy as np
import pytest

from photoar.index import InvertedIndex, InvertedIndexBuilder


def _build(docs, n_words=50):
    b = InvertedIndexBuilder(n_words)
    for d in docs:
        b.add(np.asarray(d, np.int32))
    return b.build()


def test_add_returns_sequential_doc_indices():
    b = InvertedIndexBuilder(10)
    assert b.add(np.array([1, 2], np.int32)) == 0
    assert b.add(np.array([3], np.int32)) == 1


def test_query_ranks_exact_match_first():
    idx = _build([[1, 2, 3], [4, 5, 6], [1, 2, 7]])
    top = idx.query(np.array([1, 2, 3], np.int32), top_k=3)
    assert top[0][0] == 0
    assert top[0][1] > 0


def test_query_respects_top_k():
    idx = _build([[1], [2], [3], [4], [5]])
    assert len(idx.query(np.array([1], np.int32), top_k=2)) == 2


def test_query_top_k_larger_than_corpus():
    idx = _build([[1], [2]])
    assert len(idx.query(np.array([1], np.int32), top_k=10)) == 2


def test_scores_are_descending():
    idx = _build([[1, 2, 3], [1, 2, 9], [1, 8, 9], [7, 8, 9]])
    scores = [s for _, s in idx.query(np.array([1, 2, 3], np.int32), top_k=4)]
    assert scores == sorted(scores, reverse=True)


def test_idf_downweights_ubiquitous_words():
    """词 0 出现在所有文档里，应几乎不贡献区分度；
    只共享词 0 的文档不应排在共享稀有词 5 的文档之前。
    """
    idx = _build([[0, 5], [0, 6], [0, 7], [0, 8]])
    top = idx.query(np.array([0, 5], np.int32), top_k=4)
    assert top[0][0] == 0


def test_query_on_empty_words_returns_empty():
    idx = _build([[1, 2]])
    assert idx.query(np.zeros((0,), np.int32), top_k=5) == []


def test_empty_corpus_query_returns_empty():
    idx = InvertedIndexBuilder(10).build()
    assert idx.n_docs == 0
    assert idx.query(np.array([1], np.int32), top_k=5) == []


def test_word_out_of_range_rejected():
    b = InvertedIndexBuilder(5)
    with pytest.raises(ValueError):
        b.add(np.array([7], np.int32))


def test_save_load_roundtrip(tmp_path):
    idx = _build([[1, 2, 3], [2, 3, 4], [5, 6, 7]])
    path = tmp_path / "idx.npz"
    idx.save(path)
    loaded = InvertedIndex.load(path)
    q = np.array([1, 2, 3], np.int32)
    assert loaded.n_docs == idx.n_docs
    assert loaded.query(q, top_k=3) == idx.query(q, top_k=3)


def test_recall_at_k_on_many_docs():
    """1000 篇文档，查询取自其中一篇并扰动 20% 的词，Top-20 必须包含它。
    这是粗排召回率的最小保证；不满足则两阶段检索的第一阶段就是瓶颈。
    """
    rng = np.random.default_rng(0)
    docs = [rng.integers(0, 500, 60).astype(np.int32) for _ in range(1000)]
    idx = _build(docs, n_words=500)

    hits = 0
    for target in range(0, 1000, 50):
        q = docs[target].copy()
        mutate = rng.choice(len(q), size=len(q) // 5, replace=False)
        q[mutate] = rng.integers(0, 500, len(mutate))
        if target in [d for d, _ in idx.query(q, top_k=20)]:
            hits += 1
    assert hits >= 18  # 20 次里至少 18 次


def test_ties_break_by_ascending_doc_index():
    """当多篇文档对同一 query 得分完全相同时，返回顺序必须按 doc_index 升序排列。

    np.argpartition 用的是 introselect，不保证保留原始下标顺序；如果只在其后
    接一个 argsort(kind="stable")，"稳定"针对的是 argpartition 输出的候选数组
    本身（其内部顺序已不确定），并不是原始文档下标——即使分数完全相同，也可能
    返回下标乱序的结果。用这里完全相同的构造，在只有 argsort(stable) 而没有
    按 (-score, doc_index) 做 lexsort 的旧实现下跑，返回的是
    [0, 4, 12, 10, 24]（10 排在 12 之后，不是升序）；对随机同分数组的 2000 次
    试验里，有 1550 次违反了升序保证。修复后必须显式按 (-score, doc_index)
    lexsort，不能只依赖 argpartition + 稳定排序。
    """
    tied_words = np.array([1, 2, 3], np.int32)
    tied_positions = {0, 4, 10, 12, 16, 20, 24, 28}
    n_total = 30

    b = InvertedIndexBuilder(50)
    for i in range(n_total):
        if i in tied_positions:
            b.add(tied_words.copy())
        else:
            b.add(np.array([40 + (i % 5)], np.int32))
    idx = b.build()

    top_k = 5  # 切入 8 篇同分文档中间，容不下全部
    result = idx.query(tied_words, top_k=top_k)

    docs = [d for d, _ in result]
    scores = [s for _, s in result]
    assert len(docs) == top_k
    assert all(d in tied_positions for d in docs)
    assert max(scores) - min(scores) < 1e-6  # 确认这些文档确实同分
    assert docs == sorted(docs)


def test_query_rejects_positive_out_of_range_word():
    idx = _build([[1, 2, 3]], n_words=10)
    with pytest.raises(ValueError):
        idx.query(np.array([999], np.int32), top_k=5)


def test_idf_property_matches_ubiquitous_word_zero():
    """I3：idf 属性要暴露给调用方（corpus._verify_self_query）用来判断
    "这篇文档是不是全部由 ubiquitous 词组成"——词 0 出现在全部 3 篇文档里
    （df == n_docs），idf 必须恰好是 0。"""
    idx = _build([[0, 1], [0, 2], [0, 3]], n_words=10)
    assert idx.idf[0] == 0.0
    assert idx.idf[1] > 0.0


def test_unretrievable_docs_reports_docs_dropped_from_postings():
    """I3：一篇文档如果全部的词都是 ubiquitous 词（df == n_docs），build()
    算出的 tf-idf 范数是 0，会被整体排除在倒排表之外——unretrievable_docs()
    必须能报出这类文档，而不是让它们在 n_docs 里悄悄消失。"""
    # doc 0 只有词 0（3 篇文档共有，df=3=n_docs，idf=0，范数 0，被跳过）
    # doc 1、doc 2 各自还有一个非共享词，不会被跳过
    idx = _build([[0], [0, 1], [0, 2]], n_words=10)
    assert idx.unretrievable_docs() == [0]


def test_unretrievable_docs_empty_when_every_doc_has_a_distinguishing_word():
    idx = _build([[0, 1], [0, 2], [0, 3]], n_words=10)
    assert idx.unretrievable_docs() == []


def test_query_rejects_negative_word_id():
    """负数词 id 若不校验，numpy 会用负索引悄悄绕过：self._idf[w] 在 w=-1 时
    取到最后一个词的 idf；切片端点 self._offsets[w] 与 self._offsets[w + 1]
    分别回绕到总长度（offsets[-1]）和 0（offsets[0]），形成 start > end 的
    空切片——不会给任何文档加分，但幽灵词的 idf 权重仍计入查询向量的归一化
    分母 qnorm，导致所有真实文档的分数被静默压低，且不抛出任何异常。
    实测（10 词词表，文档 0 含词 1,2,3,9）：查询 [1,2,3] 时文档 0 得分
    0.9258；查询 [1,2,3,-1] 时同一文档得分被压低到 0.8571，排名不变但分数
    失真，且没有任何异常提示这是非法输入。
    """
    idx = _build([[1, 2, 3, 9], [4, 5, 6], [1, 2, 7], [8, 9]], n_words=10)
    with pytest.raises(ValueError):
        idx.query(np.array([1, 2, 3, -1], np.int32), top_k=3)
