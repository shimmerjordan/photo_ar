#!/usr/bin/env python3
"""容器健康检查：探一次 `GET /v1/ping`。

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

## 为什么不用 `python -c` 塞在 Dockerfile 里

原来那版是三行 `python -c`，用反斜杠续行。它能工作，但上面这一整段取舍没有任何地方
写得下 —— 而这个文件里最重要的东西就是那段取舍。另外同一份逻辑要在 Dockerfile 的
HEALTHCHECK 与 compose 的 healthcheck 里各写一遍，两份迟早分叉。
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

# 探测超时。比 HEALTHCHECK 的 --timeout=5s 小一点，这样超时的时候是这个脚本自己
# 报出"超时"（日志里看得见），而不是被 docker 直接掐掉（只留一个空的失败）。
TIMEOUT_S = 4


def main() -> int:
    port = (os.environ.get("PHOTOAR_PORT") or "8964").strip() or "8964"
    token = (os.environ.get("PHOTOAR_TOKEN") or "").strip()
    # 探 127.0.0.1 而不是 cfg.bind：bind 可能是 0.0.0.0（不是一个可连的目标地址），
    # 而健康检查是在容器**内部**跑的，回环一定通。
    url = f"http://127.0.0.1:{port}/v1/ping"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read()
            return 0
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not token:
            # 见模块 docstring：没有凭证时，401 就是"服务活着"的证明。
            return 0
        why = (
            "容器里的 PHOTOAR_TOKEN 与请求带的不一致"
            if exc.code == 401
            else f"HTTP {exc.code}"
        )
        print(f"[healthcheck] {url} → {exc.code}（{why}）", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 连不上/超时/DNS，全部算不健康
        print(f"[healthcheck] {url} 探不通：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
