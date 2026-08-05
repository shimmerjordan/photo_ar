#!/usr/bin/env python3
"""容器入口：建目录 → 需要时取模型 → 打印一次生效配置摘要 → 起服务。

## 一个容器里为什么有两个进程

2026-08-05 把网页版（Node）与后端（Python）合成了**一个镜像、一个端口**。之前是两个
容器，端口 8964 与 48082 各一个，宾客要访问的地址和管理台的地址不同源 —— 于是
`/admin` 那个按钮点开是 404，而反代又要在外面再配一层。

合了之后端口只有一个，按 URI 分：

    /            网页版（宾客扫照片的那个页面）        ← Node 直接发静态
    /api/*       网页版自己的端点（识别库 / 票据 / 流） ← Node
    /admin       网页管理台                            ← Node 反代 Python
    /v1/*        后端 API                              ← Node 反代 Python

所以 Node 必须在前面（它是唯一的监听者），Python 退到 `127.0.0.1:<内部端口>`。
两个进程，谁都不能当 PID 1，于是这个脚本从"exec 一下就消失"变成了一个真正的
**进程管理器**：转发信号、任一子进程退出就把另一个收干净、给孤儿收尸。

不用 s6/supervisord 是因为它们要么加一个基础镜像依赖、要么加一份配置文件语法，
而这里要管的进程只有两个、规则只有一条（"谁死了都一起死，让 docker 去重启"）。

`PHOTOAR_WEB=0` 退回旧形态：只起 Python，直接监听公开端口。留着它是因为这是一条
**出事时的退路** —— 网页那一半有问题时，改一个环境变量就能回到"只有 API 和管理台"
的已知状态，不用回滚镜像。

## 为什么是 Python 而不是 shell

三个具体理由，不是偏好：

1. **摘要必须是"真正会生效的那份配置"。** 这个脚本直接 `ServerConfig.from_env()`
   然后把那个对象打印出来，所以摘要与服务读到的是**同一个解析结果**。shell 版只能
   把环境变量再读一遍、按自己的理解拼一遍 —— 而那份"理解"会与 `config.py` 慢慢分叉
   （比如 `PHOTOAR_ROOTS` 的两种写法、`_env_flag` 对空串的处理），于是摘要显示的和
   实际跑的不是一回事，而摘要恰恰是用来排查配置问题的。
2. 配置不对时它能在**服务启动之前**给出那句中文说明并退出（`ConfigError`），而不是
   让服务自己起到一半失败。
3. slim 镜像里的 `/bin/sh` 是 dash：没有 `pipefail`，`[[ ]]` 也没有。写得对的
   shell 脚本在这里能跑，但"看起来对"的那种（用了 bashism）会静默行为不同。

代价是启动时多 import 一次 cv2/numpy（约 0.3 秒）。可以接受 —— 它每次容器启动只
付一次，而 `exec` 之后这个进程就不存在了。

## 取模型失败为什么不阻断启动

模型是运行时资产，取它要外网。NAS 重启的时候外网可能正好不通。让服务因此起不来是
把一个"XFeat 用不了"的降级放大成"整个服务不可用"——而 ORB 才是通过出口条件的基线，
它一个字节的模型都不需要。所以这里只打日志，`Server._open_backend` 那边会回退 ORB
并把降级状态挂在 `/v1/ping` 上。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FETCH = REPO / "tools" / "fetch_models.py"
WEB_ENTRY = REPO / "web-front" / "server" / "index.js"

# 算好的端口写在这里给健康检查读。**与 docker/healthcheck.py 里那个常量必须一致。**
RUNTIME_FILE = Path("/tmp/photoar-runtime.json")

# Python 后端在合并形态下监听的**内部**端口。它不经 `ports:` 暴露，容器外打不到。
#
# 挑 8965 而不是随便一个高端口：与公开的 8964 只差 1，日志里两个端口并排出现时
# 一眼看得出哪个是哪个。真撞了（比如有人把公开端口也设成 8965）用
# PHOTOAR_INTERNAL_PORT 换。
DEFAULT_INTERNAL_PORT = 8965

# 收到 SIGTERM 之后留给子进程收尾的时间。超了就 SIGKILL。
#
# 10 秒是照着两边各自的收尾逻辑定的：Node 那边 `server.close()` 之后自己也有一个
# 10 秒的兜底定时器（见 web-front/server/index.js 末尾），Python 那边是
# `httpd.shutdown()`。docker stop 的默认宽限期也是 10 秒，所以再长也没用 ——
# 到时候是 dockerd 直接 SIGKILL 整个容器，比我们自己 kill 更粗暴。
STOP_GRACE_S = 10.0

# 这些值一个字都不能进日志。
#
# 写成一个显式名单而不是"只打印我想打印的字段"：摘要是按字段逐条写的，将来有人往
# ServerConfig 上加一个 `smtp_password` 并顺手加进摘要时，这个名单不会自动救他 ——
# 所以这里同时保留"逐条挑字段"这一层（下面 `_summary` 里没有任何 `for k, v in
# vars(cfg)` 的循环）。两层都要，因为泄露一次就是永久泄露（日志会被收集、会被贴到
# issue 里）。
_SECRET_ENVS = ("PHOTOAR_TOKEN", "PHOTOAR_ADMIN_PASSWORD")


def _flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _mask(name: str) -> str:
    """只说"设了没设"，绝不回显长度或前缀。

    连长度都不给：一个"12 个字符"的提示对排查几乎没用，但对拿到日志的人是实打实的
    信息。而"设了/没设"恰好就是排查这两个变量时唯一需要知道的事（"我明明填了 token
    怎么还是 401" → 看这一行是不是"未设置"）。
    """
    return "已设置" if (os.environ.get(name) or "").strip() else "未设置"


def _fetch_model_if_needed() -> None:
    """需要时取 XFeat 模型。**不看 recog.backend**，只看要不要取。

    刻意不去读库里的 `recog.backend` 再决定取不取：那会让"在管理台把后端切成 xfeat →
    重启"这个最自然的操作序列在**第一次**重启时失败（重启那一刻库里已经是 xfeat 了，
    但模型还没取过，而这次启动才是第一次有机会取）。反过来，无条件取的代价只是一次
    4MB 的下载，而它是幂等的（已存在就跳过）。

    真的不想要就设 `PHOTOAR_FETCH_MODELS=0`（离线部署、或者模型是手工放进卷里的）。
    """
    if not _flag("PHOTOAR_FETCH_MODELS", True):
        print("[entrypoint] PHOTOAR_FETCH_MODELS=0，跳过取模型", flush=True)
        return
    models = os.environ.get("PHOTOAR_MODELS") or (
        str(Path(os.environ.get("PHOTOAR_DATA", "/data")) / "models")
    )
    if not FETCH.is_file():
        # 镜像里没有 tools/fetch_models.py（构建上下文不对）。不该发生，但如果发生了
        # 要说清楚，而不是让 subprocess 抛一个 FileNotFoundError。
        print(f"[entrypoint] ⚠️ 找不到 {FETCH}，跳过取模型", flush=True)
        return
    cmd = [sys.executable, str(FETCH), "--out", models]
    url = os.environ.get("PHOTOAR_MODEL_URL")
    if url and url.strip():
        cmd += ["--url", url.strip()]
    print(f"[entrypoint] 取 XFeat 模型 → {models}", flush=True)
    # check=False：失败不阻断启动（理由见模块 docstring 最后一节）。
    # fetch_models.py 自己已经把可执行的建议打到 stderr 上了，这里不再包一层。
    rc = subprocess.run(cmd, check=False).returncode
    if rc != 0:
        print(
            "[entrypoint] ⚠️ 模型没取到（上面有原因和出路）。服务照常启动，"
            "识别后端会回退到 orb。",
            flush=True,
        )


def _summary(cfg, layout: "Layout") -> str:
    """一次生效配置摘要。逐条挑字段，**不遍历 dataclass**（理由见 _SECRET_ENVS）。"""
    lines = [
        "[entrypoint] ---- 生效配置 ----",
        f"[entrypoint]   对外监听    {layout.public_desc}",
        f"[entrypoint]   后端监听    {layout.backend_desc}",
        f"[entrypoint]   数据目录    {cfg.data_dir}",
        f"[entrypoint]   模型/词表   {cfg.model_dir}",
        f"[entrypoint]   白名单根    "
        + ("、".join(f"{k}={v}" for k, v in cfg.roots.items()) or "（空）"),
        f"[entrypoint]   上传落地    {cfg.upload_dir_root or '（关闭）'}",
        f"[entrypoint]   识别后端    "
        f"{os.environ.get('PHOTOAR_BACKEND') or '（不指定，用库里 recog.backend 的值）'}",
        f"[entrypoint]   编码器      {cfg.video_encoder}（preset {cfg.video_preset}，"
        f"vaapi {cfg.vaapi_device}）",
        f"[entrypoint]   arcoreimg   {cfg.arcoreimg}",
        f"[entrypoint]   cookie 安全 {'Secure 开' if cfg.cookie_secure else 'Secure 关（http 直连也能登录）'}",
        f"[entrypoint]   引导管理员  {cfg.admin_name}（口令：{_mask('PHOTOAR_ADMIN_PASSWORD')}）",
        f"[entrypoint]   运维凭证    {_mask('PHOTOAR_TOKEN')}",
        "[entrypoint] ------------------",
    ]
    return "\n".join(lines)


class Layout:
    """端口怎么分。**一个地方算完，日志、子进程环境、健康检查读同一份结果。**

    以前只有一个端口，这件事不值得一个类。合并之后有三个来源要对齐（公开端口、
    Python 的内部端口、Node 拿到的 upstream 地址），而它们错开一位的表现是
    "页面能开、`/v1` 全 502" —— 一个看起来像后端挂了、其实是端口算错了的故障。
    """

    def __init__(self, cfg) -> None:
        # 公开端口取 `cfg.port` 而不是自己再读一遍 PHOTOAR_PORT：`cfg` 已经把
        # "环境变量 > config.json > 默认值"这条优先级解完了（config.py 里那一行），
        # 在这里重解一遍就是第二份实现 —— 而挂了 config.json 的部署会因此对不上。
        self.public_port = int(cfg.port)
        self.web = _flag("PHOTOAR_WEB", True) and WEB_ENTRY.is_file()
        if self.web:
            self.backend_port = int(
                (os.environ.get("PHOTOAR_INTERNAL_PORT") or "").strip()
                or DEFAULT_INTERNAL_PORT
            )
            if self.backend_port == self.public_port:
                raise SystemExit(
                    f"[entrypoint] 内部端口与对外端口都是 {self.public_port}，"
                    f"两个进程抢同一个端口谁也起不来。改 PHOTOAR_INTERNAL_PORT。"
                )
            self.backend_bind = "127.0.0.1"
        else:
            # 没有网页那一半时，Python 自己就是对外的那个监听者 —— 回到合并之前的形态。
            self.backend_port = self.public_port
            self.backend_bind = cfg.bind

    @property
    def public_desc(self) -> str:
        if not self.web:
            return f"{self.backend_bind}:{self.public_port}（只有后端，PHOTOAR_WEB=0）"
        tls = os.environ.get("WEBFRONT_TLS_CERT") and os.environ.get("WEBFRONT_TLS_KEY")
        scheme = "https" if tls else "http"
        return f"{scheme}://0.0.0.0:{self.public_port}  /=网页版 /admin=管理台 /v1/*=API"

    @property
    def backend_desc(self) -> str:
        if not self.web:
            return "（同上，就是它自己）"
        return f"{self.backend_bind}:{self.backend_port}（容器内部，不对外）"

    @property
    def tls(self) -> bool:
        return bool(
            os.environ.get("WEBFRONT_TLS_CERT") and os.environ.get("WEBFRONT_TLS_KEY")
        )

    def publish(self) -> None:
        """把算出来的端口写给健康检查看。

        健康检查是**另一个进程**（docker 每 30 秒起一次），它拿不到我们的环境。让它
        自己再解一遍"环境变量 > config.json > 默认值"就是第三份实现，而它解错的表现
        是"服务好好的，容器一直 unhealthy" —— 编排会去反复重启一个健康的容器。

        写不进去不算错误：健康检查那边有一套按环境变量的兜底（只在挂了 config.json
        且里面改过 port 时才会不准），为这个让服务起不来是本末倒置。
        """
        try:
            RUNTIME_FILE.write_text(
                json.dumps(
                    {
                        "public_port": self.public_port,
                        "backend_port": self.backend_port,
                        "web": self.web,
                        "tls": self.tls,
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[entrypoint] ⚠️ 写不了 {RUNTIME_FILE}（{exc}），健康检查改走环境变量兜底", flush=True)


def _spawn(name: str, argv: list[str], env: dict[str, str]) -> int:
    """起一个子进程，返回 pid。

    用 `os.posix_spawn` 而不是 `subprocess.Popen`：下面的等待是
    `os.waitpid(-1, …)`（要同时等两个亲儿子**和**被 PID 1 收养的孤儿），而
    Popen 的内部账本会因为"状态被别人收走了"而在解释器退出时抱怨。裸 pid 没这问题。
    """
    print(f"[entrypoint] 起 {name}：{' '.join(argv)}", flush=True)
    return os.posix_spawn(argv[0], argv, env)


def _supervise(specs: list[tuple[str, list[str], dict[str, str]]]) -> int:
    """起 specs 里的每一个，任何一个退出就把其余的收干净，返回第一个退出者的退出码。

    ## 为什么"任一退出即整体退出"

    这两个进程谁也离不开谁：Node 没有后端就是一个 502 生成器，后端没有 Node 就没人
    发页面。让活着的那个继续跑，得到的是一个"容器 healthy、功能坏了"的状态 ——
    而那正是编排系统帮不上忙的形态。整体退出之后 `restart: unless-stopped` 会把
    两个一起重新拉起来，那才是我们要的。

    ## 孤儿

    这个进程是 PID 1，容器里任何进程的父进程死了都会被过继到这里。不给它们收尸就是
    一堆僵尸占着进程表（长跑的容器里 ffmpeg 的孙子进程真的会走这条路）。所以下面
    `waitpid(-1)` 收到不认识的 pid 时只是丢掉，不当成"子进程退出"。
    """
    children: dict[int, str] = {}
    for name, argv, env in specs:
        children[_spawn(name, argv, env)] = name

    stopping = False
    first_code: int | None = None

    def _forward(signum: int, _frame) -> None:
        nonlocal stopping
        stopping = True
        print(f"[entrypoint] 收到 {signal.Signals(signum).name}，转发给 {len(children)} 个子进程", flush=True)
        for pid in list(children):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _forward)

    deadline: float | None = None
    while children:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            if deadline is not None and time.monotonic() > deadline:
                for p, name in children.items():
                    print(f"[entrypoint] {name} 超过 {STOP_GRACE_S:.0f}s 没退，SIGKILL", flush=True)
                    try:
                        os.kill(p, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                deadline = None  # 只发一次；下一轮 waitpid 会把它们收掉
            time.sleep(0.2)
            continue
        name = children.pop(pid, None)
        if name is None:
            continue  # 被收养的孤儿，收尸即可
        code = os.waitstatus_to_exitcode(status)
        # "意外"的判据是**这次收尾还没开始**。少了这一层，第一个死掉之后我们自己发的
        # 那轮 SIGTERM 会让第二个也被标成"意外退出" —— 于是日志里两条都像故障，
        # 而真正的那一条（第一条）就不显眼了。
        unexpected = not stopping
        if first_code is None:
            first_code = code if code >= 0 else 128 - code
        print(f"[entrypoint] {name} {'**意外退出**' if unexpected else '退出'}（{code}）", flush=True)
        if children and deadline is None:
            if unexpected:
                print("[entrypoint] 两半缺一不可，把另一半也停掉，交给 docker 重启整个容器", flush=True)
            stopping = True
            for p, other in children.items():
                try:
                    os.kill(p, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + STOP_GRACE_S

    return first_code or 0


# `photoar-server` 的子命令。见 `httpd.main` 里的 `sub.add_parser`。
#
# 这份名单的用途是**区分"起服务"和"跑个别的东西"**（下面 `_is_server_invocation`）。
# 名单漏了一个新加的子命令时，行为是"那个子命令被当成一个可执行文件去 exec"，
# 于是报 FileNotFoundError —— 一个响亮的失败，而不是静默起了个服务。
_SUBCOMMANDS = frozenset({"serve", "reindex", "build-vocab", "verify", "check"})


def _is_server_invocation(argv: list[str]) -> bool:
    """这次要起的是服务（或跑一个 photoar-server 子命令），还是别的东西？

    需要这个判断，是因为 ENTRYPOINT 会吃掉 `docker run <镜像> <命令>` 里的那个命令：
    不区分的话 `docker run img bash` 会变成
    `python -m photoar.server.httpd bash`，而 `docker run img python -c "..."` 会
    变成 `python -m photoar.server.httpd python -c ...`。两者都报一句 argparse 的
    "invalid choice"，看起来像镜像坏了。

    （`docker compose exec` 不走 ENTRYPOINT，所以 docs/deploy.md 里那些 exec 命令
    本来就没事。这里管的是 `docker run` 那条路 —— 排查时最顺手的那条。）

    判据：没给参数 = 起服务（CMD 的默认值）；第一个参数以 `-` 开头 = 是给
    photoar-server 的选项（比如 `--help`）；在子命令名单里 = 子命令；其余一律
    当成"要跑的别的东西"，原样 exec。
    """
    if not argv:
        return True
    first = argv[0]
    return first.startswith("-") or first in _SUBCOMMANDS


def main(argv: list[str]) -> int:
    if not _is_server_invocation(argv):
        # 一次性命令：**不**建目录、不取模型、不打配置摘要。它可能就是来看看文件系统
        # 的（`docker run img ls /data`），那时在旁边建一堆目录属于副作用。
        print(f"[entrypoint] 直接 exec：{' '.join(argv)}", flush=True)
        os.execvp(argv[0], argv)

    # 先把 photoar 导进来。装不上的话下面什么都不用做了，而这个错误要能一眼看出是
    # 镜像的问题而不是配置的问题。
    from photoar.server.config import ConfigError, ServerConfig

    config_file = os.environ.get("PHOTOAR_CONFIG", "/config/config.json")
    have_file = Path(config_file).is_file()

    try:
        cfg = ServerConfig.load(config_file) if have_file else ServerConfig.from_env()
    except ConfigError as exc:
        print(f"[entrypoint] 配置不对，起不来：{exc}", file=sys.stderr, flush=True)
        return 2

    print(
        f"[entrypoint] 配置来源：{'文件 ' + config_file if have_file else '环境变量'}",
        flush=True,
    )
    # 建目录放在取模型**之前**：模型要落到 model_dir 里，而那个目录可能还不存在。
    cfg.ensure_dirs()
    _fetch_model_if_needed()

    layout = Layout(cfg)
    layout.publish()
    print(_summary(cfg, layout), flush=True)

    # 只有文件真的在的时候才传 `-c`。传一个不存在的路径也能跑（`httpd._load` 会退回
    # 环境变量），但那样会多打一行"配置文件不存在"的提示 —— 而在"用户本来就没打算用
    # 配置文件"这条正常路径上，那行提示只会让人以为漏配了什么。
    cmd = [sys.executable, "-m", "photoar.server.httpd"]
    if have_file:
        cmd += ["-c", config_file]
    sub = argv or ["serve"]
    cmd += sub

    # `reindex` / `build-vocab` / `verify` / `check` 是**跑完就走**的一次性命令，
    # 不监听端口，也就不需要网页那一半。给它们起一个 Node 只会在日志里多一段噪音，
    # 而且那个 Node 会因为端口已被占用（同一台机器上真正的服务还在跑）直接失败。
    if sub[0] != "serve" or not layout.web:
        print(f"[entrypoint] exec {' '.join(cmd)}", flush=True)
        # execvp 而不是 subprocess：只有一个进程要跑的时候，让它**直接当 PID 1** ——
        # 否则 `docker stop` 的 SIGTERM 发给这个 wrapper，Python 默认会直接死掉而不
        # 转发，服务进程被留给 10 秒后的 SIGKILL，SQLite 那边就少了一次干净的关闭机会。
        os.execvp(cmd[0], cmd)
        return 0  # 到不了

    node = shutil.which("node")
    if node is None:
        print(
            "[entrypoint] 镜像里有 web-front 的代码却没有 node —— 这是镜像构建的问题，"
            "不是配置的问题。要临时绕过就设 PHOTOAR_WEB=0（只起后端）。",
            file=sys.stderr,
            flush=True,
        )
        return 3

    backend_env = {
        **os.environ,
        # 这两个是**这里说了算**，不看外面设了什么：合并形态下后端必须待在回环上，
        # 否则容器网络里的别人能绕过前面那一层直接打 /v1。
        "PHOTOAR_BIND": layout.backend_bind,
        "PHOTOAR_PORT": str(layout.backend_port),
    }
    front_env = {
        **os.environ,
        "PORT": str(layout.public_port),
        "HOST": "0.0.0.0",
        "PHOTOAR_UPSTREAM": f"http://127.0.0.1:{layout.backend_port}",
        # 网页版认的是 ORB 库（`cfg.library_dir` 就是 ORB 那个，见 config.py 的注释）。
        # 从 cfg 取而不是拼 `/data/library`：data_dir 是可配的，拼死的那份迟早对不上。
        "PHOTOAR_LIBRARY": str(cfg.library_dir),
    }

    return _supervise([
        ("后端 photoar", cmd, backend_env),
        ("网页版 web-front", [node, str(WEB_ENTRY)], front_env),
    ])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
