"""服务配置。JSON 文件 + 环境变量覆盖。

token 支持从环境变量取（`PHOTOAR_TOKEN`），优先级高于配置文件：容器镜像里
不该躺着一个明文预共享 token，而 QNAP 的 Container Station 能直接注入环境变量。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import transcode
from ..quality import ARCOREIMG
from .mediaresolve import DEFAULT_STRATEGIES

DEFAULT_PORT = 8964  # spec §9.1 的 LAN endpoint 用的端口

# `/v1/recognize` 的请求体上限。spec §7 说 frame 是长边 640px q70 的 JPEG，
# 约 50KB。给 40 倍余量挡住误发原图（4000 万像素手机原图约 8-15MB），同时
# 不至于把一张稍大的帧判成攻击。上传接口另有自己的上限。
MAX_RECOGNIZE_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

# 算自匹配分用的扰动样本数。`dedup.self_score` 取中位数，样本太少中位数不稳；
# 20 与 §14.1 的回归测试同一个数，也是 0d 全部实测数字的来源。每张约 1s。
SELF_SCORE_SAMPLES = 20


class ConfigError(ValueError):
    pass


@dataclass
class ServerConfig:
    token: str
    roots: dict[str, str]
    data_dir: Path
    vocab_path: Path
    bind: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    arcoreimg: str = ARCOREIMG
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    # 转码编码器。"auto"（缺省）探测到核显就走 h264_vaapi，否则静默回退
    # libx264；显式写 "h264_vaapi" 时探测失败会直接报错，是部署时验证硬编
    # 到底有没有生效的唯一可靠手段（见 transcode.resolve_encoder）。
    video_encoder: str = transcode.ENCODER_AUTO
    video_preset: str = transcode.SW_PRESET  # 只对 libx264 生效
    vaapi_device: str = transcode.VAAPI_DEVICE
    media_strategies: tuple[str, ...] = DEFAULT_STRATEGIES
    media_custom_prefix: str | None = None
    self_score_samples: int = SELF_SCORE_SAMPLES
    upload_dir_root: str | None = None  # POST /v1/upload 的落地根，须在白名单内
    version: str = "phase1"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.db"

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def imgdb_dir(self) -> Path:
        return self.data_dir / "imgdb"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumb"

    @property
    def playable_dir(self) -> Path:
        return self.data_dir / "playable"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir, self.library_dir, self.imgdb_dir,
            self.thumb_dir, self.playable_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_dict(cls, doc: dict[str, Any], *, base: Path | None = None) -> "ServerConfig":
        def rel(p: str) -> Path:
            path = Path(p).expanduser()
            return path if path.is_absolute() or base is None else (base / path)

        token = os.environ.get("PHOTOAR_TOKEN") or doc.get("token") or ""
        if not token:
            raise ConfigError(
                "必须配置 token（配置文件的 token 字段或环境变量 PHOTOAR_TOKEN）。"
                "服务会暴露 NAS 上的文件，不设 token 等于对隧道全网开放。"
            )
        roots = doc.get("roots") or {}
        if not roots:
            raise ConfigError("必须配置至少一个白名单根目录 roots")
        for name, p in roots.items():
            if not str(p).startswith("/"):
                raise ConfigError(f"白名单根目录必须是绝对路径：{name}={p!r}")
        if "data_dir" not in doc:
            raise ConfigError("必须配置 data_dir（转码产物与索引都写在这里）")
        if "vocab_path" not in doc:
            raise ConfigError(
                "必须配置 vocab_path。词汇树是固定的、由 `photoar build` 预先"
                "训练好；服务端不训练（换 vocab 要全库重建索引）。"
            )
        media = doc.get("media") or {}
        return cls(
            token=str(token),
            roots={str(k): str(v) for k, v in roots.items()},
            data_dir=rel(str(doc["data_dir"])),
            vocab_path=rel(str(doc["vocab_path"])),
            bind=str(doc.get("bind", "0.0.0.0")),
            port=int(os.environ.get("PHOTOAR_PORT") or doc.get("port", DEFAULT_PORT)),
            arcoreimg=str(doc.get("arcoreimg", ARCOREIMG)),
            ffmpeg=str(doc.get("ffmpeg", "ffmpeg")),
            ffprobe=str(doc.get("ffprobe", "ffprobe")),
            video_encoder=str(doc.get("video_encoder", transcode.ENCODER_AUTO)),
            video_preset=str(doc.get("video_preset", transcode.SW_PRESET)),
            vaapi_device=str(doc.get("vaapi_device", transcode.VAAPI_DEVICE)),
            media_strategies=tuple(media.get("strategies", DEFAULT_STRATEGIES)),
            media_custom_prefix=media.get("custom_prefix"),
            self_score_samples=int(doc.get("self_score_samples", SELF_SCORE_SAMPLES)),
            upload_dir_root=doc.get("upload_dir_root"),
            version=str(doc.get("version", "phase1")),
            extra={
                k: v
                for k, v in doc.items()
                if k
                not in {
                    "token", "roots", "data_dir", "vocab_path", "bind", "port",
                    "arcoreimg", "ffmpeg", "ffprobe", "media", "self_score_samples",
                    "upload_dir_root", "version",
                    "video_encoder", "video_preset", "vaapi_device",
                }
            },
        )

    @classmethod
    def load(cls, path: str | Path) -> "ServerConfig":
        path = Path(path)
        doc = json.loads(path.read_text("utf-8"))
        return cls.from_dict(doc, base=path.parent)
