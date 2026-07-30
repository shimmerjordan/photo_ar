import shutil
import subprocess

import pytest

from photoar import transcode as T

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="需要 ffmpeg/ffprobe",
)


@pytest.fixture
def sample_video(tmp_path):
    """用 ffmpeg 自带的 testsrc 造视频，不依赖任何素材文件。"""

    def _make(name="in.mp4", w=1920, h=1080, seconds=20, faststart=False):
        path = tmp_path / name
        args = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
        ]
        if faststart:
            args += ["-movflags", "+faststart"]
        args.append(str(path))
        subprocess.run(args, check=True, capture_output=True)
        return path

    return _make


def test_probe_reads_dimensions_and_duration(sample_video):
    info = T.probe(sample_video(w=1280, h=720, seconds=3))
    assert (info.width, info.height) == (1280, 720)
    assert 2500 <= info.duration_ms <= 3500


def test_probe_raises_when_ffprobe_missing(sample_video):
    with pytest.raises(T.FfmpegMissing):
        T.probe(sample_video(seconds=1), ffprobe="not-a-real-ffprobe-xyz")


def test_has_faststart_detects_both_cases(sample_video):
    assert T.has_faststart(sample_video("fs.mp4", seconds=2, faststart=True))
    assert not T.has_faststart(sample_video("nofs.mp4", seconds=2, faststart=False))


def test_has_faststart_on_file_larger_than_probe_window(sample_video):
    """校验有界读取在大文件上依然给出正确答案（不代表"只读了前 128KB"本身
    这一点——那由实现里的 f.read(n) 调用保证）。"""
    fs_path = sample_video("big_fs.mp4", w=1280, h=720, seconds=5, faststart=True)
    nofs_path = sample_video("big_nofs.mp4", w=1280, h=720, seconds=5, faststart=False)

    assert fs_path.stat().st_size > T._FASTSTART_PROBE_BYTES
    assert nofs_path.stat().st_size > T._FASTSTART_PROBE_BYTES

    assert T.has_faststart(fs_path)
    assert not T.has_faststart(nofs_path)


def test_needs_transcode_for_oversized_video():
    assert T.needs_transcode(T.VideoInfo(3840, 2160, 8_000, True))


def test_needs_transcode_for_overlong_video():
    assert T.needs_transcode(T.VideoInfo(1280, 720, 40_000, True))


def test_needs_transcode_without_faststart():
    assert T.needs_transcode(T.VideoInfo(1280, 720, 8_000, False))


def test_no_transcode_when_already_compliant():
    assert not T.needs_transcode(T.VideoInfo(1280, 720, 8_000, True, 1_000_000))


# ---- 体积判据（2026-07-30 补） ----
#
# 把 TARGET_HEIGHT 从 720 提到 1080 之后，「分辨率」这一条不再拦得住手机原片
# ——手机拍的就是 1080p，只是码率高五六倍。旧判据一个字节都不看，于是这种
# 原片会被判成「已合规」原样直发：30 秒 90MB，客户端要等半分钟才出画，
# 而两端都不报错。这两条测试锁死那个洞。


def test_needs_transcode_for_a_high_bitrate_original():
    """1080p、时长合规、有 faststart，但体积是规格的六倍——必须转码。"""
    huge = T.MAX_PLAYABLE_BYTES * 6
    assert T.needs_transcode(T.VideoInfo(1920, 1080, 25_000, True, huge))


def test_playable_ceiling_is_derived_from_the_spec_constants():
    """上限必须是那几个常量算出来的，不能是手写的字面量。

    写成字面量的话，下一次改规格（这次就是从 15s/1.5M 改到 30s/4M）会留下一个
    对不上的数，而对不上是静默的：偏小 → 合规的片子被反复重转，偏大 → 放行
    超出端侧缓存预算的文件。
    """
    raw = (4000 + 128) * 1000 // 8 * 30  # 与当前常量对应的裸算术
    assert T.MAX_PLAYABLE_BYTES > raw, "要留出容器开销与 maxrate 滑窗的余量"
    assert T.MAX_PLAYABLE_BYTES < raw * 1.25, "余量不能大到放过真正超标的文件"


# ---- 编码器选择 ----


