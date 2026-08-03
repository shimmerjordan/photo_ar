"""`DELETE /v1/photo/{id}`，以及未命中原因进历史。

两件事都是同一次真实排查的产物：库里进了两张同一内容的照片，比值检验把两张都判成
ambiguous，941 帧真机记录只命中 44 帧 —— 而那张历史表里只有 `inliers` 一列，
897 条内点数 160~229（门槛 40）的失败**完全看不出**是被哪一条判据挡住的。

所以要有两样东西：一条能把重复删掉的路（在此之前唯一的出路是重建整个库），
和一列能说清「为什么没命中」的记录。
"""

import cv2
import pytest

from photoar.server import app


@pytest.fixture
def env(make_env):
    return make_env()


def test_删掉之后列表和识别里都没有它了(env, tmp_path, textured_image):
    ref = _ref(env, "a.png", textured_image(seed=3))
    pid = env.ingest_ok(ref)

    resp = env.request("DELETE", f"/v1/photo/{pid}")
    assert resp.status == 200, env.body_json(resp)
    assert env.body_json(resp)["deleted"] is True

    listing = env.body_json(env.request("GET", "/v1/photos"))["photos"]
    assert [p["photoId"] for p in listing] == []
    assert env.request("GET", f"/v1/photo/{pid}").status in (403, 404)
    assert env.srv.library.photo_ids() == []


def test_删掉一张不影响另一张(env, tmp_path, textured_image):
    # 墓碑方案唯一要证明的东西：slot 下标不平移。真删（往前挪）会让 photo_id 与
    # slot 错开一格，而错位不报错 —— 命中之后播的是别人的视频。
    keep = env.ingest_ok(_ref(env, "keep.png", textured_image(seed=4)))
    drop = env.ingest_ok(_ref(env, "drop.png", textured_image(seed=5)))
    assert env.request("DELETE", f"/v1/photo/{drop}").status == 200

    detail = env.request("GET", f"/v1/photo/{keep}")
    assert detail.status == 200, env.body_json(detail)
    assert env.srv.library.photo_ids() == [keep]


def test_重复删除不报错(env, tmp_path, textured_image):
    # 管理台上双击一下就会发两次。第二次回 404 只会弹一个没有意义的错。
    pid = env.ingest_ok(_ref(env, "a.png", textured_image(seed=6)))
    assert env.request("DELETE", f"/v1/photo/{pid}").status == 200
    assert env.request("DELETE", f"/v1/photo/{pid}").status in (403, 404)


def test_删掉之后同一张能重新入库(env, tmp_path, textured_image):
    """「删掉再传一次」是用户手上唯一的修复路径，它必须真的走得通。

    退役的照片还留在 desc.bin 里，如果去重闸门仍然把它算进候选，重新入库会被判
    near_duplicate —— 那等于删除功能只删了一半。
    """
    ref = _ref(env, "same.png", textured_image(seed=7))
    first = env.ingest_ok(ref)
    assert env.request("DELETE", f"/v1/photo/{first}").status == 200

    resp = env.ingest(ref)
    assert resp.status == 201, f"删掉之后本该能重新传：{env.body_json(resp)}"
    assert env.body_json(resp)["photoId"] != first, "重新入库是新的 photoId"


def test_访客不能删(env, tmp_path, textured_image):
    pid = env.ingest_ok(_ref(env, "a.png", textured_image(seed=8)))
    guest = env.viewer("客人")
    resp = env.request("DELETE", f"/v1/photo/{pid}", as_=guest)
    assert resp.status == 403, env.body_json(resp)
    assert env.srv.library.photo_ids() == [pid], "拒绝之后库里必须一点没动"


# ---------------------------------------------------------------- 历史里的原因


def test_未命中的原因进历史(env, tmp_path, textured_image):
    """`weak` 与 `ambiguous` 修法毫不相干：前者要改取景，后者要清库。

    只记 inliers 的话，941 帧失败里分不出是哪一种 —— 而那正是这次排查卡住的地方。
    """
    env.ingest_ok(_ref(env, "a.png", textured_image(seed=9)))
    other = textured_image(seed=4242)
    resp = env.request(
        "POST",
        "/v1/recognize",
        body=_multipart(_ref(env, "q.png", other).read_bytes()),
        headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
    )
    assert resp.status == 200
    assert env.body_json(resp)["matched"] is False

    entries = env.body_json(env.request("GET", "/v1/history"))["entries"]
    assert entries, "未命中也要留一条"
    top = entries[0]
    assert top["reason"], f"未命中必须带原因：{top}"
    assert "runnerUp" in top and top["runnerUp"] is not None
    assert top["topk"] is not None, "前几名候选也要留，否则看不出差多少"


def test_命中的记录也带原因(env, tmp_path, textured_image):
    ref = _ref(env, "a.png", textured_image(seed=11))
    pid = env.ingest_ok(ref)
    resp = env.request(
        "POST",
        "/v1/recognize",
        body=_multipart(ref.read_bytes()),
        headers={"content-type": f"multipart/form-data; boundary={_BOUNDARY}"},
    )
    assert env.body_json(resp)["matched"] is True
    top = env.body_json(env.request("GET", "/v1/history"))["entries"][0]
    assert top["photoId"] == pid
    assert top["reason"] == "ok"


def _ref(env, name, img):
    """参考图必须落在白名单根目录内（`env.nas/photos`），否则入库回 403 path_denied。"""
    path = env.nas / "photos" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), img)
    return path


_BOUNDARY = "----photoarTest"


def _multipart(jpeg: bytes) -> bytes:
    head = (
        f"--{_BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()
    return head + jpeg + f"\r\n--{_BOUNDARY}--\r\n".encode()
