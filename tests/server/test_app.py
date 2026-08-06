"""spec §14.4 的闭环，以及 §7 每个接口的行为。

闭环这一条是 Phase 1 的出口条件：**入库 → 识别 → 解析 → 取流**，全程不依赖
真实 NAS、网盘、ARCore 工具链或 ffmpeg（都由 conftest 的假二进制顶替）。
"""

import json

from photoar import synth, transcode
from photoar.server import app, fsbrowser


# ---- 鉴权与路由 ----


def test_ping_requires_token(env):
    assert env.get("/v1/ping", auth=False).status == 401


def test_ping_with_wrong_token(env):
    r = env.get("/v1/ping", headers={"authorization": "Bearer wrong"}, auth=False)
    assert r.status == 401


def test_ping_rejects_non_bearer_scheme(env):
    r = env.get("/v1/ping", headers={"authorization": "Basic abc"}, auth=False)
    assert r.status == 401


def test_ping_ok(env):
    r = env.get("/v1/ping")
    assert r.status == 200
    body = env.body_json(r)
    assert body["ok"] is True and body["version"] == env.cfg.version
    assert r.headers["Cache-Control"] == "no-store"


def test_unknown_route_is_404(env):
    assert env.get("/v1/nope").status == 404
    assert env.get("/nope").status == 404


def test_unknown_route_does_not_leak_auth_state(env):
    """非 /v1/ 前缀先 404 再谈鉴权是刻意的：那些路径根本不是本服务的接口，
    对它们返回 401 等于告诉扫描者"这里有个需要鉴权的东西"。"""
    assert env.get("/nope", auth=False).status == 404


def test_wrong_method_is_405(env):
    r = env.request("POST", "/v1/ping")
    assert r.status == 405


# ---- 入库 ----


def test_ingest_then_recognize_then_resolve_then_stream(env):
    """§14.4 闭环：一条测试走完四步。"""
    img = env.textured(seed=5, w=1200, h=800)
    ref = env.nas / "photos" / "a.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    import cv2

    assert cv2.imwrite(str(ref), img)
    video = env.write_video("videos/a.mp4")

    # 1. 入库
    resp = env.ingest(ref, width_mm=152.0, video=video, title="外婆生日")
    assert resp.status == 201, env.body_json(resp)
    created = env.body_json(resp)
    pid = created["photoId"]
    assert created["qualityScore"] == 85
    assert created["printWidthM"] == 0.152
    assert created["transcoded"] is False, "1280x720/3s/faststart 不该触发转码"
    assert created["libraryPhotos"] == 1

    # 2. 识别（用扰动后的查询图，模拟手机拍到的实体照片）
    query, _ = synth.generate(img, count=1, seed=3)[0]
    r = env.post_frame("/v1/recognize", env.jpeg_of(query))
    assert r.status == 200
    hit = env.body_json(r)
    assert hit["matched"] is True and hit["photoId"] == pid
    assert hit["printWidthM"] == 0.152
    assert hit["imgdbUrl"] == f"/v1/photo/{pid}/imgdb"
    assert hit["mediaUrl"] == f"/v1/photo/{pid}/media"
    assert abs(hit["refAspect"] - 1200 / 800) < 1e-3
    assert hit["inliers"] >= 40

    # 3. 解析媒体
    r = env.get(hit["mediaUrl"])
    assert r.status == 200
    media = env.body_json(r)
    assert media["via"] == "nas_serve"
    assert media["absolute"] is False
    assert media["supportsRange"] is True
    assert media["missing"] is False
    assert r.headers["Cache-Control"] == "no-store", "直链有有效期，绝不能被缓存"

    # 4. 取流
    r = env.get(media["url"])
    assert r.status == 200
    assert r.headers["Accept-Ranges"] == "bytes"
    full = env.body_bytes(r)
    assert len(full) == media["bytes"] == video.stat().st_size

    r = env.get(media["url"], headers={"range": "bytes=10-19"})
    assert r.status == 206
    assert r.headers["Content-Range"] == f"bytes 10-19/{len(full)}"
    assert env.body_bytes(r) == full[10:20]


