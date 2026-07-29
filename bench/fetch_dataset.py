"""取真实照片语料：图像检索领域的标准基准集（Oxford5k / Paris6k）。

为什么换掉 Wikimedia Commons（`fetch_real_photos.py` 那条路）：

1. **它现在直接以 robot policy 拒绝**，不只是限流：
   `HTTP 429: Your request does not comply with our robot policy,
   https://w.wiki/4wJS`。这是明确的"不欢迎自动访问"，不该继续敲。
2. 就算不被拒，`categorymembers` 那条查询本身就是低效的 —— 实测
   `Category:Trevi Fountain` 的 `gcmtype=file` 返回 **0** 个文件（照片都在
   别处），所以上一轮 40 个分类只取到 153 张。改用 `generator=search`
   一次就能返回 50 个合格 jpg，但那也绕不开上面第 1 条。
3. 限流下 1.5 小时才 153 张，上规模不可行。

Oxford5k / Paris6k 是**为批量下载而托管**的研究基准集（VGG，牛津），
单个 tarball、支持 Range 并发，实测 6 段并发约 3.4 MB/s，1.94 GB 约 10 分钟。
更关键的是内容对口：它们是同一批地标建筑的大量不同视角照片 —— 正是
Phase 0 要回答的那个问题（"上万张**高度自相似**的照片能否区分"）的标准难例，
比随机抓来的 Commons 照片更硬。

语义提醒（与 Commons 那轮相同）：同一建筑的两张不同照片**应当被区分开**，
它们是两张不同的实体照片，各自关联不同的视频。组内相似是难度来源。

用法：
    python bench/fetch_dataset.py --dataset oxford5k --out /path/to/data
    python bench/fetch_dataset.py --dataset paris6k  --out /path/to/data

产物：<out>/photos/*.jpg（扁平化）以及 <out>/<name>.tgz（保留，便于重跑）
"""

import argparse
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = ("photo-ar-phase0/0.1 (private offline image-retrieval benchmark; "
      "non-redistributive)")

# 已实测的直链与体积（Content-Length）。www.robots.ox.ac.uk 上的旧地址会 301
# 到 thor.robots.ox.ac.uk，这里直接写终点，避免 urllib 处理跨主机重定向。
DATASETS = {
    "oxford5k": [
        ("oxbuild_images-v1.tgz",
         "https://thor.robots.ox.ac.uk/oxbuildings/oxbuild_images-v1.tgz",
         1_938_238_004),
    ],
    "paris6k": [
        ("paris_1-v1.tgz", "https://thor.robots.ox.ac.uk/parisbuildings/paris_1.tgz",
         1_269_538_001),
        ("paris_2-v1.tgz", "https://thor.robots.ox.ac.uk/parisbuildings/paris_2.tgz",
         None),
    ],
}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
CHUNK = 1 << 20


def log(msg: str) -> None:
    print(msg, flush=True)


def _head_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n else None
    except urllib.error.HTTPError as e:
        log(f"  ! HEAD 失败 HTTP {e.code}")
        return None


def _download_segment(url: str, dst: Path, start: int, end: int, state: dict) -> None:
    """把 [start, end] 这段字节写进 dst 的对应偏移。失败重试 3 次。

    每段独立打开文件、seek 到自己的偏移写入 —— 段之间不共享文件句柄，
    所以不需要锁；state 只用于进度统计，用锁保护。
    """
    for attempt in range(3):
        written = 0
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"}
            )
            with urllib.request.urlopen(req, timeout=120) as r, dst.open("r+b") as f:
                f.seek(start)
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
                    with state["lock"]:
                        state["done"] += len(buf)
            return
        except Exception as e:  # noqa: BLE001 — 网络错误形态很多，统一重试
            # 重试是从 start 重新拉整段，本次已写的字节会被覆盖重写。必须把
            # 它们从进度计数里扣回去，否则进度会越 100% 越走（重试几次就报
            # "2.4/1.94 GB (124%)"），把一个正常的重试显示成明显的故障。
            with state["lock"]:
                state["done"] -= written
            wait = 2 ** attempt
            log(f"  · 段 {start}-{end} 失败（{e}），已回退 {written / 1e6:.0f} MB 计数，"
                f"{wait}s 后重试 ({attempt + 1}/3)")
            time.sleep(wait)
    raise RuntimeError(f"段 {start}-{end} 三次重试后仍失败")


