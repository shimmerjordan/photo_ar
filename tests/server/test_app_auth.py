"""HTTP 层的身份、授权、热配置接线与管理接口。

这套测试盯的是"错一次就等于根本没有这层"的那几条：

1. **两条凭证路都要认**。Bearer 是 App 的路，cookie 是网页的唯一可行路
   （`<img>`/`<video>` 带不了头）。少认一条的表现是"手机上好的，网页上一片 401"。
2. **授权不能被绕过**。照片授权靠 photo_id 判，而 `/v1/asset/<id>/stream` 吃的是
   asset id —— 不反查归属的话，拿到一个 asset id 就能取到视频本身。
3. **recognize 的授权必须在判定之后**。先过滤候选会让 ratio 检验少了参与者，从而
   **提高误识别率、且只对权限受限的用户提高**（管理员怎么测都测不出来）。这里同时
   钉住"未授权时返回 matched:false / forbidden"和"HTTP 仍是 200"。
4. **热配置不是假开关**。改 `recog.min_inliers` 之后判定行为必须真的变 —— 这一条
   证明 appconfig 那句 `needs_restart=False` 的承诺被接上了，而不是写进库就完事。
5. **管理员不能把自己锁在门外**。降级/停用/删除自己都必须拒绝，否则一次误操作只能
   进容器改库。
6. **静态页面路由不能穿越**。它是全服务第二个碰文件系统的地方。
"""

import pytest

from photoar import synth
from photoar.server import app

from .conftest import ADMIN_NAME, ADMIN_PASSWORD


# ---- 登录 ----


def test_viewer_logs_in_with_name_only(env):
    """viewer 只输名字就能进：家里人隔几周才用一次，每次输口令等于"这东西太麻烦"。"""
    creds = env.viewer("小明")
    assert creds.role == "viewer" and creds.name == "小明" and creds.token
    assert env.body_json(env.get("/v1/auth/me", as_=creds))["isAdmin"] is False


def test_admin_login_without_password_is_401(env):
    """"viewer 不用口令"这条便利绝不能顺着对称性漏到 admin 身上。"""
    r = env.post_json("/v1/auth/login", {"name": ADMIN_NAME}, auth=False)
    assert r.status == 401
    assert env.body_json(r)["error"] == "bad_credentials"


def test_admin_login_with_wrong_password_is_401(env):
    r = env.post_json(
        "/v1/auth/login", {"name": ADMIN_NAME, "password": "nope"}, auth=False
    )
    assert r.status == 401 and env.body_json(r)["error"] == "bad_credentials"


def test_unknown_user_is_403_not_401(env):
    """403 的含义是"别重试"：名字不在册，重输一万次结果一样（账号只能管理员建）。
    401 会让客户端把登录框再弹一次。"""
    r = env.post_json("/v1/auth/login", {"name": "查无此人"}, auth=False)
    assert r.status == 403 and env.body_json(r)["error"] == "unknown_user"


def test_disabled_account_login_is_403(env):
    creds = env.viewer("停用的人")
    r = env.patch_json(f"/v1/admin/users/{creds.user_id}", {"disabled": True})
    assert r.status == 200 and env.body_json(r)["disabled"] is True
    r = env.post_json("/v1/auth/login", {"name": "停用的人"}, auth=False)
    assert r.status == 403 and env.body_json(r)["error"] == "account_disabled"


def test_disabling_a_user_kills_the_session_he_already_has(env):
    """管理台上显示"已停用"，而他手机上那个还没过期的 token 还能看照片 —— 这是
    "停用"这个动作最容易漏的一半。"""
    creds = env.viewer("先登录再被停用")
    assert env.get("/v1/auth/me", as_=creds).status == 200
    env.patch_json(f"/v1/admin/users/{creds.user_id}", {"disabled": True})
    assert env.get("/v1/auth/me", as_=creds).status == 401


def test_login_is_the_only_public_v1_path(env):
    """免鉴权白名单只有登录一条。logout 与 me 都要凭证。"""
    assert app.PUBLIC_PATHS == frozenset({"/v1/auth/login"})
    assert env.request("POST", "/v1/auth/logout", auth=False).status == 401
    assert env.get("/v1/auth/me", auth=False).status == 401


def test_login_rejects_empty_name_as_400_not_401(env):
    """少填字段是输入错误，不是"凭证不对" —— 401 会让管理台去弹重新登录。"""
    r = env.post_json("/v1/auth/login", {"name": "   "}, auth=False)
    assert r.status == 400 and env.body_json(r)["error"] == "missing_name"


# ---- 两条凭证路 ----


def test_bearer_and_cookie_both_authenticate(env):
    """网页里的 `<img>`/`<video>` 没法带 Authorization 头，所以 cookie 那条路是
    管理台能显示缩略图和视频的唯一前提。"""
    creds = env.admin()
    assert env.get("/v1/auth/me", as_=creds).status == 200
    assert env.get("/v1/auth/me", as_=creds, cookie=True).status == 200
    # 真正要紧的是取文件那两个接口也认 cookie，而不只是 /auth/me
    pid = env.ingest_ok(env.write_image("photos/cookie.jpg", seed=101))
    assert env.get(f"/v1/photo/{pid}/thumb", as_=creds, cookie=True).status == 200


