import pytest

from photoar.server.multipart import MultipartError, boundary_of, parse_multipart

B = b"----abc123"


def _body(parts: list[bytes], *, close: bool = True) -> bytes:
    out = b""
    for p in parts:
        out += b"--" + B + b"\r\n" + p + b"\r\n"
    out += b"--" + B + (b"--\r\n" if close else b"\r\n")
    return out


def test_boundary_from_content_type():
    assert boundary_of("multipart/form-data; boundary=----abc123") == B


def test_boundary_quoted_and_case_insensitive():
    assert boundary_of('MULTIPART/FORM-DATA; BOUNDARY="----abc123"') == B


def test_boundary_missing_is_error():
    for ct in (None, "application/json", "multipart/form-data", "multipart/form-data; boundary="):
        with pytest.raises(MultipartError):
            boundary_of(ct)


def test_parses_file_part():
    body = _body(
        [
            b'Content-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n"
            b"\r\n"
            b"\xff\xd8\xff\xe0JPEGBYTES"
        ]
    )
    parts = parse_multipart(body, B)
    assert set(parts) == {"frame"}
    p = parts["frame"]
    assert p.filename == "f.jpg"
    assert p.content_type == "image/jpeg"
    assert p.data == b"\xff\xd8\xff\xe0JPEGBYTES"


def test_binary_payload_containing_crlf_dash_survives():
    """JPEG 里出现 `\\r\\n--` 是完全可能的，不能因此把体截断。

    这是手写解析器最容易出错的地方：按 `\\r\\n--` 切而不是按完整 boundary 切，
    结果是帧被截断成一半，解码失败表现为"识别偶发失效"，无从追查。
    """
    payload = b"\xff\xd8" + b"\r\n--" + b"\x00\x01\r\n----ab" + b"\xff\xd9"
    body = _body(
        [b'Content-Disposition: form-data; name="frame"\r\n\r\n' + payload]
    )
    assert parse_multipart(body, B)["frame"].data == payload


def test_multiple_fields():
    body = _body(
        [
            b'Content-Disposition: form-data; name="frame"\r\n\r\nIMG',
            b'Content-Disposition: form-data; name="hint"\r\n\r\n42',
        ]
    )
    parts = parse_multipart(body, B)
    assert parts["frame"].data == b"IMG"
    assert parts["hint"].data == b"42"


def test_bare_lf_client_tolerated():
    body = (
        b"--" + B + b"\n"
        b'Content-Disposition: form-data; name="frame"\n\nIMG\n'
        b"--" + B + b"--\n"
    )
    assert parse_multipart(body, B)["frame"].data == b"IMG"


def test_missing_closing_boundary_is_error():
    body = _body([b'Content-Disposition: form-data; name="frame"\r\n\r\nIMG'], close=False)
    with pytest.raises(MultipartError):
        parse_multipart(body, B)


def test_wrong_boundary_is_error():
    body = _body([b'Content-Disposition: form-data; name="frame"\r\n\r\nIMG'])
    with pytest.raises(MultipartError):
        parse_multipart(body, b"----different")


def test_part_without_name_is_error():
    body = _body([b"Content-Type: image/jpeg\r\n\r\nIMG"])
    with pytest.raises(MultipartError):
        parse_multipart(body, B)


def test_part_without_blank_line_is_error():
    body = _body([b'Content-Disposition: form-data; name="frame"'])
    with pytest.raises(MultipartError):
        parse_multipart(body, B)


def test_empty_body_is_error():
    with pytest.raises(MultipartError):
        parse_multipart(b"", B)


def test_empty_part_data_is_allowed():
    """空 frame 不是 multipart 层的错误 —— 它该在解码那一步变成 400 bad_frame，
    错误信息才对得上用户能看懂的原因。"""
    body = _body([b'Content-Disposition: form-data; name="frame"\r\n\r\n'])
    assert parse_multipart(body, B)["frame"].data == b""
