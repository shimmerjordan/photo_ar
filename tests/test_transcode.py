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
    assert T.needs_transcode(T.VideoInfo(1920, 1080, 8_000, True))


def test_needs_transcode_for_overlong_video():
    assert T.needs_transcode(T.VideoInfo(1280, 720, 20_000, True))


def test_needs_transcode_without_faststart():
    assert T.needs_transcode(T.VideoInfo(1280, 720, 8_000, False))


def test_no_transcode_when_already_compliant():
    assert not T.needs_transcode(T.VideoInfo(1280, 720, 8_000, True))


def test_transcode_produces_compliant_output(tmp_path, sample_video):
    src = sample_video(w=1920, h=1080, seconds=20, faststart=False)
    dst = tmp_path / "out.mp4"
    T.transcode(src, dst)

    info = T.probe(dst)
    assert info.height == T.TARGET_HEIGHT
    assert info.width % 2 == 0, "H.264 要求宽度为偶数，故用 scale=-2:720"
    assert info.duration_ms <= T.MAX_DURATION_MS + 500
    assert T.has_faststart(dst)
    assert not T.needs_transcode(info)


def test_transcode_does_not_upscale_a_sub_720p_source(tmp_path, sample_video):
    """M11：scale=-2:720 是绝对目标高度，遇到本来就矮于 720p 的源也会一样
    被放大到 720。needs_transcode() 判定要不要转码只看"是否已经 faststart"
    等条件，跟分辨率无关——一个 640x480、缺 faststart 的素材会触发转码，
    转码过程本不该把它从 480 放大到 720（放大不增加任何真实清晰度，只是
    白白增加体积和编码时间，且违背"转码只是把不合规的素材整形，不是升清"
    这条隐含契约）。用 scale=-2:min(720,ih) 代替 scale=-2:720，让目标高度
    不超过源高度。
    """
    src = sample_video("small.mp4", w=640, h=480, seconds=3, faststart=False)
    dst = tmp_path / "out.mp4"
    T.transcode(src, dst)

    info = T.probe(dst)
    assert info.height <= 480, f"源高度只有 480，转码后不应该被放大到 {info.height}"
    assert info.width % 2 == 0
    assert T.has_faststart(dst)
