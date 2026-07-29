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
