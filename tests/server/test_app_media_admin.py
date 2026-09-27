"""管理台「全部素材 / 重复」背后的接口。

四个接口一起测，是因为它们共享同一个「这个文件有没有被用」的口径
（`Server._asset_users`）：总表按它分 mapped / unused / orphan，删除按它决定
能不能删、要不要解绑，`/v1/admin/inbox` 也按它判「未入库」。口径一旦在某一处
漂了，表现是「总表说没人用、删除却说在用」这种两边各自看起来都对的矛盾。

最要紧的一条钉在 `test_参考图在用_拒绝删除且不代劳删照片`：被当参考图用的文件
**无论带不带 force 都不删**，也绝不顺手把照片删掉 —— 删照片会连带丢掉授权
（`photo_grant` 是 ON DELETE CASCADE），那不是一个删素材的按钮该有的后果。
"""

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from photoar.server import app, db


@pytest.fixture
def inbox_env(make_env, tmp_path):
    inbox = tmp_path / "nas" / "photos" / "_inbox"
    env = make_env(upload_dir_root=str(inbox))
    # 落地目录得等 make_env 建完 nas/photos 之后再建（make_env 用不带 exist_ok 的
    # mkdir 建 nas/photos，先建它的子目录就会撞上 FileExistsError）。
    inbox.mkdir(parents=True)
    env.inbox = inbox
    return env


def items_by_name(env):
    doc = env.body_json(env.get("/v1/admin/media"))
    return {i["name"]: i for i in doc["items"]}, doc


def test_总表覆盖四种状态(inbox_env):
    env = inbox_env
    vid = env.write_video("videos/v.mp4")
    mapped = env.ingest_ok(env.write_image("photos/_inbox/mapped.jpg", seed=1), video=vid)
    env.write_image("photos/_inbox/fresh.jpg", seed=2)          # 传上来没入库
    gone = env.ingest_ok(env.write_image("photos/other.jpg", seed=3))
    env.request("DELETE", f"/v1/photo/{gone}")                   # 落地目录外的孤儿记录
    by, doc = items_by_name(env)
    assert by["mapped.jpg"]["status"] == "mapped"
    assert by["mapped.jpg"]["usedAsRef"][0]["photoId"] == mapped
    assert by["v.mp4"]["status"] == "mapped" and by["v.mp4"]["usedAsVideo"][0]["photoId"] == mapped
    assert by["fresh.jpg"]["status"] == "unused" and by["fresh.jpg"]["inUploadDir"]
    assert by["fresh.jpg"]["assetId"] is None and by["fresh.jpg"]["deletable"]
    assert by["other.jpg"]["status"] == "orphan" and not by["other.jpg"]["deletable"]
    assert doc["counts"]["unused"] >= 1 and doc["counts"]["orphan"] >= 1


def test_总表的形状_顶层与每一行字段齐全且不缓存(inbox_env):
    # Task 9 的管理台逐字消费这些字段名，少一个或改了名字前端不会报错，只会显示空白。
    env = inbox_env
    env.write_image("photos/_inbox/one.jpg", seed=4)
    resp = env.get("/v1/admin/media")
    assert resp.status == 200
    assert resp.headers.get("Cache-Control") == "no-store"
    doc = env.body_json(resp)
    assert doc["uploadDir"] == str(env.inbox)
    assert set(doc["counts"]) == {
        "total", "mapped", "unused", "orphan", "duplicate", "rejected"
    }
    assert doc["counts"]["total"] == len(doc["items"])
    row = next(i for i in doc["items"] if i["name"] == "one.jpg")
    assert set(row) == {
        "path", "name", "kind", "bytes", "mtime", "exists",
        "inUploadDir", "generated", "deletable",
        "assetId", "sha256", "usedAsRef", "usedAsVideo",
        "status", "duplicateOf", "reject",
    }
    assert row["kind"] == "image" and row["exists"] is True
    assert row["generated"] is False and row["reject"] is None
    assert row["bytes"] == (env.inbox / "one.jpg").stat().st_size
    assert len(row["sha256"]) == 64


