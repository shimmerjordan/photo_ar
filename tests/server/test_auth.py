"""用户 / 口令 / 会话 / 授权。

这套测试盯的是"错一次就等于根本没有鉴权"的那几条，其余的（比如列表排序）随手带过：

1. viewer 只输名字就能进，但名字**必须在册** —— 自动建号等于对隧道全网开放。
2. admin 不输口令必须失败；口令列意外为 NULL 的 admin 也必须失败。"viewer 不用
   口令"这条便利绝不能顺着对称性漏到 admin 身上。
3. 明文 token 不进库；过期 session 拒绝并顺手删掉。
4. 运维凭证（`PHOTOAR_TOKEN`）是 admin、没有 user_id、且登不出。
5. `photo_filter` 是"谁能看哪些照片"的唯一判据，它答错的方向必须是"看不到"而不是
   "全都能看"。

时间全部走可拨动的 `clock`，没有一处 sleep：TTL 的产品取值是"30 天"，靠真实时间
去等它过期的测试只有两种写法，慢到没人跑、或者把 TTL 调成 1 秒去测一个产品里
不存在的取值。
"""

import sqlite3

import pytest

from photoar.server import auth, db

LEGACY = "ops-token-for-scripts"


class Clock:
    """可拨动的毫秒时钟。`Auth(now_ms=clock)` 直接接它。"""

    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance_s(self, seconds: float) -> None:
        self.now += int(seconds * 1000)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def catalog(tmp_path):
    return db.Catalog(tmp_path / "catalog.db")


@pytest.fixture
def make_auth(catalog, clock):
    """TTL 取的是短值（viewer 1 小时 / admin 10 分钟），只为让"两个角色拿到不同
    TTL"这件事在断言里看得出来；产品默认值另有测试对着 appconfig 的默认值验。"""

    def _make(**kw):
        kw.setdefault("viewer_ttl_s", 3600)
        kw.setdefault("admin_ttl_s", 600)
        kw.setdefault("legacy_token", LEGACY)
        return auth.Auth(catalog, now_ms=clock, **kw)

    return _make


@pytest.fixture
def a(make_auth):
    return make_auth()


@pytest.fixture
def photo_ids(catalog):
    """三条最小 photo 记录。insert_photo 不碰文件系统，所以不需要真图。"""
    ids = []
    for i in range(3):
        asset_id = catalog.upsert_asset(
            nas_path=f"/nas/photos/p{i}.jpg",
            kind="image",
            sha256=f"{i:064x}",
            bytes_=100 + i,
            mtime=1000 + i,
            width_px=1200,
            height_px=800,
        )
        pid = f"{i:032x}"
        catalog.insert_photo(
            photo_id=pid,
            ref_asset_id=asset_id,
            video_asset_id=None,
            playable_asset_id=None,
            title=f"照片{i}",
            print_width_m=0.152,
            quality_score=90,
            imgdb_path=f"/data/imgdb/{pid}.imgdb",
            imgdb_bytes=4300,
            thumb_path=f"/data/thumb/{pid}.jpg",
            self_score=60,
        )
        ids.append(pid)
    return ids


# ---- 名字规范化 ----


def test_normalize_name_collapses_whitespace_and_case():
    assert auth.normalize_name("  Alice  ") == "alice"
    assert auth.normalize_name("小明") == "小明"
    # 内部连续空白压成一个：手机上"小 明"很容易多打一个空格，而它在输入框里
    # 看不出区别。
    assert auth.normalize_name("小  明") == auth.normalize_name("小 明") == "小 明"


def test_normalize_name_folds_fullwidth_input():
    """中文输入法状态下打出的是全角字母与全角空格。NFKC 之后它们与半角同形。"""
    assert auth.normalize_name("Ａlice") == "alice"
    assert auth.normalize_name("小　明") == "小 明"


def test_display_name_keeps_case_but_shares_whitespace_rules():
    """显示名与唯一键的空白处理必须完全一致，否则会出现"显示名带个尾随空格、
    唯一键没有"这种只在肉眼对齐时才发现的脏数据。"""
    assert auth.display_name("  Alice  Smith ") == "Alice Smith"
    assert auth.normalize_name("  Alice  Smith ") == "alice smith"


