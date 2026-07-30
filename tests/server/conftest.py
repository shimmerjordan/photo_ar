"""服务端测试的公共装置。

spec §14.4 要求"完整入库→识别→解析→取流闭环，不依赖真实 NAS 或网盘"。这里
把外部依赖全部替换成假二进制（arcoreimg / ffprobe / ffmpeg），因此整套服务端
测试在任何机器上都能跑，不需要装 ARCore 工具链，也不需要 ffmpeg。假视频用
"moov 在 mdat 之前"的字节头伪造 faststart —— `transcode.has_faststart` 是真的
去读文件头的，不能靠假 ffprobe 糊过去。

HTTP 层不起端口：`app.Server.handle(Request) -> Response` 是纯函数式的，测试
直接构造 Request。真实 socket 路径由 `test_httpd.py` 单独用一个真端口验证。
"""

import io
import json
import stat
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pytest

from photoar import features as F
from photoar import vocab as V
from photoar.server import app
from photoar.server.config import ServerConfig

TOKEN = "test-token-0123456789"
AUTH = {"authorization": f"Bearer {TOKEN}"}


# ---- 假二进制 ----


def _script(path: Path, body: str) -> str:
    path.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def fake_ffprobe(tmp_path):
    """输出固定的 720p / 3s 视频信息，让 needs_transcode() 判为不需要转码。"""

    def _make(name="ffprobe", width=1280, height=720, duration=3.0, fail=False):
        return _script(
            tmp_path / name,
            f"""
            import json, sys
            if {fail!r}:
                sys.stderr.write("boom\\n"); sys.exit(1)
            print(json.dumps({{
                "streams": [{{"codec_type": "video", "width": {width},
                              "height": {height}, "duration": "{duration}"}}],
                "format": {{"duration": "{duration}"}},
            }}))
            """,
        )

    return _make


@pytest.fixture
def fake_ffmpeg(tmp_path):
    """把输入原样"转码"成一个同样带 faststart 头的假文件。"""

    def _make(name="ffmpeg", fail=False):
        return _script(
            tmp_path / name,
            f"""
            import sys, pathlib
            if {fail!r}:
                sys.stderr.write("boom\\n"); sys.exit(1)
            out = sys.argv[-1]
            # "-" 是 stdout 而不是文件名（真 ffmpeg 的 `-f null -` 什么都不写）。
            # 照着当文件名写的话，硬编探测那条命令会在**当前工作目录**留下一个
            # 名字叫 `-` 的垃圾文件 —— 而 rm 掉它还得先想起来加 `--`。
            if out == "-" or out.startswith("pipe:"):
                sys.exit(0)
            pathlib.Path(out).write_bytes(
                b"\\x00\\x00\\x00\\x18ftypisom" + b"moov" + b"\\x00" * 64
                + b"mdat" + b"\\xab" * 4096
            )
            """,
        )

    return _make


@pytest.fixture
def fake_video(tmp_path):
    """写一个 moov 在 mdat 之前的假 mp4。has_faststart() 会认它。"""

    def _make(path: str | Path, payload: int = 4096) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"\x00\x00\x00\x18ftypisom"
            + b"moov"
            + b"\x00" * 64
            + b"mdat"
            + bytes(range(256)) * (payload // 256)
        )
        return path

    return _make


# ---- 环境 ----


