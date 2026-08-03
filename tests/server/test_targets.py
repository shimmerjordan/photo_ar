"""服务端预建的整库多目标 `.imgdb`：`targets.TargetStore` 与两个下发端点。

这套测试盯的是"错一次就让端上离线识别静默失效"的那几条：

1. **版本号必须是内容的函数**。授权集一样 → 同一个文件（不按用户各存一份）；
   任何一张参考图的内容或打印宽度变了 → 版本必须变。反过来错的方向更糟：版本
   不变而内容变了，客户端会一直用 304 拿着一个描述另一批照片的库。
2. **manifest 与 db 不能配错**。db 里有而 manifest 里没有的目标 = 端上认出来了却
   没有元数据（printWidthM / fitMode），视频要么不播要么贴错尺寸。这里钉住
   "ETag 就是 manifest 里那个 version"，那是客户端唯一能自己验证这一对的判据。
3. **一张照片的文件被挪走不能让所有人的离线识别都坏掉**。跳过它，其余照片照样
   建库。
4. **构建不能把请求线程占几十秒**。真实建库耗时未测量（arcoreimg 是闭源二进制且
   不在仓库里），所以构建在后台、请求拿 503 + Retry-After，而"正在建"是正常状态。
5. **ACL 是按调用者的授权集**。manifest 里有标题，标题本身可能是隐私。
"""

import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from photoar import quality, synth
from photoar.server import targets

from .conftest import TOKEN

# 轮询等构建完成的上限。假 arcoreimg 是毫秒级的，给 10 秒是为了在负载很高的 CI 上
# 也不假红。
BUILD_TIMEOUT_S = 10.0


def _prin(env, creds=None):
    """拿一个 `Principal`。不传 creds 就是运维 token（= admin 视角）。"""
    p = env.srv.auth.principal_of(creds.token if creds else TOKEN)
    assert p is not None
    return p


def _built(store, prin, timeout: float = BUILD_TIMEOUT_S) -> targets.TargetSet:
    """轮询到构建完成 —— 客户端拿到 503 之后做的正是这件事。"""
    deadline = time.monotonic() + timeout
    while True:
        got = store.resolve(prin)
        if isinstance(got, targets.TargetSet):
            return got
        assert time.monotonic() < deadline, f"构建超过 {timeout}s 还没好"
        time.sleep(0.02)


def _get_db(env, **kw):
    """GET /v1/targets/db，等到它不再是 503。"""
    deadline = time.monotonic() + BUILD_TIMEOUT_S
    while True:
        r = env.get("/v1/targets/db", **kw)
        if r.status != 503:
            return r
        assert time.monotonic() < deadline, "端点一直在 503"
        time.sleep(0.02)


def _store(env, **kw) -> targets.TargetStore:
    return targets.TargetStore(env.cfg, env.srv.catalog, env.srv.config, **kw)


def _three(env):
    """入三张照片，返回 photoId（入库顺序 = created_at 升序）。"""
    return [
        env.ingest_ok(env.write_image(f"photos/t{i}.jpg", seed=200 + i))
        for i in range(3)
    ]


# ---------------------------------------------------------------------------
# 版本号 = 内容哈希
# ---------------------------------------------------------------------------


def test_same_grant_set_shares_one_file(env):
    """授权集相同的两个人拿到同一个 version 和同一个文件。

    按用户各存一份也能工作，代价是：五口人都 grantAll 的家庭在磁盘上有五份完全相同
    的字节，且入库一张照片要建五次库（而建库耗时未知，可能是几十秒）。
    """
    pids = _three(env)
    store = _store(env)
    a = env.viewer("甲", photo_ids=pids[:2])
    b = env.viewer("乙", photo_ids=pids[:2])

    set_a = _built(store, _prin(env, a))
    set_b = _built(store, _prin(env, b))

    assert set_a.version == set_b.version
    assert set_a.path == set_b.path
    assert set_a.photo_ids == set_b.photo_ids
    assert len(list(env.cfg.targets_dir.glob("*.imgdb"))) == 1, "同一套不该有两个文件"


