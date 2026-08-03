"""素材挂载点：CRUD、白名单热重建、浏览、取文件；以及重复文件的映射反查。

两组重点：

1. **热重建白名单**。加一个 local 挂载点之后，它下面的文件要能立刻被 `/v1/fs/list` 浏览、
   能直接入库；删掉之后要立刻不行 —— 而 `PHOTOAR_ROOTS` 里那几个根**始终**在。
   （第一版差点写成「重建时只用挂载点」，那样删一个挂载点会把环境变量给的根一起弄丢。）

2. **重复上传不是死胡同**。同名同内容直接复用那条路径；而 `/v1/admin/lookup` 要能说出
   这个文件在库里的身份 —— 照片最多一张（一张照片只有一个参考图），视频可以是多张。
"""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def mounts_of(env) -> list[dict]:
    return env.body_json(env.get("/v1/admin/mounts"))["mounts"]


def make_local(env, name: str, path) -> str:
    r = env.post_json(
        "/v1/admin/mounts", {"name": name, "kind": "local", "location": str(path)}
    )
    assert r.status == 201, env.body_json(r)
    return env.body_json(r)["id"]


# ---------------------------------------------------------------- CRUD


def test_空的时候只有环境变量给的根(env):
    doc = env.body_json(env.get("/v1/admin/mounts"))
    assert doc["mounts"] == []
    # 环境变量那几个要列出来（只读）。不列的话管理台上会出现「我明明配了，
    # 怎么这里是空的」这种困惑。
    assert doc["envRoots"], "PHOTOAR_ROOTS 给的根应该列出来"
    assert any(str(env.nas) in r["path"] for r in doc["envRoots"])


def test_建一个本地挂载点(env, tmp_path):
    d = tmp_path / "extra"
    d.mkdir()
    mid = make_local(env, "额外素材", d)
    (row,) = mounts_of(env)
    assert row["id"] == mid
    assert row["name"] == "额外素材"
    assert row["kind"] == "local"
    assert row["location"] == str(d.resolve())
    assert row["enabled"] is True
    assert row["hasPassword"] is False