def test_login_sets_a_session_cookie(env):
    resp = env.post_json(
        "/v1/auth/login", {"name": ADMIN_NAME, "password": ADMIN_PASSWORD}, auth=False
    )
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith(f"{app.SESSION_COOKIE}=")
    assert "HttpOnly" in cookie  # 页面脚本读不到 → XSS 偷不走会话
    assert "SameSite=Lax" in cookie  # 第三方发起的请求不带它
    assert "Path=/" in cookie  # 要同时覆盖 /v1/* 与 /admin
    assert "Max-Age=" in cookie
    assert resp.headers["Cache-Control"] == "no-store", "响应体里有明文 token"


def test_secure_flag_is_off_by_default(env):
    """写死 Secure 会让局域网 http 直连登录后一刷新就掉线，而响应里明明有
    Set-Cookie —— 几乎不可能往 cookie 属性上想。所以默认关。"""
    assert "Secure" not in env.admin_cookie()


def test_secure_flag_is_opt_in(make_env):
    """只走 https 的部署（Cloudflare 隧道）该把它打开。"""
    assert "Secure" in make_env(cookie_secure=True).admin_cookie()


def test_bearer_wins_over_a_stale_cookie(env):
    """反过来的顺序下，一个属于别人的 cookie 会静默顶掉调用方明确表达的身份。"""
    admin = env.admin()
    viewer = env.viewer("被顶掉的人")
    r = env.get(
        "/v1/auth/me",
        as_=admin,
        headers={"cookie": f"{app.SESSION_COOKIE}={viewer.token}"},
    )
    assert env.body_json(r)["name"] == admin.name


def test_empty_bearer_header_falls_through_to_cookie(env):
    """`Authorization: Bearer`（空值）会被某些代理在没有凭证时加上。把它当成
    "调用方选了 Bearer 这条路"会让同一个浏览器上的 cookie 白存。"""
    creds = env.admin()
    r = env.get(
        "/v1/auth/me",
        as_=creds,
        cookie=True,
        headers={"authorization": "Bearer"},
    )
    assert r.status == 200


def test_logout_invalidates_both_paths(env):
    creds = env.admin()
    r = env.request("POST", "/v1/auth/logout", as_=creds)
    assert r.status == 204
    # 顺手清 cookie，否则浏览器会继续带一个已作废的 token，管理台每个 fetch 都 401
    assert "Max-Age=0" in r.headers["Set-Cookie"]
    assert env.get("/v1/auth/me", as_=creds).status == 401
    assert env.get("/v1/auth/me", as_=creds, cookie=True).status == 401


def test_legacy_token_still_works_and_is_admin(env):
    """`tools/batch_ingest.py` 与 docker 健康检查靠它，那些调用方没有人坐在前面
    输口令。它换来的 Principal 没有 user_id。"""
    body = env.body_json(env.get("/v1/auth/me"))
    assert body["isAdmin"] is True and body["role"] == "admin"
    assert body["userId"] is None
    # 而且它登不出（没有服务端状态可删），登出一次也不能把它变成 401
    assert env.request("POST", "/v1/auth/logout").status == 204
    assert env.get("/v1/auth/me").status == 200


def test_logout_of_an_unknown_token_is_still_204(env):
    """幂等。让健康检查脚本误调一次 logout 就 500，比静默无事发生糟得多。"""
    assert env.request("POST", "/v1/auth/logout").status == 204


# ---- 按用户授权 ----


@pytest.fixture
def two_photos(env):
    """两张照片 + 各自的视频。seed 不同，不会撞近重复闸门。"""
    a = env.ingest_ok(
        env.write_image("photos/acl-a.jpg", seed=201),
        video=env.write_video("videos/acl-a.mp4"),
    )
    b = env.ingest_ok(
        env.write_image("photos/acl-b.jpg", seed=202),
        video=env.write_video("videos/acl-b.mp4"),
    )
    return a, b


def test_photos_list_is_filtered_per_user(env, two_photos):
    a, b = two_photos
    viewer = env.viewer("只看一张的人", photo_ids=[a])
    body = env.body_json(env.get("/v1/photos", as_=viewer))
    assert [p["photoId"] for p in body["photos"]] == [a] and body["total"] == 1
    # admin 不过滤
    assert env.body_json(env.get("/v1/photos"))["total"] == 2


def test_grant_all_sees_everything_without_being_admin(env, two_photos):
    viewer = env.viewer("看全部的访客", grant_all=True)
    body = env.body_json(env.get("/v1/photos", as_=viewer))
    assert {p["photoId"] for p in body["photos"]} == set(two_photos)
    # 但他仍然不是 admin
    assert env.get("/v1/fs/list", as_=viewer).status == 403


def test_viewer_cannot_reach_an_ungranted_photo(env, two_photos):
    a, b = two_photos
    viewer = env.viewer("只看 a 的人", photo_ids=[a])
    for suffix in ("", "/imgdb", "/thumb", "/media"):
        r = env.get(f"/v1/photo/{b}{suffix}", as_=viewer)
        assert r.status == 403, suffix
        assert env.body_json(r)["error"] == "forbidden", suffix
    # 授权过的那张一切正常（否则上面的 403 可能只是"全都不行"）
    for suffix in ("", "/imgdb", "/thumb", "/media"):
        assert env.get(f"/v1/photo/{a}{suffix}", as_=viewer).status == 200, suffix


