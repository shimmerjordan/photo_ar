"""ffmpeg/ffprobe 封装：视频探测与转码到播放规格（spec §12）。

+faststart 是硬要求：没有它 moov box 在文件尾部，客户端无法边下边播。
scale=-2:min(TARGET_HEIGHT,ih) 而非 scale=-2:TARGET_HEIGHT：-2 保证宽度为
偶数（H.264 的要求）；用 min() 而不是恒定的目标高度，是因为
needs_transcode() 判断"要不要转码"还看 faststart/时长/体积/宽度奇偶等条件，
跟分辨率高矮无关——一个 640x480、缺 faststart 的素材一样会触发转码（M11）。
如果目标高度写死，这类矮于目标的源会被放大：放大不增加任何真实清晰度，只是
白白增加编码时间与体积，也违背"转码只整形不合规的素材、不升清"这个隐含
契约。ffmpeg 的 scale 滤镜支持在参数里直接用 min()/ih 表达式，min() 的
逗号必须转义（写成 \\,）——不转义的话 ffmpeg 的滤镜链解析器会把它当成
"下一个滤镜"的分隔符，报 "No such filter" 而不是数值意义上的错误（已用
真实 ffmpeg 二进制实测两种写法的行为）。

## 「视频多大」这件事，以前没有任何一处在管

播放规格由下面几个常量决定，其中时长×（视频+音频码率）直接决定了体积上限，
`MAX_PLAYABLE_BYTES` 就是那个**算术后果**，不是另外拍的一个数。
写成推导而不是字面量，是因为这几个常量
一改（2026-07-30 从 15s/720p/1.5M 改到 30s/1080p/4M 就是一次），字面量必然
对不上，而对不上是**静默**的：要么白转码本来合规的片子，要么放行一个超出
端侧缓存预算和带宽账的大文件。

它同时是「原片能不能直接拿来播」的判据（见 [needs_transcode]）。这一条以前
是漏的：旧判据只看高度、时长、faststart、宽度奇偶，**完全不看体积或码率**。
`TARGET_HEIGHT = 720` 把这个漏洞掩盖住了——手机视频基本都 ≥1080p，一律要转，
所以没人撞到。提到 1080p 之后就不一样了：一条 1080p 25Mbps 的手机原片会被
判成「已合规」，原样发给客户端。30 秒 90MB，AR 里认出照片后要等半分钟才出画，
而服务端日志、入库结果、客户端都不会报任何错。
"""

import functools
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TARGET_HEIGHT = 1080
MAX_DURATION_MS = 30_000
MAX_BITRATE = "4000k"
BUF_SIZE = "8000k"
CRF = "23"
AUDIO_BITRATE = "128k"

# VBR 模式下给硬编用的目标码率。**必须低于 [MAX_BITRATE]**：VBR 的 -b:v 是
# 「想要的平均」，maxrate 只是天花板，两个写成一样等于 CBR——静态画面也照样
# 吃满 4Mbps，体积一路顶到上限，而画质一点没多。软编走 CRF 不需要这个数。
TARGET_BITRATE = "3200k"

_FASTSTART_PROBE_BYTES = 128 * 1024


def _kbps(v: str) -> int:
    """把 "4000k" 这种 ffmpeg 码率写法读成 4000。"""
    return int(v.removesuffix("k"))


# 播放版的体积上限，也是本项目**唯一**一处「视频多大」的硬判据。
#
# 一成余量**不是保险起见，是实测必需**（单位统一用 MiB）：
#   裸算术 (4000+128)kbps × 30s = 15,480,000 字节 = 14.76 MiB
#   实测最坏 30s 1080p 纯噪声源（压缩比最差的一端）产物 = 14.90 MiB
# 产物比裸算术**大**——因为 `-maxrate` 限的是滑动窗口内的平均码率、不是总量，
# 瞬时允许冲到 bufsize；mp4 容器自身还有开销（moov/stco 表随时长线性增长）。
# 没有余量的话，我们自己转出来的最坏情况产物会被判成「不合规」，于是下次重建
# 又去转一遍，转出来还是不合规。留一成（→ 16.24 MiB）刚好容下这些，又不至于
# 放过一个真正过大的文件。
MAX_PLAYABLE_BYTES = int(
    (_kbps(MAX_BITRATE) + _kbps(AUDIO_BITRATE)) * 1000 // 8
    * (MAX_DURATION_MS / 1000)
    * 1.1
)

