"""查询侧特征预算是一个**独立于入库侧**的旋钮。

为什么要独立，而不是把 `features.N_FEATURES` 一起调大：

* 入库侧的数字决定 `descstore` 的定长槽位（`8 + N*8 + N*32` 字节）。改它等于
  改磁盘布局 —— 全库描述子作废，必须重新入库。
* 入库侧还是自匹配分的量纲（`dedup.self_score` → `library.conflicts` 的
  `min(s_new, s_exist) < ratio * m`）。老照片的分是 300 特征算出来的，新照片
  换成 4000 就不可比，去重闸门会整体失准 —— 而且不报错。

所以这里锁的是"两边可以不一样，且入库那边必须保持 300"。这几条断言是在
挡一次很自然的"清理"：有人看到两个提特征入口做同一件事，把它们合回一个。
"""

import numpy as np

from photoar import backend as B
from photoar import features


def _textured(w: int = 1280, h: int = 900) -> np.ndarray:
    """随机方块拼图：ORB 在上面能提到远超 300 个角点，才测得出预算差异。

    长边必须是 1280（= `Frames.LONG_EDGE` = `B.QUERY_LONG_EDGE`），不能图省事用
    900：`features.resize_to_long_edge` 在图比目标**小**的时候会 INTER_LINEAR
    **放大**，插值出来的像素上找不到强角点，900 宽的图提到的是 3765 个而不是满
    4000 —— 那测的是插值行为，不是预算。真实帧就是 1280，测试图跟着它。
    """
    rng = np.random.default_rng(7)
    base = rng.integers(0, 256, (h // 8, w // 8, 3), dtype=np.uint8)
    import cv2

    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
    for _ in range(60):
        x1, y1 = int(rng.integers(0, w)), int(rng.integers(0, h))
        x2 = min(w - 1, x1 + int(rng.integers(20, 120)))
        y2 = min(h - 1, y1 + int(rng.integers(20, 120)))
        cv2.rectangle(img, (x1, y1), (x2, y2),
                      tuple(int(c) for c in rng.integers(0, 256, 3)), -1)
    return img


def test_入库侧仍然是300() -> None:
    """这条断言的价值全在"改它会红"：动了就得重建全库。"""
    img = _textured()
    assert len(B.orb_backend().extract(img)) == features.N_FEATURES == 300


def test_查询侧比入库侧多() -> None:
    b = B.orb_backend()
    img = _textured()
    assert len(b.extract_query(img)) > len(b.extract(img))
    assert len(b.extract_query(img)) == B.QUERY_N_FEATURES


def test_查询侧在1280上提特征而不是缩回640() -> None:
    """**处理长边**是识别率的主导变量，比发帧长边更关键。

    `features.extract` 的 `long_edge` 默认是 640，会把送进来的帧先缩到 640 再提
    特征。所以只把客户端发帧改成 1280、这里不跟着传 `long_edge`，帧一进门就被缩
    回 640。实测（用户真实婚礼照 + 真实桌面场景，5 个随机视角取"全部过门槛"）：

        发帧    处理    全过的最小占比
        640     640     一档都不全过     ← 原状态，真机扫不出来就是这一行
        1280    640     一档都不全过     ← **只改客户端等于完全没改**
        640     1280    0.5
        1280    1280    0.4              ← 现在这一档
        1280    1600    0.4（持平，白花 CPU）
        1280    1920    退化成一档都不全过

    第二行是这条测试真正在挡的东西：漏传 `long_edge` 不报错、日志无异常，表现和
    没做优化一模一样。

    量法用坐标而不是特征数：`Features.pts` 的坐标在**缩放后**的图像坐标系里
    （见 features.py 的字段注释），所以 1280 宽的帧要是被缩到 640，横坐标就绝不
    可能超过 640。比数特征数稳 —— 特征数还受插值平滑影响，会左右晃。
    """
    frame = _textured(1280, 720)
    b = B.orb_backend()
    assert B.QUERY_LONG_EDGE == 1280
    assert b.extract_query(frame).pts[:, 0].max() > 640
    # 对照：入库侧仍在 640 上处理，横坐标就到不了 640
    assert b.extract(frame).pts[:, 0].max() <= 640
    assert len(b.extract_query(frame)) == B.QUERY_N_FEATURES


def test_查询侧描述子仍是ORB布局() -> None:
    """多提特征不能顺带换掉描述子形状 —— 词表和 Hamming 配对都吃这个宽度。"""
    q = B.orb_backend().extract_query(_textured())
    assert q.desc.dtype == np.uint8
    assert q.desc.shape[1] == features.DESC_BYTES


def test_没配查询侧的后端回退到入库那一个() -> None:
    """XFeat 没有这个旋钮（`xfeat.extract` 不收特征数），不能因此就崩。

    直接构造一个 `_extract_query=None` 的后端来验证回退，而不是去实例化
    `xfeat_backend()` —— 后者会立刻加载 ONNX 模型，模型不在时这条测试会以
    一个和本主题无关的理由失败。
    """
    b = B.orb_backend()
    fallback = B.Backend(**{**b.__dict__, "_extract_query": None})
    img = _textured()
    assert len(fallback.extract_query(img)) == len(fallback.extract(img))
