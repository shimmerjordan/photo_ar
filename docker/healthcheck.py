#!/usr/bin/env python3
"""容器健康检查：两半各探一次。

合并容器之后这里面跑着两个进程（Node 的网页版在前、Python 的后端在后，见
`entrypoint.py`），所以要探两个地方：

* **后端** `GET http://127.0.0.1:<内部端口>/v1/ping` —— 判据见下面那段"401 也算健康"。
* **网页版** `GET http(s)://127.0.0.1:<对外端口>/healthz` —— 必须 200。

任何一个不通就是 unhealthy。这与 `entrypoint._supervise` 的规则一致：两半缺一不可，
活着的那一半单独跑起来只是一个"容器 healthy、功能坏了"的状态。

（`PHOTOAR_WEB=0` 时只有后端，那就只探后端。）

## 端口从哪儿来

优先读 `entrypoint` 写下的 `/tmp/photoar-runtime.json`。它是**唯一**把
"环境变量 > config.json > 默认值"这条优先级解完的地方 —— 在这里再解一遍就是第三份
实现，而它解错的表现是"服务好好的、容器一直 unhealthy"，编排会去反复重启一个健康的
容器。文件读不到时退回环境变量（只在挂了 config.json 且里面改过 port 时才会不准）。

## 为什么 401 也算健康（在没配 token 的时候）

`/v1/ping` 要鉴权（它不在 `PUBLIC_PATHS` 里）。以前的健康检查拿 `PHOTOAR_TOKEN` 当
Bearer，而 token 现在**允许为空**（鉴权由用户体系接管，见 `config.py` 的模块
docstring）—— 空 token 下那次探测必然 401，于是容器**永远不会转 healthy**，
`docker compose up -d` 之后 `depends_on: service_healthy` 的东西全部起不来。一个
"配置合法、服务完全正常"的部署被健康检查判死。

所以判据按 token 有没有配分成两种，各自都是"最强的那个"：

* **配了 token**：必须 200。此时 401 是一个**真问题**（容器里的 token 与你手上那份
  不一致 → 批量入库脚本会全部 401），健康检查该红。
* **没配 token**：200 或 401 都算健康。401 证明的是：进程在监听、HTTP 解析通了、
  路由匹配到了 `/v1/ping`、鉴权层跑过了并做出了判断。那已经是这个探针在没有凭证时
  能证明的全部。5xx、连不上、超时仍然是红的。

反过来"没配 token 就不检查了"更糟：那等于一个没设 token 的部署完全没有健康检查。

## 自签证书不校验

网页版那一半可以被配成自己监听 https（`WEBFRONT_TLS_CERT`，给手机在局域网自测用）。
这是进程对自己的探测、走的是 127.0.0.1，中间没有网络可被中间人 —— 而自测用的证书
本来就是自签的。校验它只会让健康检查在唯一需要它的场景下失效。

这条以前踩过：合并之前的 `web-front/server/healthcheck.js` 存在的全部理由，就是
Dockerfile 里那版一行 `node -e` 把 `http://` 写死了，于是**一开 TLS 容器就变
unhealthy**，而服务完全正常 —— `docker logs` 干净、页面也能打开，只有 `docker ps`
那一列在说不健康。

## 为什么不用 `python -c` 塞在 Dockerfile 里

原来那版是三行 `python -c`，用反斜杠续行。它能工作，但上面这一整段取舍没有任何地方
写得下 —— 而这个文件里最重要的东西就是那段取舍。另外同一份逻辑要在 Dockerfile 的
HEALTHCHECK 与 compose 的 healthcheck 里各写一遍，两份迟早分叉。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 探测超时。比 HEALTHCHECK 的 --timeout=5s 小一点，这样超时的时候是这个脚本自己
# 报出"超时"（日志里看得见），而不是被 docker 直接掐掉（只留一个空的失败）。
# 两个探测串行跑，所以每个给 2 秒。
TIMEOUT_S = 2

# 与 entrypoint.RUNTIME_FILE 必须一致。
RUNTIME_FILE = Path("/tmp/photoar-runtime.json")

# 自签证书不校验，理由见模块 docstring。
_NOVERIFY = ssl._create_unverified_context()


def _runtime() -> dict:
    """读 entrypoint 写下的端口，读不到就按环境变量兜底。"""
    try:
        doc = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            return doc
    except (OSError, ValueError):
        pass
    port = int((os.environ.get("PHOTOAR_PORT") or "8964").strip() or 8964)
    web = (os.environ.get("PHOTOAR_WEB") or "1").strip().lower() not in ("0", "false", "no", "off")
    internal = int((os.environ.get("PHOTOAR_INTERNAL_PORT") or "").strip() or 8965)
    return {
        "public_port": port,
        "backend_port": internal if web else port,
        "web": web,
        "tls": bool(os.environ.get("WEBFRONT_TLS_CERT") and os.environ.get("WEBFRONT_TLS_KEY")),
    }


def _probe(url: str, *, headers: dict[str, str] | None = None) -> int | Exception:
    """返回状态码，或者那个把请求挡下来的异常。"""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_NOVERIFY) as resp:
            resp.read(1)
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code
    except Exception as exc:  # noqa: BLE001 - 连不上/超时/TLS，全部算不健康
        return exc


def _check_backend(port: int) -> bool:
    token = (os.environ.get("PHOTOAR_TOKEN") or "").strip()
    # 探 127.0.0.1 而不是 cfg.bind：bind 可能是 0.0.0.0（不是一个可连的目标地址），
    # 而健康检查是在容器**内部**跑的，回环一定通。
    url = f"http://127.0.0.1:{port}/v1/ping"
    got = _probe(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    if isinstance(got, Exception):
        print(f"[healthcheck] 后端 {url} 探不通：{type(got).__name__}: {got}", file=sys.stderr)
        return False
    if got == 200:
        return True
    if got == 401 and not token:
        # 见模块 docstring：没有凭证时，401 就是"服务活着"的证明。
        return True
    why = "容器里的 PHOTOAR_TOKEN 与请求带的不一致" if got == 401 else f"HTTP {got}"
    print(f"[healthcheck] 后端 {url} → {got}（{why}）", file=sys.stderr)
    return False


def _check_web(port: int, tls: bool) -> bool:
    url = f"{'https' if tls else 'http'}://127.0.0.1:{port}/healthz"
    got = _probe(url)
    if isinstance(got, Exception):
        print(f"[healthcheck] 网页版 {url} 探不通：{type(got).__name__}: {got}", file=sys.stderr)
        return False
    if got == 200:
        return True
    # /healthz 只会回 200，别的状态码本身就说明路由或进程状态不对。
    print(f"[healthcheck] 网页版 {url} → {got}", file=sys.stderr)
    return False


def main() -> int:
    rt = _runtime()
    ok = _check_backend(int(rt.get("backend_port") or 8964))
    if ok and rt.get("web"):
        ok = _check_web(int(rt.get("public_port") or 8964), bool(rt.get("tls")))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