# ---- 编码器 ----
#
# 硬编用 **VAAPI 而不是 QSV**，这一条是查出来的不是猜的：QNAP TS-464C2 的
# N5095 是 Jasper Lake，核显是 Gen11。Debian trixie 的 ffmpeg 链的是 oneVPL
# （`libvpl.so.2`），而 oneVPL 的 GPU runtime（`libmfx-gen1.2`）只覆盖
# Gen12+；Gen11 要靠已被弃用的 Media SDK 运行时，而 trixie 里
# `intel-media-sdk`/`libmfxhw64`/`libmfx1` **三个包都不存在**。所以容器里
# 虽然 `ffmpeg -encoders` 列得出 h264_qsv，真跑起来是
# 「Error creating a MFX session: -9」（已实测）。
# iHD 驱动（`intel-media-va-driver`）覆盖 Gen8+，含 Jasper Lake，
# 所以 h264_vaapi 是这台机器上唯一走得通的硬编路径。
ENCODER_AUTO = "auto"
SW_ENCODER = "libx264"
HW_ENCODER = "h264_vaapi"
VAAPI_DEVICE = "/dev/dri/renderD128"

# 软编默认 veryfast，不是 slow。**这不是拍的，是量的**：本机 Docker（--cpus=3，
# 与 docker-compose 一致）转一条 34s→30s 的 1080p 高噪声源，同一条源同一套参数，
# 只换 preset：
#
#     slow 89.2s / medium 50.7s / fast 33.8s / veryfast 18.2s / ultrafast 13.3s
#
# 产物体积几乎不动（14.84 / 14.84 / 14.90 / 14.66 MiB）—— 高码率源下 -maxrate
# 先撞上，crf 根本没约束到，所以慢 preset 换来的是**同码率下的画质**，不是更小
# 的文件。而时间差是 5 倍。
#
# 5 倍在这台机器上是定性的差别：N5095 的单线程约为本机的 1/3.1，slow 折算下来
# **一条视频约 4.6 分钟**，veryfast 约 56 秒。入库是同步 HTTP 请求（attach 那一
# 下要等转码完），4.6 分钟的请求本身就会撞客户端超时；一万张规模更是几百小时和
# 几十小时的区别。
#
# 挂上 /dev/dri 走 VAAPI 时这个值**完全不生效**（硬编不吃 x264 preset），
# 它只是软编回退路径的档位。想要更好的画质就在 config 里把 video_preset 调到
# medium/slow，代价明确写在上面。
SW_PRESET = "veryfast"


@functools.lru_cache(maxsize=8)
def hardware_ready(ffmpeg: str = "ffmpeg", vaapi_device: str = VAAPI_DEVICE) -> bool:
    """真跑一帧，看硬编到底能不能用。

    **不能只看 `ffmpeg -encoders` 里有没有 h264_vaapi**：编进去和跑得动是两件事。
    容器里 h264_qsv 一直列得出来，实际因为缺运行时驱动一帧都编不出——只查列表
    的话会一路走到入库时才炸，而且炸在每一条视频上。

    也不能只看 `/dev/dri/renderD128` 在不在：设备节点存在但属于别的厂商
    （开发机上它就是 nvidia 的 render 节点）、或者容器里没有 render 组权限，
    都是「文件在、驱动起不来」。

    结果缓存住：探测要开一次 ffmpeg（约 0.1-0.3s），而批量入库会问上万次。
    测试里改了 ffmpeg 路径要 `hardware_ready.cache_clear()`。
    """
    if not Path(vaapi_device).exists():
        return False
    if shutil.which(ffmpeg) is None and not Path(ffmpeg).is_file():
        return False
    try:
        proc = subprocess.run(
            [
                ffmpeg, "-v", "error",
                "-init_hw_device", f"vaapi=hw:{vaapi_device}",
                "-filter_hw_device", "hw",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=0.2",
                "-vf", "format=nv12,hwupload",
                "-c:v", HW_ENCODER, "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def resolve_encoder(
    pref: str = ENCODER_AUTO,
    *,
    ffmpeg: str = "ffmpeg",
    vaapi_device: str = VAAPI_DEVICE,
) -> str:
    """把配置里的 video_encoder 解析成真正要用的编码器名。

    `auto` 探测不到硬编就**静默回退**软编——转码慢十倍总比入库全线失败好。
    显式写 `h264_vaapi` 则**不回退**，直接抛：这是留给部署时验证用的。不这么分
    的话，「以为在用硬编、其实全程软编」唯一的发现方式是掐表，而那要等到
    一万条视频跑了三天之后。
    """
    if pref == ENCODER_AUTO:
        return HW_ENCODER if hardware_ready(ffmpeg, vaapi_device) else SW_ENCODER
    if pref == SW_ENCODER:
        return SW_ENCODER
    if pref == HW_ENCODER:
        if not hardware_ready(ffmpeg, vaapi_device):
            raise FfmpegMissing(
                f"配置指定了硬件编码 {HW_ENCODER}，但 {vaapi_device} 上跑不起来。"
                f"检查：容器有没有 --device /dev/dri、镜像里有没有 "
                f"intel-media-va-driver、以及这颗核显是不是 Gen8 以上。"
                f"想让它自己回退软编就把 video_encoder 设成 \"auto\"。"
            )
        return HW_ENCODER
    raise ValueError(
        f"不认识的 video_encoder：{pref!r}，只能是 "
        f"{ENCODER_AUTO}/{SW_ENCODER}/{HW_ENCODER}"
    )


class FfmpegMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    """视频探测结果。

    duration_ms：取视频流、音频流（若存在且报告了 duration）中较大的一个；
    只有当所有媒体流都没有报告 duration 时才退回容器级 format.duration。
    容器级数值会被 AAC 编码器的 priming delay 拉高（实测 ~24ms），因此只在
    没有更精确来源时才使用它。

    size_bytes：文件大小。**不是从码率算的**——ffprobe 报的 bit_rate 对
    VBR 片子只是个平均值，而且有些容器压根不报。直接 stat 拿到的是真实字节数，
    也正是客户端要下载的那个数。默认 0 让手写 VideoInfo 的测试不至于全部要改，
    代价是「0 字节永远算合规」，而这在真实路径上到不了（probe 一定会填）。
    """

    width: int
    height: int
    duration_ms: int
    faststart: bool
    size_bytes: int = 0


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
        size_bytes=path.stat().st_size,
    )


