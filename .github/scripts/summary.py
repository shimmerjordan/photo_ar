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

它声称的每一条访问路径（`/`、`/admin`、`/v1/*`）都是同一个 job 上面那一步
用 curl 真打过的；它给的那条 `docker run` 与 CI 里跑通的那条是同一组必填项。所以要是
哪天多出一个必填的环境变量，CI 的容器会先变 unhealthy、job 先红 —— 而红的时候这份
摘要走的是另一条分支（不给部署说明）。**它讲不出没被验证过的话。**

## 用法

CI 里由 workflow 传环境变量调用。也可以直接跑来眼看输出（没有 `GITHUB_STEP_SUMMARY`
时打到 stdout）：

    IMAGE=ghcr.io/me/photo-ar-server VERSION=sha-1a2b3c4 PUBLISHED=true \
      BUILD_OUTCOME=success RUN_OUTCOME=success LOGIN_OUTCOME=success \
      VERSION_OUTCOME=success PERSIST_OUTCOME=success \
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
# e2e 是四个 step，workflow 分别传各自的 outcome —— 不再只看最后一个。
E2E_STAGES = [("起容器", "RUN_OUTCOME"), ("打接口", "LOGIN_OUTCOME"), ("版本号", "VERSION_OUTCOME"), ("重启验持久化", "PERSIST_OUTCOME")]
E2E_OK = all(env(var) == "success" for _, var in E2E_STAGES)


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


def _stage_status(outcome: str) -> str:
    return "✅ 通过" if outcome == "success" else ("❌ 失败" if outcome else "⏭ 没跑到")


def blocked() -> str:
    """构建或冒烟没过时的分支。**刻意不给部署说明。**

    绿一半的运行页上放一份"怎么部署"，读的人会以为有东西可部署。而这条流水线的设计
    正好相反：推镜像那两步排在冒烟之后，就是为了让不可用的镜像根本出不去。
    """
    e2e_rows = "\n".join(f"| {name} | {_stage_status(env(var))} |" for name, var in E2E_STAGES)
    return f"""## ⛔ 这一版不可部署

| 阶段 | 结果 |
|---|---|
| 编镜像 | {_stage_status(env("BUILD_OUTCOME"))} |
{e2e_rows}

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
    return """### 1 · 拿镜像

**这次没有推镜像。** 它编出来了、也跑通了，但只存在于这台 runner 上，run 结束就没了 ——
因为这次跑的时候 **publish 没勾**。

要发出去：**Actions → server → Run workflow**，四个输入：

| 输入 | 填什么 |
|---|---|
| `publish` | ✅ 勾上。不勾就永远只是跑一遍检查 |
| `version` | `0.1.2` 这种。留空只会得到 `:sha-<短sha>`（能拉，但不占版本号） |
| `latest` | 要不要把 `:latest` 指到这一版 |
| `release` | 要不要建 GitHub Release（顺带打 git tag） |

**推 main / 开 PR 会自动跑检查（test/web/lint），但不发版**；发版只能走上面这个
手动入口。打 git tag 本身也不会触发任何东西。

想跑含这次改动的镜像但完全不经过 GHCR：在**开发机**上带覆盖层构建（`COMPOSE_FILE=docker-compose.yml:deploy/compose.local.yml`，`build:` 只在覆盖层里；版本号会显示成 `x.y.z-dev`）。

⚠️ **`:latest` 只在发布时勾了 latest 才动** —— 按版本 tag（或 `:sha-xxxxxxx`）拉更可靠。
"""


def deploy() -> str:
    tag = TAGS[0] if (PUBLISHED and TAGS) else f"{IMAGE}:latest"
    return f"""### 2 · 起容器
```bash
docker run -d --name photo-ar-server -p {PORT}:{PORT} -e PHOTOAR_ROOTS=photos=/你的/照片 -v /你的/照片:/你的/照片:ro -v photoar-data:/data {tag}
```
必填只有 `PHOTOAR_ROOTS`（容器内路径的白名单根目录，与上面 CI 跑通的那组严格一致）和端口 `{PORT}`。登录 `admin`/`admin`，会被强制改密。
要开手机上传再加 `-e PHOTOAR_UPLOAD_DIR=/你的/inbox -v /你的/inbox:/你的/inbox`（须在 `PHOTOAR_ROOTS` 之内、挂载可写、和照片目录是兄弟目录）——不设就只是关掉上传，容器照样 healthy。
完整 compose：{doc("docker-compose.yml", "docker-compose.yml")}；首次部署：{doc("docs/deploy.md")}。
"""


def access() -> str:
    return f"""### 3 · 从哪访问
| URI | 谁用 |
|---|---|
| `http://<host>:{PORT}/` | 网页版 |
| `http://<host>:{PORT}/admin` | 管理台 |
| `http://<host>:{PORT}/v1/*` | 后端 API，未登录 **401** |
相机要 https（`https://` 或 `http://localhost`），见 {doc("docs/faq.md", "faq.md")}；给宾客发链接前：{doc("docs/deploy.md#7-发给宾客")}。
"""


def caveats() -> str:
    return f"""### 几件要留意的
* **GHCR 上的包默认 private**：拉之前先 `docker login ghcr.io`（或把 Package settings 改成 public）。
* **要回滚**：`docker pull {IMAGE}:sha-xxxxxxx` —— 这个 tag 每次构建都有，精确对应一次提交。

更多排障（隧道 502、latest 没更新、入库被拒……）：{doc("docs/faq.md")}。
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