def test_grant_set_change_changes_version(env):
    """授权集变了 → 版本变。不变的话那个人会一直用 304 拿着一个不含新照片的库。"""
    pids = _three(env)
    store = _store(env)
    creds = env.viewer("小明", photo_ids=pids[:1])
    before = _built(store, _prin(env, creds)).version

    uid = creds.user_id
    assert env.put_json(f"/v1/admin/users/{uid}/grants", {"photoIds": pids}).status == 200
    after = _built(store, _prin(env, creds))

    assert after.version != before
    assert set(after.photo_ids) == set(pids)


def test_ref_content_change_changes_version(env):
    """参考图内容变了 → 版本变。

    判据用的是 catalog 里记着的 `asset.sha256`（由 integrity 那条路维护），而不是
    请求路径上重新哈希 1000 张原图 —— 那是几百 MB 到几 GB 的读盘，而 `/v1/ping`
    会走这条路。这里直接改库里那个值，模拟 verify 跑过之后的状态。
    """
    pid = env.ingest_ok(env.write_image("photos/one.jpg", seed=41))
    store = _store(env)
    prin = _prin(env)
    before = _built(store, prin).version

    ref_asset_id = str(env.srv.catalog.get_photo(pid)["ref_asset_id"])
    env.srv.catalog.update_asset_fingerprint(ref_asset_id, sha256="f" * 64)

    assert _built(store, prin).version != before


def test_print_width_is_part_of_version(env, monkeypatch):
    """打印宽度也在版本号里 —— 它是**烘进** .imgdb 的（清单第三列）。

    不在的话，改一张照片的打印尺寸后端上仍然用旧尺寸贴视频：照片认得出来、视频
    大小不对，而没有任何地方显示"你的库是旧的"。
    """
    pid = env.ingest_ok(env.write_image("photos/w.jpg", seed=42))
    store = _store(env)
    prin = _prin(env)
    before = store.manifest(prin)["version"]

    # 直接改库里那一列：改打印宽度没有对应的生产接口（入库时定下来的），为这条
    # 测试加一个接口反而是把测试的需要塞进产品。
    conn = sqlite3.connect(env.cfg.db_path)
    with conn:
        conn.execute("UPDATE photo SET print_width_m = ? WHERE id = ?", (0.3, pid))
    conn.close()

    assert store.manifest(prin)["version"] != before


def test_empty_grant_set_has_a_stable_version_and_no_file(env):
    """一张都没授权：不是错误状态，也不该有文件。

    空库不能建（0 目标的 .imgdb 没有意义），但版本号照样是一个确定的值 —— 否则
    ETag 语义在这个状态下就断了。
    """
    _three(env)
    store = _store(env)
    creds = env.viewer("没授权的人")
    got = store.resolve(_prin(env, creds))

    assert isinstance(got, targets.TargetSet)
    assert got.photo_ids == () and got.bytes == 0
    assert got.version == store.resolve(_prin(env, creds)).version
    assert not got.path.exists()


# ---------------------------------------------------------------------------
# 容量上限
# ---------------------------------------------------------------------------


def test_overflow_keeps_the_newest(env):
    """超过上限时留下 `created_at` 最新的那些，其余计入 overflow。

    这个规则可以接受，是因为端上没命中会自然落回服务端 `/v1/recognize`（那条路认
    全库），也就是被截掉的照片只是慢一点，不是扫不出来。
    """
    pids = _three(env)
    store = _store(env, max_targets=2)
    got = _built(store, _prin(env))

    assert set(got.photo_ids) == set(pids[1:]), "留下的必须是后入库的两张"
    assert got.overflow == 1


def test_manifest_reports_overflow_and_limit(env):
    pids = _three(env)
    m = _store(env, max_targets=2).manifest(_prin(env))
    assert m["count"] == 2 and m["overflow"] == 1 and m["maxTargets"] == 2
    assert {t["photoId"] for t in m["targets"]} == set(pids[1:])


def test_store_refuses_max_targets_above_arcore_limit(env):
    """配一个超过 1000 的上限要**立刻**拒绝。

    不拒绝的话，那个失败会发生在每一次请求上（`TooManyTargets`），而配置里那一行
    看起来只是一个数字。
    """
    with pytest.raises(ValueError) as exc:
        _store(env, max_targets=quality.MAX_TARGETS_PER_DB + 1)
    assert str(quality.MAX_TARGETS_PER_DB) in str(exc.value)
    with pytest.raises(ValueError):
        _store(env, max_targets=0)


