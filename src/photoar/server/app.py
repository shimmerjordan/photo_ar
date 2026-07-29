"""路由、鉴权与 spec §7 的全部接口。

这一层刻意不依赖 `http.server`：`handle(Request) -> Response` 是纯函数式的，
`httpd.py` 才负责把 socket 上的字节变成 `Request`。好处是集成测试可以直接
构造 `Request` 调 `handle`，不需要起端口、不需要真网络 —— spec §14.4 要求的
"完整入库→识别→解析→取流闭环，不依赖真实 NAS 或网盘"因此能全程离线跑完。

URL 一律返回**相对路径**（spec §7）：服务端不知道客户端此刻走的是 LAN、
Tailscale 还是隧道，返回绝对 URL 会把客户端锁死在一条通道上。唯一例外是
`via == "direct_link"` 的网盘 CDN 地址，由 `via` 字段明确区分。
"""

import hmac
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from ..vocab import Vocab
from . import fsbrowser, integrity, ingest, mediaresolve
from .config import (
    MAX_JSON_BYTES,
    MAX_RECOGNIZE_BYTES,
    MAX_UPLOAD_BYTES,
    ServerConfig,
)
from .db import Catalog
from .library import PhotoLibrary
from .multipart import MultipartError, boundary_of, parse_multipart
from .ranges import ByteRange, RangeNotSatisfiable, parse_range
from .safepath import PathDenied, Roots

_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 客户端可以用这个头告诉服务端它此刻走的是哪个 endpoint，只进 recognize_log
# 的 `via` 列（spec §6 的注释："命中时客户端用的 api endpoint 名"）。服务端
# 自己推断不出来 —— 隧道、Tailscale、LAN 到这里都是一个 TCP 连接。
ENDPOINT_HEADER = "x-photoar-endpoint"

# cloudflared 一定会加的头。用来在服务端也挡一道上传（spec §9.4 要求客户端
# 隐藏入口，但客户端可能是旧版本或别人写的）。让它 413 并说明原因，比让用户
# 传了 100MB 再被 Cloudflare 掐断要好。
_TUNNEL_HEADERS = ("cf-ray", "cf-connecting-ip")


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str, **detail) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


class BodyTooLarge(HttpError):
    def __init__(self, limit: int) -> None:
        super().__init__(413, "body_too_large", f"请求体超过上限 {limit} 字节")


