#!/usr/bin/env python3
"""容器入口：建目录 → 需要时取模型 → 打印一次生效配置摘要 → exec 起服务。

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

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FETCH = REPO / "tools" / "fetch_models.py"

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


def _summary(cfg) -> str:
    """一次生效配置摘要。逐条挑字段，**不遍历 dataclass**（理由见 _SECRET_ENVS）。"""
    lines = [
        "[entrypoint] ---- 生效配置 ----",
        f"[entrypoint]   监听        {cfg.bind}:{cfg.port}",
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
    print(_summary(cfg), flush=True)

    # 只有文件真的在的时候才传 `-c`。传一个不存在的路径也能跑（`httpd._load` 会退回
    # 环境变量），但那样会多打一行"配置文件不存在"的提示 —— 而在"用户本来就没打算用
    # 配置文件"这条正常路径上，那行提示只会让人以为漏配了什么。
    cmd = [sys.executable, "-m", "photoar.server.httpd"]
    if have_file:
        cmd += ["-c", config_file]
    cmd += argv or ["serve"]

    print(f"[entrypoint] exec {' '.join(cmd)}", flush=True)
    # execvp 而不是 subprocess：服务必须是 **PID 1**，否则 `docker stop` 的 SIGTERM
    # 发给这个 wrapper，Python 默认会直接死掉而不转发 —— 服务进程被留给 10 秒后的
    # SIGKILL，SQLite 那边就少了一次干净的关闭机会。
    os.execvp(cmd[0], cmd)
    return 0  # 到不了


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
