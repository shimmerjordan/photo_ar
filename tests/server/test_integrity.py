"""spec §6.1 的四种素材变化：内容变 / 只 mtime 变 / 文件不在 / 文件回来。

这一层的意义在于 asset 记的是**别人也会动**的文件。测试因此都是"先入库，再在
文件系统上真的动它，再校验"，不是给 catalog 塞假指纹。
"""

import os
import time

import pytest

from photoar.server import integrity as I
from photoar.server.db import Catalog


@pytest.fixture
def cat(tmp_path):
    return Catalog(tmp_path / "catalog.db")


def _add(cat, path, kind="video"):
    size, mtime = I.stat_fingerprint(path)
    return cat.upsert_asset(
        nas_path=str(path),
        kind=kind,
        sha256=I.sha256_file(path),
        bytes_=size,
        mtime=mtime,
    )


def test_unchanged_file_is_ok_without_hashing(cat, tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 1000)
    aid = _add(cat, p)
    r = I.verify_asset(cat, cat.get_asset(aid))
    assert r.status == I.STATUS_OK
    assert not r.hashed, "指纹一致时绝不能哈希——上万个素材每周一次会跑到天亮"


def test_mtime_only_change_is_detected_and_fingerprint_refreshed(cat, tmp_path):
    """内容没变、mtime 变了（rsync、网盘挂载点重挂都会这样）。

    必须判成 mtime_only 而不是 content_changed：后者会把引用它的照片标成
    ref_stale，用户看到一堆莫名标红的照片。
    """
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 1000)
    aid = _add(cat, p)
    os.utime(p, (time.time() + 10, time.time() + 10))

    r = I.verify_asset(cat, cat.get_asset(aid))
    assert r.status == I.STATUS_MTIME_ONLY
    assert r.hashed
    # 刷新过指纹，所以再校验一次就不用再哈希了
    r2 = I.verify_asset(cat, cat.get_asset(aid))
    assert r2.status == I.STATUS_OK and not r2.hashed


def test_content_change_marks_referencing_photo_stale(cat, tmp_path):
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(b"a" * 1000)
    ref_id = _add(cat, ref, kind="image")
    cat.insert_photo(
        photo_id="p" * 32,
        ref_asset_id=ref_id,
        video_asset_id=None,
        playable_asset_id=None,
        title=None,
        print_width_m=0.152,
        thumb_path="/tmp/x.jpg",
        self_score=60,
    )
    ref.write_bytes(b"b" * 2000)

    r = I.verify_asset(cat, cat.get_asset(ref_id))
    assert r.status == I.STATUS_CONTENT_CHANGED
    assert r.stale_photo_ids == ("p" * 32,)
    assert cat.get_photo("p" * 32)["ref_stale"] == 1
    # 指纹更新成新内容：下次校验不再重复报同一件事
    assert I.verify_asset(cat, cat.get_asset(ref_id)).status == I.STATUS_OK


def test_video_content_change_does_not_mark_photo_stale(cat, tmp_path):
    """视频换内容不影响识别 —— 播的是文件本身，没有派生特征失效。

    把它也标 ref_stale 会让"这张照片的识别特征过期了"这个信号失去意义。
    """
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(b"a" * 10)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"v" * 100)
    ref_id, vid_id = _add(cat, ref, "image"), _add(cat, vid)
    cat.insert_photo(
        photo_id="q" * 32,
        ref_asset_id=ref_id,
        video_asset_id=vid_id,
        playable_asset_id=vid_id,
        title=None,
        print_width_m=0.152,
        thumb_path="/tmp/x.jpg",
        self_score=60,
    )
    vid.write_bytes(b"w" * 200)

    r = I.verify_asset(cat, cat.get_asset(vid_id))
    assert r.status == I.STATUS_CONTENT_CHANGED
    assert r.stale_photo_ids == ()
    assert cat.get_photo("q" * 32)["ref_stale"] == 0


def test_missing_then_restored(cat, tmp_path):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 1000)
    aid = _add(cat, p)
    fingerprint = I.stat_fingerprint(p)

    p.unlink()
    r = I.verify_asset(cat, cat.get_asset(aid))
    assert r.status == I.STATUS_MISSING and not r.usable
    assert cat.get_asset(aid)["missing"] == 1

    # 用户把文件放回原处，mtime 也一样（比如从回收站还原）
    p.write_bytes(b"x" * 1000)
    os.utime(p, (fingerprint[1] / 1000, fingerprint[1] / 1000))
    r = I.verify_asset(cat, cat.get_asset(aid))
    assert r.status == I.STATUS_RESTORED and r.usable
    assert cat.get_asset(aid)["missing"] == 0


def test_no_automatic_rebinding_when_file_moves(cat, tmp_path):
    """文件被移到同一目录下的新名字，内容完全一样。仍然只报 missing。

    spec §6.1 明确要求不做路径追踪：把"外婆生日"的视频自动重绑到一个碰巧
    同名同大小的文件上，用户会在家人面前才发现。
    """
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * 1000)
    aid = _add(cat, p)
    p.rename(tmp_path / "v-renamed.mp4")

    r = I.verify_asset(cat, cat.get_asset(aid))
    assert r.status == I.STATUS_MISSING
    assert cat.get_asset(aid)["nas_path"] == str(p), "路径不能被悄悄改掉"


def test_verify_all_covers_every_asset(cat, tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"v{i}.mp4"
        p.write_bytes(bytes([i]) * (100 + i))
        _add(cat, p)
        paths.append(p)
    paths[1].unlink()

    results = {r.nas_path: r.status for r in I.verify_all(cat)}
    assert results[str(paths[0])] == I.STATUS_OK
    assert results[str(paths[1])] == I.STATUS_MISSING
    assert results[str(paths[2])] == I.STATUS_OK


def test_stat_fingerprint_is_stable_across_calls(tmp_path):
    """mtime 的浮点→毫秒转换只有这一处，所以同一个文件必须给出完全相同的值。

    各处自己 `int(st.st_mtime*1000)` 会在边界上差 1，让"mtime 变了"随机假
    成立，每次假成立就多一次全文件 sha256 —— 上万素材时是分钟级的浪费。
    """
    p = tmp_path / "a.bin"
    p.write_bytes(b"z" * 12345)
    assert I.stat_fingerprint(p) == I.stat_fingerprint(p)
