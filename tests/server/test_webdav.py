"""WebDAV 客户端。

**这个文件里最重要的不是「我们自己造的 XML 能不能解」。** 那种测试一定过 —— 造 XML 的和
解 XML 的是同一个人的同一套假设。真实的 WebDAV 响应在几个地方和「教科书写法」不一样，而
每一处都会让整个目录浏览失效：

1. **一个 `<response>` 里有多个 `<propstat>`**（Nextcloud 就是）。我们问了四个属性，存在的
   那些归 200 一组、不存在的归 404 一组，而 **404 那组可能排在前面**。只取第一个 propstat
   会读到空 prop → 所有条目都变成「不是目录、没有大小」→ 目录点不进去。
2. **命名空间前缀不固定**：`<D:response>` / `<d:response>` / `<response xmlns="DAV:">` 都有。
3. **`displayname` 可能根本不返回**，得从 href 最后一段解码。
4. **目录自己也在响应里**（Depth:1 的语义包含自身），不跳掉就会有一个指向自己的条目。

所以下面的 XML 全是照着真实服务端的形状手写的，而且末尾有一组走**真实 HTTP**的端到端用例。
"""

from __future__ import annotations

import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from photoar.server.webdav import (
    MAX_ENTRIES,
    DavEntry,
    WebDavClient,
    WebDavError,
    join_url,
)


def parse(xml: str, self_url: str = "https://nas/dav/photos") -> list[DavEntry]:
    c = WebDavClient("https://nas/dav/photos")
    return c._parse_multistatus(xml.encode("utf-8"), self_url)


# ---------------------------------------------------------------- 真实形状


NEXTCLOUD = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/dav/photos/</d:href>
    <d:propstat>
      <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
    <d:propstat>
      <d:prop><d:getcontentlength/><d:displayname/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/photos/%E5%A9%9A%E7%A4%BC/</d:href>
    <d:propstat>
      <d:prop><d:getcontentlength/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status>
    </d:propstat>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
        <d:getlastmodified>Mon, 03 Aug 2026 10:00:00 GMT</d:getlastmodified>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/photos/a.jpg</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype/>
        <d:getcontentlength>102400</d:getcontentlength>
        <d:getlastmodified>Mon, 03 Aug 2026 09:00:00 GMT</d:getlastmodified>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def test_Nextcloud_形状_404_那组排在前面时也要解对():
    # 这是最要紧的一条。取第一个 propstat 的实现会让「婚礼」那一项变成非目录、
    # 于是在管理台上点不进去，而 XML 看起来完全正常。
    out = parse(NEXTCLOUD)
    assert [e.name for e in out] == ["婚礼", "a.jpg"]
    wedding, photo = out
    assert wedding.is_dir is True, "404 propstat 排在前面时目录判断错了"
    assert photo.is_dir is False
    assert photo.bytes == 102400
    assert photo.mtime == "Mon, 03 Aug 2026 09:00:00 GMT"


def test_目录自己不出现在结果里():
    # Depth:1 包含自身。不跳掉的话每一层都多一个指向自己的条目，点进去原地打转。
    out = parse(NEXTCLOUD)
    assert all(e.name != "photos" for e in out)


def test_中文名从_href_解码_而不是显示成百分号编码():
    # 拿 href 当显示名会让整个列表变成一串 %E5%A9%9A%E7%A4%BC。
    out = parse(NEXTCLOUD)
    assert out[0].name == "婚礼"
    assert out[0].href == "/dav/photos/%E5%A9%9A%E7%A4%BC/", "href 要保持编码后的原样"


SYNOLOGY = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/photo/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
    <D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/photo/2026/</D:href>
    <D:propstat><D:prop>
      <D:resourcetype><D:collection/></D:resourcetype>
      <D:displayname>2026</D:displayname>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
  <D:response>
    <D:href>/photo/v.mp4</D:href>
    <D:propstat><D:prop>
      <D:resourcetype/><D:getcontentlength>9999999</D:getcontentlength>
      <D:displayname>v.mp4</D:displayname>
    </D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
  </D:response>
</D:multistatus>
"""


def test_大写前缀的命名空间():
    out = parse(SYNOLOGY, self_url="https://nas/photo")
    assert [(e.name, e.is_dir) for e in out] == [("2026", True), ("v.mp4", False)]


DEFAULT_NS = """<?xml version="1.0"?>
<multistatus xmlns="DAV:">
  <response>
    <href>/dav/</href>
    <propstat><prop><resourcetype><collection/></resourcetype></prop>
    <status>HTTP/1.1 200 OK</status></propstat>
  </response>
  <response>
    <href>/dav/b.png</href>
    <propstat><prop><resourcetype/><getcontentlength>1</getcontentlength></prop>
    <status>HTTP/1.1 200 OK</status></propstat>
  </response>
