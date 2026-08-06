"""运行页上那份部署说明（`.github/scripts/summary.py`）。

## 为什么这一份值得测

这份摘要是**给还没部署过的人在运行页上照着抄的**，而它有两种坏法，两种都不响：

1. **在不该给的时候给。** 构建或冒烟挂了的时候还印一份"怎么部署"，读的人会以为
   registry 上有东西可拉。而这条流水线的整个设计（推送排在冒烟之后）就是为了让
   坏镜像出不去 —— 摘要说反了就把那个设计抵消了。
2. **和它声称的那条命令漂了。** 摘要里那条最小 `docker run` 的价值全在"这一组必填项
   是被真的跑通过的"。哪天 CI 那一步多了一个必填的环境变量而摘要没跟上，摘要就成了
   一份跑不起来的说明 —— 而 CI 全绿，没有任何症状。

所以下面第二组用例是**从 workflow 里把那条 `docker run` 抠出来做比对**，而不是把
期望值再抄一遍。抄一遍就是把漂移搬了个地方。

用子进程跑而不是 import：那个脚本在模块级读环境变量（CI 就是这么调它的），import
一次就固定了，测不了分支。子进程也正好是 CI 里真实的调用形态。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "scripts" / "summary.py"
WORKFLOW = REPO / ".github" / "workflows" / "server.yml"

IMAGE = "ghcr.io/owner/photo-ar-server"
TAGS = "\n".join([f"{IMAGE}:0.2.0", f"{IMAGE}:0.2", f"{IMAGE}:sha-1a2b3c4", f"{IMAGE}:latest"])


def run(tmp_path: Path, **env) -> str:
    """跑一次脚本，返回它写进 `GITHUB_STEP_SUMMARY` 的内容。"""
    out = tmp_path / "summary.md"
    base = {
        "IMAGE": IMAGE,
        "VERSION": "sha-1a2b3c4",
        "GITHUB_REPOSITORY": "owner/photo-ar",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": "1a2b3c4d5e6f7890",
        "BUILD_OUTCOME": "success",
        "E2E_OUTCOME": "success",
        "PUBLISHED": "false",
        "TAGS": "",
        "GITHUB_STEP_SUMMARY": str(out),
    }
    base.update({k: v for k, v in env.items()})
    r = subprocess.run([sys.executable, str(SCRIPT)], env=base, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return out.read_text(encoding="utf-8")


# ---- 什么时候**不该**给部署说明 ----


@pytest.mark.parametrize(
    "build,e2e",
    [
        ("failure", ""),        # 镜像没编出来，冒烟压根没跑到（outcome 是空串不是缺失）
        ("success", "failure"),  # 编出来了但起不来
        ("success", ""),         # 冒烟被跳过 —— 没验过就等于没验过
    ],
)
def test_没验过的版本不给部署说明(tmp_path, build, e2e):
    md = run(tmp_path, BUILD_OUTCOME=build, E2E_OUTCOME=e2e)
    assert "不可部署" in md
    # 这三个是"照着抄就能起来"的入口。一个都不许出现 —— 出现一个就有人会抄。
    for lure in ("docker pull", "docker run", "docker compose up"):
        assert lure not in md, f"不可部署的版本里出现了 {lure}"


def test_推失败不说已推送(tmp_path):
    # meta 那一步跑过（所以 TAGS 有值）但 push 挂了。这时 registry 上还是上一版，
    # 说"已推送 GHCR"是谎话，而照着那个 tag 去 pull 会 manifest unknown。
    md = run(tmp_path, PUBLISHED="false", TAGS=TAGS)
    assert "没有推镜像" in md
    assert "已推送" not in md


# ---- 与 CI 里那条 docker run 的契约 ----


def _ci_env_names() -> set[str]:
    """从 workflow 里抠出冒烟那一步真正传给容器的环境变量名。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("docker run -d --name photoar-ci")
    block = text[start:text.index("photo-ar-server:ci", start)]
    return set(re.findall(r"-e (\w+)=", block))


def test_摘要里那条最小命令与_CI_跑通的那条是同一组必填项(tmp_path):
    md = run(tmp_path, PUBLISHED="true", TAGS=TAGS)
    # 两个 PHOTOAR_ADMIN_* 只是为了让 CI 能自动登录测一遍，不是部署必填的
    # （不设 = 生成随机口令打印一次，那才是推荐的形态）。
    required = _ci_env_names() - {"PHOTOAR_ADMIN_NAME", "PHOTOAR_ADMIN_PASSWORD"}
    assert required, "从 workflow 里没抠出任何环境变量 —— 正则和那一步漂了"
    for name in required:
        assert name in md, (
            f"CI 给容器传了 {name} 而摘要里没提。要么它是必填的（写进摘要），"
            f"要么它在 CI 里是多余的（从 workflow 删掉）—— 两者都不能不管。"
        )


def test_端口与容器内固定端口一致(tmp_path):
    # 摘要里写死了 8964。它和 Dockerfile 的 EXPOSE 必须是同一个数，否则那条
    # `-p` 映射抄过去打不通。
    md = run(tmp_path, PUBLISHED="true", TAGS=TAGS)
    expose = re.search(r"^EXPOSE (\d+)", (REPO / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert expose and f":{expose.group(1)}" in md


# ---- 运行页上才有的那几样信息 ----


def test_每个_tag_都带一句什么时候用(tmp_path):
    md = run(tmp_path, PUBLISHED="true", TAGS=TAGS)
    for tag in TAGS.splitlines():
        row = next((ln for ln in md.splitlines() if f"`{tag}`" in ln and ln.startswith("|")), None)
        assert row, f"{tag} 没出现在 tag 表里"
        # 光列出来没用：`:0.2` 和 `:latest` 的区别正是发版时会踩的那个
        assert len(row.split("|")[2].strip()) > 6, f"{tag} 只列了名字没说什么时候用"


def test_版本号一定出现(tmp_path):
    # 版本号是这份摘要唯一无法从仓库里查到的东西（设置页显示它、/api/config 回它）。
    for published in ("true", "false"):
        md = run(tmp_path, VERSION="v9.9.9", PUBLISHED=published, TAGS=TAGS)
        assert "v9.9.9" in md


def test_文档链接钉在这次构建的_commit_上(tmp_path):
    # 钉 main 的话，几个月后回来看一次旧构建，点进去是改过的文档。
    md = run(tmp_path, PUBLISHED="true", TAGS=TAGS)
    assert "/blob/1a2b3c4d5e6f7890/docs/deploy.md" in md
    assert "/blob/main/" not in md


def test_是追加而不是覆盖(tmp_path):
    # Actions 允许多个 step 往同一个文件里写。覆盖会吃掉别人写的那一段。
    out = tmp_path / "summary.md"
    out.write_text("上一步写的东西\n", encoding="utf-8")
    md = run(tmp_path, GITHUB_STEP_SUMMARY=str(out), PUBLISHED="true", TAGS=TAGS)
    assert md.startswith("上一步写的东西")


def test_没有_GITHUB_STEP_SUMMARY_时打到标准输出():
    # 本地眼看输出用的那条路。坏了不影响 CI，但坏了就没人会在改文案后先看一眼。
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={"IMAGE": IMAGE, "VERSION": "x", "BUILD_OUTCOME": "success", "E2E_OUTCOME": "success"},
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "docker run" in r.stdout
