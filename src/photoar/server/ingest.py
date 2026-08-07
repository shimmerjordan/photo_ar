"""入库流水线。spec §7 的 `POST /v1/photo`。

顺序（每一步失败都必须在写库之前就失败，所以贵的检查放前面、写库放最后）：

  1. 路径解析（白名单）→ 参考图必须是图片、必须能解开
  2. 同一张参考图已入库 → 409，不重复建条目
  3. 提 ORB 特征（零特征 → 拒绝）
  4. 算自匹配分（20 张扰动查询图，`dedup.self_score`）
  5. **近重复闸门**（`library.conflicts`）→ 有冲突 → 409，列出冲突照片
  6. 生成 640px 缩略图
  7. 视频：探测 → 需要则转码（产物落服务自有目录，不污染用户视频目录）
  8. 写 catalog → 追加进识别库（重建倒排索引）

第 5 步是 Phase 0 的第一条硬结论，不是可选的健壮性措施：`asset.nas_path UNIQUE`
只挡得住同一路径重复入库，内容哈希只挡得住字节完全相同的重复。**重新编码或
裁切过的近似重复会两份都入库，然后在识别时互相触发 ratio test 判 ambiguous，
两份都永久漏检** —— 而用户看到的现象是"识别器坏了"，无从追查。判据的两次
返工过程见 `photoar.dedup` 的模块 docstring。

第 5 步有一个能在管理台上关掉的开关（`appconfig` 的 `ingest.dedup_gate`），由
HTTP 层从热配置读出来传进 `ingest_photo`。**默认必须是开的**，关掉不是"宽松
一点"，是让两张照片双双永久扫不出来（见参数注释）。

⚠️ 这条流水线上**曾经**还有 arcoreimg 的两步：`eval-img` 质量分闸门（< 75 拒）
和 `build-db` 产出 `.imgdb`。整条删掉了（2026-08-07，decisions §46）：两个消费方
都是已下线的安卓客户端（质量分预测的是 ARCore 的跟踪质量、`.imgdb` 是 ARCore 的
目标库），而那个闭源二进制因授权不入 git、发布镜像里必然没有 —— 它把每一台按文档
部署的机器的入库都挡在 503 上。网页版识别（ORB 库 + 浏览器光流）从头到尾不碰它们；
"太糊认不出"仍有防线：零特征拒绝 + 自匹配分 + 近重复闸门。

阈值一律作为参数传进来、不在这里读 `appconfig`：本模块也被 CLI 的批量入库路径
调（那条路径没有 AppConfig 实例），而且"谁决定阈值"只该有一处 —— 让入库自己去
读配置的话，同一次入库的阈值和 HTTP 层刚校验过的那个值可能来自两次不同的缓存快照。
"""

import time
from dataclasses import dataclass
from pathlib import Path

import cv2

