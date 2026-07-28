import numpy as np
import pytest

from photoar import features as F
from photoar.descstore import SLOT_STRIDE, DescStore, DescStoreWriter


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
    big = F.Features(
        pts=np.zeros((n, 2), np.float32),
        desc=np.zeros((n, 32), np.uint8),
    )
    path = tmp_path / "big.bin"
    with DescStoreWriter(path, capacity=2) as w:
        w.append(big)
        w.append(F.Features(np.ones((1, 2), np.float32), np.ones((1, 32), np.uint8)))
    with DescStore(path) as store:
        assert len(store.read(0)) == F.N_FEATURES
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


def test_read_out_of_range_raises(tmp_path):
    path = tmp_path / "oob.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)))
    with DescStore(path) as store:
        with pytest.raises(IndexError):
            store.read(1)
        with pytest.raises(IndexError):
            store.read(-1)
