"""SQLite catalog。表结构来自 spec §6，偏离之处在下方逐条说明。

并发模型：`ThreadingHTTPServer` 每个请求一个线程，而 sqlite3 的连接默认不
跨线程。所以连接放 `threading.local`，写操作用一把进程内锁串行化。这不是
性能妥协 —— 家庭自用没有并发写压力（spec §17 选 SQLite 的理由），锁的作用
是把 `SQLITE_BUSY` 从"偶发 500"变成"根本不会发生"。同时开 WAL，让识别路径
上的读不被入库的写阻塞（入库要跑 ffmpeg，可能持锁几十秒）。

对 spec §6 DDL 的三处增补（spec 要求这些状态但没给它们位置）：

- `photo.ref_stale`：§6.1 与 §13 都要求"参考图 sha256 变化 → 标记该 photo
  需重新入库，扫描时仍尝试命中但提示特征可能已过期"。DDL 里没有能记这个
  状态的列，只能新增一列，否则那条错误处理无法实现。
- `photo.self_score`：增量去重判据的分子。Phase 0 的结论是"近重复判据不是
  绝对内点数阈值，而是 min(自匹配分) < RATIO × 互查内点数"（见
  `photoar.dedup` 模块 docstring）。自匹配分要跑 20 次扰动查询才能得到，
  入库时算一次存下来，否则每次新照片入库都要为库里每一张现算一遍。
- `photo.thumb_path`：`/v1/photo/{id}/thumb` 的产物路径。与 `imgdb_path`
  同类（都是服务自有数据目录下的生成物），DDL 给了后者却漏了前者。

`refAspect`（§7 的 recognize 响应字段）**没有**新增列：它等于参考图 asset 的
`width_px / height_px`，两处存同一个事实必然会分叉。

---

schema v2（多用户 / 会话 / 按用户授权 / 热配置）在 `_DDL_V2` 里，设计取舍见那段
注释。这里只记一条全局性的：v1 的库文件必须能**原地**升上来。家里那台 NAS 上
已经躺着一个入过库的 catalog.db，"重建库"意味着重新跑一遍 arcoreimg 与转码
（每张照片几十秒），所以"删库重来"从来不是一个可选项。
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS asset (
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

-- photo 还有两列 fit_mode / backend，**故意**不写在这里，见 _PHOTO_V2_COLUMNS。
CREATE TABLE IF NOT EXISTS photo (
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

CREATE TABLE IF NOT EXISTS recognize_log (
  ts         INTEGER NOT NULL,
  photo_id   TEXT,
  inliers    INTEGER,
  latency_ms INTEGER,
  via        TEXT,
  topk_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_asset_path ON asset(nas_path);
CREATE INDEX IF NOT EXISTS idx_photo_ref  ON photo(ref_asset_id);
CREATE INDEX IF NOT EXISTS idx_log_ts     ON recognize_log(ts);
"""

