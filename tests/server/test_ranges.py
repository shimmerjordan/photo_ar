"""spec §14.3：单区间 / 多区间 / 越界 / bytes=0- / bytes=-500 五类必测输入。"""

import pytest

from photoar.server.ranges import ByteRange, RangeNotSatisfiable, parse_range


def test_no_header_means_full_body():
    assert parse_range(None, 1000) is None
    assert parse_range("", 1000) is None


def test_single_range():
    r = parse_range("bytes=0-499", 1000)
    assert (r.start, r.end, r.length) == (0, 499, 500)
    assert r.content_range(1000) == "bytes 0-499/1000"


def test_open_ended_range():
    r = parse_range("bytes=0-", 1000)
    assert (r.start, r.end) == (0, 999)


def test_open_ended_range_from_middle():
    r = parse_range("bytes=500-", 1000)
    assert (r.start, r.end, r.length) == (500, 999, 500)


def test_suffix_range():
    r = parse_range("bytes=-500", 1000)
    assert (r.start, r.end) == (500, 999)


def test_suffix_range_larger_than_file_clamps_to_whole_file():
    r = parse_range("bytes=-5000", 1000)
    assert (r.start, r.end) == (0, 999)


def test_end_beyond_eof_is_clamped_not_rejected():
    """`bytes=900-5000` 是合法且常见的（ExoPlayer 预读到文件尾）。

    RFC 7233：last-byte-pos 大于文件长度时按文件长度处理，不是 416。
    """
    r = parse_range("bytes=900-5000", 1000)
    assert (r.start, r.end) == (900, 999)


def test_start_beyond_eof_is_416():
    with pytest.raises(RangeNotSatisfiable) as e:
        parse_range("bytes=1000-1500", 1000)
    assert e.value.content_range == "bytes */1000"


def test_zero_length_suffix_is_416():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=-0", 1000)


def test_range_on_empty_file_is_416():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-", 0)
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=-1", 0)


def test_multi_range_falls_back_to_full_body():
    """多区间不实现 multipart/byteranges，按 RFC 允许的方式忽略 Range。

    返回 None（→ 200 全量）而不是只给第一段配 206 —— 后者会让客户端把一段
    字节当成整个文件。理由见 ranges.py 的模块 docstring。
    """
    assert parse_range("bytes=0-99,200-299", 1000) is None


def test_unknown_unit_is_ignored():
    assert parse_range("items=0-99", 1000) is None


def test_garbage_is_ignored_not_500():
    for header in ("bytes=", "bytes=-", "bytes=abc-def", "bytes=1-x", "bytes=x-2", "bytes"):
        assert parse_range(header, 1000) is None


def test_reversed_range_is_ignored():
    assert parse_range("bytes=500-100", 1000) is None


def test_whitespace_tolerated():
    r = parse_range("  bytes= 10 - 20 ", 1000)
    assert (r.start, r.end) == (10, 20)


def test_single_byte_range():
    r = parse_range("bytes=0-0", 1000)
    assert (r.start, r.end, r.length) == (0, 0, 1)


def test_byte_range_length_is_inclusive():
    assert ByteRange(0, 0).length == 1
    assert ByteRange(10, 19).length == 10
