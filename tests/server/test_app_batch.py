"""批量导入解析、导出、双向映射这几个接口。

计划构建本身在 `test_batch.py` 里测（纯逻辑，不用起服务）。这个文件只管 HTTP 层多出来
的那几件事：鉴权、体积上限、**路径校验真的接到了 roots 和文件系统上**、导出的
Content-Disposition、以及「导出能被导入吃回去」这个往返。
"""

from __future__ import annotations

import io
import zipfile

from photoar import sheet as sheet_mod
from photoar.server import batch


def body_sheet(resp) -> list[list[str]]:
    """把导出响应的 body 解析成行。"""
    return sheet_mod.read_table(resp.body).rows


def upload(env, rows: list[list[str]], *, fmt: str = "xlsx", **kw):
    data = (
        sheet_mod.write_xlsx(rows)
        if fmt == "xlsx"
        else sheet_mod.write_csv_bytes(rows)
    )
    return env.request("POST", "/v1/admin/import/parse", body=data, **kw)


H = batch.TEMPLATE_HEADER


# ---------------------------------------------------------------- 鉴权


def test_导入解析要管理员(make_env):
    env = make_env()
    v = env.viewer()
    resp = upload(env, [H, ["张三", "", "", "", "", "", ""]], as_=v)
    assert resp.status == 403


def test_导出要管理员(make_env):
    env = make_env()
    v = env.viewer()
    assert env.get("/v1/admin/export/template", as_=v).status == 403
    assert env.get("/v1/admin/export/users", as_=v).status == 403
    assert env.get("/v1/admin/mapping", as_=v).status == 403
    assert env.get("/v1/admin/videos", as_=v).status == 403


def test_解除视频关联要管理员(make_env):
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    pid = env.ingest_ok(ref)
    # 两道检查，顺序与 _photo_attach_video 一致：先「这张授权给你了吗」，再「要管理员」。
    # 两者都是 403 但 error code 不同 —— 一个没授权的 viewer 该知道的是「这张不是
    # 你的」，而不是「这个操作要管理员」（后者会让他去找管理员要权限，而权限不是
    # 问题所在）。
    v = env.viewer()
    denied = env.request("DELETE", f"/v1/photo/{pid}/video", as_=v)
    assert denied.status == 403
    assert env.body_json(denied)["error"] == "forbidden"
    # 授权之后再试，这时挡住他的才是「要管理员」
    v2 = env.viewer(name="有授权的", photo_ids=[pid])
    need_admin = env.request("DELETE", f"/v1/photo/{pid}/video", as_=v2)
    assert need_admin.status == 403
    assert "管理员" in env.body_json(need_admin)["message"]


# ---------------------------------------------------------------- 导入解析


def test_解析一份合格的表(make_env):
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    resp = upload(
        env, [H, ["张三", "", "", str(ref), str(vid), "合照", "152"]]
    )
    assert resp.status == 200
    doc = env.body_json(resp)
    assert doc["errors"] == []
    assert doc["format"] == "xlsx"
    (row,) = doc["rows"]
    assert row["errors"] == []
    assert row["actions"] == ["user", "photo", "video", "grant"]
    assert row["printWidthMm"] == 152
    assert doc["summary"]["okRows"] == 1


def test_解析不写任何东西进库(make_env):
    # 这是整个设计的前提。破了它的话「预演」就是假的。
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    before_users = env.body_json(env.get("/v1/admin/users"))
    before_photos = env.body_json(env.get("/v1/photos"))["total"]
    resp = upload(env, [H, ["新用户", "", "", str(ref), "", "", ""]])
    assert resp.status == 200
    assert env.body_json(resp)["summary"]["okRows"] == 1, "这一行确实是可执行的"
    after_users = env.body_json(env.get("/v1/admin/users"))
    after_photos = env.body_json(env.get("/v1/photos"))["total"]
    assert len(after_users) == len(before_users)
    assert after_photos == before_photos


def test_csv_也能解析_并如实报告格式(make_env):
    env = make_env()
    resp = upload(env, [H, ["张三", "", "", "", "", "", ""]], fmt="csv")
    assert resp.status == 200
    assert env.body_json(resp)["format"] == "csv"


def test_空请求体(make_env):
    env = make_env()
    resp = env.request("POST", "/v1/admin/import/parse", body=b"")
    assert resp.status == 400
    assert env.body_json(resp)["error"] == "empty_body"


def test_不是表格的文件(make_env):
    env = make_env()
    # 一个 JPEG。detect_format 会认成 csv（不是 zip），然后编码解不开。
    resp = env.request("POST", "/v1/admin/import/parse", body=b"\xff\xd8\xff\xe0" * 50)
    assert resp.status == 400
    assert env.body_json(resp)["error"] == "bad_encoding"


