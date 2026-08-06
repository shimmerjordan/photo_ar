#!/usr/bin/env python3
"""把「这一版怎么拿、怎么起、从哪访问」写进 GitHub 的 Job Summary。

## 为什么这份东西存在

部署流程本来就在仓库里（`docs/deploy.md`），所以这份摘要**不是**它的第三份拷贝 ——
这个仓库已经因为"同一套流程写在两处"删过一次重复文件（见 `deploy/README.md` 顶部
那段）。它存在的理由是：你刚刚看着一次构建变绿，而**此刻你手上唯一缺的信息就是运行
页上才有的那几样** —— 这一版的 tag 叫什么、版本号是什么、推没推出去、哪些接口刚刚被
真的打通过。那几样在仓库的任何一份文档里都写不出来。

所以这里的分工是：
  * **只有在这里才知道的** —— tag、版本号、发布与否、这一次验过什么 —— 写全；
  * **稳定的架构事实** —— 一个端口按 URI 分、`PHOTOAR_ROOTS` 必填、相机要安全上下文
    —— 写上，因为不写就不成"照着能起来"，而它们是不会按月漂移的那一类；
  * **会漂的** —— 完整环境变量表、NAS 上的资源与设备透传、维护命令 —— 只给链接，
    而且链接**钉在这次构建的那个 commit 上**（下面 `doc()`），文档改了名也不会 404。

## 摘要里的说法为什么不会变成谎话

它声称的每一条访问路径（`/`、`/admin`、`/v1/*`、`/healthz`）都是同一个 job 上面那一步
用 curl 真打过的；它给的那条 `docker run` 与 CI 里跑通的那条是同一组必填项。所以要是
哪天多出一个必填的环境变量，CI 的容器会先变 unhealthy、job 先红 —— 而红的时候这份
摘要走的是另一条分支（不给部署说明）。**它讲不出没被验证过的话。**

## 用法

CI 里由 workflow 传环境变量调用。也可以直接跑来眼看输出（没有 `GITHUB_STEP_SUMMARY`
时打到 stdout）：

    IMAGE=ghcr.io/me/photo-ar-server VERSION=sha-1a2b3c4 \
      BUILD_OUTCOME=success E2E_OUTCOME=success PUBLISHED=true \
      TAGS=$'ghcr.io/me/photo-ar-server:0.2.0\\nghcr.io/me/photo-ar-server:latest' \
      python3 .github/scripts/summary.py
"""

from __future__ import annotations

import os
import sys

PORT = "8964"


def env(name: str, default: str = "") -> str:
    # `os.environ.get(name) or default` 而不是 `.get(name, default)`：Actions 里
    # 一个没走到的 step 的 outcome 是**空字符串**而不是缺失，两者要走同一条路。
    return (os.environ.get(name) or default).strip()


REPO = env("GITHUB_REPOSITORY", "OWNER/photo-ar")
SERVER = env("GITHUB_SERVER_URL", "https://github.com")
SHA = env("GITHUB_SHA", "main")
IMAGE = env("IMAGE", "ghcr.io/OWNER/photo-ar-server")
VERSION = env("VERSION", "unknown")
TAGS = [t.strip() for t in env("TAGS").splitlines() if t.strip()]
PUBLISHED = env("PUBLISHED") == "true"
BUILD_OK = env("BUILD_OUTCOME") == "success"
E2E_OK = env("E2E_OUTCOME") == "success"


def doc(path: str, label: str | None = None) -> str:
    """指向**这次构建那个 commit** 的文档链接。

    钉 sha 而不是 main：几个月后回来看一次旧构建，main 上的文档早就改了，而你想知道
    的是"当时那一版是怎么部署的"。
    """
    return f"[{label or path}]({SERVER}/{REPO}/blob/{SHA}/{path})"


def describe_tag(tag: str) -> str:
    """一个 tag 该在什么时候用。规则照着 workflow 里 `metadata-action` 那段配置来。"""
    name = tag.rsplit(":", 1)[-1]
    if name == "latest":
        return "最近一次**特意发布**的那一版（只有打 tag / 手动勾 publish 才会动它）"
    if name.startswith("sha-"):
        return "精确钉到某次提交。**回滚就用这个**"
    parts = name.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return "钉住这一版，不会被后续发布带走"
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return "跟着这条线的补丁版本走"
    return "分支名（手动 Run workflow 得到的）"


def blocked() -> str:
    """构建或冒烟没过时的分支。**刻意不给部署说明。**

    绿一半的运行页上放一份"怎么部署"，读的人会以为有东西可部署。而这条流水线的设计
    正好相反：推镜像那两步排在冒烟之后，就是为了让不可用的镜像根本出不去。
    """
    e2e = "✅ 通过" if E2E_OK else ("❌ 失败" if env("E2E_OUTCOME") else "⏭ 没跑到")
    return f"""## ⛔ 这一版不可部署

| 阶段 | 结果 |
|---|---|
| 编镜像 | {"✅ 通过" if BUILD_OK else "❌ 失败"} |
| 起容器并打接口 | {e2e} |

**镜像没有被推到 registry。** 推送那两步排在冒烟之后，所以出不去的正是出了问题的
那一版 —— registry 上还是上一次发布的镜像，线上服务不受这次失败影响。

第一个该看的地方是这个 job 里的 **「容器日志（失败时看这里）」** 那一步：容器起不来的
原因几乎都在它的前 20 行里（少了必填的环境变量、`/data` 写不进去、后端起来了但网页版
那一半没有）。

{doc("docs/deploy-details.md", "docs/deploy-details.md")} 里有排障那一节。
"""


