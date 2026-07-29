"""Range 请求解析。spec §14.3 点名要测的五种输入都在这里处理。

为什么必须做对：ExoPlayer 靠 `Accept-Ranges: bytes` + 206 才能 seek（spec §7）。
一个只会返回 200 全量的服务不会报错，只表现为"视频拖不动进度条"。

**多区间请求返回 200 全量而不是 multipart/byteranges。** RFC 7233 §3.1 明确
允许服务端忽略 Range（"A server MAY ignore the Range header field"），而
ExoPlayer 只发单区间。实现 multipart/byteranges 是为一个不存在的客户端写一段
不会被测到的代码。返回 200 全量的语义是"我不支持你要的分段，这是整个文件"，
客户端一定能正确处理；返回 206 却只给第一段才是真正的错误。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # 闭区间，与 HTTP 的 Content-Range 一致

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self, size: int) -> str:
        return f"bytes {self.start}-{self.end}/{size}"


class RangeNotSatisfiable(Exception):
    """416。响应必须带 `Content-Range: bytes */<size>`。"""

    def __init__(self, size: int) -> None:
        super().__init__(f"请求的区间超出文件大小 {size}")
        self.size = size

    @property
    def content_range(self) -> str:
        return f"bytes */{self.size}"


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """返回要吐的区间；返回 None 表示"忽略这个 Range，吐 200 全量"。

    区分"忽略"与"416"是刻意的：语法上无法理解的 Range 按 RFC 忽略（当作没发），
    而语法正确但落在文件之外的 Range 必须 416 —— 后者是客户端的状态与服务端
    不一致（比如文件被换过），静默吐全量会让客户端把错位的字节当成它要的那段。
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None  # 不认识的单位，按 RFC 7233 忽略
    spec = header[len("bytes=") :].strip()
    if "," in spec:
        return None  # 多区间：见模块 docstring
    if "-" not in spec:
        return None

    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    if not first and not last:
        return None  # "bytes=-" 无意义
    if first and not first.isdigit():
        return None
    if last and not last.isdigit():
        return None

    if not first:
        # 后缀式 bytes=-N：最后 N 字节
        n = int(last)
        if n == 0:
            # RFC 7233：suffix-length 为 0 不可满足
            raise RangeNotSatisfiable(size)
        if size == 0:
            raise RangeNotSatisfiable(size)
        start = max(0, size - n)
        return ByteRange(start, size - 1)

    start = int(first)
    if size == 0 or start >= size:
        raise RangeNotSatisfiable(size)
    if not last:
        return ByteRange(start, size - 1)
    end = int(last)
    if end < start:
        return None  # 语法合法但区间反了，按无法理解处理
    return ByteRange(start, min(end, size - 1))