def test_asset_stream_checks_the_owning_photo(env, two_photos):
    """⚠️ 这个接口吃 asset id，不反查归属的话，拿到一个 asset id 就绕过了整套照片
    授权 —— 而 asset id 会从 media 响应里发出去。取到的还是最要紧的东西：视频。"""
    a, b = two_photos
    url_a = env.body_json(env.get(f"/v1/photo/{a}/media"))["url"]
    url_b = env.body_json(env.get(f"/v1/photo/{b}/media"))["url"]
    viewer = env.viewer("只看 a 的人", photo_ids=[a])
    assert env.get(url_a, as_=viewer).status == 200
    r = env.get(url_b, as_=viewer)
    assert r.status == 403 and env.body_json(r)["error"] == "forbidden"


def test_orphan_asset_stream_is_admin_only(env):
    """没挂在任何 photo 上的 asset（入库半路失败、或换过视频后被解绑的旧记录）
    查不到归属，按 `_may_see` 的逻辑会变成"谁都不能取"，所以必须显式放行 admin。"""
    orphan = env.write_video("videos/orphan.mp4")
    asset_id = env.srv.catalog.upsert_asset(
        nas_path=str(orphan),
        kind="video",
        sha256="0" * 64,
        bytes_=orphan.stat().st_size,
        mtime=1,
    )
    assert env.get(f"/v1/asset/{asset_id}/stream").status == 200
    viewer = env.viewer("拿不到孤儿的人", grant_all=True)
    r = env.get(f"/v1/asset/{asset_id}/stream", as_=viewer)
    assert r.status == 403 and env.body_json(r)["error"] == "forbidden"


def test_write_endpoints_are_admin_only(env, two_photos):
    a, _ = two_photos
    viewer = env.viewer("什么都不许写的人", photo_ids=[a])
    cases = [
        ("GET", "/v1/fs/list"),
        ("GET", f"/v1/fs/thumb?path={env.nas / 'photos' / 'acl-a.jpg'}"),
        ("GET", "/v1/history"),
        ("GET", "/v1/admin/users"),
        ("GET", "/v1/admin/config"),
    ]
    for method, path in cases:
        r = env.request(method, path, as_=viewer)
        assert r.status == 403, path
        assert env.body_json(r)["error"] == "admin_only", path

    r = env.post_json(
        "/v1/photo", {"refPath": str(env.nas / "photos" / "x.jpg"), "printWidthMm": 152},
        as_=viewer,
    )
    assert r.status == 403 and env.body_json(r)["error"] == "admin_only"
    r = env.request("POST", "/v1/upload?name=x.mp4", body=b"x", as_=viewer)
    assert r.status == 403 and env.body_json(r)["error"] == "admin_only"


def test_attach_video_is_admin_only_even_for_a_granted_photo(env, two_photos):
    """它把一个 `videoPath` 送进 roots.resolve，也就是说调用方能靠错误码的区别
    去探 NAS 上有哪些文件 —— 正是 /v1/fs/* 定成 admin only 想避免的事。"""
    a, _ = two_photos
    viewer = env.viewer("有 a 但不许改的人", photo_ids=[a])
    video = env.write_video("videos/attach-later.mp4")
    r = env.post_json(f"/v1/photo/{a}/video", {"videoPath": str(video)}, as_=viewer)
    assert r.status == 403 and env.body_json(r)["error"] == "admin_only"


def test_ping_needs_auth_but_no_authorization(env):
    """客户端切网络时四个 endpoint 一起探，viewer 也得能探。"""
    viewer = env.viewer("只探活的人")
    assert env.get("/v1/ping", as_=viewer).status == 200
    assert env.get("/v1/ping", auth=False).status == 401


# ---- recognize 的授权顺序 ----


