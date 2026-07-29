import numpy as np
import pytest

from photoar import corpus as C
from photoar import features as F
from photoar.descstore import (
    IncompleteWrite,
    SLOT_STRIDE,
    DescStore,
    DescStoreWriter,
    truncate_count,
)


def test_slot_stride_matches_documented_budget():
    # 4 字节计数 + 4 字节对齐填充 + 300*2*float32 + 300*32
    assert SLOT_STRIDE == 8 + F.N_FEATURES * 2 * 4 + F.N_FEATURES * F.DESC_BYTES
    assert SLOT_STRIDE == 12008
    # spec §6 写的 9600 字节/张漏算了关键点坐标，实际约 12KB/张
    assert SLOT_STRIDE * 10_000 < 130 * 1024 * 1024


def test_roundtrip_preserves_features(tmp_path, textured_image):
    path = tmp_path / "desc.bin"
    originals = [F.extract(textured_image(seed=s)) for s in range(3)]

    with DescStoreWriter(path, capacity=3) as w:
        slots = [w.append(f) for f in originals]
    assert slots == [0, 1, 2]

    with DescStore(path) as store:
        assert len(store) == 3
        for slot, orig in zip(slots, originals):
            got = store.read(slot)
            assert np.array_equal(got.desc, orig.desc)
            assert np.allclose(got.pts, orig.pts)


def test_handles_fewer_than_n_features(tmp_path):
    few = F.Features(
        pts=np.array([[1.0, 2.0], [3.0, 4.0]], np.float32),
        desc=np.arange(64, dtype=np.uint8).reshape(2, 32),
    )
    path = tmp_path / "few.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(few)
    with DescStore(path) as store:
        got = store.read(0)
        assert len(got) == 2
        assert np.array_equal(got.desc, few.desc)
        assert np.allclose(got.pts, few.pts)


def test_handles_empty_features(tmp_path):
    empty = F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8))
    path = tmp_path / "empty.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(empty)
    with DescStore(path) as store:
        assert len(store.read(0)) == 0


def test_truncates_when_over_capacity_features(tmp_path):
    """超过 N_FEATURES 的输入被截断而不是越界写坏邻居 slot。"""
    n = F.N_FEATURES + 17
    # 逐行取不同的值（而非全零），这样才能断言保留的确实是前 N_FEATURES 行，
    # 而不是随便某 N_FEATURES 行零值——全零输入下两者无法区分。
    big = F.Features(
        pts=np.arange(n * 2, dtype=np.float32).reshape(n, 2),
        desc=(np.arange(n * 32, dtype=np.int64) % 256).astype(np.uint8).reshape(n, 32),
    )
    path = tmp_path / "big.bin"
    with DescStoreWriter(path, capacity=2) as w:
        w.append(big)
        w.append(F.Features(np.ones((1, 2), np.float32), np.ones((1, 32), np.uint8)))
    with DescStore(path) as store:
        got = store.read(0)
        assert len(got) == F.N_FEATURES
        assert np.allclose(got.pts, big.pts[: F.N_FEATURES])
        assert np.array_equal(got.desc, big.desc[: F.N_FEATURES])
        second = store.read(1)
        assert len(second) == 1
        assert np.array_equal(second.desc, np.ones((1, 32), np.uint8))


def test_append_beyond_capacity_raises(tmp_path):
    path = tmp_path / "cap.bin"
    empty = F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8))
    with DescStoreWriter(path, capacity=1) as w:
        w.append(empty)
        with pytest.raises(IndexError):
            w.append(empty)


def test_incomplete_write_raises_on_clean_exit(tmp_path):
    """capacity=5 只 append 1 次就正常退出 with——半途结束的写入必须当场报错。"""
    path = tmp_path / "incomplete.bin"
    with pytest.raises(IncompleteWrite):
        with DescStoreWriter(path, capacity=5) as w:
            w.append(F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)))


def test_incomplete_write_does_not_mask_inflight_exception(tmp_path):
    """最关键的一条：with 块内已经在抛异常时，__exit__ 不能用 IncompleteWrite 把它顶掉。

    否则调用方看到的永远是"没写完"，而看不到真正导致中途失败的原因
    （这里模拟的是一次崩溃/跳过导致的提前退出）。
    """

    class BoomError(Exception):
        pass

    path = tmp_path / "boom.bin"
    with pytest.raises(BoomError):
        with DescStoreWriter(path, capacity=5) as w:
            w.append(F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)))
            raise BoomError("mid-loop failure")


def test_incomplete_write_error_path_leaves_file_readable(tmp_path):
    """守卫报错的路径不能损坏或截断文件：大小仍是 capacity*SLOT_STRIDE，slot 0 仍可读。"""
    path = tmp_path / "boom2.bin"
    first = F.Features(
        pts=np.array([[1.0, 2.0]], np.float32),
        desc=np.ones((1, 32), np.uint8),
    )
    with pytest.raises(RuntimeError):
        with DescStoreWriter(path, capacity=5) as w:
            w.append(first)

    assert path.stat().st_size == 5 * SLOT_STRIDE
    with DescStore(path) as store:
        assert len(store) == 5
        got = store.read(0)
        assert np.array_equal(got.desc, first.desc)
        assert np.allclose(got.pts, first.pts)


# ---------------------------------------------------------------------------
# Minor #23（最终审阅追加）：min(count, N_FEATURES) 这条截断规则原来在
# DescStoreWriter.append 和 corpus._desc_fingerprint 里各自独立写了一份。
# 两处一旦分叉，fingerprint 校验就会系统性地误判——现在共用
# descstore.truncate_count。
# ---------------------------------------------------------------------------


def test_truncate_count_caps_at_n_features():
    assert truncate_count(F.N_FEATURES - 1) == F.N_FEATURES - 1
    assert truncate_count(F.N_FEATURES) == F.N_FEATURES
    assert truncate_count(F.N_FEATURES + 100) == F.N_FEATURES
    assert truncate_count(0) == 0


def test_corpus_desc_fingerprint_uses_the_same_truncation_as_descstore_writer(tmp_path):
    """corpus._desc_fingerprint 必须和 DescStoreWriter.append 实际写入的
    字节完全一致——两者现在共用同一个 truncate_count，这里直接构造一份
    超过 N_FEATURES 的描述子，验证指纹确实是对截断后的内容算的，而不是
    对全量内容算的。"""
    n = F.N_FEATURES + 17
    desc = (np.arange(n * F.DESC_BYTES, dtype=np.int64) % 256).astype(np.uint8).reshape(
        n, F.DESC_BYTES
    )
    fp = C._desc_fingerprint(desc)

    path = tmp_path / "one.bin"
    features = F.Features(
        pts=np.zeros((n, 2), np.float32), desc=desc,
    )
    with DescStoreWriter(path, capacity=1) as w:
        w.append(features)
    with DescStore(path) as store:
        actual = store.read(0)
        assert len(actual) == F.N_FEATURES  # 确认真的被截断了
        import hashlib
        written_fp = hashlib.sha256(
            np.ascontiguousarray(actual.desc, np.uint8).tobytes()
        ).hexdigest()
    assert fp == written_fp


def test_read_out_of_range_raises(tmp_path):
    path = tmp_path / "oob.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)))
    with DescStore(path) as store:
        with pytest.raises(IndexError):
            store.read(1)
        with pytest.raises(IndexError):
            store.read(-1)
