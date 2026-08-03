"""定长 slot 的描述子/关键点存储，用 mmap 随机读。

每张照片占固定 SLOT_STRIDE 字节，slot 下标即偏移，因此精排阶段只需
按 Top-K 的下标随机读 K 个 slot，无需把全库描述子常驻内存。

slot 布局（本机字节序 / native order，见下方 Minor #8 说明）：
  offset 0   uint32  count      实际特征数（<= N_FEATURES）
  offset 4   uint32  _pad       对齐填充，保证 float32 数组 8 字节对齐
  offset 8   float32[N_FEATURES*2]  关键点 xy
  offset ..  uint8[N_FEATURES*32]   描述子

spec §6 给的 9600 字节/张只算了描述子，漏了 RANSAC 必需的关键点坐标。
实际每张 12008 字节，1 万张约 120MB（仍在预算内）。

Minor #8：这是一份**文件格式**声明，Phase 1 会用别的代码直接读这些文件，
所以必须写清楚真实约束，不能想当然。np.uint32/np.float32 走的是运行
该进程的 CPU 本机字节序（native order），不是"写死小端"——numpy 默认
dtype（不带 '<'/'>' 前缀）就是本机序，这里从来没有显式要求过小端。
文档曾经写"小端"，只是因为 Phase 0/1 目前唯一会跑这份代码的目标
（x86-64、ARM64 手机 SoC）全部是小端，从未被验证过、也从未被强制过。
如果将来在大端机器上写入再拿到小端机器上读（反之亦然），这里不会
自动转换字节序，会读出错误的 count/坐标/描述子——但目前每一个受支持
的目标平台都是小端，所以这不是一个已知的活 bug，只是一个不应该被
文档过度承诺成"小端"的真实前提条件。
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .features import DESC_BYTES, N_FEATURES, Features

_HEADER_BYTES = 8


@dataclass(frozen=True)
class SlotLayout:
    """一个 slot 的字节布局。

    为什么要参数化：识别后端从 ORB 扩到 XFeat 之后，描述子从「32 字节二进制」变成
    「64 维 float32」，slot 步长和 dtype 都不一样。**但字节布局的实现只能有一份** ——
    这个文件的 docstring 把布局声明成一种文件格式，Phase 1 之外还有别的代码直接读它，
    抄第二份出来就等于让两种后端的文件格式各自漂移，而漂移不会报错，只会读出错位的
    坐标。所以这里把「布局」抽成参数，编解码逻辑保持一份。

    两种后端的库文件**互不兼容也不该兼容**：换后端等于换特征，全库描述子都得重算，
    所以它们本来就落在不同目录（见 PhotoLibrary 的 root）。
    """

    n_features: int
    desc_dim: int  # 描述子长度：ORB 是 32（字节），XFeat 是 64（float32 分量数）
    desc_dtype: np.dtype

    @property
    def desc_itemsize(self) -> int:
        return int(np.dtype(self.desc_dtype).itemsize)

    @property
    def pts_bytes(self) -> int:
        return self.n_features * 2 * 4

    @property
    def desc_bytes(self) -> int:
        return self.n_features * self.desc_dim * self.desc_itemsize

    @property
    def pts_offset(self) -> int:
        return _HEADER_BYTES

    @property
    def desc_offset(self) -> int:
        return _HEADER_BYTES + self.pts_bytes

    @property
    def stride(self) -> int:
        return _HEADER_BYTES + self.pts_bytes + self.desc_bytes

    def empty(self) -> Features:
        return Features(
            pts=np.zeros((0, 2), np.float32),
            desc=np.zeros((0, self.desc_dim), self.desc_dtype),
        )


# ORB 布局。模块级常量保持原值与原语义，既有代码与测试不必改一个字。
ORB_LAYOUT = SlotLayout(n_features=N_FEATURES, desc_dim=DESC_BYTES, desc_dtype=np.uint8)
SLOT_STRIDE = ORB_LAYOUT.stride

_PTS_OFFSET = ORB_LAYOUT.pts_offset
_DESC_OFFSET = ORB_LAYOUT.desc_offset


def truncate_count(
    n_features_available: int, layout: SlotLayout = ORB_LAYOUT
) -> int:
    """Minor #23：算出真正会被写进/读出一个 slot 的特征数上限。

    descstore.DescStoreWriter.append 与 corpus._desc_fingerprint 都需要
    这个数字，且两处**必须**永远一致——fingerprint 校验的就是"manifest
    记录的指纹"与"DescStoreWriter 实际写入的字节"是不是同一份内容，如果
    两处各自独立写 `min(count, N_FEATURES)`，未来只要有一处改了截断规则
    而另一处没跟着改，指纹校验就会系统性地假报不匹配（或者更糟：系统性
    地假通过）。extract() 本身已经把返回的特征数上限收在 N_FEATURES，
    这条 min() 目前恒等于"什么都不做"，但正是因为它现在不可达才最容易
    被两边各自维护到分叉而不被测试发现，所以显式抽出来共用一个函数。
    """
    return min(n_features_available, layout.n_features)


def encode_slot(features: Features, layout: SlotLayout = ORB_LAYOUT) -> bytes:
    """把一张照片编成恰好 layout.stride 字节的一个 slot。

    抽出来的理由和 truncate_count 一样：现在有两个写入方——Phase 0 的定长
    `DescStoreWriter`（预声明容量、mmap 覆写）与 Phase 1 服务端的增量追加
    （`append_slot`，边入库边长）。布局写两遍，改一遍忘一遍不会有任何报错，
    只会让两个写入方产出的文件在同一个 `DescStore` 下读出错误的坐标。
    """
    buf = np.zeros(layout.stride, np.uint8)
    count = truncate_count(len(features), layout)
    buf[0:4].view(np.uint32)[0] = count
    if count:
        pts = np.ascontiguousarray(features.pts[:count], np.float32)
        lo = layout.pts_offset
        buf[lo : lo + count * 8].view(np.float32)[:] = pts.ravel()
        desc = np.ascontiguousarray(features.desc[:count], layout.desc_dtype)
        lo = layout.desc_offset
        span = count * layout.desc_dim * layout.desc_itemsize
        buf[lo : lo + span].view(layout.desc_dtype)[:] = desc.ravel()
    return buf.tobytes()


def append_slot(
    path: str | Path, features: Features, layout: SlotLayout = ORB_LAYOUT
) -> int:
    """把一张照片追加到（可能还不存在的）描述子库末尾，返回它的 slot 下标。

    Phase 1 的入库是一张一张来的，没有"预先知道总数"这回事，所以不能用
    `DescStoreWriter`（它要求预声明 capacity，且未写满就 raise）。这里用
    追加写而不是 mmap：追加是原子的（单次 write 小于 12KB），进程在中途
    被杀最多留下一个尾部残缺的文件，`DescStore` 构造时的"大小必须是步长
    整数倍"检查会当场发现，而不是静默读出错位的描述子。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.stat().st_size if path.exists() else 0
    if existing % layout.stride:
        raise ValueError(
            f"{path} 大小 {existing} 不是 slot 步长 {layout.stride} 的整数倍，"
            f"追加会让整个文件错位"
        )
    with open(path, "ab") as fh:
        fh.write(encode_slot(features, layout))
        fh.flush()
    return existing // layout.stride


