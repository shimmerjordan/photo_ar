import numpy as np
import pytest

from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.bruteforce import BruteForceRecognizer
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TOP_K, TwoStageRecognizer


@pytest.fixture
def corpus(tmp_path, textured_image):
    """40 张合成图的完整语料：描述子库 + 词汇树 + 倒排索引。"""
    n = 40
    images = [textured_image(seed=s, w=900, h=650) for s in range(n)]
    ids = [f"p{i}" for i in range(n)]
    feats = [F.extract(img) for img in images]

    path = tmp_path / "desc.bin"
    with DescStoreWriter(path, capacity=n) as w:
        for f in feats:
            w.append(f)

    voc = V.train(np.vstack([f.desc for f in feats]), branching=6, depth=3, seed=0)
    builder = InvertedIndexBuilder(voc.n_words)
    for f in feats:
        builder.add(voc.words_of(f.desc))
    index = builder.build()

    store = DescStore(path)
    yield images, ids, voc, index, store
    store.close()


def test_recognizes_synthetic_query(corpus):
    images, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids)
    query, _ = synth.generate(images[7], count=1, seed=4)[0]
    d = rec.recognize(query)
    assert d.matched
    assert d.photo_id == "p7"


def test_rejects_photo_outside_library(corpus, textured_image):
    _, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids)
    assert not rec.recognize(textured_image(seed=98765)).matched


def test_candidates_are_capped_at_top_k(corpus):
    images, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids, top_k=5)
    assert len(rec.candidates(images[3])) <= 5


def test_coarse_stage_recalls_the_right_photo(corpus):
    """粗排召回率：Top-20 候选必须包含正确答案，否则精排再准也没用。"""
    images, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids, top_k=TOP_K)

    hits = 0
    for i in range(0, len(images), 4):
        query, _ = synth.generate(images[i], count=1, seed=100 + i)[0]
        if ids[i] in rec.candidates(query):
            hits += 1
    total = len(range(0, len(images), 4))
    assert hits >= total - 1


def test_agrees_with_bruteforce_on_matched_ids(corpus):
    """两阶段与暴力检索在"命中的是哪张"上必须一致。
    不断言两者的 matched 完全相同——粗排漏召回会让两阶段更保守，
    那是可接受的（漏检），但绝不允许指向不同的照片（误识别）。
    """
    images, ids, voc, index, store = corpus
    two = TwoStageRecognizer(voc, index, store, ids)
    brute = BruteForceRecognizer(store, ids)

    for i in range(0, len(images), 5):
        query, _ = synth.generate(images[i], count=1, seed=200 + i)[0]
        a, b = two.recognize(query), brute.recognize(query)
        if a.matched and b.matched:
            assert a.photo_id == b.photo_id


def test_blank_query_is_rejected(corpus):
    _, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids)
    assert not rec.recognize(np.full((400, 600, 3), 128, np.uint8)).matched


def test_id_count_must_match_index_and_store(corpus):
    _, ids, voc, index, store = corpus
    with pytest.raises(ValueError):
        TwoStageRecognizer(voc, index, store, ids[:-1])