def test_没配落地目录时只列素材记录(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=6))
    by, doc = items_by_name(env)
    assert doc["uploadDir"] is None
    assert by["a.jpg"]["status"] == "mapped" and by["a.jpg"]["usedAsRef"][0]["photoId"] == pid
    assert by["a.jpg"]["inUploadDir"] is False and by["a.jpg"]["deletable"] is False


def test_同内容两份互相标重复(inbox_env):
    env = inbox_env
    a = env.write_image("photos/_inbox/a.jpg", seed=5)
    (env.inbox / "a-2.jpg").write_bytes(a.read_bytes())
    by, doc = items_by_name(env)
    assert by["a.jpg"]["sha256"] == by["a-2.jpg"]["sha256"]
    assert [d["path"] for d in by["a.jpg"]["duplicateOf"]] == [str(env.inbox / "a-2.jpg")]
    assert doc["counts"]["duplicate"] == 2


def test_落地目录文件的哈希按指纹缓存_只算一次(inbox_env, monkeypatch):
    # 总表每次打开都要给出落地目录里每个文件的 sha256，而手机视频动辄几百 MB ——
    # 不缓存的话每点一次「全部素材」就把整个落地目录重读一遍。
    env = inbox_env
    env.write_image("photos/_inbox/h1.jpg", seed=40)
    env.write_image("photos/_inbox/h2.jpg", seed=41)
    calls = []
    real = app.sha256_file
    monkeypatch.setattr(app, "sha256_file", lambda p: calls.append(str(p)) or real(p))
    items_by_name(env)
    items_by_name(env)
    assert sorted(calls) == [str(env.inbox / "h1.jpg"), str(env.inbox / "h2.jpg")]
    # 内容变了（大小变了 → 指纹变了）就得重算，不能拿旧哈希判重复。
    (env.inbox / "h1.jpg").write_bytes(b"changed")
    by, _ = items_by_name(env)
    assert calls[-1] == str(env.inbox / "h1.jpg") and len(calls) == 3
    assert by["h1.jpg"]["duplicateOf"] == []


def test_落地目录里的符号链接不列出_也不会和它指向的文件互标重复(inbox_env):
    # 链接和它指向的文件是**同一份**内容。列成两行、互标重复，管理员就会以为「删一份
    # 还剩一份」—— 而删哪一个最后都落在同一个文件上。
    env = inbox_env
    real = env.write_image("photos/_inbox/real.jpg", seed=3)
    (env.inbox / "link.jpg").symlink_to(real)
    by, doc = items_by_name(env)
    assert "link.jpg" not in by
    assert by["real.jpg"]["duplicateOf"] == [] and doc["counts"]["duplicate"] == 0


@pytest.mark.parametrize("real_has_asset", [False, True])
def test_按符号链接的路径删除会被拒_链接和它指向的文件都不动(inbox_env, real_has_asset):
    env = inbox_env
    real = env.write_image("photos/_inbox/real.jpg", seed=3)
    if real_has_asset:
        # 指向的是一条孤儿记录的文件：按解析后的路径能对上那条记录，更不能顺着删掉。
        env.request("DELETE", f"/v1/photo/{env.ingest_ok(real)}")
    link = env.inbox / "link.jpg"
    link.symlink_to(real)
    before = {a["id"] for a in env.srv.catalog.list_assets()}
    for q in ("", "&force=1"):
        r = env.request("DELETE", f"/v1/admin/media?path={link}{q}")
        assert r.status == 404, env.body_json(r)
        assert env.body_json(r)["error"] == "not_found"
    assert real.exists() and link.is_symlink()
    assert {a["id"] for a in env.srv.catalog.list_assets()} == before