def acquire() -> str:
    """怎么拿到镜像。三种情况措辞完全不同，含糊在这里是最贵的。"""
    if PUBLISHED and TAGS:
        rows = "\n".join(f"| `{t}` | {describe_tag(t)} |" for t in TAGS)
        first = TAGS[0]
        return f"""### 1 · 拿镜像

```bash
docker pull {first}
```

这次推上去的全部 tag（**同一个镜像**，就是下面那些检查跑过的那一个）：

| tag | 什么时候用 |
|---|---|
{rows}

> 第一次发布之后要手动做一件事：GHCR 上新建的包**默认 private**，NAS 上
> `docker compose pull` 会报 `denied`。仓库右侧 Packages → photo-ar-server →
> Package settings → Change visibility 改成 public（或者在 NAS 上 `docker login ghcr.io`）。
"""
    return f"""### 1 · 拿镜像

**这次没有推镜像。** 它编出来了、也跑通了，但只存在于这台 runner 上，run 结束就没了。
只有两种情况会推：打 `v*` tag，或者手动 Run workflow 把 publish 勾上。

```bash
# 发版（推荐）——推上去的必然是被起来打过接口的那一个镜像
git tag v0.2.0 && git push origin v0.2.0

# 只想临时发一版：Actions → server → Run workflow → 勾 publish

# 想跑含这次改动的镜像但不发版：在目标机器上自己构建
docker compose build   # 版本号会显示成 `x.y.z-dev`，那正是"不是 CI 出的镜像"的标记
```

要部署**已发布**的那一版（不含这次的提交）：`docker pull {IMAGE}:latest`。
"""


def deploy() -> str:
    return f"""### 2 · 起容器

**推荐走 compose** —— 资源限制、核显透传、只读挂载那些取舍都已经写在那份文件里，
而且每一条旁边都有为什么：

```bash
git clone {SERVER}/{REPO}.git && cd photo-ar
cp .env.example .env && $EDITOR .env    # 只有 PHOTOAR_ROOTS 需要你真的看一眼
docker compose up -d
docker compose logs -f photo-ar-server  # 抄走里面那行随机管理员口令
```

**最小的 `docker run`**（CI 里刚刚跑通的就是这一组必填项，只多两个
`PHOTOAR_ADMIN_*` 好让它自动登录测一遍）：

```bash
docker run -d --name photo-ar-server --restart unless-stopped \\
  -e PHOTOAR_ROOTS=照片=/share/Photo \\
  -v /share/Photo:/share/Photo:ro \\
  -v photoar-data:/data \\
  -p {PORT}:{PORT} \\
  {TAGS[0] if (PUBLISHED and TAGS) else f"{IMAGE}:latest"}
```

必填与必须持久的就这两样，其余全部有能用的默认值：

| | 为什么 |
|---|---|
| `PHOTOAR_ROOTS` | 白名单根目录，**容器内**的路径，不给的话 entrypoint 直接拒绝启动（症状是容器 2 秒就 unhealthy）。写法 `名字=路径,名字=路径`，名字会显示在目录浏览器里 |
| `/data` | 索引、SQLite、缩略图、转码产物。**必须是持久卷** —— 丢了要全库重新入库，每张约 5s |

阈值、闸门、识别后端、贴图方式都是**热配置**，在 `/admin` 里改，不用改这条命令。
完整的环境变量表在 {doc(".env.example")}。
"""


