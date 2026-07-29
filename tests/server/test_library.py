"""可增量图库。三件必须钉住的事：

1. 库大于 TOP_K 且无退化文档时，候选集与 Phase 0 的 `TwoStageRecognizer`
   **逐位相同** —— 否则 Phase 0 实测的那批数字（命中率、库外假阳性率、阈值）
   对服务端不成立。
2. `n_docs == 1` 时仍能识别。`idf = log(n_docs/df)` 在此时全为 0，唯一文档的
   tf-idf 范数是 0，粗排永远返回空 —— 刚部署完入库第一张照片就识别不出来。
3. 每次 add 之后旧照片仍然识别得出。idf 随 n_docs 变，只追加 postings 不重算
   权重会让粗排静默漂移（不报错，只是召回率慢慢变差）。
"""

import numpy as np
import pytest

from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TOP_K, TwoStageRecognizer
from photoar.server.library import LibraryCorrupt, PhotoLibrary


@pytest.fixture
def vocab(textured_image):
    descs = [
        F.extract(textured_image(seed=1000 + s, w=900, h=650)).desc for s in range(8)
    ]
    return V.train(np.vstack(descs), branching=6, depth=3, seed=0)


def _fill(lib, images, *, defer=False):
    ids = []
    for i, img in enumerate(images):
        pid = f"{i:032x}"
        lib.add(pid, F.extract(img), defer_reindex=defer)
        ids.append(pid)
    return ids