# ---------------------------------------------------------------------------
# 参考图丢了
# ---------------------------------------------------------------------------


def test_asset_marked_missing_is_left_out(env):
    """catalog 里标了 missing 的照片不进这一套（也不算 overflow）。"""
    pids = _three(env)
    cat = env.srv.catalog
    cat.update_asset_fingerprint(
        str(cat.get_photo(pids[0])["ref_asset_id"]), missing=1
    )
    m = _store(env).manifest(_prin(env))

    assert m["count"] == 2 and m["overflow"] == 0
    assert pids[0] not in {t["photoId"] for t in m["targets"]}


def test_missing_file_is_skipped_not_fatal(env, monkeypatch):
    """一张照片的文件被挪走了（catalog 还不知道）→ 跳过它，其余照样建。

    整次建库失败的后果是**所有人**的离线识别都坏掉，而原因只是某一张照片被挪了
    位置。同时钉住"跳过的那张确实没进清单"，而不只是"没抛异常"。
    """
    pids = _three(env)
    seen: list[list[str]] = []
    real = targets.quality.build_multi_target_db

    def spy(items, out_path, arcoreimg=quality.ARCOREIMG):
        items = list(items)
        seen.append([name for name, _, _ in items])
        return real(items, out_path, arcoreimg=arcoreimg)

    monkeypatch.setattr(targets.quality, "build_multi_target_db", spy)

    cat = env.srv.catalog
    ref_asset = cat.get_asset(str(cat.get_photo(pids[1])["ref_asset_id"]))
    Path(str(ref_asset["nas_path"])).unlink()

    got = _built(_store(env), _prin(env))
    assert got.skipped == (pids[1],)
    assert got.bytes > 0
    assert seen and pids[1] not in seen[0]
    # manifest 仍然列着它（构建集合的**超集**）—— 那个方向只会让这张照片在端上
    # 不命中并落回服务端识别，反方向（db 有而 manifest 没有）才会贴错元数据。
    assert pids[1] in got.photo_ids


def test_all_refs_missing_is_a_build_failure(env):
    """一张都读不到（挂载点掉了）不是"跳过几张"，是真的建不出来 → 500。

    这里必须与"正在建"（503）分开：503 会让客户端一直重试一个必然失败的东西，
    而运维那边没有任何信号。
    """
    pids = _three(env)
    cat = env.srv.catalog
    for pid in pids:
        os.unlink(str(cat.get_asset(str(cat.get_photo(pid)["ref_asset_id"]))["nas_path"]))

    # 故意用服务自己那个 store（而不是新造一个）：失败状态记在实例里，端点要看到的
    # 就是这一份。新造一个的话，下面那个 500 断言测的其实是另一次构建。
    store = env.srv.targets
    prin = _prin(env)
    deadline = time.monotonic() + BUILD_TIMEOUT_S
    while True:
        try:
            store.resolve(prin)
        except targets.BuildFailed as exc:
            assert "一张都读不到" in exc.reason or "读不到" in exc.reason
            break
        assert time.monotonic() < deadline, "一直没等到构建失败"
        time.sleep(0.02)

    r = env.get("/v1/targets/db")
    assert r.status == 500 and env.body_json(r)["error"] == "targets_build_failed"


# ---------------------------------------------------------------------------
# 并发与清理
# ---------------------------------------------------------------------------


def test_concurrent_resolve_builds_once(env, monkeypatch):
    """四个线程同时要同一个版本 → 只建一次，四个都拿到 `Building`。

    没有这条守卫的后果不是"多花一点 CPU"：四个 arcoreimg 同时写同一个目标路径，
    而其中任何一个的中间态都会被 `path.is_file()` 当成成品发给别人。
    """
    _three(env)
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []
    real = targets.quality.build_multi_target_db

    def gated(items, out_path, arcoreimg=quality.ARCOREIMG):
        calls.append(1)
        started.set()
        assert release.wait(BUILD_TIMEOUT_S), "测试没有释放这次构建"
        return real(list(items), out_path, arcoreimg=arcoreimg)

    monkeypatch.setattr(targets.quality, "build_multi_target_db", gated)

    store = _store(env)
    prin = _prin(env)
    barrier = threading.Barrier(4)
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        barrier.wait(BUILD_TIMEOUT_S)
        got = store.resolve(prin)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(BUILD_TIMEOUT_S)
        assert started.wait(BUILD_TIMEOUT_S), "一个构建都没起来"
        assert all(isinstance(r, targets.Building) for r in results), results
        assert len({r.version for r in results}) == 1
    finally:
        release.set()

    got = _built(store, prin)
    assert got.bytes > 0
    assert calls == [1], f"同一个版本建了 {len(calls)} 次"


