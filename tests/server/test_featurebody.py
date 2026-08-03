"""`featurebody.parse` 的校验。

这些检查存在的唯一理由是：端上提特征这条路上**每一种错都是静默的**。描述子对不上
不会抛异常、不会 500，只会让识别率变低 —— 而在一个家用部署里没人会把"扫不太出来"
归因到字节序或归一化上。所以每一条都得有一条测试盯着，包括那些看起来不可能发生的。
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from photoar import xfeat
from photoar.features import Features
from photoar.server import featurebody
from photoar.server.featurebody import FeaturesRejected


def unit_desc(n: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    d = rng.standard_normal((n, xfeat.DESC_DIM)).astype(np.float32)
    return np.ascontiguousarray(d / np.linalg.norm(d, axis=1, keepdims=True), np.float32)


def body(
    n: int = 8,
    *,
    height: int = 480,
    width: int = 640,
    desc: np.ndarray | None = None,
    pts: np.ndarray | None = None,
) -> dict:
    if pts is None:
        pts = np.linspace(0, 300, n * 2, dtype=np.float32).reshape(n, 2)
    if desc is None:
        desc = unit_desc(n)
    return featurebody.encode(Features(pts=pts, desc=desc), height, width)


def rejected(doc: dict) -> FeaturesRejected:
    with pytest.raises(FeaturesRejected) as exc:
        featurebody.parse(doc)
    return exc.value


# ---- 正常路径 ----


def test_roundtrip_is_byte_exact():
    """float32 经 base64 往返必须逐字节相同。

    不是的话 `_check_norms` 那道 1e-2 的线会开始偶发误伤，而现象会像"客户端有时候
    归一化不对"。
    """
    desc = unit_desc(31, seed=7)
    pts = np.arange(62, dtype=np.float32).reshape(31, 2)
    out = featurebody.parse(featurebody.encode(Features(pts=pts, desc=desc), 480, 640))
    assert np.array_equal(out.pts, pts)
    assert np.array_equal(out.desc, desc)
    assert out.pts.dtype == np.float32 and out.desc.dtype == np.float32


def test_full_512_is_accepted():
    out = featurebody.parse(
        body(
            xfeat.TOP_K,
            pts=np.zeros((xfeat.TOP_K, 2), np.float32),
            desc=unit_desc(xfeat.TOP_K),
        )
    )
    assert len(out) == xfeat.TOP_K


def test_zero_features_is_accepted_as_empty():
    """空串 = 0 个特征。白墙上确实提不出关键点，那是"未命中"而不是客户端 bug。"""
    doc = {"width": 640, "height": 480, "keypoints": "", "descriptors": ""}
    out = featurebody.parse(doc)
    assert len(out) == 0
    assert out.pts.shape == (0, 2) and out.desc.shape == (0, xfeat.DESC_DIM)


def test_result_arrays_are_writable():
    """`np.frombuffer` 给的是只读视图，下游有原地操作的路径。"""
    out = featurebody.parse(body(4))
    assert out.pts.flags.writeable and out.desc.flags.writeable


# ---- 字段缺失与类型 ----


@pytest.mark.parametrize("field", ["width", "height", "keypoints", "descriptors"])
def test_missing_field_is_rejected(field):
    doc = body(4)
    del doc[field]
    err = rejected(doc)
    assert err.code in ("bad_size", "missing_field")


def test_field_name_typo_does_not_silently_become_empty():
    """字段名拼错必须报错，不能当 0 个特征。

    当 0 个特征处理的话，一个拼错字段名的客户端会永远收到"未命中"，然后作者去查
    照片、阈值、词表 —— 唯独不会想到字段名。
    """
    doc = body(4)
    doc["keypoint"] = doc.pop("keypoints")
    assert rejected(doc).code == "missing_field"


def test_bool_is_not_an_int_for_size():
    """`isinstance(True, int)` 是 True，所以必须显式排除 —— 否则 `width: true`
    会静默变成 width=1。"""
    doc = body(4)
    doc["width"] = True
    assert rejected(doc).code == "bad_size"


@pytest.mark.parametrize("bad", [0, -640, 10**9, "640", 640.0, None])
def test_bad_size_values(bad):
    doc = body(4)
    doc["width"] = bad
    assert rejected(doc).code == "bad_size"


# ---- base64 与长度 ----


def test_non_base64_is_rejected():
    doc = body(4)
    doc["descriptors"] = "这不是 base64"
    assert rejected(doc).code == "bad_base64"


def test_truncated_base64_is_rejected_not_silently_trimmed():
    """默认的 `b64decode` 会**静默丢掉**字母表外的字符，于是一段被污染的载荷可能仍然
    解出一个长度刚好对得上的数组。`validate=True` 是为了拦住这个。"""
    doc = body(4)
    doc["descriptors"] = doc["descriptors"][:-4] + "!!!!"
    assert rejected(doc).code == "bad_base64"


def test_length_not_multiple_of_row_is_rejected():
    desc = unit_desc(4)
    raw = desc.tobytes()[:-4]  # 少一个 float32
    doc = body(4)
    doc["descriptors"] = base64.b64encode(raw).decode("ascii")
    assert rejected(doc).code == "bad_length"


def test_count_mismatch_is_rejected():
    doc = body(8)
    doc["keypoints"] = base64.b64encode(
        np.zeros((7, 2), "<f4").tobytes()
    ).decode("ascii")
    err = rejected(doc)
    assert err.code == "count_mismatch"
    assert "8" in err.message and "7" in err.message


def test_too_many_keypoints_is_rejected():
    n = xfeat.TOP_K + 1
    doc = body(
        n, pts=np.zeros((n, 2), np.float32), desc=unit_desc(n)
    )
    err = rejected(doc)
    assert err.code == "too_many"
    assert str(xfeat.TOP_K) in err.message


# ---- 归一化：**拒绝**而不是重新归一化 ----


def test_unnormalized_descriptors_are_rejected():
    """服务端**不**替客户端归一化。

    理由写在 `_check_norms` 的 docstring 里：归一化一个解错的缓冲区只会得到"看起来
    合法、内容是垃圾"的单位向量，然后正常参与余弦互近邻 —— 把"客户端管线坏了"变成
    "识别效果不太好"。
    """
    desc = unit_desc(6) * 3.7
    err = rejected(body(6, desc=desc))
    assert err.code == "bad_descriptors"
    # 报文里要有行号和实际范数，客户端作者才下得了手
    assert "范数" in err.message and "3.7" in err.message


def test_a_single_bad_row_is_enough():
    desc = unit_desc(10)
    desc[4] *= 0.5
    err = rejected(body(10, desc=desc))
    assert err.code == "bad_descriptors"
    assert "第 4 行" in err.message


def test_float32_normalize_noise_is_accepted():
    """真实客户端的范数偏差在 1e-7 量级，不能被这道闸门误伤。"""
    desc = unit_desc(64, seed=3)
    assert np.abs(np.linalg.norm(desc, axis=1) - 1).max() < featurebody.NORM_TOLERANCE
    assert len(featurebody.parse(body(64, desc=desc))) == 64


def test_all_zero_descriptor_row_is_rejected():
    """全零行的范数是 0。它不会报错，但在余弦匹配里与**任何**描述子的相似度都是 0，
    于是那个关键点永远配不上 —— 静默地少掉一部分特征。"""
    desc = unit_desc(5)
    desc[2] = 0.0
    assert rejected(body(5, desc=desc)).code == "bad_descriptors"


@pytest.mark.parametrize("poison", [np.nan, np.inf, -np.inf])
def test_non_finite_descriptors_are_rejected(poison):
    """NaN 必须在范数检查**之前**被挡掉：与 NaN 的比较恒假，也就是"检查通过"。"""
    desc = unit_desc(5)
    desc[1, 3] = poison
    err = rejected(body(5, desc=desc))
    assert err.code == "bad_descriptors"
    assert "NaN" in err.message


def test_byte_order_swap_is_caught():
    """字节序反了是这条路上最可能发生的一种错（两侧一个 Kotlin 一个 numpy）。

    它一定会被抓到，因为反过来读的 float32 范数会飞到天文数字或 NaN —— 这条测试
    钉住"抓得到"，而不是靠推理。
    """
    desc = unit_desc(6, seed=11)
    doc = body(6, desc=desc)
    doc["descriptors"] = base64.b64encode(
        desc.astype(">f4").tobytes()  # 大端
    ).decode("ascii")
    assert rejected(doc).code == "bad_descriptors"


# ---- 关键点坐标 ----


def test_keypoints_outside_valid_region_are_rejected():
    """坐标越界 = 端上预处理写歪了。这是服务端唯一能抓到那件事的检查。"""
    pts = np.array([[10.0, 10.0], [639.0, 500.0]], np.float32)  # 有效区是 640×360
    err = rejected(body(2, height=720, width=1280, pts=pts, desc=unit_desc(2)))
    assert err.code == "bad_keypoints"
    assert "640×360" in err.message


def test_negative_keypoints_are_rejected():
    pts = np.array([[-5.0, 10.0]], np.float32)
    assert rejected(body(1, pts=pts, desc=unit_desc(1))).code == "bad_keypoints"


def test_unscaled_keypoints_are_rejected():
    """"忘了缩到长边 640"：坐标会直接是原图尺度上的，大到上千。"""
    pts = np.array([[1180.0, 700.0]], np.float32)
    assert rejected(
        body(1, height=720, width=1280, pts=pts, desc=unit_desc(1))
    ).code == "bad_keypoints"


def test_xy_swapped_on_portrait_is_rejected():
    """xy 反了在竖图上立刻越界（有效区 360 宽、640 高）。"""
    pts = np.array([[600.0, 300.0]], np.float32)  # x=600 > nw=360
    assert rejected(
        body(1, height=1280, width=720, pts=pts, desc=unit_desc(1))
    ).code == "bad_keypoints"


def test_edge_coordinate_is_accepted():
    """有效区最后一个像素（nw-1, nh-1）必须过得去 —— 图内那道 inside 掩码用的就是
    `< nw`，所以它是合法的最大值。"""
    pts = np.array([[639.0, 359.0]], np.float32)
    assert len(
        featurebody.parse(body(1, height=720, width=1280, pts=pts, desc=unit_desc(1)))
    ) == 1


def test_rounding_slack_absorbs_one_pixel():
    """客户端与服务端算 (nh, nw) 时可能差 1 个像素（两边都在算 round()）。那点差异
    不该把合法请求判成越界。"""
    nh, nw = xfeat.canvas_size(800, 1200)
    pts = np.array([[nw + 0.5, nh + 0.5]], np.float32)
    assert len(
        featurebody.parse(body(1, height=800, width=1200, pts=pts, desc=unit_desc(1)))
    ) == 1


def test_client_may_report_already_scaled_size():
    """上报原始帧尺寸或已缩过的尺寸都行 —— `canvas_size` 对成比例缩放不敏感。

    这一条让端上不必记住"要报哪一个"，而那种约定正是最容易两边理解不一致的东西。
    """
    pts = np.array([[600.0, 350.0]], np.float32)
    desc = unit_desc(1)
    assert len(featurebody.parse(body(1, height=720, width=1280, pts=pts, desc=desc))) == 1
    assert len(featurebody.parse(body(1, height=360, width=640, pts=pts, desc=desc))) == 1


def test_non_finite_keypoints_are_rejected():
    pts = np.array([[np.nan, 10.0]], np.float32)
    err = rejected(body(1, pts=pts, desc=unit_desc(1)))
    assert err.code == "bad_keypoints"