def test_check_name_rejects_empty_and_overlong():
    with pytest.raises(auth.InvalidName):
        auth.check_name("   ")
    with pytest.raises(auth.InvalidName):
        auth.check_name("　")  # 全角空格规范化后也是空
    with pytest.raises(auth.InvalidName):
        auth.check_name("x" * (auth.NAME_MAX_LEN + 1))
    assert auth.check_name(" Bob ") == ("Bob", "bob")


# ---- 口令 ----


def test_password_roundtrip():
    h, s = auth.hash_password("正确的马电池订书钉")
    assert auth.verify_password("正确的马电池订书钉", h, s)
    assert not auth.verify_password("错的", h, s)


def test_each_hash_uses_a_fresh_salt():
    """同一个口令两次散列必须不同。相同的话意味着盐是固定的，一张彩虹表就能一次
    问出全部账号。"""
    h1, s1 = auth.hash_password("same")
    h2, s2 = auth.hash_password("same")
    assert s1 != s2
    assert h1 != h2
    assert auth.verify_password("same", h1, s1)
    assert auth.verify_password("same", h2, s2)


def test_verify_rejects_missing_hash_or_salt():
    """散列/盐缺失时 False，绝不"没设口令就放行"—— 那正是"意外清空 admin 的口令列"
    变成"任何人都能当 admin"的那条路径。"""
    assert not auth.verify_password("x", None, b"salt")
    assert not auth.verify_password("x", b"hash", None)
    assert not auth.verify_password("x", b"", b"")


def test_password_is_unicode_normalized():
    """全角输入的口令要能对上半角设的口令，否则表现是"同一个口令在这台手机上能进、
    在那台上不能进"，且无从排查。"""
    h, s = auth.hash_password("abc123")
    assert auth.verify_password("ａｂｃ１２３", h, s)


def test_scrypt_memory_stays_within_the_nas_budget():
    """把 scrypt 的内存开销钉住。

    128 * n * r = 16 MiB/次，而 HTTP 是每请求一线程，所以峰值是"并发登录数 ×
    16 MiB"。这个断言的作用是：以后有人为了"更安全"把 n 调大时，必须在这里改一个
    写着 3GB NAS 的数字，而不是改一个看不出后果的常量。
    """
    cost = 128 * auth.SCRYPT_N * auth.SCRYPT_R
    assert cost == 16 * 1024 * 1024
    # maxmem 必须显式高于实际开销：默认值 0 会落到 OpenSSL 的 32 MiB 上限，
    # 于是"把 n 翻一倍"这个改动会在真的有人登录时才抛 ValueError。
    assert auth.SCRYPT_MAXMEM > cost


# ---- 登录 ----


def test_viewer_logs_in_with_name_only(a, catalog):
    uid = a.create_user(name="小明", role=auth.VIEWER)
    token, p = a.login("小明", None)
    assert token
    assert p.user_id == uid
    assert p.name == "小明"
    assert p.role == auth.VIEWER
    assert p.via == "session"
    assert not p.is_admin


def test_viewer_login_tolerates_case_and_whitespace(a):
    a.create_user(name="Alice", role=auth.VIEWER)
    _, p = a.login("  aLiCe ", None)
    assert p.name == "Alice", "显示名要回原样，不是用户这次输入的写法"


def test_unknown_name_is_refused_and_creates_nothing(a, catalog):
    """自动建号会让 viewer 这一层鉴权完全不存在（任何人输个新名字就能进）。"""
    with pytest.raises(auth.UnknownUser):
        a.login("路人", None)
    assert catalog.count_users() == 0


def test_empty_name_is_refused(a):
    a.create_user(name="小明", role=auth.VIEWER)
    with pytest.raises(auth.UnknownUser):
        a.login("   ", None)


def test_admin_needs_a_password(a):
    a.create_user(name="root", role=auth.ADMIN, password="s3cret")
    with pytest.raises(auth.BadCredentials):
        a.login("root", None)
    with pytest.raises(auth.BadCredentials):
        a.login("root", "")
    with pytest.raises(auth.BadCredentials):
        a.login("root", "wrong")
    token, p = a.login("root", "s3cret")
    assert token and p.is_admin


