"""`POST /v1/photo/<id>/ref`：换参考图。

这个接口存在的理由是「换掉那张图，但保住这张照片的身份」。所以下面最要紧的一组用例钉的
是**什么东西没变**：photo_id、授权、配的视频、标题、打印宽度。任何一项在换图之后丢了，
这个接口就不如「删掉重建」了 —— 而它本来就是为了避开那条路才存在的。
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------- 鉴权


def test_要管理员(make_env):
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    pid = env.ingest_ok(ref)
    other = env.write_image("photos/b.jpg", seed=2)

    # 没授权的 viewer：先按「这张跟你无关」挡住
    v = env.viewer()
    denied = env.post_json(f"/v1/photo/{pid}/ref", {"refPath": str(other)}, as_=v)
    assert denied.status == 403
    assert env.body_json(denied)["error"] == "forbidden"

    # 有授权的 viewer：这时挡住他的是「要管理员」
    v2 = env.viewer(name="有授权的", photo_ids=[pid])
    need_admin = env.post_json(f"/v1/photo/{pid}/ref", {"refPath": str(other)}, as_=v2)
    assert need_admin.status == 403
    assert "管理员" in env.body_json(need_admin)["message"]


def test_照片不存在(make_env):
    env = make_env()
    img = env.write_image("photos/a.jpg", seed=1)
    resp = env.post_json("/v1/photo/" + "f" * 32 + "/ref", {"refPath": str(img)})
    assert resp.status == 404


def test_缺参数(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    resp = env.post_json(f"/v1/photo/{pid}/ref", {})
    assert resp.status == 400
    assert env.body_json(resp)["error"] == "missing_ref_path"


def test_白名单外的路径(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    resp = env.post_json(
        f"/v1/photo/{pid}/ref", {"refPath": str(env.outside / "secret.jpg")}
    )
    assert resp.status == 403


# ---------------------------------------------------------------- 换成功


def test_换完之后基本信息更新了(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), title="原来的标题")
    new_ref = env.write_image("photos/better.jpg", seed=42)

    resp = env.post_json(f"/v1/photo/{pid}/ref", {"refPath": str(new_ref)})
    assert resp.status == 200, env.body_json(resp)
    doc = env.body_json(resp)
    assert doc["photoId"] == pid
    assert doc["selfScore"] > 0

    detail = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert detail["refPath"] == str(new_ref), "参考图路径要指向新文件"


def test_photo_id_不变_库的条数也不变(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    env.ingest_ok(env.write_image("photos/b.jpg", seed=2))
    before = env.body_json(env.get("/v1/photos"))["total"]

    env.post_json(
        f"/v1/photo/{pid}/ref",
        {"refPath": str(env.write_image("photos/new.jpg", seed=42))},
    )

    after = env.body_json(env.get("/v1/photos"))
    assert after["total"] == before, "换图不该多出或少掉照片"
    assert pid in [p["photoId"] for p in after["photos"]]


def test_授权保住了(make_env):
    # 这一条是这个接口存在的首要理由：删掉重建会让 photo_grant 级联删除，
    # 重建之后要一张张重新勾。
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    v = env.viewer(name="张三", photo_ids=[pid])
    users = env.body_json(env.get("/v1/admin/users"))
    uid = next(u["id"] for u in users if u["name"] == "张三")
    assert env.body_json(env.get(f"/v1/admin/users/{uid}/grants"))["photoIds"] == [pid]

    env.post_json(
        f"/v1/photo/{pid}/ref",
        {"refPath": str(env.write_image("photos/new.jpg", seed=42))},
    )

    assert env.body_json(env.get(f"/v1/admin/users/{uid}/grants"))["photoIds"] == [pid]
    # 而且那个 viewer 现在仍然看得到它
    mine = env.body_json(env.get("/v1/photos", as_=v))["photos"]
    assert [p["photoId"] for p in mine] == [pid]


def test_配的视频保住了(make_env):
    env = make_env()
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)

    env.post_json(
        f"/v1/photo/{pid}/ref",
        {"refPath": str(env.write_image("photos/new.jpg", seed=42))},
    )

    detail = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert detail["videoPath"] == str(vid), "换的是图，视频不该动"


def test_标题与打印宽度保住了(make_env):
    env = make_env()
    pid = env.ingest_ok(
        env.write_image("photos/a.jpg", seed=1), title="婚礼合照", width_mm=152.0
    )

    env.post_json(
        f"/v1/photo/{pid}/ref",
        {"refPath": str(env.write_image("photos/new.jpg", seed=42))},
    )

    detail = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert detail["title"] == "婚礼合照"
    assert abs(detail["printWidthM"] - 0.152) < 1e-6


def test_缩略图换成新图了(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    before = env.body_bytes(env.get(f"/v1/photo/{pid}/thumb"))

    env.post_json(
        f"/v1/photo/{pid}/ref",
        {"refPath": str(env.write_image("photos/new.jpg", seed=42))},
    )

    after = env.body_bytes(env.get(f"/v1/photo/{pid}/thumb"))
    assert after != before, "缩略图要跟着换"


def test_换完之后新图能识别出来(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    env.ingest_ok(env.write_image("photos/b.jpg", seed=2))
    new_img = env.textured(seed=42, w=1200, h=800)
    new_ref = env.nas / "photos" / "new.jpg"
    import cv2

    assert cv2.imwrite(str(new_ref), new_img)

    resp = env.post_json(f"/v1/photo/{pid}/ref", {"refPath": str(new_ref)})
    assert resp.status == 200, env.body_json(resp)

    doc = env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(new_img)))
    assert doc.get("photoId") == pid, doc


# ---------------------------------------------------------------- 闸门


def test_去重要排除自己_否则重新扫一遍换上去必然失败(make_env):
    # 最主要的用法就是「同一张照片重新扫一遍，换上更清楚的那份」。不排除自己的话
    # 新图必然与自己的旧特征判成近重复，这个接口对主要场景恒定失败。
    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    pid = env.ingest_ok(ref)
    # 用**同一张图**再换一次 —— 这是自己与自己比
    resp = env.post_json(f"/v1/photo/{pid}/ref", {"refPath": str(ref)})
    assert resp.status == 200, env.body_json(resp)


def test_和别人近重复时拒绝_并说明原图没被换掉(make_env):
    env = make_env()
    a = env.write_image("photos/a.jpg", seed=1)
    b = env.write_image("photos/b.jpg", seed=2)
    pid_a = env.ingest_ok(a)
    env.ingest_ok(b)
    before = env.body_json(env.get(f"/v1/photo/{pid_a}"))["refPath"]

    # 拿 b 的**同一个文件**去换 a 的参考图 → 与库里的 b 近重复
    resp = env.post_json(f"/v1/photo/{pid_a}/ref", {"refPath": str(b)})
    assert resp.status == 409
    doc = env.body_json(resp)
    assert doc["error"] == "near_duplicate"
    # 必须说清「原来那张没有被换掉」，否则人不知道现在库里是什么状态
    assert "没有被换掉" in doc["message"]
    # 而且真的没换
    assert env.body_json(env.get(f"/v1/photo/{pid_a}"))["refPath"] == before


def test_不是图片(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    resp = env.post_json(
        f"/v1/photo/{pid}/ref", {"refPath": str(env.write_video("videos/a.mp4"))}
    )
    assert resp.status == 415
    assert env.body_json(resp)["error"] == "ref_not_image"


def test_文件不存在(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    resp = env.post_json(
        f"/v1/photo/{pid}/ref", {"refPath": str(env.nas / "photos" / "没有.jpg")}
    )
    assert resp.status == 404
    assert env.body_json(resp)["error"] == "ref_not_found"


def test_ref_stale_被清零(make_env):
    # ref_stale 的含义是「磁盘上的参考图变了但特征还是旧的」，而换参考图正是把特征
    # 重算了一遍。不清零的话管理台会一直显示「参考图已变」，而已经不是了。
    from photoar.server import integrity

    env = make_env()
    ref = env.write_image("photos/a.jpg", seed=1)
    pid = env.ingest_ok(ref)
    # 改动磁盘上那个参考图文件，然后跑全量校验 —— 置位 ref_stale 的是
    # `integrity.verify_asset`（内容哈希变了 → STATUS_CONTENT_CHANGED），
    # 不是 `Server.check_consistency`（那个查的是 catalog 与识别库对不对得上）。
    env.write_image("photos/a.jpg", seed=99)
    integrity.verify_all(env.srv.catalog)
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["refStale"] is True

    env.post_json(
        f"/v1/photo/{pid}/ref",
        {"refPath": str(env.write_image("photos/new.jpg", seed=42))},
    )
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["refStale"] is False