</multistatus>
"""


def test_没有前缀_直接用默认命名空间():
    out = parse(DEFAULT_NS, self_url="https://nas/dav")
    assert [e.name for e in out] == ["b.png"]


def test_目录在前_然后按名字排():
    # 和 fsbrowser.list_dir 同一个口径，这样管理台上本地挂载点和 WebDAV 看起来一样。
    xml = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/dav/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/dav/z.jpg</d:href><d:propstat><d:prop><d:resourcetype/>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/dav/B/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/dav/a.jpg</d:href><d:propstat><d:prop><d:resourcetype/>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/dav/A/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    </d:multistatus>"""
    out = parse(xml, self_url="https://nas/dav")
    assert [e.name for e in out] == ["A", "B", "a.jpg", "z.jpg"]


def test_目录的_getcontentlength_不是数字时当没有大小():
    xml = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/dav/x/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
        <d:getcontentlength></d:getcontentlength></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    </d:multistatus>"""
    out = parse(xml, self_url="https://nas/dav")
    assert out[0].bytes is None


def test_没有_href_的响应被跳过():
    xml = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
      <d:response><d:propstat><d:prop><d:resourcetype/></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/dav/ok.jpg</d:href><d:propstat><d:prop><d:resourcetype/>
        </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    </d:multistatus>"""
    assert [e.name for e in parse(xml, "https://nas/dav")] == ["ok.jpg"]


def test_根本不是_XML_时报错():
    with pytest.raises(WebDavError) as e:
        parse("不是 xml <<<")
    assert e.value.code == "bad_webdav_response"


def test_是合法_XML_但不是_WebDAV_响应时也要报错():
    # 一个 HTML 登录页**本身就是合法 XML**，所以「解得开」不等于「是 WebDAV」。
    # 少了根元素检查的话，这种响应会解出一个空列表 → 界面显示「这个目录是空的」，
    # 而人会去 WebDAV 那边找自己的照片为什么不见了。
    for xml in (
        "<html><body>登录页</body></html>",
        '<?xml version="1.0"?><error>nope</error>',
        '<?xml version="1.0"?><multistatus xmlns="http://example.com/"/>',
    ):
        with pytest.raises(WebDavError) as e:
            parse(xml)
        assert e.value.code == "bad_webdav_response", xml
        assert "网页" in e.value.message, xml


def test_目录项太多时如实报出来_不静默截断():
    rows = "".join(
        f"<d:response><d:href>/dav/f{i}.jpg</d:href><d:propstat><d:prop>"
        "<d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status>"
        "</d:propstat></d:response>"
        for i in range(MAX_ENTRIES + 5)
    )
    xml = f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{rows}</d:multistatus>'
    with pytest.raises(WebDavError) as e:
        parse(xml, "https://nas/dav")
    assert e.value.code == "webdav_dir_too_big"


# ---------------------------------------------------------------- URL 处理


def test_地址必须带协议():
    for bad in ("nas.local/dav", "//nas/dav", "", "ftp://nas/dav"):
        with pytest.raises(WebDavError) as e:
            WebDavClient(bad)
        assert e.value.code == "bad_webdav_url", bad


def test_join_url_把每一段都编码_包括斜杠():
    # 名字里真的有斜杠时必须编成 %2F，否则会被当成路径分隔符访问到别的目录。
    assert join_url("https://n/d", "婚礼") == "https://n/d/%E5%A9%9A%E7%A4%BC"
    assert join_url("https://n/d/", "a b.jpg") == "https://n/d/a%20b.jpg"
    assert join_url("https://n/d", "a/b") == "https://n/d/a%2Fb"


def test_href_形式的路径不会被二次编码():
    # PROPFIND 回来的 href 已经是编码过的。再编一遍会变成 %25E7%2585%25A7。
    c = WebDavClient("https://nas/dav/photos")
    assert c._url_of("/dav/photos/%E5%A9%9A%E7%A4%BC") == \
        "https://nas/dav/photos/%E5%A9%9A%E7%A4%BC"


def test_相对路径会被编码():
    c = WebDavClient("https://nas/dav")
    assert c._url_of("婚礼/a.jpg") == "https://nas/dav/%E5%A9%9A%E7%A4%BC/a.jpg"


