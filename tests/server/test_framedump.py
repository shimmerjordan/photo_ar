"""留帧诊断：`framedump.FrameDump`。

这套测试盯的是"排查功能自己变成故障源"的那几条：

1. **写盘出任何问题都不能抛**。留帧挂在识别的主路径上，盘满或权限不对时如果
   抛出去，表现就是"开了排查开关之后扫描全线 500" —— 为了看清问题反而制造了
   更大的问题。
2. **`via` 来自客户端自己填的 HTTP 头，不能直接进路径**。`../../etc/x` 就是
   路径穿越，而这个字段任何人都能填。
3. **有上限，且真的会删**。这个开关一定会有人忘记关（开的时候在查问题，查完
   注意力已经跟着结论走了），没上限就是慢慢把盘写满。
4. **文件名要能一眼看出哪帧差**。命中与否 + 内点数编在名字里，`ls` 一下就能
   分出"这个角度认得出、那个角度认不出"，不用逐个打开。
"""

from pathlib import Path

from photoar.server import framedump

JPEG = b"\xff\xd8\xff\xd9"  # 内容不重要：FrameDump 只搬字节，不解码


def test_写盘并把判定编进文件名(tmp_path: Path) -> None:
    d = framedump.FrameDump(tmp_path)
    p = d.save(JPEG, matched=False, inliers=6, reason="weak", via="tailscale")

    assert p is not None
    assert p.read_bytes() == JPEG
    assert p.parent == tmp_path / framedump.DIR_NAME
    # 排查时的第一个动作是 ls，所以这四样必须在名字里
    assert "miss" in p.name
    assert "in6" in p.name
    assert "weak" in p.name
    assert "tailscale" in p.name


def test_命中的帧也留(tmp_path: Path) -> None:
    """命中和未命中放在一起才有诊断价值 —— 差别是靠对比看出来的。"""
    d = framedump.FrameDump(tmp_path)
    p = d.save(JPEG, matched=True, inliers=81, reason=None, via="lan")

    assert p is not None
    assert "hit" in p.name
    assert "in81" in p.name


def test_构造不碰盘(tmp_path: Path) -> None:
    """否则 data/ 下会留一个空目录，让人以为开关是开着的。"""
    framedump.FrameDump(tmp_path)
    assert not (tmp_path / framedump.DIR_NAME).exists()


def test_via_里的路径穿越被挡掉(tmp_path: Path) -> None:
    d = framedump.FrameDump(tmp_path)
    p = d.save(JPEG, matched=False, inliers=0, reason=None, via="../../../etc/passwd")

    assert p is not None
    # 关键断言是"没跑出 debug_frames 目录"，而不是名字长什么样
    assert p.parent == tmp_path / framedump.DIR_NAME
    assert ".." not in p.name
    assert "/" not in p.name


def test_via_为空也能写(tmp_path: Path) -> None:
    """`X-PhotoAR-Endpoint` 是可选头，没有它不该让留帧失效。"""
    d = framedump.FrameDump(tmp_path)
    p = d.save(JPEG, matched=False, inliers=3, reason="weak", via=None)
    assert p is not None and p.exists()


def test_空帧不写(tmp_path: Path) -> None:
    d = framedump.FrameDump(tmp_path)
    assert d.save(b"", matched=False, inliers=0, reason=None, via=None) is None
    assert not (tmp_path / framedump.DIR_NAME).exists()


def test_超过上限删最旧的(tmp_path: Path) -> None:
    d = framedump.FrameDump(tmp_path)
    d.dir.mkdir(parents=True)
    # 名字前缀就是时间戳，字典序即时间序（_trim 依赖这一点）
    for i in range(framedump.MAX_FILES + 5):
        (d.dir / f"{i:06d}_x_miss_in1.jpg").write_bytes(JPEG)

    d.save(JPEG, matched=False, inliers=6, reason="weak", via="lan")

    names = sorted(p.name for p in d.dir.glob("*.jpg"))
    assert len(names) == framedump.MAX_FILES
    # 最旧的那几个走了，刚写的那个留着
    assert "000000_x_miss_in1.jpg" not in names
    assert any("in6" in n for n in names)


def test_目录建不出来时不抛(tmp_path: Path) -> None:
    """把目标目录位置占成一个普通文件 —— mkdir 必然失败。

    模拟的是盘满 / 权限不对 / 目录被人删了这一类环境问题：识别必须照旧返回。
    """
    (tmp_path / framedump.DIR_NAME).write_text("我是个文件，不是目录")
    d = framedump.FrameDump(tmp_path)

    assert d.save(JPEG, matched=False, inliers=6, reason="weak", via="lan") is None
