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
        return "最近一次发布时**特意勾了「同时更新 latest」**的那一版"
    if name.startswith("sha-"):
        return "精确钉到某次提交。**回滚就用这个**，而且它每次发布都有"
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

**这次没有推镜像。** 它编出来了、也跑通了，但只存在于这台 runner 上，run 结束就没了 ——
因为这次跑的时候 **publish 没勾**。

要发出去：**Actions → server → Run workflow**，四个输入：

| 输入 | 填什么 |
|---|---|
| `publish` | ✅ 勾上。不勾就永远只是跑一遍检查 |
| `version` | `0.1.2` 这种。留空只会得到 `:sha-<短sha>`（能拉，但不占版本号） |
| `latest` | 要不要把 `:latest` 指到这一版 |
| `release` | 要不要建 GitHub Release（顺带打 git tag） |

**打 git tag 不会触发任何东西** —— 这条流水线只手动触发。

想跑含这次改动的镜像但完全不经过 GHCR：在目标机器上 `docker compose build`
（版本号会显示成 `x.y.z-dev`，那正是"不是 CI 出的镜像"的标记）。

⚠️ **别指望 `:latest` 是新的。** 它**只在发布时勾了 latest 才动**，所以停在
「上一次特意发布并勾了它」那一刻 —— 中间往 main 推过多少次都不会动它。真踩过：拉了 `:main`
拿到的还是几个月前的镜像，而那一版连网页版都还没合进容器，表现是 `/` 回
「没有这个接口」。**确认办法**：起来之后 `curl -s <host>/api/config` 看 `version` 字段，
或者 `docker exec <容器> printenv PHOTOAR_VERSION` —— 空的就是没经过版本注入的老镜像。
"""


def deploy() -> str:
    tag = TAGS[0] if (PUBLISHED and TAGS) else f"{IMAGE}:latest"
    return f"""### 2 · 起容器

**一份自包含的 compose，不用 clone 仓库。** 这就是 NAS 上实际在跑的那份 ——
把 `/share/Study/media_bed/photo-ar` 换成你自己的目录（**六处都要换**），其余照抄：

```yaml
services:
  photo-ar-server:
    image: {tag}
    container_name: photo-ar-server
    restart: unless-stopped
    ports:
      - "{PORT}:{PORT}"
    environment:
      PHOTOAR_ROOTS: photos=/share/Study/media_bed/photo-ar/photos,videos=/share/Study/media_bed/photo-ar/videos
      PHOTOAR_UPLOAD_DIR: /share/Study/media_bed/photo-ar/inbox
      LANG: C.UTF-8
    volumes:
      # 服务自己的库。**必须持久**，丢了要全库重新入库（每张约 5s）
      - /share/Study/media_bed/photo-ar/data:/data
      # 素材：只读，这个服务永远不改你的原始文件
      - /share/Study/media_bed/photo-ar/photos:/share/Study/media_bed/photo-ar/photos:ro
      - /share/Study/media_bed/photo-ar/videos:/share/Study/media_bed/photo-ar/videos:ro
      # 上传落地：唯一可写的用户目录，且必须在 ROOTS 之内
      - /share/Study/media_bed/photo-ar/inbox:/share/Study/media_bed/photo-ar/inbox
    healthcheck:
      test: ["CMD", "python", "/opt/photoar/docker/healthcheck.py"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
```

```bash
mkdir -p /share/Study/media_bed/photo-ar/{{photos,videos,inbox,data}}   # 先建出来，见下
docker compose up -d
docker compose logs -f photo-ar-server
```

登录用 **`admin` / `admin`**，进去会被强制改密（见下面「第一次登录」）。想跳过那一步
就在 `environment:` 里加一行 `PHOTOAR_ADMIN_PASSWORD: 你的强口令`。

必填与必须持久的只有两样，其余全部有能用的默认值（完整变量表在 {doc(".env.example")}；
阈值、闸门、识别后端这些是**热配置**，在 `/admin` 里改，不用动这份文件）：

| | 为什么 |
|---|---|
| `PHOTOAR_ROOTS` | 白名单根目录，**容器内**的路径。不给的话 entrypoint 直接拒绝启动 —— 症状是容器 2 秒就 unhealthy |
| `/data` | 索引、SQLite、缩略图、转码产物。**必须是持久卷** |

**四个不响的坑**，每个都真踩过：

1. **宿主机目录要先 `mkdir -p`。** bind mount 的源不存在时 dockerd 会**以 root 建一个空目录**，
   不报错 —— 然后服务真的去索引那个空目录，表现是"一张都认不出来"。
2. **`/data` 别落在 `PHOTOAR_ROOTS` 之内**，否则服务自己的 SQLite 会出现在管理台的目录浏览器里。
   上面把 ROOTS 指到 `photos/` 和 `videos/` 两个子目录而不是整个 `photo-ar/`，就是为了这个。
3. **别设 `WEBFRONT_TLS_CERT/KEY`**，除非你知道自己在干什么。设了之后容器在 {PORT} 上说的是
   TLS，而 Cloudflare Tunnel 的 ingress 若仍写 `http://` 会**502，且容器看起来完全健康**
   （`docker ps` 绿的、日志干净、healthcheck 也过，因为它在容器内部探）。
   走隧道时证书由 Cloudflare 提供，容器不需要再包一层。
4. **有核显想走硬件转码**，再加 `devices: [/dev/dri:/dev/dri]`。不加会**静默**回退 libx264 ——
   在 N5095 上慢一个量级，慢到会撞上隧道的 125 秒超时。

**只想快速试一下**（不落盘配置）：

```bash
docker run -d --name photo-ar-server -p {PORT}:{PORT} \\
  -e PHOTOAR_ROOTS=photos=/media/photos \\
  -v /你的/照片:/media/photos:ro -v photoar-data:/data \\
  {tag}
```
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

**第一次登录**：默认是 **`admin` / `admin`**。

管理台会**强制**你先改掉它才让进（服务端在 `/auth/login` 与 `/auth/me` 上都回一个
`mustChangePassword`，前端见到就把界面锁成只剩改密表单）。

⚠️ **但在你改掉之前，这个站等于没有口令** —— 那个默认值就印在公开源码里，而这里
没挂 Cloudflare Access、登录也没有速率限制。所以**部署完立刻登录**，别放着过夜。
更好的做法是一开始就设 `PHOTOAR_ADMIN_PASSWORD`，那样默认值根本不会被用到、
也不会弹强制改密页。

```bash
curl -s http://<host>:{PORT}/api/config    # 顺手确认版本号是 {VERSION}
```

`PHOTOAR_ADMIN_PASSWORD` **只在库里一个 admin 都没有时**生效 —— 改过口令之后它不会
把口令顶回去，忘了也不能靠改它找回（那时只能 `delete from user where name='admin'`
再重启，让它重新引导）。

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