def test_recognize_of_an_ungranted_photo_is_forbidden_not_a_hit(env):
    """未授权时返回 `matched:false / reason:forbidden`，HTTP 仍 200
    （"没认出来"在这个 API 里是正常状态，客户端每 400ms 一次）。"""
    img = env.textured(seed=211, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "recog-acl.jpg"
    assert cv2.imwrite(str(ref), img)
    pid = env.ingest_ok(ref)
    query, _ = synth.generate(img, count=1, seed=3)[0]
    jpeg = env.jpeg_of(query)

    # 对照：admin 扫同一帧是命中的，所以下面的 forbidden 不是"根本没认出来"
    hit = env.body_json(env.post_frame("/v1/recognize", jpeg))
    assert hit["matched"] is True and hit["photoId"] == pid

    viewer = env.viewer("没这张的人")
    body = env.body_json(env.post_frame("/v1/recognize", jpeg, as_=viewer))
    assert body["matched"] is False
    assert body["reason"] == "forbidden", "要能和真正的未命中区分开"
    assert "photoId" not in body and "mediaUrl" not in body

    granted = env.viewer("有这张的人", photo_ids=[pid])
    ok = env.body_json(env.post_frame("/v1/recognize", jpeg, as_=granted))
    assert ok["matched"] is True and ok["photoId"] == pid


def test_forbidden_recognize_is_still_logged_as_a_hit(env):
    """history 是排查"家里人说扫不出来"的唯一线索。记成未命中会让"其实认出来了、
    只是没授权给他"这个最可能的原因彻底看不见。"""
    img = env.textured(seed=212, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "recog-log.jpg"
    assert cv2.imwrite(str(ref), img)
    pid = env.ingest_ok(ref)
    query, _ = synth.generate(img, count=1, seed=4)[0]
    viewer = env.viewer("没授权的扫描者")
    env.post_frame("/v1/recognize", env.jpeg_of(query), as_=viewer)

    entries = env.body_json(env.get("/v1/history"))["entries"]
    assert entries[0]["photoId"] == pid


# ---- 热配置真的接上了 ----


def test_changing_min_inliers_changes_the_decision(env):
    """**这一条证明热配置不是假开关。**

    `recog.min_inliers` 的 `needs_restart` 是 False，也就是"读取方每次用的时候来
    AppConfig 取当前值"。识别路径如果继续用 verify.py 的模块常量，那么管理台上改
    完会显示成功、库里也确实写了，识别行为一点变化都没有 —— 而那是一个既不报错、
    也不可能从响应里看出来的状态。
    """
    img = env.textured(seed=221, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "hot.jpg"
    assert cv2.imwrite(str(ref), img)
    env.ingest_ok(ref)
    query, _ = synth.generate(img, count=1, seed=5)[0]
    jpeg = env.jpeg_of(query)

    hit = env.body_json(env.post_frame("/v1/recognize", jpeg))
    assert hit["matched"] is True
    inliers = hit["inliers"]

    # 把门槛抬到这次实测内点数之上（留 10 的余量，RANSAC 不是逐位确定的）
    r = env.patch_json("/v1/admin/config", {"recog.min_inliers": min(500, inliers + 10)})
    assert r.status == 200
    assert env.body_json(r)["needsRestart"] == [], "阈值改动不该要求重启"

    miss = env.body_json(env.post_frame("/v1/recognize", jpeg))
    assert miss["matched"] is False
    assert miss["reason"] == "weak", "要是走的是 decide_with，判否的理由就是内点不够"

    # 再调回来必须能重新命中 —— 否则上面的失败可能是别的原因（比如库坏了）
    assert env.patch_json("/v1/admin/config", {"recog.min_inliers": 40}).status == 200
    assert env.body_json(env.post_frame("/v1/recognize", jpeg))["matched"] is True


def test_ratio_of_1_disables_the_ambiguity_check(env):
    """`recog.ratio` 同样要真的生效。填 1.0 等于关掉这条判定（第一名恒不小于
    第二名），而它正是挡住近重复照片互相顶掉的那道判据。"""
    body = env.body_json(env.get("/v1/admin/config"))
    fields = {f["key"]: f for f in body["fields"]}
    assert fields["recog.ratio"]["value"] == 1.5
    r = env.patch_json("/v1/admin/config", {"recog.ratio": 1.0})
    assert r.status == 200
    assert env.body_json(env.get("/v1/admin/config"))["values"]["recog.ratio"] == 1.0


def test_top_k_is_passed_through(env):
    """粗排候选数也来自热配置。1 是合法下界（现场排查用），不该被拒。"""
    assert env.patch_json("/v1/admin/config", {"recog.top_k": 1}).status == 200
    img = env.textured(seed=222, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "topk.jpg"
    assert cv2.imwrite(str(ref), img)
    env.ingest_ok(ref)
    query, _ = synth.generate(img, count=1, seed=6)[0]
    assert env.post_frame("/v1/recognize", env.jpeg_of(query)).status == 200


def test_quality_gate_can_be_turned_off_but_imgdb_is_still_built(make_env):
    """闸门只关**判定**。仍然要产出 .imgdb，否则 AR 端拿不到识别目标 —— 一个
    "入库成功"却永远播不了的条目。"""
    env = make_env(quality_score=40)
    ref = env.write_image("photos/lowq.jpg", seed=231)
    assert env.ingest(ref).status == 422

    assert env.patch_json("/v1/admin/config", {"ingest.quality_gate": False}).status == 200
    pid = env.ingest_ok(ref)
    r = env.get(f"/v1/photo/{pid}/imgdb")
    assert r.status == 200 and len(env.body_bytes(r)) == 4300
    # 分数仍然如实记下来：闸门关着时更需要看得到它
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["qualityScore"] == 40


def test_min_quality_score_is_read_from_config(make_env):
    env = make_env(quality_score=60)
    ref = env.write_image("photos/mid.jpg", seed=232)
    r = env.ingest(ref)
    assert r.status == 422 and env.body_json(r)["minScore"] == 75

    assert env.patch_json(
        "/v1/admin/config", {"ingest.min_quality_score": 50}
    ).status == 200
    assert env.ingest(ref).status == 201


def test_dedup_gate_can_be_turned_off(env):
    """⚠️ 关掉之后两张近重复都能入库，然后互相判 ambiguous —— **两张都永久扫不
    出来**（Phase 0 的第一条硬结论）。这条测试只钉住开关本身生效。"""
    import cv2

    img = env.textured(seed=233, w=1200, h=800)
    a = env.nas / "photos" / "dedup-a.jpg"
    b = env.nas / "photos" / "dedup-b.jpg"
    assert cv2.imwrite(str(a), img)
    assert cv2.imwrite(str(b), img, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
    env.ingest_ok(a)
    assert env.ingest(b).status == 409

    assert env.patch_json("/v1/admin/config", {"ingest.dedup_gate": False}).status == 200
    assert env.ingest(b).status == 201
    # 后果如实发生：两张都在库里，而识别判 ambiguous
    assert env.body_json(env.get("/v1/photos"))["total"] == 2
    query, _ = synth.generate(img, count=1, seed=7)[0]
    body = env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(query)))
    assert body["matched"] is False and body["reason"] == "ambiguous"


def test_self_score_is_still_computed_when_dedup_is_off(env):
    """自匹配分是**别人**入库时的分母。跟着闸门一起跳过的话它会是 0，而 0 恒小于
    任何值 —— 于是"关掉去重"会变成"以后入库全被拦住"。"""
    assert env.patch_json("/v1/admin/config", {"ingest.dedup_gate": False}).status == 200
    pid = env.ingest_ok(env.write_image("photos/ss.jpg", seed=234))
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["selfScore"] > 0


def test_config_rejects_unknown_key_and_bad_value(env):
    r = env.patch_json("/v1/admin/config", {"recog.minInliers": 40})
    assert r.status == 400 and env.body_json(r)["error"] == "bad_config"
    r = env.patch_json("/v1/admin/config", {"recog.min_inliers": "abc"})
    assert r.status == 400
    r = env.patch_json("/v1/admin/config", {"recog.min_inliers": 99999})
    assert r.status == 400
    # 整批拒绝：合法的那个也不能落地
    assert env.patch_json(
        "/v1/admin/config", {"recog.top_k": 7, "recog.ratio": "nope"}
    ).status == 400
    assert env.body_json(env.get("/v1/admin/config"))["values"]["recog.top_k"] == 20


def test_config_reports_which_keys_need_a_restart(env):
    r = env.patch_json("/v1/admin/config", {"session.admin_hours": 24, "recog.top_k": 30})
    assert r.status == 200
    assert env.body_json(r)["needsRestart"] == ["session.admin_hours"]


def test_config_describe_has_everything_the_form_needs(env):
    body = env.body_json(env.get("/v1/admin/config"))
    keys = {f["key"] for f in body["fields"]}
    assert "recog.min_inliers" in keys and "video.fit_mode" in keys
    assert set(body["values"]) == keys
    field = next(f for f in body["fields"] if f["key"] == "recog.min_inliers")
    for attr in ("kind", "value", "default", "label", "help", "needsRestart", "min", "max"):
        assert attr in field, attr
    assert field["default"] == 40, "默认值必须等于 verify.MIN_INLIERS 那个标定值"


# ---- fitMode ----


def test_fit_mode_defaults_to_fill_and_is_reported(env):
    pid = env.ingest_ok(env.write_image("photos/fit1.jpg", seed=241))
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["fitMode"] == "fill"


def test_fit_mode_is_frozen_at_ingest_time(env):
    """改全局默认只影响之后入库的照片。照片现在只是"触发条件 + 画布"，用户为某张
    调过的贴合方式不该被一次改全局默认悄悄改回去。"""
    old = env.ingest_ok(env.write_image("photos/fit2.jpg", seed=242))
    assert env.patch_json("/v1/admin/config", {"video.fit_mode": "fit"}).status == 200
    new = env.ingest_ok(env.write_image("photos/fit3.jpg", seed=243))
    assert env.body_json(env.get(f"/v1/photo/{old}"))["fitMode"] == "fill"
    assert env.body_json(env.get(f"/v1/photo/{new}"))["fitMode"] == "fit"


def test_recognize_hit_reports_fit_mode(env):
    """AR 端要靠它决定贴图方式，而它只在命中响应里拿得到（那时还没调详情接口）。"""
    img = env.textured(seed=244, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "fit4.jpg"
    assert cv2.imwrite(str(ref), img)
    env.ingest_ok(ref)
    query, _ = synth.generate(img, count=1, seed=8)[0]
    hit = env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(query)))
    assert hit["matched"] is True and hit["fitMode"] == "fill"


def test_null_fit_mode_falls_back_to_the_global_default(env):
    """v1 时期入库的照片这一列是 NULL（"跟随全局"），兜底必须留着。"""
    pid = env.ingest_ok(env.write_image("photos/fit5.jpg", seed=245))
    env.srv.catalog.set_photo_fit_mode(pid, None)
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["fitMode"] == "fill"
    env.patch_json("/v1/admin/config", {"video.fit_mode": "fit"})
    assert env.body_json(env.get(f"/v1/photo/{pid}"))["fitMode"] == "fit"


# ---- 用户管理 ----


def test_bootstrap_admin_is_created_and_can_log_in(env):
    users = env.body_json(env.get("/v1/admin/users"))
    assert [u["name"] for u in users] == [ADMIN_NAME]
    assert users[0]["role"] == "admin" and users[0]["disabled"] is False
    assert env.admin().role == "admin"


def test_bootstrap_generates_a_usable_random_password_and_prints_it_once(
    make_env, capsys
):
    """没配 `PHOTOAR_ADMIN_PASSWORD` 时的行为，三条都要成立：

    - **仍然建出管理员**。不建的话部署完没人能进管理台（那是建号/发授权/改配置的
      唯一入口），只能进容器手工 INSERT 一行。
    - 口令是**随机**的，不是固定默认值 —— "admin/admin 然后提醒用户改"在一个挂在
      隧道后面的服务上等于没有口令，而那个默认值就印在源码里。
    - 打印出来的那个口令**真的能登录**。只打印不能用的话，部署者会以为是自己抄错了。
    """
    env = make_env(admin_password="")
    printed = capsys.readouterr().out
    assert "随机口令" in printed
    password = printed.split("随机口令：")[1].split("\n")[0].strip()
    assert password and password != ADMIN_PASSWORD
    assert env.login(ADMIN_NAME, password).role == "admin"


def test_bootstrap_does_not_reprint_on_a_second_start(make_env, capsys):
    """每次重启都刷一行"这是你的口令"的话，那个口令早就被改掉了，而日志还在言之凿凿。"""
    env = make_env()
    capsys.readouterr()
    # 同一个 data_dir 上再起一次（库里已经有 admin 了）
    app.Server.create(env.cfg)
    assert "引导管理员" not in capsys.readouterr().out


def test_creating_an_admin_requires_a_password(env):
    r = env.post_json("/v1/admin/users", {"name": "无口令管理员", "role": "admin"})
    assert r.status == 400 and env.body_json(r)["error"] == "password_required"


def test_creating_a_viewer_with_a_password_is_400_not_ignored(env):
    """静默忽略的话管理员会以为自己设上了口令，而实际上任何知道这个名字的人都能进
    —— 一个自认为做了防护的空防护。"""
    r = env.post_json(
        "/v1/admin/users", {"name": "带口令的访客", "role": "viewer", "password": "x"}
    )
    assert r.status == 400 and env.body_json(r)["error"] == "password_not_allowed"


def test_creating_a_user_validates_role_and_name(env):
    assert env.post_json("/v1/admin/users", {"name": "谁", "role": "root"}).status == 400
    assert env.post_json("/v1/admin/users", {"name": "  ", "role": "viewer"}).status == 400
    assert env.post_json("/v1/admin/users", {"role": "viewer"}).status == 400


def test_duplicate_name_is_409(env):
    env.viewer("重名的人")
    r = env.post_json("/v1/admin/users", {"name": " 重名的人 ", "role": "viewer"})
    assert r.status == 409 and env.body_json(r)["error"] == "name_taken"


def test_admin_cannot_demote_itself(env):
    """降完就没人能把他升回来了（升级需要 admin 身份），`ensure_bootstrap_admin`
    也救不了 —— 它的判据是"存在任何 admin 行"，包括被停用的。只能进容器改库。"""
    me = env.admin()
    r = env.patch_json(f"/v1/admin/users/{me.user_id}", {"role": "viewer"}, as_=me)
    assert r.status == 400 and env.body_json(r)["error"] == "cannot_demote_self"
    assert env.body_json(env.get("/v1/auth/me", as_=me))["role"] == "admin"


def test_admin_cannot_disable_itself(env):
    me = env.admin()
    r = env.patch_json(f"/v1/admin/users/{me.user_id}", {"disabled": True}, as_=me)
    assert r.status == 400 and env.body_json(r)["error"] == "cannot_disable_self"
    assert env.get("/v1/auth/me", as_=me).status == 200


def test_admin_cannot_delete_itself(env):
    me = env.admin()
    r = env.request("DELETE", f"/v1/admin/users/{me.user_id}", as_=me)
    assert r.status == 400 and env.body_json(r)["error"] == "cannot_delete_self"
    assert env.get("/v1/auth/me", as_=me).status == 200


def test_self_protection_still_allows_renaming_yourself(env):
    """自保护只挡降级/停用/删除。按"不能让 admin 数量降到 0"写判据的话，改自己的
    名字这种无关操作也会被拖进去。"""
    me = env.admin()
    r = env.patch_json(f"/v1/admin/users/{me.user_id}", {"name": "新名字"}, as_=me)
    assert r.status == 200 and env.body_json(r)["name"] == "新名字"


def test_one_admin_can_demote_another(env):
    """挡的是"自己"，不是"任何 admin"：两个管理员互相降级是可恢复的。"""
    me = env.admin()
    r = env.post_json(
        "/v1/admin/users", {"name": "二号管理员", "role": "admin", "password": "pw-2"}
    )
    assert r.status == 201
    other = env.body_json(r)["id"]
    assert env.patch_json(
        f"/v1/admin/users/{other}", {"role": "viewer"}, as_=me
    ).status == 200
    # 降级顺手清掉口令：留着的话这个 viewer 从此要输一个管理台上根本没有那一栏的口令
    assert env.login("二号管理员").role == "viewer"


def test_promoting_a_viewer_requires_a_password_in_the_same_request(env):
    """不拦的话库里会出现一个 pwd_hash 为 NULL 的 admin —— `Auth.login` 会拒绝它
    登录，所以结果是一个谁都用不了的管理员，而界面上它看起来完全正常。"""
    viewer = env.viewer("待升级的人")
    r = env.patch_json(f"/v1/admin/users/{viewer.user_id}", {"role": "admin"})
    assert r.status == 400 and env.body_json(r)["error"] == "password_required"
    # 按 id 取那一行，**不要**用 `[-1]`。`list_users` 是 `ORDER BY created_at, id`，
    # 而引导管理员和这个 viewer 经常落在同一毫秒里（实测约四分之一的运行）——
    # 那时排序由随机的 uuid 决定，`[-1]` 有一半概率是引导管理员，于是这条测试会
    # 无缘无故地红一次。按 id 取还顺带更强：它不可能"碰巧检查了另一行而通过"。
    users = {u["id"]: u for u in env.body_json(env.get("/v1/admin/users"))}
    assert users[viewer.user_id]["role"] == "viewer"

    r = env.patch_json(
        f"/v1/admin/users/{viewer.user_id}", {"role": "admin", "password": "pw-3"}
    )
    assert r.status == 200 and env.body_json(r)["role"] == "admin"
    assert env.login("待升级的人", "pw-3").role == "admin"


def test_patch_is_all_or_nothing(env):
    """半套生效的用户改动可能是"角色已经降成 viewer 了、口令还没清" —— 一个谁都
    登不进去的账号。"""
    viewer = env.viewer("不该被改的人")
    r = env.patch_json(
        f"/v1/admin/users/{viewer.user_id}", {"name": "改过的名字", "role": "root"}
    )
    assert r.status == 400
    # 按 id 取，不要用 `[-1]` —— 理由与
    # `test_promoting_a_viewer_requires_a_password_in_the_same_request` 里那段相同：
    # `list_users` 是 `ORDER BY created_at, id`，引导管理员与这个 viewer 常落在同一
    # 毫秒里，那时排序由随机 uuid 决定，`[-1]` 会有一半概率是引导管理员。
    users = {u["id"]: u for u in env.body_json(env.get("/v1/admin/users"))}
    assert users[viewer.user_id]["name"] == "不该被改的人"


def test_changing_a_password_kicks_existing_sessions(env):
    """改口令的常见动机是"我怀疑泄露了"，只换散列的话泄露方手上那个 token 照样能用。"""
    r = env.post_json(
        "/v1/admin/users", {"name": "改口令的人", "role": "admin", "password": "old-pw"}
    )
    uid = env.body_json(r)["id"]
    creds = env.login("改口令的人", "old-pw")
    assert env.get("/v1/auth/me", as_=creds).status == 200
    assert env.patch_json(f"/v1/admin/users/{uid}", {"password": "new-pw"}).status == 200
    assert env.get("/v1/auth/me", as_=creds).status == 401
    assert env.login("改口令的人", "new-pw").role == "admin"


def test_delete_user_is_204_and_takes_the_grants_with_it(env, two_photos):
    a, _ = two_photos
    viewer = env.viewer("要被删的人", photo_ids=[a])
    uid = viewer.user_id
    assert env.request("DELETE", f"/v1/admin/users/{uid}").status == 204
    assert env.get(f"/v1/admin/users/{uid}/grants").status == 404
    assert env.get("/v1/auth/me", as_=viewer).status == 401


def test_patching_an_unknown_user_is_404(env):
    assert env.patch_json("/v1/admin/users/nope", {"name": "x"}).status == 404
    assert env.request("DELETE", "/v1/admin/users/nope").status == 404
    assert env.get("/v1/admin/users/nope/grants").status == 404


def test_user_list_reports_grant_count_without_letting_grant_all_hide_it(env, two_photos):
    """grant_all 的人这里显示的仍是"单独勾了几张"的真实数字 —— 管理台把那个勾去掉
    时，用户剩下的就是这几张，界面必须能在关掉之前就显示出来。"""
    a, _ = two_photos
    viewer = env.viewer("勾了一张又给了全部的人", grant_all=True, photo_ids=[a])
    row = next(u for u in env.body_json(env.get("/v1/admin/users")) if u["id"] == viewer.user_id)
    assert row["grantAll"] is True and row["grantCount"] == 1
    assert row["lastSeenAt"] is not None, "登录过就该有 lastSeenAt"


# ---- 授权管理 ----


def test_grants_round_trip(env, two_photos):
    a, b = two_photos
    viewer = env.viewer("授权改来改去的人")
    body = env.body_json(env.get(f"/v1/admin/users/{viewer.user_id}/grants"))
    assert body == {"grantAll": False, "photoIds": []}

    r = env.put_json(
        f"/v1/admin/users/{viewer.user_id}/grants", {"photoIds": [a, b]}
    )
    assert r.status == 200 and set(env.body_json(r)["photoIds"]) == {a, b}
    assert env.body_json(env.get("/v1/photos", as_=viewer))["total"] == 2

    # 整体替换，不是增量：勾选框提交的语义就是"这就是全集"
    r = env.put_json(f"/v1/admin/users/{viewer.user_id}/grants", {"photoIds": [b]})
    assert env.body_json(r)["photoIds"] == [b]
    assert [p["photoId"] for p in env.body_json(env.get("/v1/photos", as_=viewer))["photos"]] == [b]


def test_put_grants_names_the_unknown_photo_ids(env, two_photos):
    """管理台一次提交几十个勾选框，只说"有一个不对"等于让人一个一个试。"""
    a, _ = two_photos
    viewer = env.viewer("勾错了的人")
    r = env.put_json(
        f"/v1/admin/users/{viewer.user_id}/grants", {"photoIds": [a, "f" * 32]}
    )
    assert r.status == 400 and env.body_json(r)["error"] == "unknown_photo"
    assert env.body_json(r)["unknownPhotoIds"] == ["f" * 32]
    # 整批失败：合法的那个也不能落地
    assert env.body_json(env.get(f"/v1/admin/users/{viewer.user_id}/grants"))["photoIds"] == []


def test_put_grants_can_toggle_grant_all(env, two_photos):
    viewer = env.viewer("被给了全部的人")
    r = env.put_json(
        f"/v1/admin/users/{viewer.user_id}/grants", {"grantAll": True, "photoIds": []}
    )
    assert r.status == 200 and env.body_json(r)["grantAll"] is True
    assert env.body_json(env.get("/v1/photos", as_=viewer))["total"] == 2
    env.put_json(f"/v1/admin/users/{viewer.user_id}/grants", {"grantAll": False})
    assert env.body_json(env.get("/v1/photos", as_=viewer))["total"] == 0


def test_put_grants_rejects_a_non_list(env):
    viewer = env.viewer("提交了个字符串的人")
    r = env.put_json(f"/v1/admin/users/{viewer.user_id}/grants", {"photoIds": "abc"})
    assert r.status == 400 and env.body_json(r)["error"] == "bad_photo_ids"


# ---- 管理台静态页 ----


def test_admin_page_is_served_without_auth(env):
    """页面本身就是输口令的那个界面：要求先鉴权才能拿到它等于要求先登录才能看到
    登录框。边界在 /v1/admin/*，而不在这里。"""
    for path in ("/admin", "/admin/", "/admin/index.html"):
        r = env.get(path, auth=False)
        assert r.status == 200, path
        assert r.headers["Content-Type"].startswith("text/html"), path
        # 认的是"这三条路径都拿到了管理台首页"，不是页面内容本身。挑标题里那句话
        # 而不是某个 class 名或元素：改版会动 DOM，但这个服务的管理台叫什么不会变。
        assert "photoar 管理台" in env.body_bytes(r).decode("utf-8"), path
    # 而页面里的调用仍然要鉴权
    assert env.get("/v1/admin/users", auth=False).status == 401


def test_admin_page_revalidates_instead_of_caching_for_an_hour(env):
    """`max-age=3600` 的后果是升级完之后有一小时里旧页面配新接口，而"清一下缓存
    就好了"是最不该出现在家用部署里的指示。"""
    r = env.get("/admin", auth=False)
    assert r.headers["Cache-Control"] == "no-cache"
    etag = r.headers["ETag"]
    r2 = env.get("/admin", auth=False, headers={"if-none-match": etag})
    assert r2.status == 304


def test_admin_page_rejects_other_methods(env):
    assert env.request("POST", "/admin", auth=False).status == 405


@pytest.mark.parametrize(
    "path",
    [
        "/admin/nope.html",  # 不存在
        "/admin/..",  # 目录本身
        "/admin/.",
        "/admin/.env",  # 以点开头的一律不给（.env、.git 一并挡住）
        "/admin/../../etc/passwd",
        "/admin/%2e%2e%2f%2e%2e%2fetc%2fpasswd",  # 解码后就是上一行
        "/admin/%2e%2e/index.html",
        "/admin/sub/dir.js",  # 只有一层，不支持子目录
        "/admin/a\\b.js",  # Windows 风格分隔符
    ],
)
def test_admin_page_path_traversal(env, path):
    r = env.get(path, auth=False)
    assert r.status in (403, 404), f"{path} -> {r.status}"
    if r.status == 403:
        assert env.body_json(r)["error"] == "path_denied"


def test_admin_page_symlink_escape_is_denied(env, tmp_path):
    """白名单内的符号链接指向目录外 —— 纯前缀比较会放行，`safepath` 是解析后再比。"""
    secret = tmp_path / "secret.html"
    secret.write_text("绝密", encoding="utf-8")
    fake_webui = tmp_path / "webui"
    fake_webui.mkdir()
    (fake_webui / "index.html").write_text("ok", encoding="utf-8")
    (fake_webui / "escape.html").symlink_to(secret)

    env.srv = app.Server(
        cfg=env.cfg,
        catalog=env.srv.catalog,
        library=env.srv.library,
        roots=env.srv.roots,
        resolver=env.srv.resolver,
        auth=env.srv.auth,
        config=env.srv.config,
        webui_dir=fake_webui,
    )
    assert env.get("/admin", auth=False).status == 200  # 目录本身是通的
    r = env.get("/admin/escape.html", auth=False)
    assert r.status == 403 and env.body_json(r)["error"] == "path_denied"
    assert "secret" not in env.body_json(r)["message"], "不回显解析结果"
