"""空词表下的识别库：**结果必须与有词表时一样正确**，只是慢。

`nullvocab.py` 断言"全零词表 ⇒ 全量扫描 ⇒ 结果正确"。那是一条跨模块的推理，所以
这里不信它，直接在一个真的库上量：25 张照片、空词表、用扰动查询图去认，必须命中，
而且候选集必须**恰好是全库**（不是"碰巧命中了"）。

25 这个数是刻意的：它 **大于** `recognizer.TOP_K`(20)。库比 TOP_K 小的时候
`_candidate_slots` 本来就有一条"跳过粗排直接全查"的分支，那条分支下空词表当然能用 ——
测不出任何东西。只有 n_docs > TOP_K 时才真的走到"粗排返回空 + extra_slots 兜底"
这条路上。
"""

import numpy as np
import pytest

from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.nullvocab import NullVocab
from photoar.recognizer import TOP_K
from photoar.server.library import PhotoLibrary

N_PHOTOS = 25  # > TOP_K(20)，理由见模块 docstring


@pytest.fixture
def images(textured_image):
    return [textured_image(seed=s, w=900, h=650) for s in range(N_PHOTOS)]


@pytest.fixture
def trained_vocab(textured_image):
    """一份真词表，用来做"空词表与有词表的结果是否一致"的对照。"""
    descs = [
        F.extract(textured_image(seed=1000 + s, w=900, h=650)).desc for s in range(8)
    ]
    return V.train(np.vstack(descs), branching=6, depth=3, seed=0)


def _fill(lib, images):
    ids = []
    for i, img in enumerate(images):
        pid = f"{i:032x}"
        lib.add(pid, F.extract(img), defer_reindex=True)
        ids.append(pid)
    lib.reindex()
    return ids


def test_recognizes_with_null_vocab_on_a_25_photo_library(tmp_path, images):
    """任务书里那条"NullVocab + 25 张照片，识别仍然命中"的直接验证。

    每一张都试，不是抽一张：空词表下候选集是全库，如果 extra_slots 的顺序/内容有问题
    （比如只补进了一部分），表现会是"某些照片认得出、某些认不出"，抽一张有很大概率
    正好抽到能认出来的那些。
    """
    lib = PhotoLibrary(tmp_path / "lib", NullVocab())
    ids = _fill(lib, images)
    assert len(lib) == N_PHOTOS > TOP_K

    for i, img in enumerate(images):
        query, _ = synth.generate(img, count=1, seed=4)[0]
        d = lib.recognize(query)
        assert d.matched and d.photo_id == ids[i], f"第 {i} 张认不出：{d}"


def test_null_vocab_scans_the_whole_library(tmp_path, images):
    """行为是**全量扫描**：每一张照片都进了候选集、都跑了一次几何校验。

    这一条既是"为什么它正确"，也是"为什么它慢" —— 两件事是同一个事实的两面，
    所以钉在同一个断言上（候选数 == 库大小）。
    """
    lib = PhotoLibrary(tmp_path / "lib", NullVocab())
    _fill(lib, images)
    query, _ = synth.generate(images[3], count=1, seed=4)[0]
    results = lib.verify_candidates(query, top_k=TOP_K)
    assert len(results) == N_PHOTOS, "空词表下候选集必须是全库，否则那条推理不成立"


def test_null_vocab_matches_trained_vocab_verdict(tmp_path, images, trained_vocab):
    """同一批照片、同一张查询图，空词表与真词表给出**同一个判定**。

    这是"结果正确"最直接的形式：不是"也能认出来"，而是"认出来的是同一张"。
    """
    null_lib = PhotoLibrary(tmp_path / "null", NullVocab())
    real_lib = PhotoLibrary(tmp_path / "real", trained_vocab)
    _fill(null_lib, images)
    _fill(real_lib, images)

    for i in (0, 7, 13, N_PHOTOS - 1):
        query, _ = synth.generate(images[i], count=1, seed=9)[0]
        a = null_lib.recognize(query)
        b = real_lib.recognize(query)
        assert (a.matched, a.photo_id) == (b.matched, b.photo_id), f"第 {i} 张判定不一致"


def test_small_library_is_identical_with_or_without_vocab(tmp_path, images, trained_vocab):
    """n_docs <= TOP_K 时两者**完全等价** —— 走的是同一条"跳过粗排"的分支。

    `nullvocab.py` 里那句"空词表在小库上与有词表完全等价"的依据。
    """
    small = images[:10]
    null_lib = PhotoLibrary(tmp_path / "null", NullVocab())
    real_lib = PhotoLibrary(tmp_path / "real", trained_vocab)
    _fill(null_lib, small)
    _fill(real_lib, small)
    query, _ = synth.generate(small[2], count=1, seed=5)[0]
    a = [(r.photo_id, r.inliers) for r in null_lib.verify_candidates(query)]
    b = [(r.photo_id, r.inliers) for r in real_lib.verify_candidates(query)]
    assert a == b


def test_recog_top_k_smaller_than_TOP_K_still_finds_candidates(tmp_path, images):
    """`recog.top_k` 被调小、库又不到 TOP_K 时，候选集不能变成空的。

    这是空词表打开的一个真实的洞，也是 `_make_snapshot` 改成**无条件**算
    `extra_slots` 的原因：原来那句 `if index.n_docs > TOP_K` 会让
    `TOP_K >= n_docs > recog.top_k` 这一段区间里两个兜底分支都不成立
    （全查分支要 n_docs <= top_k，extra_slots 因为 n_docs <= TOP_K 而是空的），
    于是空词表下**每次识别都必然未命中**，而日志里是一片正常的 200。
    """
    fifteen = images[:15]  # 15 <= TOP_K(20)
    lib = PhotoLibrary(tmp_path / "lib", NullVocab())
    ids = _fill(lib, fifteen)
    query, _ = synth.generate(fifteen[6], count=1, seed=2)[0]
    results = lib.verify_candidates(query, top_k=10)  # 10 < 15 <= 20
    assert len(results) == 15
    d = lib.recognize(query, top_k=10)
    assert d.matched and d.photo_id == ids[6]


def test_null_vocab_library_survives_reload(tmp_path, images):
    """重开一次库（模拟重启）仍然能认 —— words.bin 里存的是全 0，`_read_words`
    与 `_build_index` 都得接受它。"""
    lib = PhotoLibrary(tmp_path / "lib", NullVocab())
    ids = _fill(lib, images)
    del lib
    again = PhotoLibrary(tmp_path / "lib", NullVocab())
    assert len(again) == N_PHOTOS
    query, _ = synth.generate(images[11], count=1, seed=4)[0]
    d = again.recognize(query)
    assert d.matched and d.photo_id == ids[11]
