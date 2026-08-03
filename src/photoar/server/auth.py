"""身份：用户、口令、会话、一次请求的 Principal。

零第三方依赖（与 pyproject 的运行时依赖只有 opencv + numpy 这条约束一致），
所以口令散列用 `hashlib.scrypt`（走 OpenSSL，不是纯 Python），不引 bcrypt /
passlib。

## 两套凭证，为什么并存

1. **会话 token**（本模块的主线）：人用的。viewer 只输名字、admin 输名字+口令，
   拿到一个明文 token，服务端只存它的 sha256。
2. **`legacy_token`**（环境变量 `PHOTOAR_TOKEN`，就是 `ServerConfig.token`）：
   机器用的。批量入库脚本、`docker compose` 里的健康检查、以后可能有的定时
   任务都拿它调接口 —— 这些调用方没有人坐在前面输口令，也不该在库里占一个
   用户行。它换来的 Principal 是 `via='legacy_token'`、`role=admin`、
   `user_id=None`。

   `user_id=None` 是刻意的，不是偷懒：运维凭证不对应任何一个人，硬造一个
   "system"用户行会让"删掉这个用户"变成一个能把入库脚本搞挂的操作，也会让
   `last_seen_at`、逐张授权这些属于"人"的概念套到一把钥匙上。代价是任何按
   user_id 记账/过滤的代码都必须处理 None —— 见 `photo_filter` 里那条断言。

   它也**不能被登出**（没有服务端状态可删）。要作废只有改环境变量重启。所以它
   不该发给家里人，只该躺在 `.env` 里。

## 时间

所有时间从注入的 `now_ms` 拿（默认 `db.now_ms`）。测"过期会话会被拒"必须能把
时钟拨过去，靠 `time.sleep` 等真实 TTL 流过去的测试要么慢到没人跑，要么把 TTL
调到 1 秒去测一个产品里永远不会出现的取值。
"""

import hashlib
import hmac
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Callable

from . import db

VIEWER = "viewer"
ADMIN = "admin"
ROLES = (VIEWER, ADMIN)

# 会话 TTL 的默认值。放在这里而不是 appconfig，是为了让 appconfig 的默认值能
# 从代码里取到同一个数（"默认值永远等于代码里的值"）。
#
# viewer 30 天：家里人拿手机扫墙上的照片，隔几周才用一次，每次都要重新输名字
# 就会变成"这东西太麻烦"。它能看到的最坏情况是几张授权过的照片。
# admin 12 小时：能改热配置、能删用户（不可撤销）、能看全库。12 小时约等于
# "一次维护窗口"，睡一觉回来必须重新输口令。
VIEWER_TTL_DAYS = 30
ADMIN_TTL_HOURS = 12

# 运维凭证换来的 Principal 的显示名。带前后括号是为了让它在任何"最近活跃用户"
# 之类的列表里一眼看出来不是人 —— 而且规范化后的真实用户名不可能长这样
# （normalize_name 不会产生括号，但用户可以自己输括号，所以这只是可读性，
# 不是安全边界；安全边界是 via 字段）。
LEGACY_NAME = "[运维凭证]"