@dataclass
class Env:
    tmp: Path
    nas: Path
    outside: Path
    cfg: ServerConfig
    srv: app.Server
    textured: object
    fake_video: object
    _seq: list = field(default_factory=list)

    # -- 素材 --

    def write_image(self, rel: str, seed: int = 0, w: int = 1200, h: int = 800) -> Path:
        img = self.textured(seed=seed, w=w, h=h)
        path = self.nas / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        assert cv2.imwrite(str(path), img)
        return path

    def write_video(self, rel: str) -> Path:
        return self.fake_video(self.nas / rel)

    def jpeg_of(self, img: np.ndarray, quality: int = 70) -> bytes:
        small = F.resize_to_long_edge(img, F.LONG_EDGE)
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        assert ok
        return bytes(buf.tobytes())

    # -- HTTP --

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> app.Response:
        h = dict(AUTH) if auth else {}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        return self.srv.handle(
            app.Request(
                method=method,
                raw_path=path,
                headers=h,
                rfile=io.BytesIO(body),
                content_length=len(body),
                client="127.0.0.1",
            )
        )

    def get(self, path: str, **kw) -> app.Response:
        return self.request("GET", path, **kw)

    def post_json(self, path: str, obj: dict, **kw) -> app.Response:
        return self.request(
            "POST",
            path,
            body=json.dumps(obj).encode("utf-8"),
            headers={"content-type": "application/json"},
            **kw,
        )

    def post_frame(
        self, path: str, jpeg: bytes, *, headers: dict[str, str] | None = None, **kw
    ) -> app.Response:
        boundary = "----photoartest"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode("utf-8") + jpeg + f"\r\n--{boundary}--\r\n".encode("utf-8")
        h = {"content-type": f"multipart/form-data; boundary={boundary}"}
        h.update(headers or {})
        return self.request(path=path, method="POST", body=body, headers=h, **kw)

    # -- 便捷 --

    @staticmethod
    def body_json(resp: app.Response) -> dict:
        return json.loads(resp.body.decode("utf-8"))

    @staticmethod
    def body_bytes(resp: app.Response) -> bytes:
        if resp.file is None:
            return resp.body
        out = io.BytesIO()
        app.send_file(resp, out)
        return out.getvalue()

    def ingest(
        self,
        ref: Path,
        *,
        width_mm: float = 152.0,
        video: Path | None = None,
        title: str | None = None,
    ) -> app.Response:
        doc: dict = {"refPath": str(ref), "printWidthMm": width_mm}
        if video is not None:
            doc["videoPath"] = str(video)
        if title is not None:
            doc["title"] = title
        return self.post_json("/v1/photo", doc)

    def ingest_ok(self, ref: Path, **kw) -> str:
        resp = self.ingest(ref, **kw)
        assert resp.status == 201, self.body_json(resp)
        return self.body_json(resp)["photoId"]


@pytest.fixture
def make_env(tmp_path, textured_image, fake_arcoreimg, fake_ffprobe, fake_ffmpeg, fake_video):
    """造一套完整服务环境。词汇树用合成图的真实 ORB 描述子训练。"""

    def _make(
        *,
        quality_score: int = 85,
        self_score_samples: int = 6,
        media: dict | None = None,
        upload_dir_root: str | None = None,
        vocab_seeds: range = range(8),
    ) -> Env:
        nas = tmp_path / "nas"
        (nas / "photos").mkdir(parents=True)
        (nas / "videos").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(b"not-an-image")

        # 词汇树：与 tests/test_recognizer.py 同一套参数（branching=6, depth=3），
        # 必须用真实 ORB 描述子训练——均匀随机描述子的成对距离过于集中，量化
        # 结果不稳定（见 test_vocab.py 的对照测试）。
        descs = [
            F.extract(textured_image(seed=1000 + s, w=900, h=650)).desc
            for s in vocab_seeds
        ]
        voc = V.train(np.vstack(descs), branching=6, depth=3, seed=0)
        vocab_path = tmp_path / "vocab.npz"
        voc.save(vocab_path)

        doc = {
            "token": TOKEN,
            "roots": {"nas": str(nas)},
            "data_dir": str(tmp_path / "data"),
            "vocab_path": str(vocab_path),
            "arcoreimg": fake_arcoreimg(score=quality_score),
            "ffprobe": fake_ffprobe(),
            "ffmpeg": fake_ffmpeg(),
            # 指到一个不存在的路径，让编码器解析**确定地**落到软编。
            #
            # 不指的话默认是 /dev/dri/renderD128，而那个节点在带核显或独显的
            # 开发机上是真实存在的（本机就是 nvidia 的 render 节点）—— 于是
            # transcode.hardware_ready() 会去跑假 ffmpeg，假 ffmpeg 一律退出 0，
            # 于是整套服务端测试在「有 /dev/dri 的机器」上走 h264_vaapi 分支、
            # 在没有的机器上走 libx264 分支。同一份测试在两台机器上测的是两条
            # 不同的代码路径，而且谁都不会发现 —— 直到某天只有一台机器红。
            # 硬编那条路由 tests/test_transcode.py 显式 monkeypatch 后单独测。
            "vaapi_device": str(tmp_path / "no-such-render-node"),
            "self_score_samples": self_score_samples,
        }
        if media is not None:
            doc["media"] = media
        if upload_dir_root is not None:
            doc["upload_dir_root"] = upload_dir_root
        cfg = ServerConfig.from_dict(doc)
        return Env(
            tmp=tmp_path,
            nas=nas,
            outside=outside,
            cfg=cfg,
            srv=app.Server.create(cfg),
            textured=textured_image,
            fake_video=fake_video,
        )

    return _make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()