def test_素材记录的路径后来被换成符号链接_不算文件也不删盘(inbox_env):
    # 入库时它是普通文件；之后被人（SMB/ssh）换成了指向别的文件的链接。记录里的那份
    # 内容已经不在这个路径上了 —— 当它不在，只删记录，绝不 unlink。
    env = inbox_env
    a = env.write_image("photos/_inbox/a.jpg", seed=18)
    env.request("DELETE", f"/v1/photo/{env.ingest_ok(a)}")
    b = env.write_image("photos/_inbox/b.jpg", seed=19)
    a.unlink()
    a.symlink_to(b)
    by, _ = items_by_name(env)
    row = by["a.jpg"]
    assert row["exists"] is False and row["deletable"] is False
    assert row["duplicateOf"] == [] and by["b.jpg"]["duplicateOf"] == []
    r = env.request("DELETE", f"/v1/admin/media?assetId={row['assetId']}")
    assert r.status == 200, env.body_json(r)
    assert env.body_json(r)["deleted"] == {"file": False, "assetIds": [row["assetId"]]}
    assert a.is_symlink() and b.exists()


def test_有记录的文件被换了内容_哈希按盘上现算_不会被标成旧内容的重复(inbox_env, monkeypatch):
    # 照片删掉后 asset 行还在；有人用 SMB 把同名文件换成了另一张图。这一行如果还报
    # 入库时的旧哈希，就会和「旧内容的另一份拷贝」互标重复 —— 管理员删掉那份拷贝，
    # 旧内容就一份都不剩了。
    env = inbox_env
    a = env.write_image("photos/_inbox/a.jpg", seed=4)
    env.request("DELETE", f"/v1/photo/{env.ingest_ok(a)}")
    old = a.read_bytes()
    (env.inbox / "a-old.jpg").write_bytes(old)

    calls = []
    real = app.sha256_file
    monkeypatch.setattr(app, "sha256_file", lambda p: calls.append(str(p)) or real(p))
    by, _ = items_by_name(env)
    # 指纹没变：用记录里的哈希，不重读文件。
    assert str(a) not in calls
    assert [d["path"] for d in by["a.jpg"]["duplicateOf"]] == [str(env.inbox / "a-old.jpg")]

    # 同样大小、不同内容，只有 mtime 能看出来变了 —— 最难发现的那种替换。
    new = bytearray(old)
    new[len(new) // 2] ^= 0xFF
    st = a.stat()
    a.write_bytes(bytes(new))
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    by, _ = items_by_name(env)
    assert by["a.jpg"]["sha256"] == hashlib.sha256(bytes(new)).hexdigest()
    assert by["a.jpg"]["duplicateOf"] == [] and by["a-old.jpg"]["duplicateOf"] == []


def test_入库被拒会留记录_成功后清掉(inbox_env):
    env = inbox_env
    p = env.write_image("photos/_inbox/twice.jpg", seed=7)
    env.ingest_ok(p)
    copy = env.inbox / "twice-copy.jpg"
    copy.write_bytes(p.read_bytes())
    resp = env.ingest(copy)                                      # 近重复（同一张图）→ 409
    assert resp.status == 409
    by, doc = items_by_name(env)
    rej = by["twice-copy.jpg"]["reject"]
    assert rej["code"] == env.body_json(resp)["error"]
    assert doc["counts"]["rejected"] == 1
    # 近重复要带上撞的是哪一张，管理台才能给「用它替换『X』的参考图」。
    assert rej["code"] == "near_duplicate" and rej["conflicts"][0]["photoId"]
    assert rej["message"] and isinstance(rej["at"], int)

    # 拒绝响应本身不能因为多记了一笔而变样。
    assert env.body_json(resp)["conflicts"]

    # 闸门关掉再入一次 → 成功 → 这条拒绝记录就该消失。
    assert env.patch_json("/v1/admin/config", {"ingest.dedup_gate": False}).status == 200
    env.ingest_ok(copy)
    by, doc = items_by_name(env)
    assert by["twice-copy.jpg"]["reject"] is None
    assert doc["counts"]["rejected"] == 0


def test_同一路径重复入库不算拒绝(inbox_env):
    # `already_ingested` 在批量导入与 tools/batch_ingest.py 里都被当成「这张已经好了」，
    # 记成拒绝的话，每重跑一次导入表，所有已映射的文件都会挂上一条「入库被拒」。
    env = inbox_env
    p = env.write_image("photos/_inbox/again.jpg", seed=13)
    env.ingest_ok(p)
    resp = env.ingest(p)
    assert resp.status == 409 and env.body_json(resp)["error"] == "already_ingested"
    by, doc = items_by_name(env)
    assert by["again.jpg"]["reject"] is None and doc["counts"]["rejected"] == 0


def test_换参考图被拒也留记录_换成功后清掉(inbox_env):
    env = inbox_env
    pa = env.ingest_ok(env.write_image("photos/a.jpg", seed=20))
    b = env.write_image("photos/b.jpg", seed=21)
    pb = env.ingest_ok(b)
    copy = env.inbox / "b-copy.jpg"
    copy.write_bytes(b.read_bytes())
    resp = env.post_json(f"/v1/photo/{pa}/ref", {"refPath": str(copy)})
    assert resp.status == 409 and env.body_json(resp)["error"] == "near_duplicate"
    by, _ = items_by_name(env)
    rej = by["b-copy.jpg"]["reject"]
    assert rej["code"] == "near_duplicate"
    assert [c["photoId"] for c in rej["conflicts"]] == [pb]

    assert env.patch_json("/v1/admin/config", {"ingest.dedup_gate": False}).status == 200
    assert env.post_json(f"/v1/photo/{pa}/ref", {"refPath": str(copy)}).status == 200
    by, _ = items_by_name(env)
    assert by["b-copy.jpg"]["reject"] is None
    assert by["b-copy.jpg"]["usedAsRef"][0]["photoId"] == pa


def test_删未入库的文件会删盘(inbox_env):
    env = inbox_env
    f = env.write_image("photos/_inbox/x.jpg", seed=8)
    resp = env.request("DELETE", f"/v1/admin/media?path={f}")
    assert resp.status == 200, env.body_json(resp)
    assert env.body_json(resp)["deleted"]["file"] is True
    assert not f.exists()


def test_删掉被拒的文件连拒绝记录一起清(inbox_env):
    env = inbox_env
    p = env.write_image("photos/_inbox/keep.jpg", seed=14)
    env.ingest_ok(p)
    copy = env.inbox / "dup.jpg"
    copy.write_bytes(p.read_bytes())
    assert env.ingest(copy).status == 409
    resp = env.request("DELETE", f"/v1/admin/media?path={copy}")
    assert resp.status == 200, env.body_json(resp)
    assert env.body_json(resp) == {
        "deleted": {"file": True, "assetIds": []}, "detachedPhotos": []
    }
    assert not copy.exists()
    assert env.srv.catalog.list_rejects() == []


def test_参考图在用_拒绝删除且不代劳删照片(inbox_env):
    env = inbox_env
    f = env.write_image("photos/_inbox/ref.jpg", seed=9)
    pid = env.ingest_ok(f)
    for q in ("", "&force=1"):
        resp = env.request("DELETE", f"/v1/admin/media?path={f}{q}")
        assert resp.status == 409
        doc = env.body_json(resp)
        assert doc["error"] == "ref_in_use" and doc["usedAsRef"][0]["photoId"] == pid
        # 两个 409 都带齐 usedAsRef 与 usedAsVideo：Task 9 的管理台逐字读这两个字段，
        # 少一个前端拿到的是 undefined。
        assert doc["usedAsVideo"] == []
    assert f.exists() and env.get(f"/v1/photo/{pid}").status == 200


def test_视频在用_不带_force_拒绝_带了就解绑再删(inbox_env):
    env = inbox_env
    vid = env.write_video("photos/_inbox/v.mp4")
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=10), video=vid)
    r1 = env.request("DELETE", f"/v1/admin/media?path={vid}")
    assert r1.status == 409 and env.body_json(r1)["error"] == "in_use"
    assert env.body_json(r1)["usedAsVideo"][0]["photoId"] == pid
    assert env.body_json(r1)["usedAsRef"] == []
    r2 = env.request("DELETE", f"/v1/admin/media?path={vid}&force=1")
    assert r2.status == 200
    assert env.body_json(r2)["detachedPhotos"] == [pid]
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["videoPath"] is None
    assert not vid.exists()


