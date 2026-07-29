"""ffmpeg/ffprobe 封装：视频探测与转码到播放规格（spec §12）。

+faststart 是硬要求：没有它 moov box 在文件尾部，客户端无法边下边播。
scale=-2:720 而非 -1:720，保证宽度为偶数（H.264 的要求）。
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TARGET_HEIGHT = 720
MAX_DURATION_MS = 15_000
MAX_BITRATE = "1500k"
BUF_SIZE = "3000k"
CRF = "26"
AUDIO_BITRATE = "96k"

_FASTSTART_PROBE_BYTES = 128 * 1024


class FfmpegMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    """视频探测结果。

    duration_ms：取视频流、音频流（若存在且报告了 duration）中较大的一个；
    只有当所有媒体流都没有报告 duration 时才退回容器级 format.duration。
    容器级数值会被 AAC 编码器的 priming delay 拉高（实测 ~24ms），因此只在
    没有更精确来源时才使用它。
    """

    width: int
    height: int
    duration_ms: int
    faststart: bool


def _require(binary: str) -> None:
    if shutil.which(binary) is None and not Path(binary).is_file():
        raise FfmpegMissing(f"找不到 {binary}，请安装 ffmpeg 套件或用参数指定路径")


def has_faststart(path: str | Path) -> bool:
    """检查 moov 是否出现在 mdat 之前。

    直接读文件头判断，比解析 ffprobe 的 trace 输出稳得多。用 f.read(n) 做
    有界读取，只取前 _FASTSTART_PROBE_BYTES 字节，不会把整个文件（源视频
    可能几百 MB）载入内存——这一点由 f.read(n) 的调用本身保证，未单独用
    测试验证。
    """
    with open(path, "rb") as f:
        head = f.read(_FASTSTART_PROBE_BYTES)
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1:
        return False  # 头部没有 moov，说明它在后面
    return mdat == -1 or moov < mdat


def probe(path: str | Path, ffprobe: str = "ffprobe") -> VideoInfo:
    _require(ffprobe)
    path = Path(path)
    proc = subprocess.run(
        [
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{proc.stderr.strip()}")

    data = json.loads(proc.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError(f"{path} 里没有视频流")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # 优先用媒体流自身报告的 duration，取其中的最大值：容器级 format.duration
    # 会把 AAC 编码器的 priming delay（约一帧的静音填充）计入总时长，实测比
    # 各流的实际时长多出 ~24ms，足以让本应合规的转码产物被 needs_transcode()
    # 误判为超时。用 "duration" 键是否存在来判断某条流是否报告了时长，而不是
    # 看转换后数值的真值——否则合法的 0.0 会被 falsy 判断误伤。
    stream_durations = [
        float(s["duration"]) for s in (video, audio) if s is not None and "duration" in s
    ]
    duration_s = max(stream_durations) if stream_durations else float(
        data.get("format", {}).get("duration") or 0.0
    )
    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        duration_ms=int(round(duration_s * 1000)),
        faststart=has_faststart(path),
    )


def needs_transcode(info: VideoInfo) -> bool:
    return (
        info.height > TARGET_HEIGHT
        or info.duration_ms > MAX_DURATION_MS
        or not info.faststart
        or info.width % 2 != 0
    )


def transcode(
    src: str | Path,
    dst: str | Path,
    ffmpeg: str = "ffmpeg",
    max_duration_ms: int = MAX_DURATION_MS,
) -> None:
    _require(ffmpeg)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-i", str(src),
            "-t", f"{max_duration_ms / 1000:.3f}",
            "-vf", f"scale=-2:{TARGET_HEIGHT}",
            "-c:v", "libx264", "-preset", "slow", "-crf", CRF,
            "-maxrate", MAX_BITRATE, "-bufsize", BUF_SIZE,
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(dst),
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败：{proc.stderr.strip()}")
