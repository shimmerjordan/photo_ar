"""v1 → v2 的原地迁移。

这套测试存在的唯一理由：家里那台 NAS 上已经躺着一个入过库的 catalog.db。每张
照片入库都跑过 arcoreimg 与 ffmpeg（几十秒一张），"升级请删库重建"不是一个能说
出口的方案。所以要钉住的不是"新库能建出来"（那个平凡），而是：

1. 只有 v1 三张表的库文件打开后自动补齐 v2 的表与列，**已有数据一行不丢**；
2. 同一个文件反复打开不会因为重复 ALTER 而炸（`ADD COLUMN` 没有 IF NOT EXISTS）；
3. user_version 说自己已经是 2、但列其实没加上（上一次升级写完表却在 commit 前
   断电）时，仍然会把缺的补齐 —— 迁移不由版本号驱动，见 `Catalog._migrate`。
"""

import sqlite3

import pytest

from photoar.server import db

# v1 的 DDL，**照抄当时的样子冻在这里**。
#
# 不 import `db._DDL` 来造这个库：那样测的就变成"当前 DDL 建出来的库能被当前
# 代码打开"，而这条永远成立。迁移测试要的是一份真正来自旧版本的库文件，所以这
# 段字符串必须与 db.py 解耦，哪怕它现在看起来和 `_DDL` 一模一样。
_V1_DDL = """
CREATE TABLE asset (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  nas_path    TEXT NOT NULL UNIQUE,
  sha256      TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  mtime       INTEGER NOT NULL,
  width_px    INTEGER,
  height_px   INTEGER,
  duration_ms INTEGER,
  missing     INTEGER NOT NULL DEFAULT 0,
  checked_at  INTEGER,
  created_at  INTEGER NOT NULL
);

CREATE TABLE photo (
  id                TEXT PRIMARY KEY,
  ref_asset_id      TEXT NOT NULL REFERENCES asset(id),
  video_asset_id    TEXT REFERENCES asset(id),
  playable_asset_id TEXT REFERENCES asset(id),
  title             TEXT,
  print_width_m     REAL NOT NULL,
  quality_score     INTEGER NOT NULL,
  imgdb_path        TEXT NOT NULL,
  imgdb_bytes       INTEGER NOT NULL,
  thumb_path        TEXT NOT NULL,
  self_score        INTEGER NOT NULL,
  ref_stale         INTEGER NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL
);

CREATE TABLE recognize_log (
  ts         INTEGER NOT NULL,
  photo_id   TEXT,
  inliers    INTEGER,
  latency_ms INTEGER,
  via        TEXT,
  topk_json  TEXT
);

CREATE INDEX idx_asset_path ON asset(nas_path);
CREATE INDEX idx_photo_ref  ON photo(ref_asset_id);
CREATE INDEX idx_log_ts     ON recognize_log(ts);
"""

V1_PHOTO_ID = "a" * 32
V1_ASSET_ID = "b" * 32


@pytest.fixture
def v1_db(tmp_path):
    """造一个 v1 的库文件，里面有一条 asset 和一条引用它的 photo。"""
    path = tmp_path / "catalog.db"
    conn = sqlite3.connect(path)
    conn.executescript(_V1_DDL)
    conn.execute(
        "INSERT INTO asset (id, kind, nas_path, sha256, bytes, mtime, width_px,"
        " height_px, duration_ms, missing, checked_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,NULL,0,?,?)",
        (V1_ASSET_ID, "image", "/nas/photos/old.jpg", "f" * 64, 1234, 111, 1200, 800, 222, 333),
    )
    conn.execute(
        "INSERT INTO photo (id, ref_asset_id, video_asset_id, playable_asset_id,"
        " title, print_width_m, quality_score, imgdb_path, imgdb_bytes, thumb_path,"
        " self_score, ref_stale, created_at, updated_at)"
        " VALUES (?,?,NULL,NULL,?,?,?,?,?,?,?,0,?,?)",
        (
            V1_PHOTO_ID, V1_ASSET_ID, "爷爷的照片", 0.152, 88,
            "/data/imgdb/a.imgdb", 4300, "/data/thumb/a.jpg", 61, 444, 555,
        ),
    )
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    return path