def test_recognizes_after_incremental_adds(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(25)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    assert len(lib) == 25

    query, _ = synth.generate(images[7], count=1, seed=4)[0]
    d = lib.recognize(query)
    assert d.matched and d.photo_id == ids[7]


def test_earliest_photo_still_recognizable_after_many_adds(tmp_path, vocab, textured_image):
    """idf 会随 n_docs 变。只追加 postings 而不重算全库权重时，最早入库的
    照片会因为权重过期而逐渐掉出粗排 Top-K —— 没有任何报错。"""
    images = [textured_image(seed=s, w=900, h=650) for s in range(30)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)

    query, _ = synth.generate(images[0], count=1, seed=1)[0]
    d = lib.recognize(query)
    assert d.matched and d.photo_id == ids[0]


def test_single_photo_library_can_recognize(tmp_path, vocab, textured_image):
    """n_docs == 1 的退化：每个词的 df 都等于 n_docs，idf 全为 0，唯一那篇
    文档的 tf-idf 范数是 0，粗排永远返回空。

    这不是理论问题 —— 它就是"刚装好服务、入库第一张照片、扫描永远不命中"。
    """
    img = textured_image(seed=3, w=900, h=650)
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    pid = "a" * 32
    lib.add(pid, F.extract(img))

    assert lib._snapshot.index.n_docs == 1
    assert list(lib._snapshot.index.unretrievable_docs()) == [0], (
        "前提变了：这张文档本应是粗排检索不到的，测试断言的意义也就变了"
    )
    query, _ = synth.generate(img, count=1, seed=9)[0]
    d = lib.recognize(query)
    assert d.matched and d.photo_id == pid


def test_small_library_verifies_every_photo(tmp_path, vocab, textured_image):
    """库比 TOP_K 小时直接全查：粗排此时什么也筛不掉，跳过它既等价又更快。"""
    images = [textured_image(seed=s, w=900, h=650) for s in range(5)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    _fill(lib, images)
    snap = lib._snapshot
    assert lib._candidate_slots(snap, F.extract(images[0]), TOP_K) == list(range(5))


def test_candidate_set_matches_phase0_recognizer(tmp_path, vocab, textured_image):
    """库大于 TOP_K 时，候选集必须与 `TwoStageRecognizer` 逐位相同。

    这是把 Phase 0 的全部实测数字（MIN_INLIERS=40 的标定、库外假阳性率）搬到
    服务端的**唯一依据**。不同就意味着那批数字对服务端不成立。
    """
    n = TOP_K + 15
    images = [textured_image(seed=s, w=900, h=650) for s in range(n)]
    feats = [F.extract(img) for img in images]
    ids = [f"{i:032x}" for i in range(n)]

    lib = PhotoLibrary(tmp_path / "lib", vocab)
    for pid, f in zip(ids, feats):
        lib.add(pid, f)

    # 同一批照片按 Phase 0 的路径独立建一套
    desc_path = tmp_path / "phase0.bin"
    with DescStoreWriter(desc_path, capacity=n) as w:
        for f in feats:
            w.append(f)
    builder = InvertedIndexBuilder(vocab.n_words)
    for f in feats:
        builder.add(vocab.words_of(f.desc))
    store = DescStore(desc_path)
    try:
        rec = TwoStageRecognizer(vocab, builder.build(), store, ids)
        snap = lib._snapshot
        assert not snap.extra_slots, (
            "这批合成图里出现了粗排检索不到的文档，那本测试比较的就不是同一件事了"
        )
        for seed in range(4):
            query, _ = synth.generate(images[seed * 3], count=1, seed=seed)[0]
            q = F.extract(query)
            mine = lib._candidate_slots(snap, q, TOP_K)
            theirs = [slot for slot, _ in rec._index.query(vocab.words_of(q.desc), TOP_K)]
            assert mine == theirs
            assert lib.recognize(query).photo_id == rec.recognize(query).photo_id
    finally:
        store.close()


def test_unretrievable_docs_are_always_candidates(tmp_path, vocab, textured_image):
    """粗排检索不到的文档必须无条件并进候选，否则它永久漏检。

    多给候选只会让 ratio 判据更保守（多一个竞争者），不会放宽判定。
    """
    n = TOP_K + 5
    images = [textured_image(seed=s, w=900, h=650) for s in range(n)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    _fill(lib, images)

    snap = lib._snapshot
    # 人为制造一个检索不到的 slot，验证它确实被并入
    faked = lib._snapshot.__class__(
        index=snap.index, store=snap.store, photo_ids=snap.photo_ids, extra_slots=(3,)
    )
    q = F.extract(images[0])
    assert 3 in lib._candidate_slots(faked, q, TOP_K)


def test_reopen_reads_persisted_state(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    ids = _fill(PhotoLibrary(tmp_path / "lib", vocab), images)

    again = PhotoLibrary(tmp_path / "lib", vocab)
    assert again.photo_ids() == ids
    query, _ = synth.generate(images[2], count=1, seed=2)[0]
    assert again.recognize(query).photo_id == ids[2]


def test_defer_reindex_then_reindex_once(tmp_path, vocab, textured_image):
    """批量入库：追加时不重建索引，最后重建一次。

    重建前新照片查不到 —— 这是刻意的可见状态，不是隐藏的不一致。
    """
    images = [textured_image(seed=s, w=900, h=650) for s in range(TOP_K + 6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images, defer=True)
    assert lib._snapshot is None or lib._snapshot.n_docs == 0
    # slots.json 必须随每次 add 增长。曾经的缺陷：add() 拿快照（defer 时刻意
    # 不更新）当基准续写，于是每次都从旧列表加一条，slots.json 停在 1 条而
    # desc.bin 长到 26 条 —— photo_id 与 slot 从此错位，识别命中后播的是别人
    # 的视频，且全程无任何报错。
    assert lib._read_slots() == ids

    lib.reindex()
    assert len(lib) == len(ids)
    query, _ = synth.generate(images[3], count=1, seed=5)[0]
    assert lib.recognize(query).photo_id == ids[3]


def test_rebuild_words_reproduces_the_same_index(tmp_path, vocab, textured_image):
    """换 vocab 用的路径：从 desc.bin 重新量化。同一个 vocab 下结果必须不变。"""
    images = [textured_image(seed=s, w=900, h=650) for s in range(8)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    before = lib.words_path.read_bytes()

    lib.reindex(rebuild_words=True)
    assert lib.words_path.read_bytes() == before
    query, _ = synth.generate(images[5], count=1, seed=3)[0]
    assert lib.recognize(query).photo_id == ids[5]


def test_truncated_words_file_refuses_to_open(tmp_path, vocab, textured_image):
    """三份记录条数不一致时拒绝启动，而不是按错位的 slot 识别。

    错位的后果是"照片 A 的特征配照片 B 的 id"——识别命中后播的是别人的视频。
    """
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    _fill(lib, images)
    raw = lib.words_path.read_bytes()
    lib.words_path.write_bytes(raw[: len(raw) // 2])

    with pytest.raises(LibraryCorrupt):
        PhotoLibrary(tmp_path / "lib", vocab)


def test_add_after_interrupted_add_refuses_instead_of_misaligning(
    tmp_path, vocab, textured_image
):
    """上一次入库在 append 到一半时断电，下一次 add 必须炸而不是接着写。

    入库要按顺序写 desc.bin → words.bin → slots.json，中间任何一步崩掉都会留下
    条数不齐的目录。此时若照常 append，新照片会占用上一次残留的那个 slot 号，
    从此每一条 photo_id 都对着别人的特征。宁可拒绝启动让人跑 reindex。
    """
    from photoar import descstore

    images = [textured_image(seed=s, w=900, h=650) for s in range(3)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    # 模拟崩在 desc.bin 写完、words.bin 未写之时
    descstore.append_slot(lib.desc_path, F.extract(textured_image(seed=99)))

    with pytest.raises(LibraryCorrupt):
        lib.add("f" * 32, F.extract(textured_image(seed=98)))
    # 也不该能重新打开
    with pytest.raises(LibraryCorrupt):
        PhotoLibrary(tmp_path / "lib", vocab)
    assert lib._read_slots() == ids, "拒绝后不得改动 slots.json"


def test_duplicate_photo_id_is_rejected(tmp_path, vocab, textured_image):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    f = F.extract(textured_image(seed=1))
    lib.add("a" * 32, f)
    with pytest.raises(ValueError):
        lib.add("a" * 32, f)


def test_empty_library_recognizes_nothing(tmp_path, vocab, textured_image):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    assert len(lib) == 0
    assert not lib.recognize(textured_image(seed=1)).matched


def test_features_roundtrip(tmp_path, vocab, textured_image):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    f = F.extract(textured_image(seed=2))
    lib.add("b" * 32, f)
    got = lib.features_of("b" * 32)
    assert got is not None
    assert np.array_equal(got.desc, f.desc[: len(got)])


# ---- 近重复闸门 ----


def test_near_duplicate_is_flagged(tmp_path, vocab, textured_image):
    """同一张图重新编码后再入库 → 必须报冲突。

    Phase 0 的第一条硬结论：两份都入库会互相判 ambiguous，**两份都永久漏检**，
    而用户看到的现象是"识别器坏了"。
    """
    import cv2

    from photoar import dedup

    img = textured_image(seed=11, w=900, h=650)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    assert ok
    recoded = cv2.imdecode(buf, cv2.IMREAD_COLOR)

    lib = PhotoLibrary(tmp_path / "lib", vocab)
    f0 = F.extract(img)
    s0 = dedup.self_score(f0, [F.extract(q) for q, _ in synth.generate(img, 6, seed=0)])
    lib.add("a" * 32, f0)

    f1 = F.extract(recoded)
    s1 = dedup.self_score(
        f1, [F.extract(q) for q, _ in synth.generate(recoded, 6, seed=0)]
    )
    conflicts = lib.conflicts(f1, s1, {"a" * 32: s0})
    assert [c.photo_id for c in conflicts] == ["a" * 32]


def test_distinct_photos_are_not_flagged(tmp_path, vocab, textured_image):
    from photoar import dedup

    lib = PhotoLibrary(tmp_path / "lib", vocab)
    scores = {}
    for i in range(6):
        img = textured_image(seed=200 + i, w=900, h=650)
        f = F.extract(img)
        pid = f"{i:032x}"
        scores[pid] = dedup.self_score(
            f, [F.extract(q) for q, _ in synth.generate(img, 6, seed=0)]
        )
        lib.add(pid, f)

    new = textured_image(seed=999, w=900, h=650)
    fn = F.extract(new)
    sn = dedup.self_score(
        fn, [F.extract(q) for q, _ in synth.generate(new, 6, seed=0)]
    )
    assert lib.conflicts(fn, sn, scores) == []


def test_missing_self_score_is_treated_as_conflict_prone(tmp_path, vocab, textured_image):
    """catalog 里查不到某张的自匹配分时按 0 处理 —— 宁可多报一次冲突让用户
    确认，也不要因为缺一个数就放行一张会导致双双漏检的近重复。"""
    img = textured_image(seed=11, w=900, h=650)
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    f = F.extract(img)
    lib.add("a" * 32, f)
    assert lib.conflicts(f, 0, {}) != []


def test_conflicts_on_empty_library(tmp_path, vocab, textured_image):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    assert lib.conflicts(F.extract(textured_image(seed=1)), 50, {}) == []
