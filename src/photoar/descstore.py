"""定长 slot 的描述子/关键点存储，用 mmap 随机读。

每张照片占固定 SLOT_STRIDE 字节，slot 下标即偏移，因此精排阶段只需
按 Top-K 的下标随机读 K 个 slot，无需把全库描述子常驻内存。

slot 布局（小端）：
  offset 0   uint32  count      实际特征数（<= N_FEATURES）
  offset 4   uint32  _pad       对齐填充，保证 float32 数组 8 字节对齐
  offset 8   float32[N_FEATURES*2]  关键点 xy
  offset ..  uint8[N_FEATURES*32]   描述子

spec §6 给的 9600 字节/张只算了描述子，漏了 RANSAC 必需的关键点坐标。
实际每张 12008 字节，1 万张约 120MB（仍在预算内）。
"""

from pathlib import Path

import numpy as np

from .features import DESC_BYTES, N_FEATURES, Features

_HEADER_BYTES = 8
_PTS_BYTES = N_FEATURES * 2 * 4
_DESC_BYTES_TOTAL = N_FEATURES * DESC_BYTES
SLOT_STRIDE = _HEADER_BYTES + _PTS_BYTES + _DESC_BYTES_TOTAL

_PTS_OFFSET = _HEADER_BYTES
_DESC_OFFSET = _HEADER_BYTES + _PTS_BYTES


class IncompleteWrite(RuntimeError):
    """写入的 slot 数少于声明的 capacity。

    未写过的 slot 读出来是 count=0，与"这张照片确实零特征"无法区分，
    所以半途结束的写入必须当场报错，不能留给读侧去猜。
    """


class DescStoreWriter:
    """顺序写入固定容量的描述子库。"""

    def __init__(self, path: str | Path, capacity: int) -> None:
        self._path = Path(path)
        self._capacity = int(capacity)
        self._next = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._map = np.memmap(
            self._path, dtype=np.uint8, mode="w+",
            shape=(self._capacity * SLOT_STRIDE,),
        )

    def append(self, features: Features) -> int:
        if self._next >= self._capacity:
            raise IndexError(
                f"描述子库容量已满（capacity={self._capacity}）"
            )
        slot = self._next
        self._next += 1

        count = min(len(features), N_FEATURES)
        base = slot * SLOT_STRIDE
        raw = self._map[base : base + SLOT_STRIDE]
        raw[:] = 0

        raw[0:4].view(np.uint32)[0] = count
        if count:
            pts = np.ascontiguousarray(features.pts[:count], np.float32)
            raw[_PTS_OFFSET : _PTS_OFFSET + count * 8].view(np.float32)[:] = pts.ravel()
            desc = np.ascontiguousarray(features.desc[:count], np.uint8)
            raw[_DESC_OFFSET : _DESC_OFFSET + count * DESC_BYTES] = desc.ravel()
        return slot

    def close(self, require_complete: bool = True) -> None:
        self._map.flush()
        del self._map
        if require_complete and self._next < self._capacity:
            raise IncompleteWrite(
                f"只写入了 {self._next} 个 slot，声明的 capacity 是 {self._capacity}"
            )

    def __enter__(self) -> "DescStoreWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(require_complete=exc_type is None)


class DescStore:
    """只读随机访问描述子库。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        size = self._path.stat().st_size
        if size % SLOT_STRIDE:
            raise ValueError(
                f"{self._path} 大小 {size} 不是 slot 步长 {SLOT_STRIDE} 的整数倍"
            )
        self._count = size // SLOT_STRIDE
        self._map = np.memmap(self._path, dtype=np.uint8, mode="r", shape=(size,))

    def __len__(self) -> int:
        return self._count

    def read(self, slot: int) -> Features:
        if slot < 0 or slot >= self._count:
            raise IndexError(f"slot {slot} 超出范围 [0, {self._count})")
        base = slot * SLOT_STRIDE
        raw = self._map[base : base + SLOT_STRIDE]
        count = int(raw[0:4].view(np.uint32)[0])
        if count == 0:
            return Features(
                np.zeros((0, 2), np.float32), np.zeros((0, DESC_BYTES), np.uint8)
            )
        pts = (
            raw[_PTS_OFFSET : _PTS_OFFSET + count * 8]
            .view(np.float32)
            .reshape(count, 2)
            .copy()
        )
        desc = (
            raw[_DESC_OFFSET : _DESC_OFFSET + count * DESC_BYTES]
            .reshape(count, DESC_BYTES)
            .copy()
        )
        return Features(pts=pts, desc=desc)

    def close(self) -> None:
        del self._map

    def __enter__(self) -> "DescStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