def test_old_versions_are_pruned(env):
    """只留最近 N 个版本。

    每入库一张照片就是一个新版本，每个版本是一个几百 KB 到几 MB 的文件 —— 不清理
    就是无界增长，且增长速度正比于"管理员今天入了多少张"。
    """
    pids = _three(env)
    store = _store(env, keep_versions=2)
    versions = []
    for i in range(3):
        creds = env.viewer(f"人{i}", photo_ids=pids[: i + 1])
        versions.append(_built(store, _prin(env, creds)).version)

    left = {p.stem for p in env.cfg.targets_dir.glob("*.imgdb")}
    # 断言"只剩两个 + 最新那个还在"而不是逐个点名：谁被留下按 mtime 排，而在时间戳
    # 粒度粗的文件系统上（不是 ext4/tmpfs，但存在）两次相邻构建的 mtime 可能相等，
    # 那时"留下哪两个"是任意的。真正要钉住的是"会清理"和"刚建的那个不会被清掉"。
    assert len(left) == 2, f"该只剩两个版本，实际 {left}"
    assert versions[2] in left, "刚建好的那个版本不能被自己这次清理带走"


def test_prune_keeps_temp_files_of_running_builds(env):
    """清理只 glob 成品（`*.imgdb`），正在写的临时文件不在它的射程里。

    删掉一个正在写的临时文件不会报错（写它的进程还持着 fd），但 `os.replace` 之后
    磁盘上就是一个内容不完整的成品 —— 而 `_resolve` 判的只是"文件在不在"。
    """
    _three(env)
    store = _store(env, keep_versions=1)
    tmp = env.cfg.targets_dir / "deadbeef.imgdb.tmp-1-2"
    env.cfg.targets_dir.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"half-written")

    _built(store, _prin(env))
    assert tmp.exists(), "刚写下的临时文件不该被这次清理带走"


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def test_endpoints_require_auth(env):
    for path in ("/v1/targets/manifest", "/v1/targets/db"):
        r = env.get(path, auth=False)
        assert r.status == 401, path


def test_viewer_only_gets_granted_targets(env):
    """manifest 里有标题，而标题本身可能是隐私。"""
    pids = _three(env)
    creds = env.viewer("小红", photo_ids=pids[:1])
    m = env.body_json(env.get("/v1/targets/manifest", as_=creds))

    assert m["count"] == 1
    assert [t["photoId"] for t in m["targets"]] == pids[:1]
    assert m["maxTargets"] == quality.MAX_TARGETS_PER_DB


def test_viewer_db_differs_from_admin_db(env):
    """两个不同的授权集必须是两个不同的版本 —— 否则 ETag 会让 viewer 拿到全库。"""
    pids = _three(env)
    creds = env.viewer("小刚", photo_ids=pids[:1])
    viewer_etag = _get_db(env, as_=creds).headers["ETag"]
    admin_etag = _get_db(env).headers["ETag"]
    assert viewer_etag != admin_etag


def test_manifest_and_db_versions_match(env):
    """ETag 就是 manifest 里那个 version。

    这是客户端唯一能自己验证"这一对是配好的"的判据：先取 manifest、几分钟后再取
    db，中间管理员入了十张照片的话，db 的 ETag 会是新版本，客户端一眼看得出对不上。
    """
    _three(env)
    m = env.body_json(env.get("/v1/targets/manifest"))
    r = _get_db(env)
    assert r.status == 200
    assert r.headers["ETag"] == f'"{m["version"]}"'
    assert len(env.body_bytes(r)) > 0


