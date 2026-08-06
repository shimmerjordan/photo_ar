"""服务配置。JSON 文件 + 环境变量覆盖，或者**纯环境变量**（`from_env`）。

token 支持从环境变量取（`PHOTOAR_TOKEN`），优先级高于配置文件：容器镜像里
不该躺着一个明文预共享 token，而 QNAP 的 Container Station 能直接注入环境变量。
引导管理员的口令（`PHOTOAR_ADMIN_PASSWORD`）同理，而且更甚 —— 那是一个人用的
口令，写进配置文件就等于写进备份和 git。

这里放的都是"改它需要重新决定进程启动时做过的事"的参数，能在运行时改的那些在
`appconfig.py`（分界线写在那边的模块 docstring 里）。

## 为什么有 `from_env`（而不是只让 compose 生成一份 config.json）

一键部署的目标是 `docker compose up -d` 就能起。让 compose 里的 entrypoint 去
**生成**一份 config.json 也能达到这个效果，但那样磁盘上就有了两份真相：用户改了
`.env` 里的变量，容器里那份生成出来的 json 要么被覆盖（用户手工改过的内容丢了）、
要么不被覆盖（改 `.env` 没有任何效果）。两种都很难解释。所以环境变量是一条**独立
且完整**的配置路径：给了 `-c` 且文件存在就读文件，否则全部从环境变量来。

## 为什么 token 不再是必填

它曾经是唯一一层鉴权，所以"没设 token"等于"对隧道全网开放"，必须拒绝启动。现在
用户体系（`auth.py`）接管了鉴权：每个人一个账号，`PUBLIC_PATHS` 只有登录一条。
`ServerConfig.token` 退化成"给机器用的运维凭证"，而 `Auth` 那三处 legacy 分支都写成
`if self._legacy_token and ...` —— 空 token 让那条分支**整体禁用**，不是"谁都通过"
（`tests/server/test_auth.py::test_empty_legacy_token_is_not_a_credential` 钉住了
这一点，`test_app_auth.py` 里还有一条走完整 HTTP 栈的）。所以留空是安全的，代价只是
批量入库脚本和健康检查用不了它。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import backend as backend_mod
from .. import transcode, xfeat
from ..quality import ARCOREIMG
from .mediaresolve import DEFAULT_STRATEGIES

DEFAULT_PORT = 8964  # spec §9.1 的 LAN endpoint 用的端口

# `/v1/recognize` 的请求体上限。spec §7 说 frame 是长边 640px q70 的 JPEG，
# 约 50KB。给 40 倍余量挡住误发原图（4000 万像素手机原图约 8-15MB），同时
# 不至于把一张稍大的帧判成攻击。上传接口另有自己的上限。
MAX_RECOGNIZE_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024

# 走 Cloudflare 隧道时的上传上限。**只对隧道生效**，LAN / Tailscale 仍是
# `MAX_UPLOAD_BYTES`。
#
# 这里最初写的是"隧道上传一律拒"，理由是 App 那边上传的都是几百 MB 的视频，走隧道
# 必挂。网页版把这条前提推翻了：网页的**正常访问路径就是隧道**，而婚礼现场随手挑的
# 一张照片加一段短视频通常只有几十 MB —— 一律拒等于网页上传功能整个不存在。
# 所以判据从"来路"改成"体积"。
#
# 95MiB ≈ 99.6MB，卡在 Cloudflare 那个"100MB"的两种读法（10^8 与 2^20×100）之下。
# 宁可我们先拒、把话说清楚，也不要让 Cloudflare 在传到一半时掐断 —— 它掐断时返回的
# 是一张没有上下文的错误页，用户只会看到"上传失败"。
TUNNEL_MAX_UPLOAD_BYTES = 95 * 1024 * 1024

# `POST /v1/recognize/features`（端上提特征）的请求体上限，**单独一条**。
#
# 不能沿用 MAX_JSON_BYTES（64KB）：一个完全合法的请求就有约 180KB，那条上限会把这个
# 接口的每一次调用都拒掉 —— 而且拒的是 413，看起来像"客户端发了个巨大的东西"。
# 也不该沿用 MAX_RECOGNIZE_BYTES（2MB）：那个数是给"用户误发了原图"留的 40 倍余量，
# 而这里的体积由 `xfeat.TOP_K` **完全确定**，收紧到刚够用是免费的。
#
# 明细：关键点 512×2×4 = 4KB，描述子 512×64×4 = 128KB，共 132KB；base64 膨胀 4/3
# 后约 176KB，再加 JSON 键名与转义。取 2 倍余量，跟着 TOP_K 自动变。
MAX_FEATURES_BYTES = 2 * (xfeat.TOP_K * (2 + xfeat.DESC_DIM) * 4 * 4 // 3 + 4096)

# 算自匹配分用的扰动样本数。`dedup.self_score` 取中位数，样本太少中位数不稳；
# 20 与 §14.1 的回归测试同一个数，也是 0d 全部实测数字的来源。每张约 1s。
SELF_SCORE_SAMPLES = 20

# 引导管理员的默认名字。名字可以有默认值（它不是秘密），口令不行 —— 见
# `app.Server._bootstrap_admin` 里那段"为什么不能有固定默认口令"。
DEFAULT_ADMIN_NAME = "admin"


class ConfigError(ValueError):
    pass


def _env_flag(name: str, default: bool) -> bool:
    """环境变量当布尔用。

    空字符串视为"没设"而不是 False：`docker-compose.yml` 里写
    `PHOTOAR_COOKIE_SECURE: ${COOKIE_SECURE}` 而外面那个变量没定义时，容器里拿到
    的就是空串。按 False 处理的话，它会静默盖掉配置文件里显式写的 true。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_path(name: str) -> Path | None:
    """环境变量当路径用。空串视为"没设"，理由同 `_env_flag`。"""
    raw = os.environ.get(name)
    return Path(raw.strip()).expanduser() if raw and raw.strip() else None


