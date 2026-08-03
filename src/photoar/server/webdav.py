"""WebDAV 客户端：列目录与取文件。零第三方依赖。

## 为什么自己写

要做的只有两件事：**列一个目录**（PROPFIND，深度 1）和**下载一个文件**（GET）。
两者都是 HTTP + 一点 XML，stdlib 的 `urllib.request` 与 `xml.etree` 正好够。
`webdavclient3` 之类要拉进来 lxml 与 requests 一整串传递依赖，换的是我们不用的
那 90%（锁、属性写、版本管理）。

## 只读

这个模块**没有** PUT / MKCOL / DELETE。素材挂载点的用途是「从那儿拿照片和视频进
我们的库」，往别人的网盘上写东西不在需求里，而一个能写的客户端意味着一个配错的
挂载点可以删掉用户的相册。

## 认证

只支持 HTTP Basic。NAS 的 WebDAV（群晖、威联通、Nextcloud）默认都是它。Digest 要
两次往返加 nonce 状态，而在这条链路上（局域网或者自家域名）Basic + https 已经够；
明文 http 上的 Basic 等于明文口令，所以下面会在用 http 时把这件事记进日志。
"""

from __future__ import annotations

import base64
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

__all__ = [
    "DavEntry",
    "WebDavError",
    "WebDavClient",
    "join_url",
]

# DAV: 命名空间。ElementTree 解出来的 tag 是 `{uri}local`。
_NS_DAV = "DAV:"

# 一次 PROPFIND 最多接受多少字节的响应体。一个几千项的目录约几 MB；给 32 MiB
# 上限是为了别让一个（可能是恶意的、也可能只是很大的）目录把内存吃光。
MAX_PROPFIND_BYTES = 32 * 1024 * 1024

# 一个目录里最多认多少项。超过就截断并如实报出来 —— 静默截断会让人以为「我的照片
# 不在这个目录里」然后去别处找。
MAX_ENTRIES = 5000

# 连接与读取超时（秒）。WebDAV 在这条链路上是局域网或者自家域名，10 秒连不上就是
# 配错了地址；读取给 60 秒是因为 PROPFIND 在很大的目录上确实会慢。
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 60

# 下载单个文件的超时。视频可能几百 MB。
DOWNLOAD_TIMEOUT_S = 15 * 60

# PROPFIND 的请求体。只问我们真的会用的四个属性 —— `allprop` 会让服务端把一大堆
# 自定义属性也塞回来（Nextcloud 尤其多），白读几倍的数据。
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:"><D:prop>'
    "<D:resourcetype/><D:getcontentlength/>"
    "<D:getlastmodified/><D:displayname/>"
    "</D:prop></D:propfind>"
).encode("utf-8")