def test_db_etag_hit_returns_304(env):
    _three(env)
    first = _get_db(env)
    etag = first.headers["ETag"]
    again = env.get("/v1/targets/db", headers={"if-none-match": etag})
    assert again.status == 304
    assert again.headers["ETag"] == etag
    assert again.body == b"" and again.file is None


def test_db_etag_is_the_version_not_mtime(env):
    """ETag 必须是内容哈希，不是文件 mtime 派生的。

    mtime 派生的值有两个毛病：客户端没法拿它与 manifest 的 version 比；而同一个
    版本被清理后重建一次，字节完全相同而 ETag 变了，全体客户端白重下一遍。
    """
    _three(env)
    r = _get_db(env)
    version = env.body_json(env.get("/v1/targets/manifest"))["version"]
    path = env.cfg.targets_dir / f"{version}.imgdb"
    assert path.is_file()

    # 把 mtime 推后一小时：ETag 不能因此变。
    st = path.stat()
    os.utime(path, (st.st_atime + 3600, st.st_mtime + 3600))
    assert env.get("/v1/targets/db").headers["ETag"] == r.headers["ETag"]


def test_db_returns_503_with_retry_after_while_building(env, monkeypatch):
    """正在建 → 503 + `Retry-After`，而不是把请求线程占住几十秒。

    真实建库耗时未测量（arcoreimg 是闭源二进制、不在仓库里）。同步等它的话，这个
    请求穿过 Cloudflare 隧道时可能撞上代理的响应超时，客户端拿到的是一个与"服务器
    挂了"无法区分的 5xx。
    """
    _three(env)
    release = threading.Event()
    started = threading.Event()
    real = targets.quality.build_multi_target_db

    def gated(items, out_path, arcoreimg=quality.ARCOREIMG):
        started.set()
        assert release.wait(BUILD_TIMEOUT_S)
        return real(list(items), out_path, arcoreimg=arcoreimg)

    monkeypatch.setattr(targets.quality, "build_multi_target_db", gated)
    try:
        r = env.get("/v1/targets/db")
        assert r.status == 503
        body = env.body_json(r)
        assert body["error"] == "targets_building"
        assert r.headers["Retry-After"] == str(body["retryAfterS"])
        assert int(r.headers["Retry-After"]) > 0
        assert body["version"] == env.body_json(env.get("/v1/targets/manifest"))["version"]
        assert started.wait(BUILD_TIMEOUT_S)
    finally:
        release.set()

    assert _get_db(env).status == 200


def test_db_404_when_nothing_granted(env):
    """一张都没授权 → 404，**不是**一个 0 字节的文件。

    200 + 空文件的后果：客户端认为离线识别已就绪，然后每一帧都不命中 —— 与"库坏了"
    完全无法区分。
    """
    _three(env)
    creds = env.viewer("路人")
    r = env.get("/v1/targets/db", as_=creds)
    assert r.status == 404 and env.body_json(r)["error"] == "no_targets"


def test_manifest_urls_actually_resolve(env):
    """manifest 给的 URL 真的取得到。

    这两条路径字面量在 `targets.py` 里重复了一次 app.py 的路由表（反向依赖会成环），
    所以由这条测试钉住它们一致 —— 不然改了路由只会表现为"离线命中之后取不到视频"。
    """
    ref = env.write_image("photos/u.jpg", seed=61)
    video = env.write_video("videos/u.mp4")
    pid = env.ingest_ok(ref, video=video)
    m = env.body_json(env.get("/v1/targets/manifest"))
    entry = next(t for t in m["targets"] if t["photoId"] == pid)

    assert env.get(entry["imgdbUrl"]).status == 200
    assert env.get(entry["mediaUrl"]).status == 200
    assert entry["hasVideo"] is True