# ---- schema v2 ----
#
# 关于 `user.name` 与 `user.name_key` 为什么是**两列**：
#
# 登录只输名字（viewer 路径），所以"李四"和" 李四 "、"Alice"和"alice"必须是同一个
# 人，否则家里人会因为多打了一个空格而登录成一个新账号。唯一性因此必须建在**规范化
# 后**的值上（见 auth.normalize_name：压空白 + casefold）。
#
# 三种做法，选了第三种：
#   1. `name` 直接存规范化值 —— 一列搞定，但显示名从此是 "alice"，用户自己输的
#      "Alice" 永久丢失。这是把数据模型的方便建立在用户可见的退化上。
#   2. `name` 存原样 + UNIQUE 建在表达式 `lower(trim(name))` 上 —— SQLite 支持
#      表达式唯一索引，但它只有 ASCII 的 lower()（没链 ICU），且没有任何办法表达
#      "内部连续空白压成一个"。于是 SQLite 认为唯一的两行，Python 的 normalize_name
#      会认为是同一个人：唯一键与登录查找用的键**语义不同**，是一个只在非 ASCII
#      名字上才暴露的静默 bug。
#   3. `name` 存原样（只用于显示），`name_key` 存 normalize_name 的输出并建唯一索引
#      —— 多一列且必须两处同时写（所以所有写入都只走 Catalog.create_user /
#      rename_user 这两个入口，别处不许 UPDATE name）。换来的是唯一键、登录查找键
#      与 Python 的规范化规则**永远是同一个函数算出来的**。
#
# `name TEXT NOT NULL UNIQUE` 保留原样（本表 DDL 来自需求）：它对唯一性没有额外
# 作用（原样相同 → name_key 必然相同 → 先撞 name_key 的唯一索引），留着只是让
# "名字不能重复"这件事在 DDL 里看得见。
#
# ⚠️ `session` 与 `photo_grant` 都是 `ON DELETE CASCADE`（`PRAGMA foreign_keys=ON`
# 在 `_conn()` 里已开，所以级联是真的会发生）。这意味着 **delete_user 不可撤销**：
# 删一个用户会连他的全部逐张授权一起消失，重建账号后必须重新一张张勾。管理台上
# "删除用户"与"停用用户"（disabled=1）是两件不同的事，不要把前者当后者用。
_DDL_V2 = """
CREATE TABLE IF NOT EXISTS user (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  name_key    TEXT NOT NULL,
  role        TEXT NOT NULL,
  pwd_hash    BLOB,
  pwd_salt    BLOB,
  grant_all   INTEGER NOT NULL DEFAULT 0,
  disabled    INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL,
  last_seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS session (
  token_sha256 BLOB PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  created_at   INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS photo_grant (
  user_id  TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
  photo_id TEXT NOT NULL REFERENCES photo(id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, photo_id)
);

CREATE TABLE IF NOT EXISTS app_config (
  key        TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_name_key ON user(name_key);
CREATE INDEX IF NOT EXISTS idx_grant_user      ON photo_grant(user_id);
CREATE INDEX IF NOT EXISTS idx_session_expires ON session(expires_at);
"""

# v2 给 photo 加的两列。**唯一的声明处** —— 上面 `_DDL` 的 CREATE TABLE 里刻意
# 没有它们。
#
# 直觉上应该两边都写（新库由 CREATE TABLE 带出来、老库由 ALTER 补上），但那样
# ALTER 这条路径就只在"别人机器上那个老库文件"里跑过，本地开发和 412 个测试全
# 部走 CREATE TABLE 分支 —— 迁移代码于是变成一段永远不被执行的代码，等真的拿去
# 升级线上库时才第一次运行。
#
# 只声明一次的代价是读 `_DDL` 看不到 photo 的完整形状（所以那里留了一行指路
# 注释），换来的是：每次打开任何库文件都走一遍 ALTER 补列逻辑，包括全部测试。
_PHOTO_V2_COLUMNS = (
    # 视频怎么贴进照片区域。NULL = 跟随全局热配置（appconfig 的 video.fit_mode），
    # 不是 "未设置所以出错"：绝大多数照片不需要单独指定，逐张存一个值只会让"改
    # 全局默认"变成"改全局默认 + 批量刷全表"。
    ("fit_mode", "TEXT"),
    # 这张照片的特征是哪个识别后端提的（photoar.backend 的 'orb' / 'xfeat'）。
    # NULL 视为 'orb'：v1 时期入库的照片全都是 ORB，回填一遍 UPDATE 只是把同一个
    # 事实写两遍，而"NULL 就是 orb"这条规则反正得在读取侧写一次。
    ("backend", "TEXT"),
)

# fit_mode 的取值。放在 db.py 是因为它是列的值域；appconfig 从这里 import 同一份
# 常量，好过两个模块各写一遍字符串字面量。
FIT_FILL = "fill"  # 居中裁切填满照片区域
FIT_FIT = "fit"  # 完整放入，留边
FIT_MODES = (FIT_FILL, FIT_FIT)


def effective_fit_mode(photo: dict[str, Any], default: str) -> str:
    """这张照片实际生效的贴合方式。`photo.fit_mode` 为 NULL = 跟随全局默认。

    **只有这一份实现**，两个调用点（`/v1/recognize` 与 `/v1/photo/*` 走的
    `app._fit_mode_of`、整库 manifest 走的 `targets.py`）都调它。抄第二份的后果
    很具体：两条路对同一张照片给出不同的 fitMode，而客户端在离线命中与在线命中
    时用的是两条不同的路 —— 表现是"同一张照片有时候视频铺满、有时候留边"，
    而两边的代码各自看起来都对。

    用 `in FIT_MODES` 而不是判真假：那一列被手工改成一个不认识的字符串时，回退到
    全局默认（一个一定合法的值）好过把它原样发给客户端 —— 客户端拿到一个不认识的
    fitMode 只能自己猜一个，而两端猜得不一样时画面对不上。
    """
    mode = photo.get("fit_mode")
    if mode in FIT_MODES:
        return str(mode)
    return default


