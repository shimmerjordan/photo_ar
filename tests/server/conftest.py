"""服务端测试的公共装置。

spec §14.4 要求"完整入库→识别→解析→取流闭环，不依赖真实 NAS 或网盘"。这里
把外部依赖全部替换成假二进制（arcoreimg / ffprobe / ffmpeg），因此整套服务端
测试在任何机器上都能跑，不需要装 ARCore 工具链，也不需要 ffmpeg。假视频用
"moov 在 mdat 之前"的字节头伪造 faststart —— `transcode.has_faststart` 是真的
去读文件头的，不能靠假 ffprobe 糊过去。

HTTP 层不起端口：`app.Server.handle(Request) -> Response` 是纯函数式的，测试
直接构造 Request。真实 socket 路径由 `test_httpd.py` 单独用一个真端口验证。

## 凭证

默认凭证仍然是**运维 token**（`AUTH`），也就是 `PHOTOAR_TOKEN` 那条路 —— 它换来
一个 `role=admin` 的 Principal，所以既有的测试一行不改就仍然测的是 admin 视角。
这不是为了少改测试：那条路是 `tools/batch_ingest.py` 与 docker 健康检查在用的，
让绝大多数接口测试都跑在它上面，正好把"运维凭证仍然全权有效"这件事持续钉住。

要按具体用户测（会话 token、cookie、viewer 的授权范围）用 `Env.login()` /
`Env.admin()` / `Env.viewer()`，它们返回一个 `Creds`，传给 `request(as_=...)`。
每个测试各自 `post_json("/v1/auth/login", ...)` 一遍的话，那串 `{"name":...}` 与
"响应里 token 字段叫什么"会散在几十处，改一次得改几十次。
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

# 引导管理员。名字与口令都固定，测试才能真的登录进去（口令不给的话
# `Server._bootstrap_admin` 会生成一个随机的并只打印在日志里，测试拿不到）。
ADMIN_NAME = "管理员"
ADMIN_PASSWORD = "bootstrap-pw-0123"

# `make_env(vocab_path=...)` 的哨兵。
#
# 不能用 None 当"没传"的默认值：None 在这里是一个**有意义的取值**（"不要词表"，
# 用来测全新部署）。用 None 当默认的话，那两种情况就分不开了 —— 而它们的行为
# 完全相反（一个训词表落盘，一个刻意不落）。
_KEEP = object()


def _assert_headers_sendable(resp, method: str, path: str) -> None:
    """响应头必须能真的发出去。

    这道检查存在的原因是一个真实事故：导出接口把中文文件名放进了
    `Content-Disposition: filename="photoar-模板.xlsx"`，**整套测试全绿**，但真实
    请求一打过来服务端线程就 `UnicodeEncodeError` 崩掉。

    根因是这个测试装置与真货的差异：`Env.request` 直接调 `Server.handle()`，拿到的是
    一个 `Response` 对象；而真实路径上 `httpd._write` 会把每个头交给
    `http.server.send_header`，那个函数是拿 **latin-1** 硬编码的
    （`("%s: %s\\r\\n" % (k, v)).encode('latin-1', 'strict')`）。也就是说"能构造出来"
    和"能发出去"是两件事，而测试只验了前一件。

    放在这里而不是只在导出那几条用例里，是因为这一类 bug 与接口无关：任何一个把
    库里的中文（文件名、用户名、照片标题）拼进响应头的地方都会踩到。放在
    `Env.request` 上，全套服务端测试都在替这件事把关，包括以后新加的接口。
    """
    for key, value in resp.headers.items():
        for label, raw in (("头名", key), ("头值", value)):
            try:
                str(raw).encode("latin-1")
            except UnicodeEncodeError as exc:
                raise AssertionError(
                    f"{method} {path} 的响应{label} {raw!r} 编不进 latin-1，"
                    f"真实请求会在 http.server.send_header 里崩掉（{exc}）。"
                    "中文要走 RFC 5987 的 `filename*=UTF-8''…` 百分号编码，"
                    "或者百分号编码/转成 ASCII。"
                ) from exc


@dataclass(frozen=True)
class Creds:
    """一次登录的产物。

    同时给出 Bearer 与 cookie 两种带法，因为服务端必须两条路都认（App 用头、
    网页里的 `<img>`/`<video>` 只能用 cookie），而"两条都认"这件事只有在测试能
    分别构造出这两种请求时才钉得住。
    """

    token: str
    user_id: str | None
    role: str
    name: str

    @property
    def headers(self) -> dict[str, str]:
        """默认带法：Bearer 头。"""
        return {"authorization": f"Bearer {self.token}"}

    @property
    def cookie_headers(self) -> dict[str, str]:
        return {"cookie": f"{app.SESSION_COOKIE}={self.token}"}


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
        as_: Creds | None = None,
        cookie: bool = False,
    ) -> app.Response:
        """`as_` 给了就用那个人的会话凭证；否则 `auth=True` 用运维 token。

        `cookie=True` 把 `as_` 的凭证改成 cookie 带法。单独一个开关而不是让调用方
        自己拼 header，是为了让"这条测试测的是 cookie 那条路"在测试名之外还能从
        参数上看出来。
        """
        if as_ is not None:
            h = dict(as_.cookie_headers if cookie else as_.headers)
        else:
            assert not cookie, "cookie=True 需要配 as_（cookie 里放的是会话 token）"
            h = dict(AUTH) if auth else {}
        h.update({k.lower(): v for k, v in (headers or {}).items()})
        resp = self.srv.handle(
            app.Request(
                method=method,
                raw_path=path,
                headers=h,
                rfile=io.BytesIO(body),
                content_length=len(body),
                client="127.0.0.1",
            )
        )
        _assert_headers_sendable(resp, method, path)
        return resp

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

    def patch_json(self, path: str, obj: dict, **kw) -> app.Response:
        return self.request(
            "PATCH",
            path,
            body=json.dumps(obj).encode("utf-8"),
            headers={"content-type": "application/json"},
            **kw,
        )

    def put_json(self, path: str, obj: dict, **kw) -> app.Response:
        return self.request(
            "PUT",
            path,
            body=json.dumps(obj).encode("utf-8"),
            headers={"content-type": "application/json"},
            **kw,
        )

    # -- 凭证 --

    def login_body(self, name: str, password: str | None = None, **kw) -> dict:
        """登录并返回**整份响应体**。

        `login()` 只把它投影成 `Creds`，而有几个字段（`mustChangePassword`、
        `expiresAt`）不在那个投影里 —— 要断言它们就得拿原始 body。
        """
        doc: dict = {"name": name}
        if password is not None:
            doc["password"] = password
        resp = self.post_json("/v1/auth/login", doc, auth=False, **kw)
        assert resp.status == 200, self.body_json(resp)
        return self.body_json(resp)

    def login(self, name: str, password: str | None = None, **kw) -> Creds:
        """真的走一遍 `POST /v1/auth/login`，不是在库里伪造一行 session。

        伪造 session 能省掉一次 scrypt（约 50ms），但那样"登录"这条路径就只被专门
        测登录的那几条覆盖，其余用会话凭证的测试全都跑在一个测试自己造出来的状态
        上 —— 而登录响应的形状（token 字段名、cookie 属性）正是最容易在改动中悄悄
        变掉的东西。
        """
        body = self.login_body(name, password, **kw)
        return Creds(
            token=body["token"],
            user_id=body["userId"],
            role=body["role"],
            name=body["name"],
        )

    def admin(self) -> Creds:
        """引导管理员的一份**真实会话**凭证（区别于默认的运维 token）。"""
        return self.login(ADMIN_NAME, ADMIN_PASSWORD)

    def admin_cookie(self) -> str:
        """引导管理员登录一次，返回原始的 `Set-Cookie` 头（测 cookie 属性用）。"""
        resp = self.post_json(
            "/v1/auth/login",
            {"name": ADMIN_NAME, "password": ADMIN_PASSWORD},
            auth=False,
        )
        assert resp.status == 200, self.body_json(resp)
        return resp.headers["Set-Cookie"]

    def viewer(
        self,
        name: str = "小明",
        *,
        grant_all: bool = False,
        photo_ids: tuple[str, ...] | list[str] = (),
    ) -> Creds:
        """建一个 viewer、发好授权、登录，一步到位。

        建号走的是管理接口（用运维 token 调），不是直接 `catalog.create_user` ——
        那样会绕过 `_check_password_for_role` 之类的策略，测出来的 viewer 可能是
        管理接口根本建不出来的形状。
        """
        resp = self.post_json(
            "/v1/admin/users", {"name": name, "role": "viewer", "grantAll": grant_all}
        )
        assert resp.status == 201, self.body_json(resp)
        uid = self.body_json(resp)["id"]
        if photo_ids:
            r = self.put_json(
                f"/v1/admin/users/{uid}/grants", {"photoIds": list(photo_ids)}
            )
            assert r.status == 200, self.body_json(r)
        return self.login(name)

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

    @property
    def arcoreimg_calls_path(self) -> Path:
        """假 arcoreimg 记下的 build-db 清单日志。

        用途见 `tests/server/test_app_replace_ref.py` 里那条 imgdb 的用例：这个 fake
        产出的 .imgdb 内容与输入图无关，所以「imgdb 有没有按新图重建」只能靠「build-db
        这次拿到的清单里写的是哪张图」来验。
        """
        return self.tmp / "arcoreimg-calls.log"

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
        cookie_secure: bool = False,
        admin_password: str = ADMIN_PASSWORD,
        # 下面两个是"一键部署"那条路要测的状态，默认值保持既有行为不变。
        #
        # `vocab_path`：给 None = **完全不写这个字段、也不落盘词表**，模拟全新部署
        # （词表是用用户自己的照片训的，库空着的时候它不可能存在）。给一个字符串就是
        # 显式指定路径。默认（省略）走原来那条"训一份 ORB 词表存盘"的路。
        vocab_path: str | None | object = _KEEP,
        # `token`：给 "" = 没配运维凭证。放宽"token 必填"之后这是一个合法状态，
        # 而它必须**不能**变成一把万能钥匙。
        token: str = TOKEN,
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
        if vocab_path is _KEEP:
            descs = [
                F.extract(textured_image(seed=1000 + s, w=900, h=650)).desc
                for s in vocab_seeds
            ]
            voc = V.train(np.vstack(descs), branching=6, depth=3, seed=0)
            resolved_vocab: str | None = str(tmp_path / "vocab.npz")
            voc.save(resolved_vocab)
        else:
            # None（或显式给的路径）时**一份词表都不训**：训了再不用它，测出来的
            # "没有词表也能跑"就只是"没有引用那个变量"，而磁盘上其实躺着一份。
            resolved_vocab = vocab_path  # type: ignore[assignment]

        doc = {
            "token": token,
            "roots": {"nas": str(nas)},
            "data_dir": str(tmp_path / "data"),
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
            # 固定引导管理员，测试才登得进去。留空的话 `_bootstrap_admin` 会生成一个
            # 随机口令、只打印在日志里 —— 那正是生产环境该有的行为，但测试拿不到它。
            "admin_name": ADMIN_NAME,
            "admin_password": admin_password,
            "cookie_secure": cookie_secure,
        }
        if resolved_vocab is not None:
            doc["vocab_path"] = resolved_vocab
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