from .. import dedup, synth, transcode
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
    self_score: int
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
    # ⚠️ 关掉它的后果是 Phase 0 的第一条硬结论，不是"宽松一点"：两张近重复照片都
    # 入库之后，识别时它们会互相触发 ratio 检验判 ambiguous，**两张都永久扫不
    # 出来**，而用户看到的现象是"识别器坏了"，无从追查（见模块 docstring 第 5 步
    # 与 `library.conflicts`）。只在"我确定这两张不是同一张"或"临时排查闸门本身
    # 是不是误拦"时关，关完记得开回来。
    dedup_gate: bool = True,
    # 自匹配分的合成分辨率。理由与实测数字见 `synth.SYNTH_LONG_EDGE`（一句话版：
    # 不限的话一张 12MP 手机照片要 111 秒，而 97% 的像素在下一步提特征时就被扔掉）。
    # 由 HTTP 层从热配置 `ingest.synth_long_edge` 传进来。
    synth_long_edge: int = synth.SYNTH_LONG_EDGE,
    # 视频怎么贴进照片区域。None = 这一列留 NULL，读取侧回退到全局默认
    # （见 `db._PHOTO_V2_COLUMNS`）。HTTP 层会把当时的全局默认显式传进来，理由
    # 写在 `app.Server._create_photo` 里。
    fit_mode: str | None = None,
) -> IngestResult:
    t0 = time.perf_counter()

    if not ref_path.is_file():
        raise IngestRejected(404, "ref_not_found", f"参考图不存在：{ref_path}")
    if fsbrowser.kind_of(ref_path) != "image":
        raise IngestRejected(
            415, "ref_not_image", f"参考图不是支持的图片格式：{ref_path.suffix}"
        )
    # 0 = 物理宽度未知，合法（交给 ARCore 自己量，理由见 `app._create_photo`）。
    # 负数仍然拒：它不是"未知"，是算错了或单位搞反了，静默当未知处理会把一个真实
    # 的 bug 藏起来。
    if print_width_m < 0:
        raise IngestRejected(
            400,
            "bad_print_width",
            f"打印宽度不能是负数（米），收到 {print_width_m!r}。未知就给 0。",
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

    # 提特征、配对、算自匹配分都必须走**库自己那个后端**，不能用模块级的
    # `features.extract`（那是 ORB）。用错的后果不是"稍微不准"：XFeat 的库 slot 是
    # 512×64 float32，把 300×32 的 uint8 描述子塞进去会在
    # `descstore.encode_slot` 的最后一步抛一句 numpy 广播错误 —— 而那句错误里没有
    # 任何字提到"后端"，排查时会先怀疑照片、再怀疑 opencv。
    backend = library.backend
    features = backend.extract(img)
    if len(features) == 0:
        raise IngestRejected(
            422,
            "no_features",
            f"这张照片提不出任何 {backend.name} 特征，无法识别",
        )

    # --- 自匹配分 + 近重复闸门（这条流水线上最贵的一步）---
    #
    # 自匹配分**无论闸门开关都要算**，虽然它是这条流水线上最贵的一步（20 次 ORB
    # 提取 + RANSAC，约 1 秒）：它是**别人**入库时的分母（`library.conflicts` 的
    # `known_self_scores` 从 catalog 取这一列）。跟着闸门一起跳过的话，这一行会
    # 写进一个 0 分，而 conflicts 的判据是 `min(s_new, s_exist) < ratio * m` ——
    # 0 恒小于任何值，于是以后每一张与它沾点关系的新照片都会被判成冲突。
    # 也就是说"关掉去重闸门"会变成"以后入库全被拦住"，而拦人的是一个已经关掉的开关。
    samples = synth.generate(
        img, cfg.self_score_samples, seed=0, long_edge=synth_long_edge
    )
    self_score = dedup.self_score(
        features,
        # 这里的合成样本是"查询"语义，但**故意不用 `backend.extract_query`**
        # （查询侧提 4000 个特征，入库侧 300）。自匹配分是去重判据
        # `min(s_new, s_exist) < ratio * m` 的分子，而 `s_exist` 是老照片入库时
        # 按 300 特征算出来的、存在 catalog 里的历史值。这一边换成 4000 会让新照片
        # 的分数系统性地高出一截，两个量纲一比 —— 闸门整体失准，且不报错、不留日志。
        # 要改的话得连着把全库的 self_score 重算一遍，那是一次数据迁移，不是改一行。
        [backend.extract(q) for q, _ in samples],
        # 配对函数也得跟着后端换：ORB 是 Hamming + crossCheck，XFeat 是余弦互近邻。
        # 拿 `verify_pair` 去比 float32 描述子，cv2 的 BFMatcher(NORM_HAMMING) 会
        # 直接抛（dtype 不对），但那是运气好 —— 真正要防的是自匹配分被算成一个
        # **另一个量纲**的数字，因为它是去重判据 `min(s_new,s_exist) < ratio*m`
        # 的分子，量纲错了闸门就整体失准，而不会报错。
        verify_fn=backend.verify,
    )

    if dedup_gate:
        known = {
            str(p["id"]): int(p["self_score"])
            for p in catalog.list_photos()
        }
        # `query_features` 让 `m` 按识别时的口径量（4000 特征 / 1280px）。不传的话
        # 闸门用入库口径的 300 特征去量交叉分，实测同一对近重复图 63 vs 123 ——
        # 低一半，正好让一次真实的重复入库漏了网。理由全文见 `library.conflicts`。
        conflicts: list[Conflict] = library.conflicts(
            features, self_score, known, query_features=backend.extract_query(img)
        )
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
        thumb_path=str(thumb_path),
        self_score=self_score,
        fit_mode=fit_mode,
    )
    # catalog 先写、library 后写是刻意的：反过来一旦 catalog 写失败，识别库里
    # 就有一个查得到却在 catalog 里不存在的 photo_id，识别命中后所有取流接口
    # 都 404，而且没有任何地方会报出这个不一致。现在这个顺序下失败的形态是
    # "catalog 里有、识别不到"，`Server.check_consistency()` 启动时会报出来，
    # `reindex` 能修。
    library.add(photo_id, features)

    return IngestResult(
        photo_id=photo_id,
        self_score=self_score,
        print_width_m=print_width_m,
        ref_asset_id=ref_asset_id,
        video_asset_id=video_asset_id,
        playable_asset_id=playable_asset_id,
        transcoded=transcoded,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )


@dataclass(frozen=True)
class ReplaceRefResult:
    photo_id: str
    self_score: int
    ref_asset_id: str
    slot: int
    elapsed_ms: int