def test_manifest_fields_match_recognize_hit(env):
    """manifest 每条的字段与 `/v1/recognize` 命中响应**语义一致**。

    客户端解析命中元数据的代码是共用的一份（离线命中与在线命中都走它）。同名字段
    含义不同、或者取整精度不同，表现是"同一张照片离线时视频贴得不对" —— 而两边的
    代码各自看起来都对。
    """
    img = env.textured(seed=71, w=1200, h=800)
    import cv2

    ref = env.nas / "photos" / "same.jpg"
    assert cv2.imwrite(str(ref), img)
    video = env.write_video("videos/same.mp4")
    pid = env.ingest_ok(ref, video=video, title="外婆生日")

    query, _ = synth.generate(img, count=1, seed=5)[0]
    hit = env.body_json(env.post_frame("/v1/recognize", env.jpeg_of(query)))
    assert hit["matched"] is True and hit["photoId"] == pid

    entry = next(
        t
        for t in env.body_json(env.get("/v1/targets/manifest"))["targets"]
        if t["photoId"] == pid
    )
    for key in ("printWidthM", "fitMode", "imgdbUrl", "mediaUrl", "refAspect"):
        assert entry[key] == hit[key], key
    assert entry["title"] == "外婆生日"


def test_manifest_reports_building_flag(env, monkeypatch):
    """manifest 顺手把构建踢起来，并如实说"正在建"。

    少这个字段的话，紧接着那个 503 对客户端就是一次需要猜原因的失败。
    """
    _three(env)
    release = threading.Event()
    real = targets.quality.build_multi_target_db

    def gated(items, out_path, arcoreimg=quality.ARCOREIMG):
        assert release.wait(BUILD_TIMEOUT_S)
        return real(list(items), out_path, arcoreimg=arcoreimg)

    monkeypatch.setattr(targets.quality, "build_multi_target_db", gated)
    try:
        assert env.body_json(env.get("/v1/targets/manifest"))["building"] is True
    finally:
        release.set()

    _get_db(env)
    assert env.body_json(env.get("/v1/targets/manifest"))["building"] is False


def test_manifest_survives_a_broken_build(env, monkeypatch):
    """arcoreimg 坏了也要能取到元数据 —— manifest 的内容与那个文件无关。

    失败会在真正需要它的那个请求（`GET /v1/targets/db`）上以 500 露出来。
    """
    _three(env)

    def boom(items, out_path, arcoreimg=quality.ARCOREIMG):
        raise RuntimeError("arcoreimg 没了")

    monkeypatch.setattr(targets.quality, "build_multi_target_db", boom)

    deadline = time.monotonic() + BUILD_TIMEOUT_S
    while env.get("/v1/targets/db").status != 500:
        assert time.monotonic() < deadline, "一直没等到 500"
        time.sleep(0.02)

    m = env.body_json(env.get("/v1/targets/manifest"))
    assert m["count"] == 3 and m["building"] is False
    r = env.get("/v1/targets/db")
    assert r.status == 500
    assert "arcoreimg" in env.body_json(r)["message"]


# ---------------------------------------------------------------------------
# /v1/ping
# ---------------------------------------------------------------------------


def test_ping_reports_targets_state(env):
    """部署完一条 curl 就能确认端上识别的前提是否就绪。"""
    _three(env)
    body = env.body_json(env.get("/v1/ping"))
    m = env.body_json(env.get("/v1/targets/manifest"))

    assert body["targetsVersion"] == m["version"]
    assert body["targetsCount"] == 3
    assert body["targetsOverflow"] == 0
    assert body["targetsBuilding"] in (True, False)


def test_ping_is_scoped_to_the_caller(env):
    """一个 viewer 的 ping 报的是**他自己那一套**的状态。

    他要确认的正是"我这台手机能不能离线识别"，而不是管理员那一套有多少张。
    """
    pids = _three(env)
    creds = env.viewer("小美", photo_ids=pids[:2])
    body = env.body_json(env.get("/v1/ping", as_=creds))
    assert body["targetsCount"] == 2
    assert body["targetsVersion"] != env.body_json(env.get("/v1/ping"))["targetsVersion"]


def test_ping_does_not_trigger_a_build(env, monkeypatch):
    """ping **不能**触发构建。

    客户端每次网络变化都会对四个 endpoint 并行探活，让它顺手启动一次几十秒的建库
    就是把"通不通"的探测变成一个副作用。
    """
    _three(env)
    calls: list[int] = []

    def spy(items, out_path, arcoreimg=quality.ARCOREIMG):
        calls.append(1)
        raise AssertionError("ping 触发了构建")

    monkeypatch.setattr(targets.quality, "build_multi_target_db", spy)
    assert env.get("/v1/ping").status == 200
    time.sleep(0.05)  # 后台线程真的起了的话，这点时间足够它跑到 spy
    assert calls == []