def test_重名被拒(env, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    make_local(env, "同名", d)
    r = env.post_json(
        "/v1/admin/mounts", {"name": "同名", "kind": "local", "location": str(d)}
    )
    assert r.status == 409
    assert env.body_json(r)["error"] == "name_taken"


def test_本地路径必须存在_并说清是容器内路径(env):
    r = env.post_json(
        "/v1/admin/mounts",
        {"name": "打错了", "kind": "local", "location": "/media/photo"},
    )
    assert r.status == 404
    doc = env.body_json(r)
    assert doc["error"] == "location_not_found"
    # 这是最常见的真实原因：填了宿主机路径，或者路径打错一个字
    assert "容器内" in doc["message"]


def test_本地路径不能是文件(env):
    f = env.write_image("photos/x.jpg", seed=1)
    r = env.post_json(
        "/v1/admin/mounts", {"name": "是文件", "kind": "local", "location": str(f)}
    )
    assert r.status == 400
    assert "目录" in env.body_json(r)["message"]


def test_本地路径必须是绝对路径(env):
    r = env.post_json(
        "/v1/admin/mounts", {"name": "相对", "kind": "local", "location": "photos"}
    )
    assert r.status == 400
    assert env.body_json(r)["error"] == "bad_location"


def test_webdav_地址必须带协议_并给出常见_NAS_的写法(env):
    r = env.post_json(
        "/v1/admin/mounts", {"name": "dav", "kind": "webdav", "location": "nas/dav"}
    )
    assert r.status == 400
    msg = env.body_json(r)["message"]
    assert "http://" in msg
    # 光说「要带协议」不够用 —— 人不知道自己 NAS 的 WebDAV 地址长什么样
    assert "Nextcloud" in msg or "群晖" in msg


def test_kind_只能是两种(env, tmp_path):
    r = env.post_json(
        "/v1/admin/mounts", {"name": "smb", "kind": "smb", "location": "//nas/photo"}
    )
    assert r.status == 400
    assert env.body_json(r)["error"] == "bad_kind"


def test_改名与启停(env, tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    mid = make_local(env, "旧名", d)
    r = env.patch_json(f"/v1/admin/mounts/{mid}", {"name": "新名", "enabled": False})
    assert r.status == 200
    row = env.body_json(r)
    assert row["name"] == "新名" and row["enabled"] is False


def test_口令永不回显(env):
    r = env.post_json(
        "/v1/admin/mounts",
        {
            "name": "dav",
            "kind": "webdav",
            "location": "https://nas.example/dav",
            "username": "u",
            "password": "秘密口令",
        },
    )
    assert r.status == 201
    assert b"\xe7\xa7\x98\xe5\xaf\x86" not in r.body, "口令不该回显"
    assert env.body_json(r)["hasPassword"] is True
    assert "password" not in env.body_json(r)
    # 列表里也不能有
    assert b"\xe7\xa7\x98\xe5\xaf\x86" not in env.get("/v1/admin/mounts").body


def test_改别的字段不会把口令抹掉(env):
    # 管理台的口令框是空的（服务端不回显），提交上来的 None 意思是「我没改口令」。
    # 混成「清空」的话，改一次名字就会让这个挂载点第二天浏览失败。
    mid = env.body_json(
        env.post_json(
            "/v1/admin/mounts",
            {
                "name": "dav",
                "kind": "webdav",
                "location": "https://nas.example/dav",
                "username": "u",
                "password": "pw",
            },
        )
    )["id"]
    env.patch_json(f"/v1/admin/mounts/{mid}", {"name": "dav2"})
    (row,) = mounts_of(env)
    assert row["hasPassword"] is True, "改名不该把口令抹掉"


def test_不存在的挂载点(env):
    assert env.get("/v1/admin/mounts/" + "f" * 32 + "/list").status == 404
    assert env.request("DELETE", "/v1/admin/mounts/nope").status == 404


# ---------------------------------------------------------------- 白名单热重建


def test_加了本地挂载点之后立刻能浏览_不用重启(env, tmp_path):
    outside = tmp_path / "newsource"
    outside.mkdir()
    (outside / "hi.txt").write_text("x")

    # 加之前：不在白名单里
    before = env.get(f"/v1/fs/list?path={outside}")
    assert before.status == 403, "加之前就该是白名单外"

    make_local(env, "新素材", outside)

    after = env.get(f"/v1/fs/list?path={outside}")
    assert after.status == 200, "加完应该立刻能浏览，不用重启"
    assert [e["name"] for e in env.body_json(after)["entries"]] == ["hi.txt"]


def test_环境变量给的根始终在_不会被挂载点重建弄丢(env, tmp_path):
    # 第一版差点写成「重建时只用挂载点」，那样删一个挂载点会把 PHOTOAR_ROOTS
    # 里的根一起弄丢 —— 而那意味着整个库突然全部读不到。
    d = tmp_path / "extra"
    d.mkdir()
    mid = make_local(env, "额外", d)
    assert env.get(f"/v1/fs/list?path={env.nas / 'photos'}").status == 200
    env.request("DELETE", f"/v1/admin/mounts/{mid}")
    assert env.get(f"/v1/fs/list?path={env.nas / 'photos'}").status == 200, \
        "删挂载点之后环境变量给的根必须还在"


def test_删了挂载点之后不再能浏览(env, tmp_path):
    outside = tmp_path / "gone"
    outside.mkdir()
    mid = make_local(env, "会被删", outside)
    assert env.get(f"/v1/fs/list?path={outside}").status == 200
    env.request("DELETE", f"/v1/admin/mounts/{mid}")
    assert env.get(f"/v1/fs/list?path={outside}").status == 403


def test_停用的挂载点不在白名单里(env, tmp_path):
    outside = tmp_path / "off"
    outside.mkdir()
    mid = make_local(env, "停用", outside)
    assert env.get(f"/v1/fs/list?path={outside}").status == 200
    env.patch_json(f"/v1/admin/mounts/{mid}", {"enabled": False})
    assert env.get(f"/v1/fs/list?path={outside}").status == 403


def test_停用的挂载点不能浏览也不能取文件(env, tmp_path):
    outside = tmp_path / "off2"
    outside.mkdir()
    mid = make_local(env, "停用2", outside)
    env.patch_json(f"/v1/admin/mounts/{mid}", {"enabled": False})
    r = env.get(f"/v1/admin/mounts/{mid}/list")
    assert r.status == 409 and env.body_json(r)["error"] == "mount_disabled"
    r = env.post_json(f"/v1/admin/mounts/{mid}/fetch", {"path": "x.jpg"})
    assert r.status == 409


def test_挂载点里的文件能直接入库(env, tmp_path):
    # 这才是「配好挂载路径即可获取到」的意思：不只是能看见，是能拿它入库。
    outside = tmp_path / "src"
    outside.mkdir()
    import cv2

    img = env.textured(seed=7, w=1200, h=800)
    ref = outside / "from-mount.jpg"
    assert cv2.imwrite(str(ref), img)

    make_local(env, "外部素材", outside)
    r = env.post_json("/v1/photo", {"refPath": str(ref)})
    assert r.status == 201, env.body_json(r)


# ---------------------------------------------------------------- 浏览本地挂载点


def test_浏览本地挂载点_路径是相对挂载点根的(env, tmp_path):
    # 管理台只该知道「在这个挂载点的哪一层」，不需要也不该拿到服务端的绝对路径。
    src = tmp_path / "m"
    (src / "sub").mkdir(parents=True)
    (src / "a.jpg").write_bytes(b"x")
    (src / "sub" / "b.mp4").write_bytes(b"y")
    mid = make_local(env, "m", src)

    root = env.body_json(env.get(f"/v1/admin/mounts/{mid}/list"))
    assert root["path"] == ""
    assert root["parent"] is None, "根没有上一级"
    assert [e["name"] for e in root["entries"]] == ["sub", "a.jpg"]

    deeper = env.body_json(env.get(f"/v1/admin/mounts/{mid}/list?path=sub"))
    assert deeper["path"] == "sub"
    assert deeper["parent"] == ""
    assert [e["name"] for e in deeper["entries"]] == ["b.mp4"]


def test_浏览挂载点时穿越被挡住(env, tmp_path):
    src = tmp_path / "m2"
    src.mkdir()
    mid = make_local(env, "m2", src)
    r = env.get(f"/v1/admin/mounts/{mid}/list?path=../../etc")
    assert r.status in (403, 404), f"穿越应该被挡，拿到 {r.status}"


def test_从本地挂载点取文件不拷贝(env, tmp_path):
    # 文件本来就在服务端的文件系统上，拷一份只是白占一倍磁盘 —— 而这个部署形态下
    # 磁盘就是 NAS 的磁盘。
    src = tmp_path / "m3"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"x")
    mid = make_local(env, "m3", src)
    r = env.post_json(f"/v1/admin/mounts/{mid}/fetch", {"path": "a.jpg"})
    assert r.status == 200
    doc = env.body_json(r)
    assert doc["copied"] is False
    assert doc["path"] == str((src / "a.jpg").resolve())


# ---------------------------------------------------------------- WebDAV 挂载点


class _Dav(BaseHTTPRequestHandler):
    files = {"/dav/pic.jpg": b"PIC" * 300}

    def log_message(self, *a):
        pass

    def do_PROPFIND(self):  # noqa: N802
        body = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
            "<d:response><d:href>/dav/</d:href><d:propstat><d:prop>"
            "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "<d:response><d:href>/dav/pic.jpg</d:href><d:propstat><d:prop>"
            "<d:resourcetype/><d:getcontentlength>900</d:getcontentlength>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "</d:multistatus>"
        ).encode()
        self.send_response(207)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        data = self.files.get(self.path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def dav_url():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Dav)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/dav"
    srv.shutdown()
    srv.server_close()