def replace_ref(
    *,
    cfg: ServerConfig,
    catalog: Catalog,
    library: PhotoLibrary,
    photo_id: str,
    ref_path: Path,
    dedup_gate: bool = True,
    synth_long_edge: int = synth.SYNTH_LONG_EDGE,
) -> ReplaceRefResult:
    """换掉一张已入库照片的**参考图**，photo_id 不变。

    ## 为什么需要它，以及为什么不是「删掉重建」

    真实场景：先拿手机拍的一张糊照片入了库，后来有了扫描件/原图，想换上去。或者
    打印件重印了一版，颜色和裁切都不一样。

    「删掉重建」要付两笔代价：**授权全丢**（`photo_grant.photo_id` 是
    `ON DELETE CASCADE`，重建之后得一张张重新勾），以及删除本身要把识别库里后面
    所有 slot 往前挪 —— 而那条路径出错的症状是「照片 A 的描述子挂在照片 B 的 id
    上」，识别命中后播的是别人的视频，没有任何一步会报错。

    换参考图不用碰这些：slot 原地替换（`PhotoLibrary.replace`），photo_id、授权、
    配的视频、标题、打印宽度、贴合模式全部留着。

    ## 顺序与失败形态

    和 `ingest_photo` 同一个原则：所有可能失败的重活（特征、自匹配分、去重、
    缩略图）都在写库之前做完。写库分两步 —— catalog 先、library 后，与
    `ingest_photo` 一致，理由也一样（反过来失败会留下一个「识别得到但 catalog
    里没有」的 photo_id，而那不会被任何检查报出来）。

    这里 catalog 先写还有一个额外的好处：`library.replace` 失败时，库里还是旧特征，
    而 catalog 指向新的缩略图 —— 那是一个 `check_consistency` 之外的软不一致
    （识别仍然按旧图工作，只是管理台上的缩略图换了），比反过来温和。

    ## 去重要排除自己

    近重复闸门必须把**这张照片自己**排除掉。不排的话，用一张只做了轻微调整的新图去
    换（这正是最常见的用法：同一张照片重新扫一遍），必然与自己的旧特征判成近重复，
    于是这个接口对最主要的场景恒定失败。
    """
    t0 = time.perf_counter()
    if catalog.get_photo(photo_id) is None:
        raise IngestRejected(404, "photo_not_found", f"照片不存在：{photo_id}")
    if photo_id not in set(library.photo_ids()):
        # catalog 里有、识别库里没有。这是 `check_consistency` 管的那种不一致，
        # 在这里如实说出来而不是让 `library.replace` 抛一句 ValueError。
        raise IngestRejected(
            409,
            "photo_not_in_library",
            f"这张照片在识别库里没有对应的记录（{photo_id}），"
            "先跑 `photoar-server reindex` 修一下。",
        )
    if not ref_path.is_file():
        raise IngestRejected(404, "ref_not_found", f"参考图不存在：{ref_path}")
    if fsbrowser.kind_of(ref_path) != "image":
        raise IngestRejected(
            415, "ref_not_image", f"参考图不是支持的图片格式：{ref_path.suffix}"
        )

    try:
        img = fsbrowser.decode_for_thumb(ref_path, "image")
    except fsbrowser.ThumbFailed as exc:
        raise IngestRejected(415, "ref_undecodable", str(exc)) from exc

    backend = library.backend
    features = backend.extract(img)
    if len(features) == 0:
        raise IngestRejected(
            422, "no_features", f"这张照片提不出任何 {backend.name} 特征，无法识别"
        )

    samples = synth.generate(
        img, cfg.self_score_samples, seed=0, long_edge=synth_long_edge
    )
    self_score = dedup.self_score(
        features,
        [backend.extract(q) for q, _ in samples],
        verify_fn=backend.verify,
    )

    if dedup_gate:
        # 排除自己走 `exclude=` 参数，**不能**把它从 known 里删掉：`conflicts` 对
        # 查不到分数的照片按「极低」处理（宁可多报冲突），于是删掉它会让
        # `min(s_new, 0) < ratio * m` 恒成立 —— 把「可能冲突」变成「必然冲突」。
        # 第一版就是这么写错的，症状是这个接口对主要用法 100% 失败。
        known = {
            str(p["id"]): int(p["self_score"]) for p in catalog.list_photos()
        }
        conflicts: list[Conflict] = library.conflicts(
            features,
            self_score,
            known,
            query_features=backend.extract_query(img),
            exclude=photo_id,
        )
        if conflicts:
            raise IngestRejected(
                409,
                "near_duplicate",
                "库里**另一张**照片和你要换上去的这张几乎相同。换上去会让它们互相判"
                "ambiguous，结果是两张都永久识别不出来。原来那张没有被换掉。",
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

    # --- 生成物。文件名按 photo_id，所以是原地覆盖旧的那份 ---
    thumb_path = cfg.thumb_dir / f"{photo_id}.jpg"
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(
        fsbrowser.encode_thumb(img, fsbrowser.REF_THUMB_LONG_EDGE)
    )

    ref_asset_id = _upsert_file_asset(
        catalog, ref_path, "image", probe_video=False, cfg=cfg
    )

    catalog.set_photo_ref(
        photo_id,
        ref_asset_id=ref_asset_id,
        self_score=self_score,
        thumb_path=str(thumb_path),
    )
    slot = library.replace(photo_id, features)

    return ReplaceRefResult(
        photo_id=photo_id,
        self_score=self_score,
        ref_asset_id=ref_asset_id,
        slot=slot,
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