def test_admin_without_stored_password_cannot_log_in(a, catalog):
    """口令列为 NULL 的 admin 必须谁都登不进去，**给什么口令都不行**。

    这一行只能由手工改库或代码 bug 造出来。它的危险在于："viewer 没口令就放行"
    是一条正当规则，顺着对称性把它套到 admin 上就得到一个"知道管理员名字就能进"
    的后门。
    """
    uid = catalog.create_user(name="root", name_key="root", role=auth.ADMIN)
    assert catalog.get_user(uid)["pwd_hash"] is None
    with pytest.raises(auth.BadCredentials):
        a.login("root", None)
    with pytest.raises(auth.BadCredentials):
        a.login("root", "anything")


def test_viewer_with_a_password_must_supply_it(a):
    """schema 没禁止 viewer 设口令。设了就必须验 —— "这一列有值但从来不检查"
    是最糟的一种状态：界面显示"已设置口令"，实际谁都能进。"""
    a.create_user(name="客人", role=auth.VIEWER, password="pw")
    with pytest.raises(auth.BadCredentials):
        a.login("客人", None)
    with pytest.raises(auth.BadCredentials):
        a.login("客人", "nope")
    assert a.login("客人", "pw")[1].role == auth.VIEWER


def test_disabled_account_is_refused(a, catalog):
    uid = a.create_user(name="小明", role=auth.VIEWER)
    catalog.update_user(uid, disabled=True)
    with pytest.raises(auth.AccountDisabled):
        a.login("小明", None)


def test_unknown_role_fails_closed(a, catalog):
    """角色只能是 viewer/admin。库里出现别的值时"当 viewer 处理"是在猜，
    而猜错的方向是放行。"""
    catalog.create_user(name="怪人", name_key="怪人", role="superuser")
    with pytest.raises(auth.BadCredentials):
        a.login("怪人", None)


def test_ttl_depends_on_role(a, catalog, clock):
    a.create_user(name="小明", role=auth.VIEWER)
    a.create_user(name="root", role=auth.ADMIN, password="pw")
    viewer_token, _ = a.login("小明", None)
    admin_token, _ = a.login("root", "pw")

    def expires_of(token):
        row = catalog.get_session(auth._sha256(token))
        return row["expires_at"] - clock.now

    assert expires_of(viewer_token) == 3600 * 1000
    assert expires_of(admin_token) == 600 * 1000


def test_plaintext_token_never_reaches_the_database(a, tmp_path, catalog):
    """库文件里不能出现明文 token。库被拖走 = 永久登录，是这张表只存 sha256 的
    全部理由。"""
    a.create_user(name="小明", role=auth.VIEWER)
    token, _ = a.login("小明", None)
    catalog.close()  # 逼 WAL 落盘，否则刚写的行可能还在 -wal 文件里
    blob = b"".join(
        p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("catalog.db")
    )
    assert token.encode() not in blob
    assert auth._sha256(token) in blob


def test_login_updates_last_seen(a, catalog, clock):
    uid = a.create_user(name="小明", role=auth.VIEWER)
    assert catalog.get_user(uid)["last_seen_at"] is None
    a.login("小明", None)
    assert catalog.get_user(uid)["last_seen_at"] == clock.now


# ---- principal_of ----


def test_session_token_resolves_to_its_user(a):
    a.create_user(name="小明", role=auth.VIEWER, grant_all=True)
    token, _ = a.login("小明", None)
    p = a.principal_of(token)
    assert p is not None
    assert p.name == "小明"
    assert p.grant_all is True
    assert p.via == "session"


def test_garbage_tokens_resolve_to_none(a):
    assert a.principal_of("") is None
    assert a.principal_of("not-a-token") is None


def test_non_ascii_token_does_not_crash(a):
    """token 来自 Authorization 头，客户端可以塞任何字节进来。

    `hmac.compare_digest` 只接受"两个都是 ASCII-only 的 str"，直接拿它比字符串
    会抛 TypeError —— 表现是一个 500 而不是 401，而 500 会被当成服务端故障去查。
    """
    assert a.principal_of("令牌🙂") is None


def test_expired_session_is_rejected_and_deleted(a, catalog, clock):
    a.create_user(name="小明", role=auth.VIEWER)
    token, _ = a.login("小明", None)
    sha = auth._sha256(token)
    assert catalog.get_session(sha) is not None

    clock.advance_s(3600)  # 正好到期。expires_at <= now 判过期，边界也算过期。
    assert a.principal_of(token) is None
    assert catalog.get_session(sha) is None, (
        "过期会话要顺手删掉，否则一个几个月不重启的服务会攒下一堆死行"
    )