def access() -> str:
    return f"""### 3 · 从哪访问

**一个容器、一个端口，按 URI 分。** 容器里其实是两个进程（Node 在前发页面并反代、
Python 在后跑识别与 API），后者绑在容器内的 `127.0.0.1:8965` 上，不 EXPOSE 也打不到。

| URI | 谁用 |
|---|---|
| `http://<host>:{PORT}/` | 宾客扫照片的网页版 |
| `http://<host>:{PORT}/admin` | 网页管理台：入库、绑视频、阈值、用户与授权 |
| `http://<host>:{PORT}/v1/*` | 后端 API（`tools/batch_ingest.py` 打这里）。未登录 **401** |
| `http://<host>:{PORT}/healthz` | 网页版自己的存活探测，不碰上游也不碰识别库 |
| `http://<host>:{PORT}/api/config` | 确认线上跑的是哪一版：回的 JSON 里 `version` 字段 |

**第一次登录**：口令在启动日志里**只打印一次**。

```bash
docker compose logs photo-ar-server | grep 随机口令
curl -s http://<host>:{PORT}/api/config    # 顺手确认版本号是 {VERSION}
```

登进 `/admin` 之后立刻改掉它。想要一个自己记得住的就在 `.env` 里设
`PHOTOAR_ADMIN_PASSWORD` —— 它**只在库里一个 admin 都没有时**生效，所以改过口令之后
它不会把口令顶回去。

#### ⚠️ 要给宾客用，前面必须有一层 https

网页版要相机，而 `getUserMedia` **只在安全上下文里存在**：

| | |
|---|---|
| `https://` 任意域名 | ✅ 公网、隧道、反代都算 |
| `http://localhost` | ✅ 只有本机（手机上可以用 `adb reverse` 造出来） |
| `http://192.168.x.x` | ❌ |
| `http://100.x.x.x`（Tailscale） | ❌ |

现成的 Cloudflare Tunnel 加一条 ingress 指到这个端口就够。**只用 `/admin` 和 `/v1`
的话 http 直连没问题** —— 那两条不碰相机。

而且真证书不是"体验优化"：**Chromium 对有证书错误的源整站禁用磁盘缓存**，自签之下
每次进页面都要重下 2.4MB 的识别引擎（实测 71.5s vs 1.6s）。手机自测用
`WEBFRONT_TLS_CERT` / `WEBFRONT_TLS_KEY`（两个必须同时给，只给一个进程直接退出），
优先用 `tailscale cert` 的真证书。
"""


def caveats() -> str:
    verified = ""
    if E2E_OK:
        verified = f"""
<details><summary><b>这一版被真的验过什么</b></summary>

镜像编出来之后被**当生产环境起了一遍**（非 root、`/data` 是 volume、healthcheck 用
镜像自己带的那条），然后：

* 等到 healthcheck 变 `healthy`（不是 sleep 固定秒数 —— 用它就顺手验了那条命令是对的）
* `/v1/ping` 未登录 → **401**（不是 200 也不是 500）
* `/` → **200**：网页版那一半真的起来了，而且 `public/` 拷进了镜像
* `/admin` → **200**：反代到后端那条路由是通的
* `/healthz` → **200**
* 登录 → 拿 token → 打 `/v1/ping`：路由表、鉴权链、SQLite 能写，三样一起过
* 容器 `restart` 一次，原来的会话仍然有效 → `/data` 真的持久
* 容器报出来的版本号确实是 `{VERSION}`（不然设置页那行是假的）

覆盖不到的：`arcoreimg` 不在镜像里，所以**入库那条路在 CI 上会回 503**。这是预期的，
那一整条的覆盖在单元测试里（`fake_arcoreimg` fixture）。

</details>
"""
    return f"""### 镜像里**故意**没有的三样

| 缺什么 | 后果 | 怎么给 |
|---|---|---|
| `tools/arcoreimg` | 入库回 **503 `arcoreimg_missing`**，其余一切正常 | ARCore SDK 里的闭源二进制，不可再分发。自己取来挂到 `/opt/photoar/tools/arcoreimg`（记得 `chmod +x`） |
| `vocab.npz` | 能起、能识别，但扫一扫读数显示**「无词表」**，走全量比对、慢 | 入完库再训（用的是你这批照片自己的描述子）：`docker compose exec photo-ar-server photoar-server build-vocab` |
| `xfeat.onnx` | 自动回退 ORB，`/v1/ping` 的 `backendDegraded` 会说明 | ORB 是通过出口条件的基线，**不需要任何模型文件**；要 xfeat 见 {doc(".env.example")} |

<details><summary><b>出问题时先看这三条</b></summary>

* **一直 restarting** —— 先怀疑 `PHOTOAR_ROOTS` 没给或者路径写的是宿主机的。
  QNAP 上还有一条：用 `/share/Photo` 这一层，**不要用 `ls -l` 出来的
  `/share/CACHEDEV1_DATA/Photo`** —— 前者是符号链接，两者混用会 403。
* **`/` 打不开但 `/v1` 正常** —— 网页版那一半的问题。退路是设 `PHOTOAR_WEB=0`：
  `/` 变 404，管理台与 API 照常，**不用回滚镜像**。
* **要回滚** —— `docker pull {IMAGE}:sha-xxxxxxx`，那个 tag 每次构建都有，精确对应一次提交。

</details>
{verified}
### 更细的

| | |
|---|---|
| 第一次部署照着走（带「看到什么算成」） | {doc("docs/deploy.md")} |
| 取舍、实测数字、排障 | {doc("docs/deploy-details.md")} |
| 例行维护命令、`/data` 里每个文件丢了会怎样 | {doc("deploy/README.md")} |
| 全部环境变量 | {doc(".env.example")} |
| 为什么两个进程一个端口 | {doc("docker/entrypoint.py")} 的模块 docstring |
"""


def main() -> int:
    if not (BUILD_OK and E2E_OK):
        body = blocked()
    else:
        head = "已推送 GHCR" if (PUBLISHED and TAGS) else "已验证，未推送"
        body = "\n".join([
            f"## 📦 photo-ar `{VERSION}` · {head}",
            "",
            "一个容器跑起网页版 + 管理台 + API，**同一个端口按 URI 分**。",
            "",
            acquire(),
            deploy(),
            access(),
            caveats(),
        ])

    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(body)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
