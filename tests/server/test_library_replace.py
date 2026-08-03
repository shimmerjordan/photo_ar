"""换参考图：`PhotoLibrary.replace`。

这个操作的价值在于 **slot 不变 → photo_id 不变 → 授权/视频/历史都留着**，所以下面第一
组用例钉的是「别的 slot 一个字节都没动」。

第二组钉的是**并发安全**：不能原地覆盖 desc.bin，因为 `DescStore` 的 mmap 是 MAP_SHARED
的，原地写会让一次在飞的精排读到半新半旧的槽（pts 来自旧图、desc 来自新图）—— 不崩，
只是匹配结果静默错。所以实现走「写临时文件 + 原子替换」，而这里用一个**仍然持着旧 mmap
的 DescStore** 来证明它确实没被改动。
"""

import numpy as np
import pytest

from photoar import features as F
from photoar import vocab as V
from photoar.descstore import DescStore
from photoar.server.library import PhotoLibrary


@pytest.fixture
def vocab(textured_image):
    descs = [
        F.extract(textured_image(seed=1000 + s, w=900, h=650)).desc for s in range(8)
    ]
    return V.train(np.vstack(descs), branching=6, depth=3, seed=0)


def _fill(lib, images):
    ids = []
    for i, img in enumerate(images):
        pid = f"{i:032x}"
        lib.add(pid, F.extract(img))
        ids.append(pid)
    return ids


# ---------------------------------------------------------------- 基本语义


def test_换完之后新图能被识别出来(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)

    fresh = textured_image(seed=777, w=900, h=650)
    lib.replace(ids[2], F.extract(fresh))

    d = lib.recognize(fresh)
    assert d.photo_id == ids[2], "换上去的新图要能匹配到同一个 photo_id"


