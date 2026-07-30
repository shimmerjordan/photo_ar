"""入库流水线。spec §7 的 `POST /v1/photo`。

顺序（每一步失败都必须在写库之前就失败，所以贵的检查放前面、写库放最后）：

  1. 路径解析（白名单）→ 参考图必须是图片、必须能解开
  2. 同一张参考图已入库 → 409，不重复建条目
  3. `arcoreimg eval-img` 质量分 < 75 → **拒绝**（spec §7/§13），返回分数与建议
  4. 提 ORB 特征（零特征 → 拒绝）
  5. 算自匹配分（20 张扰动查询图，`dedup.self_score`）
  6. **近重复闸门**（`library.conflicts`）→ 有冲突 → 409，列出冲突照片
  7. `arcoreimg build-db` 产出单目标 `.imgdb`、生成 640px 兜底缩略图
  8. 视频：探测 → 需要则转码（产物落服务自有目录，不污染用户视频目录）
  9. 写 catalog → 追加进识别库（重建倒排索引）

第 6 步是 Phase 0 的第一条硬结论，不是可选的健壮性措施：`asset.nas_path UNIQUE`
只挡得住同一路径重复入库，内容哈希只挡得住字节完全相同的重复。**重新编码或
裁切过的近似重复会两份都入库，然后在识别时互相触发 ratio test 判 ambiguous，
两份都永久漏检** —— 而用户看到的现象是"识别器坏了"，无从追查。判据的两次
返工过程见 `photoar.dedup` 的模块 docstring。

第 3 步与第 5、6 步的顺序是刻意的：质量分是一次 `arcoreimg` 调用（快），自匹配分
要 20 次 ORB 提取 + RANSAC（约 1 秒），近重复闸门还要对 50 个候选各做两次
RANSAC。先用便宜的检查把明显不合格的照片挡掉。
"""

import time
from dataclasses import dataclass
from pathlib import Path

import cv2

from .. import dedup, quality, synth, transcode
from ..features import extract
from . import fsbrowser
from .config import ServerConfig
from .db import Catalog, new_id
from .integrity import sha256_file, stat_fingerprint
from .library import Conflict, PhotoLibrary


class IngestRejected(Exception):
    """入库被拒绝。`code` 决定 HTTP 状态码，`detail` 原样进响应体。

    所有拒绝都必须给出**用户能据此行动**的信息（spec §13：质量分不足要"返回
    分数与建议"）。只回 400 "bad request" 等于让用户猜。
    """

    def __init__(self, status: int, code: str, message: str, **detail) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class IngestResult:
    photo_id: str
    quality_score: int
    self_score: int
    imgdb_bytes: int
    print_width_m: float
    ref_asset_id: str
    video_asset_id: str | None
    playable_asset_id: str | None
    transcoded: bool
    elapsed_ms: int


def _upsert_file_asset(
    catalog: Catalog, path: Path, kind: str, *, probe_video: bool, cfg: ServerConfig
) -> str:
    size, mtime = stat_fingerprint(path)
    width = height = duration = None
    if kind == "image":
        img = fsbrowser.decode_for_thumb(path, "image")
        height, width = int(img.shape[0]), int(img.shape[1])
    elif probe_video:
        try:
            info = transcode.probe(path, ffprobe=cfg.ffprobe)
            width, height, duration = info.width, info.height, info.duration_ms
        except Exception:
            # 探测失败不该阻断入库：宽高/时长只是给客户端的提示信息，
            # 播放本身不依赖它们。缺了就是缺了，不要编一个。
            pass
    return catalog.upsert_asset(
        nas_path=str(path),
        kind=kind,
        sha256=sha256_file(path),
        bytes_=size,
        mtime=mtime,
        width_px=width,
        height_px=height,
        duration_ms=duration,
    )


