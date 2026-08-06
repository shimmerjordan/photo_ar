"""路由、鉴权与 spec §7 的全部接口。

这一层刻意不依赖 `http.server`：`handle(Request) -> Response` 是纯函数式的，
`httpd.py` 才负责把 socket 上的字节变成 `Request`。好处是集成测试可以直接
构造 `Request` 调 `handle`，不需要起端口、不需要真网络 —— spec §14.4 要求的
"完整入库→识别→解析→取流闭环，不依赖真实 NAS 或网盘"因此能全程离线跑完。

URL 一律返回**相对路径**（spec §7）：服务端不知道客户端此刻走的是 LAN、
Tailscale 还是隧道，返回绝对 URL 会把客户端锁死在一条通道上。唯一例外是
`via == "direct_link"` 的网盘 CDN 地址，由 `via` 字段明确区分。

## 身份怎么流过这一层

`_dispatch` 认完凭证就拿到一个 `auth.Principal`，然后把它作为**第二个位置参数**
传给每个处理器。刻意不挂在 `Request` 上（也不放 threading.local）：Request 是个
可变 dataclass，任何一层"顺手改一下 principal"都是权限提升，而它会长得像一次
无害的字段赋值。作为参数传的话，一个处理器想拿到别人的身份必须显式地把它传进去。

授权判定分三处，各自的边界要分清：
- `auth.photo_filter(principal)` —— "谁能看全部照片"这条策略的唯一实现处。
- `Server._may_see(principal, photo_id)` —— 单张照片的可见性，内部走上面那个。
- `Server._require_admin(...)` —— 写操作与管理接口，逐个处理器自己声明。
"""

import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import backend as backend_mod
from .. import sheet as sheet_mod
from .. import streak, verify, xfeat
from ..nullvocab import NullVocab
from ..sheet import SheetError
from . import (
    batch,
    featurebody,
    framedump,
    fsbrowser,
    integrity,
    ingest,
    mediaresolve,
    targets,
)
from .appconfig import AppConfig, ConfigRejected
from .auth import (
    ADMIN,
    ROLES,
    VIEWER,
    AccountDisabled,
    Auth,
    BadCredentials,
    InvalidName,
    Principal,
    UnknownUser,
    check_name,
    normalize_name,
    photo_filter,
    verify_password,
)
from .config import (
    MAX_FEATURES_BYTES,
    MAX_JSON_BYTES,
    MAX_RECOGNIZE_BYTES,
    MAX_UPLOAD_BYTES,
    TUNNEL_MAX_UPLOAD_BYTES,
    ServerConfig,
)
from .db import (
    MOUNT_KINDS,
    MOUNT_LOCAL,
    MOUNT_WEBDAV,
    Catalog,
    NameTaken,
    effective_fit_mode,
    ref_aspect,
)
from .integrity import sha256_file
from .webdav import WebDavClient, WebDavError
from .library import EmptyLibrary, PhotoLibrary
from .multipart import MultipartError, boundary_of, parse_multipart
from .ranges import ByteRange, RangeNotSatisfiable, parse_range
from .safepath import PathDenied, Roots

_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# 会话 cookie 的名字。浏览器那一侧唯一能用的凭证载体，理由见 `_session_cookie`。
SESSION_COOKIE = "photoar_session"

# 鉴权之前就能到达的 `/v1/*` 路径。
#
# **一个显式的常量集合**，而不是散在 `_dispatch` 里的几个 if：这份名单是整个服务
# 的攻击面清单，得能一眼看完、也得能被测试直接读。写成 if 的话，以后有人为了
# "让健康检查不用带 token"加一条 `or path.startswith("/v1/pub")`，没有任何地方
# 会显示出免鉴权路径变多了。
#
# 只有登录在里面：没有它谁都登不进来。`logout` 与 `me` 都要鉴权 —— logout 得知道
# 作废哪个 token，me 的全部内容就是"你是谁"。
PUBLIC_PATHS = frozenset({"/v1/auth/login"})

#: 引导管理员的**固定初始口令**。
#:
#: ## 这一条推翻了原来的设计，理由和代价都写在这里
#:
#: 原来是「不给 `PHOTOAR_ADMIN_PASSWORD` 就生成随机口令、在启动日志里打印一次」。
#: 那个设计在安全上更好，但它有一个**在真实部署里致命**的失败模式，2026-08-06 撞上了：
#:
#: 那行只在**真的建出账号时**打印一次（已经有 admin 就一个字都不输出）。而
#: `docker compose up -d` 重建容器会**清空旧容器的日志** —— 于是「库里已有 admin
#: + 日志已被清」这个组合一旦出现，口令就永久拿不回来了：设 `PHOTOAR_ADMIN_PASSWORD`
#: 不管用（已有 admin 就不动它），`photoar-server` 也没有重置口令的子命令，
#: 唯一出路是进 SQLite 手动 `delete from user`。这不是理论风险，是实际发生的。
#:
#: 所以改成固定初始口令，代价用**强制改密**来抵：`/auth/login` 与 `/auth/me` 都会
#: 回一个 `mustChangePassword`，管理台见到它就把整个界面锁住、只留改密表单
#: （见 `_using_default_password`）。也就是说 `admin/admin` 只在「部署完成」到
#: 「你第一次登录」这个窗口里有效。
#:
#: ⚠️ **那个窗口是真实的暴露**。这个服务挂在公网隧道后面、没有 Cloudflare Access、
#: 登录也没有速率限制，而这个默认值就印在这份公开源码里。所以：
#:   * 部署完**立刻**登录改掉，别放着过夜；
#:   * 更好的做法是一开始就设 `PHOTOAR_ADMIN_PASSWORD`，那样这个默认值根本不会被用到
#:     （设了它就不会走到这里，而且登录时也不会触发强制改密）。
#:
#: ## 强制改密**只拦在管理台前端**，这是刻意的
#:
#: 服务端没有"用默认口令的会话只能调改密接口"那种拦截。看起来像是漏了一层，其实
#: 加上也**不提供任何保护**：抢先用 `admin/admin` 登进来的人，第一件事就是改密 ——
#: 而那恰好是唯一被允许的操作。拦完的结果是他把口令改成自己的、你被锁在外面，
#: 比不拦更糟。
#:
#: 所以这一层的定位是**防遗忘，不是防攻击**：它保证善意的部署者不会"登进去看一眼
#: 就忘了改"。真正的防线只有两条 —— 尽快改掉，或者一开始就设
#: `PHOTOAR_ADMIN_PASSWORD`。别把前端这个锁当成安全措施。
DEFAULT_ADMIN_PASSWORD = "admin"

#: 每条识别记录里存前几名候选。
#:
#: 5 而不是 `recog.top_k`（20）：`ambiguous` 只由前两名决定，第三名之后是给
#: 「到底有几张长得像」用的旁证。存 20 会让这张表在一次几百帧的扫描后膨胀十几倍，
#: 而多出来的那 15 条一次都没被看过。
DEDUP_LOG_TOP_K = 5

# 管理台静态页面的目录。放在包内（而不是 data_dir）是因为它属于代码而不是数据：
# 换一个版本的页面靠换镜像，不该靠往数据卷里拷文件。
WEBUI_DIR = Path(__file__).resolve().parent / "webui"

# 管理台文件名的白名单。见 `_route_webui` 里为什么它必须排在路径解析之前。
#
# 首字符不许是点：这样 `.`、`..`、`.env`、`.git` 之类一次全挡掉。只写
# `[A-Za-z0-9._-]+` 是不够的 —— `..` 完全符合那个模式。
_WEBUI_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")

# 管理台的分区。`/admin/<这里的名字>` 都返回首页，由前端按 `location.pathname`
# 决定打开哪一个（见 `_route_webui`）。
#
# ⚠️ 这张清单必须与 `webui/app.js` 里的 `TABS` **一致**。多了一项：那个 URI 打得开
# 但前端不认，会回落到默认分区（能用，只是地址栏和内容对不上）。少了一项：那个 URI
# 刷新时 404 —— 而「刷新」正是独立 URI 最主要的用途。
_WEBUI_TABS = frozenset({"users", "grants", "config", "photos", "batch"})


def _rel_to(base: Path, target: Path) -> str:
    """`target` 相对 `base` 的路径。相同就是空串（= 挂载点根）。

    给管理台用：那边只该知道「在这个挂载点的哪一层」，不需要也不该拿到服务端的绝对
    路径（那是部署细节，而且在界面上很长很难读）。
    """
    if target == base:
        return ""
    try:
        return str(target.relative_to(base))
    except ValueError:
        # 到不了：调用方给的路径已经过 `roots.resolve` 且是从 base 拼出来的。
        # 真出现说明 base 本身不在白名单里（挂载点被删了但没重建 roots），
        # 那时给绝对路径比给一个错的相对路径好。
        return str(target)


def _safe_upload_name(raw: str) -> str:
    """把一个**远端给的**文件名清成能落地的纯文件名。

    注意这和 `_upload` 里那段是**刻意不同的两种策略**，不是重复实现：

    - `_upload` 收到不合规的名字直接 **400 拒掉**。那条路上名字由客户端指定，让它改比
      替它改好 —— 静默重命名会让文件落在一个客户端不知道的名字上，而客户端接下来要拿
      这条路径去入库。
    - 这里是**清洗**。名字来自 WebDAV 服务端，我们控制不了它（可能带路径、可能以点
      开头、可能是 Windows 风格的反斜杠路径）。拒掉的话用户唯一的出路是去改远端的
      文件名，而那常常不是他能改的。

    把它们统一成一种是错的：要么接口开始悄悄改名，要么 WebDAV 在一个我们无权修改的
    文件名上永久失败。
    """
    base = Path(raw).name.strip().lstrip(".")
    # 路径分隔符在 Path().name 之后就没了，但 Windows 风格的反斜杠在 posix 上不算
    # 分隔符，所以再切一次。
    base = base.rsplit("\\", 1)[-1]
    return base or "download.bin"


def _suggest_name(name: str) -> str:
    """同名不同内容时，给一个不会撞的建议名：`a.jpg` → `a-2.jpg`。

    只加一个 `-2` 而不是拼时间戳或随机串：这个名字是要给人看、让人认出「哦这是
    第二张」的，而 `a-20260803T142233.jpg` 只会让相册里多一个读不出来的文件名。
    真的撞到第二次时用户会再改一次 —— 那比一个必然唯一但不可读的名字好。
    """
    p = Path(name)
    return f"{p.stem}-2{p.suffix}"

_WEBUI_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}

# 客户端可以用这个头告诉服务端它此刻走的是哪个 endpoint，只进 recognize_log
# 的 `via` 列（spec §6 的注释："命中时客户端用的 api endpoint 名"）。服务端
# 自己推断不出来 —— 隧道、Tailscale、LAN 到这里都是一个 TCP 连接。
ENDPOINT_HEADER = "x-photoar-endpoint"

# cloudflared 一定会加的头。用来判断这个请求是不是从隧道进来的 —— 隧道有请求体
# 上限（见 `TUNNEL_MAX_UPLOAD_BYTES`），超了要在**我们这里**拒并说清原因，
# 而不是让 Cloudflare 在传到一半时掐断（它只会给一张没有上下文的错误页）。
_TUNNEL_HEADERS = ("cf-ray", "cf-connecting-ip")


def _via_tunnel(req: "Request") -> bool:
    return any(req.header(h) for h in _TUNNEL_HEADERS)


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


def _cookie_value(header: str | None, name: str) -> str:
    """从 `Cookie` 请求头里取一个值。取不到返回空串。

    手写而不用 `http.cookies.SimpleCookie`：那个类的职责是"实现 cookie 的全部
    语义"，包括对带引号的值做 unquote、把 `\\073` 之类的转义还原。我们要的只是
    "按名字取出一个字符串，原样"，而这个字符串接下来要参与一次定长时间的密钥
    比较 —— 任何"值经过解析会变形"的可能性在这里都是纯风险，换不到任何东西
    （会话 token 是 `token_urlsafe`，字符集里没有需要转义的字符）。

    重复的同名 cookie 取第一个。浏览器在正常情况下不会发两个同名的，会发是因为
    同一个名字在不同 Path/Domain 上各存了一份（比如先在 `/admin` 下签过一次），
    此时按浏览器自己的规则更具体的 Path 会排在前面，取第一个正好。
    """
    for chunk in (header or "").split(";"):
        key, sep, value = chunk.partition("=")
        if sep and key.strip() == name:
            return value.strip()
    return ""