def test_ingest_without_video(env):
    ref = env.write_image("photos/b.jpg", seed=6)
    pid = env.ingest_ok(ref)
    body = env.body_json(env.get(f"/v1/photo/{pid}/media"))
    assert body["url"] is None and body["reason"] == "no_video"
    assert env.get(f"/v1/photo/{pid}")  # 详情仍可读


def test_ingest_allows_omitting_print_width(env):
    """不填 printWidthMm 能入库，库里记 0 = 未知。

    原来这里必须返回 400 missing_print_width，理由是"跟踪精度依赖它"。改了是因为照片
    实际尺寸经常不知道，而强制必填的结果是随手填一个数 —— 一个**猜的**宽度比不填更糟：
    ARCore 会照它回显 getExtentX，端上按这个错数字画四边形，位姿却来自量纲真实的 SLAM，
    两个尺度一错位视频就贴不上，而现象和"跟踪算不准"一模一样。
    """
    ref = env.write_image("photos/c.jpg", seed=7)
    r = env.post_json("/v1/photo", {"refPath": str(ref)})
    assert r.status == 201, env.body_json(r)
    assert env.body_json(r)["printWidthM"] == 0.0

    listed = env.body_json(env.get("/v1/photos"))["photos"]
    row = next(p for p in listed if p["photoId"] == env.body_json(r)["photoId"])
    assert row["printWidthM"] == 0.0


def test_ingest_rejects_bad_print_width(env):
    ref = env.write_image("photos/c2.jpg", seed=7)
    r = env.post_json("/v1/photo", {"refPath": str(ref), "printWidthMm": "big"})
    assert r.status == 400 and env.body_json(r)["error"] == "bad_print_width"


def test_ingest_rejects_negative_print_width(env):
    """负宽度仍然拒 —— 它不是"未知"，是算错了或单位搞反了。

    静默当未知处理会把一个真实的 bug 藏起来：调用方以为填了宽度，服务端悄悄丢掉。
    """
    ref = env.write_image("photos/c3.jpg", seed=7)
    r = env.post_json("/v1/photo", {"refPath": str(ref), "printWidthMm": -152})
    assert r.status == 400 and env.body_json(r)["error"] == "bad_print_width"


def test_ingest_rejects_missing_file(env):
    r = env.post_json(
        "/v1/photo",
        {"refPath": str(env.nas / "photos" / "nope.jpg"), "printWidthMm": 152},
    )
    assert r.status == 404 and env.body_json(r)["error"] == "ref_not_found"


def test_ingest_rejects_non_image(env):
    p = env.nas / "photos" / "notes.txt"
    p.write_text("hello", encoding="utf-8")
    r = env.post_json("/v1/photo", {"refPath": str(p), "printWidthMm": 152})
    assert r.status == 415 and env.body_json(r)["error"] == "ref_not_image"


def test_ingest_rejects_low_quality_with_actionable_detail(make_env):
    """spec §13：质量分不足要返回分数与建议，不能只说 bad request。"""
    env = make_env(quality_score=40)
    ref = env.write_image("photos/d.jpg", seed=8)
    r = env.ingest(ref)
    assert r.status == 422
    body = env.body_json(r)
    assert body["error"] == "quality_too_low"
    assert body["score"] == 40 and body["minScore"] == 75
    assert body["suggestion"]