def _race_after_first_look(monkeypatch, catalog, concurrent):
    """模拟「另一个请求恰好插在删除请求看完引用之后」：第一次查引用照常返回（那一刻
    的快照），然后立刻执行 `concurrent`（那个并发请求的写入），快照随之过期。"""
    real = catalog.photos_referencing_asset
    fired = []

    def wrapper(asset_id):
        rows = real(asset_id)
        if not fired:
            fired.append(True)
            concurrent()
        return rows

    monkeypatch.setattr(catalog, "photos_referencing_asset", wrapper)
    return fired


def test_强制删视频时恰好又被另一张照片配上_回409带全字段且一张都不解绑(inbox_env, monkeypatch):
    env = inbox_env
    cat = env.srv.catalog
    vid = env.write_video("photos/_inbox/v.mp4")
    p1 = env.ingest_ok(env.write_image("photos/a.jpg", seed=22), video=vid)
    p2 = env.ingest_ok(env.write_image("photos/b.jpg", seed=23))
    row1 = cat.get_photo(p1)
    fired = _race_after_first_look(
        monkeypatch,
        cat,
        lambda: cat.set_photo_video(
            p2,
            video_asset_id=row1["video_asset_id"],
            playable_asset_id=row1["playable_asset_id"],
        ),
    )
    r = env.request("DELETE", f"/v1/admin/media?path={vid}&force=1")
    assert fired
    assert r.status == 409, env.body_json(r)
    doc = env.body_json(r)
    assert doc["error"] == "in_use" and doc["usedAsRef"] == []
    assert {u["photoId"] for u in doc["usedAsVideo"]} == {p1, p2}
    # 要么全做、要么一张都不动：p1 不能已经被解绑了却回一个 409。
    assert cat.get_photo(p1)["video_asset_id"] == row1["video_asset_id"]
    assert vid.exists() and cat.get_asset(row1["video_asset_id"]) is not None