def test_disabled_user_loses_access_but_keeps_the_session_row(a, catalog):
    """停用是可撤销的，所以不删 session 行；要立刻踢掉全部设备是"停用"这个动作
    自己该做的事（delete_sessions_of_user），不是 principal_of 该顺手做的。"""
    uid = a.create_user(name="小明", role=auth.VIEWER)
    token, _ = a.login("小明", None)
    catalog.update_user(uid, disabled=True)
    assert a.principal_of(token) is None
    assert catalog.get_session(auth._sha256(token)) is not None

    catalog.update_user(uid, disabled=False)
    assert a.principal_of(token) is not None, "恢复启用后原来的 token 应当继续有效"


def test_last_used_is_throttled(make_auth, catalog, clock):
    """`last_used_at` 不能每个请求都写。

    识别请求在扫描时是每秒好几次，而 Catalog 的写全部串行在一把锁上（入库时那把
    锁可能被 ffmpeg 持有几十秒）。每请求一次写，等于把纯读的识别请求变成要排队的
    写事务 —— 现象是扫描时莫名卡顿，而没有一行代码看起来像在写库。
    """
    a = make_auth(touch_interval_s=60)
    a.create_user(name="小明", role=auth.VIEWER)
    token, _ = a.login("小明", None)
    sha = auth._sha256(token)
    first = catalog.get_session(sha)["last_used_at"]

    clock.advance_s(30)
    a.principal_of(token)
    assert catalog.get_session(sha)["last_used_at"] == first, "间隔内不该写库"

    clock.advance_s(31)
    a.principal_of(token)
    assert catalog.get_session(sha)["last_used_at"] == clock.now


# ---- 登出 ----


def test_logout_invalidates_the_token_and_is_idempotent(a):
    a.create_user(name="小明", role=auth.VIEWER)
    token, _ = a.login("小明", None)
    a.logout(token)
    assert a.principal_of(token) is None
    a.logout(token)  # 再来一次不该抛
    a.logout("")


# ---- 运维凭证 ----


def test_legacy_token_is_a_machine_admin(a):
    p = a.principal_of(LEGACY)
    assert p is not None
    assert p.via == "legacy_token"
    assert p.role == auth.ADMIN and p.is_admin
    assert p.user_id is None, "运维凭证不对应任何一个人，硬造 user 行会让「删用户」能把入库脚本搞挂"
    assert p.grant_all is True


def test_legacy_token_cannot_be_logged_out(a):
    """它没有服务端状态可删，要作废只能改环境变量重启。这里刻意不抛异常：
    让健康检查脚本误调一次 logout 就 500，比静默无事发生糟得多。"""
    a.logout(LEGACY)
    assert a.principal_of(LEGACY) is not None


def test_empty_legacy_token_is_not_a_credential(make_auth):
    """没配 `PHOTOAR_TOKEN` 时，空字符串不能变成一把万能钥匙。"""
    a = make_auth(legacy_token="")
    assert a.principal_of("") is None
    assert a.principal_of("anything") is None


def test_wrong_legacy_token_is_refused(a):
    assert a.principal_of(LEGACY + "x") is None
    assert a.principal_of(LEGACY[:-1]) is None


# ---- 引导管理员 ----


def test_bootstrap_creates_the_first_admin(a, catalog, clock):
    uid = a.ensure_bootstrap_admin(" Root ", "pw")
    assert uid is not None
    row = catalog.get_user(uid)
    assert row["name"] == "Root" and row["name_key"] == "root"
    assert row["role"] == auth.ADMIN
    assert row["created_at"] == clock.now
    assert row["grant_all"] == 0, (
        "admin 看全库来自 role，不来自 grant_all；写 1 的话把他降级成 viewer "
        "时会静默保留全库权限"
    )
    assert a.login("root", "pw")[1].is_admin


def test_bootstrap_is_a_noop_when_an_admin_exists(a):
    a.create_user(name="root", role=auth.ADMIN, password="pw")
    assert a.ensure_bootstrap_admin("root2", "pw2") is None