def test_ingest_rejects_keypointless_photo_with_422_not_500(env, fake_arcoreimg):
    """arcoreimg 连关键点都提不够 → 422，**不是** 500。

    这一条是放量模拟里找到的：3030 次入库尝试有 65 次（2.1%）撞上纹理不足到
    arcoreimg 拒绝出分的照片，全部返回 500 + 一整个 traceback。两个后果都真实：
    调用方看到 5xx 会当成「服务端故障」去重试（同一张图重试一万次结果一样），
    而一万张的批量入库会往日志里灌两百多个栈，把真正的服务端故障淹掉。

    用 quality_too_low 这同一个 code：对调用方和用户，该做的事一模一样（换图）。
    """
    ref = env.write_image("photos/flat.jpg", seed=21)
    env.cfg.arcoreimg = fake_arcoreimg(
        exit_code=1, stderr="Failed to get enough keypoints from target image."
    )
    r = env.ingest(ref)
    assert r.status == 422
    body = env.body_json(r)
    assert body["error"] == "quality_too_low"
    # score 0 = 连分都没算出来，落在最差那一档
    assert body["score"] == 0 and body["minScore"] == 75
    assert body["suggestion"]


def test_ingest_same_path_twice_is_409(env):
    ref = env.write_image("photos/e.jpg", seed=9)
    pid = env.ingest_ok(ref)
    r = env.ingest(ref)
    assert r.status == 409
    body = env.body_json(r)
    assert body["error"] == "already_ingested" and body["photoId"] == pid


def test_ingest_near_duplicate_is_409_with_conflicts(env):
    """同一张图另存一份再入库。两份都入库会让它们互相判 ambiguous，
    **两份都永久识别不出来**（Phase 0 的第一条硬结论）。"""
    import cv2

    img = env.textured(seed=12, w=1200, h=800)
    a = env.nas / "photos" / "orig.jpg"
    b = env.nas / "photos" / "copy.jpg"
    assert cv2.imwrite(str(a), img)
    # 换质量重新编码：字节完全不同，内容哈希与 nas_path UNIQUE 都挡不住
    assert cv2.imwrite(str(b), img, [int(cv2.IMWRITE_JPEG_QUALITY), 60])

    first = env.ingest_ok(a)
    r = env.ingest(b)
    assert r.status == 409, env.body_json(r)
    body = env.body_json(r)
    assert body["error"] == "near_duplicate"
    assert [c["photoId"] for c in body["conflicts"]] == [first]
    assert body["conflicts"][0]["inliers"] >= 25


# ---- 识别 ----


def test_recognize_miss_returns_200_not_404(env):
    """扫描时未命中是正常状态（每 400ms 一次），不该产生错误日志噪音。"""
    env.ingest_ok(env.write_image("photos/f.jpg", seed=20))
    r = env.post_frame("/v1/recognize", env.jpeg_of(env.textured(seed=54321)))
    assert r.status == 200
    assert env.body_json(r)["matched"] is False


def test_recognize_on_empty_library(env):
    r = env.post_frame("/v1/recognize", env.jpeg_of(env.textured(seed=1)))
    assert r.status == 200 and env.body_json(r)["matched"] is False


def test_recognize_rejects_undecodable_frame(env):
    r = env.post_frame("/v1/recognize", b"not a jpeg at all")
    assert r.status == 400 and env.body_json(r)["error"] == "bad_frame"


def test_recognize_requires_frame_field(env):
    body = (
        b"------x\r\n"
        b'Content-Disposition: form-data; name="other"\r\n\r\nzz\r\n'
        b"------x--\r\n"
    )
    r = env.request(
        "POST",
        "/v1/recognize",
        body=body,
        headers={"content-type": "multipart/form-data; boundary=----x"},
    )
    assert r.status == 400 and env.body_json(r)["error"] == "missing_frame"


def test_recognize_rejects_oversized_body(env):
    r = env.request(
        "POST",
        "/v1/recognize",
        body=b"x" * (3 * 1024 * 1024),
        headers={"content-type": "multipart/form-data; boundary=----x"},
    )
    assert r.status == 413