def ref_aspect(width_px: Any, height_px: Any) -> float | None:
    """参考图的宽高比，缺尺寸时 None。

    同样只此一份：`round(..., 6)` 这个精度是客户端拿去算贴图矩形的输入，两个接口
    给出精度不同的同名字段时，差异小到不会有人怀疑，但视频边缘会差出几个像素。
    """
    if not width_px or not height_px:
        return None
    return round(float(width_px) / float(height_px), 6)


class SchemaTooNew(RuntimeError):
    """库文件的 schema 版本高于本程序。降级运行会静默写坏数据，必须拒绝启动。"""


class NameTaken(ValueError):
    """用户名（规范化后）已存在。

    单独一个异常类型，而不是把 `sqlite3.IntegrityError` 漏给上层：名字重复是
    管理台上最常见的一次手滑，上层要把它变成一句"这个名字已经有人用了"的 400。
    让 HTTP 层去 `except sqlite3.IntegrityError` 意味着它得知道我们用的是
    SQLite，还得靠匹配错误文案来区分"名字撞了"和"外键坏了"。
    """


def new_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


class Catalog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._migrate()

    # ---- 连接管理 ----

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        """建表 / 原地升级。全部步骤都必须幂等 —— 每次打开库都会跑一遍。

        为什么不写"版本号 → 迁移脚本"那种阶梯（if version < 2: ...）：
        这里每一步本身就是幂等的（`CREATE TABLE IF NOT EXISTS` 与"先查
        PRAGMA table_info 再决定要不要 ALTER"），无条件执行与按版本号跳转的
        结果完全一样，但少了一类 bug —— 版本号因为任何原因（手工改过、上一次
        升级写完表却在 commit 前崩掉）与实际表结构不一致时，阶梯式迁移会跳过
        本该补的表，而这里会把缺的补齐。user_version 于是退化成"给
        SchemaTooNew 用的一个上界"，不再是迁移的驱动变量。
        """
        with self._write_lock:
            conn = self._conn()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise SchemaTooNew(
                    f"{self._path} 的 schema 版本是 {version}，本程序只认到 "
                    f"{SCHEMA_VERSION}。用新版程序打开，不要降级运行。"
                )
            conn.executescript(_DDL)
            # v2 的表引用了 photo(id)，必须排在 _DDL 之后。
            conn.executescript(_DDL_V2)
            self._add_missing_columns(conn, "photo", _PHOTO_V2_COLUMNS)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

    @staticmethod
    def _add_missing_columns(
        conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
    ) -> None:
        """按需 ALTER 补列。

        `ALTER TABLE ... ADD COLUMN` 没有 IF NOT EXISTS，重复执行会直接抛
        `duplicate column name`，所以必须先 `PRAGMA table_info` 查一遍现状。
        "捕获 OperationalError 然后忽略"看着更短，但那样会把真正的失败（比如
        表根本不存在、磁盘只读）一起吞掉。

        新列一律可空、无 DEFAULT：这样 ALTER 不需要重写已有行（SQLite 只改
        schema 元数据，O(1)），几万行的库也是瞬间完成；而"NULL 表示什么"这件事
        每一列都在 `_PHOTO_V2_COLUMNS` 里写明了。
        """
        have = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---- asset ----

    def upsert_asset(
        self,
        *,
        nas_path: str,
        kind: str,
        sha256: str,
        bytes_: int,
        mtime: int,
        width_px: int | None = None,
        height_px: int | None = None,
        duration_ms: int | None = None,
    ) -> str:
        """按 `nas_path` 复用已有 asset（引用不复制 → 同一个文件只有一条记录）。

        复用时刷新指纹与尺寸：文件可能已经被换过内容，这次入库看到的是新的
        真相。同时把 `missing` 清零 —— 文件又能读到了，上次标红应该撤掉。
        """
        if kind not in ("image", "video"):
            raise ValueError(f"asset.kind 只能是 image / video，收到 {kind!r}")
        ts = now_ms()
        with self._write_lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT id FROM asset WHERE nas_path = ?", (nas_path,)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE asset SET kind=?, sha256=?, bytes=?, mtime=?, width_px=?,"
                    " height_px=?, duration_ms=?, missing=0, checked_at=? WHERE id=?",
                    (
                        kind, sha256, bytes_, mtime, width_px, height_px,
                        duration_ms, ts, row["id"],
                    ),
                )
                conn.commit()
                return str(row["id"])
            asset_id = new_id()
            conn.execute(
                "INSERT INTO asset (id, kind, nas_path, sha256, bytes, mtime,"
                " width_px, height_px, duration_ms, missing, checked_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    asset_id, kind, nas_path, sha256, bytes_, mtime,
                    width_px, height_px, duration_ms, ts, ts,
                ),
            )
            conn.commit()
            return asset_id

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM asset WHERE id = ?", (asset_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_asset_by_path(self, nas_path: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM asset WHERE nas_path = ?", (nas_path,)
        ).fetchone()
        return dict(row) if row else None

    def list_assets(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn().execute("SELECT * FROM asset ORDER BY created_at, id")
        ]

    def update_asset_fingerprint(
        self,
        asset_id: str,
        *,
        sha256: str | None = None,
        bytes_: int | None = None,
        mtime: int | None = None,
        missing: int | None = None,
    ) -> None:
        sets, args = ["checked_at = ?"], [now_ms()]
        for col, val in (
            ("sha256", sha256), ("bytes", bytes_),
            ("mtime", mtime), ("missing", missing),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        args.append(asset_id)
        with self._write_lock:
            conn = self._conn()
            conn.execute(f"UPDATE asset SET {', '.join(sets)} WHERE id = ?", args)
            conn.commit()

    # ---- photo ----

    def insert_photo(
        self,
        *,
        photo_id: str,
        ref_asset_id: str,
        video_asset_id: str | None,
        playable_asset_id: str | None,
        title: str | None,
        print_width_m: float,
        quality_score: int,
        imgdb_path: str,
        imgdb_bytes: int,
        thumb_path: str,
        self_score: int,
        fit_mode: str | None = None,
        backend: str | None = None,
    ) -> str:
        """`fit_mode=None` 表示跟随全局配置，`backend=None` 表示 ORB（见
        `_PHOTO_V2_COLUMNS`）。两个都给默认值是为了让 v1 时期的调用点一行不改
        就继续成立 —— 它们当时的行为正好就是这两个 None 的含义。"""
        if fit_mode is not None and fit_mode not in FIT_MODES:
            raise ValueError(f"photo.fit_mode 只能是 {FIT_MODES}，收到 {fit_mode!r}")
        ts = now_ms()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO photo (id, ref_asset_id, video_asset_id,"
                " playable_asset_id, title, print_width_m, quality_score,"
                " imgdb_path, imgdb_bytes, thumb_path, self_score, ref_stale,"
                " created_at, updated_at, fit_mode, backend)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (
                    photo_id, ref_asset_id, video_asset_id, playable_asset_id,
                    title, float(print_width_m), int(quality_score), imgdb_path,
                    int(imgdb_bytes), thumb_path, int(self_score), ts, ts,
                    fit_mode, backend,
                ),
            )
            conn.commit()
        return photo_id

    # 下面三个查询都能按 user 过滤。`user_id=None` 表示**不过滤**，与 v1 的行为
    # 逐字节相同。
    #
    # 为什么 db 层不直接认 Principal：db.py 不该 import auth（auth 要 import db，
    # 会成环）。而"admin 或 grant_all 的人不过滤"这条策略只有一个正确写法，所以
    # 它以 `auth.photo_filter(principal)` 的形式**只写一次**，HTTP 层调它拿到
    # user_id 或 None 再传进来。db 层只负责"给了 user_id 就 JOIN 授权表"。
    #
    # 过滤用 INNER JOIN 而不是 `id IN (SELECT ...)`：两者语义相同（授权表的主键是
    # (user_id, photo_id)，一个 user 对一张照片最多一行，所以 JOIN 不会放大行数），
    # 而 JOIN 形式直接吃 `idx_grant_user`，用不着依赖优化器把 IN 子查询改写成半连接。
    @staticmethod
    def _grant_scope(user_id: str | None) -> tuple[str, str, tuple[Any, ...]]:
        if user_id is None:
            return "", "1", ()
        return (
            " JOIN photo_grant ON photo_grant.photo_id = photo.id",
            "photo_grant.user_id = ?",
            (user_id,),
        )

    def get_photo(self, photo_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        """给了 `user_id` 而这张没授权给他时返回 None —— 与"照片不存在"同一个
        返回值，调用方于是自然回 404 而不是 403。这是刻意的：403 会告诉一个没
        授权的人"这张照片确实存在"，而照片标题本身就可能是隐私。"""
        join, cond, args = self._grant_scope(user_id)
        row = self._conn().execute(
            f"SELECT photo.* FROM photo{join} WHERE {cond} AND photo.id = ?",
            (*args, photo_id),
        ).fetchone()
        return dict(row) if row else None

    def get_photo_by_ref_asset(self, ref_asset_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM photo WHERE ref_asset_id = ?", (ref_asset_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_photos(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        join, cond, args = self._grant_scope(user_id)
        return [
            dict(r)
            for r in self._conn().execute(
                f"SELECT photo.* FROM photo{join} WHERE {cond}"
                " ORDER BY photo.created_at, photo.id",
                args,
            )
        ]

    def list_photo_targets(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        """建整库 `.imgdb` 与它的 manifest 需要的**全部**信息，一次查询取完。

        `photo.*` 加上参考图 asset 的 `nas_path` / `sha256` / `missing` / 宽高
        （前缀 `ref_`）。

        为什么是一次 JOIN 而不是"`list_photos()` 再逐张 `get_asset()`"：后者是
        N+1 次查询，而这里的 N 上限是 1000（ARCore 的库容量上限），且这条路会被
        `/v1/ping` 调到 —— ping 的契约是"极轻"。1000 次 SQLite 往返在本地磁盘上
        是几十毫秒，在 QNAP 的机械盘上不好说，而这个代价完全是白付的。
        （`list_photos` 那条路仍然是 N+1，那是既有行为，不在这次改动范围内。）

        `ORDER BY created_at DESC, id DESC`：超出容量上限时要留下"最新的 N 张"
        （理由见 `targets.TargetStore._plan`）。第二个排序键不是装饰 —— 批量入库
        时同一毫秒里可以进好几张，只按 created_at 排的话这几张的相对顺序由 SQLite
        决定，于是"截断后留下哪些"在两次调用之间可能不一样，而版本号是那个集合的
        哈希：结果是版本号在没有任何东西改变的情况下来回跳，每跳一次全体客户端
        重下一遍整库。
        """
        join, cond, args = self._grant_scope(user_id)
        return [
            dict(r)
            for r in self._conn().execute(
                "SELECT photo.*,"
                " asset.nas_path AS ref_path,"
                " asset.sha256   AS ref_sha256,"
                " asset.missing  AS ref_missing,"
                " asset.width_px AS ref_width_px,"
                " asset.height_px AS ref_height_px"
                " FROM photo JOIN asset ON asset.id = photo.ref_asset_id"
                f"{join} WHERE {cond}"
                " ORDER BY photo.created_at DESC, photo.id DESC",
                args,
            )
        ]

    def count_photos(self, *, user_id: str | None = None) -> int:
        join, cond, args = self._grant_scope(user_id)
        return int(
            self._conn().execute(
                f"SELECT COUNT(*) FROM photo{join} WHERE {cond}", args
            ).fetchone()[0]
        )

    def set_photo_fit_mode(self, photo_id: str, fit_mode: str | None) -> None:
        """`None` 是把这张恢复成"跟随全局配置"，不是"清空成非法值"。"""
        if fit_mode is not None and fit_mode not in FIT_MODES:
            raise ValueError(f"photo.fit_mode 只能是 {FIT_MODES}，收到 {fit_mode!r}")
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE photo SET fit_mode = ?, updated_at = ? WHERE id = ?",
                (fit_mode, now_ms(), photo_id),
            )
            conn.commit()

    def set_photo_ref_stale(self, photo_id: str, stale: bool) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE photo SET ref_stale = ?, updated_at = ? WHERE id = ?",
                (1 if stale else 0, now_ms(), photo_id),
            )
            conn.commit()

    def set_photo_video(
        self,
        photo_id: str,
        *,
        video_asset_id: str | None,
        playable_asset_id: str | None,
    ) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE photo SET video_asset_id = ?, playable_asset_id = ?,"
                " updated_at = ? WHERE id = ?",
                (video_asset_id, playable_asset_id, now_ms(), photo_id),
            )
            conn.commit()

    def photos_referencing_asset(self, asset_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM photo WHERE ref_asset_id = ? OR video_asset_id = ?"
            " OR playable_asset_id = ?",
            (asset_id, asset_id, asset_id),
        )
        return [dict(r) for r in rows]

    # ---- recognize_log ----

    def log_recognize(
        self,
        *,
        photo_id: str | None,
        inliers: int | None,
        latency_ms: int,
        via: str | None,
        topk: list[tuple[str, int]] | None = None,
    ) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO recognize_log (ts, photo_id, inliers, latency_ms, via,"
                " topk_json) VALUES (?,?,?,?,?,?)",
                (
                    now_ms(), photo_id, inliers, int(latency_ms), via,
                    json.dumps(topk, ensure_ascii=False) if topk is not None else None,
                ),
            )
            conn.commit()

    def recent_logs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM recognize_log ORDER BY ts DESC LIMIT ?", (int(limit),)
        )
        return [dict(r) for r in rows]

    # ---- user ----
    #
    # 这一段所有带时间的方法都能显式传 `now`。db 层别处（upsert_asset 等）都是直接
    # 调 `now_ms()`，这里之所以不一样：会话的过期、"最近活跃"这些是**可测的业务
    # 语义**，测"过期 session 会被拒"不该靠 time.sleep 去等真实时间流过去。
    # `now=None` 保持"就用现在"的老行为，所以不传的调用点感觉不到差别。

    def create_user(
        self,
        *,
        name: str,
        name_key: str,
        role: str,
        pwd_hash: bytes | None = None,
        pwd_salt: bytes | None = None,
        grant_all: bool = False,
        disabled: bool = False,
        user_id: str | None = None,
        now: int | None = None,
    ) -> str:
        """`name` 存原样（显示用），`name_key` 存规范化值（唯一键，见 `_DDL_V2`）。

        两个参数都要求调用方给，而不是在这里 import auth.normalize_name 自己算
        —— db.py 不能依赖 auth.py（那边要 import 这边）。代价是"忘了规范化"这种
        错误 db 层拦不住，所以用户创建只有 auth/HTTP 一条入口。
        """
        ts = now_ms() if now is None else int(now)
        uid = user_id or new_id()
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO user (id, name, name_key, role, pwd_hash, pwd_salt,"
                    " grant_all, disabled, created_at, last_seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        uid, name, name_key, role, pwd_hash, pwd_salt,
                        1 if grant_all else 0, 1 if disabled else 0, ts,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise NameTaken(f"用户名已存在：{name!r}") from exc
            conn.commit()
        return uid

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM user WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_name_key(self, name_key: str) -> dict[str, Any] | None:
        """按**规范化后**的名字查。方法名里带 `name_key` 是刻意的：叫
        `get_user_by_name` 的话，调用方几乎必然会把用户原样输入直接传进来，
        而那样查不到任何多打了一个空格 / 大小写不同的账号，表现为"明明有这个
        人却说查不到"。"""
        row = self._conn().execute(
            "SELECT * FROM user WHERE name_key = ?", (name_key,)
        ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn().execute("SELECT * FROM user ORDER BY created_at, id")
        ]

    def count_users(self, role: str | None = None) -> int:
        if role is None:
            return int(self._conn().execute("SELECT COUNT(*) FROM user").fetchone()[0])
        return int(
            self._conn().execute(
                "SELECT COUNT(*) FROM user WHERE role = ?", (role,)
            ).fetchone()[0]
        )

    def update_user(
        self,
        user_id: str,
        *,
        role: str | None = None,
        grant_all: bool | None = None,
        disabled: bool | None = None,
    ) -> None:
        """`None` = 这一项不改。口令不在这里改，见 `set_user_password`。

        口令分开是因为它需要区分"不改"和"清空成 NULL"（把 admin 降级成 viewer
        时就要清），而 `None` 在这个签名里已经被"不改"占用了。再引入一个哨兵
        对象只为了一个字段，不如让"改口令"这件事有自己的名字。
        """
        sets: list[str] = []
        args: list[Any] = []
        for col, val in (("role", role), ("grant_all", grant_all), ("disabled", disabled)):
            if val is None:
                continue
            sets.append(f"{col} = ?")
            args.append(int(val) if isinstance(val, bool) else val)
        if not sets:
            return
        args.append(user_id)
        with self._write_lock:
            conn = self._conn()
            conn.execute(f"UPDATE user SET {', '.join(sets)} WHERE id = ?", args)
            conn.commit()

    def set_user_password(
        self, user_id: str, pwd_hash: bytes | None, pwd_salt: bytes | None
    ) -> None:
        """两个都传 None 就是清掉口令（回到"只输名字就能进"）。

        散列与盐一起写，绝不分两次 UPDATE：中间崩一次就会留下"新盐配旧散列"，
        而那种状态的表现是"口令莫名其妙不对了"，且没有任何日志能指向原因。
        """
        if (pwd_hash is None) != (pwd_salt is None):
            raise ValueError("pwd_hash 与 pwd_salt 必须同时有值或同时为 None")
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE user SET pwd_hash = ?, pwd_salt = ? WHERE id = ?",
                (pwd_hash, pwd_salt, user_id),
            )
            conn.commit()

    def rename_user(self, user_id: str, *, name: str, name_key: str) -> None:
        """改名必须同时改 `name_key` —— 这也是"改名只走这个方法"的全部理由。"""
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE user SET name = ?, name_key = ? WHERE id = ?",
                    (name, name_key, user_id),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise NameTaken(f"用户名已存在：{name!r}") from exc
            conn.commit()

    def touch_user_seen(self, user_id: str, now: int | None = None) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE user SET last_seen_at = ? WHERE id = ?",
                (now_ms() if now is None else int(now), user_id),
            )
            conn.commit()

    def delete_user(self, user_id: str) -> None:
        """⚠️ 连带删掉这个人的全部 session 与全部逐张授权（外键 CASCADE）。

        不可撤销：重建同名账号后拿到的是一个新的 user.id，之前一张张勾出来的
        授权不会回来。"临时不让某人登录"要用 `update_user(disabled=True)`。
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute("DELETE FROM user WHERE id = ?", (user_id,))
            conn.commit()

    # ---- session ----

    def create_session(
        self,
        *,
        token_sha256: bytes,
        user_id: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        """只存 token 的 sha256，明文 token 只在响应里回给客户端一次。

        `last_used_at` 初始化成 `created_at` 而不是 0/NULL：它的用途是"这个会话
        多久没动过"，刚建出来的会话"最后活跃"就是现在。设成 0 会让任何按
        last_used_at 做空闲回收的逻辑一上线就把全部新会话判成僵尸。

        用裸 INSERT 而不是 `INSERT OR REPLACE`：主键撞了意味着两个 token 的
        sha256 相同，那要么是随机源坏了、要么是有人在做碰撞攻击。OR REPLACE 会
        把这件事变成"静默顶掉另一个人的会话"，裸 INSERT 会抛 IntegrityError。
        """
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO session (token_sha256, user_id, created_at,"
                " expires_at, last_used_at) VALUES (?,?,?,?,?)",
                (token_sha256, user_id, int(created_at), int(expires_at), int(created_at)),
            )
            conn.commit()

    def get_session(self, token_sha256: bytes) -> dict[str, Any] | None:
        """一次查回 session + 它的 user（LEFT JOIN 不需要：外键保证 user 一定在）。

        用户各列加 `u_` 前缀，因为两张表都有 `created_at` —— 不加前缀的话
        `dict(row)` 里后一个会静默盖掉前一个，而"会话创建时间"和"用户创建时间"
        长得一模一样，看日志时根本发现不了取错了。
        """
        row = self._conn().execute(
            "SELECT s.token_sha256 AS token_sha256, s.user_id AS user_id,"
            " s.created_at AS created_at, s.expires_at AS expires_at,"
            " s.last_used_at AS last_used_at,"
            " u.name AS u_name, u.name_key AS u_name_key, u.role AS u_role,"
            " u.grant_all AS u_grant_all, u.disabled AS u_disabled,"
            " u.created_at AS u_created_at, u.last_seen_at AS u_last_seen_at"
            " FROM session s JOIN user u ON u.id = s.user_id"
            " WHERE s.token_sha256 = ?",
            (token_sha256,),
        ).fetchone()
        return dict(row) if row else None

    def touch_session(self, token_sha256: bytes, now: int) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE session SET last_used_at = ? WHERE token_sha256 = ?",
                (int(now), token_sha256),
            )
            conn.commit()

    def delete_session(self, token_sha256: bytes) -> None:
        with self._write_lock:
            conn = self._conn()
            conn.execute("DELETE FROM session WHERE token_sha256 = ?", (token_sha256,))
            conn.commit()

    def delete_sessions_of_user(self, user_id: str) -> int:
        """改口令 / 停用账号后把该用户的会话全踢掉。返回踢掉几个。

        不做这一步的话，"停用某人"只挡住了他下次登录，他手机上那个还没过期的
        session（viewer 的 TTL 是按天算的）能继续看照片 —— 而管理台上已经显示
        "已停用"了。
        """
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM session WHERE user_id = ?", (user_id,))
            conn.commit()
            return int(cur.rowcount or 0)

    def purge_expired_sessions(self, now: int | None = None) -> int:
        ts = now_ms() if now is None else int(now)
        with self._write_lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM session WHERE expires_at <= ?", (ts,))
            conn.commit()
            return int(cur.rowcount or 0)

    # ---- photo_grant ----

    def granted_photo_ids(self, user_id: str) -> list[str]:
        """只回 id，不回整行：管理台的"勾选框"和授权判定要的都只是 id 集合，
        回整行会让调用方顺手拿着一份可能过期的 photo 快照到处传。"""
        return [
            str(r["photo_id"])
            for r in self._conn().execute(
                "SELECT photo_id FROM photo_grant WHERE user_id = ? ORDER BY photo_id",
                (user_id,),
            )
        ]

    def is_granted(self, user_id: str, photo_id: str) -> bool:
        """注意：这里**不看** role 与 grant_all，只看授权表里有没有这一行。

        "admin 与 grant_all 的人看全部"是上层策略（auth.photo_filter），不是这张
        表的事实。把两者混在一个函数里的后果是：管理台想显示"这个人被单独勾了
        哪几张"时，拿到的是一片全选。
        """
        row = self._conn().execute(
            "SELECT 1 FROM photo_grant WHERE user_id = ? AND photo_id = ?",
            (user_id, photo_id),
        ).fetchone()
        return row is not None

    def replace_grants(self, user_id: str, photo_ids: list[str] | tuple[str, ...]) -> None:
        """整体替换某人的授权集合（管理台勾选框提交的语义就是"这就是全集"）。

        先 DELETE 再批量 INSERT，同一个事务里做完：分成"取差集→逐条加/逐条删"
        看着更省 IO，但那需要先读一遍再写，中间任何一次并发修改都会让结果既不是
        旧集合也不是新集合。这里照片数量是家庭规模（几百张），全删全插的代价可以
        忽略，换来的是"提交完 = 库里就是勾选框里的样子"这个不需要推理的保证。

        不存在的 photo_id 会被外键挡下并整体回滚 —— 宁可整批失败，也不要"勾了
        10 张成功 7 张"却没人知道是哪 3 张没成。
        """
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM photo_grant WHERE user_id = ?", (user_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO photo_grant (user_id, photo_id) VALUES (?,?)",
                    [(user_id, pid) for pid in photo_ids],
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                raise
            conn.commit()

    # ---- app_config ----

    def all_app_config(self) -> dict[str, str]:
        """回**未解析**的 JSON 文本，解析交给 appconfig。

        db 层不 json.loads 是因为"这一行的 JSON 坏了怎么办"只有 appconfig 答得上
        （答案是回退到代码里的默认值、并且不要连带把整个服务拖死）。在这里
        loads 就只能抛，于是一行坏数据 = 服务起不来。
        """
        return {
            str(r["key"]): str(r["value_json"])
            for r in self._conn().execute("SELECT key, value_json FROM app_config")
        }

    def put_app_config(self, items: dict[str, str], now: int | None = None) -> None:
        """一批一起写，一个事务。管理台一次提交多个字段，半套生效比全不生效更糟。"""
        ts = now_ms() if now is None else int(now)
        with self._write_lock:
            conn = self._conn()
            conn.executemany(
                "INSERT INTO app_config (key, value_json, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,"
                " updated_at = excluded.updated_at",
                [(k, v, ts) for k, v in items.items()],
            )
            conn.commit()