def parse_roots(raw: str) -> dict[str, str]:
    """`PHOTOAR_ROOTS` 的解析：`名字=路径` 逗号分隔，也接受纯路径列表。

    两种写法都支持，因为它们对应两种真实的心智模型：
    - `photos=/share/Photo,video=/share/Video` —— 名字会显示在 App 的目录浏览器里，
      想让它是中文/自定义的人必须能指定。
    - `/share/Photo,/share/Video` —— "我就挂这两个目录"，此时拿目录名当键
      （`Photo` / `Video`）。不支持这种写法的话，`.env` 里最常见的那个写法会静默
      得到一个空 roots，然后服务报"必须配置至少一个白名单根目录"，而用户明明配了。

    重名（`/a/Photo` 与 `/b/Photo` 都取 `Photo`）时**报错而不是后者覆盖前者**：
    覆盖的后果是其中一个目录整体访问不到，而 `/v1/fs/list` 上只是少了一项，
    没有任何地方会说"你有两个根撞名了"。
    """
    roots: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        name, sep, path = item.partition("=")
        if sep:
            name, path = name.strip(), path.strip()
        else:
            # 纯路径：拿目录名当键。`/share/Photo/` 的 Path.name 是 "Photo"
            # （Path 会吃掉尾随斜杠），所以不必自己 rstrip。
            path = item
            name = Path(path).name or path
        if not path:
            raise ConfigError(f"PHOTOAR_ROOTS 里这一项没有路径：{item!r}")
        if name in roots and roots[name] != path:
            raise ConfigError(
                f"PHOTOAR_ROOTS 里有两个根都叫 {name!r}（{roots[name]} 与 {path}）。"
                f"后者覆盖前者的话，其中一个目录会整体访问不到而没有任何提示，"
                f"所以这里拒绝 —— 用 `名字=路径` 的写法各给一个名字。"
            )
        roots[name] = path
    return roots