def test_空路径就是根():
    assert WebDavClient("https://nas/dav/").  _url_of("") == "https://nas/dav/"


def test_明文_http_带口令时会被标出来():
    # 调用方据此记一行日志。Basic over http 等于明文口令。
    assert WebDavClient("http://nas/dav", "u", "p").insecure is True
    assert WebDavClient("https://nas/dav", "u", "p").insecure is False
    assert WebDavClient("http://nas/dav").insecure is False, "没口令就没什么可泄露的"


# ---------------------------------------------------------------- 真实 HTTP


class _DavHandler(BaseHTTPRequestHandler):
    """一个够用的假 WebDAV 服务端。只实现 PROPFIND 与 GET。"""

    files = {"/dav/a.jpg": b"JPEGDATA" * 100}
    need_auth = False

    def log_message(self, *a):  # 别把测试输出刷满
        pass

    def _auth_ok(self) -> bool:
        if not self.need_auth:
            return True
        want = "Basic " + base64.b64encode(b"u:p").decode()
        return self.headers.get("Authorization") == want

    def do_PROPFIND(self):  # noqa: N802
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return
        if self.path.rstrip("/") != "/dav":
            self.send_response(404)
            self.end_headers()
            return
        assert self.headers.get("Depth") == "1", "必须带 Depth: 1"
        body = (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
            "<d:response><d:href>/dav/</d:href><d:propstat><d:prop>"
            "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "<d:response><d:href>/dav/a.jpg</d:href><d:propstat><d:prop>"
            "<d:resourcetype/><d:getcontentlength>800</d:getcontentlength>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "<d:response><d:href>/dav/%E5%AD%90%E7%9B%AE%E5%BD%95/</d:href>"
            "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            "</d:multistatus>"
        ).encode()
        self.send_response(207)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return
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
def dav_server():
    def _start(need_auth: bool = False):
        handler = type("H", (_DavHandler,), {"need_auth": need_auth})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}/dav"

    started = []

    def start(need_auth: bool = False):
        srv, url = _start(need_auth)
        started.append(srv)
        return url

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_端到端_列目录(dav_server):
    url = dav_server()
    out = WebDavClient(url).list_dir()
    assert [(e.name, e.is_dir) for e in out] == [("子目录", True), ("a.jpg", False)]
    assert out[1].bytes == 800


def test_端到端_下载(dav_server, tmp_path):
    url = dav_server()
    dst = tmp_path / "got.jpg"
    n = WebDavClient(url).download_to("a.jpg", dst, max_bytes=10 * 1024)
    assert n == 800
    assert dst.read_bytes() == b"JPEGDATA" * 100


def test_端到端_超大文件不留半个文件(dav_server, tmp_path):
    # 留着一个截断的 mp4 比没有文件糟得多：它能入库、能转码，然后在播放时才出问题。
    url = dav_server()
    dst = tmp_path / "trunc.jpg"
    with pytest.raises(WebDavError) as e:
        WebDavClient(url).download_to("a.jpg", dst, max_bytes=10)
    assert e.value.code == "webdav_file_too_big"
    assert not dst.exists(), "半个文件必须删掉"


def test_端到端_要认证时不给凭证是_401(dav_server):
    url = dav_server(need_auth=True)
    with pytest.raises(WebDavError) as e:
        WebDavClient(url).list_dir()
    assert e.value.code == "webdav_unauthorized"
    assert "口令" in e.value.message


def test_端到端_给对凭证就能通(dav_server):
    url = dav_server(need_auth=True)
    out = WebDavClient(url, "u", "p").list_dir()
    assert [e.name for e in out] == ["子目录", "a.jpg"]


def test_端到端_路径不存在(dav_server):
    url = dav_server()
    with pytest.raises(WebDavError) as e:
        WebDavClient(url).list_dir("没有这个目录")
    assert e.value.code == "webdav_not_found"


def test_端到端_下载不存在的文件(dav_server, tmp_path):
    url = dav_server()
    with pytest.raises(WebDavError) as e:
        WebDavClient(url).download_to("nope.jpg", tmp_path / "x", max_bytes=1 << 20)
    assert e.value.code == "webdav_not_found"


def test_连不上的地址给出可行动的提示():
    # 端口 1 上不会有服务。提示里要提「容器里的网络和你电脑上的可能不一样」——
    # 那是这条错误在这个部署形态下最常见的真实原因。
    with pytest.raises(WebDavError) as e:
        WebDavClient("http://127.0.0.1:1/dav").list_dir()
    assert e.value.code == "webdav_unreachable"
    assert "容器" in e.value.message