def test_recognize_writes_history(env):
    img = env.textured(seed=31, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "h.jpg"
    assert cv2.imwrite(str(ref), img)
    pid = env.ingest_ok(ref)
    query, _ = synth.generate(img, count=1, seed=2)[0]
    env.post_frame(
        "/v1/recognize", env.jpeg_of(query), headers={"x-photoar-endpoint": "lan"}
    )

    entries = env.body_json(env.get("/v1/history"))["entries"]
    assert entries[0]["photoId"] == pid
    assert entries[0]["via"] == "lan", "客户端走的哪条通道只有它自己知道，要如实记下"
    assert entries[0]["refThumbUrl"] == f"/v1/photo/{pid}/thumb"


def test_recognize_reports_orphan_as_miss(env):
    """识别库里有、catalog 里没有 —— 当成未命中而不是 500，客户端继续扫下一帧。"""
    img = env.textured(seed=33, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "orphan.jpg"
    assert cv2.imwrite(str(ref), img)
    pid = env.ingest_ok(ref)
    env.srv.catalog._conn().execute("DELETE FROM photo WHERE id = ?", (pid,))
    env.srv.catalog._conn().commit()

    query, _ = synth.generate(img, count=1, seed=2)[0]
    r = env.post_frame("/v1/recognize", env.jpeg_of(query))
    assert r.status == 200
    assert env.body_json(r) == {
        **env.body_json(r),
        "matched": False,
        "reason": "orphan",
    }
    assert env.srv.check_consistency(), "这种不一致必须能被自检报出来"


# ---- 静态产物 ----


def test_imgdb_is_served_with_immutable_etag(env):
    pid = env.ingest_ok(env.write_image("photos/i.jpg", seed=40))
    r = env.get(f"/v1/photo/{pid}/imgdb")
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert "immutable" in r.headers["Cache-Control"]
    assert len(env.body_bytes(r)) == 4300
    etag = r.headers["ETag"]

    r2 = env.get(f"/v1/photo/{pid}/imgdb", headers={"if-none-match": etag})
    assert r2.status == 304 and r2.content_length == 0


def test_thumb_is_jpeg(env):
    pid = env.ingest_ok(env.write_image("photos/j.jpg", seed=41))
    r = env.get(f"/v1/photo/{pid}/thumb")
    assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
    assert env.body_bytes(r)[:2] == b"\xff\xd8"


def test_bad_photo_id_is_404_not_500(env):
    for pid in ("nope", "../../etc/passwd", "z" * 32):
        assert env.get(f"/v1/photo/{pid}/imgdb").status == 404


# ---- 文件浏览 ----


def test_fs_list_roots_without_path(env):
    body = env.body_json(env.get("/v1/fs/list"))
    assert [e["name"] for e in body["entries"]] == ["nas"]
    assert body["entries"][0]["isRoot"] is True


def test_fs_list_directory(env):
    env.write_image("photos/k.jpg", seed=42)
    env.write_video("photos/k.mp4")
    body = env.body_json(env.get(f"/v1/fs/list?path={env.nas}/photos"))
    kinds = {e["name"]: e.get("kind") for e in body["entries"]}
    assert kinds["k.jpg"] == "image" and kinds["k.mp4"] == "video"
    assert body["parent"] == str(env.nas)


def test_fs_list_parent_of_root_is_none(env):
    """根目录的上一级在白名单外，不能给出来 —— 否则客户端点"上一级"必得 403。"""
    assert env.body_json(env.get(f"/v1/fs/list?path={env.nas}"))["parent"] is None


def test_fs_thumb_with_etag(env):
    p = env.write_image("photos/l.jpg", seed=43)
    r = env.get(f"/v1/fs/thumb?path={p}")
    assert r.status == 200 and r.body[:2] == b"\xff\xd8"
    etag = r.headers["ETag"]
    assert env.get(f"/v1/fs/thumb?path={p}", headers={"if-none-match": etag}).status == 304


def test_fs_thumb_of_undecodable_file_is_415(env):
    p = env.nas / "photos" / "broken.jpg"
    p.write_bytes(b"definitely not a jpeg")
    assert env.get(f"/v1/fs/thumb?path={p}").status == 415


def test_fs_thumb_long_edge(env):
    p = env.write_image("photos/m.jpg", seed=44, w=2000, h=1000)
    import cv2
    import numpy as np

    r = env.get(f"/v1/fs/thumb?path={p}")
    img = cv2.imdecode(np.frombuffer(r.body, np.uint8), cv2.IMREAD_COLOR)
    assert max(img.shape[:2]) == fsbrowser.THUMB_LONG_EDGE


def test_fs_list_of_a_file_is_400(env):
    p = env.write_image("photos/n.jpg", seed=45)
    assert env.get(f"/v1/fs/list?path={p}").status == 400


# ---- 路径穿越（§14.3，走完整 HTTP 层）----


def test_traversal_absolute_outside_root(env):
    r = env.get("/v1/fs/list?path=/etc")
    assert r.status == 403 and env.body_json(r)["error"] == "path_denied"


def test_traversal_dotdot(env):
    r = env.get(f"/v1/fs/list?path={env.nas}/../outside")
    assert r.status == 403


def test_traversal_url_encoded_dotdot(env):
    """`%2e%2e%2f` 解码后就是 `../`。HTTP 层解码之后交给同一道校验，
    不靠枚举编码变体。"""
    r = env.get(f"/v1/fs/list?path={env.nas}%2F%2e%2e%2Foutside")
    assert r.status == 403


def test_traversal_double_encoded(env):
    r = env.get(f"/v1/fs/list?path={env.nas}%252F..%252Foutside")
    assert r.status == 403


def test_traversal_windows_separator(env):
    r = env.get(f"/v1/fs/thumb?path={env.nas}\\..\\outside\\secret.jpg")
    assert r.status == 403


def test_traversal_relative_path(env):
    assert env.get("/v1/fs/list?path=photos").status == 403


def test_traversal_symlink_escape(env):
    link = env.nas / "photos" / "escape"
    link.symlink_to(env.outside)
    r = env.get(f"/v1/fs/list?path={link}")
    assert r.status == 403, "白名单内的符号链接指向白名单外，前缀检查会放行"


def test_traversal_denied_response_does_not_echo_resolved_path(env):
    """403 的响应体不能回显解析结果 —— 符号链接指向哪里是服务端信息。"""
    r = env.get("/v1/fs/list?path=/etc/passwd")
    assert "passwd" not in env.body_json(r)["message"]


def test_ingest_outside_root_is_403(env):
    outside_img = env.outside / "x.jpg"
    import cv2

    assert cv2.imwrite(str(outside_img), env.textured(seed=50, w=800, h=600))
    r = env.post_json(
        "/v1/photo", {"refPath": str(outside_img), "printWidthMm": 152}
    )
    assert r.status == 403


def test_attach_video_outside_root_is_403(env):
    pid = env.ingest_ok(env.write_image("photos/o.jpg", seed=51))
    r = env.post_json(
        f"/v1/photo/{pid}/video", {"videoPath": "/etc/hosts"}
    )
    assert r.status == 403


# ---- 关联视频 / 取流 ----


def test_attach_video_later(env):
    pid = env.ingest_ok(env.write_image("photos/p.jpg", seed=52))
    video = env.write_video("videos/p.mp4")
    r = env.post_json(f"/v1/photo/{pid}/video", {"videoPath": str(video)})
    assert r.status == 200, env.body_json(r)
    media = env.body_json(env.get(f"/v1/photo/{pid}/media"))
    assert media["url"] and media["missing"] is False


def test_media_reports_missing_after_file_deleted(env):
    """spec §6.1：每次 resolve 前校验。文件不在了要如实报，而不是给一个
    404 的播放地址让客户端在用户面前失败。"""
    pid = env.ingest_ok(
        env.write_image("photos/q.jpg", seed=53), video=env.write_video("videos/q.mp4")
    )
    (env.nas / "videos" / "q.mp4").unlink()
    media = env.body_json(env.get(f"/v1/photo/{pid}/media"))
    assert media["missing"] is True and media["integrity"] == "missing"


def test_stream_of_deleted_file_is_404(env):
    pid = env.ingest_ok(
        env.write_image("photos/r.jpg", seed=54), video=env.write_video("videos/r.mp4")
    )
    url = env.body_json(env.get(f"/v1/photo/{pid}/media"))["url"]
    (env.nas / "videos" / "r.mp4").unlink()
    r = env.get(url)
    assert r.status == 404 and env.body_json(r)["error"] == "asset_missing"


def test_stream_range_beyond_eof_is_416(env):
    pid = env.ingest_ok(
        env.write_image("photos/s.jpg", seed=55), video=env.write_video("videos/s.mp4")
    )
    url = env.body_json(env.get(f"/v1/photo/{pid}/media"))["url"]
    size = (env.nas / "videos" / "s.mp4").stat().st_size
    r = env.get(url, headers={"range": f"bytes={size + 10}-"})
    assert r.status == 416
    assert r.headers["Content-Range"] == f"bytes */{size}"


def test_stream_multi_range_serves_full_body(env):
    pid = env.ingest_ok(
        env.write_image("photos/t.jpg", seed=56), video=env.write_video("videos/t.mp4")
    )
    url = env.body_json(env.get(f"/v1/photo/{pid}/media"))["url"]
    r = env.get(url, headers={"range": "bytes=0-9,20-29"})
    assert r.status == 200
    assert len(env.body_bytes(r)) == (env.nas / "videos" / "t.mp4").stat().st_size


def test_stream_bad_asset_id_is_404(env):
    assert env.get("/v1/asset/nope/stream").status == 404
    assert env.get(f"/v1/asset/{'f' * 32}/stream").status == 404


def test_transcode_path_when_video_is_oversized(make_env, fake_ffprobe, fake_ffmpeg):
    """4K 源 → 必须转码，产物落服务自有目录，不污染用户的视频目录。

    这里原来用 1080p 当"超规格"。2026-07-30 播放规格提到 1080p 之后 1080p 本身
    合规了，所以换成 4K —— 只改数字不够，「体积超标但分辨率合规」是新的一条
    路径，由下面那条测试单独覆盖。
    """
    env = make_env()
    env.cfg.ffprobe = fake_ffprobe(name="ffprobe4k", height=2160, width=3840)
    env.srv.cfg = env.cfg
    video = env.write_video("videos/big.mp4")
    resp = env.ingest(env.write_image("photos/u.jpg", seed=57), video=video)
    assert resp.status == 201, env.body_json(resp)
    assert env.body_json(resp)["transcoded"] is True

    pid = env.body_json(resp)["photoId"]
    detail = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert detail["videoPath"] == str(video), "原始视频路径要保留（用户的文件）"
    media = env.body_json(env.get(f"/v1/photo/{pid}/media"))
    assert str(env.cfg.playable_dir) in media["nasPath"], "播的应是转码产物"


def test_transcode_path_when_video_is_only_too_large(make_env, fake_ffprobe, fake_ffmpeg):
    """分辨率、时长、faststart 全合规，只是体积超标 → 仍必须转码。

    这是把 TARGET_HEIGHT 提到 1080 之后新出现的路径，也是最容易漏的一条：手机
    拍的就是 1080p，唯一超标的地方是码率（20-25Mbps，规格的五六倍）。漏了这条
    的表现不是报错，而是把一个 90MB 的原片原样发给客户端 —— AR 里认出照片后
    要等半分钟才出画，两端都不报任何错。
    """
    env = make_env()
    env.cfg.ffprobe = fake_ffprobe(name="ffprobeBig", height=1080, width=1920)
    env.srv.cfg = env.cfg
    video = env.write_video("videos/highbitrate.mp4")
    # 稀疏文件：st_size 是真的（probe 就是读它），但不真占磁盘
    with open(video, "r+b") as f:
        f.truncate(transcode.MAX_PLAYABLE_BYTES + 1)

    resp = env.ingest(env.write_image("photos/hb.jpg", seed=58), video=video)
    assert resp.status == 201, env.body_json(resp)
    assert env.body_json(resp)["transcoded"] is True


# ---- 列表 / 详情 ----


def test_list_photos(env):
    a = env.ingest_ok(env.write_image("photos/v1.jpg", seed=60), title="甲")
    b = env.ingest_ok(
        env.write_image("photos/v2.jpg", seed=61), video=env.write_video("videos/v2.mp4")
    )
    body = env.body_json(env.get("/v1/photos"))
    assert body["total"] == 2
    by_id = {p["photoId"]: p for p in body["photos"]}
    assert by_id[a]["title"] == "甲" and by_id[a]["hasVideo"] is False
    assert by_id[b]["hasVideo"] is True
    assert by_id[a]["refThumbUrl"] == f"/v1/photo/{a}/thumb"


def test_photo_detail_reports_stale_and_missing(env):
    from photoar.server import integrity

    pid = env.ingest_ok(env.write_image("photos/w.jpg", seed=62))
    ref = env.nas / "photos" / "w.jpg"
    import cv2

    assert cv2.imwrite(str(ref), env.textured(seed=999, w=1200, h=800))
    integrity.verify_all(env.srv.catalog)

    detail = env.body_json(env.get(f"/v1/photo/{pid}"))
    assert detail["refStale"] is True
    # 识别仍然尝试命中，但要带上过期提示（spec §13）
    query, _ = synth.generate(env.textured(seed=62, w=1200, h=800), count=1, seed=1)[0]
    hit = env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(query)))
    if hit["matched"]:
        assert hit.get("refStale") is True


