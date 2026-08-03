"""空词表本身的性质，以及它在 `InvertedIndex` 上的后果。

这一整份测试的存在理由是 `nullvocab.py` 模块 docstring 里那条六步推理 ——
"全零词表 ⇒ 每篇文档都检索不到 ⇒ 候选集退化成全库扫描"。那条推理跨了三个模块
（nullvocab / index / library），任何一环变了它就不成立，而不成立的表现是**识别
永远未命中**且日志里全是正常的 200。所以每一步都单独钉住，不只测最终行为。
"""

import numpy as np
import pytest

from photoar.index import InvertedIndexBuilder
from photoar.nullvocab import NullVocab, NullVocabNotPersistable


@pytest.fixture
def voc():
    return NullVocab()


def test_n_words_is_one_not_zero(voc):
    """必须是 1：0 会让词 id 0 越出 `InvertedIndex` 的范围校验，每次识别 500。"""
    assert voc.n_words == 1


def test_words_of_length_tracks_descriptor_count(voc):
    """长度必须等于描述子条数 —— `library._encode_words` 拿它当那条记录的 count。"""
    for n in (0, 1, 7, 300, 512):
        words = voc.words_of(np.zeros((n, 32), np.uint8))
        assert words.shape == (n,)
        assert words.dtype == np.int32
        assert not words.any()  # 全 0


def test_words_of_works_for_both_backends_descriptor_dtypes(voc):
    """对 ORB 的 uint8 与 XFeat 的 float32 都要能用（它是两个后端共用的占位词表）。"""
    assert voc.words_of(np.zeros((300, 32), np.uint8)).shape == (300,)
    assert voc.words_of(np.zeros((512, 64), np.float32)).shape == (512,)


def test_save_is_refused(voc, tmp_path):
    """存盘之后下次启动会把它当成一份真词表，"该不该提醒用户训词表"的判据就失效了。"""
    with pytest.raises(NullVocabNotPersistable):
        voc.save(tmp_path / "vocab.npz")
    assert not (tmp_path / "vocab.npz").exists()


# ---- 与 InvertedIndex 的交互：那条推理的每一步 ----


def _index_of(n_docs: int, voc: NullVocab, feats: int = 300):
    b = InvertedIndexBuilder(voc.n_words)
    for _ in range(n_docs):
        b.add(voc.words_of(np.zeros((feats, 32), np.uint8)))
    return b.build()


@pytest.mark.parametrize("n_docs", [1, 2, 25, 60])
def test_idf_is_zero_because_df_equals_n_docs(n_docs, voc):
    """第 2 步：唯一那个词的 df 恒等于 n_docs，于是 idf = log(1) = 0。"""
    idx = _index_of(n_docs, voc)
    assert idx.n_docs == n_docs
    assert idx.idf.shape == (1,)
    assert float(idx.idf[0]) == 0.0


@pytest.mark.parametrize("n_docs", [1, 2, 25, 60])
def test_every_document_is_unretrievable(n_docs, voc):
    """第 3、4 步：tf-idf 范数为 0 ⇒ 一条 posting 都没有 ⇒ 全部文档"检索不到"。

    这是整条推理的枢纽：`PhotoLibrary` 正是靠这份名单把候选集补成全库的。
    """
    idx = _index_of(n_docs, voc)
    assert idx.unretrievable_docs() == list(range(n_docs))


@pytest.mark.parametrize("n_docs", [1, 25, 60])
def test_query_returns_nothing(n_docs, voc):
    """第 6 步：查询侧也是空的（查询向量的范数为 0）。

    所以"候选集 = 空的粗排结果 ∪ 全部文档 = 全部文档"，不是"粗排的 20 个 + 全部"。
    """
    idx = _index_of(n_docs, voc)
    words = voc.words_of(np.zeros((300, 32), np.uint8))
    assert idx.query(words, 20) == []


def test_word_id_stays_in_range(voc):
    """词 id 0 必须通得过 `InvertedIndex` 的范围校验（n_words == 1 时 0 < 1）。

    单独一条，因为这正是"把 n_words 写成 0 会怎样"的分界线。
    """
    idx = _index_of(3, voc)
    idx.query(voc.words_of(np.zeros((10, 32), np.uint8)), 20)  # 不抛就算过
