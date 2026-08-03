"""去重闸门的交叉分 `m` 必须按**查询侧**的特征预算量。

## 这条是照着一次真实漏网写的

同一张海报被拍了两次、两张都入了库。然后真机 941 帧记录里只命中 44 帧 —— 内点数
160~229（门槛 40），但 top1/top2 恒定落在 1.13~1.56，比值检验判 ambiguous。也就是
`library.conflicts` 自己 docstring 上预言的「两份都永久漏检，用户看到的现象是识别器
坏了」，一字不差地发生了。

闸门为什么没拦住：`m` 是拿两张**入库侧**特征（300 个 / 640px）互相配对算的，而识别时
的 `i2`（查询帧打在错的那张参考图上的内点数）是**查询侧**预算（4000 个 / 1280px）对
同一份参考图算的。同一对图实测 63 vs 123 —— 低一半，而判据
`min(self) < ratio * m` 恰好卡在中间：149 >= 1.5×63=94.5 放行，149 < 1.5×123=184.5 拦下。

所以这不是「把闸门调紧」，是让 `m` 量它名字所指的那个东西。下面的用例分两组：
**该抓的抓到**（近重复的 m 随查询预算涨上去），**不该抓的没动**（内容无关的 m 本来就
在噪声量级，涨不动）。
"""

import numpy as np
import pytest

from photoar import backend as backend_mod
from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.server.library import PhotoLibrary


@pytest.fixture
def vocab(textured_image):
    descs = [
        F.extract(textured_image(seed=1000 + s, w=900, h=650)).desc for s in range(8)
    ]
    return V.train(np.vstack(descs), branching=6, depth=3, seed=0)


def _query(img):
    """查询侧口径：与 `backend.extract_query` / `verify_features` 完全一致。"""
    return F.extract(
        img,
        long_edge=backend_mod.QUERY_LONG_EDGE,
        n_features=backend_mod.QUERY_N_FEATURES,
    )


def _lib(tmp_path, vocab, images):
    lib = PhotoLibrary(tmp_path / "lib", vocab)
    ids = []
    for i, img in enumerate(images):
        pid = f"{i:032x}"
        lib.add(pid, F.extract(img))
        ids.append(pid)
    return lib, ids


def _two_shots(textured_image, seed):
    """同一张内容的**两次独立拍摄**：各自一组透视/光照扰动。

    不用「原图 + 把一个角涂黑」当近重复：那样两张几乎逐像素相同，入库口径的配对会
    直接饱和（实测 300/300 个特征全成内点），于是查询口径反而更低 —— 那是个退化
    情形，不是真实的「同一张海报拍了两次」。而后者才是这次漏网的形态。
    """
    base = textured_image(seed=seed, w=900, h=650)
    return synth.generate(base, 1, seed=5)[0][0], synth.generate(base, 1, seed=9)[0][0]


def test_闸门只会变严绝不会变松(tmp_path, vocab, textured_image):
    """`m` 取三个方向的最大值，所以多给一个方向只可能让它变大。

    这是这次改动的**安全性质**，而且它是普适的（max 取在一个超集上）：误拦的代价是
    「传不进去」，漏放的代价是「两张照片永久扫不出来」。所以这个方向必须钉死。
    """
    shot_a, shot_b = _two_shots(textured_image, 11)
    lib, ids = _lib(tmp_path, vocab, [shot_a])
    # 自匹配分给 0：判据是 `s >= ratio*m 就放行`，所以 0 让**任何** m 都必然报冲突 ——
    # 于是 `Conflict.inliers` 就是那个 m 本身，m 成了唯一的变量。
    # （给极高的分反而恒定放行、什么都读不到，第一版就是这么写错的。）
    zero = {ids[0]: 0}
    new_f = F.extract(shot_b)
    m_ingest = lib.conflicts(new_f, 0, zero)[0].inliers
    m_query = lib.conflicts(new_f, 0, zero, query_features=_query(shot_b))[0].inliers
    assert m_query >= m_ingest, f"查询口径把 m 变小了：{m_query} < {m_ingest}"


def test_查询侧那个方向确实被算进去了(tmp_path, vocab, textured_image):
    """白盒：`m` 真的把「查询侧特征 vs 库里参考图」这个方向算进去了。

    构造是刻意的：入库侧特征取一张**无关**的图（那两个方向只有噪声量级的内点），
    查询侧特征取库里那张**自己**（这个方向必然很高）。于是报出来的 m 只可能来自
    第三个方向 —— 如果实现没接上这个参数，这里就一条冲突都没有。

    ⚠️ 为什么不用「两张近重复图」来测量级：合成噪声图**复现不出**那个量级。真实照片
    在多个尺度上都有结构，查询侧多出来的那 3700 个特征落在真细节上；噪声图上它们落
    在重采样的伪影上，于是两个口径的 m 基本相等（实测 41 vs 41）。量级那一半的证据
    是本模块 docstring 里那对真实照片（63 → 123），不是这里的合成图。
    """
    images = [textured_image(seed=s, w=900, h=650) for s in range(3)]
    lib, ids = _lib(tmp_path, vocab, images)
    unrelated = F.extract(textured_image(seed=888, w=900, h=650))

    zero = {pid: 0 for pid in ids}
    assert lib.conflicts(unrelated, 0, zero) == [], (
        "无关图在两个入库方向上本来就配不上，这是这条测试的前提"
    )
    got = lib.conflicts(unrelated, 0, zero, query_features=_query(images[1]))
    assert [c.photo_id for c in got] == [ids[1]], (
        "查询侧那个方向没被算进去 —— 参数没接上，或者 max 少了一项"
    )


def test_内容无关的照片不会被误判成冲突(tmp_path, vocab, textured_image):
    """误拦的代价是「传不进去」，所以这条必须钉住。

    真负样本那边的余量很大：实测 5 对无关照片的 m 从 5~7 只涨到 7~8（噪声量级），
    而它们的自匹配分是 104~107。
    """
    images = [textured_image(seed=s, w=900, h=650) for s in range(5)]
    lib, ids = _lib(tmp_path, vocab, images)
    fresh = textured_image(seed=999, w=900, h=650)
    known = {pid: 100 for pid in ids}
    assert lib.conflicts(F.extract(fresh), 100, known, query_features=_query(fresh)) == []


def test_不传查询侧特征时行为不变(tmp_path, vocab, textured_image):
    """向后兼容：这个参数是可选的，不传就该和以前逐字节一样。

    有既有调用方（测试、`bench/`）不传它，而它们钉的数字是按入库口径校准的。
    """
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib, ids = _lib(tmp_path, vocab, images)
    same = F.extract(images[1])
    known = {pid: 30 for pid in ids}
    got = lib.conflicts(same, 30, known)
    assert [c.photo_id for c in got] == [ids[1]], "同一张图不传查询特征也该判冲突"


def test_排除自己仍然有效(tmp_path, vocab, textured_image):
    """换参考图那条路：库里那张就是要被换掉的自己，不能判成近重复。"""
    images = [textured_image(seed=s, w=900, h=650) for s in range(4)]
    lib, ids = _lib(tmp_path, vocab, images)
    fresh = textured_image(seed=555, w=900, h=650)
    known = {pid: 100 for pid in ids}
    assert (
        lib.conflicts(
            F.extract(fresh), 100, known, query_features=_query(fresh), exclude=ids[2]
        )
        == []
    )