def test_bootstrap_counts_disabled_admins_too(a, catalog):
    """否则"停用唯一的管理员"会让下次启动用环境变量里的口令悄悄建出第二个管理员
    —— 一个没人操作过、却拥有全部权限的账号。"""
    uid = a.create_user(name="root", role=auth.ADMIN, password="pw")
    catalog.update_user(uid, disabled=True)
    assert a.ensure_bootstrap_admin("root2", "pw2") is None
    assert catalog.count_users(auth.ADMIN) == 1


def test_bootstrap_still_runs_when_only_viewers_exist(a):
    a.create_user(name="小明", role=auth.VIEWER)
    assert a.ensure_bootstrap_admin("root", "pw") is not None


def test_bootstrap_refuses_to_promote_an_existing_user(a):
    """把一个 viewer 静默提权，是环境变量能做到的最危险的事。"""
    a.create_user(name="小明", role=auth.VIEWER)
    with pytest.raises(db.NameTaken):
        a.ensure_bootstrap_admin("小明", "pw")


def test_bootstrap_requires_a_password(a):
    with pytest.raises(auth.BadCredentials):
        a.ensure_bootstrap_admin("root", "")


# ---- purge ----


def test_purge_expired_removes_only_expired_sessions(a, catalog, clock):
    a.create_user(name="小明", role=auth.VIEWER)
    a.create_user(name="root", role=auth.ADMIN, password="pw")
    admin_token, _ = a.login("root", "pw")  # TTL 600s
    viewer_token, _ = a.login("小明", None)  # TTL 3600s

    clock.advance_s(700)
    assert a.purge_expired() == 1
    assert catalog.get_session(auth._sha256(admin_token)) is None
    assert catalog.get_session(auth._sha256(viewer_token)) is not None
    assert a.purge_expired() == 0


# ---- set_password ----


def test_changing_password_kicks_existing_sessions(a, catalog):
    """改口令的常见动机是"我怀疑泄露了"。只换散列的话，泄露方手上那个还没过期的
    token 照样能用。"""
    uid = a.create_user(name="root", role=auth.ADMIN, password="old")
    token, _ = a.login("root", "old")
    a.set_password(uid, "new")
    assert a.principal_of(token) is None
    with pytest.raises(auth.BadCredentials):
        a.login("root", "old")
    assert a.login("root", "new")[1].is_admin


def test_clearing_password_makes_a_viewer_nameonly_again(a):
    uid = a.create_user(name="客人", role=auth.VIEWER, password="pw")
    a.set_password(uid, None)
    assert a.login("客人", None)[1].role == auth.VIEWER


def test_create_user_validates_role_and_admin_password(a):
    with pytest.raises(auth.InvalidName):
        a.create_user(name="x", role="superuser")
    with pytest.raises(auth.BadCredentials):
        a.create_user(name="x", role=auth.ADMIN)


# ---- photo_filter ----


def test_photo_filter_by_role_and_grant_all(a):
    admin = auth.Principal(user_id="u1", name="root", role=auth.ADMIN, grant_all=False, via="session")
    everything = auth.Principal(user_id="u2", name="妈", role=auth.VIEWER, grant_all=True, via="session")
    limited = auth.Principal(user_id="u3", name="客人", role=auth.VIEWER, grant_all=False, via="session")
    assert auth.photo_filter(admin) is None
    assert auth.photo_filter(everything) is None
    assert auth.photo_filter(limited) == "u3"
    assert auth.photo_filter(a.principal_of(LEGACY)) is None


def test_photo_filter_refuses_to_guess_without_a_user_id():
    """"没有 user_id 又不是 admin"只能是构造 Principal 的代码写错了。此时返回
    None（= 不过滤 = 全库可见）是最坏的失败方式，所以宁可 500。"""
    broken = auth.Principal(user_id=None, name="?", role=auth.VIEWER, grant_all=False, via="session")
    with pytest.raises(ValueError):
        auth.photo_filter(broken)


# ---- Catalog：用户 / 会话 / 授权 ----


def test_user_crud(catalog):
    uid = catalog.create_user(name="Alice", name_key="alice", role=auth.VIEWER)
    assert catalog.get_user(uid)["name"] == "Alice"
    assert catalog.get_user_by_name_key("alice")["id"] == uid
    assert catalog.get_user_by_name_key("Alice") is None, "查找键必须是规范化后的值"
    assert [u["id"] for u in catalog.list_users()] == [uid]
    assert catalog.count_users() == 1
    assert catalog.count_users(auth.ADMIN) == 0

    catalog.update_user(uid, role=auth.ADMIN, grant_all=True, disabled=True)
    row = catalog.get_user(uid)
    assert (row["role"], row["grant_all"], row["disabled"]) == (auth.ADMIN, 1, 1)
    catalog.update_user(uid)  # 什么都不传 = 什么都不改，且不该报错
    assert catalog.get_user(uid)["role"] == auth.ADMIN


