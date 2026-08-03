#!/usr/bin/env python3
"""取 XFeat 的 ONNX 模型，校验 sha256。**容器每次启动都会跑它，所以必须幂等。**

模型不进镜像（几 MB 的运行时资产，进镜像等于每次发版都重传一遍，也让"换模型"必须
重建镜像），所以它得在部署现场落到数据卷上。这个脚本就是那一步。

只用标准库：它跑在 entrypoint 里、在服务起来**之前**，那时唯一可以确定装好的东西
就是 Python 自己。requests 会好写一点，但为了一次下载在镜像里多装一个包，换来的是
"下载模型这一步自己依赖 pip 装成功"。

## 幂等：已存在且校验通过就直接返回

这不是优化，是必需的。entrypoint 每次启动都调它，而 NAS 上的启动可能发生在没有外网
的时候（重启路由、Cloudflare 挂了、ISP 抽风）。"文件已经在了就什么都不做"让那些启动
仍然能正常起来。

## 为什么校验完整性、以及为什么校验失败要删掉

一个截断的 onnx 文件（下载到一半断网、或者代理返回了一个 HTML 错误页）在
`onnxruntime.InferenceSession` 那里会抛，而抛出来的是一句 protobuf 解析错误 ——
它读起来像"这个模型格式不对"，完全不像"你下载的东西不完整"。留着它更糟：下次启动时
`--out` 已存在，幂等检查（如果只看存在性）会跳过下载，于是那个坏文件永久留在卷上。
所以校验失败必须删掉半成品，让下一次启动重新下。

## 默认 URL 指向本项目自己的 GitHub release

huggingface.co 在目标网络里**不可达**（实测），github.com 与
objects.githubusercontent.com 可达。所以默认地址是本项目的 release。

⚠️ **那个 release 目前还不存在**，所以默认地址会 404。这是已知且预期的状态，不是 bug。
刻意**不**把默认地址指向某个第三方的 XFeat ONNX 镜像来"让它跑通"：那些文件与官方权重
是否数值等价没人验证过（`tools/export_models.py` 的 docstring 里写了社区导出版的具体
问题），而一个描述子对不上的模型不会报错、只会让识别率静默变低 —— 那正是这个项目
最不该引入的一类缺陷。下载不到时给出三条可执行的出路（发布 release / 自己导出 /
`--url` 指别处），比让它"看起来能用"要好。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

MODEL_FILENAME = "xfeat.onnx"

# 当前导出产物的 sha256（`python tools/export_models.py` 的输出，4,313,719 字节
# = 4.31 MB）。
#
# 写成常量而不是"下载一份 .sha256 一起校验"：那种做法只能证明"下到的文件与同一个
# 服务器上那个校验和文件一致"，而两者是同一个人放上去的、走同一条链路 —— 被换掉的
# 时候会一起被换掉。写在源码里的期望值是与二进制**不同渠道**的，这才是校验有意义的
# 前提。代价是换模型必须同时改这行，而那正是应该被 code review 看见的一次改动。
EXPECTED_SHA256 = "29a81cefdac67fe2f1bb980ffec8e45e2d7b48c72d7a29771205e1430455c652"
EXPECTED_BYTES = 4_313_719

DEFAULT_URL = (
    "https://github.com/shimmerjordan/photo_ar/releases/download/models-v1/xfeat.onnx"
)

# 下载超时（秒）。给得比较宽：NAS 上的上行/下行可能很慢，而这一步失败的代价是
# XFeat 后端不可用（服务照样以 ORB 起来），不是启动卡死 —— entrypoint 不会等它
# 之外的任何东西。太短的超时会让一条慢但可用的链路永远下不完。
TIMEOUT_S = 120

# 读块大小。1 MiB：既不让一个 4MB 的文件产生几千次系统调用，也不在只有 3GB 内存的
# 机器上一次性吃下整个响应体。
CHUNK = 1 << 20


class FetchFailed(RuntimeError):
    """下载或校验失败。信息里必须带可执行的下一步。"""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def expected_bytes_for(expected_sha: str) -> int | None:
    """这个 sha256 对应的文件应该有多少字节，不知道就 None。

    字节数是**某一个具体产物**的属性，不是一条全局规则。所以它只在校验的正是那个已知
    产物时才成立：`--sha256` 指向另一份文件（比如你自己重新导出了一版、或者上游换了
    权重）时，`EXPECTED_BYTES` 与它无关 —— 拿它去比会让 `--sha256` 这个override
    **永远校验失败**，而失败信息说的是"大小不对"，看起来像文件下坏了，跟"你覆盖了
    sha"毫无关系。
    """
    return EXPECTED_BYTES if expected_sha == EXPECTED_SHA256 else None


def verify(path: Path, expected_sha: str) -> tuple[bool, str]:
    """`(通过吗, 说明)`。

    先比大小是因为它不用读完整个文件就能否掉最常见的那种坏法（截断），而且失败信息里
    "少了多少字节"比一个不匹配的十六进制串好读得多。大小未知时（见
    `expected_bytes_for`）跳过这一步，sha256 本身就够。
    """
    if not path.is_file():
        return False, "文件不存在"
    size = path.stat().st_size
    want = expected_bytes_for(expected_sha)
    if want is not None and size != want:
        return False, f"大小是 {size} 字节，期望 {want}（差 {size - want:+d}）"
    got = sha256_of(path)
    if got != expected_sha:
        return False, f"sha256 是 {got}，期望 {expected_sha}"
    return True, f"sha256 校验通过（{size} 字节）"


def download(url: str, dst: Path, expected_sha: str) -> None:
    """下到**同目录下的临时文件**，校验通过之后再 rename 到位。

    不直接写 `dst`：那样任何一次中断都会在目标位置留下一个坏文件（见模块 docstring
    第三节）。临时文件放在同一个目录里而不是 /tmp，是为了 `Path.replace` 是同一个
    文件系统内的原子 rename —— 跨文件系统时它退化成"拷贝 + 删除"，中途被杀又回到
    "目标位置有半个文件"。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name + ".", suffix=".part")
    tmp = Path(tmp_name)
    os.close(fd)
    try:
        req = urllib.request.Request(
            url,
            # 有些 CDN 对没有 User-Agent 的请求返回 403。写清是谁在下，出问题时
            # 对面的访问日志里能对上。
            headers={"User-Agent": "photoar-fetch-models/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, CHUNK)
        except urllib.error.HTTPError as exc:
            raise FetchFailed(_advice(url, f"HTTP {exc.code} {exc.reason}")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchFailed(_advice(url, f"连不上：{exc}")) from exc

        ok, why = verify(tmp, expected_sha)
        if not ok:
            # 删掉半成品，理由见模块 docstring 第三节。
            tmp.unlink(missing_ok=True)
            raise FetchFailed(
                f"从 {url} 下到的文件校验不过（{why}）。已删掉，没有留下半成品。\n"
                f"最常见的原因是中途断网，或者这个地址返回的是一个错误页/重定向页而不是"
                f"模型本身（`curl -sI '{url}'` 看一眼 Content-Type 和 Content-Length）。\n"
                f"如果你是**故意**换了模型，那就同时改 tools/fetch_models.py 里的 "
                f"EXPECTED_SHA256 与 EXPECTED_BYTES —— 那是一次应该被 review 看到的改动。"
            )
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _advice(url: str, problem: str) -> str:
    """下载失败时的可执行说明。

    三条出路都写出来，而且写清各自的代价 —— 只说"下载失败"会让人去反复重试一个
    根本不存在的地址（默认那个 release 现在确实还没发布）。
    """
    return (
        f"取不到 XFeat 模型：{problem}\n"
        f"  地址：{url}\n"
        f"\n"
        f"三条出路，任选其一：\n"
        f"  1) 自己导出（最可靠，与服务端/App 用的是同一份产物）：\n"
        f'       pip install -e ".[export]" && python tools/export_models.py --out <模型目录>\n'
        f"  2) 把导出好的 xfeat.onnx 放到本项目的 GitHub release（tag models-v1）上，"
        f"这个默认地址就通了；\n"
        f"  3) 已经有一份可信的 xfeat.onnx（sha256 == {EXPECTED_SHA256[:16]}…）挂在别处："
        f"用 --url 指过去，或设环境变量 PHOTOAR_MODEL_URL。\n"
        f"\n"
        f"取不到不影响服务启动：识别后端会以 orb 跑起来（那是已经通过出口条件的基线），"
        f"只是 xfeat 用不了。GET /v1/ping 的 backendDegraded 会告诉你这件事。"
    )


def fetch(
    out: Path,
    url: str = DEFAULT_URL,
    *,
    expected_sha: str = EXPECTED_SHA256,
    force: bool = False,
) -> tuple[bool, str]:
    """确保 `out` 是一份校验通过的模型。返回 `(有没有真的下载, 说明)`。

    `force` 之外**不重下已经对的文件**：容器每次启动都调这个函数（见模块 docstring）。
    """
    if not force:
        ok, why = verify(out, expected_sha)
        if ok:
            return False, f"已存在且{why}，跳过下载：{out}"
        if out.exists():
            # 存在但不对：先说清楚，再重下。静默覆盖的话，"我明明放了一份模型进去，
            # 它怎么又去下载"这件事没有任何线索。
            print(f"[fetch-models] {out} 存在但校验不过（{why}），重新下载", flush=True)
    download(url, out, expected_sha)
    return True, f"下载完成并校验通过：{out}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_models.py", description="取 XFeat 的 ONNX 模型并校验 sha256"
    )
    ap.add_argument(
        "--out",
        required=True,
        help=f"目标路径，或一个目录（那时文件名用 {MODEL_FILENAME}）",
    )
    ap.add_argument(
        "--url",
        default=os.environ.get("PHOTOAR_MODEL_URL") or DEFAULT_URL,
        help="下载地址。默认取环境变量 PHOTOAR_MODEL_URL，再退到本项目的 GitHub release",
    )
    ap.add_argument(
        "--sha256",
        default=EXPECTED_SHA256,
        help="期望的 sha256。默认是当前导出产物的那个，一般不该改（改了见 --help 里的说明）",
    )
    ap.add_argument("--force", action="store_true", help="即使已存在且校验通过也重下")
    args = ap.parse_args(argv)

    out = Path(args.out)
    # `--out` 给的是目录时自动补文件名：compose 里那个变量是"模型目录"（同一个目录
    # 还放词表），让用户在两处写同一个文件名迟早写歪一处。
    if out.is_dir() or str(args.out).endswith("/"):
        out = out / MODEL_FILENAME

    try:
        downloaded, why = fetch(out, args.url, expected_sha=args.sha256, force=args.force)
    except FetchFailed as exc:
        print(f"[fetch-models] {exc}", file=sys.stderr)
        return 1
    print(f"[fetch-models] {why}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
