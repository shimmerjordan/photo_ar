"""真的起一个端口跑一遍。

`test_app.py` 直接调 `Server.handle`，覆盖不到 socket 层：Content-Length 是否
与实际写出的字节一致、206 的分段是否真的从正确偏移开始、HEAD 是否不写体、
keep-alive 下一个请求会不会从上一个请求的残留字节开始解析。这些都是"逻辑全对
但 ExoPlayer 播不了"的经典位置，所以必须有一组真 socket 的测试。

用 stdlib 的 urllib 做客户端，不引入 requests。
"""

import contextlib
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from photoar.server import httpd

from . import conftest
from .conftest import TOKEN


@contextlib.contextmanager
def _serving(env):
    env.cfg.bind = "127.0.0.1"
    env.cfg.port = 0  # 让内核挑一个空闲端口
    server = httpd.make_server(env.cfg, env.srv)
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield env, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def live(env):
    with _serving(env) as pair:
        yield pair


@pytest.fixture
def live_upload(make_env, tmp_path):
    """开了上传功能的实例。上传是唯一自己流式读请求体的接口，keep-alive 的
    收尾逻辑在它身上与别处不同，所以必须单独在真 socket 上跑。"""
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "inbox"))
    with _serving(env) as pair:
        yield pair


def _req(url, *, method="GET", data=None, headers=None, token=TOKEN):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _open(req):
    try:
        return urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        return e


def test_ping_over_real_socket(live):
    env, base = live
    resp = _open(_req(base + "/v1/ping"))
    assert resp.status == 200
    assert json.loads(resp.read())["ok"] is True


def test_unauthorized_carries_www_authenticate(live):
    _, base = live
    resp = _open(_req(base + "/v1/ping", token=None))
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


def test_stream_full_body_matches_file(live):
    env, base = live
    pid = env.ingest_ok(
        env.write_image("photos/a.jpg", seed=5), video=env.write_video("videos/a.mp4")
    )
    url = json.loads(_open(_req(f"{base}/v1/photo/{pid}/media")).read())["url"]
    raw = (env.nas / "videos" / "a.mp4").read_bytes()

    resp = _open(_req(base + url))
    body = resp.read()
    assert resp.status == 200
    assert int(resp.headers["Content-Length"]) == len(raw) == len(body)
    assert body == raw


def test_stream_partial_body_starts_at_offset(live):
    """206 的体必须真的从 start 开始 —— Content-Range 说对了但体从 0 开始，
    表现是"拖进度条后画面花屏"，日志里一切正常。"""
    env, base = live
    pid = env.ingest_ok(
        env.write_image("photos/b.jpg", seed=6), video=env.write_video("videos/b.mp4")
    )
    url = json.loads(_open(_req(f"{base}/v1/photo/{pid}/media")).read())["url"]
    raw = (env.nas / "videos" / "b.mp4").read_bytes()

    resp = _open(_req(base + url, headers={"Range": "bytes=100-199"}))
    body = resp.read()
    assert resp.status == 206
    assert resp.headers["Content-Range"] == f"bytes 100-199/{len(raw)}"
    assert int(resp.headers["Content-Length"]) == 100
    assert body == raw[100:200]


def test_head_sends_headers_without_body(live):
    """ExoPlayer 会先发 HEAD 探能力。写了体会让 keep-alive 的下一个请求错位。"""
    env, base = live
    pid = env.ingest_ok(
        env.write_image("photos/c.jpg", seed=7), video=env.write_video("videos/c.mp4")
    )
    url = json.loads(_open(_req(f"{base}/v1/photo/{pid}/media")).read())["url"]
    raw = (env.nas / "videos" / "c.mp4").read_bytes()

    resp = _open(_req(base + url, method="HEAD"))
    assert resp.status == 200
    assert int(resp.headers["Content-Length"]) == len(raw)
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.read() == b""


