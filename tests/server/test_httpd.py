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
import threading
import urllib.error
import urllib.request

import pytest

from photoar.server import httpd

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


def test_304_has_no_body(live):
    env, base = live
    pid = env.ingest_ok(env.write_image("photos/e.jpg", seed=9))
    r1 = _open(_req(f"{base}/v1/photo/{pid}/imgdb"))
    r1.read()
    etag = r1.headers["ETag"]
    r2 = _open(_req(f"{base}/v1/photo/{pid}/imgdb", headers={"If-None-Match": etag}))
    assert r2.status == 304
    assert r2.read() == b""