# ---- scrypt 参数 ----
#
# 内存开销 = 128 * n * r 字节 = 128 * 16384 * 8 = 16 MiB（每次调用，调用期间持有）。
#
# 为什么是 2**14 而不是更高：
# - 目标机是家里那台 3GB 内存的 NAS，上面还跑着识别服务（词汇树 + mmap 的描述子
#   库）和 ffmpeg 转码。scrypt 的内存是**并发相乘**的：HTTP 服务是
#   ThreadingHTTPServer，每个登录请求一个线程，8 个人同时点登录就是 8 × 16 MiB
#   = 128 MiB 的瞬时峰值。取 2**17（OWASP 对交互式登录的推荐值，128 MiB/次）时
#   同样 8 个并发就是 1 GiB —— 在一台 3GB 的机器上，那不是"慢一点"，是转码进程
#   被 OOM killer 挑走。
# - 用户规模是家庭（个位数账号），口令由管理员设置而不是用户自选弱口令，攻击面
#   是一条 Cloudflare 隧道后面的私有服务，不是公网注册入口。16 MiB / 约 50ms
#   的代价对"库被拖走后离线爆破"仍是有意义的量级。
# - ⚠️ `hashlib.scrypt` 的 `maxmem` 默认值 0 会落到 OpenSSL 自己的 32 MiB 上限，
#   而 n=2**15 需要约 33.5 MiB —— 也就是说"把 n 翻一倍"这个看起来无害的改动会
#   直接抛 `ValueError: Invalid parameter combination`，且只在真的有人登录时才炸。
#   所以这里把 maxmem 显式写出来，比默认上限留一档余量。
#
# ⚠️ 参数是**硬编码在代码里**的，schema 里没有存 n/r/p 的列（表结构由需求给定）。
# 这意味着改动下面任何一个数字都会让全部已存口令验证失败。真要改：先给 user 表
# 加一列存参数、验证时按行读，或者接受"所有 admin 必须重设口令"。不要以为改个
# 常量就行。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16

# `secrets.token_urlsafe(32)` = 32 字节熵（约 43 个字符）。256 bit 的会话 token
# 不需要更长；短于 16 字节才需要担心在线猜测。
TOKEN_BYTES = 32

# 名字长度上限。没有它的话，一个 10MB 的名字会被原样写进库、并出现在每个
# Principal 里。64 对中文名/昵称绰绰有余。
NAME_MAX_LEN = 64

# `last_used_at` / `last_seen_at` 的最小刷新间隔。
#
# 为什么不是每次请求都刷：`/v1/recognize` 在扫描时是每秒好几次，而 Catalog 的写
# 操作全部串行在一把 `_write_lock` 上，且入库时那把锁可能被 ffmpeg 持有几十秒。
# "每个请求写一次 session"会把一个纯读的识别请求变成一次要排队的写事务 —— 表现
# 为扫描时莫名卡顿，而没有任何一行代码看起来像在写库。
# 60 秒的精度对"最近活跃"这种用途完全够用。
TOUCH_INTERVAL_S = 60


class AuthError(Exception):
    """登录/鉴权失败的基类。HTTP 层可以整类映射到 401。

    `InvalidName` **不**在这一类里：它是"输入不合法"（400），不是"凭证不对"。
    混在一起的后果是管理台建号时把"名字空的"提示成"登录失败"。
    """


class UnknownUser(AuthError):
    """名字不在册。

    刻意**不**自动建号：viewer 登录只输名字，自动建号就等于"任何知道 URL 的人
    输个新名字就能进"，那 viewer 这一层鉴权完全不存在。账号只能由 admin 建。
    """


class BadCredentials(AuthError):
    pass


class AccountDisabled(AuthError):
    pass


class InvalidName(ValueError):
    """名字规范化后为空、或超长。"""


def normalize_name(raw: str) -> str:
    """算出用于**唯一性与查找**的键：NFKC → 压缩空白 → casefold。

    三步各自的理由：
    - NFKC：手机输入法在中文状态下打出的是全角字母/空格。"Ａlice" 与 "Alice"
      在屏幕上几乎一样，让它们成为两个账号只会制造"我明明输对了"。
    - 压缩空白（首尾去掉、内部连续压成一个）：复制粘贴带来的尾随空格是登录
      失败最常见的原因，而它在输入框里完全看不见。
    - casefold 而不是 lower：lower 对德语 "ß" 不做处理，而 casefold 会把它折成
      "ss"。既然目的是"看起来同一个名字就是同一个人"，就该用为此设计的那个函数。

    返回值只用于比较与索引，**不用于显示** —— 显示走 `display_name`。原样输入
    存在 `user.name` 列，规范化值存在 `user.name_key` 列，两列并存的完整取舍
    写在 `db._DDL_V2` 的注释里。

    本函数**不抛异常**（纯规范化）：空字符串规范化后就是空字符串，"空名字是不是
    合法输入"是调用方的问题 —— 登录时它自然查不到人（UnknownUser），建号时由
    `check_name` 拦下。
    """
    return " ".join(unicodedata.normalize("NFKC", raw).split()).casefold()


