"""`POST /v1/recognize/features`（端上提特征）与 `GET /v1/model/xfeat`。

## 为什么这里有一个"假 XFeat 后端"

真 XFeat 后端在构造时就会加载 `xfeat.onnx`（刻意的，见 `backend.xfeat_backend`），
而那个文件不进版本库也不进镜像 —— 测试不能依赖开发机上恰好有一份。

替代方案里只有"换掉提特征那一步"是诚实的：本文件要验的是**接口**（路由、后端判据、
请求体校验、ACL、阈值、响应形状），而这些全都在提特征之后。所以假后端把 `_extract`
换成"ORB 的 256bit 描述子摊成 64 维 float32 再 L2 归一化"，其余四件事
（配对函数 `verify_pair_xfeat`、存储布局 `XFEAT_LAYOUT`、词表类、阈值）**全部用真的**
—— 也就是说余弦互近邻、那道 0.82 的闸门、512×64 float32 的 slot 都是真在跑的。

假描述子的内点数分布不等于真 XFeat 的（也不等于 ORB 的），所以查询用"参考图自己过
一遍 JPEG 编解码"而不是 `synth.generate` 的强扰动：那给到 400+ 内点，离默认阈值 40
有一个数量级的余量，测试不会因为特征强度的抖动而变红。真实语料上的命中率与误识别率
由 `tests/test_evaluate.py` 与 0d 的上规模跑负责，不是这里的职责。
"""

from __future__ import annotations

import base64
import json

import cv2
import numpy as np
import pytest

from photoar import backend as backend_mod
from photoar import features as F
from photoar import floatvocab, nullvocab, verify, xfeat
from photoar.features import Features
from photoar.server import featurebody

# 一个 nibble 的位权。把 ORB 的 256 位按每 4 位一组变成 64 个 0..15 的数。
_NIBBLE = np.array([8.0, 4.0, 2.0, 1.0], np.float32)


