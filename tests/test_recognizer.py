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


class TestVerifyCandidates:
    """暴露原始候选分数，供 bench/threshold_scan.py 录制后离线重放阈值。

    这一段本来内联在 recognize() 里。拆出来的理由不是美观：扫描脚本需要
    每个候选的 inliers/det，如果脚本自己抄一遍粗排+精排，"扫描用的管线"就会
    和产品管线各自漂移，而扫出来的阈值是要直接写回产品的。
    """

    def test_recognize_is_exactly_decide_over_verify_candidates(self, corpus):
        """recognize() 必须等于 decide(verify_candidates())，不是"大致等于"。

        这条是重放法的地基：如果 recognize 里还藏着别的判定逻辑，录下来的
        候选分数就还原不出真实判定，整套扫描结论都不成立。
        """
        from photoar.verify import decide

        images, ids, voc, index, store = corpus
        rec = TwoStageRecognizer(voc, index, store, ids)
        for i in (0, 7, 23, 39):
            query, _ = synth.generate(images[i], count=1, seed=i + 1)[0]
            assert rec.recognize(query) == decide(rec.verify_candidates(query))

    def test_returns_at_most_top_k_scored_candidates(self, corpus):
        images, ids, voc, index, store = corpus
        rec = TwoStageRecognizer(voc, index, store, ids, top_k=5)
        query, _ = synth.generate(images[3], count=1, seed=9)[0]
        results = rec.verify_candidates(query)
        assert 0 < len(results) <= 5
        assert all(r.photo_id in ids for r in results)

    def test_candidate_scores_do_not_depend_on_the_thresholds(self, corpus):
        """inliers/det 与阈值无关——这是"录一次、任意阈值重放"能成立的另一半
        前提。改掉模块常量后重跑同一个查询，分数必须逐个字节相同，只有 ok
        这个派生字段会跟着变。
        """
        import photoar.verify as V

        images, ids, voc, index, store = corpus
        rec = TwoStageRecognizer(voc, index, store, ids)
        query, _ = synth.generate(images[11], count=1, seed=2)[0]

        before = [(r.photo_id, r.inliers, r.det) for r in rec.verify_candidates(query)]
        original = V.MIN_INLIERS
        try:
            V.MIN_INLIERS = 999
            after = [(r.photo_id, r.inliers, r.det) for r in rec.verify_candidates(query)]
        finally:
            V.MIN_INLIERS = original
        assert before == after