class WebDavError(Exception):
    """WebDAV 这一侧出的错。`code` 会原样进 HTTP 响应的 `error` 字段。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DavEntry:
    """目录里的一项。

    `name` 是**显示名**（已经过 URL 解码），`href` 是服务端给的原始路径，用来继续
    往下走。两者分开是必须的：href 里的中文是百分号编码的，拿它当显示名会让整个
    目录列表变成一串 `%E5%A9%9A%E7%A4%BC`。
    """

    name: str
    href: str
    is_dir: bool
    bytes: int | None = None
    mtime: str | None = None


def join_url(base: str, *parts: str) -> str:
    """拼 URL，每一段都做百分号编码。

    `quote` 的 `safe` 里**不含** `/`：这里的每一段都是一个文件名或目录名，名字里真的
    出现斜杠时（WebDAV 上是可能的）必须编成 `%2F`，否则它会被当成路径分隔符，
    结果是访问到另一个目录。
    """
    out = base.rstrip("/")
    for p in parts:
        if not p:
            continue
        out += "/" + urllib.parse.quote(p, safe="")
    return out


class WebDavClient:
    """一个 WebDAV 端点。

    @param base 形如 `https://nas.example.com/dav/photos`。尾随斜杠有没有都行。
    """

    def __init__(
        self,
        base: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        base = (base or "").strip()
        if not base.startswith(("http://", "https://")):
            raise WebDavError(
                "bad_webdav_url",
                f"WebDAV 地址要以 http:// 或 https:// 开头，收到 {base!r}",
            )
        self.base = base.rstrip("/")
        self._auth: str | None = None
        if username:
            raw = f"{username}:{password or ''}".encode("utf-8")
            self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    @property
    def insecure(self) -> bool:
        """明文 http 且带口令。调用方据此记一行日志。"""
        return self.base.startswith("http://") and self._auth is not None

    # ---------------------------------------------------------------- 列目录

    def list_dir(self, rel: str = "") -> list[DavEntry]:
        """列一层目录。`rel` 是相对 [base] 的路径（已解码的形式）。"""
        url = self._url_of(rel)
        body = self._request(
            url,
            method="PROPFIND",
            headers={
                # Depth: 1 = 这一层加它的直接子项。`infinity` 在大多数服务端上
                # 是禁用的，而且真开着的话一个大目录会拉回几十 MB。
                "Depth": "1",
                "Content-Type": 'application/xml; charset="utf-8"',
            },
            data=_PROPFIND_BODY,
            timeout=READ_TIMEOUT_S,
            max_bytes=MAX_PROPFIND_BYTES,
        )
        return self._parse_multistatus(body, url)

    def _parse_multistatus(self, body: bytes, self_url: str) -> list[DavEntry]:
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise WebDavError(
                "bad_webdav_response",
                f"WebDAV 的响应不是能解析的 XML（{exc}）。"
                "最常见的原因是这个地址其实不是 WebDAV 端点，而是一个网页。",
            ) from exc

        # 根元素必须是 `{DAV:}multistatus`。
        #
        # 光靠「解得开 XML」是不够的：一个 HTML 登录页（`<html><body>…</body></html>`）
        # **本身就是合法 XML**，解析会成功，然后因为里面没有 `{DAV:}response` 而返回一个
        # 空列表 —— 于是「地址指到了登录页」在界面上显示成「这个目录是空的」，而人会去
        # WebDAV 那边找自己的照片为什么不见了。这条检查是把那个静默失败变成一句话。
        if root.tag != f"{{{_NS_DAV}}}multistatus":
            raise WebDavError(
                "bad_webdav_response",
                f"这个地址返回的不是 WebDAV 响应（根元素是 {root.tag!r}，"
                "应该是 DAV:multistatus）。最常见的原因是地址指到了一个网页"
                "（比如 NAS 的登录页），而不是 WebDAV 端点。",
            )

        self_path = urllib.parse.urlsplit(self_url).path.rstrip("/")
        out: list[DavEntry] = []
        truncated = False
        for resp in root.findall(f"{{{_NS_DAV}}}response"):
            href_el = resp.find(f"{{{_NS_DAV}}}href")
            if href_el is None or not (href_el.text or "").strip():
                continue
            href = (href_el.text or "").strip()
            href_path = urllib.parse.urlsplit(href).path
            # 目录自己也在 multistatus 里（Depth:1 的语义包含自身），跳过它 ——
            # 不跳的话每一层都会多出一个指向自己的条目，点进去就是原地打转。
            if href_path.rstrip("/") == self_path:
                continue

            props = self._first_ok_prop(resp)
            if props is None:
                continue
            is_dir = props.find(f"{{{_NS_DAV}}}resourcetype/{{{_NS_DAV}}}collection") is not None

            # 显示名优先用 displayname，没有就从 href 最后一段解码。
            # 不能只靠 displayname：不少服务端根本不返回它。
            name_el = props.find(f"{{{_NS_DAV}}}displayname")
            name = (name_el.text or "").strip() if name_el is not None else ""
            if not name:
                name = urllib.parse.unquote(href_path.rstrip("/").rsplit("/", 1)[-1])
            if not name:
                continue

            if len(out) >= MAX_ENTRIES:
                truncated = True
                break
            out.append(
                DavEntry(
                    name=name,
                    href=href_path,
                    is_dir=is_dir,
                    bytes=_int_or_none(props, f"{{{_NS_DAV}}}getcontentlength"),
                    mtime=_text_or_none(props, f"{{{_NS_DAV}}}getlastmodified"),
                )
            )
        if truncated:
            raise WebDavError(
                "webdav_dir_too_big",
                f"这个目录里超过 {MAX_ENTRIES} 项，列不完。"
                "在 WebDAV 那边分成几个子目录再来。",
            )
        # 目录在前，然后按名字。和 `fsbrowser.list_dir` 同一个口径，这样管理台上
        # 本地挂载点和 WebDAV 挂载点看起来是一样的。
        out.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
        return out

    @staticmethod
    def _first_ok_prop(resp: ElementTree.Element) -> ElementTree.Element | None:
        """取第一个 2xx 的 `<propstat><prop>`。

        一个 `<response>` 里可以有**多个** `<propstat>`，各带自己的 `<status>`：
        我们问的四个属性里有的存在（200）、有的不存在（404）。只取第一个 propstat
        会在服务端把 404 那组排在前面时读到一个空的 prop —— 于是所有条目都变成
        「不是目录、没有大小」，而目录点不进去。
        """
        first_any = None
        for ps in resp.findall(f"{{{_NS_DAV}}}propstat"):
            prop = ps.find(f"{{{_NS_DAV}}}prop")
            if prop is None:
                continue
            if first_any is None:
                first_any = prop
            status = ps.find(f"{{{_NS_DAV}}}status")
            text = (status.text or "") if status is not None else ""
            if " 2" in text:  # "HTTP/1.1 200 OK"
                return prop
        return first_any

    # ---------------------------------------------------------------- 下载

    def download_to(self, rel: str, dst: Path, max_bytes: int) -> int:
        """把一个文件下载到 `dst`，返回字节数。

        分块写盘，不在内存里囤 —— 视频可能几百 MB。超过 `max_bytes` 时**删掉半个
        文件**再抛：留着一个截断的 mp4 比没有文件糟得多（它能被入库、能转码、
        然后在播放时才出问题）。
        """
        url = self._url_of(rel)
        req = self._build(url, method="GET", headers={})
        dst.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with self._open(req, DOWNLOAD_TIMEOUT_S) as resp, open(dst, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise WebDavError(
                            "webdav_file_too_big",
                            f"这个文件超过 {max_bytes // (1024 * 1024)} MiB，没有下载完。",
                        )
                    fh.write(chunk)
        except Exception:
            dst.unlink(missing_ok=True)
            raise
        return written

    # ---------------------------------------------------------------- 内部

    def _url_of(self, rel: str) -> str:
        """相对路径 → 绝对 URL。

        `rel` 可能是两种东西，都要认：
          1. 由调用方拼出来的相对路径（`"照片/2026"`）；
          2. PROPFIND 回来的 `href`（`/dav/photos/%E7%85%A7%E7%89%87`）—— 已经是
             绝对且**已编码**的，不能再编一遍（那会变成 `%25E7%2585%25A7`）。
        """
        rel = (rel or "").strip()
        if not rel:
            return self.base + "/"
        if rel.startswith("/"):
            # href 形式：拿 base 的 scheme+host 拼上去，路径原样不动。
            parts = urllib.parse.urlsplit(self.base)
            return urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, rel, "", "")
            )
        return join_url(self.base, *[p for p in rel.split("/") if p])

    def _build(
        self, url: str, *, method: str, headers: dict[str, str], data: bytes | None = None
    ) -> urllib.request.Request:
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        if self._auth:
            req.add_header("Authorization", self._auth)
        # 有些服务端对没有 UA 的 PROPFIND 直接 400。
        req.add_header("User-Agent", "photoar/1")
        return req

    @staticmethod
    def _open(req: urllib.request.Request, timeout: int):
        try:
            return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise WebDavError(
                "webdav_unreachable",
                f"连不上 WebDAV：{reason}。检查地址、端口，以及这台服务器能不能"
                "访问到它（容器里的网络和你电脑上的可能不一样）。",
            ) from exc

    def _request(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        data: bytes | None,
        timeout: int,
        max_bytes: int,
    ) -> bytes:
        req = self._build(url, method=method, headers=headers, data=data)
        with self._open(req, timeout) as resp:
            body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise WebDavError(
                "webdav_response_too_big",
                f"WebDAV 的响应超过 {max_bytes // (1024 * 1024)} MiB。",
            )
        return body


def _http_error(exc: urllib.error.HTTPError) -> WebDavError:
    """HTTP 状态码 → 一句能据此行动的中文。

    401/403 与 404 分开说，因为下一步动作完全不同：一个是去改凭证，一个是去改路径。
    405 单独说是因为它几乎总是同一个原因 —— 地址指到了一个普通网页而不是 WebDAV
    端点（那台服务器不认识 PROPFIND 这个方法）。
    """
    code = exc.code
    if code in (401, 403):
        return WebDavError(
            "webdav_unauthorized",
            f"WebDAV 拒绝了（HTTP {code}）。用户名或口令不对，"
            "或者这个账号没有这个目录的权限。",
        )
    if code == 404:
        return WebDavError(
            "webdav_not_found", f"WebDAV 上找不到这个路径（HTTP 404）。"
        )
    if code in (405, 501):
        return WebDavError(
            "webdav_not_dav",
            f"这个地址不接受 PROPFIND（HTTP {code}）—— 它大概不是一个 WebDAV 端点。"
            "群晖是 `https://<host>:5006/`，Nextcloud 是 "
            "`https://<host>/remote.php/dav/files/<用户名>/`。",
        )
    return WebDavError("webdav_http_error", f"WebDAV 返回 HTTP {code}。")


def _text_or_none(prop: ElementTree.Element, tag: str) -> str | None:
    el = prop.find(tag)
    if el is None:
        return None
    text = (el.text or "").strip()
    return text or None


def _int_or_none(prop: ElementTree.Element, tag: str) -> int | None:
    text = _text_or_none(prop, tag)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        # 目录的 getcontentlength 常常是空的或者不是数字。当「没有大小」处理，
        # 不是错误。
        return None