def test_webdav_挂载点不进白名单(env, dav_url):
    # 它不在本地文件系统上。进白名单是没有意义的（`Roots` 只认路径），而且
    # `Roots` 构造时会 resolve，一个 URL 会被当成相对路径解析成一个奇怪的绝对路径。
    env.post_json(
        "/v1/admin/mounts",
        {"name": "dav", "kind": "webdav", "location": dav_url},
    )
    # 加完之后既有的根还能用（说明重建没被 URL 搞坏）
    assert env.get(f"/v1/fs/list?path={env.nas / 'photos'}").status == 200


def test_浏览_webdav_挂载点_形状与本地一致(env, dav_url):
    # 形状一样，管理台上一个文件浏览器就能同时用在两种挂载点上。
    mid = env.body_json(
        env.post_json(
            "/v1/admin/mounts",
            {"name": "dav", "kind": "webdav", "location": dav_url},
        )
    )["id"]
    doc = env.body_json(env.get(f"/v1/admin/mounts/{mid}/list"))
    assert set(doc) >= {"path", "parent", "entries"}
    (entry,) = doc["entries"]
    assert entry["name"] == "pic.jpg"
    assert entry["isDir"] is False
    assert entry["kind"] == "image"
    assert entry["bytes"] == 900


def test_从_webdav_取文件会落到本地(make_env, tmp_path, dav_url):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    mid = env.body_json(
        env.post_json(
            "/v1/admin/mounts",
            {"name": "dav", "kind": "webdav", "location": dav_url},
        )
    )["id"]
    r = env.post_json(f"/v1/admin/mounts/{mid}/fetch", {"path": "pic.jpg"})
    assert r.status == 201, env.body_json(r)
    doc = env.body_json(r)
    assert doc["copied"] is True
    assert doc["bytes"] == 900
    from pathlib import Path

    assert Path(doc["path"]).read_bytes() == b"PIC" * 300