def test_坏掉的_xlsx(make_env):
    env = make_env()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    resp = env.request("POST", "/v1/admin/import/parse", body=buf.getvalue())
    assert resp.status == 400
    assert env.body_json(resp)["error"] == "bad_xlsx"


def test_超过体积上限(make_env):
    env = make_env()
    resp = env.request(
        "POST",
        "/v1/admin/import/parse",
        body=b"x" * (env.srv.MAX_IMPORT_BYTES + 1),
    )
    assert resp.status == 413


# ------------------------------------------------- 路径校验真的接上了文件系统


def test_白名单外的路径在预览阶段就被指出(make_env):
    env = make_env()
    outside = env.outside / "secret.jpg"
    resp = upload(env, [H, ["张三", "", "", str(outside), "", "", ""]])
    assert resp.status == 200, "整份表不该因为一行路径不对而失败"
    (row,) = env.body_json(resp)["rows"]
    assert row["errors"]
    assert row["errors"][0].startswith("照片路径：")
    # 最常见的真实原因是填了宿主机路径而不是容器内路径 —— 得说出来
    assert "容器内" in row["errors"][0]


def test_不存在的文件在预览阶段就被指出(make_env):
    env = make_env()
    ghost = env.nas / "photos" / "没有这个文件.jpg"
    resp = upload(env, [H, ["张三", "", "", str(ghost), "", "", ""]])
    (row,) = env.body_json(resp)["rows"]
    assert any("文件不存在" in e for e in row["errors"])


def test_把视频填进照片那一列会被指出(make_env):
    # 这是最难自己发现的一种错：两列都填了合法路径，只是填反了。
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    resp = upload(env, [H, ["张三", "", "", str(vid), str(ref), "", ""]])
    (row,) = env.body_json(resp)["rows"]
    assert len(row["errors"]) == 2, row["errors"]
    assert "要图片" in row["errors"][0] and "是视频" in row["errors"][0]
    assert "要视频" in row["errors"][1] and "是图片" in row["errors"][1]


def test_目录而不是文件(make_env):
    env = make_env()
    resp = upload(env, [H, ["张三", "", "", str(env.nas / "photos"), "", "", ""]])
    (row,) = env.body_json(resp)["rows"]
    assert any("目录" in e for e in row["errors"])


def test_口令要回显_否则批量建不出管理员(make_env):
    # 执行者是浏览器，它建管理员时必须把口令放进 POST /v1/admin/users 的请求体。
    # 不回显 = 模板里那一列是个填了也没用的摆设。
    env = make_env()
    resp = upload(env, [H, ["老板", "admin", "秘密口令", "", "", "", ""]])
    (row,) = env.body_json(resp)["rows"]
    assert row["password"] == "秘密口令"
    assert row["hasPassword"] is True


def test_带口令的解析响应不许落盘(make_env):
    # 回显本身不多泄露什么（口令几秒前就在这位管理员上传的文件里），要防的是缓存。
    env = make_env()
    resp = upload(env, [H, ["老板", "admin", "秘密口令", "", "", "", ""]])
    assert resp.headers["Cache-Control"] == "no-store"


def test_回显的口令能真的建出管理员(make_env):
    # 这条才是「回显」的意义所在：把解析出来的行直接喂给建用户接口。
    env = make_env()
    resp = upload(env, [H, ["新管理员", "admin", "口令12345", "", "", "", ""]])
    (row,) = env.body_json(resp)["rows"]
    created = env.post_json(
        "/v1/admin/users",
        {"name": row["userName"], "role": row["role"], "password": row["password"]},
    )
    assert created.status == 201, env.body_json(created)
    # 而且这个口令是真能登录的
    creds = env.login("新管理员", "口令12345")
    assert creds.role == "admin"


# ---------------------------------------------------------------- 导出


def test_模板导出是能打开的_xlsx(make_env):
    env = make_env()
    resp = env.get("/v1/admin/export/template")
    assert resp.status == 200
    assert resp.headers["Content-Type"].endswith("spreadsheetml.sheet")
    assert resp.body[:4] == b"PK\x03\x04"
    rows = body_sheet(resp)
    assert rows[0] == batch.TEMPLATE_HEADER
    assert len(rows) == 1 + len(batch.TEMPLATE_EXAMPLES)


def test_模板导出的中文文件名两种写法都给(make_env):
    # 只写 filename= 的话浏览器按 latin-1 解，中文会乱码或整段丢掉。
    env = make_env()
    cd = env.get("/v1/admin/export/template").headers["Content-Disposition"]
    assert cd.startswith("attachment;")
    assert "filename=" in cd
    assert "filename*=UTF-8''" in cd