def fake_xfeat_extract(img_bgr: np.ndarray) -> Features:
    """ORB → 64 维 L2 归一化 float32。确定性，且与真 XFeat 一样"同一张图两次相同"。

    减去每行均值再归一化，不是装饰：不减的话 64 个分量全是非负数，任意两个描述子的
    余弦都在 0.9 以上，那道 `MIN_COSSIM=0.82` 的闸门就等于不存在 —— 于是测试跑的是
    一条产品里不存在的代码路径。
    """
    f = F.extract(img_bgr, n_features=xfeat.TOP_K)
    if len(f) == 0:
        return Features(
            pts=np.zeros((0, 2), np.float32),
            desc=np.zeros((0, xfeat.DESC_DIM), np.float32),
        )
    bits = np.unpackbits(f.desc, axis=1).astype(np.float32)  # (N, 256)
    desc = bits.reshape(len(f), xfeat.DESC_DIM, 4) @ _NIBBLE  # (N, 64)
    desc -= desc.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(desc, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return Features(pts=f.pts, desc=np.ascontiguousarray(desc / norms, np.float32))


def _fake_backend(model_path=None) -> backend_mod.Backend:
    return backend_mod.Backend(
        name=backend_mod.XFEAT,
        layout=backend_mod.XFEAT_LAYOUT,
        min_inliers=verify.XFEAT_MIN_INLIERS,
        dedup_min_inliers=verify.XFEAT_DEDUP_MIN_INLIERS,
        vocab_file="vocab_xfeat.npz",
        vocab_cls=(floatvocab.FloatVocab, nullvocab.NullVocab),
        _extract=fake_xfeat_extract,
        _verify=verify.verify_pair_xfeat,
        _train_vocab=floatvocab.train,
        _load_vocab=floatvocab.FloatVocab.load,
    )


@pytest.fixture
def xenv(make_env, monkeypatch):
    """一套跑在 XFeat 后端上的服务。

    走 `PHOTOAR_BACKEND` 而不是直接塞 `recog.backend`：那是真实部署选后端的路径，
    顺带把"环境变量确实能把后端播种成 xfeat"也钉住了。
    """
    monkeypatch.setattr(backend_mod, "xfeat_backend", _fake_backend)
    monkeypatch.setenv("PHOTOAR_BACKEND", backend_mod.XFEAT)
    env = make_env()
    assert env.srv.library.backend.name == backend_mod.XFEAT
    return env


def features_body(img: np.ndarray, *, jpeg: bool = True) -> dict:
    """把一张图变成端上会发的那个请求体。

    默认先过一遍长边 640 + q70 的 JPEG 编解码，与真实客户端一致（它拿到的就是相机
    编码出来的那张 JPEG）—— 也就是说查询特征与入库特征不是同一批字节。
    """
    query = F.resize_to_long_edge(img, F.LONG_EDGE)
    if jpeg:
        ok, buf = cv2.imencode(".jpg", query, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        assert ok
        query = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    h, w = query.shape[:2]
    return featurebody.encode(fake_xfeat_extract(query), h, w)


# ---- 闭环 ----


def test_ingest_then_recognize_by_features(xenv):
    """端上提特征那条路走通：入库 → 传描述子 → 命中，字段与 `/v1/recognize` 一致。"""
    img = xenv.textured(seed=5, w=1200, h=800)
    ref = xenv.nas / "photos" / "a.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    video = xenv.write_video("videos/a.mp4")
    pid = xenv.ingest_ok(ref, video=video)

    r = xenv.post_json("/v1/recognize/features", features_body(img))
    assert r.status == 200, xenv.body_json(r)
    hit = xenv.body_json(r)
    assert hit["matched"] is True and hit["photoId"] == pid
    assert hit["printWidthM"] == 0.152
    assert hit["imgdbUrl"] == f"/v1/photo/{pid}/imgdb"
    assert hit["refThumbUrl"] == f"/v1/photo/{pid}/thumb"
    assert hit["mediaUrl"] == f"/v1/photo/{pid}/media"
    assert hit["inliers"] >= 40
    assert abs(hit["refAspect"] - 1200 / 800) < 1e-3
    assert "latencyMs" in hit
    assert r.headers["Cache-Control"] == "no-store"


def test_response_shape_is_identical_to_jpeg_path(xenv):
    """两条路的响应**键集合**必须完全一样。

    客户端解析命中响应的是同一份代码（Android 侧 `ApiParse.recognize`）。多一个键
    少一个键都会表现成"换了路径之后偶发解析失败"，而两条路各自单独测都是绿的。
    """
    img = xenv.textured(seed=7)
    ref = xenv.nas / "photos" / "b.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    xenv.ingest_ok(ref, video=xenv.write_video("videos/b.mp4"))

    by_jpeg = xenv.body_json(xenv.post_frame("/v1/recognize", xenv.jpeg_of(img)))
    by_feat = xenv.body_json(xenv.post_json("/v1/recognize/features", features_body(img)))
    assert by_jpeg["matched"] is True and by_feat["matched"] is True
    assert set(by_jpeg) == set(by_feat)
    assert by_jpeg["photoId"] == by_feat["photoId"]


def test_miss_returns_200_like_the_jpeg_path(xenv):
    """未命中是正常状态（客户端每 400ms 一次），必须 200 而不是 404。"""
    ref = xenv.write_image("photos/c.jpg", seed=11)
    xenv.ingest_ok(ref)
    r = xenv.post_json(
        "/v1/recognize/features", features_body(xenv.textured(seed=98765))
    )
    assert r.status == 200
    body = xenv.body_json(r)
    assert body["matched"] is False and body["reason"] in ("weak", "empty")


def test_empty_library_is_a_miss_not_an_error(xenv):
    r = xenv.post_json("/v1/recognize/features", features_body(xenv.textured(seed=1)))
    assert r.status == 200 and xenv.body_json(r)["matched"] is False


def test_zero_keypoints_is_a_miss(xenv):
    """一面白墙会提不出关键点。那是未命中，不是 400。"""
    body = featurebody.encode(
        Features(
            pts=np.zeros((0, 2), np.float32),
            desc=np.zeros((0, xfeat.DESC_DIM), np.float32),
        ),
        480,
        640,
    )
    r = xenv.post_json("/v1/recognize/features", body)
    assert r.status == 200 and xenv.body_json(r)["matched"] is False


def test_writes_history_with_via_label(xenv):
    """两条路都要记进识别历史，否则"家里人说扫不出来"在换了路径之后就查不到了。"""
    img = xenv.textured(seed=6)
    ref = xenv.nas / "photos" / "d.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    pid = xenv.ingest_ok(ref)
    # 不能用 `post_json`：它自己占了 headers 那个关键字。直接构造请求，顺带把
    # content-type 也显式写出来。
    xenv.request(
        "POST",
        "/v1/recognize/features",
        body=json.dumps(features_body(img)).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-photoar-endpoint": "lan",
        },
    )
    entries = xenv.body_json(xenv.get("/v1/history"))["entries"]
    assert entries and entries[0]["photoId"] == pid
    assert entries[0]["via"] == "lan"


# ---- 后端判据 ----


def test_rejected_on_orb_backend(env):
    """ORB 后端上这个接口必须 400 并说清原因。

    描述子格式不兼容：硬收下只会按 ORB 的 stride 去读一段 float 缓冲，读出垃圾且
    **不报错**。
    """
    r = env.post_json(
        "/v1/recognize/features", features_body(env.textured(seed=2))
    )
    assert r.status == 400
    body = env.body_json(r)
    assert body["error"] == "unsupported_backend"
    assert body["activeBackend"] == backend_mod.ORB
    assert "orb" in body["message"].lower()


def test_backend_check_uses_the_active_backend_not_the_requested_one(
    make_env, monkeypatch
):
    """配置说 xfeat、模型不在于是回退了 ORB —— 此时必须**按实际在跑的 ORB** 拒掉。

    按配置判的话会收下一批永远匹配不上的 float 描述子，而客户端看到的是"一直未命中"。
    """

    def _boom(model_path=None):
        raise xfeat.ModelMissing("测试里没有模型")

    monkeypatch.setattr(backend_mod, "xfeat_backend", _boom)
    monkeypatch.setenv("PHOTOAR_BACKEND", backend_mod.XFEAT)
    env = make_env()
    assert env.srv.library.backend.name == backend_mod.ORB
    assert env.body_json(env.get("/v1/ping"))["backendDegraded"] is True

    r = env.post_json("/v1/recognize/features", features_body(env.textured(seed=3)))
    assert r.status == 400
    body = env.body_json(r)
    assert body["error"] == "unsupported_backend"
    assert body["activeBackend"] == backend_mod.ORB
    assert body["requestedBackend"] == backend_mod.XFEAT


# ---- 鉴权与授权 ----


def test_requires_auth(xenv):
    r = xenv.post_json(
        "/v1/recognize/features", features_body(xenv.textured(seed=4)), auth=False
    )
    assert r.status == 401


def test_unauthorized_hit_is_reported_as_forbidden_miss(xenv):
    """命中一张没授权给这个人的照片 → `matched:false, reason:"forbidden"`。

    与 `/v1/recognize` 逐字相同（连 HTTP 状态码都一样）：客户端对"没认出来"和"认出来
    但你没权限"的处理是同一种 —— 继续扫下一帧。
    """
    img = xenv.textured(seed=9)
    ref = xenv.nas / "photos" / "secret.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    pid = xenv.ingest_ok(ref)
    outsider = xenv.viewer("小红")  # 一张都没授权

    r = xenv.post_json("/v1/recognize/features", features_body(img), as_=outsider)
    assert r.status == 200
    body = xenv.body_json(r)
    assert body["matched"] is False and body["reason"] == "forbidden"

    # 历史里仍然如实记着命中的是哪张（admin only，不会泄露给他）
    entries = xenv.body_json(xenv.get("/v1/history"))["entries"]
    assert entries[0]["photoId"] == pid


def test_granted_viewer_gets_the_hit(xenv):
    img = xenv.textured(seed=10)
    ref = xenv.nas / "photos" / "shared.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    pid = xenv.ingest_ok(ref)
    guest = xenv.viewer("小刚", photo_ids=(pid,))
    body = xenv.body_json(
        xenv.post_json("/v1/recognize/features", features_body(img), as_=guest)
    )
    assert body["matched"] is True and body["photoId"] == pid


# ---- 阈值走同一套热配置 ----


def test_min_inliers_from_hot_config_applies(xenv):
    """把 `recog.min_inliers` 调到一个够不到的值，这条路也必须跟着不命中。

    两条识别路径共用 `_decide_and_respond`，这条测试就是在钉那件事 —— 如果新接口
    自己抄了一遍判定，管理台上改阈值只会影响传 JPEG 那条路。
    """
    img = xenv.textured(seed=12)
    ref = xenv.nas / "photos" / "e.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    xenv.ingest_ok(ref)
    assert xenv.body_json(
        xenv.post_json("/v1/recognize/features", features_body(img))
    )["matched"] is True

    assert xenv.patch_json("/v1/admin/config", {"recog.min_inliers": 500}).status == 200
    body = xenv.body_json(
        xenv.post_json("/v1/recognize/features", features_body(img))
    )
    assert body["matched"] is False and body["reason"] == "weak"


# ---- 请求体上限 ----


def test_body_limit_is_bigger_than_a_legal_request(xenv):
    """一个满编（512 点）的合法请求必须过得去。

    `MAX_JSON_BYTES` 是 64KB，而合法请求约 180KB —— 沿用它会把这个接口的每一次调用
    都 413 掉。这条测试就是拦住"顺手把上限改回 MAX_JSON_BYTES"。
    """
    from photoar.server.config import MAX_FEATURES_BYTES, MAX_JSON_BYTES

    body = features_body(xenv.textured(seed=13))
    assert len(body["descriptors"]) > MAX_JSON_BYTES, "样本没到满编，这条测试没意义"
    assert MAX_FEATURES_BYTES > MAX_JSON_BYTES
    r = xenv.post_json("/v1/recognize/features", body)
    assert r.status == 200


def test_oversized_body_is_413(xenv):
    from photoar.server.config import MAX_FEATURES_BYTES

    body = {
        "width": 640,
        "height": 480,
        "keypoints": "A" * 8,
        "descriptors": "A" * (MAX_FEATURES_BYTES + 10),
    }
    assert xenv.post_json("/v1/recognize/features", body).status == 413


# ---- 方法与路由 ----


def test_get_is_405(xenv):
    assert xenv.get("/v1/recognize/features").status == 405


def test_plain_recognize_still_works_on_xfeat_backend(xenv):
    """传 JPEG 那条路在 XFeat 后端上照样是可用的 —— 端上提特征是**可选**的加速，
    不是替代。客户端的默认路径仍然是它。"""
    img = xenv.textured(seed=14)
    ref = xenv.nas / "photos" / "f.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(ref), img)
    pid = xenv.ingest_ok(ref)
    body = xenv.body_json(xenv.post_frame("/v1/recognize", xenv.jpeg_of(img)))
    assert body["matched"] is True and body["photoId"] == pid


# ---- GET /v1/model/xfeat ----


def _write_model(env, payload: bytes = b"ONNX-fake-model-bytes") -> None:
    path = env.cfg.xfeat_model_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def test_model_download_requires_auth(env):
    _write_model(env)
    assert env.get("/v1/model/xfeat", auth=False).status == 401


def test_model_download_is_404_when_absent(env):
    r = env.get("/v1/model/xfeat")
    assert r.status == 404
    body = env.body_json(r)
    # 客户端要能按 code 判断"服务端没有模型"→ 静默退回传 JPEG，而不是弹错误
    assert body["error"] == "model_missing"
    assert "xfeat.onnx" in body["message"]


def test_model_download_serves_bytes_with_etag(env):
    _write_model(env, b"\x08\x01" + b"weights" * 100)
    r = env.get("/v1/model/xfeat")
    assert r.status == 200
    assert env.body_bytes(r) == b"\x08\x01" + b"weights" * 100
    assert r.headers["Content-Type"] == "application/octet-stream"
    etag = r.headers["ETag"]
    assert etag

    # 带上 ETag 应当 304 空体 —— 端上那份缓存靠它避免每次扫描前重下 4.31MB
    again = env.get("/v1/model/xfeat", headers={"if-none-match": etag})
    assert again.status == 304
    assert again.body == b""


def test_model_download_is_not_immutable(env):
    """模型是**可以被换掉**的（换一份重启服务），所以不能 immutable。

    immutable 的后果：换了模型之后手机上永远还是旧的那份，而库里的描述子已经是新
    模型提的 —— 描述子对不上，识别率静默下降，且没有任何地方看得出来。
    """
    _write_model(env)
    cache = env.get("/v1/model/xfeat").headers["Cache-Control"]
    assert "immutable" not in cache
    assert cache == "no-cache"


def test_model_download_allowed_for_viewer(env):
    """需要模型的正是拿手机扫照片的 viewer，不能是 admin only。"""
    _write_model(env)
    guest = env.viewer("小美")
    assert env.get("/v1/model/xfeat", as_=guest).status == 200


def test_model_download_base64_roundtrip_is_byte_exact(env):
    """顺手钉一下 `featurebody.encode`/`parse` 的往返是逐字节精确的。

    base64 的 float32 往返如果有任何损失，`_check_norms` 那道 1e-2 的线就会开始
    偶发误伤，而那看起来会像"客户端有时候归一化不对"。
    """
    _write_model(env)
    rng = np.random.default_rng(0)
    desc = rng.standard_normal((37, xfeat.DESC_DIM)).astype(np.float32)
    desc /= np.linalg.norm(desc, axis=1, keepdims=True)
    pts = rng.uniform(0, 400, (37, 2)).astype(np.float32)
    doc = featurebody.encode(Features(pts=pts, desc=desc), 480, 640)
    back = featurebody.parse(doc)
    assert np.array_equal(back.pts, pts)
    assert np.array_equal(back.desc, desc)
    assert base64.b64decode(doc["descriptors"]) == desc.tobytes()