def needs_transcode(info: VideoInfo) -> bool:
    """原片能不能原样拿去播。

    体积这一条（[MAX_PLAYABLE_BYTES]）是 2026-07-30 补的，和把
    [TARGET_HEIGHT] 提到 1080 是同一件事的两半：只提分辨率不加体积判据，
    等于把「手机原片直接放行」变成默认路径——而手机拍的 1080p 是 20-25Mbps，
    比我们的播放规格高五六倍。见模块 docstring。

    体积判据顺带覆盖了「码率过高」：时长已经单独判过，同样时长下体积就是
    平均码率，所以不需要再去读 ffprobe 那个对 VBR 不可靠的 bit_rate。
    """
    return (
        info.height > TARGET_HEIGHT
        or info.duration_ms > MAX_DURATION_MS
        or info.size_bytes > MAX_PLAYABLE_BYTES
        or not info.faststart
        or info.width % 2 != 0
    )


def transcode(
    src: str | Path,
    dst: str | Path,
    ffmpeg: str = "ffmpeg",
    max_duration_ms: int = MAX_DURATION_MS,
    encoder: str = ENCODER_AUTO,
    preset: str = SW_PRESET,
    vaapi_device: str = VAAPI_DEVICE,
) -> None:
    """转成播放规格。产物必须自身合规（`not needs_transcode(probe(dst))`）。

    那条不变量约束了硬编怎么写：VAAPI **没有 CRF**，只有 CQP 和 VBR。CQP
    是「固定质量、体积随内容跑」，噪声大的片子能冲到远超上限，于是转码产物
    自己不合规——下次启动或者重建时又会被判成要转码，转出来还是不合规。
    所以硬编走 VBR 并显式限 maxrate/bufsize，把体积钉在账内。

    preset 只对 libx264 生效：VAAPI 那边对应的旋钮是 -compression_level，
    语义完全不同（而且默认值就够用），不做映射比瞎映射好。
    """
    _require(ffmpeg)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    enc = resolve_encoder(encoder, ffmpeg=ffmpeg, vaapi_device=vaapi_device)

    scale = f"scale=-2:min({TARGET_HEIGHT}\\,ih)"
    if enc == HW_ENCODER:
        head = [
            "-init_hw_device", f"vaapi=hw:{vaapi_device}",
            "-filter_hw_device", "hw",
        ]
        # 缩放留在 CPU 上做（scale_vaapi 在 Gen11 上对奇数尺寸的处理和 -2 的
        # 语义对不上），只把编码搬到 GPU：format=nv12 是 hwupload 的前置要求，
        # 少了它 ffmpeg 报 "Impossible to convert between the formats"。
        vf = f"{scale},format=nv12,hwupload"
        codec = [
            "-c:v", HW_ENCODER,
            "-rc_mode", "VBR", "-b:v", TARGET_BITRATE,
            "-maxrate", MAX_BITRATE, "-bufsize", BUF_SIZE,
        ]
    else:
        head = []
        vf = scale
        codec = [
            "-c:v", SW_ENCODER, "-preset", preset, "-crf", CRF,
            "-maxrate", MAX_BITRATE, "-bufsize", BUF_SIZE,
        ]

    proc = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", *head,
            "-i", str(src),
            "-t", f"{max_duration_ms / 1000:.3f}",
            "-vf", vf, *codec,
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(dst),
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败（编码器 {enc}）：{proc.stderr.strip()}")