def test_导出不许被缓存(make_env):
    # 改完用户再点导出拿到旧表，而且看不出是缓存。
    env = make_env()
    assert env.get("/v1/admin/export/users").headers["Cache-Control"] == "no-store"


def test_模板导出成_csv(make_env):
    env = make_env()
    resp = env.get("/v1/admin/export/template?format=csv")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    assert resp.body.startswith(b"\xef\xbb\xbf"), "Excel 要 BOM 才认 UTF-8"
    assert body_sheet(resp)[0] == batch.TEMPLATE_HEADER


def test_没有这种导出(make_env):
    env = make_env()
    resp = env.get("/v1/admin/export/随便")
    assert resp.status == 404
    assert env.body_json(resp)["error"] == "unknown_export"
    # 得把可选项列出来
    assert "template" in env.body_json(resp)["message"]


def test_导出格式只能是两种(make_env):
    env = make_env()
    resp = env.get("/v1/admin/export/template?format=pdf")
    assert resp.status == 400
    assert env.body_json(resp)["error"] == "bad_format"


def test_用户导出的表头与模板一致_否则导回去那条路就断了(make_env):
    env = make_env()
    rows = body_sheet(env.get("/v1/admin/export/users"))
    assert rows[0] == batch.TEMPLATE_HEADER


def test_用户导出_一个用户一张照片一行(make_env):
    env = make_env()
    ref1 = env.write_image("photos/a.jpg", seed=1)
    ref2 = env.write_image("photos/b.jpg", seed=2)
    p1 = env.ingest_ok(ref1, title="第一张")
    p2 = env.ingest_ok(ref2, title="第二张")
    env.viewer(name="张三", photo_ids=[p1, p2])
    rows = body_sheet(env.get("/v1/admin/export/users"))
    mine = [r for r in rows[1:] if r and r[0] == "张三"]
    assert len(mine) == 2
    assert {r[5] for r in mine} == {"第一张", "第二张"}


def test_用户导出_没有授权的用户也占一行(make_env):
    # 少了那一行的话，「导出 → 改 → 导入」会把没授权的用户弄丢，而人不会注意到。
    env = make_env()
    env.viewer(name="光棍用户")
    rows = body_sheet(env.get("/v1/admin/export/users"))
    assert any(r and r[0] == "光棍用户" for r in rows[1:])


def test_用户导出的口令列永远是空的(make_env):
    # 库里只有散列，导不出原文；留空的语义正好是「不改口令」。
    env = make_env()
    env.viewer(name="张三")
    rows = body_sheet(env.get("/v1/admin/export/users"))
    for r in rows[1:]:
        assert len(r) < 3 or r[2] == "", r


def test_导出的用户表能被导入接口吃回去(make_env):
    # 「导出 → 在 Excel 里改 → 导回去」是这份表的主要用途。这条往返一断，人会以为
    # 是自己表格改坏了。
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(ref, video=vid, title="合照")
    env.viewer(name="张三", photo_ids=[pid])

    exported = env.get("/v1/admin/export/users").body
    resp = env.request("POST", "/v1/admin/import/parse", body=exported)
    assert resp.status == 200
    doc = env.body_json(resp)
    assert doc["errors"] == []
    mine = [r for r in doc["rows"] if r["userName"] == "张三"]
    assert len(mine) == 1
    row = mine[0]
    assert row["errors"] == [], row
    assert row["photoPath"] == str(ref)
    assert row["videoPath"] == str(vid)
    assert row["title"] == "合照"
    assert row["printWidthMm"] == 152


def test_未知宽度导出成空_而不是_0(make_env):
    # 导成 "0" 虽然导入侧也当未知，但会让人以为库里真记着一个 0。
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    pid = env.body_json(env.post_json("/v1/photo", {"refPath": str(ref)}))["photoId"]
    env.viewer(name="张三", photo_ids=[pid])
    rows = body_sheet(env.get("/v1/admin/export/users"))
    mine = [r for r in rows[1:] if r and r[0] == "张三"]
    assert len(mine) == 1
    assert len(mine[0]) < 7 or mine[0][6] == ""


def test_映射导出的表头与模板不同_刻意的(make_env):
    # 硬凑成同一套表头会让「导出映射 → 直接导入」看起来可行，实际上会把 photoId
    # 当用户名去建用户。
    env = make_env()
    rows = body_sheet(env.get("/v1/admin/export/mapping"))
    assert rows[0] == batch.MAPPING_HEADER
    assert rows[0] != batch.TEMPLATE_HEADER