def download(url: str, dst: Path, expected: int | None, segments: int) -> None:
    """并发分段下载。dst 已存在且体积正确时直接跳过（可重跑）。"""
    size = expected or _head_size(url)
    if size is None:
        raise RuntimeError(f"拿不到 {url} 的 Content-Length，无法分段下载")
    if dst.exists() and dst.stat().st_size == size:
        log(f"  已存在且体积正确（{size / 1e9:.2f} GB），跳过下载")
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    # 预分配到目标大小，各段才能各自 seek 写入
    with dst.open("wb") as f:
        f.truncate(size)

    per = size // segments
    bounds = [(i * per, (size - 1) if i == segments - 1 else (i + 1) * per - 1)
              for i in range(segments)]
    state = {"done": 0, "lock": threading.Lock()}
    threads = [
        threading.Thread(target=_download_segment, args=(url, dst, s, e, state),
                         daemon=True)
        for s, e in bounds
    ]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        time.sleep(5)
        with state["lock"]:
            done = state["done"]
        el = time.perf_counter() - t0
        rate = done / el if el else 0
        eta = (size - done) / rate if rate else 0
        log(f"  {done / 1e9:.2f}/{size / 1e9:.2f} GB "
            f"({done / size:.0%})  {rate / 1e6:.1f} MB/s  剩余 {eta / 60:.0f} 分")
    for t in threads:
        t.join()

    actual = dst.stat().st_size
    if actual != size:
        raise RuntimeError(f"下载后体积不符：期望 {size}，实际 {actual}")
    log(f"  下载完成 {size / 1e9:.2f} GB，{(time.perf_counter() - t0) / 60:.1f} 分")


def extract_images(tar_path: Path, photos: Path) -> int:
    """把 tarball 里的图片扁平化解到 photos/。同名冲突时加数字后缀。

    只解普通文件、只解图片后缀，并拒绝任何绝对路径或 '..' 成分 —— tarball
    来自外部，不能无条件相信里面的路径（经典的 tar 路径穿越）。
    """
    photos.mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf:
            if not m.isfile():
                continue
            name = Path(m.name)
            if name.is_absolute() or ".." in name.parts:
                log(f"  ! 跳过可疑路径 {m.name}")
                continue
            if name.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            dst = photos / name.name
            if dst.exists():
                stem, suf, k = dst.stem, dst.suffix, 1
                while dst.exists():
                    dst = photos / f"{stem}_{k}{suf}"
                    k += 1
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, dst.open("wb") as f:
                shutil.copyfileobj(src, f)
            n += 1
            if n % 1000 == 0:
                log(f"  解出 {n} 张")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    ap.add_argument("--out", required=True)
    ap.add_argument("--segments", type=int, default=6,
                    help="并发分段数，默认 6（实测单连接 0.9 MB/s，6 段约 3.4 MB/s）")
    args = ap.parse_args()
    # 刻意没有 --keep-tarball 开关：tarball 永远保留（重跑时体积对得上就跳过
    # 下载），所以那个开关只会是一个永远为真、代码里根本没读的假选项。

    if args.segments < 1:
        print(f"--segments 必须为正整数，收到 {args.segments!r}", file=sys.stderr)
        return 2

    out = Path(args.out)
    photos = out / "photos"
    total = 0
    t0 = time.perf_counter()
    for name, url, expected in DATASETS[args.dataset]:
        log(f"[fetch] {name}  <- {url}")
        tar_path = out / name
        try:
            download(url, tar_path, expected, args.segments)
        except RuntimeError as e:
            print(f"下载 {name} 失败：{e}", file=sys.stderr)
            return 1
        log(f"[fetch] 解包 {name}")
        n = extract_images(tar_path, photos)
        log(f"[fetch] {name}: {n} 张图")
        total += n

    log(f"[fetch] 完成：{total} 张 -> {photos}"
        f"（{(time.perf_counter() - t0) / 60:.1f} 分）")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