def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _user_version(path):
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_v1_database_upgrades_in_place(v1_db):
    """打开一个 v1 的库：一路补齐到**当前**版本，老数据原样在。

    这里刻意不写死版本号（原来是 `== 2`）。写死的话每次加一版 schema 都要来改一个字面量，
    而那个动作本身不验证任何东西 —— 真正要钉的是「打开一个老库之后，当前版本该有的表和
    列都在，而且 user_version 被推到了当前值」。所以下面按 `db.SCHEMA_VERSION` 断言，
    并逐版列出该出现的东西。
    """
    cat = db.Catalog(v1_db)
    try:
        # v2 的四张表 + photo 的两列
        assert _tables(v1_db) >= {"user", "session", "photo_grant", "app_config"}
        assert set(_columns(v1_db, "photo")) >= {"fit_mode", "backend"}
        # v3 的素材挂载点表
        assert "mount" in _tables(v1_db)
        assert _user_version(v1_db) == db.SCHEMA_VERSION

        photo = cat.get_photo(V1_PHOTO_ID)
        assert photo is not None
        assert photo["title"] == "爷爷的照片"
        assert photo["quality_score"] == 88
        assert photo["self_score"] == 61
        assert photo["created_at"] == 444
        # 新列对老行是 NULL，而 NULL 在这两列上都有明确含义（跟随全局配置 / ORB），
        # 不是"待回填"。回填一遍 UPDATE 只是把同一个事实写第二遍。
        assert photo["fit_mode"] is None
        assert photo["backend"] is None

        asset = cat.get_asset(V1_ASSET_ID)
        assert asset is not None and asset["nas_path"] == "/nas/photos/old.jpg"
        assert asset["sha256"] == "f" * 64
    finally:
        cat.close()


def test_upgraded_database_accepts_v2_writes(v1_db):
    """升上来的库能立刻用 v2 的功能：建用户、给老照片授权、写热配置。

    只验"表建出来了"是不够的 —— `photo_grant.photo_id` 有外键指向 photo(id)，
    而这条外键是在 photo 表已经存在的情况下由 ALTER 之外的路径建的。真正要确认
    的是"老照片能被新授权表引用"。
    """
    cat = db.Catalog(v1_db)
    try:
        uid = cat.create_user(name="小明", name_key="小明", role="viewer")
        cat.replace_grants(uid, [V1_PHOTO_ID])
        assert cat.granted_photo_ids(uid) == [V1_PHOTO_ID]
        assert cat.is_granted(uid, V1_PHOTO_ID)
        assert [p["id"] for p in cat.list_photos(user_id=uid)] == [V1_PHOTO_ID]

        cat.put_app_config({"recog.top_k": "25"})
        assert cat.all_app_config() == {"recog.top_k": "25"}
    finally:
        cat.close()


def test_reopening_does_not_alter_twice(v1_db):
    """反复打开必须幂等。`ALTER TABLE ... ADD COLUMN` 没有 IF NOT EXISTS，
    第二次执行会抛 `duplicate column name` —— 也就是说"升级过一次的库从此打不开"。"""
    for _ in range(3):
        cat = db.Catalog(v1_db)
        cat.close()
    assert _columns(v1_db, "photo").count("fit_mode") == 1


def test_columns_are_added_even_if_version_already_says_v2(v1_db):
    """版本号说 2、列却没加上时仍然补齐。

    这个状态是真会出现的：上一次升级里 executescript 建完表、ALTER 加完列、
    `PRAGMA user_version=2` 也执行了，但进程在 commit 之前被 kill —— 或者更简单，
    有人手工改过 user_version。按"if version < 2 才迁移"写的迁移会直接跳过，留下
    一个缺列的库，而缺列的表现是每次读 photo 都 KeyError。
    """
    conn = sqlite3.connect(v1_db)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()

    cat = db.Catalog(v1_db)
    try:
        assert set(_columns(v1_db, "photo")) >= {"fit_mode", "backend"}
        assert "user" in _tables(v1_db)
    finally:
        cat.close()


def test_fresh_database_has_the_same_shape_as_an_upgraded_one(tmp_path, v1_db):
    """新建的库与升上来的库结构必须一致。

    photo 的两列只在 `_PHOTO_V2_COLUMNS` 里声明一次（CREATE TABLE 里刻意没有），
    这条断言就是在钉"新库也确实走了 ALTER 那条路"。如果哪天有人"顺手"把两列补进
    CREATE TABLE 而 ALTER 那段被删掉，本测试仍然通过 —— 但那时
    test_v1_database_upgrades_in_place 会红，两条一起才是完整的约束。
    """
    fresh = db.Catalog(tmp_path / "fresh.db")
    fresh.close()
    upgraded = db.Catalog(v1_db)
    upgraded.close()
    assert _columns(tmp_path / "fresh.db", "photo") == _columns(v1_db, "photo")
    assert _tables(tmp_path / "fresh.db") == _tables(v1_db)


def test_schema_from_the_future_refuses_to_open(tmp_path):
    """版本高于本程序 → 拒绝启动。降级运行会用旧代码往新表里写，静默写坏数据。"""
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(db.SchemaTooNew):
        db.Catalog(path)