def display_name(raw: str) -> str:
    """算出用于**显示**的名字：NFKC → 压缩空白，保留大小写。

    空白处理必须与 `normalize_name` 完全一致，否则会出现"显示名里有个尾随空格、
    唯一键里没有"这种只在对齐时才看得出来的脏数据。
    """
    return " ".join(unicodedata.normalize("NFKC", raw).split())


def check_name(raw: str) -> tuple[str, str]:
    """建号/改名的唯一入口：返回 `(显示名, 唯一键)`，不合法就抛 `InvalidName`。

    存在的理由是这两个值必须成对产生。分散在各个调用点的话，迟早有人只算了
    `normalize_name` 就往 `name` 列里写（于是显示名变成小写），或者只算了显示名
    就往 `name_key` 里写（于是唯一性退化成"字符串完全相同"）。
    """
    shown = display_name(raw)
    key = normalize_name(raw)
    if not key:
        raise InvalidName("名字不能为空")
    if len(shown) > NAME_MAX_LEN or len(key) > NAME_MAX_LEN:
        raise InvalidName(f"名字太长（上限 {NAME_MAX_LEN} 个字符）")
    return shown, key


def _derive(password: str, salt: bytes) -> bytes:
    """口令也过一遍 NFKC。

    与名字同理：全角/半角、以及 macOS 上某些输入法产生的分解形式（"é" 可能是
    一个码位也可能是 "e" + 组合音符），肉眼完全无法区分。不规范化的话用户会遇到
    "同一个口令在这台手机上能进、在那台上不能进"，且永远查不出原因。

    只做 NFKC，**不**去空白：口令里的空格是口令的一部分。
    """
    return hashlib.scrypt(
        unicodedata.normalize("NFKC", password).encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> tuple[bytes, bytes]:
    """返回 `(hash, salt)`。每次调用都是新盐。"""
    salt = secrets.token_bytes(SALT_BYTES)
    return _derive(password, salt), salt


def verify_password(password: str, hash_: bytes | None, salt: bytes | None) -> bool:
    """比较用 `hmac.compare_digest`：`==` 会在第一个不同的字节上返回，
    逐字节的耗时差是可测的，理论上能被用来一个字节一个字节地问出散列值。

    `hash_` 或 `salt` 为空时直接 False，**不**回退成"没设口令就放行" —— 那正好是
    "把 admin 的口令列意外清空"变成"任何人都能当 admin"的路径。谁需要"没设口令
    就放行"由 `Auth.login` 按角色显式决定。
    """
    if not hash_ or not salt:
        return False
    return hmac.compare_digest(_derive(password, bytes(salt)), bytes(hash_))


def _sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _same_secret(a: str, b: str) -> bool:
    """定长时间比较两个字符串形式的密钥。

    先 encode 成 bytes 再比：`hmac.compare_digest` 只接受"两个都是 ASCII-only
    的 str"，而 token 是从 Authorization 头里来的，客户端完全可以塞非 ASCII
    进来 —— 那时 compare_digest 会抛 TypeError，表现为一个 500 而不是 401。
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass(frozen=True)
class Principal:
    """一次请求的身份。frozen 是因为它会被传进各层处理函数，任何一层"顺手改一下
    role"都是权限提升。"""

    user_id: str | None
    name: str
    role: str
    grant_all: bool
    via: str  # 'session' | 'legacy_token'

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN


def photo_filter(principal: Principal) -> str | None:
    """把 Principal 翻译成 `Catalog.list_photos(user_id=...)` 要的那个参数。

    "admin 或 grant_all 的人不过滤"这条策略**只写在这里一处**。写在每个路由里的
    话，漏掉一处的后果是那个接口把全库照片给了一个只授权了三张的人，而它看起来
    与其它接口一模一样。
    """
    if principal.is_admin or principal.grant_all:
        return None
    if principal.user_id is None:
        # 只有运维凭证的 user_id 是 None，而它是 admin，上面已经返回了。真的走到
        # 这里说明有人构造了一个"没有 user_id 又不是 admin"的 Principal —— 此时
        # 返回 None（= 不过滤 = 全库可见）是最坏的失败方式，所以宁可 500。
        raise ValueError(f"Principal 缺 user_id 且不是 admin，拒绝推断可见范围：{principal!r}")
    return principal.user_id


class Auth:
    def __init__(
        self,
        catalog: db.Catalog,
        *,
        viewer_ttl_s: int,
        admin_ttl_s: int,
        legacy_token: str = "",
        now_ms: Callable[[], int] = db.now_ms,
        touch_interval_s: int = TOUCH_INTERVAL_S,
    ) -> None:
        self._cat = catalog
        self._viewer_ttl_ms = int(viewer_ttl_s) * 1000
        self._admin_ttl_ms = int(admin_ttl_s) * 1000
        self._legacy_token = str(legacy_token or "")
        self._now = now_ms
        self._touch_ms = int(touch_interval_s) * 1000

    # ---- 登录 ----

    def login(self, name: str, password: str | None) -> tuple[str, Principal]:
        """返回 `(明文 token, Principal)`。明文 token 只在这里出现一次。

        检查顺序是"存在 → 停用 → 口令"，也就是说"这个名字不在册"和"这个账号被
        停用了"都会在验口令之前泄露出去。这是有意的：viewer 登录只需要名字，本
        系统在设计上就没打算隐藏"谁在册"这件事，为此把 disabled 的判断挪到口令
        之后只会换来"停用的 admin 输错口令得到的提示不一样"这种更难解释的行为。
        """
        key = normalize_name(name)
        row = self._cat.get_user_by_name_key(key) if key else None
        if row is None:
            raise UnknownUser(f"没有这个用户：{display_name(name)!r}")
        if int(row["disabled"] or 0):
            raise AccountDisabled(f"账号已停用：{row['name']!r}")

        role = str(row["role"])
        if role == ADMIN:
            # admin 必须带口令，**且**库里必须有散列。
            #
            # 少了后半句的话，一个 pwd_hash 意外为 NULL 的 admin 行会让
            # verify_password 返回 False -> BadCredentials，看起来是对的；但如果
            # 有人为了"viewer 只要名字"的对称性顺手写成"没散列就放行"，就会得到
            # 一个只要知道管理员名字就能登进去的后门。所以两条都显式写出来。
            if not password:
                raise BadCredentials("管理员登录必须输口令")
            if not verify_password(password, row["pwd_hash"], row["pwd_salt"]):
                raise BadCredentials("口令不对")
        elif role == VIEWER:
            if row["pwd_hash"] is not None:
                # viewer 也允许设口令（schema 没禁止）。设了就必须验 —— "这一列
                # 有值但从来不检查"是最糟的一种状态：管理台显示"已设置口令"，
                # 实际上谁都能进。
                if not password or not verify_password(
                    password, row["pwd_hash"], row["pwd_salt"]
                ):
                    raise BadCredentials("口令不对")
        else:
            # 角色只能是 ROLES 里的两个。库里出现别的值只能是有人手工改过 DB 或
            # 未来版本降级留下的，此时"当 viewer 处理"是在猜，而猜错的方向是放行。
            raise BadCredentials(f"用户 {row['name']!r} 的角色未知：{role!r}")

        now = self._now()
        ttl = self._admin_ttl_ms if role == ADMIN else self._viewer_ttl_ms
        token = secrets.token_urlsafe(TOKEN_BYTES)
        uid = str(row["id"])
        self._cat.create_session(
            token_sha256=_sha256(token),
            user_id=uid,
            created_at=now,
            expires_at=now + ttl,
        )
        self._cat.touch_user_seen(uid, now)
        return token, Principal(
            user_id=uid,
            name=str(row["name"]),
            role=role,
            grant_all=bool(row["grant_all"]),
            via="session",
        )

    def logout(self, token: str) -> None:
        """幂等。运维凭证登出是**空操作** —— 它没有服务端状态可删，要作废只能改
        环境变量重启。这里刻意不抛异常：让健康检查脚本误调一次 logout 就 500，
        比静默无事发生糟得多。"""
        if not token:
            return
        if self._legacy_token and _same_secret(token, self._legacy_token):
            return
        self._cat.delete_session(_sha256(token))

    # ---- 鉴权 ----

    def principal_of(self, token: str) -> Principal | None:
        """token 无效/过期/属于已停用账号 → None。

        先比运维凭证再查 session：两者的取值空间不可能相撞（session token 是
        `token_urlsafe(32)`），顺序不影响正确性，但运维凭证是入库脚本每次调用都
        要走的路径，让它不必先查一次库更省事。
        """
        if not token:
            return None
        if self._legacy_token and _same_secret(token, self._legacy_token):
            return Principal(
                user_id=None,
                name=LEGACY_NAME,
                role=ADMIN,
                # grant_all 也给 True。它本来就因为 role=admin 而看得到全部
                # （photo_filter 第一个分支），这里写 True 只是为了让这个
                # Principal 自己是自洽的：任何直接读 grant_all 而没走
                # photo_filter 的代码不会得到一个矛盾的答案。
                grant_all=True,
                via="legacy_token",
            )

        row = self._cat.get_session(_sha256(token))
        if row is None:
            return None
        now = self._now()
        if int(row["expires_at"]) <= now:
            # 顺手删掉：过期会话没有任何用途，留着只会让 session 表随时间单调增长
            # （家里人每换一台设备就多一行）。purge_expired 是定期的兜底，这里是
            # "碰到就清"，两者都要有 —— 只有定期清理的话，一个几个月不重启的服务
            # 会攒下几千行死数据；只有碰到才清的话，永不再来的旧 token 永远不清。
            self._cat.delete_session(bytes(row["token_sha256"]))
            return None
        if int(row["u_disabled"] or 0):
            # 不删这一行：停用是可撤销的，撤销后手机上那个 token 还能继续用。
            # （要立刻踢掉全部设备就调 `Catalog.delete_sessions_of_user`，那是
            # "停用"这个动作本身该做的事，不是这里该顺手做的。）
            return None

        if now - int(row["last_used_at"]) >= self._touch_ms:
            self._cat.touch_session(bytes(row["token_sha256"]), now)
            self._cat.touch_user_seen(str(row["user_id"]), now)

        return Principal(
            user_id=str(row["user_id"]),
            name=str(row["u_name"]),
            role=str(row["u_role"]),
            grant_all=bool(row["u_grant_all"]),
            via="session",
        )

    # ---- 会话时长（给 HTTP 层签 cookie 用）----

    def ttl_s(self, role: str) -> int:
        """这个角色的会话有效期（秒）。

        暴露出来而不是让 HTTP 层自己按 role 去挑 `VIEWER_TTL_DAYS` /
        `ADMIN_TTL_HOURS`：真正生效的 TTL 是构造时注入的（启动时从热配置的
        `session.*` 读），那两个常量只是它的默认值。HTTP 层照常量算 cookie 的
        Max-Age 的话，改过 `session.viewer_days` 的部署会得到一个比服务端会话先
        过期的 cookie —— 表现是"用着好好的忽然要重新登录"，而库里那一行 session
        还活得好好的，从哪儿都查不出原因。
        """
        ms = self._admin_ttl_ms if role == ADMIN else self._viewer_ttl_ms
        return int(ms // 1000)

    def expires_at_of(self, token: str) -> int | None:
        """这个 token 的过期时刻（毫秒）。运维凭证与无效 token 都返回 None。

        登录响应里的 `expiresAt` 必须是**库里那个值**，而不是在 HTTP 层用
        "现在 + ttl" 重算一遍：重算出来的数会和库里差几毫秒（两次取时钟之间还
        隔着一次 scrypt 和一次 INSERT），而"两个本该相等的数字不相等"会在排查
        任何会话问题时先把人绕进去一次。多一次 SELECT 的代价只落在登录上，
        登录不是热路径。

        运维凭证返回 None 是如实的：它没有服务端状态，也就没有过期时刻
        （要作废只能改环境变量重启）。
        """
        if not token:
            return None
        if self._legacy_token and _same_secret(token, self._legacy_token):
            return None
        row = self._cat.get_session(_sha256(token))
        return int(row["expires_at"]) if row is not None else None

    # ---- 运维 ----

    def ensure_bootstrap_admin(self, name: str, password: str) -> str | None:
        """没有任何 admin 时建一个，返回新 id；已经有了就什么都不做并返回 None。

        判据是"存在任何 admin 行"，**包括被停用的那些**。否则"停用唯一的管理员"
        会让下一次启动用环境变量里的口令悄悄建出第二个管理员 —— 一个既没人操作
        过、又拥有全部权限的账号。

        已存在同名（规范化后）的非 admin 用户时抛 `db.NameTaken` 而不是把那个人
        提成 admin：把一个 viewer 静默提权，是环境变量能做到的最危险的事。

        `grant_all` 给 0 而不是 1：admin 看得到全库是因为 `role == admin`
        （见 photo_filter），不是因为这一列。写 1 的话，以后把这个人降级成
        viewer 时会静默保留"能看全部照片"。
        """
        if self._cat.count_users(ADMIN) > 0:
            return None
        if not password:
            raise BadCredentials("引导管理员必须给口令，否则建出来的是一个谁都能登的 admin")
        shown, key = check_name(name)
        pwd_hash, pwd_salt = hash_password(password)
        return self._cat.create_user(
            name=shown,
            name_key=key,
            role=ADMIN,
            pwd_hash=pwd_hash,
            pwd_salt=pwd_salt,
            grant_all=False,
            now=self._now(),
        )

    def purge_expired(self) -> int:
        """删掉全部过期会话，返回删了几行。启动时与定期各调一次。"""
        return self._cat.purge_expired_sessions(self._now())

    # ---- 给上层建号用的小工具 ----

    def create_user(
        self,
        *,
        name: str,
        role: str,
        password: str | None = None,
        grant_all: bool = False,
    ) -> str:
        """建号。把"名字规范化 + 口令散列 + 角色校验"收在一处。

        HTTP 层直接调 `Catalog.create_user` 也能建出用户，但那样它得自己记住
        `name`/`name_key` 要成对算、admin 必须有口令 —— 而这些规则一旦有第二个
        实现，就一定会有一个是错的。
        """
        if role not in ROLES:
            raise InvalidName(f"角色只能是 {ROLES}，收到 {role!r}")
        if role == ADMIN and not password:
            raise BadCredentials("管理员必须设口令")
        shown, key = check_name(name)
        pwd_hash: bytes | None = None
        pwd_salt: bytes | None = None
        if password:
            pwd_hash, pwd_salt = hash_password(password)
        return self._cat.create_user(
            name=shown,
            name_key=key,
            role=role,
            pwd_hash=pwd_hash,
            pwd_salt=pwd_salt,
            grant_all=grant_all,
            now=self._now(),
        )

    def set_password(self, user_id: str, password: str | None) -> None:
        """改/清口令，并把该用户已有的会话全部踢掉。

        踢会话是这个动作的一半语义：改口令的常见动机是"我怀疑口令泄露了"，而只
        换散列的话，泄露方手上那个还没过期的 session token 照样能用。
        """
        if password:
            pwd_hash, pwd_salt = hash_password(password)
        else:
            pwd_hash, pwd_salt = None, None
        self._cat.set_user_password(user_id, pwd_hash, pwd_salt)
        self._cat.delete_sessions_of_user(user_id)
