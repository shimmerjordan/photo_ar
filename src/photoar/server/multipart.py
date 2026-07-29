"""multipart/form-data 解析。只为 `POST /v1/recognize` 的 `frame` 字段而存在。

不用 `cgi.FieldStorage`（Python 3.13 已移除）也不用 `email` 解析器（它会把
二进制体按文本猜编码，JPEG 里出现 \\r\\n 序列时行为不可靠）。手写一个只支持
本项目实际需要的子集，边界情况一律**拒绝**而不是猜：

- 不支持 `Content-Transfer-Encoding`（浏览器与 OkHttp 都不发）
- 不支持嵌套 multipart
- 不做流式解析：`frame` 约 50KB，体积上限由 HTTP 层的 `max_body` 卡住

一个 JPEG 里完全可能出现 `\\r\\n--` 这样的字节，但不可能出现完整的随机
boundary（客户端库生成的 boundary 含足够随机位）。这是 multipart 协议本身的
前提，不是这里的额外假设。
"""

import re
from dataclasses import dataclass

_NAME_RE = re.compile(rb'name="([^"]*)"')
_FILENAME_RE = re.compile(rb'filename="([^"]*)"')


class MultipartError(ValueError):
    """请求体不是合法的 multipart/form-data。调用方映射成 400。"""


@dataclass(frozen=True)
class Part:
    name: str
    filename: str | None
    content_type: str | None
    data: bytes


def boundary_of(content_type: str | None) -> bytes:
    if not content_type:
        raise MultipartError("缺少 Content-Type")
    main, *params = [s.strip() for s in content_type.split(";")]
    if main.lower() != "multipart/form-data":
        raise MultipartError(f"Content-Type 不是 multipart/form-data：{main!r}")
    for p in params:
        key, _, value = p.partition("=")
        if key.strip().lower() == "boundary":
            value = value.strip().strip('"')
            if not value:
                raise MultipartError("boundary 为空")
            return value.encode("ascii", "strict")
    raise MultipartError("Content-Type 里没有 boundary 参数")


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, Part]:
    """解析出 name -> Part。同名字段以最后一个为准（本项目只有单值字段）。"""
    delim = b"--" + boundary
    chunks = body.split(delim)
    if len(chunks) < 3:
        raise MultipartError("找不到任何 part（boundary 不匹配？）")
    if not chunks[-1].lstrip().startswith(b"--"):
        raise MultipartError("缺少结束 boundary（--boundary--）")

    out: dict[str, Part] = {}
    for raw in chunks[1:-1]:
        if raw.startswith(b"\r\n"):
            raw = raw[2:]
        elif raw.startswith(b"\n"):
            raw = raw[1:]  # 少数客户端只发 LF
        else:
            raise MultipartError("part 未以 CRLF 开头")
        # part 体末尾的 CRLF 属于下一个 delimiter，不是数据
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]

        head, sep, data = raw.partition(b"\r\n\r\n")
        if not sep:
            head, sep, data = raw.partition(b"\n\n")
        if not sep:
            raise MultipartError("part 缺少头体分隔的空行")

        name = filename = ctype = None
        for line in head.replace(b"\r\n", b"\n").split(b"\n"):
            key, _, value = line.partition(b":")
            key = key.strip().lower()
            if key == b"content-disposition":
                m = _NAME_RE.search(value)
                if m:
                    name = m.group(1).decode("utf-8", "replace")
                m = _FILENAME_RE.search(value)
                if m:
                    filename = m.group(1).decode("utf-8", "replace")
            elif key == b"content-type":
                ctype = value.strip().decode("ascii", "replace")
        if name is None:
            raise MultipartError("part 的 Content-Disposition 里没有 name")
        out[name] = Part(name=name, filename=filename, content_type=ctype, data=data)
    return out
