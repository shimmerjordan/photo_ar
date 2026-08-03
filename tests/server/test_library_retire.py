"""删除照片：`PhotoLibrary.retire`。

## 为什么这件事非要有

库里进了两张同一内容的照片，比值检验（`verify.RATIO`）会把**两张都**判成
ambiguous —— 两张都永久扫不出来。这是一次真机上真实发生过的事：941 帧记录只命中
44 帧，内点数 160~229（门槛 40），挡住它们的就是这一条。去重闸门现在会拦住新的
（见 `test_dedup_gate_query_budget.py`），但**已经进去的那一对拦不住**，而在有
`retire` 之前解开它的唯一办法是重建整个库。

## 为什么是墓碑而不是真删

slot 是 desc.bin / words.bin 里的下标。摘掉一项就要把后面每一条往前挪，而那会让
photo_id ↔ slot 的对应关系整体平移 —— 错位不报错，命中之后播的是别人的视频。
所以下面第一组用例钉的是「别的 slot 一个字节都没动、id 一个都没挪」。
"""

import numpy as np
import pytest

from photoar import features as F
from photoar import vocab as V
from photoar.server.library import RETIRED, PhotoLibrary


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


def test_退役之后这张图不再被识别出来(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    assert lib.recognize(images[2]).photo_id == ids[2], "退役之前它是认得出来的"

    lib.retire(ids[2])
    assert lib.recognize(images[2]).photo_id != ids[2]


def test_别的照片一张都没受影响(tmp_path, vocab, textured_image):
    # 这是墓碑方案唯一要证明的东西：下标不平移。真删（往前挪）会让每一张的
    # photo_id ↔ slot 关系错开一格，而错位不报错 —— 命中之后播别人的视频。
    images = [textured_image(seed=s, w=900, h=650) for s in range(6)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    lib.retire(ids[2])
    for i, img in enumerate(images):
        if i == 2:
            continue
        assert lib.recognize(img).photo_id == ids[i], f"第 {i} 张被牵连了"


def test_槽位数不变而那一格变成墓碑(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    slot = lib.retire(ids[1])
    assert slot == 1

    raw = lib._read_slots()
    assert len(raw) == 4, "条数必须不变，否则 _assert_aligned 会判库损坏"
    assert raw[1] == RETIRED
    assert raw[0] == ids[0] and raw[2] == ids[2] and raw[3] == ids[3]


def test_photo_ids_不再报它(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    lib.retire(ids[1])
    assert lib.photo_ids() == [ids[0], ids[2], ids[3]]
    assert RETIRED not in lib.photo_ids()


def test_重开库之后墓碑还在(tmp_path, vocab, textured_image):
    # 墓碑在 slots.json 里，重开要能读回来。读不回来的后果是删掉的照片复活，
    # 而它复活之后继续把别人挤成 ambiguous —— 正是这个功能要解决的问题。
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    lib.retire(ids[1])

    again = PhotoLibrary(tmp_path / "lib", vocab)
    assert again.photo_ids() == [ids[0], ids[2], ids[3]]
    assert again.recognize(images[1]).photo_id != ids[1]


def test_退役是幂等的(tmp_path, vocab, textured_image):
    images = [textured_image(seed=s, w=900, h=650) for s in range(3)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    assert lib.retire(ids[0]) == 0
    assert lib.retire(ids[0]) == -1, "重复删不该抛，HTTP 层会因此 500"
    assert lib.retire("不存在的 id") == -1


def test_退役的照片不再参与去重判定(tmp_path, vocab, textured_image):
    # 删掉重复的那一张之后，同一张内容必须能重新入库 —— 否则「删掉再传一次」
    # 这条最自然的修复路径走不通，而那正是用户手上唯一的出路。
    images = [textured_image(seed=s, w=900, h=650) for s in range(3)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, images)
    same = F.extract(images[0])

    known = {pid: 200 for pid in ids}
    assert lib.conflicts(same, 200, known, query_features=same), "退役前应判冲突"
    lib.retire(ids[0])
    assert lib.conflicts(same, 200, known, query_features=same) == []


def test_退役之后剩下那张能重新被认出来(tmp_path, vocab, textured_image):
    # 这条是整件事的**出口条件**：两张近重复互相挤成 ambiguous，删掉一张之后
    # 另一张要立刻恢复可识别。
    base = textured_image(seed=42, w=900, h=650)
    near = base.copy()
    near[:12, :12] = 0  # 几乎相同，只改一个角
    others = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = _fill(lib, others + [base, near])
    dup_a, dup_b = ids[4], ids[5]

    before = lib.recognize(base)
    assert not before.matched and before.reason == "ambiguous", (
        f"两张近重复本该互相挤掉，实际 {before}"
    )

    lib.retire(dup_b)
    after = lib.recognize(base)
    assert after.matched and after.photo_id == dup_a, f"删掉一张之后该恢复，实际 {after}"