class IncompleteWrite(RuntimeError):
    """写入的 slot 数少于声明的 capacity。

    未写过的 slot 读出来是 count=0，与"这张照片确实零特征"无法区分，
    所以半途结束的写入必须当场报错，不能留给读侧去猜。
    """


class DescStoreWriter:
    """顺序写入固定容量的描述子库。"""

    def __init__(
        self, path: str | Path, capacity: int, layout: SlotLayout = ORB_LAYOUT
    ) -> None:
        self._path = Path(path)
        self._capacity = int(capacity)
        self._layout = layout
        self._next = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._map = np.memmap(
            self._path, dtype=np.uint8, mode="w+",
            shape=(self._capacity * layout.stride,),
        )

    def append(self, features: Features) -> int:
        if self._next >= self._capacity:
            raise IndexError(
                f"描述子库容量已满（capacity={self._capacity}）"
            )
        slot = self._next
        self._next += 1

        stride = self._layout.stride
        base = slot * stride
        self._map[base : base + stride] = np.frombuffer(
            encode_slot(features, self._layout), np.uint8
        )
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

    def __init__(self, path: str | Path, layout: SlotLayout = ORB_LAYOUT) -> None:
        self._path = Path(path)
        self._layout = layout
        size = self._path.stat().st_size
        if size % layout.stride:
            raise ValueError(
                f"{self._path} 大小 {size} 不是 slot 步长 {layout.stride} 的整数倍"
            )
        self._count = size // layout.stride
        self._map = np.memmap(self._path, dtype=np.uint8, mode="r", shape=(size,))

    def __len__(self) -> int:
        return self._count

    def read(self, slot: int) -> Features:
        if slot < 0 or slot >= self._count:
            raise IndexError(f"slot {slot} 超出范围 [0, {self._count})")
        lay = self._layout
        base = slot * lay.stride
        raw = self._map[base : base + lay.stride]
        count = int(raw[0:4].view(np.uint32)[0])
        if count == 0:
            return lay.empty()
        lo = lay.pts_offset
        pts = raw[lo : lo + count * 8].view(np.float32).reshape(count, 2).copy()
        lo = lay.desc_offset
        span = count * lay.desc_dim * lay.desc_itemsize
        desc = (
            raw[lo : lo + span]
            .view(lay.desc_dtype)
            .reshape(count, lay.desc_dim)
            .copy()
        )
        return Features(pts=pts, desc=desc)

    def close(self) -> None:
        del self._map

    def __enter__(self) -> "DescStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