def test_换完之后旧图不再匹配到它(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    old = images[2]

    lib.replace(ids[2], F.extract(textured_image(seed=777, w=900, h=650)))

    d = lib.recognize(old)
    assert d.photo_id != ids[2], "旧图的描述子已经被换掉了，不该还能匹配"


def test_slot_不变(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    before = lib.slot_of(ids[2])

    slot = lib.replace(ids[2], F.extract(textured_image(seed=777, w=900, h=650)))

    assert slot == before
    assert lib.slot_of(ids[2]) == before
    assert lib.photo_ids() == ids, "slots.json 的顺序不该变"


def test_库的条数不变(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)

    lib.replace(ids[0], F.extract(textured_image(seed=777, w=900, h=650)))

    assert len(lib) == 6


def test_换第一张和最后一张都对(tmp_path, vocab, textured_image):
    # 边界：copy_replacing_slot 里那个「跳过一槽」的循环最容易在首尾出错。
    images = [textured_image(seed=s, w=900, h=650) for s in range(5)]
    for target in (0, 4):
        lib = PhotoLibrary(tmp_path / f"lib{target}", vocab)
        ids = _fill(lib, images)
        fresh = textured_image(seed=900 + target, w=900, h=650)
        lib.replace(ids[target], F.extract(fresh))
        assert lib.recognize(fresh).photo_id == ids[target]
        # 其余每一张仍然认得出自己
        for i, img in enumerate(images):
            if i == target:
                continue
            assert lib.recognize(img).photo_id == ids[i], f"换 {target} 影响了 {i}"


def test_只有一张照片的库也能换(tmp_path, vocab, textured_image):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, [textured_image(seed=1, w=900, h=650)])
    fresh = textured_image(seed=2, w=900, h=650)
    lib.replace(ids[0], F.extract(fresh))
    assert lib.recognize(fresh).photo_id == ids[0]


def test_不在库里的_photo_id_被拒(tmp_path, vocab, textured_image):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    _fill(lib, [textured_image(seed=1, w=900, h=650)])
    with pytest.raises(ValueError) as e:
        lib.replace("f" * 32, F.extract(textured_image(seed=2, w=900, h=650)))
    assert "不在库中" in str(e.value)


# ---------------------------------------------------------------- 不碰别人


def test_别的槽一个字节都没动(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(5)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)

    desc_path = lib.desc_path
    words_path = lib.words_path
    before_desc = desc_path.read_bytes()
    before_words = words_path.read_bytes()
    stride = lib.backend.layout.stride
    wstride = (1 + 300) * 4  # ORB: 1 + N_FEATURES 个 uint32

    lib.replace(ids[2], F.extract(textured_image(seed=777, w=900, h=650)))

    after_desc = desc_path.read_bytes()
    after_words = words_path.read_bytes()
    assert len(after_desc) == len(before_desc), "文件长度不该变"
    assert len(after_words) == len(before_words)
    for i in range(5):
        d_same = after_desc[i * stride:(i + 1) * stride] == \
            before_desc[i * stride:(i + 1) * stride]
        w_same = after_words[i * wstride:(i + 1) * wstride] == \
            before_words[i * wstride:(i + 1) * wstride]
        if i == 2:
            assert not d_same, "目标槽的描述子必须变了"
        else:
            assert d_same, f"slot {i} 的描述子被动了"
            assert w_same, f"slot {i} 的词序列被动了"


def test_重开之后读到的是新的(tmp_path, vocab, textured_image):
    # 落盘了才算换成功。只改内存里的快照会在下次重启时悄悄回退。
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    root = tmp_path / "lib"
    lib = PhotoLibrary(root, vocab)
    ids = _fill(lib, images)
    fresh = textured_image(seed=777, w=900, h=650)
    lib.replace(ids[1], F.extract(fresh))

    again = PhotoLibrary(root, vocab)
    assert again.recognize(fresh).photo_id == ids[1]
    assert again.photo_ids() == ids


# ---------------------------------------------------------------- 并发安全


def test_在飞的读者看到的还是旧数据(tmp_path, vocab, textured_image):
    """这条是这个文件里最要紧的一条。

    模拟「一次识别请求正在精排的同时，另一个请求在换参考图」。老的 `DescStore` 持有
    旧 inode 的 mmap，必须**完全看不到**这次替换 —— 否则它会读到半新半旧的槽。

    如果实现改成原地 `seek + write`，这条会红：Linux 上 `np.memmap(mode="r")` 是
    MAP_SHARED，另一个句柄的写入会透过 mmap 被看到。
    """
    images = [textured_image(seed=s, w=900, h=650) for s in range(5)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)

    # 一个「正在精排」的读者：自己开一份 mmap，并先读一次留底。
    inflight = DescStore(lib.desc_path, lib.backend.layout)
    try:
        slot = lib.slot_of(ids[2])
        before = inflight.read(slot)

        lib.replace(ids[2], F.extract(textured_image(seed=777, w=900, h=650)))

        after = inflight.read(slot)
        assert np.array_equal(before.desc, after.desc), \
            "在飞的读者看到了新描述子 —— 说明实现是原地覆盖的，会读到半新半旧的槽"
        assert np.array_equal(before.pts, after.pts)
    finally:
        inflight.close()


def test_替换失败时库一个字节都不动(tmp_path, vocab, textured_image, monkeypatch):
    # 两份文件必须一起落地。只换一份就是错位，而错位的后果是「照片 A 的描述子挂在
    # 照片 B 的 id 上」—— 识别命中后播的是别人的视频，且条数检查查不出来。
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    before_desc = lib.desc_path.read_bytes()
    before_words = lib.words_path.read_bytes()

    # 让 words 那一步炸掉（desc 的临时文件已经写好了）
    def boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(
        PhotoLibrary, "_copy_words_replacing_slot", boom, raising=True
    )
    with pytest.raises(OSError):
        lib.replace(ids[1], F.extract(textured_image(seed=777, w=900, h=650)))

    assert lib.desc_path.read_bytes() == before_desc, "desc.bin 不该被换掉"
    assert lib.words_path.read_bytes() == before_words
    # 临时文件也要清掉，否则下次 reindex 时目录里多两个看不懂的文件
    assert not lib.desc_path.with_suffix(".bin.tmp").exists()
    assert not lib.words_path.with_suffix(".bin.tmp").exists()


def test_失败之后库仍然可用(tmp_path, vocab, textured_image, monkeypatch):
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)

    def boom(*a, **kw):
        raise OSError("磁盘满了")

    monkeypatch.setattr(PhotoLibrary, "_copy_words_replacing_slot", boom)
    with pytest.raises(OSError):
        lib.replace(ids[1], F.extract(textured_image(seed=777, w=900, h=650)))
    monkeypatch.undo()

    # 原来的每一张都还认得
    for i, img in enumerate(images):
        assert lib.recognize(img).photo_id == ids[i]
    # 而且现在还能正常换
    fresh = textured_image(seed=888, w=900, h=650)
    lib.replace(ids[1], F.extract(fresh))
    assert lib.recognize(fresh).photo_id == ids[1]
