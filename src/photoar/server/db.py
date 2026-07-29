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
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

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


class SchemaTooNew(RuntimeError):
    """库文件的 schema 版本高于本程序。降级运行会静默写坏数据，必须拒绝启动。"""


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
        with self._write_lock:
            conn = self._conn()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise SchemaTooNew(
                    f"{self._path} 的 schema 版本是 {version}，本程序只认到 "
                    f"{SCHEMA_VERSION}。用新版程序打开，不要降级运行。"
                )
            conn.executescript(_DDL)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

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
    ) -> str:
        ts = now_ms()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO photo (id, ref_asset_id, video_asset_id,"
                " playable_asset_id, title, print_width_m, quality_score,"
                " imgdb_path, imgdb_bytes, thumb_path, self_score, ref_stale,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (
                    photo_id, ref_asset_id, video_asset_id, playable_asset_id,
                    title, float(print_width_m), int(quality_score), imgdb_path,
                    int(imgdb_bytes), thumb_path, int(self_score), ts, ts,
                ),
            )
            conn.commit()
        return photo_id

    def get_photo(self, photo_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM photo WHERE id = ?", (photo_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_photo_by_ref_asset(self, ref_asset_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM photo WHERE ref_asset_id = ?", (ref_asset_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_photos(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn().execute("SELECT * FROM photo ORDER BY created_at, id")
        ]

    def count_photos(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM photo").fetchone()[0])

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
