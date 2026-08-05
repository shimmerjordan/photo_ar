"""容器入口那个双进程管理器。

## 为什么这一份值得测

`docker/entrypoint.py` 从"算一下配置然后 exec"变成了 PID 1 上的进程管理器
（2026-08-05 把网页版与后端合进一个容器）。它管的三件事全都**只在容器里出错**，
而且出错的样子都不响：

* 端口算错 → 页面能开、`/v1` 全 502，看起来像后端挂了；
* 信号不转发 → `docker stop` 十秒后 SIGKILL，SQLite 少一次干净关闭；
* 一半死了另一半还活着 → 容器 healthy、功能坏了，编排帮不上忙。

所以这里把管理器**真的跑起来**（起真进程、发真信号），而不是断言它调了什么。
下面每个用例都在 `python -c` 的子进程里跑管理器 —— 它会安装 SIGTERM 处理器并阻塞，
在 pytest 进程里跑等于劫持测试框架自己的信号。
"""

from __future__ import annotations

import importlib.util
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENTRY = REPO / "docker" / "entrypoint.py"


def _load():
    spec = importlib.util.spec_from_file_location("photoar_entrypoint", ENTRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ep = _load()


class FakeCfg:
    """`Layout` 只用到 cfg 的这两个字段。"""

    def __init__(self, port: int = 8964, bind: str = "0.0.0.0") -> None:
        self.port = port
        self.bind = bind


# ---- 端口怎么分 ----


def test_web_on_puts_backend_on_loopback(monkeypatch):
    monkeypatch.delenv("PHOTOAR_WEB", raising=False)
    monkeypatch.delenv("PHOTOAR_INTERNAL_PORT", raising=False)
    layout = ep.Layout(FakeCfg(port=8964))
    assert layout.web
    assert layout.public_port == 8964
    # 后端必须待在回环上：否则同一个 docker 网络里的别人能绕过前面那层直接打 /v1。
    assert layout.backend_bind == "127.0.0.1"
    assert layout.backend_port == ep.DEFAULT_INTERNAL_PORT
    assert layout.backend_port != layout.public_port


def test_public_port_follows_cfg_not_the_env(monkeypatch):
    """`cfg.port` 已经把"环境变量 > config.json > 默认值"解完了，这里不许再解一遍。

    挂了 config.json 且里面把 port 改成别的值时，重解一遍的那份会退回 8964，
    于是 Node 监听 8964 而 docker 映射的是另一个端口 —— 容器起来了但打不开。
    """
    monkeypatch.delenv("PHOTOAR_PORT", raising=False)
    assert ep.Layout(FakeCfg(port=9100)).public_port == 9100


def test_web_off_falls_back_to_the_old_single_process_shape(monkeypatch):
    monkeypatch.setenv("PHOTOAR_WEB", "0")
    layout = ep.Layout(FakeCfg(port=8964, bind="0.0.0.0"))
    assert not layout.web
    # 只有后端时它自己就是对外的那个监听者 —— 回到合并之前的形态。
    assert (layout.backend_bind, layout.backend_port) == ("0.0.0.0", 8964)


def test_port_collision_refuses_to_start(monkeypatch):
    """两个进程抢同一个端口时要**说出来**，而不是让后起的那个报 EADDRINUSE。

    EADDRINUSE 出现在日志里的样子是"网页版启动失败"，而真正的原因是两个端口配成了
    一样 —— 从那条报错完全看不出来。
    """
    monkeypatch.delenv("PHOTOAR_WEB", raising=False)
    monkeypatch.setenv("PHOTOAR_INTERNAL_PORT", "8964")
    with pytest.raises(SystemExit, match="8964"):
        ep.Layout(FakeCfg(port=8964))


# ---- 管理器 ----

# 子进程里跑管理器。`grace` 覆盖 STOP_GRACE_S，好让"赖着不走"那个用例一秒钟出结果
# 而不是十秒。
_RUNNER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('ep', {entry!r})
ep = importlib.util.module_from_spec(spec); spec.loader.exec_module(ep)
ep.STOP_GRACE_S = {grace!r}
import os
env = dict(os.environ)
specs = {specs}
sys.exit(ep._supervise([(n, a, env) for n, a in specs]))
"""

PY = sys.executable


def _child(src: str) -> str:
    """一个跑 `src` 的子进程命令，写成 repr 好塞进 _RUNNER。"""
    return f"[{PY!r}, '-c', {src!r}]"


def _supervise(specs_literal: str, *, grace: float = 1.0, timeout: float = 20.0):
    src = _RUNNER.format(entry=str(ENTRY), grace=grace, specs=specs_literal)
    t0 = time.monotonic()
    proc = subprocess.run(
        [PY, "-c", src], capture_output=True, text=True, timeout=timeout
    )
    return proc, time.monotonic() - t0


def test_one_child_exiting_takes_the_other_down():
    """两半缺一不可 —— 死一个就整体退出，让 docker 去重启。

    让活着的那个继续跑得到的是"容器 healthy、功能坏了"，而那正是编排系统帮不上忙
    的形态。
    """
    specs = (
        "[('先死的', " + _child("import sys; sys.exit(7)") + "),"
        " ('长跑的', " + _child("import time; time.sleep(60)") + ")]"
    )
    proc, elapsed = _supervise(specs)
    # 退出码是**先退出那个**的码，不是 0 —— 编排要能从码上看出这不是正常停机。
    assert proc.returncode == 7, proc.stderr
    assert "意外退出" in proc.stdout
    # 长跑的那个吃默认 SIGTERM 立刻就走，不该等到那个 sleep(60) 结束。
    #
    # 上界给 30 而不是 6：这条断言的**意图**是"没有等满 60 秒的 sleep"，而 30 秒同样
    # 能证明它。给 6 秒的那一版在机器忙的时候会假失败（实测：docker build + 无头
    # Chrome + pytest 同时抢 16 核时，光是起两个 python 子进程就要好几秒），
    # 而一条会因为机器忙而变红的测试，最后一定会被当成噪音忽略掉。
    assert elapsed < 30, f"收另一半收了 {elapsed:.1f}s，像是在等那个 sleep(60)"


def test_sigterm_reaches_both_children():
    """`docker stop` 的 SIGTERM 必须落到两个子进程上。

    不转发的话它们要等 10 秒后的 SIGKILL —— SQLite 那边就少了一次干净的关闭机会，
    而这件事在日志里不留痕迹。
    """
    marker_a = "/tmp/photoar-test-sigterm-a"
    marker_b = "/tmp/photoar-test-sigterm-b"
    for m in (marker_a, marker_b):
        Path(m).unlink(missing_ok=True)
    # 每个孩子收到 SIGTERM 就写一个文件再退出。文件在 = 信号真的到了它手上。
    body = (
        "import signal, sys, time, pathlib\n"
        "def h(*a):\n"
        "    pathlib.Path({m!r}).write_text('got')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, h)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    specs = (
        "[('甲', " + _child(body.format(m=marker_a)) + "),"
        " ('乙', " + _child(body.format(m=marker_b)) + ")]"
    )
    src = _RUNNER.format(entry=str(ENTRY), grace=1.0, specs=specs)
    proc = subprocess.Popen([PY, "-c", src], stdout=subprocess.PIPE, text=True)
    try:
        # 等两个孩子都起来了再发信号，否则可能在 posix_spawn 之前就打过去了。
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if Path(marker_a).exists() or proc.poll() is not None:
                break
            time.sleep(0.1)
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert Path(marker_a).read_text() == "got"
    assert Path(marker_b).read_text() == "got"
    for m in (marker_a, marker_b):
        Path(m).unlink(missing_ok=True)


def test_a_child_that_ignores_sigterm_gets_killed():
    """赖着不走的那个要在宽限期之后被 SIGKILL，不能把整个容器挂在那儿。

    挂住的后果是 dockerd 到点直接 SIGKILL 整个容器 —— 比我们自己 kill 更粗暴，
    而且日志里看不出是谁赖着。
    """
    specs = (
        "[('先死的', " + _child("import sys; sys.exit(3)") + "),"
        " ('赖着的', "
        + _child("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        + ")]"
    )
    proc, elapsed = _supervise(specs, grace=1.0, timeout=20)
    assert proc.returncode == 3, proc.stderr
    assert "SIGKILL" in proc.stdout, proc.stdout
    # 宽限期 1 秒 + 进程起停。上界同样给到 30，理由见上一条 —— 要证的是
    # "SIGKILL 真的发出去了、没有挂满 60 秒"，而不是"这台机器此刻有多快"。
    assert elapsed < 30, f"等了 {elapsed:.1f}s，像是在等那个 sleep(60)"


def test_orphans_get_reaped_without_being_mistaken_for_our_children():
    """被 PID 1 收养的孤儿只收尸，不能算成"一半死了"。

    容器里 ffmpeg 的孙子进程真的会走这条路。把它算成子进程退出的后果是：一次转码
    结束就把整个容器带下去。
    """
    # 甲会 fork 一个活得比它久的孙子，然后自己退出 —— 孙子于是过继给管理器。
    grandchild = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    time.sleep(3)\n"
        "    sys.exit(0)\n"
        "time.sleep(0.3)\n"      # 让孙子先跑起来
        "sys.exit(0)\n"
    )
    specs = (
        "[('生孙子的', " + _child(grandchild) + "),"
        " ('长跑的', " + _child("import time; time.sleep(60)") + ")]"
    )
    proc, elapsed = _supervise(specs, grace=1.0, timeout=25)
    # 甲自己退出（码 0）触发整体退出；孙子是孤儿，只被收尸。
    assert proc.returncode == 0, proc.stderr
    # 报出来的只有那两个亲儿子，孙子不在里面 —— 它退出时不该产生任何一行"退出"。
    reported = re.findall(r"^\[entrypoint\] \S+ (?:\*\*意外退出\*\*|退出)（", proc.stdout, re.M)
    assert len(reported) == 2, proc.stdout
    # 而且只有第一条是"意外"：第二条是我们自己发的 SIGTERM 造成的，标成意外会让
    # 日志里两条都像故障。
    assert proc.stdout.count("意外退出") == 1, proc.stdout
    # ⚠️ 这里**不能**断言"管理器没等孙子"。孙子继承了 stdout 那根管子，而
    # `subprocess.run(capture_output=True)` 要等管子上所有写端都关掉才返回 ——
    # 于是这个测试自己会等满孙子那 3 秒，与管理器等不等无关。写下来是因为
    # 上一版真的在这儿断言过 elapsed < 3，然后花时间去查一个不存在的 bug。


def test_publish_writes_what_healthcheck_reads(monkeypatch, tmp_path):
    """`entrypoint` 写的和 `healthcheck` 读的必须是同一份东西。

    健康检查是另一个进程，拿不到我们的环境。它自己再解一遍端口优先级的后果是
    "服务好好的、容器一直 unhealthy" —— 编排会去反复重启一个健康的容器。
    """
    import json

    monkeypatch.delenv("PHOTOAR_WEB", raising=False)
    monkeypatch.delenv("PHOTOAR_INTERNAL_PORT", raising=False)
    monkeypatch.setattr(ep, "RUNTIME_FILE", tmp_path / "rt.json")
    layout = ep.Layout(FakeCfg(port=8964))
    layout.publish()
    doc = json.loads((tmp_path / "rt.json").read_text())
    assert doc == {
        "public_port": 8964,
        "backend_port": ep.DEFAULT_INTERNAL_PORT,
        "web": True,
        "tls": False,
    }

    hc = importlib.util.spec_from_file_location(
        "photoar_healthcheck", REPO / "docker" / "healthcheck.py"
    )
    mod = importlib.util.module_from_spec(hc)
    hc.loader.exec_module(mod)
    monkeypatch.setattr(mod, "RUNTIME_FILE", tmp_path / "rt.json")
    assert mod._runtime() == doc


def test_publish_never_blocks_startup(monkeypatch, capsys):
    """写不进去也要照常启动 —— 为了一个健康检查的便利让服务起不来是本末倒置。"""
    monkeypatch.setattr(ep, "RUNTIME_FILE", Path("/proc/nonexistent-dir/rt.json"))
    ep.Layout(FakeCfg(port=8964)).publish()
    assert "写不了" in capsys.readouterr().out
