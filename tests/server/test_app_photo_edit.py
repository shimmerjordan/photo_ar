"""PATCH /v1/photo/<id>：编辑照片的元数据（标题 / 打印宽度 / 贴合模式）。"""


def test_改标题_打印宽度_贴合模式(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), title="旧标题")
    resp = env.patch_json(f"/v1/photo/{pid}", {"title": "新标题", "printWidthMm": 102, "fitMode": "fit"})
    assert resp.status == 200, env.body_json(resp)
    doc = env.body_json(resp)
    assert doc["title"] == "新标题"
    assert abs(doc["printWidthM"] - 0.102) < 1e-9
    assert doc["fitMode"] == "fit"
    # 返回的就是详情的 admin 视图
    assert "refPath" in doc and "selfScore" in doc
    again = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert again["title"] == "新标题" and again["fitMode"] == "fit"


def test_贴合模式给_null_就回到跟随全局(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    assert env.patch_json(f"/v1/photo/{pid}", {"fitMode": "fit"}).status == 200
    resp = env.patch_json(f"/v1/photo/{pid}", {"fitMode": None})
    assert resp.status == 200
    # 全局默认是 fill（appconfig 的 video.fit_mode 默认值）
    assert env.body_json(resp)["fitMode"] == "fill"


def test_空标题存成空(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), title="有")
    assert env.body_json(env.patch_json(f"/v1/photo/{pid}", {"title": ""}))["title"] is None


def test_打印宽度_0_是未知(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    assert env.body_json(env.patch_json(f"/v1/photo/{pid}", {"printWidthMm": 0}))["printWidthM"] == 0.0


def test_坏输入都是_400(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    cases = [
        ({}, "empty_patch"),
        ({"titel": "x"}, "unknown_field"),
        ({"printWidthMm": -1}, "bad_print_width"),
        ({"printWidthMm": "宽"}, "bad_print_width"),
        ({"fitMode": "stretch"}, "bad_fit_mode"),
        ({"title": "长" * 201}, "bad_title"),
    ]
    for body, code in cases:
        resp = env.patch_json(f"/v1/photo/{pid}", body)
        assert resp.status == 400, (body, env.body_json(resp))
        assert env.body_json(resp)["error"] == code, body


def test_编辑要管理员_且先查授权(make_env):
    env = make_env()
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1))
    v = env.viewer()
    denied = env.patch_json(f"/v1/photo/{pid}", {"title": "x"}, as_=v)
    assert denied.status == 403 and env.body_json(denied)["error"] == "forbidden"
    v2 = env.viewer(name="有授权的", photo_ids=[pid])
    need_admin = env.patch_json(f"/v1/photo/{pid}", {"title": "x"}, as_=v2)
    assert need_admin.status == 403 and "管理员" in env.body_json(need_admin)["message"]


def test_不存在的照片_404(make_env):
    env = make_env()
    assert env.patch_json("/v1/photo/" + "0" * 32, {"title": "x"}).status == 404


def test_照片列表带视频资产与大小(make_env):
    env = make_env()
    vid = env.write_video("videos/a.mp4")
    with_v = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)
    without = env.ingest_ok(env.write_image("photos/b.jpg", seed=2))
    rows = {p["photoId"]: p for p in env.body_json(env.get("/v1/photos"))["photos"]}
    media = env.body_json(env.get(f"/v1/photo/{with_v}/media"))
    assert rows[with_v]["videoAssetId"] == media["assetId"]
    assert media["url"] == f"/v1/asset/{media['assetId']}/stream"
    assert rows[with_v]["videoBytes"] == media["bytes"]
    assert rows[without]["videoAssetId"] is None and rows[without]["videoBytes"] is None


def test_media_的_nasPath_只给管理员(make_env):
    env = make_env()
    vid = env.write_video("videos/a.mp4")
    pid = env.ingest_ok(env.write_image("photos/a.jpg", seed=1), video=vid)
    assert "nasPath" in env.body_json(env.get(f"/v1/photo/{pid}/media"))
    v = env.viewer(photo_ids=[pid])
    doc = env.body_json(env.get(f"/v1/photo/{pid}/media", as_=v))
    assert "nasPath" not in doc and doc["assetId"]