def test_duplicate_name_key_is_rejected(catalog):
    catalog.create_user(name="Alice", name_key="alice", role=auth.VIEWER)
    with pytest.raises(db.NameTaken):
        catalog.create_user(name="ALICE", name_key="alice", role=auth.VIEWER)
    assert catalog.count_users() == 1, "被拒的那次不能留下半条记录"


def test_rename_keeps_both_columns_in_step(catalog):
    uid = catalog.create_user(name="Alice", name_key="alice", role=auth.VIEWER)
    catalog.rename_user(uid, name="Bob", name_key="bob")
    assert catalog.get_user_by_name_key("bob")["name"] == "Bob"
    assert catalog.get_user_by_name_key("alice") is None

    catalog.create_user(name="Carol", name_key="carol", role=auth.VIEWER)
    with pytest.raises(db.NameTaken):
        catalog.rename_user(uid, name="Carol", name_key="carol")


def test_password_and_salt_must_be_set_together(catalog):
    """中间崩一次就会留下"新盐配旧散列"，而那种状态的表现是"口令莫名不对了"，
    没有任何日志指向原因。"""
    uid = catalog.create_user(name="a", name_key="a", role=auth.VIEWER)
    with pytest.raises(ValueError):
        catalog.set_user_password(uid, b"hash", None)
    with pytest.raises(ValueError):
        catalog.set_user_password(uid, None, b"salt")


def test_deleting_a_user_cascades_sessions_and_grants(a, catalog, photo_ids):
    """⚠️ 这是"删用户不可撤销"的证据：授权一起消失，重建同名账号也拿不回来。"""
    uid = a.create_user(name="小明", role=auth.VIEWER)
    token, _ = a.login("小明", None)
    catalog.replace_grants(uid, photo_ids[:2])

    catalog.delete_user(uid)
    assert catalog.get_user(uid) is None
    assert catalog.get_session(auth._sha256(token)) is None
    assert catalog.granted_photo_ids(uid) == []
    # 照片本身不能被连带删掉 —— 级联的方向只有"用户 → 他的授权"。
    assert catalog.count_photos() == len(photo_ids)


def test_delete_sessions_of_user(a, catalog):
    a.create_user(name="小明", role=auth.VIEWER)
    t1, _ = a.login("小明", None)
    t2, _ = a.login("小明", None)
    uid = catalog.get_user_by_name_key("小明")["id"]
    assert catalog.delete_sessions_of_user(uid) == 2
    assert a.principal_of(t1) is None and a.principal_of(t2) is None


def test_replace_grants_is_a_whole_set_swap(catalog, photo_ids):
    uid = catalog.create_user(name="小明", name_key="小明", role=auth.VIEWER)
    catalog.replace_grants(uid, photo_ids)
    assert catalog.granted_photo_ids(uid) == sorted(photo_ids)

    catalog.replace_grants(uid, [photo_ids[1]])
    assert catalog.granted_photo_ids(uid) == [photo_ids[1]]
    assert not catalog.is_granted(uid, photo_ids[0])

    catalog.replace_grants(uid, [])
    assert catalog.granted_photo_ids(uid) == []


def test_replace_grants_is_idempotent(catalog, photo_ids):
    uid = catalog.create_user(name="小明", name_key="小明", role=auth.VIEWER)
    catalog.replace_grants(uid, photo_ids)
    catalog.replace_grants(uid, photo_ids)
    assert catalog.granted_photo_ids(uid) == sorted(photo_ids)


def test_granting_an_unknown_photo_fails_the_whole_batch(catalog, photo_ids):
    """宁可整批失败，也不要"勾了 10 张成功 7 张"却没人知道是哪 3 张没成。"""
    uid = catalog.create_user(name="小明", name_key="小明", role=auth.VIEWER)
    catalog.replace_grants(uid, [photo_ids[0]])
    with pytest.raises(sqlite3.IntegrityError):
        catalog.replace_grants(uid, [photo_ids[1], "f" * 32])
    assert catalog.granted_photo_ids(uid) == [photo_ids[0]], "失败要整体回滚到原样"