def test_映射导出一行一张照片(make_env):
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    env.ingest_ok(ref, video=vid, title="有视频的")
    env.ingest_ok(env.write_image("photos/b.jpg", seed=2), title="没视频的")
    rows = body_sheet(env.get("/v1/admin/export/mapping"))
    assert len(rows) == 3
    by_title = {r[1]: r for r in rows[1:]}
    assert by_title["有视频的"][3] == str(vid)
    assert len(by_title["没视频的"]) < 4 or by_title["没视频的"][3] == ""


def test_空库导出也是个能打开的文件(make_env):
    # 新装机时库是空的。那时不该给一个 0 字节文件 —— 人会以为下载失败了。
    env = make_env()
    for what in ("template", "users", "mapping"):
        resp = env.get(f"/v1/admin/export/{what}")
        assert resp.status == 200, what
        assert body_sheet(resp), what


# ---------------------------------------------------------------- 双向映射


def test_照片侧的映射现状(make_env):
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(ref, video=vid, title="合照")
    doc = env.body_json(env.get("/v1/admin/mapping"))
    assert doc["total"] == 1
    (row,) = doc["photos"]
    assert row["photoId"] == pid
    assert row["refPath"] == str(ref)
    assert row["videoPath"] == str(vid)
    assert row["grantCount"] == 0


def test_映射现状带上被授权人数(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    env.viewer(name="甲", photo_ids=[pid])
    env.viewer(name="乙", photo_ids=[pid])
    (row,) = env.body_json(env.get("/v1/admin/mapping"))["photos"]
    assert row["grantCount"] == 2


def test_视频侧反查_一段视频配给多张照片(make_env):
    # 一段迎宾视频配给很多张照片是真实用法，而改它之前要知道会影响谁。
    env = make_env()
    vid = env.write_video("videos/shared.mp4")
    p1 = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid, title="甲")
    p2 = env.ingest_ok(env.write_image("photos/b.jpg", seed=2), video=vid, title="乙")
    doc = env.body_json(env.get("/v1/admin/videos"))
    assert doc["total"] == 1
    (entry,) = doc["videos"]
    assert entry["path"] == str(vid)
    assert {p["photoId"] for p in entry["photos"]} == {p1, p2}
    assert {p["title"] for p in entry["photos"]} == {"甲", "乙"}


def test_视频侧反查_没配视频的照片单独列出(make_env):
    env = make_env()
    vid = env.write_video("videos/a.mp4")
    env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)
    naked = env.ingest_ok(env.write_image("photos/b.jpg", seed=2), title="还没配")
    doc = env.body_json(env.get("/v1/admin/videos"))
    assert [p["photoId"] for p in doc["unmapped"]] == [naked]


def test_视频侧反查_空库(make_env):
    env = make_env()
    doc = env.body_json(env.get("/v1/admin/videos"))
    assert doc == {"videos": [], "unmapped": [], "total": 0}


def test_解除视频关联(make_env):
    env = make_env()
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["videoPath"] == str(vid)

    resp = env.request("DELETE", f"/v1/photo/{pid}/video")
    assert resp.status == 200
    assert env.body_json(resp) == {"photoId": pid, "hasVideo": False}
    detail = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert detail["videoPath"] is None


def test_解除视频关联是幂等的(make_env):
    # 本来就没视频时回 404 只会让管理台弹一个没有意义的错 —— 调用方要的结果
    # （这张照片没有视频）已经成立。
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    assert env.request("DELETE", f"/v1/photo/{pid}/video").status == 200
    assert env.request("DELETE", f"/v1/photo/{pid}/video").status == 200


def test_解除关联不删别的照片的视频(make_env):
    # 同一段视频可能配给了多张照片。顺手删 asset 会让那些照片播放变 404。
    env = make_env()
    vid = env.write_video("videos/shared.mp4")
    p1 = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)
    p2 = env.ingest_ok(env.write_image("photos/b.jpg", seed=2), video=vid)
    env.request("DELETE", f"/v1/photo/{p1}/video")
    assert env.body_json(env.get(f"/v1/photo/{p1}"))["videoPath"] is None
    assert env.body_json(env.get(f"/v1/photo/{p2}"))["videoPath"] == str(vid)
    assert vid.exists(), "磁盘上的源文件必须还在"


def test_解除关联后又能重新配上(make_env):
    env = make_env()
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)
    env.request("DELETE", f"/v1/photo/{pid}/video")
    resp = env.post_json(f"/v1/photo/{pid}/video", {"videoPath": str(vid)})
    assert resp.status == 200
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["videoPath"] == str(vid)


def test_不存在的照片(make_env):
    env = make_env()
    assert env.request("DELETE", "/v1/photo/没有这个/video").status == 404