@dataclass
class ServerConfig:
    token: str
    roots: dict[str, str]
    data_dir: Path
    # 词表路径。**可选** —— 全新部署时它必然还不存在（词表是用用户自己的照片训的，
    # 而库是空的）。None 时按后端取 `models_dir / backend.vocab_file`，文件不在就用
    # `NullVocab`（推理见 `photoar.nullvocab`）。
    vocab_path: Path | None = None
    # 模型与词表的目录。XFeat 的 onnx、两种后端的词表都落在这里。
    #
    # 与 data_dir 分开是为了让它能挂成一个**共享**卷：同一份 xfeat.onnx 可以给几个
    # 部署用，而 data_dir 是这一个部署独有的（库、SQLite、转码产物）。默认落在
    # data_dir 下面，这样"只挂一个卷"仍然能跑（`xfeat.default_model_path` 的两级回退
    # 也是这个约定）。
    models_dir: Path | None = None
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
    # 会话 cookie 要不要带 Secure 属性。**默认关**，理由写在
    # `app.Server._session_cookie` 里：部署形态同时有局域网 http 直连和
    # Cloudflare 隧道的 https，写死 Secure 会让前者登录后一刷新就掉线。
    cookie_secure: bool = False
    # 引导管理员。库里一个 admin 都没有时按这两个字段建一个（见
    # `app.Server._bootstrap_admin`）。口令留空 = 启动时生成随机口令并打印一次。
    admin_name: str = DEFAULT_ADMIN_NAME
    admin_password: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.db"

    @property
    def library_dir(self) -> Path:
        """ORB 后端的库目录。保持 `data_dir/library` 不变 —— 已有部署的库在这里，
        换个名字等于让它们全部"照片不见了"（catalog 里还有，识别库空了）。"""
        return self.library_dir_for(backend_mod.ORB)

    def library_dir_for(self, backend_name: str) -> Path:
        """这个后端的库目录。

        **两个后端必须分开**：desc.bin 的 slot 步长与 dtype 由后端定（ORB 12,008
        字节/张 uint8，XFeat 135,176 字节/张 float32）。混用不会读出"错误的照片"这么
        温和的结果 —— `DescStore` 会按新步长去切旧文件，读出来的 count 是别人描述子
        中间的四个字节，可能是任意大的数，然后按它去 reshape。真正危险的是 slots.json
        仍然对得上条数（`_assert_aligned` 过得去），所以没有任何一步会报错。

        ORB 保持历史路径（见上面那个 property），其余后端加后缀。
        """
        if backend_name == backend_mod.ORB:
            return self.data_dir / "library"
        return self.data_dir / f"library_{backend_name}"

    @property
    def model_dir(self) -> Path:
        return self.models_dir or (self.data_dir / "models")

    @property
    def xfeat_model_path(self) -> Path:
        return self.model_dir / "xfeat.onnx"

    def vocab_path_for(self, backend_name: str, vocab_file: str) -> Path:
        """这个后端的词表落在哪。

        显式配了 `vocab_path` 时**只对 ORB 生效**。那个字段是"后端只有 ORB 一种"的
        年代定下的，已有部署里它指向的一定是一份二进制词表（`vocab.Vocab` 存的 npz，
        键名是 centers/children_flat/... 那一套）。把它当成 XFeat 的词表交给
        `floatvocab.FloatVocab.load` 会 KeyError —— 那还算好的失败；真正要防的是
        两种格式的键名将来恰好对得上，那时读出来的是一棵毫无意义的树，粗排召回静默
        崩塌，而配置文件里那一行看起来完全正常。

        所以换后端就是换词表文件（`vocab.npz` / `vocab_xfeat.npz`，名字由后端自己
        声明），而不是换配置。
        """
        if backend_name == backend_mod.ORB and self.vocab_path is not None:
            return self.vocab_path
        return self.model_dir / vocab_file

    @property
    def imgdb_dir(self) -> Path:
        return self.data_dir / "imgdb"

    @property
    def targets_dir(self) -> Path:
        """整库多目标 `.imgdb` 的落地目录（`server/targets.py`）。

        与 `imgdb_dir` 分开：那里一张照片一个文件、文件名是 photo_id、生命周期跟着
        照片；这里一个**授权集**一个文件、文件名是内容哈希、生命周期是"最近几个
        版本"（会被主动清理）。混在一个目录里的话，那个清理逻辑就得学会区分两种
        命名，而它删错的后果是把某张照片的单目标库删掉 —— 表现是那张照片进入
        AR 之后跟踪不上，而 catalog 里一切正常。
        """
        return self.data_dir / "targets"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumb"

    @property
    def playable_dir(self) -> Path:
        return self.data_dir / "playable"

    def ensure_dirs(self) -> None:
        # model_dir 也建出来：`build-vocab` 要往里写词表，而它可能在词表还不存在
        # （= 目录也不存在）时被调用。不建的话那次训练会在最后一步 save 失败，
        # 而训练本身可能已经跑了几分钟。
        for d in (
            self.data_dir, self.library_dir, self.imgdb_dir, self.targets_dir,
            self.thumb_dir, self.playable_dir, self.model_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_dict(cls, doc: dict[str, Any], *, base: Path | None = None) -> "ServerConfig":
        def rel(p: str) -> Path:
            path = Path(p).expanduser()
            return path if path.is_absolute() or base is None else (base / path)

        # token **不再必填**（理由见模块 docstring 最后一节：空 token 让 legacy 分支
        # 整体禁用，鉴权由用户体系接管）。留空只影响机器调用方，所以打一条警告而不是
        # 拒绝启动 —— 那条警告要能让人在"批量入库脚本全部 401"的时候想起这里。
        token = os.environ.get("PHOTOAR_TOKEN") or doc.get("token") or ""
        if not token:
            print(
                "[photoar] ⚠️ 没有配 PHOTOAR_TOKEN（运维凭证）。人走 /admin 登录不受"
                "影响；但 tools/batch_ingest.py 与容器健康检查用的是它，会全部 401。"
                "要用就设一个：openssl rand -hex 24",
                flush=True,
            )
        roots = dict(doc.get("roots") or {})
        if not roots:
            raise ConfigError("必须配置至少一个白名单根目录 roots")
        for name, p in roots.items():
            if not str(p).startswith("/"):
                raise ConfigError(f"白名单根目录必须是绝对路径：{name}={p!r}")

        # 上传落地目录不在任何白名单根内时，**自动把它收编成一个根**，并把这件事
        # 大声说出来。只警告不收编是不够的：`/v1/upload` 落地前要过 SafeRoots 校验，
        # 不在白名单内的表现是**每一次上传都 403 path_denied**，而 403 的响应体刻意
        # 不回显路径（免得变成探测工具）—— 用户看到的只有"传不上去"，什么线索都没有。
        # 真实部署踩过：roots 配了 photos/videos 两个根，PHOTOAR_UPLOAD_DIR 指了它们的
        # **兄弟目录** inbox，于是整个网页上传功能对这套部署从没工作过。
        #
        # 收编而不是拒绝启动：配了上传目录的人的意图明确就是"要用上传"，替他把意图
        # 补全比让服务起不来好。resolve() 双边解析是跟着 SafeRoots 的语义走的 ——
        # 它在构造时也 resolve（QNAP 上 /share 常是符号链接）。
        upload_dir = str(doc.get("upload_dir_root") or "").strip()
        if upload_dir.startswith("/"):
            up = Path(upload_dir).resolve()
            covered = any(
                up == Path(str(p)).resolve() or up.is_relative_to(Path(str(p)).resolve())
                for p in roots.values()
            )
            if not covered:
                name = "upload"
                n = 2
                while name in roots:
                    name, n = f"upload{n}", n + 1
                roots[name] = upload_dir
                print(
                    f"[photoar] ⚠️ 上传落地目录 {upload_dir} 不在任何白名单根内，"
                    f"已自动收编为根 '{name}'。不收编的话所有上传都会 403。"
                    f"想让这条警告消失：把它显式写进 roots（PHOTOAR_ROOTS），"
                    f"或把上传目录挪到某个已有根的下面。",
                    flush=True,
                )
        if "data_dir" not in doc:
            raise ConfigError("必须配置 data_dir（转码产物与索引都写在这里）")
        # vocab_path **可选**：全新部署时那个文件必然不存在（词表要用库里的描述子训，
        # 而库是空的）。缺了就按后端取默认路径，文件不在时用 NullVocab 并在启动日志里
        # 明确警告 —— 见 `app.Server._open_library`。
        media = doc.get("media") or {}
        return cls(
            token=str(token),
            roots={str(k): str(v) for k, v in roots.items()},
            data_dir=rel(str(doc["data_dir"])),
            vocab_path=(
                rel(str(doc["vocab_path"])) if doc.get("vocab_path") else None
            ),
            models_dir=(
                rel(str(doc["models_dir"])) if doc.get("models_dir") else None
            ),
            # bind 与 port 都让环境变量优先于文件。port 一直是这样；bind 是
            # 2026-08-05 补上的 —— 合并容器之后 entrypoint 要把后端按到回环上
            # （`PHOTOAR_BIND=127.0.0.1`），而挂了 config.json 的部署原先会忽略它，
            # 于是后端在容器网络里仍然是 0.0.0.0：前面那层反代就绕得过去了。
            bind=str(os.environ.get("PHOTOAR_BIND") or doc.get("bind", "0.0.0.0")),
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
            cookie_secure=_env_flag(
                "PHOTOAR_COOKIE_SECURE", bool(doc.get("cookie_secure", False))
            ),
            # `.strip() or 默认`：`PHOTOAR_ADMIN_NAME=" "` 是真空格（比如 compose 里
            # 引号没对齐），它是 truthy 的，会一路传到 `auth.check_name` 抛
            # `InvalidName` —— 于是服务**起不来**，而原因是一个看不见的空格。
            admin_name=str(
                os.environ.get("PHOTOAR_ADMIN_NAME")
                or doc.get("admin_name")
                or DEFAULT_ADMIN_NAME
            ).strip()
            or DEFAULT_ADMIN_NAME,
            # 与 token 同一个理由，只是更强：这是一个人要输进去的口令，写进配置
            # 文件就等于写进 git 和每一份备份。所以环境变量优先，且不给默认值。
            admin_password=str(
                os.environ.get("PHOTOAR_ADMIN_PASSWORD") or doc.get("admin_password") or ""
            ),
            extra={
                k: v
                for k, v in doc.items()
                if k
                not in {
                    "token", "roots", "data_dir", "vocab_path", "models_dir",
                    "bind", "port",
                    "arcoreimg", "ffmpeg", "ffprobe", "media", "self_score_samples",
                    "upload_dir_root", "version",
                    "video_encoder", "video_preset", "vaapi_device",
                    "cookie_secure", "admin_name", "admin_password",
                }
            },
        )

    @classmethod
    def load(cls, path: str | Path) -> "ServerConfig":
        path = Path(path)
        doc = json.loads(path.read_text("utf-8"))
        return cls.from_dict(doc, base=path.parent)

    @classmethod
    def from_env(cls) -> "ServerConfig":
        """完全从环境变量构造。`docker compose up -d` 这条路走的就是它。

        实现方式是"把环境变量翻译成 `from_dict` 吃的那个 doc"，而不是另写一遍字段
        赋值：`from_dict` 里已经有一批只此一份的规则（roots 必须是绝对路径、
        admin_name 的空格处理、cookie_secure 的空串语义、extra 的兜底）。抄第二遍的
        后果不是"多写几行"，而是两条配置路径的校验强度不一样 —— 而生产上跑的恰好是
        这条没被测试覆盖那么多次的路。

        `PHOTOAR_BACKEND` **不在这里**处理：它要写进 `app_config` 表的初始值，那是
        数据库里的东西，`ServerConfig` 不该碰库。见 `app.Server._seed_backend`。
        """
        roots_raw = os.environ.get("PHOTOAR_ROOTS") or ""
        roots = parse_roots(roots_raw)
        if not roots:
            raise ConfigError(
                "必须设 PHOTOAR_ROOTS（白名单根目录）。写法两种："
                "`photos=/share/Photo,video=/share/Video` 或 "
                "`/share/Photo,/share/Video`（自动取目录名当名字）。"
                "路径是**容器内**的路径，要与 compose 里的挂载点一致。"
            )
        data = _env_path("PHOTOAR_DATA") or Path("/data")
        doc: dict[str, Any] = {
            "roots": roots,
            "data_dir": str(data),
            "bind": os.environ.get("PHOTOAR_BIND") or "0.0.0.0",
        }
        # 下面这些**只在真的设了的时候**才写进 doc。写 `doc[k] = os.environ.get(k)`
        # 的话，没设的变量会变成 None / ""，把 from_dict 里那些 `doc.get(k, 默认值)`
        # 的默认值全部盖掉 —— 比如 video_encoder 会从 "auto" 变成 ""，
        # 然后 transcode.resolve_encoder 抱怨一个用户从没配过的编码器。
        for env, key in (
            ("PHOTOAR_VOCAB", "vocab_path"),
            ("PHOTOAR_MODELS", "models_dir"),
            ("PHOTOAR_VIDEO_ENCODER", "video_encoder"),
            ("PHOTOAR_UPLOAD_DIR", "upload_dir_root"),
        ):
            raw = os.environ.get(env)
            if raw and raw.strip():
                doc[key] = raw.strip()
        # token / port / admin_name / admin_password / cookie_secure 不用在这里写：
        # from_dict 自己就读那几个环境变量（而且优先级高于 doc）。
        return cls.from_dict(doc)
