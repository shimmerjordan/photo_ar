"""asset 引用完整性校验。spec §6.1。

核心前提：`asset` 记的是 NAS 上**他人也会动**的文件（用户自己、CloudDrive2、
其他 App）。所以每次解析前先校验，且校验必须便宜 —— 先比 `mtime` + `bytes`，
只在不一致时才算 sha256（上万张照片每次都哈希是不可接受的）。

**不做自动修复或路径追踪**（spec §6.1 明确要求）。文件被移动了就是失效，由用户
在界面上重新指定。猜测用户意图的自动重绑定风险更大：把"外婆生日"的视频重绑到
一个碰巧同名同大小的文件上，用户在家人面前才会发现。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Catalog

# 校验状态。
#   ok               mtime+bytes 一致，未做哈希
#   restored         文件回来了（上次标了 missing，这次读到且指纹一致）
#   mtime_only       只有 mtime 变，内容没变（如 rsync / 网盘挂载点重挂）
#   content_changed  内容被换了。若是参考图，该 photo 的特征已失效
#   missing          文件不在了
STATUS_OK = "ok"
STATUS_RESTORED = "restored"
STATUS_MTIME_ONLY = "mtime_only"
STATUS_CONTENT_CHANGED = "content_changed"
STATUS_MISSING = "missing"

_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class VerifyResult:
    asset_id: str
    status: str
    nas_path: str
    hashed: bool
    stale_photo_ids: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status != STATUS_MISSING


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def stat_fingerprint(path: str | Path) -> tuple[int, int]:
    """(bytes, mtime_ms)。全服务统一用毫秒整数存 mtime。

    统一在这里做转换，是因为 `st_mtime` 是浮点秒，各处自己乘 1000 再取整会
    在边界上得出差 1 的结果，让"mtime 变了"这个判断随机地假成立 —— 每次假
    成立都要多算一次全文件 sha256。
    """
    st = Path(path).stat()
    return int(st.st_size), int(st.st_mtime * 1000)


def verify_asset(catalog: Catalog, asset: dict[str, Any]) -> VerifyResult:
    asset_id = str(asset["id"])
    nas_path = str(asset["nas_path"])
    try:
        size, mtime = stat_fingerprint(nas_path)
    except OSError:
        if not asset["missing"]:
            catalog.update_asset_fingerprint(asset_id, missing=1)
        return VerifyResult(asset_id, STATUS_MISSING, nas_path, hashed=False)

    if size == int(asset["bytes"]) and mtime == int(asset["mtime"]):
        if asset["missing"]:
            catalog.update_asset_fingerprint(asset_id, missing=0)
            return VerifyResult(asset_id, STATUS_RESTORED, nas_path, hashed=False)
        catalog.update_asset_fingerprint(asset_id)  # 只刷 checked_at
        return VerifyResult(asset_id, STATUS_OK, nas_path, hashed=False)

    digest = sha256_file(nas_path)
    if digest == str(asset["sha256"]):
        catalog.update_asset_fingerprint(
            asset_id, bytes_=size, mtime=mtime, missing=0
        )
        return VerifyResult(asset_id, STATUS_MTIME_ONLY, nas_path, hashed=True)

    catalog.update_asset_fingerprint(
        asset_id, sha256=digest, bytes_=size, mtime=mtime, missing=0
    )
    # 内容被换了。参考图变化意味着已入库的 ORB 特征描述的是另一张图，必须标记
    # 需重新入库（spec §13：扫描时仍尝试命中，但提示特征可能已过期）。视频变化
    # 只需更新指纹 —— 播的是文件本身，没有派生产物失效。
    stale: list[str] = []
    for photo in catalog.photos_referencing_asset(asset_id):
        if photo["ref_asset_id"] == asset_id:
            catalog.set_photo_ref_stale(str(photo["id"]), True)
            stale.append(str(photo["id"]))
    return VerifyResult(
        asset_id,
        STATUS_CONTENT_CHANGED,
        nas_path,
        hashed=True,
        stale_photo_ids=tuple(stale),
    )


def verify_all(catalog: Catalog) -> list[VerifyResult]:
    """全量校验（spec §6.1 的"每周一次"任务）。由 `photoar-server verify` 触发。"""
    return [verify_asset(catalog, a) for a in catalog.list_assets()]