def test_recognize_multipart_over_socket(live):
    env, base = live
    import cv2

    from photoar import synth

    img = env.textured(seed=8, w=1200, h=800)
    ref = env.nas / "photos" / "d.jpg"
    assert cv2.imwrite(str(ref), img)
    pid = env.ingest_ok(ref)

    query, _ = synth.generate(img, count=1, seed=1)[0]
    jpeg = env.jpeg_of(query)
    boundary = "----socketboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + jpeg + f"\r\n--{boundary}--\r\n".encode()

    resp = _open(
        _req(
            base + "/v1/recognize",
            method="POST",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    )
    assert resp.status == 200
    assert json.loads(resp.read())["photoId"] == pid


def test_upload_completes_and_connection_stays_usable(live_upload):
    """上传自己把请求体流式读完，收尾时不能再去读一遍。

    曾经的缺陷：收尾按"处理器读过没有"判断，而流式上传读完后 `_body` 仍是
    None，于是又去读一遍 Content-Length —— 连接上已无字节，服务端永久阻塞在
    这里。表现是 curl 上传 20 万字节后一直挂着，文件其实已经完整落地了，
    日志里没有任何异常。所以这里不只看 201，还要在同一条连接上再发一个请求。
    """
    env, base = live_upload
    payload = bytes(range(256)) * 800  # 204800 字节，越过 curl 的 100-continue 门槛
    host = base.removeprefix("http://")
    conn = http.client.HTTPConnection(host, timeout=10)
    try:
        conn.request(
            "POST",
            "/v1/upload?name=up.bin",
            body=payload,
            headers={"Authorization": f"Bearer {TOKEN}", "Expect": "100-continue"},
        )
        r1 = conn.getresponse()
        doc = json.loads(r1.read())
        assert r1.status == 201, doc
        assert doc["bytes"] == len(payload)
        assert (env.nas / "inbox" / "up.bin").read_bytes() == payload

        # 同一条连接上的下一个请求：验证既没阻塞、也没留下残留字节
        conn.request("GET", "/v1/ping", headers={"Authorization": f"Bearer {TOKEN}"})
        r2 = conn.getresponse()
        assert r2.status == 200
        assert json.loads(r2.read())["ok"] is True
    finally:
        conn.close()


def test_rejected_upload_body_is_drained(live_upload):
    """反向情形：处理器一个字节都没读就拒了（重名 409），残留的体必须被读掉。"""
    env, base = live_upload
    payload = b"z" * 100_000
    (env.nas / "inbox").mkdir(parents=True, exist_ok=True)
    (env.nas / "inbox" / "dup.bin").write_bytes(b"already here")

    host = base.removeprefix("http://")
    conn = http.client.HTTPConnection(host, timeout=10)
    try:
        conn.request(
            "POST",
            "/v1/upload?name=dup.bin",
            body=payload,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        r1 = conn.getresponse()
        assert r1.status == 409, r1.read()
        r1.read()
        assert (env.nas / "inbox" / "dup.bin").read_bytes() == b"already here"

        conn.request("GET", "/v1/ping", headers={"Authorization": f"Bearer {TOKEN}"})
        r2 = conn.getresponse()
        assert r2.status == 200
    finally:
        conn.close()


def test_two_requests_on_one_connection(live):
    """keep-alive：上一个请求的体没读干净，下一个请求会从残留字节开始解析。

    表现是第二个请求返回 400 或直接挂住，而第一个请求看起来完全正常。
    """
    env, base = live
    host = base.removeprefix("http://")
    conn = http.client.HTTPConnection(host, timeout=10)
    try:
        # 第一个请求带一个不会被读的体（未知路由，处理器不读 body）
        conn.request(
            "POST",
            "/v1/nope",
            body=b"x" * 5000,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        r1 = conn.getresponse()
        r1.read()
        assert r1.status == 404

        conn.request("GET", "/v1/ping", headers={"Authorization": f"Bearer {TOKEN}"})
        r2 = conn.getresponse()
        assert r2.status == 200
        assert json.loads(r2.read())["ok"] is True
    finally:
        conn.close()


def test_traversal_over_real_socket(live):
    """URL 编码的穿越串在真 HTTP 层被解码后仍必须 403。"""
    _, base = live
    resp = _open(_req(base + "/v1/fs/list?path=%2Fetc%2Fpasswd"))
    assert resp.status == 403


# ---- 过载与半开连接 ----
#
# 这两组测的都是「服务看起来还活着，但已经不干活了」那一类。它们不靠真把 CPU
# 打满或真等 30 秒来触发 —— 那样既慢又不确定 —— 而是把限流闸门和超时换成
# 可控的值，测契约本身。


@contextlib.contextmanager
def _gate_full(monkeypatch):
    """把识别闸门换成一个已经占满的，并把排队预算压到几十毫秒。"""
    full = threading.BoundedSemaphore(1)
    assert full.acquire(blocking=False)
    monkeypatch.setattr(httpd, "_recognize_gate", full)
    monkeypatch.setattr(httpd, "_RECOGNIZE_QUEUE_S", 0.05)
    yield


def test_recognize_sheds_load_instead_of_queueing(live, monkeypatch):
    """槽位排满时识别必须 503，不能无限期排队。

    排队超过客户端那 2 秒超时的请求，客户端早已放弃并发下一帧了，服务端却仍会
    跑完整个 ORB + RANSAC 再往关掉的 socket 上写 —— 那份 CPU 本该给还有人在等的
    请求。于是越积压越多请求作废，越多作废越没 CPU 处理新的。

    503 让客户端丢帧（`ScanController` 对 5xx 就是静默丢帧、400ms 后重来），
    而不是让服务端替一个没人要的结果干活。
    """
    _, base = live
    with _gate_full(monkeypatch):
        resp = _open(
            _req(
                base + "/v1/recognize",
                method="POST",
                data=b"x" * 100,
                headers={"Content-Type": "multipart/form-data; boundary=b"},
            )
        )
        assert resp.status == 503
        # 没有 Retry-After 的 503 会让规矩的客户端立刻重试，等于没限流
        assert resp.headers["Retry-After"] == "1"
        assert json.loads(resp.read())["error"] == "busy"


def test_load_shedding_does_not_touch_other_routes(live, monkeypatch):
    """闸门只管识别。

    把 `gated` 的判断写宽一点（比如漏掉 path 判断）就会让视频下载和 ping 一起
    排在识别后面 —— 表现是「认出来了但视频半天不开始播」，而识别日志全是正常的。
    """
    env, base = live
    pid = env.ingest_ok(
        env.write_image("photos/f.jpg", seed=11), video=env.write_video("videos/f.mp4")
    )
    with _gate_full(monkeypatch):
        assert _open(_req(base + "/v1/ping")).status == 200
        url = json.loads(_open(_req(f"{base}/v1/photo/{pid}/media")).read())["url"]
        assert _open(_req(base + url)).status == 200


def test_idle_connection_is_closed_by_socket_timeout(live, monkeypatch):
    """连上不发数据的连接必须被服务端关掉。

    stdlib 默认没有 socket 超时。手机进电梯留下的半开连接会占住一个线程直到 TCP
    keepalive 发现（默认两小时以上），而 `ThreadingHTTPServer` 不限线程数 ——
    攒几次就是线程泄漏，服务本身「看着还活着」。这个口子还经 Cloudflare tunnel
    暴露在公网上。
    """
    _, base = live
    monkeypatch.setattr(httpd._Handler, "timeout", 0.3)
    host, port = base.removeprefix("http://").rsplit(":", 1)
    conn = socket.create_connection((host, int(port)), timeout=5)
    try:
        # 一个字节都不发。recv 返回 b"" 表示对端已关 —— 若超时没生效，
        # 这里会一直阻塞到 socket 自己的 5 秒超时并抛 TimeoutError。
        assert conn.recv(64) == b""
    finally:
        conn.close()


def test_304_has_no_body(live):
    env, base = live
    pid = env.ingest_ok(env.write_image("photos/e.jpg", seed=9))
    r1 = _open(_req(f"{base}/v1/photo/{pid}/imgdb"))
    r1.read()
    etag = r1.headers["ETag"]
    r2 = _open(_req(f"{base}/v1/photo/{pid}/imgdb", headers={"If-None-Match": etag}))
    assert r2.status == 304
    assert r2.read() == b""


def test_patch_and_delete_reach_the_dispatcher(live):
    """PATCH 与 DELETE 只有 `_Handler` 上有对应的 `do_*` 才到得了 `Server.handle`。

    这一条必须走真 socket：`test_app_auth.py` 直接调 `Server.handle`，路由表里有
    PATCH 就够了 —— 漏掉 `do_PATCH` 的表现是 stdlib 回一个 501 Unsupported
    method，那时全部管理接口的测试仍然是绿的，只有管理台是坏的。
    """
    env, base = live
    r = _open(
        _req(
            f"{base}/v1/admin/config",
            method="PATCH",
            data=json.dumps({"recog.top_k": 25}).encode(),
            headers={"Content-Type": "application/json"},
        )
    )
    assert r.status == 200, r.read()
    assert json.loads(r.read())["needsRestart"] == []

    created = _open(
        _req(
            f"{base}/v1/admin/users",
            method="POST",
            data=json.dumps({"name": "真 socket 上建的人", "role": "viewer"}).encode(),
            headers={"Content-Type": "application/json"},
        )
    )
    assert created.status == 201
    uid = json.loads(created.read())["id"]
    assert _open(_req(f"{base}/v1/admin/users/{uid}", method="DELETE")).status == 204


def test_session_cookie_authenticates_over_a_real_socket(live):
    """网页里的 `<img src>` 只能靠 cookie。这条走真 socket 是因为 Set-Cookie 与
    Cookie 头都要真的过一遍 stdlib 的头解析。"""
    env, base = live
    pid = env.ingest_ok(env.write_image("photos/f.jpg", seed=10))
    login = _open(
        _req(
            f"{base}/v1/auth/login",
            method="POST",
            data=json.dumps(
                {"name": conftest.ADMIN_NAME, "password": conftest.ADMIN_PASSWORD}
            ).encode(),
            headers={"Content-Type": "application/json"},
            token=None,
        )
    )
    assert login.status == 200
    token = json.loads(login.read())["token"]
    assert login.headers["Set-Cookie"].startswith("photoar_session=")

    r = _open(
        _req(
            f"{base}/v1/photo/{pid}/thumb",
            headers={"Cookie": f"photoar_session={token}"},
            token=None,  # 只带 cookie，不带 Authorization
        )
    )
    assert r.status == 200 and r.read()[:2] == b"\xff\xd8"


def test_admin_page_is_served_over_a_real_socket(live):
    _, base = live
    r = _open(_req(base + "/admin", token=None))
    assert r.status == 200
    assert "photoar 管理台" in r.read().decode("utf-8")