def test_webdav_取同一个文件两次_第二次复用(make_env, tmp_path, dav_url):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    mid = env.body_json(
        env.post_json(
            "/v1/admin/mounts",
            {"name": "dav", "kind": "webdav", "location": dav_url},
        )
    )["id"]
    first = env.body_json(env.post_json(f"/v1/admin/mounts/{mid}/fetch", {"path": "pic.jpg"}))
    second = env.post_json(f"/v1/admin/mounts/{mid}/fetch", {"path": "pic.jpg"})
    assert second.status == 200
    doc = env.body_json(second)
    assert doc["copied"] is False, "同名同内容该复用，不该报错也不该再下一遍"
    assert doc["path"] == first["path"]


def test_webdav_连不上时是_502_而不是_500(env):
    # 上游的问题不该表现成「我们坏了」。
    mid = env.body_json(
        env.post_json(
            "/v1/admin/mounts",
            {"name": "dead", "kind": "webdav", "location": "http://127.0.0.1:1/dav"},
        )
    )["id"]
    r = env.get(f"/v1/admin/mounts/{mid}/list")
    assert r.status == 502
    assert env.body_json(r)["error"] == "webdav_unreachable"


# ---------------------------------------------------------------- 上传去重


def test_同名同内容直接复用_不再死胡同(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    # 从手机相册第二次挑同一张照片，拿到的就是同一个文件名。原来这里是 409 死胡同。
    body = b"IDENTICAL-BYTES" * 100
    first = env.request("POST", "/v1/upload?name=same.jpg", body=body)
    assert first.status == 201
    assert env.body_json(first)["reused"] is False

    second = env.request("POST", "/v1/upload?name=same.jpg", body=body)
    assert second.status == 200
    doc = env.body_json(second)
    assert doc["reused"] is True
    assert doc["path"] == env.body_json(first)["path"]


def test_同名不同内容仍然拒绝_并给出建议名(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    # 这一条**必须**还是拒绝：直接覆盖会悄悄换掉别人的素材，而已入库的照片指着
    # 那条路径。
    env.request("POST", "/v1/upload?name=clash.jpg", body=b"AAA" * 100)
    r = env.request("POST", "/v1/upload?name=clash.jpg", body=b"BBB" * 100)
    assert r.status == 409
    doc = env.body_json(r)
    assert doc["error"] == "name_taken"
    assert "内容不一样" in doc["message"]
    assert doc["suggestedName"] == "clash-2.jpg"


def test_复用时不会留下临时文件(make_env, tmp_path):
    inbox = tmp_path / "nas" / "videos"
    env = make_env(upload_dir_root=str(inbox))
    body = b"X" * 500
    env.request("POST", "/v1/upload?name=tmp.jpg", body=body)
    env.request("POST", "/v1/upload?name=tmp.jpg", body=body)
    leftovers = [p.name for p in inbox.iterdir() if ".upload-" in p.name]
    assert leftovers == [], f"留下了临时文件：{leftovers}"


# ---------------------------------------------------------------- 重复的映射反查


def test_lookup_没入过库的文件(env):
    f = env.write_image("photos/loose.jpg", seed=3)
    doc = env.body_json(env.get(f"/v1/admin/lookup?path={f}"))
    assert doc["exists"] is True
    assert doc["kind"] == "image"
    assert doc["assetId"] is None
    assert doc["photo"] is None
    assert doc["usedByPhotos"] == []


def test_lookup_一张已入库照片的参考图(env):
    # 这是「重复上传照片」时要显示的东西：它是哪张照片、现在配的是哪段视频。
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(ref, video=vid, title="婚礼合照")

    doc = env.body_json(env.get(f"/v1/admin/lookup?path={ref}"))
    assert doc["photo"] is not None
    assert doc["photo"]["photoId"] == pid
    assert doc["photo"]["title"] == "婚礼合照"
    assert doc["photo"]["videoPath"] == str(vid), "要说出它现在配的是哪段视频"
    assert doc["usedByPhotos"] == [], "它是参考图，不是别人的视频"


def test_lookup_一段被多张照片用的视频(env):
    # 一张照片只能配一个视频，但一段视频可以被多张照片用 —— 所以这一侧是列表。
    vid = env.write_video("videos/shared.mp4")
    p1 = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid, title="甲")
    p2 = env.ingest_ok(env.write_image("photos/b.jpg", seed=2), video=vid, title="乙")

    doc = env.body_json(env.get(f"/v1/admin/lookup?path={vid}"))
    assert doc["kind"] == "video"
    assert doc["photo"] is None, "视频不是任何人的参考图"
    assert {p["photoId"] for p in doc["usedByPhotos"]} == {p1, p2}
    assert {p["title"] for p in doc["usedByPhotos"]} == {"甲", "乙"}


def test_lookup_照片不会出现在自己的_usedByPhotos_里(env):
    # `photos_referencing_asset` 三列都查（ref / video / playable），不排掉 ref 的话
    # 一张照片会把自己列成「用这个视频的照片」。
    ref = env.write_image("photos/a.jpg", seed=1)
    pid = env.ingest_ok(ref)
    doc = env.body_json(env.get(f"/v1/admin/lookup?path={ref}"))
    assert doc["photo"]["photoId"] == pid
    assert doc["usedByPhotos"] == []


def test_lookup_磁盘上没有但库里有的文件(env):
    # 文件被移走了。lookup 仍然要能说出它在库里是什么身份 —— 那正是排查
    # 「为什么这张扫不出来」时要问的。
    ref = env.write_image("photos/vanish.jpg", seed=4)
    pid = env.ingest_ok(ref)
    ref.unlink()
    doc = env.body_json(env.get(f"/v1/admin/lookup?path={ref}"))
    assert doc["exists"] is False
    assert doc["photo"]["photoId"] == pid


def test_lookup_缺参数与白名单外(env):
    assert env.get("/v1/admin/lookup").status == 400
    r = env.get(f"/v1/admin/lookup?path={env.outside / 'secret.jpg'}")
    assert r.status == 403


# ---------------------------------------------------------- 上传前校验（按内容）


def check(env, name: str, data: bytes | None = None, sha: str | None = None):
    import hashlib

    body = {"name": name}
    if sha is not None:
        body["sha256"] = sha
    elif data is not None:
        body["sha256"] = hashlib.sha256(data).hexdigest()
    return env.post_json("/v1/upload/check", body)


def test_上传前校验_全新文件(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    doc = env.body_json(check(env, "brand-new.jpg", b"NEW"))
    assert doc["nameTaken"] is False
    assert doc["knownContent"] is False
    assert doc["matches"] == []
    assert doc["suggestedName"] is None


def test_上传前校验_同名同内容(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    body = b"SAME" * 50
    env.request("POST", "/v1/upload?name=a.jpg", body=body)
    doc = env.body_json(check(env, "a.jpg", body))
    assert doc["nameTaken"] is True
    assert doc["sameContent"] is True, "同名同内容 = 可以直接复用，不用再传"
    assert doc["suggestedName"] is None


def test_上传前校验_同名不同内容_给建议名(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    env.request("POST", "/v1/upload?name=a.jpg", body=b"AAA" * 50)
    doc = env.body_json(check(env, "a.jpg", b"BBB" * 50))
    assert doc["nameTaken"] is True
    assert doc["sameContent"] is False
    assert doc["suggestedName"] == "a-2.jpg"


def test_上传前校验_按内容认出已入库的照片(make_env, tmp_path):
    """这一条才是这个接口的主要价值。

    相册第二次导出同一张照片，**文件名可能变了，内容不会变**。按名字查不出来，按内容能。
    而且要直接说出它在库里是什么身份 —— 用户接下来要决定的正是「那张照片现在配的是
    哪段视频」。
    """
    import hashlib

    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(ref, video=vid, title="婚礼合照")

    # 换一个完全不同的文件名，只有内容一样
    sha = hashlib.sha256(ref.read_bytes()).hexdigest()
    doc = env.body_json(check(env, "IMG_9999.jpg", sha=sha))
    assert doc["nameTaken"] is False, "名字确实是新的"
    assert doc["knownContent"] is True, "但内容库里已经有了"
    (m,) = doc["matches"]
    assert m["path"] == str(ref)
    assert m["photo"]["photoId"] == pid
    assert m["photo"]["title"] == "婚礼合照"
    assert m["photo"]["videoPath"] == str(vid), "要说出它现在配的是哪段视频"


def test_上传前校验_按内容认出已在用的视频(make_env, tmp_path):
    # 一段视频可以被多张照片用，所以这里要给出那个列表。
    import hashlib

    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    vid = env.write_video("videos/shared.mp4")
    p1 = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid, title="甲")
    p2 = env.ingest_ok(env.write_image("photos/b.jpg", seed=2), video=vid, title="乙")

    sha = hashlib.sha256(vid.read_bytes()).hexdigest()
    doc = env.body_json(check(env, "VID_0001.mp4", sha=sha))
    assert doc["knownContent"] is True
    (m,) = doc["matches"]
    assert m["kind"] == "video"
    assert m["photo"] is None, "视频不是任何照片的参考图"
    assert {p["photoId"] for p in m["usedByPhotos"]} == {p1, p2}


def test_上传前校验_不给哈希时只做按名字那一半(make_env, tmp_path):
    # 老版本 App 不会算哈希。「少一半信息」比「整个接口用不了」好。
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    env.request("POST", "/v1/upload?name=a.jpg", body=b"AAA")
    doc = env.body_json(env.post_json("/v1/upload/check", {"name": "a.jpg"}))
    assert doc["nameTaken"] is True
    assert doc["sameContent"] is False, "没哈希就没法说内容一样"
    assert doc["knownContent"] is False
    assert doc["suggestedName"] == "a-2.jpg"


def test_上传前校验_哈希格式不对(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    r = env.post_json("/v1/upload/check", {"name": "a.jpg", "sha256": "不是哈希"})
    assert r.status == 400
    assert env.body_json(r)["error"] == "bad_sha256"


def test_上传前校验_要管理员(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    v = env.viewer()
    r = env.post_json("/v1/upload/check", {"name": "a.jpg"}, as_=v)
    assert r.status == 403


def test_上传前校验_不许被缓存(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    r = check(env, "a.jpg", b"x")
    assert r.headers["Cache-Control"] == "no-store"


def test_lookup_与_upload_check_对同一个文件的说法一致(make_env, tmp_path):
    # 用户在上传前看到「这是某张照片的参考图」，传完之后在别处看到不一样的说法，
    # 比不说更糟。两处共用 `_identity_of_asset`，这条盯着它。
    import hashlib

    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    env.ingest_ok(ref, video=vid, title="同一张")

    sha = hashlib.sha256(ref.read_bytes()).hexdigest()
    (m,) = env.body_json(check(env, "whatever.jpg", sha=sha))["matches"]
    looked = env.body_json(env.get(f"/v1/admin/lookup?path={ref}"))
    assert m["photo"] == looked["photo"]
    assert m["usedByPhotos"] == looked["usedByPhotos"]
    assert m["assetId"] == looked["assetId"]


# ------------------------------------------- 传上来但还没入库的素材（inbox）


def test_inbox_列出还没被用起来的文件(make_env, tmp_path):
    """手机传上来的文件先落地、然后才入库。中间断了的话它躺在那儿，而管理台上原来
    **任何一处都看不到它** —— 用户看到的是「我传上去了，但哪儿都找不到」。"""
    inbox = tmp_path / "nas" / "videos"
    env = make_env(upload_dir_root=str(inbox))
    env.request("POST", "/v1/upload?name=orphan.jpg", body=b"IMG" * 100)
    env.request("POST", "/v1/upload?name=orphan.mp4", body=b"VID" * 100)

    doc = env.body_json(env.get("/v1/admin/inbox"))
    assert doc["dir"] == str(inbox)
    names = {f["name"]: f for f in doc["files"]}
    assert "orphan.jpg" in names and "orphan.mp4" in names
    assert names["orphan.jpg"]["kind"] == "image"
    assert names["orphan.mp4"]["kind"] == "video"
    assert names["orphan.jpg"]["bytes"] == 300


def test_inbox_已经用起来的不再列出(make_env, tmp_path):
    # 已入库的照片、已配上的视频在照片列表里看得到，重复列在这儿只会让人以为出了问题。
    import cv2

    inbox = tmp_path / "nas" / "videos"
    env = make_env(upload_dir_root=str(inbox))
    img = env.textured(seed=5, w=1200, h=800)
    ref = inbox / "used.jpg"
    assert cv2.imwrite(str(ref), img)
    assert [f["name"] for f in env.body_json(env.get("/v1/admin/inbox"))["files"]] == ["used.jpg"]

    env.ingest_ok(ref)
    assert env.body_json(env.get("/v1/admin/inbox"))["files"] == []


def test_inbox_不列既不是图也不是视频的东西(make_env, tmp_path):
    # `.upload-xxx` 临时文件、`.DS_Store` 之类。列出来只是噪声 —— 用户对它们无事可做。
    inbox = tmp_path / "nas" / "videos"
    env = make_env(upload_dir_root=str(inbox))
    (inbox / "notes.txt").write_text("x")
    (inbox / "a.jpg.upload-123-456").write_bytes(b"half")
    env.request("POST", "/v1/upload?name=real.jpg", body=b"IMG")
    assert [f["name"] for f in env.body_json(env.get("/v1/admin/inbox"))["files"]] == ["real.jpg"]


def test_inbox_没配落地目录时是空列表加一句说明(env):
    # 那种部署下这一页本来就该是空的，不是错误。
    doc = env.body_json(env.get("/v1/admin/inbox"))
    assert doc["files"] == []
    assert doc["dir"] is None
    assert "PHOTOAR_UPLOAD_DIR" in doc["note"]


def test_inbox_要管理员(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    assert env.get("/v1/admin/inbox", as_=env.viewer()).status == 403


# ------------------------------------------- 自匹配分的合成分辨率（热配置）


def test_合成分辨率是可配的_并且真的接上了(make_env, tmp_path, monkeypatch):
    """`needs_restart=False` 的字段是一句**需要接线才成立的承诺**（appconfig 模块
    docstring 里那段）。这条盯着它真的传到了 `synth.generate`。"""
    from photoar import synth

    env = make_env()
    seen = []
    real = synth.generate

    def spy(img, count, seed, long_edge=synth.SYNTH_LONG_EDGE):
        seen.append(long_edge)
        return real(img, count, seed, long_edge=long_edge)

    monkeypatch.setattr(synth, "generate", spy)
    assert env.patch_json(
        "/v1/admin/config", {"ingest.synth_long_edge": 960}
    ).status == 200
    env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    assert seen == [960], f"配置没接上，实际传的是 {seen}"


def test_合成分辨率的默认值就是代码常量(env):
    from photoar import synth

    doc = env.body_json(env.get("/v1/admin/config"))
    assert doc["values"]["ingest.synth_long_edge"] == synth.SYNTH_LONG_EDGE