def test_auto_falls_back_to_software_when_no_render_node(monkeypatch):
    """auto 探测不到核显必须静默回退软编。

    转码慢十倍也比入库全线失败好——绝大多数开发机、以及没透核显的容器都是
    这条路径。
    """
    T.hardware_ready.cache_clear()
    monkeypatch.setattr(T, "hardware_ready", lambda *a, **k: False)
    assert T.resolve_encoder(T.ENCODER_AUTO) == T.SW_ENCODER


def test_explicit_hardware_encoder_raises_instead_of_falling_back(monkeypatch):
    """反向：显式指定硬编时探测失败要报错，不能悄悄回退。

    悄悄回退的话「以为在用硬编、其实全程软编」唯一的发现方式是掐表，
    而那要等到一万条视频跑了三天之后。
    """
    monkeypatch.setattr(T, "hardware_ready", lambda *a, **k: False)
    with pytest.raises(T.FfmpegMissing) as exc:
        T.resolve_encoder(T.HW_ENCODER)
    assert "/dev/dri" in str(exc.value)  # 出错信息要指向怎么修


def test_unknown_encoder_is_rejected():
    with pytest.raises(ValueError):
        T.resolve_encoder("h264_nvenc")


def test_hardware_ready_is_false_without_the_device_node(tmp_path):
    """设备节点不存在时不该去开 ffmpeg（也不该抛异常）。"""
    T.hardware_ready.cache_clear()
    assert T.hardware_ready(vaapi_device=str(tmp_path / "nope")) is False


def test_transcode_produces_compliant_output(tmp_path, sample_video):
    """产物自身必须合规。

    这条不变量约束了硬编怎么写：VAAPI 没有 CRF，只有 CQP 和 VBR，而 CQP 的
    体积随内容跑，噪声大的片子会冲过上限——那样产物自己不合规，下次重建时
    又会被判成要转码，转出来还是不合规。所以硬编走 VBR 并显式限 maxrate。
    """
    src = sample_video(w=3840, h=2160, seconds=8, faststart=False)
    dst = tmp_path / "out.mp4"
    T.transcode(src, dst)

    info = T.probe(dst)
    assert info.height == T.TARGET_HEIGHT
    assert info.width % 2 == 0, "H.264 要求宽度为偶数，故用 scale=-2:min(...)"
    assert info.duration_ms <= T.MAX_DURATION_MS + 500
    assert info.size_bytes <= T.MAX_PLAYABLE_BYTES
    assert T.has_faststart(dst)
    assert not T.needs_transcode(info)


def test_transcode_truncates_an_overlong_source(tmp_path, sample_video):
    """超长源要被截到 MAX_DURATION_MS，而不是原样编完。

    不截的话产物时长超标 → 产物自身不合规（见上一条），而且体积直接线性超账。
    """
    src = sample_video("long.mp4", w=1280, h=720, seconds=35, faststart=False)
    dst = tmp_path / "out.mp4"
    T.transcode(src, dst)

    info = T.probe(dst)
    assert info.duration_ms <= T.MAX_DURATION_MS + 500
    assert not T.needs_transcode(info)


def test_transcode_does_not_upscale_a_sub_720p_source(tmp_path, sample_video):
    """M11：写死目标高度（scale=-2:TARGET_HEIGHT）会把本来就矮的源放大。
    needs_transcode() 判定要不要转码还看"是否已经 faststart"、体积、时长等
    条件，跟分辨率高矮无关——一个 640x480、缺 faststart 的素材会触发转码，
    转码过程本不该把它放大（放大不增加任何真实清晰度，只是白白增加体积和
    编码时间，且违背"转码只是把不合规的素材整形，不是升清"这条隐含契约）。
    用 scale=-2:min(TARGET_HEIGHT,ih) 让目标高度不超过源高度。
    """
    src = sample_video("small.mp4", w=640, h=480, seconds=3, faststart=False)
    dst = tmp_path / "out.mp4"
    T.transcode(src, dst)

    info = T.probe(dst)
    assert info.height <= 480, f"源高度只有 480，转码后不应该被放大到 {info.height}"
    assert info.width % 2 == 0
    assert T.has_faststart(dst)