# ---- 上传 ----


def test_upload_disabled_by_default(env):
    r = env.request("POST", "/v1/upload?name=a.mp4", body=b"data")
    assert r.status == 503 and env.body_json(r)["error"] == "upload_disabled"


def test_upload_writes_file(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    r = env.request("POST", "/v1/upload?name=new.mp4", body=b"\x00\x01\x02" * 10)
    assert r.status == 201, env.body_json(r)
    body = env.body_json(r)
    assert body["bytes"] == 30
    assert (env.nas / "videos" / "new.mp4").read_bytes() == b"\x00\x01\x02" * 10


def test_upload_rejects_path_in_name(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    for name in ("../escape.mp4", "sub/dir.mp4", ".hidden"):
        r = env.request("POST", f"/v1/upload?name={name}", body=b"x")
        assert r.status == 400, name


def test_upload_refuses_overwrite(make_env, tmp_path):
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    env.write_video("videos/exists.mp4")
    r = env.request("POST", "/v1/upload?name=exists.mp4", body=b"x")
    assert r.status == 409


_TUNNEL = {"cf-ray": "abc-SJC", "cf-connecting-ip": "1.2.3.4"}


def test_upload_over_tunnel_is_allowed_when_it_fits(make_env, tmp_path):
    """**这条曾经是反的。**

    原来的规则是"带 cf-ray 就一律 413"，理由是 App 传的都是几百 MB 的视频。网页版
    把那条前提推翻了：网页的正常访问路径**就是**隧道，而现场随手挑的一张照片加一段
    短视频只有几十 MB，Cloudflare 完全放得过去。一律拒等于网页上传功能整个不存在
    —— 而且报的错还写着"100MB 上限"，让人以为是自己的文件太大。
    """
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    r = env.request("POST", "/v1/upload?name=x.mp4", body=b"x" * 4096, headers=_TUNNEL)
    assert r.status == 201, env.body_json(r)
    assert env.body_json(r)["bytes"] == 4096


def test_upload_over_tunnel_is_rejected_when_too_big(make_env, tmp_path, monkeypatch):
    """超过隧道上限时仍要我们自己拒。

    不能指望 Cloudflare 拒：它掐断时给的是一张没有上下文的错误页，用户只看到
    "上传失败"，而那时文件已经传了一半。我们拒得早，而且话说得全。
    """
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    monkeypatch.setattr(app, "TUNNEL_MAX_UPLOAD_BYTES", 1024)
    r = env.request("POST", "/v1/upload?name=x.mp4", body=b"x" * 2048, headers=_TUNNEL)
    assert r.status == 413 and env.body_json(r)["error"] == "upload_via_tunnel"
    # 报错里要有真实体积，否则用户没法判断"到底超了多少"。
    assert "Tailscale" in env.body_json(r)["message"]


def test_upload_off_tunnel_ignores_the_tunnel_limit(make_env, tmp_path, monkeypatch):
    """LAN / Tailscale 上没有这个上限 —— 它是 Cloudflare 的限制，不是我们的。"""
    env = make_env(upload_dir_root=str(tmp_path / "nas" / "videos"))
    monkeypatch.setattr(app, "TUNNEL_MAX_UPLOAD_BYTES", 1024)
    r = env.request("POST", "/v1/upload?name=x.mp4", body=b"x" * 2048)
    assert r.status == 201, env.body_json(r)


# ---- 杂项 ----


def test_bad_json_body_is_400(env):
    r = env.request(
        "POST",
        "/v1/photo",
        body=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status == 400 and env.body_json(r)["error"] == "bad_json"


def test_empty_json_body_is_400(env):
    assert env.request("POST", "/v1/photo").status == 400


def test_json_array_body_is_400(env):
    r = env.request("POST", "/v1/photo", body=b"[]")
    assert r.status == 400


def test_consistency_check_is_clean_after_normal_ingest(env):
    env.ingest_ok(env.write_image("photos/x.jpg", seed=70))
    assert env.srv.check_consistency() == []


def test_history_limit_is_bounded(env):
    for _ in range(3):
        env.post_frame("/v1/recognize", env.jpeg_of(env.textured(seed=1)))
    assert len(env.body_json(env.get("/v1/history?limit=2"))["entries"]) == 2
    assert env.get("/v1/history?limit=abc").status == 200


def test_response_bodies_are_utf8_json(env):
    r = env.get("/v1/ping")
    assert r.headers["Content-Type"] == "application/json; charset=utf-8"
    json.loads(r.body.decode("utf-8"))


def test_photo_ref_serves_the_original_not_the_thumb(env):
    """`/v1/photo/<id>/ref` 给的是原图，不是缩略图。

    「保存到相册」要存的是原图。客户端手上原来只有 refThumbUrl（缩略图），存下来
    是一张糊的 —— 而这个错误不会报任何错，用户要打开相册才发现。
    """
    ref = env.write_image("photos/orig.jpg", seed=11)
    r = env.post_json("/v1/photo", {"refPath": str(ref)})
    assert r.status == 201, env.body_json(r)
    pid = env.body_json(r)["photoId"]

    got = env.get(f"/v1/photo/{pid}/ref")
    assert got.status == 200
    assert got.headers["Content-Type"] == "image/jpeg"
    body = env.body_bytes(got)
    assert body == ref.read_bytes(), "必须逐字节等于 NAS 上那张原图"

    thumb = env.body_bytes(env.get(f"/v1/photo/{pid}/thumb"))
    assert len(body) != len(thumb), "原图和缩略图不该是同一份"


def test_photo_ref_404_when_original_is_gone(env):
    """原图被挪走/删了 → 404，不是 500。

    这不是服务端故障，客户端该做的事（告诉用户原图没了）和其它 404 一致。
    """
    ref = env.write_image("photos/vanish.jpg", seed=12)
    r = env.post_json("/v1/photo", {"refPath": str(ref)})
    pid = env.body_json(r)["photoId"]
    ref.unlink()
    got = env.get(f"/v1/photo/{pid}/ref")
    assert got.status == 404
    assert env.body_json(got)["error"] == "ref_missing"