class Server:
    def __init__(
        self,
        cfg: ServerConfig,
        catalog: Catalog,
        library: PhotoLibrary,
        roots: Roots,
        resolver: mediaresolve.MediaResolver,
        # `auth` 与 `config` 没有默认值，虽然"没给就按默认参数造一个"会让调用点
        # 短一行：那个默认会造出一个 TTL 是代码常量（而不是热配置）、legacy_token
        # 为空（于是批量入库脚本全部 401）的 Auth，而它看起来完全正常，只在真的
        # 有人登录或跑脚本时才表现出来。让构造点必须显式给。
        auth: Auth,
        config: AppConfig,
        # 整库多目标 `.imgdb` 的缓存。**这个**可以有默认值（与上面那两个不同）：
        # 它没有任何外部配置，全部输入就是同一个 cfg/catalog/config，所以"自己造
        # 一个"和"外面造一个传进来"必然等价 —— 不存在造出一个看起来正常、行为不同
        # 的实例这种风险。可注入是为了测试能把 `max_targets` 调到 3 而不用真的入库
        # 1001 张照片。
        targets_store: "targets.TargetStore | None" = None,
        webui_dir: Path = WEBUI_DIR,
        # 配置里要的后端 vs 实际跑起来的后端。两者不同 = 降级（XFeat 模型不在，
        # 回退了 ORB）。**必须能从接口上看出来**，理由见 `_ping`。
        backend_requested: str | None = None,
        backend_error: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.library = library
        self.roots = roots
        self.resolver = resolver
        self.auth = auth
        self.config = config
        self.targets = targets_store or targets.TargetStore(cfg, catalog, config)
        # 构造它不碰盘（目录在第一次真的要写帧时才建），所以即使这个开关一辈子
        # 不开，也不会在 data/ 下留一个空目录让人以为功能开着。
        self.frames = framedump.FrameDump(cfg.data_dir)
        # 跨帧证据累积（`photoar.streak`）。**纯内存、不落盘**：它是缓存而不是数据，
        # 重启丢了最多少一次累积（下一次扫描 1.2 秒内又攒回来）。阈值不在这里定 ——
        # 它们是热配置，每次 offer 时传进去（见 `_decide_and_respond`）。
        self.streaks = streak.StreakTracker()
        self.webui_dir = webui_dir
        self.backend_requested = backend_requested or library.backend.name
        self.backend_error = backend_error
        # 管理台静态目录也过一遍白名单式路径解析，理由见 `_route_webui`。
        # 单独一个 Roots 而不是塞进 `self.roots`：那份白名单是"用户的 NAS 目录"，
        # 把包内目录混进去会让 `/v1/fs/list` 把源码目录也列出来。
        self._webui_roots = Roots({"webui": str(webui_dir)})
        self._started = time.time()
        # 环境变量给的那份白名单。挂载点是**叠加**在它上面的，所以要留一份原始的
        # —— 否则删掉一个挂载点之后重建，会把 PHOTOAR_ROOTS 里的也一起丢掉。
        self._env_roots: dict[str, str] = dict(cfg.roots)
        self._rebuild_roots()

    def _rebuild_roots(self) -> None:
        """按 `PHOTOAR_ROOTS` + 启用中的 local 挂载点，重建白名单。

        每次挂载点变动（增、改、删、启停）之后调一次。整体替换 `self.roots` 而不是往
        里加 —— `Roots` 构造时会按路径长度排序（嵌套根时 name 才是确定的那个），
        原地追加会让那个排序失效。

        webdav 挂载点**不进白名单**：它不在本地文件系统上，浏览走 PROPFIND，入库前
        先拉到暂存目录（那个目录本来就在 upload_dir_root 下，已经在白名单里了）。

        名字冲突时环境变量赢：`PHOTOAR_ROOTS` 是部署时定下的、compose 里写着的东西，
        而挂载点是运行期加的。让后者覆盖前者会让「改一下 compose 重启」这个动作
        变得不可预测。冲突的挂载点会被跳过并在日志里说一句。
        """
        merged: dict[str, str] = dict(self._env_roots)
        for m in self.catalog.list_mounts(enabled_only=True):
            if str(m["kind"]) != MOUNT_LOCAL:
                continue
            name = str(m["name"])
            path = str(m["location"])
            if name in merged and merged[name] != path:
                print(
                    f"[photoar] ⚠️ 挂载点 {name!r} 与 PHOTOAR_ROOTS 里的同名"
                    f"（{merged[name]}），按环境变量那份走，忽略挂载点 {path}"
                )
                continue
            merged[name] = path
        self.roots = Roots(merged)

    @classmethod
    def create(cls, cfg: ServerConfig) -> "Server":
        cfg.ensure_dirs()
        catalog = Catalog(cfg.db_path)
        config = AppConfig(catalog)
        cls._seed_backend(catalog, config)
        values = config.all()
        requested = str(values["recog.backend"])
        backend, backend_error = cls._open_backend(cfg, requested)
        library = cls._open_library(cfg, backend)
        # 会话 TTL 在这里读一次就定下来。这不是图省事：`session.viewer_days` /
        # `session.admin_hours` 的 `needs_restart` 就是 True，理由写在 appconfig
        # 那两个字段的 help 里 —— 已经签发的会话，过期时刻在登录那一刻就写死进库
        # 了，改这个值只影响之后的登录，所以"每次用的时候读一遍"换不到任何东西。
        auth = Auth(
            catalog,
            viewer_ttl_s=int(values["session.viewer_days"]) * 24 * 3600,
            admin_ttl_s=int(values["session.admin_hours"]) * 3600,
            legacy_token=cfg.token,
        )
        # 启动时清一次过期会话。`principal_of` 里还有"碰到就清"的那一半，两者
        # 都要有（原因写在那边）：只靠碰到才清的话，永不再来的旧 token 永远留着。
        auth.purge_expired()
        cls._bootstrap_admin(cfg, auth)
        cls._warn_threshold_mismatch(backend, values)
        return cls(
            cfg=cfg,
            catalog=catalog,
            library=library,
            roots=Roots(cfg.roots),
            resolver=mediaresolve.MediaResolver(
                strategies=tuple(cfg.media_strategies),
                custom_prefix=cfg.media_custom_prefix,
            ),
            auth=auth,
            config=config,
            backend_requested=requested,
            backend_error=backend_error,
        )

    @staticmethod
    def _seed_backend(catalog: Catalog, config: AppConfig) -> None:
        """`PHOTOAR_BACKEND` 只作为 `recog.backend` 的**初始值**写进库，一次。

        ⚠️ 已有值绝不能被覆盖。这不是"更礼貌"，是必须的：`recog.backend` 是管理台上
        能改的一个下拉框，而容器的环境变量在 compose 文件里写死。每次启动都按环境变量
        覆写的话，用户在管理台上把后端从 orb 改成 xfeat、重启一次容器就变回 orb 了 ——
        而管理台上那个下拉框显示的是库里的值，也就是显示 orb，看起来"我的修改根本没
        保存"。同一个坑对将来任何 `PHOTOAR_*` 播种的热配置都成立。

        判据是"库里这个 key 有没有行"，而不是"当前值等不等于默认值"：后者分不清
        "用户显式选了 orb（= 默认值）"与"没人设过"，于是那种情况下环境变量又会赢。
        """
        raw = os.environ.get("PHOTOAR_BACKEND")
        want = (raw or "").strip().lower()
        if not want:
            return
        if want not in backend_mod.NAMES:
            print(
                f"[photoar] ⚠️ PHOTOAR_BACKEND={raw!r} 不是可用的后端"
                f"（{list(backend_mod.NAMES)}），忽略。",
                flush=True,
            )
            return
        if "recog.backend" in catalog.all_app_config():
            return
        catalog.put_app_config({"recog.backend": json.dumps(want)})
        config.invalidate()
        print(f"[photoar] 按 PHOTOAR_BACKEND 把识别后端初始化为 {want}", flush=True)

    @staticmethod
    def _open_backend(cfg: ServerConfig, requested: str) -> tuple[Any, str | None]:
        """按配置建后端。XFeat 建不起来时**回退 ORB 并把原因带出来**，不让服务起不来。

        为什么回退而不是拒绝启动：模型文件是运行时资产（几 MB，不进镜像，容器启动时
        下载）。下载失败、卷没挂上、文件被删 —— 这些都会让一个此前正常的部署在下一次
        重启时彻底起不来，而它本来还能以 ORB 正常工作（ORB 才是通过出口条件的那条
        基线）。识别不了任何照片 vs 用一个稍弱的特征识别，后者明显是对的。

        为什么必须把原因带出来（而不只是打一行日志）：**静默跑成另一个后端**是这次
        改造最容易造成误判的一件事 —— 用户改配置换了特征、重启、扫一遍发现"识别率
        一点变化都没有"，于是得出"XFeat 在我的照片上没用"这个结论，而实际上跑的一直
        是 ORB。日志会滚走，接口不会。所以 `/v1/ping` 上有 `backendDegraded`。
        """
        try:
            return backend_mod.make(requested, model_path=cfg.xfeat_model_path), None
        except (xfeat.ModelMissing, xfeat.XFeatUnavailable) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            print(
                f"[photoar] ❌ 配置要的识别后端是 {requested}，但它起不来，"
                f"已回退到 {backend_mod.ORB}：\n[photoar]    {reason}\n"
                f"[photoar]    这不是「换了特征但没效果」—— 实际跑的是 ORB。"
                f"GET /v1/ping 的 backendDegraded 会一直是 true。",
                flush=True,
            )
            return backend_mod.orb_backend(), reason
        except ValueError as exc:
            # 库里的 recog.backend 是个不认识的名字。appconfig 的 enum 校验挡得住
            # 管理台那条路，但库可以被手工改过。同样回退而不是拒绝启动。
            reason = f"未知后端 {requested!r}：{exc}"
            print(f"[photoar] ❌ {reason}，回退到 {backend_mod.ORB}", flush=True)
            return backend_mod.orb_backend(), reason

    @staticmethod
    def _open_library(cfg: ServerConfig, backend: Any) -> PhotoLibrary:
        """打开这个后端对应的库目录，词表文件不在就用 `NullVocab`。

        以前这里是"词表不在就 FileNotFoundError 拒绝启动"。那条路在有词表可拷的年代
        是对的，但词表是用**用户自己的照片**训的，全新部署那一刻库是空的、没有描述子
        可训 —— 于是"要先有词表才能起服务"和"要先起服务才能入库训词表"互相卡死。
        `NullVocab` 让这个状态合法：全量扫描，结果正确、代价 O(库大小)
        （完整推理见 `photoar.nullvocab`）。
        """
        vocab_path = cfg.vocab_path_for(backend.name, backend.vocab_file)
        if vocab_path.is_file():
            vocab_obj = backend.load_vocab(vocab_path)
            print(
                f"[photoar] 识别后端 {backend.name}｜词表 {vocab_path}"
                f"（{vocab_obj.n_words} 个词）",
                flush=True,
            )
        else:
            vocab_obj = NullVocab()
            # 这条警告要足够明确到用户不会以为"服务起来了就没事了"：性能问题不会
            # 在小库上表现出来，等到几千张时才变成"扫描越来越慢"，那时早已忘了
            # 从来没训过词表。
            print(
                f"[photoar] ⚠️ 没有词表（找不到 {vocab_path}），"
                f"正在用空词表运行。\n"
                f"[photoar]    后果：**每次识别都会全量扫描整个库** —— "
                f"识别结果是正确的，但耗时与库里的照片数成正比，"
                f"库大了会明显变慢（每张照片一次 RANSAC）。\n"
                f"[photoar]    入库几十张之后建一份词表："
                f"`photoar-server build-vocab`（或管理台调 "
                f"POST /v1/admin/rebuild-vocab）。",
                flush=True,
            )
        return PhotoLibrary(
            cfg.library_dir_for(backend.name), vocab_obj, backend
        )

    @staticmethod
    def _warn_threshold_mismatch(backend: Any, values: dict[str, Any]) -> None:
        """后端换了而内点数阈值还停在另一个后端的标定值时，警告一次。

        两个后端的内点数是**两个不同的量**（ORB 标定 40，XFeat 标定 60；依据分别写在
        `verify.MIN_INLIERS` 与 `verify.XFEAT_MIN_INLIERS` 的注释里）。而
        `appconfig` 的 `recog.min_inliers` 只能有一个静态默认值，它取的是 ORB 那个。
        于是"在管理台把后端换成 xfeat"这个动作会留下一个偏松的阈值 —— 表现是误识别
        变多，而用户会归因到"XFeat 不准"。

        只警告、不自动改：自动改就等于**替用户覆盖他显式设过的值**，而这一列没有
        "用户有没有动过"的记录（`appconfig` 存的是值本身）。分不清就不能改。
        """
        current = int(values["recog.min_inliers"])
        if current == backend.min_inliers:
            return
        print(
            f"[photoar] ⚠️ recog.min_inliers 现在是 {current}，而 "
            f"{backend.name} 后端的标定值是 {backend.min_inliers}。"
            f"两个后端的内点数不是同一个量，沿用另一边的阈值会让判定实际变松或变紧"
            f"（松了误识别变多，紧了命中率掉）。要么在管理台改成 "
            f"{backend.min_inliers}，要么确认这是你按自己语料量出来的值。",
            flush=True,
        )

    @staticmethod
    def _bootstrap_admin(cfg: ServerConfig, auth: Auth) -> None:
        """库里一个 admin 都没有时按 `ServerConfig` 建一个。已经有了就什么都不做。

        三件事各自都不能省：

        - **必须建**。管理台是建号、发授权、改配置的唯一入口，而它只认库里的
          admin 行。不建的话，部署完谁都进不去，只能进容器手工 INSERT 一行。
        - **没给口令时用固定的 `DEFAULT_ADMIN_PASSWORD`**（2026-08-06 从"随机口令
          打印一次"改过来的，整段理由写在那个常量上）。一句话：随机口令只在建号那
          一次打印，而重建容器会清掉日志，于是"库里已有 admin + 日志没了"就等于
          永久锁死。代价由**强制改密**抵：用默认口令登进来的会话，管理台除了改密
          什么都做不了（见 `_using_default_password`）。
        """
        password = cfg.admin_password
        generated = not password
        if generated:
            password = DEFAULT_ADMIN_PASSWORD
        try:
            uid = auth.ensure_bootstrap_admin(cfg.admin_name, password)
        except NameTaken as exc:
            # 已经有一个同名的**非 admin** 用户。`ensure_bootstrap_admin` 刻意不把
            # 他提成 admin（那是环境变量能做到的最危险的事）。这里只报警不抛：抛了
            # 服务起不来，而"给那个用户改个名"这件事本身要通过服务的管理接口做。
            print(
                f"[photoar] ⚠️ 建不出引导管理员：{exc}。"
                f"改 PHOTOAR_ADMIN_NAME，或先给那个同名用户改名，再重启。",
                flush=True,
            )
            return
        if uid is None:
            return
        if generated:
            print(
                f"[photoar] 已创建引导管理员 {cfg.admin_name!r}，"
                f"初始口令：{DEFAULT_ADMIN_PASSWORD}\n"
                f"[photoar] ⚠️ 这是写死在源码里的公开默认值。管理台会**强制**你先改掉它"
                f"（不改进不去），但在你改掉之前这个服务等于没有口令 —— "
                f"部署完请立刻登录 /admin。想跳过这一步就设 PHOTOAR_ADMIN_PASSWORD。",
                flush=True,
            )
        else:
            print(
                f"[photoar] 已按 PHOTOAR_ADMIN_PASSWORD 创建引导管理员 "
                f"{cfg.admin_name!r}",
                flush=True,
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

    @staticmethod
    def _credential(req: Request) -> str:
        """这次请求带的凭证明文。两个来源，顺序固定：Bearer 头 → 会话 cookie。

        为什么 Bearer 优先：浏览器一旦存了 cookie，就会在**每一个**同源请求上带着
        它，包括页面里用 fetch 显式设了 `Authorization` 的那些。反过来的顺序下，
        一个过期或属于另一个账号的 cookie 会静默顶掉调用方明确表达的身份 ——
        表现是"我明明换了 token，怎么还是上一个人"。显式的头是更具体的意图。

        `Bearer` 后面为空时**继续往下看 cookie**，不直接返回空：`Authorization:
        Bearer` 这种空头会被某些代理/客户端库在没有凭证时加上，把它当成"调用方
        选择了 Bearer 这条路"会让同一个浏览器上的 cookie 白存。
        """
        raw = req.header("authorization") or ""
        scheme, _, token = raw.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            return token.strip()
        return _cookie_value(req.header("cookie"), SESSION_COOKIE)

    def _principal_of(self, req: Request) -> Principal | None:
        """凭证 → `Principal`。无效/过期/账号已停用都是 None（调用方 401）。

        旧的预共享 token（`PHOTOAR_TOKEN` / `cfg.token`）仍然有效，走的是 `Auth`
        的 `legacy_token` 分支，换来一个 `role=admin`、`user_id=None` 的 Principal。
        `tools/batch_ingest.py`、docker 的健康检查都靠它，而那些调用方没有人坐在
        前面输口令。它与会话 token 的取值空间不可能相撞（会话是 token_urlsafe(32)）。
        """
        return self.auth.principal_of(self._credential(req))

    def _require_admin(self, prin: Principal, what: str) -> None:
        """写操作与管理接口的统一入口。

        检查写在每个处理器的第一行，而**不是**在 `_dispatch` 里按路径前缀统一拦：
        前缀判断少写一个字符（比如漏掉结尾的斜杠）就会放过一整组接口，而那种错误
        在测试里只表现为"某个接口 viewer 也能调"，除非专门测了那一个接口否则发现
        不了。逐个处理器自己声明的话，"哪些接口是 admin only"是可以 grep 出来的。
        """
        if not prin.is_admin:
            raise HttpError(403, "admin_only", f"{what}只有管理员能用")

    def _may_see(self, prin: Principal, photo_id: str) -> bool:
        """这个人能不能看这张照片。

        判据故意绕一圈走 `auth.photo_filter`：那里是"admin 或 grant_all 的人不
        过滤"这条策略的唯一实现处。在这里直接写 `prin.is_admin or prin.grant_all`
        的话，以后多一种"能看全部"的条件就会有一处漏掉，而漏掉的方向是把照片给
        错人（漏在另一边只是把照片藏起来，用户会来问）。
        """
        if photo_filter(prin) is None:
            return True
        # photo_filter 返回非 None 就保证了 user_id 不是 None（它自己会为那种
        # Principal 抛异常），所以这里的 str() 不会得到 "None"。
        return self.catalog.is_granted(str(prin.user_id), photo_id)

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
        method = "GET" if req.method == "HEAD" else req.method

        # 管理台页面。放在 `/v1/` 判断之前是因为它不在 /v1 下，也免鉴权
        # （取舍写在 `_route_webui` 的 docstring 里）。
        if path == "/admin" or path.startswith("/admin/"):
            return self._route_webui(req, method)

        if not path.startswith("/v1/"):
            return _error(404, "not_found", f"没有这个接口：{path}")

        # 免鉴权白名单之外，一律先认身份。认不出来就 401，连"这个接口存不存在"
        # 都不告诉 —— 与既有的"非 /v1 前缀先 404 再谈鉴权"是同一个取舍的两面。
        prin: Principal | None = None
        if path not in PUBLIC_PATHS:
            prin = self._principal_of(req)
            if prin is None:
                return _error(
                    401, "unauthorized", "需要登录：Bearer token 或会话 cookie"
                )

        parts = path.strip("/").split("/")[1:]  # 去掉 v1

        table: list[tuple[str, tuple[str, ...], Callable[..., Response]]] = [
            ("GET", ("ping",), self._ping),
            ("POST", ("auth", "login"), self._auth_login),
            ("POST", ("auth", "logout"), self._auth_logout),
            ("GET", ("auth", "me"), self._auth_me),
            ("POST", ("recognize",), self._recognize),
            ("POST", ("recognize", "features"), self._recognize_features),
            ("GET", ("model", "xfeat"), self._model_xfeat),
            ("GET", ("targets", "manifest"), self._targets_manifest),
            ("GET", ("targets", "db"), self._targets_db),
            ("GET", ("photos",), self._list_photos),
            ("POST", ("photo",), self._create_photo),
            ("GET", ("photo", "*"), self._photo_detail),
            ("GET", ("photo", "*", "imgdb"), self._photo_imgdb),
            ("GET", ("photo", "*", "thumb"), self._photo_thumb),
            ("GET", ("photo", "*", "ref"), self._photo_ref),
            ("POST", ("photo", "*", "ref"), self._photo_replace_ref),
            ("GET", ("photo", "*", "media"), self._photo_media),
            ("POST", ("photo", "*", "video"), self._photo_attach_video),
            ("DELETE", ("photo", "*", "video"), self._photo_detach_video),
            ("DELETE", ("photo", "*"), self._photo_delete),
            ("GET", ("asset", "*", "stream"), self._asset_stream),
            ("GET", ("fs", "list"), self._fs_list),
            ("GET", ("fs", "thumb"), self._fs_thumb),
            ("POST", ("upload",), self._upload),
            ("POST", ("upload", "check"), self._upload_check),
            ("GET", ("history",), self._history),
            ("GET", ("admin", "users"), self._admin_list_users),
            ("POST", ("admin", "users"), self._admin_create_user),
            ("PATCH", ("admin", "users", "*"), self._admin_patch_user),
            ("DELETE", ("admin", "users", "*"), self._admin_delete_user),
            ("GET", ("admin", "users", "*", "grants"), self._admin_get_grants),
            ("PUT", ("admin", "users", "*", "grants"), self._admin_put_grants),
            ("GET", ("admin", "config"), self._admin_get_config),
            ("PATCH", ("admin", "config"), self._admin_patch_config),
            ("POST", ("admin", "rebuild-vocab"), self._admin_rebuild_vocab),
            ("GET", ("admin", "lookup"), self._admin_lookup),
            ("GET", ("admin", "inbox"), self._admin_inbox),
            ("GET", ("admin", "mounts"), self._admin_list_mounts),
            ("POST", ("admin", "mounts"), self._admin_create_mount),
            ("PATCH", ("admin", "mounts", "*"), self._admin_patch_mount),
            ("DELETE", ("admin", "mounts", "*"), self._admin_delete_mount),
            ("GET", ("admin", "mounts", "*", "list"), self._admin_mount_list),
            ("POST", ("admin", "mounts", "*", "fetch"), self._admin_mount_fetch),
            ("GET", ("admin", "videos"), self._admin_list_videos),
            ("GET", ("admin", "mapping"), self._admin_mapping),
            ("POST", ("admin", "import", "parse"), self._admin_import_parse),
            ("GET", ("admin", "export", "*"), self._admin_export),
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
                if prin is None and path not in PUBLIC_PATHS:
                    # 到不了：上面已经在这个条件下 401 返回过了。留着是因为
                    # `PUBLIC_PATHS` 是按整串精确匹配的 —— 有人往里加一条带通配的
                    # 路径（不生效）、或者把上面那段判断挪了位置时，这里是唯一能把
                    # "处理器拿到 None 身份"变成 500 而不是静默按 None 放行的地方。
                    raise HttpError(500, "internal", "身份缺失，拒绝执行")
                return handler(req, prin, *args)
        if allowed:
            return _error(
                405, "method_not_allowed", f"{path} 只支持 {sorted(allowed)}"
            )
        return _error(404, "not_found", f"没有这个接口：{path}")

    # ---- 认证 ----

    def _session_cookie(self, token: str, max_age_s: int) -> str:
        """签一个会话 cookie 的值。

        **为什么除了 token 还要下发 cookie**：管理台是网页，它要显示照片缩略图和
        视频，而 `<img src>` / `<video src>` 这两个标签没有任何办法带上
        `Authorization` 头（fetch 能，标签不能）。两个替代方案都更糟：把 token 拼进
        query string 会让它进浏览器历史、Referer 和服务端访问日志；用 fetch 取回
        blob 再塞 objectURL 则等于放弃 Range 请求，视频从此不能 seek（而
        `/v1/asset/*/stream` 实现 206 的全部意义就是能 seek）。所以浏览器那一侧
        只能靠 cookie。App 那一侧继续用 Bearer 头，两条路 `_credential` 都认。

        - `HttpOnly`：页面脚本读不到它，XSS 就偷不走会话。代价是管理台自己也读不到
          自己的 token，所以登录响应里那份明文 token 仍然要给（App 要用）。
        - `SameSite=Lax`：第三方站点发起的请求不带它（挡 CSRF 的主要一层），用户
          自己点链接/书签进来时带（否则从书签打开管理台永远显示未登录）。
        - `Path=/`：cookie 要覆盖 `/v1/*`（数据）和 `/admin`（页面）两棵子树。
        - `Secure` 由 `cfg.cookie_secure` 决定，**默认关**：部署形态有两种，局域网
          http 直连（家里手机连 WiFi 直接打 NAS 的 8964 端口）和 Cloudflare 隧道后
          面的 https。写死 Secure 的话浏览器在 http 上会直接丢掉这个 cookie ——
          表现是"登录明明成功了，一刷新又要登录"，而响应里确实有 Set-Cookie，
          几乎不可能往 cookie 属性上想。只走 https 的部署应该打开它
          （`PHOTOAR_COOKIE_SECURE=1`）。
        """
        parts = [
            f"{SESSION_COOKIE}={token}",
            "HttpOnly",
            "SameSite=Lax",
            "Path=/",
            f"Max-Age={int(max_age_s)}",
        ]
        if self.cfg.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _using_default_password(self, prin: Principal) -> bool:
        """这个人是不是还在用 `DEFAULT_ADMIN_PASSWORD`。

        ## 为什么是现算，而不是在库里存一个 `must_change_pwd` 标记

        存标记要加一列、写迁移，而且那个标记与真相之间会**漂**：从库里直接改口令、
        或者把口令又改回 `admin`，标记都不会跟着动。现算的语义就是字面意思 ——
        "你此刻的口令等于那个公开的默认值" —— 不可能不同步。

        代价是一次 hash 校验（实测 ~80ms）。只在 `/auth/login` 与 `/auth/me` 上算，
        两个都是低频接口（登录一次 + 刷新页面一次），而**只对 admin 算**：viewer
        没有管理台可进，给他算这一下纯属浪费。

        取不到用户行时返回 False：那说明账号刚被删，接下来的请求本来就会 401，
        在这里报"要改密"只会把一个已经没有意义的界面挡在前面。
        """
        if not prin.is_admin:
            return False
        row = self.catalog.get_user(prin.user_id)
        if row is None:
            return False
        return verify_password(
            DEFAULT_ADMIN_PASSWORD, row["pwd_hash"], row["pwd_salt"]
        )

    def _auth_login(self, req: Request, prin: Principal | None) -> Response:
        """`prin` 恒为 None（这是唯一免鉴权的接口）。签名保持与其它处理器一致，
        好过让路由表为一个特例分叉。"""
        doc = req.json_body()
        name = doc.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HttpError(400, "missing_name", "需要 name")
        password = doc.get("password")
        if password is not None and not isinstance(password, str):
            raise HttpError(400, "bad_password", "password 必须是字符串")

        # 401 与 403 的分界线是"**重输一次有没有可能成功**"，不是"错得严不严重"。
        # 客户端拿到 401 会把登录框再弹一次让用户重输，拿到 403 应该直接把原因显示
        # 出来、不要重试。分错的代价是家里人对着一个永远不可能对的输入框反复输名字。
        try:
            token, principal = self.auth.login(name, password)
        except UnknownUser as exc:
            # 名字不在册 —— 重输一万次结果一样，账号只能由管理员建（见
            # `auth.UnknownUser`：自动建号等于对隧道全网开放）。
            raise HttpError(403, "unknown_user", str(exc)) from exc
        except AccountDisabled as exc:
            # 口令再对也没用，得管理员先启用。
            raise HttpError(403, "account_disabled", str(exc)) from exc
        except BadCredentials as exc:
            # 只有这一条是"重输可能就对了"：口令打错，或者 admin 忘了填口令。
            raise HttpError(401, "bad_credentials", str(exc)) from exc

        return json_response(
            200,
            {
                "token": token,
                "userId": principal.user_id,
                "name": principal.name,
                "role": principal.role,
                "grantAll": principal.grant_all,
                "expiresAt": self.auth.expires_at_of(token),
                # 管理台见到它就把界面锁成"只能改密"。**`/auth/me` 上也有一份** ——
                # 只在登录响应里给的话，刷新一下页面就绕过去了（前端进主界面走的是
                # `/auth/me`）。
                "mustChangePassword": self._using_default_password(principal),
            },
            **{
                "Set-Cookie": self._session_cookie(
                    token, self.auth.ttl_s(principal.role)
                ),
                # 登录响应里有明文 token，任何一层缓存都不能留。
                "Cache-Control": "no-store",
            },
        )

    def _auth_logout(self, req: Request, prin: Principal) -> Response:
        self.auth.logout(self._credential(req))
        # 同时把 cookie 清掉（`Max-Age=0`）。不清的话浏览器会继续在每个请求上带一个
        # 已经作废的 token，于是管理台的每个 fetch 都 401 —— 一个"我已经登出了"的
        # 用户看到的是一片报错，而不是登录框。
        return Response(
            status=204,
            headers={
                "Set-Cookie": self._session_cookie("", 0),
                "Cache-Control": "no-store",
            },
        )

    def _auth_me(self, req: Request, prin: Principal) -> Response:
        return json_response(
            200,
            {
                "userId": prin.user_id,
                "name": prin.name,
                "role": prin.role,
                "grantAll": prin.grant_all,
                "isAdmin": prin.is_admin,
                # 见 `_auth_login` 里同名字段的注释：刷新页面走的是这条路，
                # 少了它强制改密就形同虚设。
                "mustChangePassword": self._using_default_password(prin),
            },
            **{"Cache-Control": "no-store"},
        )

    # ---- 管理台静态页 ----

    def _route_webui(self, req: Request, method: str) -> Response:
        """`GET /admin` 与 `GET /admin/<file>`：吐 `webui/` 目录里的静态文件。

        **这条路由免鉴权**，而页面里所有 `/v1/admin/*` 调用都要鉴权。这个取舍是
        必须的而不是偷懒：页面本身就是那个输口令的界面，要求先鉴权才能拿到它等于
        要求先登录才能看到登录框。这里发出去的只有仓库里的 HTML/CSS/JS —— 固定
        内容、不含任何用户数据、也不受 `roots` 白名单影响。真正的边界在
        `/v1/admin/*`：没有会话的浏览器打得开页面，但页面上每一次取数据都 401。

        路径安全走既有的 `safepath.Roots`（全服务唯一暴露文件系统的地方，有
        `tests/server/test_safepath.py` 的 15 条在盯着），不另写一遍前缀比较：它是
        "resolve 之后再看落在哪个根下"，所以 `..`、多重斜杠、以及指向目录外的符号
        链接一并挡住。它唯一"不合身"的地方是要求绝对路径，而我们本来就是拿固定的
        `webui_dir` 去拼，正好。

        名字白名单（`_WEBUI_NAME_RE`）排在它前面，两个作用：`..` 这类输入在碰文件
        系统之前就被拒掉；以及把"文件名根本不合法"（404）和"这个路径逃出了目录"
        （403 + 日志，那是真值得记一行的探测）分成两种可读的失败。
        """
        if method != "GET":
            return _error(405, "method_not_allowed", "/admin 只支持 GET")
        rest = req.path[len("/admin"):]
        # `/admin` 与 `/admin/` 都给首页。缺了后者的话，浏览器地址栏里那个尾随斜杠
        # （用户手打、或者从别处跳过来时补上的）会得到 404。
        name = "index.html" if rest in ("", "/") else rest[1:]

        # 每个分区一个自己的 URI：`/admin/users`、`/admin/photos`…
        #
        # 这些路径**也返回首页**，由前端读 `location.pathname` 决定打开哪个分区
        # （history.pushState 换地址，前进后退与刷新都成立）。
        #
        # 为什么要在服务端加这一句：不加的话 `/admin/users` 会走下面的文件查找，
        # 而 `users` 恰好符合文件名白名单 → 去找一个叫 `users` 的文件 → 404。
        # 也就是说**刷新页面就白屏**，而这正是「独立 URI」最主要的用途（收藏、
        # 发给别人、刷新）。
        #
        # 只认这张固定清单，不做「任何找不到的文件都回首页」的兜底：那种写法会把
        # `/admin/app.js` 拼错时的 404 变成一份 HTML，而浏览器会拿 HTML 当 JS 解，
        # 报出来的是一句莫名其妙的语法错误。
        if name.rstrip("/") in _WEBUI_TABS:
            name = "index.html"

        if not _WEBUI_NAME_RE.match(name):
            raise HttpError(404, "not_found", "管理台没有这个文件")
        path = self._webui_roots.resolve(str(self.webui_dir / name))
        return self._static_file(
            req,
            path,
            _WEBUI_TYPES.get(path.suffix.lower(), "application/octet-stream"),
            immutable=False,
            # 页面要能被换掉（换镜像就换了），所以每次都回源校验 ETag，而不是
            # `max-age`。`max-age=3600` 的后果是升级完之后有一小时里用户看到的是
            # 旧页面配新接口，而"清一下缓存就好了"是最不该出现在家用部署里的指示。
            cache="no-cache",
        )

    # ---- 接口 ----

    def _ping(self, req: Request, prin: Principal) -> Response:
        # spec §7："必须极轻"。探活频率由客户端的网络变化回调决定，且四个 endpoint
        # 是并行探的。（"不查库"这一半已经**刻意**放弃了一点，见下面 targets 那段。）
        #
        # 要鉴权、但不要任何授权：它的用途是"这条通道通不通"，viewer 也得能探
        # （客户端切网络时四个 endpoint 一起探，那是扫描前的准备动作）。
        #
        # 后端相关的那几个状态字段**全部来自启动时算好的进程内属性**（`len(library)`
        # 读的是快照里那个 tuple 的长度）。
        #
        # 为什么把它们放在 ping 而不是另开一个 `/v1/status`：ping 是唯一一个客户端
        # **本来就会定期调**的接口。降级状态的价值全在"不用专门去查就能发现"——
        # 另开一个端点等于要求用户先怀疑有问题，而这里要防的恰恰是"用户完全没意识到
        # 跑的是另一个后端"。
        #
        # `targets*` 那四个字段打破了"一次 SQL 都没有"：它们要一次
        # `list_photo_targets`（一条走索引的 JOIN，行数被 ARCore 的库容量上限封在
        # 1000 以内）加一次 sha256（几万字节的文本）。**这个代价是值得付的**，因为
        # 现在"识别在端上"是主路径，而它的前提是那个库建好了 —— 没有这四个字段，
        # 部署完确认这件事的唯一办法是拿手机去试。ping 的探活频率由客户端的网络变化
        # 回调决定（不是每帧），所以"极轻"在量级上仍然成立。
        # 它**不触发构建**（见 `TargetStore.status`）。
        #
        # 这四个字段是**按调用者的授权集**算的：一个 viewer 的 ping 报的是他自己那
        # 一套的状态。这是对的 —— 他要确认的正是"我这台手机能不能离线识别"。
        active = self.library.backend.name
        return json_response(
            200,
            {
                "ok": True,
                "version": self.cfg.version,
                "serverTime": int(time.time() * 1000),
                # 实际在跑的后端。
                "backend": active,
                # 配置里要的那个。两者不等就是降级。
                "backendRequested": self.backend_requested,
                "backendDegraded": active != self.backend_requested,
                # 降级原因原文（模型文件不在的话，这里就是那条"去哪儿取"的说明）。
                "backendError": self.backend_error,
                # 跑的是空词表还是训好的词表。false = 每次识别全量扫描。
                "vocabTrained": not isinstance(self.library.vocab, NullVocab),
                "vocabWords": int(self.library.vocab.n_words),
                "photos": len(self.library),
                # 端上离线识别的前提：targetsVersion / targetsCount /
                # targetsOverflow / targetsBuilding。一条 curl 就能确认它就绪。
                **self.targets.status(prin),
            },
            **{"Cache-Control": "no-store"},
        )

    def _recognize(self, req: Request, prin: Principal) -> Response:
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

        return self._decide_and_respond(
            req,
            prin,
            t0,
            lambda top_k: self.library.verify_candidates(img, top_k),
            # 传原始字节而不是解开的 `img`：留帧要存的是**客户端实际发出来的东西**，
            # 重新编码一次就把「客户端编码坏了」这种可能性给洗掉了。
            raw_frame=part.data,
        )

    def _recognize_features(self, req: Request, prin: Principal) -> Response:
        """端上提特征那条路。响应形状与 `/v1/recognize` **完全一致**。

        "完全一致"是硬要求而不是巧合：客户端解析命中响应的代码是共用的一份
        （Android 侧 `ApiParse.recognize`）。多一个字段、少一个字段、或者把 latencyMs
        的含义改成"只算配对"，都会在换路径时表现成"另一条路上偶发解析失败"。
        所以两条路共用 `_decide_and_respond`，差别只在候选是怎么算出来的。

        `latencyMs` 在这里如实只包含**服务端**耗时（不含端上推理与上传）。这条路的
        意义就是把推理挪走，把端上那 20-30ms 算进来会让两条路的数字不可比。
        """
        t0 = time.perf_counter()
        active = self.library.backend.name
        if active != backend_mod.XFEAT:
            # 描述子格式不兼容：ORB 是 32 字节二值、XFeat 是 64 维 float32。硬收下
            # 只会让 `descstore` 那边按 ORB 的 stride 去读一段 float 缓冲区 —— 读出
            # 来的是垃圾，而且**不报错**（长度恰好能对上时）。所以在这里就拒。
            #
            # 判据用**实际在跑的**后端而不是配置里要的那个：XFeat 模型不在时服务会
            # 回退 ORB（见 `_open_backend`），此时配置说 xfeat、库里是 ORB 描述子。
            # 按配置判就会收下一批永远匹配不上的描述子，而 `/v1/ping` 上那个
            # backendDegraded 才是真相。
            raise HttpError(
                400,
                "unsupported_backend",
                f"端上提特征只能配 {backend_mod.XFEAT} 后端使用，"
                f"当前实际在跑的是 {active}（描述子格式不兼容：ORB 是 32 字节二值，"
                f"XFeat 是 64 维 float32；硬收下只会读出垃圾）。"
                f"改用 POST /v1/recognize 传 JPEG，或把后端换成 {backend_mod.XFEAT}。",
                activeBackend=active,
                requestedBackend=self.backend_requested,
            )
        try:
            query = featurebody.parse(req.json_body(MAX_FEATURES_BYTES))
        except featurebody.FeaturesRejected as exc:
            raise HttpError(400, exc.code, exc.message) from exc

        return self._decide_and_respond(
            req, prin, t0, lambda top_k: self.library.verify_features(query, top_k)
        )

    @staticmethod
    def _streak_key(prin: Principal) -> str:
        """跨帧累积按谁分链。

        **不用 token**：这个字符串会进 `StreakTracker` 的内存字典，而那个对象可能被
        打进诊断输出。身份足够区分「谁在扫」，而 token 是凭证。

        已知限制：同一个用户拿两台手机同时扫会共用一条链。后果很轻 —— 扫不同照片时
        两台互相打断（累积失效，退回单帧判定，也就是改这一版之前的行为），扫同一张时
        会稍微提早命中。要修得把 session id 带进 [Principal]，而那个改动的面比这个
        限制大。
        """
        return f"{prin.via}:{prin.user_id or prin.name}"

    def _decide_and_respond(
        self,
        req: Request,
        prin: Principal,
        t0: float,
        candidates: Callable[[int], list[Any]],
        raw_frame: bytes | None = None,
    ) -> Response:
        """两条识别路径共用的后半段：阈值 → 判定 → 记历史 → ACL → 响应。

        `candidates` 是"给定 top_k 算出候选的成对结果"。把差异收成这一个回调，是为了
        让阈值、历史记录、授权检查、响应字段这四件事**只有一份实现** —— 其中授权检查
        与判定的先后顺序是有安全含义的（见下面那段注释），抄第二份一定会抄丢。
        """
        # 阈值从热配置取，**不**调 `library.recognize`（那个用的是 verify.py 的模块
        # 常量）。这是 `recog.min_inliers` / `recog.ratio` / `recog.top_k` 三个
        # `needs_restart=False` 的字段真正生效的地方 —— appconfig 的模块 docstring
        # 里点明了这是一句"需要接线才成立的承诺"：不接的话管理台上改完显示成功、
        # 库里也确实写了，识别行为一点变化都没有。
        #
        # 每个请求读一次配置不查一次 SQL：`AppConfig` 有 2 秒的进程内缓存，扫描时
        # 每秒好几帧只会落一次查询（TTL 与 patch 后的 invalidate 都在那边）。
        values = self.config.all()
        results = candidates(int(values["recog.top_k"]))
        decision = verify.decide_with(
            results,
            min_inliers=int(values["recog.min_inliers"]),
            ratio=float(values["recog.ratio"]),
        )
        # 单帧没过 → 让跨帧累积再看一次（`photoar.streak`）。依据是真机日志：194 条
        # weak 里 22 条（11.3%）其实是「看到照片了、就差几分」，而它们的第一名比第二名
        # 高 3 倍以上 —— 每一帧都被单独扔掉了。
        #
        # 攒够了就**原地把 decision 换掉**，于是下面每一步（留帧、记历史、orphan 判断、
        # 授权检查、响应字段）都与单帧命中走**完全同一条路**。这正是「状态放服务端」
        # 这个选择的全部理由：客户端累积要把未命中时的最佳猜测回给客户端，而 weak 那
        # 一支不跑授权检查，回 photoId 就是一次信息泄漏。这里一个新暴露面都没有。
        #
        # `need = 0` 关掉整条路 —— 那时候连 offer 都不调，链也就不会攒。
        need = int(values["recog.streak_need"])
        if not decision.matched and need > 0:
            upgraded = self.streaks.offer(
                self._streak_key(prin),
                int(time.time() * 1000),
                results,
                need=need,
                soft_min=int(values["recog.streak_soft_min"]),
            )
            if upgraded is not None:
                decision = upgraded
        latency_ms = int((time.perf_counter() - t0) * 1000)
        # 第二名与前几名一起记进历史。**这不是可选的诊断糖**：一次真实排查里 941 条
        # 记录只有 inliers 一列，其中 897 条内点数 160~229（门槛 40）却判了未命中，
        # 而光凭这张表分不出挡住它们的是 det 越界还是库里有近重复 —— 那两件事一件
        # 要改取景、一件要清库。真相是后者，而它在这张表上完全不可见。
        ranked = sorted(results, key=lambda r: -r.inliers)
        runner_up = ranked[1].inliers if len(ranked) > 1 else 0
        topk = [(r.photo_id, r.inliers) for r in ranked[:DEDUP_LOG_TOP_K]]
        via = req.header(ENDPOINT_HEADER)

        # 留帧在判定之后、分支之前：三条出口（未命中 / orphan / 命中）都要留，
        # 而**命中的帧和未命中的帧放在一起**才有诊断价值 —— 「同一张照片，这个
        # 角度认得出、那个角度认不出」是靠对比看出来的，不是靠单看失败帧。
        #
        # 判在 latency_ms 之后，所以留帧的耗时不计入返回给客户端的 latencyMs：
        # 那个数字是识别本身的成本，掺进排查开销会让它不再可比。
        if raw_frame and self.config.all()["debug.dump_frames"]:
            self.frames.save(
                raw_frame,
                matched=decision.matched,
                inliers=decision.inliers,
                reason=decision.reason,
                via=via,
            )

        if not decision.matched or decision.photo_id is None:
            self.catalog.log_recognize(
                photo_id=None,
                inliers=decision.inliers,
                latency_ms=latency_ms,
                via=via,
                reason=decision.reason,
                runner_up=runner_up,
                topk=topk,
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
                photo_id=None,
                inliers=decision.inliers,
                latency_ms=latency_ms,
                via=via,
                reason="orphan",
                runner_up=runner_up,
                topk=topk,
            )
            return json_response(
                200,
                {"matched": False, "latencyMs": latency_ms, "reason": "orphan"},
                **{"Cache-Control": "no-store"},
            )

        # 如实记下命中的是哪张，**在授权检查之前**：`/v1/history` 是排查"家里人说
        # 扫不出来"的唯一线索，把这一次记成未命中会让"其实认出来了、只是没授权给他"
        # 这个最可能的原因彻底看不见。history 本身是 admin only，不会因此泄露给谁。
        self.catalog.log_recognize(
            photo_id=decision.photo_id,
            inliers=decision.inliers,
            latency_ms=latency_ms,
            via=via,
            reason=decision.reason,
            runner_up=runner_up,
            topk=topk,
        )
        pid = decision.photo_id

        if not self._may_see(prin, pid):
            # ⚠️ 授权检查在**判定之后**。这个顺序是刻意的，不要"优化"成先把候选集
            # 过滤成这个人可见的那些再判定。
            #
            # `verify.decide_with` 的第三条判据是"第一名内点数 >= ratio × 第二名"，
            # 而它在**全部候选**之间比（理由见 verify.py 的模块 docstring）。先过滤
            # 候选的话，被滤掉的照片就不再参与这条比值检验：一个只授权了三张的用户
            # 扫一张与库内某张近重复的照片，本该判 ambiguous 不播，过滤之后第二名
            # 消失、第一名成了唯一候选，于是直接播出去 —— 播的还是他有权看的那张，
            # 界面上完全正常。
            #
            # 也就是说"先过滤候选"会**提高误识别率，且只对权限受限的用户提高**：
            # 管理员（不过滤）怎么测都测不出来。而那道比值检验正是把真实误识别从
            # 0.349% 压到 0 的判据之一（数据见 verify.MIN_INLIERS 的注释）。
            #
            # 代价是为一次注定不播的识别付满一次 RANSAC。换来的是"权限不影响判定"
            # 这条性质，值得。
            return json_response(
                200,
                # HTTP 仍是 200：在这个 API 里"没认出来"是正常状态（客户端每 400ms
                # 一次，未命中不该产生错误日志噪音），"认出来了但你没权限"对客户端
                # 是同一种处理 —— 继续扫下一帧。reason 里如实写 forbidden，好让排查
                # 的人能把它和真正的未命中区分开。
                {"matched": False, "latencyMs": latency_ms, "reason": "forbidden"},
                **{"Cache-Control": "no-store"},
            )

        payload = {
            "matched": True,
            "photoId": pid,
            "inliers": decision.inliers,
            "printWidthM": float(photo["print_width_m"]),
            "fitMode": self._fit_mode_of(photo),
            "imgdbUrl": f"/v1/photo/{pid}/imgdb",
            "refThumbUrl": f"/v1/photo/{pid}/thumb",
            "mediaUrl": f"/v1/photo/{pid}/media",
            # 客户端「保存到相册」拿它当文件名。没有的话相册里全是
            # `photoar-603409ee.jpg` 这种名字，一场婚礼存十几张之后谁也认不出哪张是哪张。
            # 可能是 None（入库时没给标题），客户端要能接受。
            "title": photo["title"],
            "latencyMs": latency_ms,
        }
        aspect = self._ref_aspect(photo)
        if aspect is not None:
            payload["refAspect"] = aspect
        if photo["ref_stale"]:
            # spec §13：参考图内容变过，仍尝试命中但要提示特征可能已过期
            payload["refStale"] = True
        return json_response(200, payload, **{"Cache-Control": "no-store"})

    def _model_xfeat(self, req: Request, prin: Principal) -> Response:
        """下发端上提特征要用的那份 ONNX 模型（4.31MB）。

        **为什么由服务端下发，而不是打进 APK**：模型与库里的描述子是绑死的 —— 换一份
        模型（换 top_k、换检测阈值、重新导出）就等于全库描述子作废。打进 APK 的话，
        "哪份模型有效"会有两个答案（用户装的那个版本 vs 服务端此刻跑的那个），而两者
        不一致的表现是识别率静默下降，不是报错。放在这里下发，答案只有一个。
        另外 4.31MB × 4 个 ABI 的 native 库已经让 APK 涨了不少，模型不该再加进去。

        要鉴权，但**不要求 admin**：需要它的人正是拿着手机扫照片的 viewer。

        `Cache-Control: no-cache` + ETag 而不是 immutable：这个文件是可以被换掉的
        （运维换一份模型重启服务），而 immutable 会让客户端上那份缓存永远不再回源 ——
        换了模型之后手机上还是旧的，且没有任何地方看得出来。no-cache 只是要求
        "每次问一句 ETag 变了没"，命中时是 304 空体，代价可以忽略。
        """
        path = self.cfg.xfeat_model_path
        if not path.is_file():
            # 404 而不是 500：这是一个**正常**的部署状态（后端是 orb 时根本不需要
            # 模型）。客户端拿到 404 应该静默退回传 JPEG 那条路，而不是报错给用户。
            raise HttpError(
                404,
                "model_missing",
                f"服务端没有 {path.name}（找的是 {path}）。"
                f"取法见 tools/fetch_models.py 或 tools/export_models.py。"
                f"端上提特征需要它，传 JPEG 的那条路不需要。",
            )
        return self._static_file(
            req,
            path,
            "application/octet-stream",
            immutable=False,
            cache="no-cache",
        )

    # ---- 端上离线识别用的整库目标 ----

    def _targets_manifest(self, req: Request, prin: Principal) -> Response:
        """`GET /v1/targets/manifest`：这个人可见的那一套整库目标的元数据。

        **按调用者的授权集**给结果 —— 这是 ACL 的一部分，不是"顺手过滤一下"：
        manifest 里有标题，而标题本身可能是隐私（"外婆生日"）。判据走
        `targets.TargetStore`，它内部绕 `auth.photo_filter`（"谁能看全部"的唯一
        实现处）。

        要鉴权但**不要求 admin**：需要它的人正是拿着手机扫照片的 viewer。

        `no-store` 而不是 ETag：这份 JSON 里有 title / fitMode / hasVideo，而它们
        **刻意不在版本号里**（改个标题不该让全体客户端重下一遍整库，理由写在
        `targets` 的模块 docstring）。拿版本号当 ETag 的话，改完标题的客户端会一直
        拿到 304 —— 一个"改了但看不到"的状态。这份 JSON 是现算的，代价只有一次
        SQL + 一次哈希。
        """
        return json_response(
            200, self.targets.manifest(prin), **{"Cache-Control": "no-store"}
        )

    def _targets_db(self, req: Request, prin: Principal) -> Response:
        """`GET /v1/targets/db`：那套整库目标的 `.imgdb` 字节。

        三种正常结果，各自的状态码都有具体理由：

        - **200 + ETag: "<version>"**。ETag 就是版本号本身（不是 `_static_file`
          默认的 mtime 派生值），这样客户端能把它与 manifest 里的 `version` 直接
          对比 —— 那是"这一对是配好的"的唯一判据（推理见 `targets` 模块 docstring
          的"一致性"一节）。mtime 派生的 ETag 在这里还有第二个毛病：同一个版本被
          清理后重建一次，字节完全相同而 ETag 变了，全体客户端白重下一遍。
        - **503 + Retry-After**：正在建。这是**正常状态**而不是失败（真实建库耗时
          未测量，可能是几十秒），所以客户端应该按 Retry-After 再来，不要报错给用户。
        - **404 `no_targets`**：这个人一张照片都没被授权（新部署，或者管理员还没
          发授权）。不回一个 0 字节的文件：客户端拿到 200 会认为离线识别已就绪，
          然后每一帧都不命中，而"没有权限"与"库有问题"看起来一模一样。
        """
        try:
            got = self.targets.resolve(prin)
        except targets.BuildFailed as exc:
            # 构建失败是**服务端故障**（arcoreimg 不在、磁盘满），不是"还没好"。
            # 用 503 的话客户端会一直重试一个必然失败的东西，而运维那边没有任何
            # 信号。500 + 原文让它一次就能被看见。
            raise HttpError(
                500, "targets_build_failed", exc.reason, version=exc.version
            ) from exc
        if isinstance(got, targets.Building):
            resp = _error(
                503,
                "targets_building",
                f"整库目标 {got.version} 正在构建，{got.retry_after_s} 秒后再来。"
                f"这不是错误：端上离线识别的库是按需建的。",
                version=got.version,
                retryAfterS=got.retry_after_s,
            )
            resp.headers["Retry-After"] = str(got.retry_after_s)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        if not got.photo_ids:
            raise HttpError(
                404,
                "no_targets",
                "你还没有被授权任何照片，没有可下发的整库目标。"
                "管理员在管理台发一下授权即可。",
                version=got.version,
            )
        return self._static_file(
            req,
            got.path,
            "application/octet-stream",
            immutable=False,
            # 库是会被换掉的（入库一张就是一个新版本），所以每次问一句 ETag 变了没。
            # `immutable` 在这里会让手机上那份缓存永远不再回源。
            cache="no-cache",
            etag=f'"{got.version}"',
        )

    def _ref_aspect(self, photo: dict[str, Any]) -> float | None:
        asset = self.catalog.get_asset(str(photo["ref_asset_id"]))
        if not asset:
            return None
        # 取整精度在 `db.ref_aspect` 里只写一次：整库 manifest 那条路
        # （`targets.py`）给的是同名字段，两处各自 round 一次迟早会不一样。
        return ref_aspect(asset["width_px"], asset["height_px"])

    def _fit_mode_of(self, photo: dict[str, Any]) -> str:
        """这张照片实际生效的视频贴合方式。

        `photo.fit_mode` 为 NULL 表示"跟随全局默认"（见 `db._PHOTO_V2_COLUMNS`），
        不是"没设置所以出错"。v1 时期入库的照片这一列全是 NULL，所以这个兜底必须
        留着 —— 新入库的照片会在 `_create_photo` 里把当时的全局默认写进去。

        规则本身在 `db.effective_fit_mode`（离线 manifest 也要用同一份，理由写在
        那边）。这里只负责"全局默认从热配置取"。
        """
        return effective_fit_mode(photo, str(self.config.get("video.fit_mode")))

    def _photo_or_404(self, photo_id: str, prin: Principal) -> dict[str, Any]:
        """取一张照片并检查这个人有没有权限看。

        未授权返回 **403 `forbidden`**，不是"当作不存在"的 404。
        `Catalog.get_photo(user_id=...)` 提供的正是后者，它的注释解释了 404 的好处
        （不泄露"这张照片确实存在"，而标题本身可能是隐私）。这里刻意不用那条路：
        photo_id 是 32 位十六进制随机值，既猜不出也枚举不了，所以那点泄露换不到
        什么；而把"链接抄错了"和"这张没授权给我"压成同一个 404，会让家里人在管理员
        面前只能复述成同一句"打不开"，而这两件事的处理方式完全不同。
        """
        if not _ID_RE.match(photo_id):
            raise HttpError(404, "not_found", f"photoId 格式不对：{photo_id}")
        photo = self.catalog.get_photo(photo_id)
        if photo is None:
            raise HttpError(404, "not_found", f"照片不存在：{photo_id}")
        if not self._may_see(prin, photo_id):
            raise HttpError(403, "forbidden", "这张照片没有授权给你")
        return photo

    def _list_photos(self, req: Request, prin: Principal) -> Response:
        out = []
        # 过滤条件由 `auth.photo_filter` 算：admin 与 grant_all 的人拿到 None
        # （= 不过滤 = 与改造前逐字节相同的行为），其余人拿到自己的 user_id。
        for p in self.catalog.list_photos(user_id=photo_filter(prin)):
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

    def _photo_detail(self, req: Request, prin: Principal, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id, prin)
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
                "fitMode": self._fit_mode_of(photo),
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
        self,
        req: Request,
        path: Path,
        content_type: str,
        *,
        immutable: bool,
        cache: str | None = None,
        etag: str | None = None,
    ) -> Response:
        """`cache` 显式给的时候盖掉 `immutable` 算出来的那个值。

        多这一个参数是为了管理台页面：它既不是 immutable（换镜像就换了），也不该
        `max-age=3600`（升级后一小时里旧页面配新接口）。加个 `cache="no-cache"`
        比给这一种情况另写一遍"检查存在 → 算 ETag → 比 If-None-Match → 304"要好，
        那四步里任何一步抄漏了都只表现为"缓存偶尔不对"。

        `etag` 同理，是为了 `/v1/targets/db`：那个文件的身份是**内容哈希**
        （文件名就是它），而默认的 `fsbrowser.etag_for` 是 mtime 派生的。默认值在
        那里有两个毛病，写在 `_targets_db` 里。给一个参数比让它自己拼一遍 304 逻辑
        好 —— 理由与 `cache` 完全一样。
        """
        if not path.is_file():
            raise HttpError(404, "not_found", f"文件不存在：{path.name}")
        etag = etag or fsbrowser.etag_for(path)
        headers = {"Content-Type": content_type, "ETag": etag}
        headers["Cache-Control"] = cache or (
            "max-age=31536000, immutable" if immutable else "max-age=3600"
        )
        if (req.header("if-none-match") or "").strip() == etag:
            return Response(status=304, headers=headers)
        return Response(status=200, headers=headers, file=path)

    def _photo_imgdb(self, req: Request, prin: Principal, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id, prin)
        # spec §7：ETag + Cache-Control immutable。.imgdb 是照片内容的函数，
        # 内容变了 photo 会被标 ref_stale 并重新入库（换 photo_id），所以
        # immutable 在这里是真的成立，不是图省事。
        return self._static_file(
            req,
            Path(str(photo["imgdb_path"])),
            "application/octet-stream",
            immutable=True,
        )

    def _photo_thumb(self, req: Request, prin: Principal, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id, prin)
        return self._static_file(
            req, Path(str(photo["thumb_path"])), "image/jpeg", immutable=True
        )

    def _photo_ref(self, req: Request, prin: Principal, photo_id: str) -> Response:
        """原始参考图（**不是**缩略图）。给「保存到相册」用。

        为什么单开一个口子，而不是让客户端去拼 `/v1/asset/<refAssetId>/stream`：
        那要求响应里带上 `ref_asset_id`，等于把内部 asset id 暴露给所有能识别的人，
        而 asset 是跨照片共享的表 —— 多一个可枚举的 id 就多一条越权的路。这里用
        photo_id 进来，权限判定与其它 `/v1/photo/*` 完全一致（`_photo_or_404` 里
        已经按授权过滤过）。

        `immutable` 与 thumb 同理：参考图内容变了会被标 ref_stale 并重新入库。

        Content-Type 走 [_content_type_of]（按扩展名）。**不能一律写 image/jpeg** ——
        入库允许 PNG/WebP，而客户端存进相册时是按 MIME 建文件的，标错了相册里就是
        一张打不开的图。
        """
        photo = self._photo_or_404(photo_id, prin)
        asset = self.catalog.get_asset(str(photo["ref_asset_id"]))
        if asset is None:
            raise HttpError(404, "not_found", f"参考图 asset 不存在：{photo_id}")
        path = Path(str(asset["nas_path"]))
        if not path.exists():
            # 原图在 NAS 上被挪走/删了。404 而不是 500：这不是服务端故障，而且
            # 客户端该做的事（告诉用户这张原图没了）与其它 404 一致。
            raise HttpError(404, "ref_missing", f"原图不在了：{path}")
        return self._static_file(
            req, path, _content_type_of(path), immutable=True
        )

    def _photo_media(self, req: Request, prin: Principal, photo_id: str) -> Response:
        photo = self._photo_or_404(photo_id, prin)
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

    def _photo_attach_video(
        self, req: Request, prin: Principal, photo_id: str
    ) -> Response:
        # 两道检查都要，顺序也有讲究：先按"这张照片授权给你了吗"回 403 forbidden，
        # 再要求 admin。
        #
        # 为什么不止 photo 级授权：这个接口吃一个 `videoPath` 并把它送进
        # `roots.resolve`，也就是说调用方能靠"403 path_denied"和"404 video_not_found"
        # 的区别去探 NAS 上有哪些文件 —— 那正是把 `/v1/fs/*` 定成 admin only 想避免
        # 的事（给 viewer 等于开放一个文件浏览器）。而且"给照片配视频"本身是策展
        # 动作，不是看照片的人该做的。
        #
        # 先判 photo 再判 admin，是为了让"这张照片跟你无关"这个更根本的原因优先
        # 显示出来：一个 viewer 拿着别人的 photoId 来调，他该知道的是"这张不是你的"，
        # 而不是"这个操作要管理员"（后者会让他去找管理员要权限，而权限不是问题所在）。
        self._photo_or_404(photo_id, prin)
        self._require_admin(prin, "给照片关联视频")
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

    def _asset_stream(self, req: Request, prin: Principal, asset_id: str) -> Response:
        if not _ID_RE.match(asset_id):
            raise HttpError(404, "not_found", f"assetId 格式不对：{asset_id}")
        asset = self.catalog.get_asset(asset_id)
        if asset is None:
            raise HttpError(404, "not_found", f"asset 不存在：{asset_id}")
        self._require_asset_access(prin, asset_id)
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

    def _require_asset_access(self, prin: Principal, asset_id: str) -> None:
        """asset 级的授权：**必须反查它属于哪张 photo**，再按那张照片判。

        这个接口吃的是 asset id 而不是 photo id，所以它是整套授权最容易被绕过的
        地方：不反查的话，任何一个拿到 asset id 的人就能取到视频流本身，而 asset id
        会从 `/v1/photo/<id>/media` 的响应里发出去 —— 一个 viewer 只要在别处见过
        一次那个 id（截图、日志、别人的手机），照片授权那一整套就完全不作数了。
        取到的还是最要紧的东西：视频内容。

        反查用 `photos_referencing_asset`：一个 asset 可能被多张 photo 引用（同一个
        视频关联给了好几张照片；`upsert_asset` 按 nas_path 复用记录，就是为了这个）。
        **只要有一张授权给他就放行** —— 他本来就能通过那张照片的 media 接口拿到同一
        条流，在这里拦住只会让那张照片的视频播不了。

        查不到归属的 asset 只有 admin 能取。这类 asset 是真实存在的：入库中途失败
        留下的、以及 `attach_video` 换过视频之后被解绑的旧记录。对它们"没有任何
        photo 授权"是事实，而按 `_may_see` 的逻辑那会变成"谁都不能取"（包括
        grant_all 的人），所以这条分支必须显式写出来 —— 否则运维想验证一个孤儿
        asset 还能不能读都做不到。
        """
        if prin.is_admin:
            return
        owners = self.catalog.photos_referencing_asset(asset_id)
        if not owners:
            raise HttpError(
                403, "forbidden", "这个素材没有挂在任何照片上，只有管理员能取"
            )
        if not any(self._may_see(prin, str(p["id"])) for p in owners):
            raise HttpError(403, "forbidden", "这个素材所属的照片没有授权给你")

    def _fs_list(self, req: Request, prin: Principal) -> Response:
        # admin only：这个接口能列出白名单根目录下的全部文件名。给 viewer 等于把一个
        # 文件浏览器开放出去 —— 而 viewer 的定位是"扫墙上那几张照片"，他连自己没被
        # 授权的照片的标题都不该看到，更不该看到 NAS 的目录结构。
        self._require_admin(prin, "浏览 NAS 目录")
        return json_response(200, fsbrowser.list_dir(self.roots, req.q1("path")))

    def _fs_thumb(self, req: Request, prin: Principal) -> Response:
        # 与 fs/list 同理：它能给出白名单内**任何**图片的缩略图，绕开照片授权。
        self._require_admin(prin, "预览 NAS 文件")
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

    def _create_photo(self, req: Request, prin: Principal) -> Response:
        # admin only：入库要读 NAS 上任意白名单路径、跑 arcoreimg 与 ffmpeg
        # （几十秒的 CPU），而且它是"库里有哪些照片"这件事的唯一入口。
        self._require_admin(prin, "入库")
        doc = req.json_body()
        ref_raw = doc.get("refPath")
        if not ref_raw:
            raise HttpError(400, "missing_ref_path", "需要 refPath")
        ref = self.roots.resolve(str(ref_raw))
        video = (
            self.roots.resolve(str(doc["videoPath"])) if doc.get("videoPath") else None
        )
        # printWidthMm 是**可选**的：省略 = 不知道实际尺寸，交给 ARCore 自己量。
        #
        # 这里原来强制必填，理由是"跟踪精度依赖它"。那个理由半对半错，而错的那半更
        # 要紧：实际照片尺寸经常就是不知道的，强制必填的结果是有人随手填一个数，而
        # 一个**猜的**宽度比不填更糟 —— ARCore 会当真并照它回显 getExtentX，端上按
        # 这个错数字画四边形，位姿却来自量纲真实的 SLAM，两个尺度错位百分之几，视频
        # 就比照片大百分之几、边缘对不齐。不填时 ARCore 自己量，测量值与位姿自洽。
        #
        # 对的那半保留下来了：**知道**真实宽度时填上确实更好（ARCore 不必估尺度，
        # 检测更快更稳），所以这个字段留着，只是不再强制。
        #
        # 库里存 0.0 表示未知（`print_width_m REAL NOT NULL` 不动，0 是合法值，
        # 不需要迁移）。客户端那一侧同样以 0 为"未知"，见 Android 的 Geometry.quadSize。
        width_mm = doc.get("printWidthMm")
        if width_mm is None:
            print_width_m = 0.0
        else:
            try:
                print_width_m = float(width_mm) / 1000.0
            except (TypeError, ValueError) as exc:
                raise HttpError(
                    400, "bad_print_width", f"printWidthMm 不是数字：{width_mm!r}"
                ) from exc
            if print_width_m < 0:
                raise HttpError(
                    400,
                    "bad_print_width",
                    f"printWidthMm 不能是负数，收到 {width_mm!r}。"
                    f"不知道实际尺寸就整个省略这个字段。",
                )

        # 两道闸门与质量分下限都从热配置取，让那三个 `needs_restart=False` 的字段
        # 真的能生效（后果写在 `ingest.ingest_photo` 那几个参数的注释里）。
        values = self.config.all()
        result = ingest.ingest_photo(
            cfg=self.cfg,
            catalog=self.catalog,
            library=self.library,
            ref_path=ref,
            video_path=video,
            print_width_m=print_width_m,
            title=doc.get("title"),
            quality_gate=bool(values["ingest.quality_gate"]),
            min_quality_score=int(values["ingest.min_quality_score"]),
            dedup_gate=bool(values["ingest.dedup_gate"]),
            synth_long_edge=int(values["ingest.synth_long_edge"]),
            # 把**入库那一刻**的全局默认写进 photo.fit_mode，而不是留 NULL 跟随全局。
            #
            # 两种做法的差别只在一句话："以后改了全局默认，已入库的照片跟不跟着变"。
            # 写死 = 不跟着变。选它是因为 fit_mode 决定的是"这条视频在这张照片上长
            # 什么样"，用户为某张照片单独调过之后，改一次全局默认把它悄悄改回去是
            # 最难解释的一类行为。db 那边的注释担心的是另一面（逐张存值会让"改全局
            # 默认"变成"改全局默认 + 批量刷全表"），代价确实是这个：改全局只影响新
            # 入库的照片，老照片要逐张改（`Catalog.set_photo_fit_mode`，设回 NULL
            # 就是恢复跟随全局）。
            fit_mode=str(values["video.fit_mode"]),
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

    def _photo_replace_ref(
        self, req: Request, prin: Principal, photo_id: str
    ) -> Response:
        """换掉这张照片的参考图，photo_id 不变。

        检查顺序与 `_photo_attach_video` 一致（先 photo 级授权，再 admin），理由也
        一样：一个拿着别人 photoId 来调的 viewer 该知道的是「这张不是你的」，而不是
        「这个操作要管理员」。

        为什么要有这个接口：`POST /v1/photo` 只能新建，而「先拿手机拍的糊照片入了库、
        后来有了扫描件」是真实需求。走「删掉重建」的话授权会全丢
        （`photo_grant.photo_id` 是 ON DELETE CASCADE），而且删除要把识别库里后面
        所有 slot 往前挪 —— 完整论证在 `ingest.replace_ref` 的 docstring 里。
        """
        self._photo_or_404(photo_id, prin)
        self._require_admin(prin, "换参考图")
        doc = req.json_body()
        raw = doc.get("refPath")
        if not raw:
            raise HttpError(400, "missing_ref_path", "需要 refPath")
        ref = self.roots.resolve(str(raw))
        values = self.config.all()
        result = ingest.replace_ref(
            cfg=self.cfg,
            catalog=self.catalog,
            library=self.library,
            photo_id=photo_id,
            ref_path=ref,
            quality_gate=bool(values["ingest.quality_gate"]),
            min_quality_score=int(values["ingest.min_quality_score"]),
            dedup_gate=bool(values["ingest.dedup_gate"]),
            synth_long_edge=int(values["ingest.synth_long_edge"]),
        )
        return json_response(
            200,
            {
                "photoId": result.photo_id,
                "qualityScore": result.quality_score,
                "selfScore": result.self_score,
                "imgdbBytes": result.imgdb_bytes,
                "slot": result.slot,
                "elapsedMs": result.elapsed_ms,
            },
        )

    def _upload(self, req: Request, prin: Principal) -> Response:
        # admin only：它往 NAS 上写文件。
        self._require_admin(prin, "上传")
        # spec §9.4：Cloudflare 免费版有请求体上限，隧道上传超了会被它掐断。
        # **按体积拒，不按来路拒** —— 网页版的正常访问路径就是隧道，几十 MB 的
        # 照片＋短视频完全传得过去。理由见 config.TUNNEL_MAX_UPLOAD_BYTES。
        if _via_tunnel(req) and req.content_length > TUNNEL_MAX_UPLOAD_BYTES:
            mb = TUNNEL_MAX_UPLOAD_BYTES / (1024 * 1024)
            # 文件体积保留一位小数：整数会把 95.4MB 印成 "95MB 超过 95MB 上限"，
            # 一句自相矛盾的话（实测过）。
            raise HttpError(
                413,
                "upload_via_tunnel",
                f"这个文件 {req.content_length / (1024 * 1024):.1f}MB，超过了 Cloudflare "
                f"隧道的 {mb:g}MB 请求体上限。连回家庭网络或开启 Tailscale 后再传，"
                "那两条路没有这个限制。",
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
        if not dst.exists():
            written = req.stream_to(dst, MAX_UPLOAD_BYTES)
            return json_response(
                201, {"path": str(dst), "bytes": written, "reused": False}
            )

        # 同名文件已经在了。**不能直接 409 了事** —— 从手机相册第二次挑同一张照片，
        # 拿到的就是同一个文件名，而那时用户要的不是一句「已存在」，是「那张照片
        # 现在配的是哪段视频」。所以先看内容一不一样。
        #
        # 落到临时文件再比哈希，而不是先读进内存：这条路上可能是一段几百 MB 的视频。
        # 临时名带 pid 与线程 id，两个管理员同时传同名文件时不会互相踩。
        tmp = dst.with_name(
            f"{dst.name}.upload-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            written = req.stream_to(tmp, MAX_UPLOAD_BYTES)
            same = sha256_file(tmp) == sha256_file(dst)
        finally:
            tmp.unlink(missing_ok=True)

        if same:
            # 同名同内容 = 这个文件已经在服务端了，直接复用那条路径。
            # 200 而不是 201：没有新建任何东西。`reused` 让调用方能把这件事说给用户
            # （App 那边会顺手去查这个文件在库里的身份，见 `_admin_lookup`）。
            return json_response(
                200, {"path": str(dst), "bytes": written, "reused": True}
            )
        raise HttpError(
            409,
            "name_taken",
            f"服务端已经有一个叫 {safe} 的文件，但**内容不一样**。"
            f"换个文件名再传（比如加上日期），否则会覆盖别人的素材。",
            existingPath=str(dst),
            suggestedName=_suggest_name(safe),
        )

    def _history(self, req: Request, prin: Principal) -> Response:
        # admin only：它是**全库**的识别记录，带标题和缩略图 URL。按 user 过滤这条
        # 记录流没有意义（recognize_log 里没有"谁扫的"这一列，schema 由需求给定），
        # 而不过滤就等于把全库照片的标题发给任何一个 viewer。
        self._require_admin(prin, "查看识别记录")
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
                    # 未命中时这三个才是有信息量的那部分：`weak` 要改取景、
                    # `ambiguous` 要清库，而只看 inliers 分不出是哪一种。
                    # 旧记录这几列是 NULL（加它们之前记的），界面上按缺省显示。
                    "reason": r["reason"] if "reason" in r.keys() else None,
                    "runnerUp": r["runner_up"] if "runner_up" in r.keys() else None,
                    "topk": (
                        json.loads(r["topk_json"])
                        if ("topk_json" in r.keys() and r["topk_json"])
                        else None
                    ),
                }
            )
        return json_response(200, {"entries": rows})

    # ---- 管理接口（全部 admin only）----

    _USER_MGMT = "用户管理"

    def _user_json(self, row: dict[str, Any]) -> dict[str, Any]:
        uid = str(row["id"])
        return {
            "id": uid,
            "name": row["name"],
            # 规范化后的名字（登录时真正用来查人的那个键）。
            #
            # 暴露它是为了批量导入：执行者是浏览器，它得把表里的「张三 」对上库里
            # 已有的「张三」。让 JS 自己实现一遍 `normalize_name` 是错的 —— casefold
            # 与 toLowerCase 对某些字符结果不同（ß → ss），两套实现只要有一处不一致，
            # 表现就是**授权静默不生效**：用户建出来了、照片入库了，就是没关联上，
            # 而界面上每一步都显示成功。把服务端算好的那个键发出来，匹配就由构造保证。
            "nameKey": row["name_key"],
            "role": row["role"],
            "disabled": bool(row["disabled"]),
            "grantAll": bool(row["grant_all"]),
            # 逐张授权的**真实**张数，即使这个人 grant_all 也不换成全库张数：与
            # `Catalog.is_granted` 同一个口径（那个方法的注释解释了为什么授权表的
            # 事实不该被"看全部"的策略盖掉）。管理台把 grant_all 的勾去掉时，这个人
            # 剩下的就是这几张，界面必须能在关掉之前就显示出来。
            #
            # 一个用户一次查询（N+1）。家庭规模是个位数账号，换成一次 GROUP BY 要
            # 在 db 层加一个只有这一处用的方法，不值得。
            "grantCount": len(self.catalog.granted_photo_ids(uid)),
            "createdAt": int(row["created_at"]),
            "lastSeenAt": row["last_seen_at"],
        }

    def _user_or_404(self, user_id: str) -> dict[str, Any]:
        row = self.catalog.get_user(user_id)
        if row is None:
            raise HttpError(404, "not_found", f"用户不存在：{user_id}")
        return row

    @staticmethod
    def _check_password_for_role(role: str, password: str | None) -> None:
        """admin 必须有口令，viewer 不许有。

        前半句 `Auth.create_user` 自己也会保证；这里先判一次只为了给出一个 400 而
        不是让 `BadCredentials` 冒到 401 —— "建号时少填了一个字段"是输入错误，不是
        "你的凭证不对"，而 401 会让管理台去弹重新登录。

        后半句是**这一层的产品策略**：数据层允许 viewer 有口令（schema 没禁，
        `Auth.login` 也会如实去验）。在这里挡住是因为 viewer 的整个前提是"只输名字
        就能进"（家里人隔几周才用一次），一部分 viewer 要输口令一部分不要，会让
        "我到底该不该输"变成只有管理员知道答案的问题。

        带了口令就 **400，不静默丢掉**：丢掉的话管理员会以为自己给这个人设上了
        口令，而实际上任何知道这个名字的人都能进 —— 一个自认为做了防护的空防护。
        """
        if role == ADMIN and not password:
            raise HttpError(400, "password_required", "管理员必须设口令")
        if role == VIEWER and password:
            raise HttpError(
                400,
                "password_not_allowed",
                "访客账号不设口令：登录只输名字。要口令的话建成管理员。",
            )

    def _admin_list_users(self, req: Request, prin: Principal) -> Response:
        self._require_admin(prin, self._USER_MGMT)
        return json_response(
            200, [self._user_json(u) for u in self.catalog.list_users()]
        )

    def _admin_create_user(self, req: Request, prin: Principal) -> Response:
        self._require_admin(prin, self._USER_MGMT)
        doc = req.json_body()
        name = doc.get("name")
        if not isinstance(name, str):
            raise HttpError(400, "missing_name", "需要 name")
        role = doc.get("role")
        if role not in ROLES:
            raise HttpError(400, "bad_role", f"role 只能是 {list(ROLES)}，收到 {role!r}")
        password = doc.get("password")
        if password is not None and not isinstance(password, str):
            raise HttpError(400, "bad_password", "password 必须是字符串")
        self._check_password_for_role(str(role), password)
        try:
            uid = self.auth.create_user(
                name=name,
                role=str(role),
                password=password or None,
                grant_all=bool(doc.get("grantAll")),
            )
        except InvalidName as exc:
            raise HttpError(400, "bad_name", str(exc)) from exc
        except NameTaken as exc:
            # 409 而不是 400：名字重复不是"你填错了格式"，是"这个名字被占了"，
            # 管理台该显示的是"换一个名字"而不是"检查输入"。
            raise HttpError(409, "name_taken", str(exc)) from exc
        return json_response(201, self._user_json(self._user_or_404(uid)))

    def _admin_patch_user(
        self, req: Request, prin: Principal, user_id: str
    ) -> Response:
        self._require_admin(prin, self._USER_MGMT)
        row = self._user_or_404(user_id)
        doc = req.json_body()

        # ---- 先全部校验，一个字段都还没写 ----
        #
        # 与 `AppConfig.patch` 同一个理由：一次提交里有一个非法值就整批拒绝。这里更
        # 要紧，因为半套生效的用户改动可能是"角色已经降成 viewer 了、口令还没清"
        # —— 一个谁都登不进去的账号。
        role = doc.get("role")
        if role is not None:
            if role not in ROLES:
                raise HttpError(
                    400, "bad_role", f"role 只能是 {list(ROLES)}，收到 {role!r}"
                )
            role = str(role)
        password = doc.get("password")
        if password is not None and not isinstance(password, str):
            raise HttpError(400, "bad_password", "password 必须是字符串")
        name = doc.get("name")
        if name is not None and not isinstance(name, str):
            raise HttpError(400, "bad_name", "name 必须是字符串")
        disabled = None if doc.get("disabled") is None else bool(doc["disabled"])
        grant_all = None if doc.get("grantAll") is None else bool(doc["grantAll"])

        # 不许把**自己**降级或停用。
        #
        # 这不是"防手滑"这么轻的事：管理入口是唯一能改配置、建号、发授权的地方，
        # 而它只认库里的 admin 行。把自己降成 viewer 之后没有任何 HTTP 接口能升回来
        # （升级需要 admin 身份），`ensure_bootstrap_admin` 也救不了 —— 它的判据是
        # "存在任何 admin 行"，**包括被停用的那些**（见那边的注释：否则"停用唯一的
        # 管理员"会让下次启动悄悄建出第二个管理员）。剩下的唯一出路是进容器用
        # sqlite3 改库。
        #
        # 只挡"自己"就够，不需要另写一条"不能让 admin 数量降到 0"：任何一次操作的
        # 执行者本身就是一个启用着的 admin，而他动不了自己，所以"至少还有一个启用的
        # admin"这条不变式自动成立。而按数量判会把"只有一个管理员时改自己的名字"
        # 这种无关操作也一起拖进判断里。
        is_self = prin.user_id is not None and str(row["id"]) == prin.user_id
        if is_self and role is not None and role != ADMIN:
            raise HttpError(
                400,
                "cannot_demote_self",
                "不能把自己降级：降完就没人能把你升回来了，只能进容器改库。"
                "让另一个管理员来做，或者先建一个新管理员。",
            )
        if is_self and disabled:
            raise HttpError(
                400, "cannot_disable_self", "不能停用自己：停完就登不进管理台了。"
            )

        new_role = role or str(row["role"])
        if password is not None:
            self._check_password_for_role(new_role, password)
        elif new_role == ADMIN and str(row["role"]) != ADMIN and not row["pwd_hash"]:
            # 把一个没有口令的 viewer 升成 admin，必须在同一个请求里给口令。
            # 不拦的话库里会出现一个 pwd_hash 为 NULL 的 admin 行 —— `Auth.login`
            # 会拒绝它登录（那边刻意写了"没散列也不放行"），所以结果是一个谁都用不了
            # 的管理员，而管理台上它看起来是个正常的 admin。
            raise HttpError(
                400,
                "password_required",
                "升成管理员必须同时设口令（管理员登录一定要验口令）",
            )

        # ---- 校验全过了，开始写 ----
        if name is not None:
            try:
                shown, key = check_name(name)
            except InvalidName as exc:
                raise HttpError(400, "bad_name", str(exc)) from exc
            try:
                self.catalog.rename_user(user_id, name=shown, name_key=key)
            except NameTaken as exc:
                raise HttpError(409, "name_taken", str(exc)) from exc

        self.catalog.update_user(
            user_id, role=role, grant_all=grant_all, disabled=disabled
        )

        if password is not None:
            # 空串走到这里只可能是 viewer（admin 已被 _check_password_for_role 拦
            # 下），语义是"清掉口令"。`set_password` 顺带踢掉他的全部会话。
            self.auth.set_password(user_id, password or None)
        elif role == VIEWER and str(row["role"]) == ADMIN:
            # 降级成 viewer 必须把口令清掉。留着的话这个 viewer 从此要输口令才能登
            # （`Auth.login` 对"pwd_hash 有值的 viewer"是真的会验的），而管理台上
            # viewer 根本没有口令那一栏 —— 谁都不知道该输什么，包括管理员自己。
            self.auth.set_password(user_id, None)

        if disabled:
            # 停用要立刻把他手上的会话删掉。
            #
            # 不是安全必需（`Auth.principal_of` 已经会拒绝已停用账号的 session），
            # 而是 db 层那句"要立刻踢掉全部设备就调 delete_sessions_of_user，那是
            # 停用这个动作本身该做的事"指的就是这里。代价是失去"停用再启用，手机上
            # 那个 token 还能接着用"这点便利 —— 而"停用"在家用场景下的动机基本只有
            # "别让他再看了"，让他重新输一次名字不算代价。
            self.catalog.delete_sessions_of_user(user_id)

        return json_response(200, self._user_json(self._user_or_404(user_id)))

    def _admin_delete_user(
        self, req: Request, prin: Principal, user_id: str
    ) -> Response:
        self._require_admin(prin, self._USER_MGMT)
        row = self._user_or_404(user_id)
        # 理由与 `cannot_demote_self` 同一段。删自己比降级自己更彻底：连
        # `disabled` 那条"起码库里还有一行"的退路都没有。
        #
        # 运维凭证（user_id 为 None）删得掉任何管理员，包括最后一个。这不是漏洞
        # 而是它的定位：它躺在 `.env` 里、不对应任何人、而且下次启动
        # `_bootstrap_admin` 会按环境变量重新建出引导管理员来。
        if prin.user_id is not None and str(row["id"]) == prin.user_id:
            raise HttpError(
                400,
                "cannot_delete_self",
                "不能删自己：删完就登不进管理台了。让另一个管理员来做。",
            )
        # ⚠️ 连带删掉他的全部会话与全部逐张授权（外键 CASCADE），不可撤销：重建同名
        # 账号拿到的是新的 user.id，之前一张张勾出来的授权不会回来。"临时不让某人
        # 登录"要用 PATCH 的 disabled。
        self.catalog.delete_user(user_id)
        return Response(status=204)

    def _admin_get_grants(
        self, req: Request, prin: Principal, user_id: str
    ) -> Response:
        self._require_admin(prin, self._USER_MGMT)
        row = self._user_or_404(user_id)
        return json_response(
            200,
            {
                "grantAll": bool(row["grant_all"]),
                "photoIds": self.catalog.granted_photo_ids(user_id),
            },
        )

    def _admin_put_grants(
        self, req: Request, prin: Principal, user_id: str
    ) -> Response:
        self._require_admin(prin, self._USER_MGMT)
        self._user_or_404(user_id)
        doc = req.json_body()
        raw = doc.get("photoIds")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise HttpError(400, "bad_photo_ids", "photoIds 必须是数组")
        # 去重但保持顺序：`replace_grants` 用的是 `INSERT OR IGNORE`，重复项本来
        # 也不会出错，去重只是为了下面报错时不把同一个 id 列三遍。
        ids = list(dict.fromkeys(str(x) for x in raw))

        # 先自己验一遍 photo 存不存在，而不是靠 `photo_grant` 的外键去挡。外键确实
        # 会挡下并整体回滚（`replace_grants` 就是这么设计的，注释里写了宁可整批失败
        # 也不要"勾了 10 张成了 7 张"），但它抛出来的是一句 `sqlite3.IntegrityError`
        # ，答不上"是哪几个 id 不存在" —— 而管理台一次提交的是几十个勾选框，只说
        # "有一个不对"等于让人一个一个试。
        unknown = [pid for pid in ids if self.catalog.get_photo(pid) is None]
        if unknown:
            raise HttpError(
                400,
                "unknown_photo",
                f"这些 photoId 不存在：{unknown[:10]}",
                unknownPhotoIds=unknown,
            )

        grant_all = doc.get("grantAll")
        if grant_all is not None:
            self.catalog.update_user(user_id, grant_all=bool(grant_all))
        # 整体替换，不是增量：管理台勾选框提交的语义就是"这就是全集"。
        self.catalog.replace_grants(user_id, ids)
        row = self._user_or_404(user_id)
        return json_response(
            200,
            {
                "grantAll": bool(row["grant_all"]),
                "photoIds": self.catalog.granted_photo_ids(user_id),
            },
        )

    def _admin_get_config(self, req: Request, prin: Principal) -> Response:
        self._require_admin(prin, "配置")
        # `fields` 是字段声明（标签、说明、范围、默认值、要不要重启），`values` 是
        # 当前值。两个一起给，管理台一次调用就能把整个表单画出来 —— 包括在还没有人
        # 改过任何配置的时候（那些信息只能来自代码，见 `appconfig.Field`）。
        return json_response(
            200, {"fields": self.config.describe(), "values": self.config.all()}
        )

    def _admin_patch_config(self, req: Request, prin: Principal) -> Response:
        self._require_admin(prin, "配置")
        doc = req.json_body()
        try:
            needs_restart = self.config.patch(doc)
        except ConfigRejected as exc:
            # `BadConfigKey` 与 `BadConfigValue` 都是它的子类，一个 except 够。
            # 400 而不是 422：这是"你发过来的东西不对"，管理台该原地显示在那个
            # 输入框旁边。
            raise HttpError(400, "bad_config", str(exc)) from exc
        # `patch` 只返回**确实变了且需要重启**的 key（理由在那边）：每次点保存都
        # 提示"需要重启"喊几次狼来了之后，真需要重启时也不会有人当真。
        return json_response(200, {"needsRestart": needs_restart})

    def _admin_rebuild_vocab(self, req: Request, prin: Principal) -> Response:
        """用库里已有的描述子训一份词表，然后重建全库词序列与倒排索引。

        admin only，理由与其它管理接口不同：这不是"会泄露什么"，而是**它会占满 CPU
        几分钟并且期间入库要排在同一把写锁后面**。给 viewer 等于让任何一个家里人能
        把服务按住。

        没有"进度"这回事，是一次同步调用：训练是一段纯 CPU 的 k-means，没法在中途给出
        有意义的百分比，而假的百分比比没有更糟。库大到会超时的时候用
        `photoar-server build-vocab`（CLI 那条路没有 HTTP 超时）—— 响应里那个
        `elapsedMs` 正是让用户知道下次该不该走 CLI 的依据。

        ⚠️ 训完的词表**只对这个进程与这个后端生效**：另一个后端的库还是它自己那份
        词表（两种词表格式不兼容）。这在响应里如实报出 `backend`。
        """
        self._require_admin(prin, "重建词表")
        backend = self.library.backend
        out = self.cfg.vocab_path_for(backend.name, backend.vocab_file)
        try:
            result = self.library.train_vocab(out)
        except EmptyLibrary as exc:
            # 409 而不是 400/500：请求本身没问题，是**服务端当前状态**不允许
            # （"库是空的"）。入库几张之后同一个请求就会成功。
            raise HttpError(409, "library_empty", str(exc)) from exc
        return json_response(
            200,
            {
                "backend": backend.name,
                "vocabPath": str(result.path),
                "photos": result.n_photos,
                "descriptors": result.n_descriptors,
                "words": result.n_words,
                "elapsedMs": result.elapsed_ms,
            },
        )

    # ---- 批量导入 / 导出 / 双向映射 ----

    # 导入文件的体积上限。5000 行 × 10 列的中文 xlsx 实测约 1 MB，8 MiB 有充足余量。
    # 单独一个常量而不是复用 MAX_UPLOAD_BYTES（几百 MB）：那个是给视频用的，拿它当
    # 表格的上限等于允许把一个视频当表格传进来，然后在 zip 解析里慢慢失败。
    MAX_IMPORT_BYTES = 8 * 1024 * 1024

    def _admin_import_parse(self, req: Request, prin: Principal) -> Response:
        """把上传的表格解析成一份**执行计划**，一行都不写库。

        请求体是文件的原始字节（不是 multipart）—— 浏览器 `fetch(url, {body: file})`
        就是这个形状，而 multipart 只是为了在一个请求里塞多个字段，这里只有一个文件。

        为什么只解析不执行：见 `batch` 模块的 docstring。要点是几十行的表逐行执行要
        几分钟（每张照片都要跑 arcoreimg + 特征，视频还可能转码），做成一个同步接口
        会先被反向代理的超时掐断，而且第 37 行才发现路径写错时前 36 行已经落库了。
        由浏览器拿着这份计划去逐个调既有接口，预演、进度、逐行重试就都是免费的。
        """
        self._require_admin(prin, "批量导入")
        raw = req.read_body(self.MAX_IMPORT_BYTES)
        if not raw:
            raise HttpError(400, "empty_body", "请求体是空的，需要上传 .xlsx 或 .csv")
        try:
            table = sheet_mod.read_table(raw)
        except SheetError as exc:
            # SheetError 的 code 直接当 HTTP 的 error code 用（bad_xlsx /
            # bad_encoding / sheet_too_big）—— 它们本来就是给人看的分类。
            raise HttpError(400, exc.code, exc.message) from exc
        plan = batch.build_plan(
            table,
            normalize_name=normalize_name,
            check_path=self._check_import_path,
        )
        payload = plan.to_json()
        payload["format"] = sheet_mod.detect_format(raw)
        # no-store 而不是 no-cache：响应体里含表格里的口令原文（理由见
        # `batch.PlanRow.to_json`）。no-cache 允许存下来但每次revalidate，
        # 那还是存在磁盘上了；no-store 是「一个字节都别落盘」。
        return json_response(200, payload, **{"Cache-Control": "no-store"})

    def _check_import_path(self, raw: str, kind: str) -> str | None:
        """校验表里的一个路径。返回错误信息，或 None 表示没问题。

        在**预览**阶段就查白名单和文件是否存在，是这个设计最实用的部分：一份表里最
        常见的错就是路径写错（复制粘贴时带了空格、写的是 Windows 路径、写的是宿主机
        路径而不是容器内路径），而它们现在全在动手之前一次说完。

        故意不区分「不在白名单」和「文件不存在」的措辞严厉程度 —— 调用方已经是
        admin，`/v1/fs/list` 对他是开放的，这里没有可泄露的东西。
        """
        try:
            path = self.roots.resolve(raw)
        except PathDenied as exc:
            return (
                f"{exc}。路径要写**容器内**的路径（和 PHOTOAR_ROOTS 一致），"
                "不是宿主机上的路径。"
            )
        if not path.exists():
            return f"文件不存在：{path}"
        if not path.is_file():
            return f"这是个目录，不是文件：{path}"
        actual = fsbrowser.kind_of(path)
        if actual != kind:
            want = "图片" if kind == "image" else "视频"
            got = {"image": "图片", "video": "视频"}.get(actual or "", "认不出的类型")
            return f"这一列要{want}，但 {path.name} 是{got}"
        return None

    # ---- 素材挂载点 ----

    _MOUNT_MGMT = "管理素材挂载点"

    def _mount_json(self, row: dict[str, Any]) -> dict[str, Any]:
        """一个挂载点的对外形状。

        **口令永不回显。** 只给一个布尔，管理台用它显示「已设置口令」。回显它没有任何
        用处（管理台不需要拿它去别处认证），而一个会把口令发出来的接口迟早会被某个
        日志、某个代理、某个截图带出去。
        """
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "kind": row["kind"],
            "location": row["location"],
            "username": row["username"],
            "hasPassword": bool(row["password"]),
            "enabled": bool(row["enabled"]),
            "createdAt": int(row["created_at"]),
        }

    def _mount_or_404(self, mount_id: str) -> dict[str, Any]:
        row = self.catalog.get_mount(mount_id)
        if row is None:
            raise HttpError(404, "mount_not_found", f"没有这个挂载点：{mount_id}")
        return row

    def _admin_list_mounts(self, req: Request, prin: Principal) -> Response:
        self._require_admin(prin, self._MOUNT_MGMT)
        rows = [self._mount_json(m) for m in self.catalog.list_mounts()]
        return json_response(
            200,
            {
                "mounts": rows,
                # 环境变量给的那几个根也列出来（只读）。不列的话管理台上会出现
                # 「我明明配了 /share/Photo，怎么这里是空的」这种困惑 —— 那几个根
                # 确实存在，只是不是在这里配的。
                "envRoots": [
                    {"name": r.name, "path": str(r.path)}
                    for r in Roots(self._env_roots).roots
                ],
            },
        )

    def _admin_create_mount(self, req: Request, prin: Principal) -> Response:
        self._require_admin(prin, self._MOUNT_MGMT)
        doc = req.json_body()
        name, kind, location = self._mount_fields(doc, require=True)
        try:
            mid = self.catalog.create_mount(
                name=name,
                kind=kind,
                location=location,
                username=(doc.get("username") or None),
                password=(doc.get("password") or None),
                enabled=bool(doc.get("enabled", True)),
            )
        except NameTaken as exc:
            raise HttpError(409, "name_taken", str(exc)) from exc
        row = self._mount_or_404(mid)
        self._after_mount_change(row, "新增")
        return json_response(201, self._mount_json(row))

    def _admin_patch_mount(
        self, req: Request, prin: Principal, mount_id: str
    ) -> Response:
        self._require_admin(prin, self._MOUNT_MGMT)
        self._mount_or_404(mount_id)
        doc = req.json_body()
        name, kind, location = self._mount_fields(doc, require=False)
        try:
            self.catalog.update_mount(
                mount_id,
                name=name,
                kind=kind,
                location=location,
                username=doc.get("username"),
                # `password` 缺省 = 不动（管理台的口令框是空的，因为服务端不回显）。
                # 要真的清空得显式传空字符串，`update_mount` 那边解释了为什么这两件
                # 事必须分得开。
                password=doc.get("password"),
                enabled=doc.get("enabled"),
            )
        except NameTaken as exc:
            raise HttpError(409, "name_taken", str(exc)) from exc
        row = self._mount_or_404(mount_id)
        self._after_mount_change(row, "改动")
        return json_response(200, self._mount_json(row))

    def _admin_delete_mount(
        self, req: Request, prin: Principal, mount_id: str
    ) -> Response:
        self._require_admin(prin, self._MOUNT_MGMT)
        row = self._mount_or_404(mount_id)
        self.catalog.delete_mount(mount_id)
        print(f"[photoar] 删除素材挂载点 {row['name']!r}（{row['location']}）")
        self._rebuild_roots()
        # 已经入库的照片不受影响 —— 它们的 asset.nas_path 还指着原来的位置。
        # 如果那条路径只由这个挂载点覆盖着，它们会在下一次一致性检查里被标 missing，
        # 那是如实反映现状。
        return json_response(200, {"deleted": mount_id})

    def _mount_fields(
        self, doc: dict[str, Any], *, require: bool
    ) -> tuple[str | None, str | None, str | None]:
        """校验 name / kind / location 三个字段，返回规范化后的值（None = 没给）。

        校验放在写库之前一次做完：半套生效的挂载点（kind 改了、location 还没改）会让
        「浏览」用 WebDAV 客户端去打一个本地路径，报出来的错和真实原因毫无关系。
        """
        name = doc.get("name")
        if name is None:
            if require:
                raise HttpError(400, "missing_name", "需要 name")
        else:
            name = str(name).strip()
            if not name:
                raise HttpError(400, "bad_name", "name 不能是空的")

        kind = doc.get("kind")
        if kind is None:
            if require:
                raise HttpError(400, "missing_kind", "需要 kind")
        else:
            kind = str(kind)
            if kind not in MOUNT_KINDS:
                raise HttpError(
                    400, "bad_kind", f"kind 只能是 {list(MOUNT_KINDS)}，收到 {kind!r}"
                )

        location = doc.get("location")
        if location is None:
            if require:
                raise HttpError(400, "missing_location", "需要 location")
        else:
            location = str(location).strip()
            if not location:
                raise HttpError(400, "bad_location", "location 不能是空的")
            # 按最终的 kind 校验 location。PATCH 只改一个字段时，另一个要从库里取。
            effective_kind = kind
            if effective_kind is None:
                raise HttpError(
                    400,
                    "missing_kind",
                    "改 location 时要一并给出 kind（两者的校验规则不同）",
                )
            location = self._check_mount_location(effective_kind, location)
        return name, kind, location

    def _check_mount_location(self, kind: str, location: str) -> str:
        """按类型校验挂载点位置，返回规范化后的值。"""
        if kind == MOUNT_WEBDAV:
            if not location.startswith(("http://", "https://")):
                raise HttpError(
                    400,
                    "bad_location",
                    "WebDAV 的地址要以 http:// 或 https:// 开头。"
                    "群晖是 `https://<host>:5006/`，Nextcloud 是 "
                    "`https://<host>/remote.php/dav/files/<用户名>/`。",
                )
            return location.rstrip("/")

        # local：必须是绝对路径、必须存在、必须是目录。
        #
        # 要求它**已经存在**而不是自动创建：这个字段是人手打的容器内路径，打错一个字
        # （`/media/photo` 而不是 `/media/photos`）时自动创建会得到一个空目录，然后
        # 「我的照片怎么一张都没有」——而真因是路径错了。让它当场失败。
        p = Path(location)
        if not p.is_absolute():
            raise HttpError(
                400,
                "bad_location",
                f"要绝对路径，收到 {location!r}。注意填的是**容器内**的路径"
                "（和 PHOTOAR_ROOTS 一个口径），不是你电脑上的路径。",
            )
        resolved = p.expanduser().resolve()
        if not resolved.exists():
            raise HttpError(
                404,
                "location_not_found",
                f"这个路径在服务端不存在：{resolved}。填的是**容器内**的路径 —— "
                "宿主机上的目录要先在 compose 里挂进容器。",
            )
        if not resolved.is_dir():
            raise HttpError(
                400, "bad_location", f"这是个文件，不是目录：{resolved}"
            )
        return str(resolved)

    def _after_mount_change(self, row: dict[str, Any], what: str) -> None:
        """挂载点变动之后：重建白名单，并把这件事记一行。

        日志里要记，是因为 local 挂载点**扩大了服务端愿意读的范围**。admin 本来就是
        最高权限，所以这不是漏洞；但「谁什么时候加了哪个根」应该有据可查。
        """
        kind = str(row["kind"])
        print(
            f"[photoar] {what}素材挂载点 {row['name']!r}｜{kind}｜{row['location']}"
            f"｜{'启用' if row['enabled'] else '停用'}"
        )
        if kind == MOUNT_WEBDAV and row["password"] and str(
            row["location"]
        ).startswith("http://"):
            print(
                "[photoar] ⚠️ 这个 WebDAV 挂载点走明文 http 且带口令 —— "
                "Basic 认证在 http 上等于明文传口令。"
            )
        self._rebuild_roots()

    def _admin_mount_list(
        self, req: Request, prin: Principal, mount_id: str
    ) -> Response:
        """列一个挂载点下的目录。`?path=` 是相对挂载点根的路径。

        两种 kind 的响应**形状一样**（`{path, parent, entries:[{name,isDir,kind,bytes}]}`），
        这样管理台上一个文件浏览器就能同时用在本地目录和 WebDAV 上。形状不同的话那边
        要写两套渲染，而它们看起来该是一样的。
        """
        self._require_admin(prin, self._MOUNT_MGMT)
        row = self._mount_or_404(mount_id)
        if not row["enabled"]:
            raise HttpError(
                409, "mount_disabled", f"挂载点 {row['name']!r} 是停用状态"
            )
        rel = req.q1("path") or ""
        if str(row["kind"]) == MOUNT_LOCAL:
            return json_response(200, self._local_mount_list(row, rel))
        return json_response(200, self._webdav_mount_list(row, rel))

    def _local_mount_list(self, row: dict[str, Any], rel: str) -> dict[str, Any]:
        """local 挂载点走既有的 `fsbrowser.list_dir`。

        路径仍然过 `self.roots.resolve` —— 挂载点已经在白名单里了（`_rebuild_roots`
        把它加进去的），所以这里不需要、也不该另写一套前缀比较。`rel` 里的 `..`
        由那一步挡掉。
        """
        base = Path(str(row["location"]))
        target = base if not rel else base / rel.lstrip("/")
        resolved = self.roots.resolve(str(target))
        listing = fsbrowser.list_dir(self.roots, str(resolved))
        # parent 换成**相对挂载点根**的形式，让管理台不用知道绝对路径。
        listing["path"] = _rel_to(base, resolved)
        listing["parent"] = (
            None if resolved == base else _rel_to(base, resolved.parent)
        )
        return listing

    def _webdav_mount_list(self, row: dict[str, Any], rel: str) -> dict[str, Any]:
        client = self._webdav_of(row)
        try:
            entries = client.list_dir(rel)
        except WebDavError as exc:
            # WebDavError 的 code 直接当 HTTP 的 error code 用 —— 它们本来就是按
            # 「下一步该做什么」分的（改凭证 / 改地址 / 检查网络）。
            raise HttpError(502, exc.code, exc.message) from exc
        return {
            "path": rel,
            # WebDAV 这边的 parent 靠 href 算不可靠（服务端给的 href 前缀各不相同），
            # 所以按调用方传进来的 rel 退一层。rel 为空就是根，没有上级。
            "parent": None if not rel else rel.rstrip("/").rsplit("/", 1)[0],
            "entries": [
                {
                    "name": e.name,
                    # href 而不是 name：继续往下走要用它（已编码、绝对）。名字里有
                    # 斜杠或者服务端做过重写时，靠 name 拼出来的路径是错的。
                    "href": e.href,
                    "isDir": e.is_dir,
                    "kind": None if e.is_dir else fsbrowser.kind_of(e.name),
                    "bytes": e.bytes,
                    "mtime": e.mtime,
                }
                for e in entries
            ],
        }

    def _webdav_of(self, row: dict[str, Any]) -> WebDavClient:
        try:
            return WebDavClient(
                str(row["location"]),
                username=row["username"] or None,
                password=row["password"] or None,
            )
        except WebDavError as exc:
            raise HttpError(400, exc.code, exc.message) from exc

    def _admin_mount_fetch(
        self, req: Request, prin: Principal, mount_id: str
    ) -> Response:
        """把挂载点上的一个文件变成「服务端本地的一条路径」，返回那条路径。

        两种 kind 的行为不同，但**对调用方是一样的**：给一个挂载点内的路径，拿回一条
        能直接喂给 `POST /v1/photo` 的绝对路径。

        - local：不拷贝，直接返回那条绝对路径。文件本来就在服务端的文件系统上，
          拷一份只是白占一倍磁盘 —— 而这个部署形态下磁盘就是 NAS 的磁盘。
        - webdav：下载到上传落地目录（`PHOTOAR_UPLOAD_DIR`），返回落地后的路径。

        webdav 的落地文件按**原名**存，撞名时的处理与 `/v1/upload` 一致（同名同内容
        复用、同名不同内容拒绝并给出建议名）—— 两条入库前的路径行为不一致的话，
        用户会以为是挂载点的问题。
        """
        self._require_admin(prin, self._MOUNT_MGMT)
        row = self._mount_or_404(mount_id)
        if not row["enabled"]:
            raise HttpError(
                409, "mount_disabled", f"挂载点 {row['name']!r} 是停用状态"
            )
        doc = req.json_body()
        rel = doc.get("path")
        if not rel:
            raise HttpError(400, "missing_path", "需要 path（挂载点内的路径）")
        rel = str(rel)

        if str(row["kind"]) == MOUNT_LOCAL:
            base = Path(str(row["location"]))
            resolved = self.roots.resolve(str(base / rel.lstrip("/")))
            if not resolved.is_file():
                raise HttpError(404, "not_found", f"文件不存在：{resolved}")
            return json_response(
                200, {"path": str(resolved), "copied": False, "bytes": None}
            )

        if not self.cfg.upload_dir_root:
            raise HttpError(
                503,
                "upload_disabled",
                "从 WebDAV 取文件要先落到本地，而服务端没配 PHOTOAR_UPLOAD_DIR。"
                "配好它再来（local 类型的挂载点不需要这个）。",
            )
        # rel 可能是 PROPFIND 回来的 href（百分号编码的），所以先解码再取文件名。
        safe = _safe_upload_name(unquote(rel))
        dst = self.roots.resolve(str(Path(self.cfg.upload_dir_root) / safe))
        client = self._webdav_of(row)

        if dst.exists():
            tmp = dst.with_name(
                f"{dst.name}.dav-{os.getpid()}-{threading.get_ident()}"
            )
            try:
                got = client.download_to(rel, tmp, MAX_UPLOAD_BYTES)
                same = sha256_file(tmp) == sha256_file(dst)
            except WebDavError as exc:
                tmp.unlink(missing_ok=True)
                raise HttpError(502, exc.code, exc.message) from exc
            finally:
                tmp.unlink(missing_ok=True)
            if same:
                return json_response(
                    200, {"path": str(dst), "copied": False, "bytes": got}
                )
            raise HttpError(
                409,
                "name_taken",
                f"落地目录里已经有一个叫 {safe} 的文件，但内容不一样。"
                "把 WebDAV 上那个文件改个名字再取。",
                existingPath=str(dst),
                suggestedName=_suggest_name(safe),
            )

        try:
            got = client.download_to(rel, dst, MAX_UPLOAD_BYTES)
        except WebDavError as exc:
            raise HttpError(502, exc.code, exc.message) from exc
        return json_response(201, {"path": str(dst), "copied": True, "bytes": got})

    def _admin_inbox(self, req: Request, prin: Principal) -> Response:
        """落地目录里**还没有被用起来**的素材。

        为什么需要它：手机传上来的文件先落到 `PHOTOAR_UPLOAD_DIR`，然后才入库。中间任何
        一步断了（入库超时、质量分不过、近重复被拒、或者人挑完视频就退出了），那个文件就
        躺在那儿，而**管理台上任何一处都看不到它** —— 照片列表只列已入库的，挂载点浏览器
        要人自己去翻目录。用户看到的是「我传上去了，但哪儿都找不到」。

        「没被用起来」= 磁盘上有这个文件，但它不是任何 asset 的路径。已经入库的照片、
        已经配上的视频都不会出现在这里 —— 那些在照片列表里看得到。

        只看**一层**，不递归：落地目录是平的（`/v1/upload` 只允许纯文件名），递归只会把
        用户手工放进去的目录结构也扫进来。
        """
        self._require_admin(prin, "查看未入库的素材")
        root = self.cfg.upload_dir_root
        if not root:
            # 没配落地目录 = 上传功能整体关闭。空列表 + 一句说明，而不是报错：
            # 这一页在那种部署下本来就该是空的。
            return json_response(
                200,
                {
                    "dir": None,
                    "files": [],
                    "note": "服务端没配 PHOTOAR_UPLOAD_DIR，上传功能是关闭的。",
                },
            )
        base = self.roots.resolve(str(root))
        out = []
        if base.is_dir():
            for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
                if not child.is_file():
                    continue
                kind = fsbrowser.kind_of(child)
                if kind is None:
                    # 既不是图也不是视频（`.upload-xxx` 临时文件、`.DS_Store` 之类）。
                    # 列出来只是噪声 —— 用户对它们无事可做。
                    continue
                if self.catalog.get_asset_by_path(str(child)) is not None:
                    continue  # 已经用起来了，在照片列表里看得到
                st = child.stat()
                out.append(
                    {
                        "path": str(child),
                        "name": child.name,
                        "kind": kind,
                        "bytes": int(st.st_size),
                        "mtime": int(st.st_mtime * 1000),
                    }
                )
        return json_response(
            200,
            {"dir": str(base), "files": out, "note": None},
            **{"Cache-Control": "no-store"},
        )

    def _upload_check(self, req: Request, prin: Principal) -> Response:
        """**上传之前**问一次：这个文件是不是已经在服务端了。

        请求体 `{name, sha256, bytes}`，都由客户端在本地算好。

        存在的理由很直接：原来要等 20 MB 传完才知道「已存在」，而手机上那是几十秒的
        等待换来一句「白等了」。哈希是客户端算的，一次请求几百字节。

        两条独立的判断，**都要报**，因为它们的下一步动作不同：

        - **按内容**（sha256）：这份内容在库里已经有了 → 直接告诉他那是哪个文件、
          在库里是什么身份（是某张照片的参考图？被哪些照片当视频用？）。这一条比按
          名字有用得多 —— 相册第二次导出同一张照片，文件名可能变了，内容不会变。
        - **按名字**：落地目录里已经有同名文件 → 内容一样就是「可以复用」，不一样就得
          换个名字（给出建议名）。

        `sha256` 是可选的：不给就只做按名字那一半。留这个余地是因为老版本 App 不会算
        哈希，而「少一半信息」比「整个接口用不了」好。
        """
        self._require_admin(prin, "上传前校验")
        doc = req.json_body()
        name = doc.get("name")
        if not name or not isinstance(name, str):
            raise HttpError(400, "missing_name", "需要 name（目标文件名）")
        safe = _safe_upload_name(name)
        sha = (doc.get("sha256") or "").strip().lower() or None
        if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise HttpError(
                400, "bad_sha256", "sha256 要是 64 位小写十六进制（不给也可以）"
            )

        out: dict[str, Any] = {
            "name": safe,
            # 按名字
            "nameTaken": False,
            "sameContent": False,
            "existingPath": None,
            "suggestedName": None,
            # 按内容
            "knownContent": False,
            "matches": [],
        }

        if self.cfg.upload_dir_root:
            dst = self.roots.resolve(str(Path(self.cfg.upload_dir_root) / safe))
            if dst.is_file():
                out["nameTaken"] = True
                out["existingPath"] = str(dst)
                if sha is not None:
                    out["sameContent"] = sha256_file(dst) == sha
                if not out["sameContent"]:
                    out["suggestedName"] = _suggest_name(safe)

        if sha is not None:
            for asset in self.catalog.get_assets_by_sha256(sha):
                out["matches"].append(self._identity_of_asset(asset))
            out["knownContent"] = bool(out["matches"])
        return json_response(200, out, **{"Cache-Control": "no-store"})

    def _identity_of_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        """一个 asset 在库里的身份。给 `lookup` 与 `upload/check` 共用。

        提出来是因为两处必须一致：用户在上传前看到「这是某张照片的参考图」，传完之后
        又在别处看到不一样的说法，那比不说更糟。

        `photo` 最多一个、`usedByPhotos` 是列表 —— 这个不对称就是「一张照片只能配一段
        视频，但一段视频可以被多张照片配」在数据上的样子。
        """
        asset_id = str(asset["id"])
        as_ref = self.catalog.get_photo_by_ref_asset(asset_id)
        photo = None
        if as_ref is not None:
            pid = str(as_ref["id"])
            video = (
                self.catalog.get_asset(str(as_ref["video_asset_id"]))
                if as_ref["video_asset_id"]
                else None
            )
            photo = {
                "photoId": pid,
                "title": as_ref["title"],
                "refThumbUrl": f"/v1/photo/{pid}/thumb",
                "videoPath": (video or {}).get("nas_path"),
                "qualityScore": int(as_ref["quality_score"]),
                "createdAt": int(as_ref["created_at"]),
            }
        used = []
        for p in self.catalog.photos_referencing_asset(asset_id):
            # 排掉「它是自己的参考图」那条 —— `photos_referencing_asset` 查的是三列
            # （ref / video / playable），不排的话一张照片会出现在自己的
            # usedByPhotos 里。
            if str(p["ref_asset_id"]) == asset_id:
                continue
            used.append(
                {
                    "photoId": str(p["id"]),
                    "title": p["title"],
                    "refThumbUrl": f"/v1/photo/{p['id']}/thumb",
                }
            )
        return {
            "assetId": asset_id,
            "path": asset.get("nas_path"),
            "kind": asset.get("kind"),
            "bytes": asset.get("bytes"),
            "missing": bool(asset.get("missing")),
            "photo": photo,
            "usedByPhotos": used,
        }

    def _admin_lookup(self, req: Request, prin: Principal) -> Response:
        """这个 NAS 路径在库里是什么身份：某张照片的参考图？某些照片的视频？还是没人用。

        存在的理由是**重复上传不该是死胡同**。从手机相册第二次挑同一张照片，服务端会
        （正确地）拦下来，但那时用户要的不是一句「已存在」—— 是「那张照片现在配的是哪段
        视频」，好接着决定要不要换。这个接口就回答那个问题。

        两种身份的**基数不一样**，而这正是界面上能给出什么动作的依据：

        - `photo`：这个文件是**某一张**照片的参考图。一张照片只能有一个参考图，所以这里
          最多一条。
        - `usedByPhotos`：这个文件是**这些**照片配的视频。一段视频可以被多张照片用
          （一段迎宾视频配给几十张是正常用法），所以这里是个列表。

        反过来说：拿一段已经在用的视频去配一张新照片是**完全正常**的，不该报任何错；而拿
        一张已经入库的照片再入一次库必然冲突，只能去改那张已有的。
        """
        self._require_admin(prin, "查询文件在库里的身份")
        raw = req.q1("path")
        if not raw:
            raise HttpError(400, "missing_path", "需要 path 参数")
        path = self.roots.resolve(raw)

        asset = self.catalog.get_asset_by_path(str(path))
        out: dict[str, Any] = {
            "path": str(path),
            "exists": path.is_file(),
            "kind": fsbrowser.kind_of(path),
            "assetId": None,
            "photo": None,
            "usedByPhotos": [],
        }
        if asset is None:
            # 文件可能在磁盘上但从没入过库 —— 那是「可以拿它入库」的状态，不是错误。
            return json_response(200, out)

        # 身份那部分与 `upload/check` 共用一个函数：两处说法不一致（上传前看到一种、
        # 传完在别处看到另一种）比不说更糟。
        identity = self._identity_of_asset(asset)
        out["assetId"] = identity["assetId"]
        out["photo"] = identity["photo"]
        out["usedByPhotos"] = identity["usedByPhotos"]
        return json_response(200, out)

    def _admin_list_videos(self, req: Request, prin: Principal) -> Response:
        """视频侧的反查：库里在用的视频，以及每段视频被哪些照片引用。

        这是「双向映射」里**反**的那个方向。正向（照片 → 视频）一直都有
        （`GET /v1/photo/<id>` 里的 videoPath），反向以前没有 —— 想知道「这段视频
        配给了哪几张照片」只能把全部照片拉下来自己聚合，而那正是管理台想问的问题
        （一段迎宾视频往往配给很多张照片，改它之前要知道会影响谁）。

        `playable` 与 `source` 分开列：转码过的照片有两个 asset，而人关心的是自己
        当初挑的那个源文件（`source`），`playable` 只是实现细节。混在一起会让管理台
        上出现一堆 `xxx_h264.mp4` 这种自己没见过的文件名。
        """
        self._require_admin(prin, "查看视频映射")
        # asset_id → (asset 行, 引用它的照片)
        by_asset: dict[str, dict[str, Any]] = {}
        for p in self.catalog.list_photos():
            vid = p["video_asset_id"]
            if not vid:
                continue
            vid = str(vid)
            entry = by_asset.get(vid)
            if entry is None:
                asset = self.catalog.get_asset(vid) or {}
                entry = {
                    "videoAssetId": vid,
                    "path": asset.get("nas_path"),
                    "bytes": asset.get("bytes"),
                    "durationMs": asset.get("duration_ms"),
                    "missing": bool(asset.get("missing")),
                    "photos": [],
                }
                by_asset[vid] = entry
            entry["photos"].append(
                {
                    "photoId": str(p["id"]),
                    "title": p["title"],
                    "refThumbUrl": f"/v1/photo/{p['id']}/thumb",
                    # 这张照片实际播的是转码产物还是源文件
                    "transcoded": str(p["playable_asset_id"] or "") != vid,
                }
            )
        videos = sorted(
            by_asset.values(), key=lambda v: (str(v["path"] or "")).casefold()
        )
        # 没配视频的照片单独给一份 —— 「哪些照片还没配」是这一页的另一半工作，
        # 而让浏览器拿全量照片自己减一遍等于把同一个聚合写两遍。
        unmapped = [
            {
                "photoId": str(p["id"]),
                "title": p["title"],
                "refThumbUrl": f"/v1/photo/{p['id']}/thumb",
            }
            for p in self.catalog.list_photos()
            if not p["video_asset_id"]
        ]
        return json_response(
            200,
            {"videos": videos, "unmapped": unmapped, "total": len(videos)},
        )

    def _photo_detach_video(
        self, req: Request, prin: Principal, photo_id: str
    ) -> Response:
        """解除照片与视频的关联。

        检查顺序与 `_photo_attach_video` 一致（先 photo 级授权再 admin），理由同那边。

        **只清 photo 表上那两列**，asset 行与磁盘上的转码产物都留着。这不是偷懒：
        同一段视频可能配给了别的照片（见 `_admin_list_videos`），顺手删掉会让那些
        照片的播放变成 404；而「没有任何照片引用的转码产物」是垃圾回收的活，不该由
        一次解除关联来顺带做 —— 那会让这个接口的失败模式变成「解除关联时删了别人的
        视频」。

        幂等：本来就没配视频时返回 200 而不是 404。调用方要的结果（这张照片没有视频）
        已经成立，回 404 只会让管理台弹一个没有意义的错。
        """
        self._photo_or_404(photo_id, prin)
        self._require_admin(prin, "解除照片的视频关联")
        self.catalog.set_photo_video(
            photo_id, video_asset_id=None, playable_asset_id=None
        )
        return json_response(200, {"photoId": photo_id, "hasVideo": False})

    def _photo_delete(self, req: Request, prin: Principal, photo_id: str) -> Response:
        """把一张照片从库里删掉。

        ## 为什么这个接口必须存在

        库里进了两张同一内容的照片时，比值检验（`verify.RATIO`）会把**两张都**判成
        ambiguous —— 于是两张都永久扫不出来。这不是假设：一次真实排查里 941 帧真机
        记录只命中 44 帧，内点数 160~229（门槛 40），挡住它们的就是这一条。

        去重闸门现在会拦住新的（`library.conflicts` 的 `query_features`），但**已经
        进去的那一对拦不住**，而在有这个接口之前解开它的唯一办法是重建整个库。

        ## 顺序：先退役库、再删 catalog 行

        反过来的话，中间那一瞬 catalog 里没有这张、库里还有 —— 此时一次识别命中它，
        `_decide_and_respond` 会走到 `photo is None` 那条 orphan 分支，回未命中。
        那是**已经处理过**的状态（不崩、不错播）。而先删 catalog 的反序里，如果退役
        那一步失败了，库里会永久留一张查不到元数据的照片，它继续参与比值检验、继续
        把别人挤成 ambiguous —— 也就是这个接口本来要解决的那个问题。

        `retire` 与 `delete_photo` 都是幂等的，所以重复点删除不会 500。
        """
        self._photo_or_404(photo_id, prin)
        self._require_admin(prin, "删除照片")
        slot = self.library.retire(photo_id)
        self.catalog.delete_photo(photo_id)
        # 整库 imgdb 不用显式作废：它的版本号是从**条目内容**算出来的
        # （`targets._version_of`），少一张照片 → 版本变 → 端上下次同步会发现自己
        # 那份过期。这里如果自己去删缓存文件，反而会把正在下载那一份的手机打断。
        return json_response(200, {"photoId": photo_id, "deleted": True, "slot": slot})

    def _mapping_snapshot(self) -> list[dict[str, Any]]:
        """照片 ↔ 视频的现状，给映射页和映射导出共用。

        提出来是因为这两处必须一致：管理台上看到的和导出的表如果对不上，人会以为
        导出坏了。
        """
        grants_count: dict[str, int] = {}
        for u in self.catalog.list_users():
            for pid in self.catalog.granted_photo_ids(str(u["id"])):
                grants_count[pid] = grants_count.get(pid, 0) + 1
        out = []
        for p in self.catalog.list_photos():
            pid = str(p["id"])
            ref = self.catalog.get_asset(str(p["ref_asset_id"])) or {}
            video = (
                self.catalog.get_asset(str(p["video_asset_id"]))
                if p["video_asset_id"]
                else None
            )
            out.append(
                {
                    "photoId": pid,
                    "title": p["title"],
                    "refPath": ref.get("nas_path"),
                    "refThumbUrl": f"/v1/photo/{pid}/thumb",
                    "refMissing": bool(ref.get("missing")),
                    "videoAssetId": str(p["video_asset_id"] or "") or None,
                    "videoPath": (video or {}).get("nas_path"),
                    "videoMissing": bool((video or {}).get("missing")),
                    "printWidthM": float(p["print_width_m"]),
                    "qualityScore": int(p["quality_score"]),
                    "grantCount": grants_count.get(pid, 0),
                    # 下面这三项是给管理台的「照片」页用的。那一页把「库里有什么」和
                    # 「各自配了哪段视频」合成了一张表 —— 它们本来就是同一份数据的
                    # 两种看法，分成两个页签的结果是同一行信息要在两处各显示一半。
                    # 合并之后这个接口是那一页的**唯一**数据源，所以要自带这几项，
                    # 否则前端得再拉一次 `/v1/photos` 按 photoId 拼起来。
                    "fitMode": self._fit_mode_of(p),
                    "refStale": bool(p["ref_stale"]),
                    "createdAt": int(p["created_at"]),
                }
            )
        return out

    def _admin_mapping(self, req: Request, prin: Principal) -> Response:
        """照片侧的映射现状（正方向），一行一张照片。"""
        self._require_admin(prin, "查看照片映射")
        rows = self._mapping_snapshot()
        return json_response(200, {"photos": rows, "total": len(rows)})

    # 导出的种类 → (sheet 名, ASCII 文件名, 中文文件名)。
    #
    # 为什么文件名要**两份**：HTTP 头只能是 latin-1（`http.server.send_header` 就是
    # 拿 latin-1 硬编码的），而 `Content-Disposition` 要给两种写法 ——
    # `filename=` 给不认 RFC 5987 的老浏览器，`filename*=UTF-8''…` 给认的。
    # 前者必须是纯 ASCII，后者是百分号编码。
    #
    # 第一版这里只有中文那一份，两处都用它。测试全绿，但真实请求打过来时服务端
    # 线程直接 `UnicodeEncodeError` 崩掉 —— 因为测试是直接调 `Server.handle()` 的，
    # 从不把响应头真的编码出去。`tests/server/conftest.py` 的 `Env.request` 现在会
    # 逐个头做 latin-1 编码检查，就是为了让这一类 bug 不能再躲过测试。
    _EXPORTS = {
        "template": ("模板", "photoar-template", "photoar-模板"),
        "users": ("用户", "photoar-users", "photoar-用户"),
        "mapping": ("映射", "photoar-mapping", "photoar-映射"),
    }

    def _admin_export(self, req: Request, prin: Principal, what: str) -> Response:
        """导出模板 / 用户 / 映射，`?format=xlsx|csv`（默认 xlsx）。

        三种都走同一个出口，是因为它们的差别只有「哪些行」——表头、编码、
        Content-Disposition 这些容易写歪的部分只该有一份。
        """
        self._require_admin(prin, "导出表格")
        spec = self._EXPORTS.get(what)
        if spec is None:
            raise HttpError(
                404,
                "unknown_export",
                f"没有 {what!r} 这种导出。可选：{sorted(self._EXPORTS)}",
            )
        sheet_name, ascii_stem, stem = spec
        fmt = (req.q1("format") or "xlsx").lower()
        if fmt not in ("xlsx", "csv"):
            raise HttpError(400, "bad_format", "format 只能是 xlsx 或 csv")

        if what == "template":
            rows = batch.template_rows()
        elif what == "users":
            photos = {p["photoId"]: p for p in self._mapping_snapshot()}
            users = self.catalog.list_users()
            grants = {
                str(u["id"]): self.catalog.granted_photo_ids(str(u["id"]))
                for u in users
            }
            rows = [batch.TEMPLATE_HEADER, *batch.users_rows(users, photos, grants)]
        else:
            rows = [
                batch.MAPPING_HEADER,
                *batch.mapping_rows(self._mapping_snapshot()),
            ]

        if fmt == "csv":
            body = sheet_mod.write_csv_bytes(rows)
            ctype = "text/csv; charset=utf-8"
        else:
            body = sheet_mod.write_xlsx(rows, sheet_name=sheet_name)
            ctype = (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            )
        return Response(
            status=200,
            headers={
                "Content-Type": ctype,
                # 两种写法都给：`filename=` 给不认 RFC 5987 的老浏览器，
                # `filename*=UTF-8''…` 给认的（认的那些会优先用它，于是拿到中文名）。
                #
                # ⚠️ `filename=` 那份**必须是纯 ASCII**。HTTP 头只能是 latin-1，
                # 往里放中文会让 `http.server.send_header` 抛 UnicodeEncodeError，
                # 也就是整个响应线程崩掉 —— 不是「文件名难看」而是「下载不了」。
                "Content-Disposition": (
                    f'attachment; filename="{ascii_stem}.{fmt}"; '
                    f"filename*=UTF-8''{quote(stem)}.{fmt}"
                ),
                # 导出的是当下的库状态，不该被缓存 —— 改完用户再点导出拿到旧表，
                # 而且看不出是缓存。
                "Cache-Control": "no-store",
            },
            body=body,
        )


def open_library_cli(cfg: ServerConfig) -> PhotoLibrary:
    """CLI 子命令（`reindex` / `build-vocab`）打开识别库的唯一入口。

    走的是与 `Server.create` **完全同一条**后端与词表解析逻辑。以前 `cmd_reindex`
    自己写了一行 `PhotoLibrary(cfg.library_dir, Vocab.load(cfg.vocab_path))`，在只有
    ORB 的年代它是对的；现在那一行会做三件错事：写死 ORB 的库目录（于是在 xfeat
    部署上 reindex 了一个空库并报"重建完成：0 张"）、写死 `Vocab`（xfeat 词表 load
    不了）、以及要求词表文件必须存在。三件里没有一件会报出"你重建错了库"。
    """
    cfg.ensure_dirs()
    catalog = Catalog(cfg.db_path)
    config = AppConfig(catalog)
    Server._seed_backend(catalog, config)
    backend, _ = Server._open_backend(cfg, str(config.get("recog.backend")))
    return Server._open_library(cfg, backend)


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
