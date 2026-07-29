"""spec §14.3 点名必测的路径穿越面。

这些用例是"能不能上线"级别的：`/v1/fs/*` 与 `POST /v1/photo` 都收客户端给的
绝对路径，一旦能穿越，整台 NAS 的文件就都能通过隧道被读走。
"""

import os

import pytest

from photoar.server.safepath import PathDenied, Roots


@pytest.fixture
def roots(tmp_path):
    share = tmp_path / "share"
    (share / "photos").mkdir(parents=True)
    (share / "photos" / "a.jpg").write_bytes(b"a")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_bytes(b"s")
    return Roots({"share": str(share)}), tmp_path


def test_accepts_path_inside_root(roots):
    r, tmp = roots
    assert r.resolve(str(tmp / "share" / "photos" / "a.jpg")) == (
        tmp / "share" / "photos" / "a.jpg"
    ).resolve()


def test_accepts_the_root_itself(roots):
    r, tmp = roots
    assert r.resolve(str(tmp / "share")) == (tmp / "share").resolve()


def test_accepts_path_that_does_not_exist_yet(roots):
    """不存在不等于越界。缺文件是 404，越界才是 403 —— 混在一起会让"用户把
    文件删了"和"有人在探测"这两件事在日志里长得一样。"""
    r, tmp = roots
    assert r.resolve(str(tmp / "share" / "photos" / "nope.jpg")).name == "nope.jpg"


def test_rejects_dotdot_segment(roots):
    r, tmp = roots
    with pytest.raises(PathDenied) as e:
        r.resolve(str(tmp / "share" / ".." / "outside" / "secret"))
    assert ".." in e.value.reason


def test_rejects_dotdot_even_when_it_stays_inside(roots):
    """`/share/photos/../photos/a.jpg` 解析后其实合法，仍然拒绝。

    正常客户端不会构造这种路径（服务端返回的都是已解析的绝对路径），会构造的
    只有在试探边界的人。放行等于把"探测"和"正常访问"在日志里混成一类。
    """
    r, tmp = roots
    with pytest.raises(PathDenied):
        r.resolve(str(tmp / "share" / "photos" / ".." / "photos" / "a.jpg"))


def test_rejects_absolute_path_outside_root(roots):
    r, tmp = roots
    with pytest.raises(PathDenied):
        r.resolve(str(tmp / "outside" / "secret"))
    with pytest.raises(PathDenied):
        r.resolve("/etc/passwd")


def test_rejects_relative_path(roots):
    r, _ = roots
    for raw in ("photos/a.jpg", "./a.jpg", "a.jpg"):
        with pytest.raises(PathDenied):
            r.resolve(raw)


def test_rejects_windows_separator(roots):
    r, tmp = roots
    with pytest.raises(PathDenied) as e:
        r.resolve(str(tmp / "share") + "\\..\\outside\\secret")
    assert "反斜杠" in e.value.reason


def test_rejects_nul_byte(roots):
    """`/share/a.jpg\\x00/../../etc/passwd`：某些底层 C 实现会在 NUL 处截断。

    Python 自己会对含 NUL 的路径抛 ValueError，但那是 500 不是 403，日志里
    也看不出这是一次探测。
    """
    r, tmp = roots
    with pytest.raises(PathDenied) as e:
        r.resolve(str(tmp / "share" / "a.jpg") + "\x00.png")
    assert "NUL" in e.value.reason


def test_rejects_empty_path(roots):
    r, _ = roots
    with pytest.raises(PathDenied):
        r.resolve("")


@pytest.mark.skipif(os.name != "posix", reason="需要符号链接")
def test_rejects_symlink_pointing_outside_root(roots):
    """白名单内的一个符号链接指向白名单外 —— 纯字符串前缀检查会放行。

    这是本模块坚持"只信任 resolve() 之后的路径"的原因。QNAP 上用户自己建的
    快捷方式很常见，不必是恶意的。
    """
    r, tmp = roots
    link = tmp / "share" / "escape"
    link.symlink_to(tmp / "outside")
    assert str(link).startswith(str(tmp / "share"))  # 前缀检查会通过
    with pytest.raises(PathDenied):
        r.resolve(str(link / "secret"))


@pytest.mark.skipif(os.name != "posix", reason="需要符号链接")
def test_accepts_symlink_that_stays_inside_root(roots):
    r, tmp = roots
    link = tmp / "share" / "alias"
    link.symlink_to(tmp / "share" / "photos")
    assert r.resolve(str(link / "a.jpg")) == (tmp / "share" / "photos" / "a.jpg").resolve()


@pytest.mark.skipif(os.name != "posix", reason="需要符号链接")
def test_root_itself_being_a_symlink_still_matches(tmp_path):
    """根目录本身是符号链接（QNAP 的 /share 就是）时，合法路径不能被判越界。

    构造时不 resolve 根、检查时 resolve 路径，会拿"未解析的根"比"已解析的
    路径"，把整个库判成越界 —— 表现为所有文件突然全部 403。
    """
    real = tmp_path / "real"
    (real / "photos").mkdir(parents=True)
    (real / "photos" / "a.jpg").write_bytes(b"a")
    link = tmp_path / "share"
    link.symlink_to(real)

    r = Roots({"share": str(link)})
    assert r.resolve(str(link / "photos" / "a.jpg")) == (
        real / "photos" / "a.jpg"
    ).resolve()


def test_deepest_root_wins_when_roots_are_nested(tmp_path):
    outer = tmp_path / "share"
    inner = outer / "photos"
    inner.mkdir(parents=True)
    r = Roots({"outer": str(outer), "inner": str(inner)})
    root = r.root_of(r.resolve(str(inner / "x.jpg")))
    assert root is not None and root.name == "inner"


def test_empty_roots_is_rejected_at_construction():
    with pytest.raises(ValueError):
        Roots({})