def test_is_granted_ignores_role_and_grant_all(catalog, photo_ids):
    """`is_granted` 只回答"授权表里有没有这一行"。混进 role/grant_all 的后果是
    管理台想显示"这个人被单独勾了哪几张"时拿到一片全选。"""
    uid = catalog.create_user(
        name="root", name_key="root", role=auth.ADMIN, grant_all=True
    )
    assert not catalog.is_granted(uid, photo_ids[0])


# ---- Catalog：按 user 过滤照片 ----


def test_photo_queries_can_be_scoped_to_a_user(catalog, photo_ids):
    uid = catalog.create_user(name="小明", name_key="小明", role=auth.VIEWER)
    catalog.replace_grants(uid, [photo_ids[2], photo_ids[0]])

    assert [p["id"] for p in catalog.list_photos(user_id=uid)] == [
        photo_ids[0], photo_ids[2],
    ], "过滤后仍按 created_at, id 排序"
    assert catalog.count_photos(user_id=uid) == 2
    assert catalog.get_photo(photo_ids[0], user_id=uid) is not None
    assert catalog.get_photo(photo_ids[1], user_id=uid) is None, (
        "没授权的照片要与「不存在」同一个返回值，让调用方自然回 404 而不是 403"
    )


def test_unscoped_photo_queries_are_unchanged(catalog, photo_ids):
    """`user_id=None` 必须与 v1 的行为完全一致 —— 现有调用点一行没改。"""
    assert [p["id"] for p in catalog.list_photos()] == photo_ids
    assert catalog.count_photos() == 3
    assert catalog.get_photo(photo_ids[1]) is not None


def test_user_with_no_grants_sees_nothing(catalog, photo_ids):
    uid = catalog.create_user(name="客人", name_key="客人", role=auth.VIEWER)
    assert catalog.list_photos(user_id=uid) == []
    assert catalog.count_photos(user_id=uid) == 0


def test_scoped_rows_carry_only_photo_columns(catalog, photo_ids):
    """JOIN 之后必须 `SELECT photo.*`。写成 `SELECT *` 会把 photo_grant 的
    user_id/photo_id 一起带进 dict，而 `photo_id` 这个键看着就像 photo 自己的
    列 —— 调用方拿它当 id 用，在不过滤的路径上却又不存在。"""
    uid = catalog.create_user(name="小明", name_key="小明", role=auth.VIEWER)
    catalog.replace_grants(uid, [photo_ids[0]])
    scoped = catalog.list_photos(user_id=uid)[0]
    assert set(scoped) == set(catalog.list_photos()[0])


# ---- photo 的 v2 两列 ----


def test_fit_mode_defaults_to_null_and_can_be_set(catalog, photo_ids):
    assert catalog.get_photo(photo_ids[0])["fit_mode"] is None
    catalog.set_photo_fit_mode(photo_ids[0], db.FIT_FIT)
    assert catalog.get_photo(photo_ids[0])["fit_mode"] == db.FIT_FIT
    catalog.set_photo_fit_mode(photo_ids[0], None)
    assert catalog.get_photo(photo_ids[0])["fit_mode"] is None


def test_illegal_fit_mode_is_rejected(catalog, photo_ids):
    with pytest.raises(ValueError):
        catalog.set_photo_fit_mode(photo_ids[0], "stretch")


def test_backend_column_is_recorded_at_insert(catalog):
    asset_id = catalog.upsert_asset(
        nas_path="/nas/photos/x.jpg", kind="image", sha256="a" * 64,
        bytes_=1, mtime=1, width_px=10, height_px=10,
    )
    pid = "e" * 32
    catalog.insert_photo(
        photo_id=pid, ref_asset_id=asset_id, video_asset_id=None,
        playable_asset_id=None, title=None, print_width_m=0.1, quality_score=80,
        imgdb_path="/x", imgdb_bytes=1, thumb_path="/t", self_score=1,
        backend="xfeat", fit_mode=db.FIT_FIT,
    )
    row = catalog.get_photo(pid)
    assert row["backend"] == "xfeat" and row["fit_mode"] == db.FIT_FIT