@dataclass
class Request:
    method: str
    raw_path: str
    headers: dict[str, str]  # 键一律小写
    rfile: Any = None  # 有 .read(n) 即可
    content_length: int = 0
    client: str = "-"
    _body: bytes | None = field(default=None, repr=False)
    # 已从 rfile 读走的字节数。httpd 靠它决定 keep-alive 前还要补读多少 ——
    # 少读会让下一个请求从残留字节开始解析，多读会一直阻塞在一个已经读空的
    # 连接上（上传就是这样：stream_to 读完了，但 _body 仍是 None）。
    consumed: int = field(default=0, repr=False)

    @property
    def path(self) -> str:
        return unquote(urlsplit(self.raw_path).path)

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.raw_path).query, keep_blank_values=True)

    def q1(self, name: str) -> str | None:
        v = self.query.get(name)
        return v[0] if v else None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def read_body(self, max_bytes: int) -> bytes:
        if self._body is not None:
            return self._body
        if self.content_length > max_bytes:
            raise BodyTooLarge(max_bytes)
        data = b"" if self.rfile is None or not self.content_length else self.rfile.read(
            self.content_length
        )
        self.consumed += len(data)
        if len(data) != self.content_length:
            raise HttpError(400, "short_body", "请求体比 Content-Length 声明的短")
        self._body = data
        return data

    def json_body(self, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
        raw = self.read_body(max_bytes)
        if not raw:
            raise HttpError(400, "empty_body", "需要 JSON 请求体")
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise HttpError(400, "bad_json", f"JSON 解析失败：{exc}") from exc
        if not isinstance(doc, dict):
            raise HttpError(400, "bad_json", "JSON 请求体必须是对象")
        return doc

    def stream_to(self, dst: Path, max_bytes: int) -> int:
        """把请求体直接写到文件，不在内存里囤 —— 上传可能是几百 MB。"""
        if self.content_length > max_bytes:
            raise BodyTooLarge(max_bytes)
        dst.parent.mkdir(parents=True, exist_ok=True)
        remaining = self.content_length
        written = 0
        with open(dst, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
                written += len(chunk)
                self.consumed += len(chunk)
        if remaining:
            dst.unlink(missing_ok=True)
            raise HttpError(400, "short_body", "上传中断：收到的字节少于声明的长度")
        return written


@dataclass
class Response:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    # 吐文件时用这两个字段代替 body，由 httpd 分块发送，不把文件读进内存
    file: Path | None = None
    file_range: ByteRange | None = None

    @property
    def content_length(self) -> int:
        if self.file is not None:
            if self.file_range is not None:
                return self.file_range.length
            return self.file.stat().st_size
        return len(self.body)


def json_response(status: int, obj: Any, **headers: str) -> Response:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8"}
    h.update(headers)
    return Response(status=status, headers=h, body=body)


def _error(status: int, code: str, message: str, **detail) -> Response:
    payload: dict[str, Any] = {"error": code, "message": message}
    payload.update(detail)
    return json_response(status, payload)


class Server:
    def __init__(
        self,
        cfg: ServerConfig,
        catalog: Catalog,
        library: PhotoLibrary,
        roots: Roots,
        resolver: mediaresolve.MediaResolver,
    ) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.library = library
        self.roots = roots
        self.resolver = resolver
        self._started = time.time()

    @classmethod
    def create(cls, cfg: ServerConfig) -> "Server":
        cfg.ensure_dirs()
        if not cfg.vocab_path.exists():
            raise FileNotFoundError(
                f"找不到词汇树 {cfg.vocab_path}。服务端不训练词汇树（换 vocab 要"
                f"全库重建索引），用 `photoar build` 先训一份。"
            )
        return cls(
            cfg=cfg,
            catalog=Catalog(cfg.db_path),
            library=PhotoLibrary(cfg.library_dir, Vocab.load(cfg.vocab_path)),
            roots=Roots(cfg.roots),
            resolver=mediaresolve.MediaResolver(
                strategies=tuple(cfg.media_strategies),
                custom_prefix=cfg.media_custom_prefix,
            ),
        )

    def check_consistency(self) -> list[str]:
        """catalog 与识别库是否记着同一批照片。启动时调，问题只报不改。

        两个方向的不一致含义完全不同：
        - catalog 有、库里没有 → 那张照片永远识别不出来（入库时 library.add
          失败过）。`reindex` 修不了，需要重新入库。
        - 库里有、catalog 没有 → 识别能命中但所有取流接口都 404。
        自动"修复"任何一边都是在猜，所以只报告。
        """
        cat = {str(p["id"]) for p in self.catalog.list_photos()}
        lib = set(self.library.photo_ids())
        problems = []
        for pid in sorted(cat - lib):
            problems.append(f"catalog 有但识别库没有（永远识别不出）：{pid}")
        for pid in sorted(lib - cat):
            problems.append(f"识别库有但 catalog 没有（命中后取流会 404）：{pid}")
        return problems

    # ---- 鉴权 ----

    def _authorized(self, req: Request) -> bool:
        raw = req.header("authorization") or ""
        scheme, _, token = raw.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(token.strip(), self.cfg.token)

    # ---- 分发 ----

    def handle(self, req: Request) -> Response:
        try:
            return self._dispatch(req)
        except PathDenied as exc:
            # spec §13：越界记日志（"正常客户端不会产生，出现即为异常"）。
            # 响应体只给通用文案，不回显解析结果 —— 符号链接指向哪里是服务端信息。
            self._log_denied(req, exc)
            return _error(403, "path_denied", "路径不在允许访问的目录内")
        except ingest.IngestRejected as exc:
            return _error(exc.status, exc.code, exc.message, **exc.detail)
        except HttpError as exc:
            return _error(exc.status, exc.code, exc.message, **exc.detail)
        except FileNotFoundError as exc:
            return _error(404, "not_found", str(exc))
        except NotADirectoryError as exc:
            return _error(400, "not_a_directory", f"不是目录：{exc}")
        except fsbrowser.ThumbFailed as exc:
            return _error(415, "thumb_failed", str(exc))
        except MultipartError as exc:
            return _error(400, "bad_multipart", str(exc))
        except RangeNotSatisfiable as exc:
            return Response(
                status=416,
                headers={"Content-Range": exc.content_range, "Accept-Ranges": "bytes"},
            )

    def _log_denied(self, req: Request, exc: PathDenied) -> None:
        print(
            f"[photoar] 403 路径越界 client={req.client} {req.method} {req.raw_path} "
            f"reason={exc.reason}",
            flush=True,
        )

    def _dispatch(self, req: Request) -> Response:
        path = req.path
        if not path.startswith("/v1/"):
            return _error(404, "not_found", f"没有这个接口：{path}")
        if not self._authorized(req):
            return _error(401, "unauthorized", "需要 Bearer token")

        method = "GET" if req.method == "HEAD" else req.method
        parts = path.strip("/").split("/")[1:]  # 去掉 v1

        table: list[tuple[str, tuple[str, ...], Callable[..., Response]]] = [
            ("GET", ("ping",), self._ping),
            ("POST", ("recognize",), self._recognize),
            ("GET", ("photos",), self._list_photos),
            ("POST", ("photo",), self._create_photo),
            ("GET", ("photo", "*"), self._photo_detail),
            ("GET", ("photo", "*", "imgdb"), self._photo_imgdb),
            ("GET", ("photo", "*", "thumb"), self._photo_thumb),
            ("GET", ("photo", "*", "media"), self._photo_media),
            ("POST", ("photo", "*", "video"), self._photo_attach_video),
            ("GET", ("asset", "*", "stream"), self._asset_stream),
            ("GET", ("fs", "list"), self._fs_list),
            ("GET", ("fs", "thumb"), self._fs_thumb),
            ("POST", ("upload",), self._upload),
            ("GET", ("history",), self._history),
        ]
        allowed: set[str] = set()
        for verb, pattern, handler in table:
            if len(pattern) != len(parts):
                continue
            args = []
            ok = True
            for want, got in zip(pattern, parts):
                if want == "*":
                    args.append(got)
                elif want != got:
                    ok = False
                    break
            if not ok:
                continue
            allowed.add(verb)
            if verb == method:
                return handler(req, *args)
        if allowed:
            return _error(
                405, "method_not_allowed", f"{path} 只支持 {sorted(allowed)}"
            )
        return _error(404, "not_found", f"没有这个接口：{path}")

    # ---- 接口 ----

    def _ping(self, req: Request) -> Response:
        # spec §7："必须极轻，不查库"。探活频率由客户端的网络变化回调决定，
        # 且四个 endpoint 是并行探的。
        return json_response(
            200,
            {
                "ok": True,
                "version": self.cfg.version,
                "serverTime": int(time.time() * 1000),
            },
            **{"Cache-Control": "no-store"},
        )

    def _recognize(self, req: Request) -> Response:
        t0 = time.perf_counter()
        boundary = boundary_of(req.header("content-type"))
        parts = parse_multipart(req.read_body(MAX_RECOGNIZE_BYTES), boundary)
        part = parts.get("frame")
        if part is None:
            raise HttpError(
                400, "missing_frame", "multipart 里没有 frame 字段（spec §7）"
            )
        img = ingest.decode_frame(part.data)
        if img is None:
            raise HttpError(400, "bad_frame", "frame 不是能解开的图片")

        decision = self.library.recognize(img)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        via = req.header(ENDPOINT_HEADER)

        if not decision.matched or decision.photo_id is None:
            self.catalog.log_recognize(
                photo_id=None, inliers=decision.inliers, latency_ms=latency_ms, via=via
            )
            # spec §7：未命中返回 200 而非 404 —— 扫描时未命中是正常状态，
            # 客户端每 400ms 调一次，不该产生错误日志噪音。
            return json_response(
                200,
                {"matched": False, "latencyMs": latency_ms, "reason": decision.reason},
                **{"Cache-Control": "no-store"},
            )

        photo = self.catalog.get_photo(decision.photo_id)
        if photo is None:
            # 识别库里有、catalog 里没有。check_consistency() 会在启动时报出
            # 这种不一致，这里当成未命中而不是 500：客户端继续扫下一帧，
            # 用户不会卡住。
            self.catalog.log_recognize(
                photo_id=None, inliers=decision.inliers, latency_ms=latency_ms, via=via
            )
            return json_response(
                200,
                {"matched": False, "latencyMs": latency_ms, "reason": "orphan"},
                **{"Cache-Control": "no-store"},
            )

        self.catalog.log_recognize(
            photo_id=decision.photo_id,
            inliers=decision.inliers,
            latency_ms=latency_ms,
            via=via,
        )
        pid = decision.photo_id
        payload = {
            "matched": True,
            "photoId": pid,
            "inliers": decision.inliers,
            "printWidthM": float(photo["print_width_m"]),
            "imgdbUrl": f"/v1/photo/{pid}/imgdb",
            "refThumbUrl": f"/v1/photo/{pid}/thumb",
            "mediaUrl": f"/v1/photo/{pid}/media",
            "latencyMs": latency_ms,
        }
        aspect = self._ref_aspect(photo)
        if aspect is not None:
            payload["refAspect"] = aspect
        if photo["ref_stale"]:
            # spec §13：参考图内容变过，仍尝试命中但要提示特征可能已过期
            payload["refStale"] = True
        return json_response(200, payload, **{"Cache-Control": "no-store"})

    def _ref_aspect(self, photo: dict[str, Any]) -> float | None:
        asset = self.catalog.get_asset(str(photo["ref_asset_id"]))
        if not asset or not asset["width_px"] or not asset["height_px"]:
            return None
        return round(float(asset["width_px"]) / float(asset["height_px"]), 6)

    def _photo_or_404(self, photo_id: str) -> dict[str, Any]:
        if not _ID_RE.match(photo_id):
            raise HttpError(404, "not_found", f"photoId 格式不对：{photo_id}")
        photo = self.catalog.get_photo(photo_id)
        if photo is None:
            raise HttpError(404, "not_found", f"照片不存在：{photo_id}")
        return photo

    def _list_photos(self, req: Request) -> Response:
        out = []
        for p in self.catalog.list_photos():
            out.append(
                {
                    "photoId": str(p["id"]),
                    "title": p["title"],
                    "printWidthM": float(p["print_width_m"]),
                    "qualityScore": int(p["quality_score"]),
                    "refAspect": self._ref_aspect(p),
                    "refThumbUrl": f"/v1/photo/{p['id']}/thumb",
                    "hasVideo": p["video_asset_id"] is not None,
                    "refStale": bool(p["ref_stale"]),
                    "createdAt": int(p["created_at"]),
                }
            )
        return json_response(200, {"photos": out, "total": len(out)})

    def _photo_detail(self, req: Request, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id)
        ref = self.catalog.get_asset(str(photo["ref_asset_id"])) or {}
        video = (
            self.catalog.get_asset(str(photo["video_asset_id"]))
            if photo["video_asset_id"]
            else None
        )
        return json_response(
            200,
            {
                "photoId": photo_id,
                "title": photo["title"],
                "printWidthM": float(photo["print_width_m"]),
                "qualityScore": int(photo["quality_score"]),
                "selfScore": int(photo["self_score"]),
                "refAspect": self._ref_aspect(photo),
                "refPath": ref.get("nas_path"),
                "refMissing": bool(ref.get("missing")),
                "refStale": bool(photo["ref_stale"]),
                "videoPath": video["nas_path"] if video else None,
                "videoMissing": bool(video["missing"]) if video else None,
                "imgdbBytes": int(photo["imgdb_bytes"]),
                "createdAt": int(photo["created_at"]),
                "updatedAt": int(photo["updated_at"]),
            },
        )

    def _static_file(
        self, req: Request, path: Path, content_type: str, *, immutable: bool
    ) -> Response:
        if not path.is_file():
            raise HttpError(404, "not_found", f"文件不存在：{path.name}")
        etag = fsbrowser.etag_for(path)
        headers = {"Content-Type": content_type, "ETag": etag}
        headers["Cache-Control"] = (
            "max-age=31536000, immutable" if immutable else "max-age=3600"
        )
        if (req.header("if-none-match") or "").strip() == etag:
            return Response(status=304, headers=headers)
        return Response(status=200, headers=headers, file=path)

    def _photo_imgdb(self, req: Request, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id)
        # spec §7：ETag + Cache-Control immutable。.imgdb 是照片内容的函数，
        # 内容变了 photo 会被标 ref_stale 并重新入库（换 photo_id），所以
        # immutable 在这里是真的成立，不是图省事。
        return self._static_file(
            req,
            Path(str(photo["imgdb_path"])),
            "application/octet-stream",
            immutable=True,
        )

    def _photo_thumb(self, req: Request, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id)
        return self._static_file(
            req, Path(str(photo["thumb_path"])), "image/jpeg", immutable=True
        )

    def _photo_media(self, req: Request, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id)
        asset_id = photo["playable_asset_id"] or photo["video_asset_id"]
        if not asset_id:
            return json_response(
                200,
                {
                    "url": None,
                    "via": None,
                    "supportsRange": False,
                    "missing": True,
                    "reason": "no_video",
                    "message": "这张照片还没有关联视频",
                },
                **{"Cache-Control": "no-store"},
            )
        asset = self.catalog.get_asset(str(asset_id))
        if asset is None:
            raise HttpError(404, "not_found", f"asset 不存在：{asset_id}")

        # spec §6.1：每次 resolve 前校验 mtime + bytes（只在不一致时才哈希）
        result = integrity.verify_asset(self.catalog, asset)
        asset = self.catalog.get_asset(str(asset_id)) or asset
        resolved = self.resolver.resolve(asset)
        return json_response(
            200,
            {
                "url": resolved.url,
                "via": resolved.via,
                "absolute": resolved.absolute,
                "supportsRange": resolved.supports_range,
                "bytes": int(asset["bytes"]),
                "durationMs": asset["duration_ms"],
                "missing": not result.usable,
                "nasPath": str(asset["nas_path"]),
                "integrity": result.status,
            },
            # 直链有有效期（spec §10：阿里云盘约 15 分钟），这个响应绝不能
            # 被任何中间层缓存。相对路径的情况下也 no-store，省一个分支。
            **{"Cache-Control": "no-store"},
        )

    def _photo_attach_video(self, req: Request, photo_id: str) -> Response:
        self._photo_or_404(photo_id)
        doc = req.json_body()
        raw = doc.get("videoPath")
        if not raw:
            raise HttpError(400, "missing_video_path", "需要 videoPath")
        video = self.roots.resolve(str(raw))
        video_asset_id, playable_asset_id, transcoded = ingest.attach_video(
            cfg=self.cfg, catalog=self.catalog, photo_id=photo_id, video_path=video
        )
        return json_response(
            200,
            {
                "photoId": photo_id,
                "videoAssetId": video_asset_id,
                "playableAssetId": playable_asset_id,
                "transcoded": transcoded,
            },
        )

    def _asset_stream(self, req: Request, asset_id: str) -> Response:
        if not _ID_RE.match(asset_id):
            raise HttpError(404, "not_found", f"assetId 格式不对：{asset_id}")
        asset = self.catalog.get_asset(asset_id)
        if asset is None:
            raise HttpError(404, "not_found", f"asset 不存在：{asset_id}")
        path = Path(str(asset["nas_path"]))
        if not path.is_file():
            self.catalog.update_asset_fingerprint(asset_id, missing=1)
            raise HttpError(
                404,
                "asset_missing",
                "关联的文件已不在 NAS 上",
                nasPath=str(path),
            )
        size = path.stat().st_size
        # spec §7：必须实现 Accept-Ranges + 206，否则 ExoPlayer 无法 seek
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": _content_type_of(path),
            "Cache-Control": "no-store",
        }
        rng = parse_range(req.header("range"), size)
        if rng is None:
            return Response(status=200, headers=headers, file=path)
        headers["Content-Range"] = rng.content_range(size)
        return Response(status=206, headers=headers, file=path, file_range=rng)

    def _fs_list(self, req: Request) -> Response:
        return json_response(200, fsbrowser.list_dir(self.roots, req.q1("path")))

    def _fs_thumb(self, req: Request) -> Response:
        raw = req.q1("path")
        if not raw:
            raise HttpError(400, "missing_path", "需要 path 参数")
        path = self.roots.resolve(raw)
        if not path.is_file():
            raise HttpError(404, "not_found", f"文件不存在：{raw}")
        # spec §7：ETag 基于 path + mtime
        etag = fsbrowser.etag_for(path, extra=f"thumb{fsbrowser.THUMB_LONG_EDGE}")
        if (req.header("if-none-match") or "").strip() == etag:
            return Response(
                status=304, headers={"ETag": etag, "Cache-Control": "max-age=86400"}
            )
        data = fsbrowser.thumb_bytes(path, fsbrowser.THUMB_LONG_EDGE)
        return Response(
            status=200,
            headers={
                "Content-Type": "image/jpeg",
                "ETag": etag,
                "Cache-Control": "max-age=86400",
            },
            body=data,
        )

    def _create_photo(self, req: Request) -> Response:
        doc = req.json_body()
        ref_raw = doc.get("refPath")
        if not ref_raw:
            raise HttpError(400, "missing_ref_path", "需要 refPath")
        ref = self.roots.resolve(str(ref_raw))
        video = (
            self.roots.resolve(str(doc["videoPath"])) if doc.get("videoPath") else None
        )
        width_mm = doc.get("printWidthMm")
        if width_mm is None:
            raise HttpError(
                400,
                "missing_print_width",
                "需要 printWidthMm（打印物理宽度，毫米）。跟踪精度依赖它，"
                "所以不给默认值（spec §11 的 print_width_m NOT NULL）。",
            )
        try:
            print_width_m = float(width_mm) / 1000.0
        except (TypeError, ValueError) as exc:
            raise HttpError(
                400, "bad_print_width", f"printWidthMm 不是数字：{width_mm!r}"
            ) from exc

        result = ingest.ingest_photo(
            cfg=self.cfg,
            catalog=self.catalog,
            library=self.library,
            ref_path=ref,
            video_path=video,
            print_width_m=print_width_m,
            title=doc.get("title"),
        )
        return json_response(
            201,
            {
                "photoId": result.photo_id,
                "qualityScore": result.quality_score,
                "selfScore": result.self_score,
                "imgdbBytes": result.imgdb_bytes,
                "printWidthM": result.print_width_m,
                "transcoded": result.transcoded,
                "elapsedMs": result.elapsed_ms,
                "libraryPhotos": len(self.library),
            },
        )

    def _upload(self, req: Request) -> Response:
        # spec §9.4：上传只允许走非隧道通道（Cloudflare 免费版 100MB 请求体上限）。
        # 客户端应隐藏入口，服务端这里再挡一道 —— 客户端可能是旧版本。
        for h in _TUNNEL_HEADERS:
            if req.header(h):
                raise HttpError(
                    413,
                    "upload_via_tunnel",
                    "上传不能走 Cloudflare 隧道（有 100MB 请求体上限）。"
                    "连回家庭网络或开启 Tailscale 后再上传。",
                )
        if not self.cfg.upload_dir_root:
            raise HttpError(
                503,
                "upload_disabled",
                "服务端未配置 upload_dir_root，上传功能关闭。"
                "正常用法是关联 NAS 上已有的文件（POST /v1/photo）。",
            )
        name = req.q1("name")
        if not name:
            raise HttpError(400, "missing_name", "需要 name 参数（目标文件名）")
        safe = Path(name).name  # 丢掉任何目录成分
        if not safe or safe.startswith(".") or safe != name:
            raise HttpError(
                400,
                "bad_name",
                "name 只能是纯文件名，不能含路径分隔符、不能以点开头",
            )
        # 落地路径同样过白名单校验，而不是信任配置里的前缀直接拼接
        dst = self.roots.resolve(str(Path(self.cfg.upload_dir_root) / safe))
        if dst.exists():
            raise HttpError(409, "already_exists", f"目标文件已存在：{safe}")
        written = req.stream_to(dst, MAX_UPLOAD_BYTES)
        return json_response(
            201, {"path": str(dst), "bytes": written}
        )

    def _history(self, req: Request) -> Response:
        try:
            limit = min(200, max(1, int(req.q1("limit") or 50)))
        except ValueError:
            limit = 50
        rows = []
        for r in self.catalog.recent_logs(limit):
            photo = self.catalog.get_photo(str(r["photo_id"])) if r["photo_id"] else None
            rows.append(
                {
                    "ts": int(r["ts"]),
                    "photoId": r["photo_id"],
                    "title": photo["title"] if photo else None,
                    "refThumbUrl": (
                        f"/v1/photo/{r['photo_id']}/thumb" if photo else None
                    ),
                    "inliers": r["inliers"],
                    "latencyMs": r["latency_ms"],
                    "via": r["via"],
                }
            )
        return json_response(200, {"entries": rows})


_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".3gp": "video/3gpp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _content_type_of(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def send_file(resp: Response, out, chunk: int = 1 << 16) -> None:
    """把 Response 的文件体写到 out。httpd 与测试共用同一份逻辑。"""
    assert resp.file is not None
    with open(resp.file, "rb") as fh:
        if resp.file_range is None:
            shutil.copyfileobj(fh, out, chunk)
            return
        fh.seek(resp.file_range.start)
        remaining = resp.file_range.length
        while remaining > 0:
            data = fh.read(min(chunk, remaining))
            if not data:
                break
            out.write(data)
            remaining -= len(data)