def test_删孤儿图时恰好被配成参考图_回ref_in_use而不是in_use(inbox_env, monkeypatch):
    env = inbox_env
    cat = env.srv.catalog
    f = env.write_image("photos/_inbox/o.jpg", seed=24)
    env.request("DELETE", f"/v1/photo/{env.ingest_ok(f)}")
    aid = cat.get_asset_by_path(str(f))["id"]
    p2 = env.ingest_ok(env.write_image("photos/b.jpg", seed=25))
    row2 = cat.get_photo(p2)
    fired = _race_after_first_look(
        monkeypatch,
        cat,
        lambda: cat.set_photo_ref(
            p2,
            ref_asset_id=aid,
            self_score=int(row2["self_score"]),
            thumb_path=str(row2["thumb_path"]),
        ),
    )
    r = env.request("DELETE", f"/v1/admin/media?assetId={aid}&force=1")
    assert fired
    assert r.status == 409, env.body_json(r)
    doc = env.body_json(r)
    assert doc["error"] == "ref_in_use"
    assert [u["photoId"] for u in doc["usedAsRef"]] == [p2] and doc["usedAsVideo"] == []
    assert f.exists() and cat.get_asset(aid) is not None


def test_删除成功后清拒绝记录失败_不会把已删的文件报成500(inbox_env, monkeypatch):
    # 文件已经删了，这时回 500 会让管理员以为没删成、再点一次，收到一个莫名的 404。
    env = inbox_env
    f = env.write_image("photos/_inbox/x.jpg", seed=26)

    def boom(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(env.srv.catalog, "clear_reject", boom)
    r = env.request("DELETE", f"/v1/admin/media?path={f}")
    assert r.status == 200, env.body_json(r)
    assert env.body_json(r)["deleted"]["file"] is True and not f.exists()


def test_强制删源视频会连带删掉只属于它的转码产物(inbox_env, fake_ffprobe):
    env = inbox_env
    # 4K 源 → 入库时必须转码，于是这张照片有两个视频 asset：源文件与 data/playable 里的产物。
    env.cfg.ffprobe = fake_ffprobe(name="ffprobe4k", height=2160, width=3840)
    env.srv.cfg = env.cfg
    vid = env.write_video("photos/_inbox/big.mp4")
    resp = env.ingest(env.write_image("photos/a.jpg", seed=15), video=vid)
    assert resp.status == 201 and env.body_json(resp)["transcoded"] is True
    pid = env.body_json(resp)["photoId"]

    items = env.body_json(env.get("/v1/admin/media"))["items"]
    gen = next(i for i in items if i["generated"])
    assert gen["status"] == "mapped" and gen["deletable"]
    assert gen["usedAsVideo"] == [{"photoId": pid, "title": None, "transcoded": True}]
    src = next(i for i in items if i["path"] == str(vid))
    assert src["usedAsVideo"][0]["transcoded"] is False

    r = env.request("DELETE", f"/v1/admin/media?path={vid}&force=1")
    assert r.status == 200, env.body_json(r)
    doc = env.body_json(r)
    assert doc["detachedPhotos"] == [pid]
    assert doc["deleted"]["file"] is True
    assert set(doc["deleted"]["assetIds"]) == {src["assetId"], gen["assetId"]}
    assert not vid.exists()
    # 转码产物只属于这张照片，解绑之后没人要了 —— 留着就是 data/ 里一份永远不会被
    # 再引用的几十 MB 文件。
    assert not Path(gen["path"]).exists()
    left = {i["path"] for i in env.body_json(env.get("/v1/admin/media"))["items"]}
    assert gen["path"] not in left and str(vid) not in left


def test_data目录是符号链接时_按解析后的路径删在播的转码产物也会被拦下(inbox_env, fake_ffprobe):
    # 转码产物的 asset 存的是未解析的 `cfg.playable_dir/...`。换一种写法（解析后的真实
    # 路径）来删，如果只按字面去对 asset 记录，就会被当成「没有记录的生成物」直接删盘，
    # 绕过「有没有照片在播它」那道检查。
    env = inbox_env
    link = env.tmp / "data-link"
    link.symlink_to(env.cfg.data_dir, target_is_directory=True)
    env.cfg.data_dir = link
    env.cfg.ffprobe = fake_ffprobe(name="ffprobe4k", height=2160, width=3840)
    env.srv.cfg = env.cfg
    vid = env.write_video("videos/big.mp4")
    resp = env.ingest(env.write_image("photos/a.jpg", seed=17), video=vid)
    assert resp.status == 201 and env.body_json(resp)["transcoded"] is True
    pid = env.body_json(resp)["photoId"]
    gen = next(
        i for i in env.body_json(env.get("/v1/admin/media"))["items"] if i["generated"]
    )
    real = Path(gen["path"]).resolve()
    assert str(real) != gen["path"]
    r = env.request("DELETE", f"/v1/admin/media?path={real}")
    assert r.status == 409 and env.body_json(r)["error"] == "in_use", env.body_json(r)
    assert env.body_json(r)["usedAsVideo"][0]["photoId"] == pid
    assert real.exists()


def test_落地目录外的孤儿只删记录不删盘(inbox_env):
    env = inbox_env
    f = env.write_image("photos/keep.jpg", seed=11)
    pid = env.ingest_ok(f)
    env.request("DELETE", f"/v1/photo/{pid}")
    by, _ = items_by_name(env)
    aid = by["keep.jpg"]["assetId"]
    resp = env.request("DELETE", f"/v1/admin/media?assetId={aid}")
    assert resp.status == 200
    assert env.body_json(resp)["deleted"] == {"file": False, "assetIds": [aid]}
    assert f.exists()
    by, _ = items_by_name(env)
    assert "keep.jpg" not in by


def test_删素材_参数缺了是400_找不到是404(inbox_env):
    env = inbox_env
    r = env.request("DELETE", "/v1/admin/media")
    assert r.status == 400 and env.body_json(r)["error"] == "bad_request"
    r = env.request("DELETE", f"/v1/admin/media?assetId={'f' * 32}")
    assert r.status == 404 and env.body_json(r)["error"] == "not_found"
    r = env.request("DELETE", f"/v1/admin/media?path={env.inbox / 'nope.jpg'}")
    assert r.status == 404 and env.body_json(r)["error"] == "not_found"
    # 白名单外的路径不是「找不到」，是根本不该问：与全服务其余接口同一条规矩
    # （safepath 模块 docstring：越界一律 403 path_denied 并记日志）。
    r = env.request("DELETE", f"/v1/admin/media?path={env.outside / 'secret.jpg'}")
    assert r.status == 403 and env.body_json(r)["error"] == "path_denied"
    assert (env.outside / "secret.jpg").exists()


def test_删asset行之前会再确认没人引用(inbox_env):
    # HTTP 层已经挡过一遍了；这一层是给「两个请求交错」留的底：刚判完没人用，
    # 另一个请求就把它配给了一张照片 —— 那时删掉 asset 行会让那张照片指向空。
    env = inbox_env
    pid = env.ingest_ok(env.write_image("photos/_inbox/r.jpg", seed=16))
    aid = env.srv.catalog.get_photo(pid)["ref_asset_id"]
    with pytest.raises(db.AssetInUse) as exc:
        env.srv.catalog.delete_asset(aid, detach_videos=True)
    # 异常自带锁里分好的类：HTTP 层照它给 ref_in_use / in_use 并列出是谁。
    assert [p["id"] for p in exc.value.refs] == [pid] and exc.value.videos == []
    assert env.srv.catalog.get_asset(aid) is not None


def test_素材接口要管理员(inbox_env):
    env = inbox_env
    v = env.viewer()
    assert env.get("/v1/admin/media", as_=v).status == 403
    assert env.request("DELETE", "/v1/admin/media?path=/x", as_=v).status == 403
    assert env.request("POST", "/v1/admin/duplicates/scan", as_=v).status == 403


def test_近重复扫描能找出闸门关着时进来的那一对(inbox_env):
    env = inbox_env
    assert env.patch_json("/v1/admin/config", {"ingest.dedup_gate": False}).status == 200
    a = env.write_image("photos/a.jpg", seed=12)
    b = env.nas / "photos" / "b.jpg"
    b.write_bytes(a.read_bytes())
    pa, pb = env.ingest_ok(a), env.ingest_ok(b)
    env.ingest_ok(env.write_image("photos/c.jpg", seed=99))
    doc = env.body_json(env.request("POST", "/v1/admin/duplicates/scan"))
    pairs = {frozenset((p["a"]["photoId"], p["b"]["photoId"])) for p in doc["pairs"]}
    assert frozenset((pa, pb)) in pairs
    assert len(pairs) == 1
    assert doc["scanned"] == 3 and doc["truncated"] is False
    # 管理台原样显示这句话，逐字钉住（Ruling 8 定的文案）。
    assert doc["note"] == (
        "用入库时存下的特征比对，比识别时的口径保守：列出来的一定会互相干扰；"
        "没列出来的不保证没有（关着闸门入库的照片尤其要留意）。"
    )
    pair = doc["pairs"][0]
    assert set(pair) == {"a", "b", "inliers"} and pair["inliers"] > 0
    assert set(pair["a"]) == {"photoId", "title", "selfScore"}
    assert isinstance(doc["elapsedMs"], int)


def test_扫描超过时间上限就停下_只给已扫的部分(inbox_env, monkeypatch):
    env = inbox_env
    for s in (30, 31, 32):
        env.ingest_ok(env.write_image(f"photos/t{s}.jpg", seed=s))
    monkeypatch.setattr(app, "_DUP_SCAN_BUDGET_S", 0.0)
    doc = env.body_json(env.request("POST", "/v1/admin/duplicates/scan"))
    # 每张之间才看表：至少扫完一张，然后停。
    assert doc["truncated"] is True and doc["scanned"] == 1