def ingest_photo(
    *,
    cfg: ServerConfig,
    catalog: Catalog,
    library: PhotoLibrary,
    ref_path: Path,
    video_path: Path | None,
    print_width_m: float,
    title: str | None,
) -> IngestResult:
    t0 = time.perf_counter()

    if not ref_path.is_file():
        raise IngestRejected(404, "ref_not_found", f"参考图不存在：{ref_path}")
    if fsbrowser.kind_of(ref_path) != "image":
        raise IngestRejected(
            415, "ref_not_image", f"参考图不是支持的图片格式：{ref_path.suffix}"
        )
    if not print_width_m > 0:
        raise IngestRejected(
            400, "bad_print_width", f"打印宽度必须为正数（米），收到 {print_width_m!r}"
        )

    existing = catalog.get_asset_by_path(str(ref_path))
    if existing is not None:
        photo = catalog.get_photo_by_ref_asset(str(existing["id"]))
        if photo is not None:
            raise IngestRejected(
                409,
                "already_ingested",
                "这张参考图已经入库了",
                photoId=str(photo["id"]),
            )

    try:
        img = fsbrowser.decode_for_thumb(ref_path, "image")
    except fsbrowser.ThumbFailed as exc:
        raise IngestRejected(415, "ref_undecodable", str(exc)) from exc

    # --- 质量分（便宜，先做）---
    try:
        score = quality.eval_img(ref_path, arcoreimg=cfg.arcoreimg)
    except quality.ArcoreimgMissing as exc:
        raise IngestRejected(503, "arcoreimg_missing", str(exc)) from exc
    if score < quality.MIN_QUALITY_SCORE:
        raise IngestRejected(
            422,
            "quality_too_low",
            f"这张照片的 AR 跟踪质量分只有 {score}，低于 "
            f"{quality.MIN_QUALITY_SCORE}，跟踪会明显抖动",
            score=score,
            minScore=quality.MIN_QUALITY_SCORE,
            suggestion=(
                "换一张纹理更丰富的照片；大片天空、纯色背景、过曝或严重模糊的"
                "照片都拿不到高分。也可以给照片加一圈细纹理边框再打印。"
            ),
        )

    features = extract(img)
    if len(features) == 0:
        raise IngestRejected(
            422, "no_features", "这张照片提不出任何 ORB 特征，无法识别"
        )

    # --- 自匹配分 + 近重复闸门（贵，放在质量分之后）---
    samples = synth.generate(img, cfg.self_score_samples, seed=0)
    self_score = dedup.self_score(features, [extract(q) for q, _ in samples])

    known = {
        str(p["id"]): int(p["self_score"])
        for p in catalog.list_photos()
    }
    conflicts: list[Conflict] = library.conflicts(features, self_score, known)
    if conflicts:
        raise IngestRejected(
            409,
            "near_duplicate",
            "库里已经有和这张几乎相同的照片。两张都入库会让它们互相判"
            "ambiguous，结果是**两张都永久识别不出来**。",
            selfScore=self_score,
            conflicts=[
                {
                    "photoId": c.photo_id,
                    "inliers": c.inliers,
                    "selfScore": c.self_score,
                    "title": (catalog.get_photo(c.photo_id) or {}).get("title"),
                }
                for c in conflicts
            ],
        )

    photo_id = new_id()

    # --- 生成物 ---
    imgdb_path = cfg.imgdb_dir / f"{photo_id}.imgdb"
    try:
        imgdb_bytes = quality.build_single_target_db(
            ref_path,
            name=photo_id,
            print_width_m=print_width_m,
            out_path=imgdb_path,
            arcoreimg=cfg.arcoreimg,
        )
    except quality.InvalidListingField as exc:
        # 路径里有字面 '|' 或换行。这不是服务端缺陷，用户改文件名即可。
        raise IngestRejected(422, "bad_ref_path", str(exc)) from exc

    thumb_path = cfg.thumb_dir / f"{photo_id}.jpg"
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(
        fsbrowser.encode_thumb(img, fsbrowser.REF_THUMB_LONG_EDGE)
    )

    ref_asset_id = _upsert_file_asset(
        catalog, ref_path, "image", probe_video=False, cfg=cfg
    )

    # --- 视频 ---
    video_asset_id = playable_asset_id = None
    transcoded = False
    if video_path is not None:
        if not video_path.is_file():
            raise IngestRejected(404, "video_not_found", f"视频不存在：{video_path}")
        if fsbrowser.kind_of(video_path) != "video":
            raise IngestRejected(
                415,
                "video_not_video",
                f"关联的文件不是支持的视频格式：{video_path.suffix}",
            )
        video_asset_id = _upsert_file_asset(
            catalog, video_path, "video", probe_video=True, cfg=cfg
        )
        playable_asset_id = video_asset_id
        try:
            info = transcode.probe(video_path, ffprobe=cfg.ffprobe)
        except transcode.FfmpegMissing as exc:
            raise IngestRejected(503, "ffmpeg_missing", str(exc)) from exc
        except Exception as exc:
            raise IngestRejected(
                422, "video_unprobeable", f"探测不了这个视频：{exc}"
            ) from exc
        if transcode.needs_transcode(info):
            out = cfg.playable_dir / f"{photo_id}.mp4"
            try:
                transcode.transcode(
                    video_path, out, ffmpeg=cfg.ffmpeg,
                    encoder=cfg.video_encoder, preset=cfg.video_preset,
                    vaapi_device=cfg.vaapi_device,
                )
            except Exception as exc:
                raise IngestRejected(
                    422, "transcode_failed", f"转码失败：{exc}"
                ) from exc
            transcoded = True
            playable_asset_id = _upsert_file_asset(
                catalog, out, "video", probe_video=True, cfg=cfg
            )

    # --- 写库（放最后：前面任何一步失败都不留半条记录）---
    catalog.insert_photo(
        photo_id=photo_id,
        ref_asset_id=ref_asset_id,
        video_asset_id=video_asset_id,
        playable_asset_id=playable_asset_id,
        title=title,
        print_width_m=print_width_m,
        quality_score=score,
        imgdb_path=str(imgdb_path),
        imgdb_bytes=imgdb_bytes,
        thumb_path=str(thumb_path),
        self_score=self_score,
    )
    # catalog 先写、library 后写是刻意的：反过来一旦 catalog 写失败，识别库里
    # 就有一个查得到却在 catalog 里不存在的 photo_id，识别命中后所有取流接口
    # 都 404，而且没有任何地方会报出这个不一致。现在这个顺序下失败的形态是
    # "catalog 里有、识别不到"，`Server.check_consistency()` 启动时会报出来，
    # `reindex` 能修。
    library.add(photo_id, features)

    return IngestResult(
        photo_id=photo_id,
        quality_score=score,
        self_score=self_score,
        imgdb_bytes=imgdb_bytes,
        print_width_m=print_width_m,
        ref_asset_id=ref_asset_id,
        video_asset_id=video_asset_id,
        playable_asset_id=playable_asset_id,
        transcoded=transcoded,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


def attach_video(
    *,
    cfg: ServerConfig,
    catalog: Catalog,
    photo_id: str,
    video_path: Path,
) -> tuple[str, str, bool]:
    """给已入库的照片（重新）关联视频。返回 (video_asset_id, playable_asset_id, 是否转码)。

    spec §6.1/§13 的"重新指定"入口：文件被用户移动后 asset.missing=1，界面标红，
    用户在 App 里挑一个新文件走这条路。不做自动路径追踪。
    """
    if catalog.get_photo(photo_id) is None:
        raise IngestRejected(404, "photo_not_found", f"照片不存在：{photo_id}")
    if not video_path.is_file():
        raise IngestRejected(404, "video_not_found", f"视频不存在：{video_path}")
    if fsbrowser.kind_of(video_path) != "video":
        raise IngestRejected(
            415, "video_not_video", f"不是支持的视频格式：{video_path.suffix}"
        )

    video_asset_id = _upsert_file_asset(
        catalog, video_path, "video", probe_video=True, cfg=cfg
    )
    playable_asset_id = video_asset_id
    transcoded = False
    info = transcode.probe(video_path, ffprobe=cfg.ffprobe)
    if transcode.needs_transcode(info):
        out = cfg.playable_dir / f"{photo_id}.mp4"
        transcode.transcode(
            video_path, out, ffmpeg=cfg.ffmpeg,
            encoder=cfg.video_encoder, preset=cfg.video_preset,
            vaapi_device=cfg.vaapi_device,
        )
        transcoded = True
        playable_asset_id = _upsert_file_asset(
            catalog, out, "video", probe_video=True, cfg=cfg
        )
    catalog.set_photo_video(
        photo_id,
        video_asset_id=video_asset_id,
        playable_asset_id=playable_asset_id,
    )
    return video_asset_id, playable_asset_id, transcoded


def decode_frame(data: bytes):
    """解 `/v1/recognize` 的 JPEG 帧。解不开返回 None（400，不是 500）。"""
    import numpy as np

    buf = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)
