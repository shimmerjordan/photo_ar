#!/usr/bin/env python3
"""批量入库：把一批「照片 + 视频」按顺序喂给 `POST /v1/photo`。

只用标准库，可以直接拷到 NAS 上跑（QTS 自带的 python3 够用），也可以在容器里跑
（`docker compose exec photo-ar-server python /opt/photoar/tools/batch_ingest.py …`）。

三个取舍都不是随手定的：

* **走 HTTP，而不是做成服务端子命令。** 跑着的服务独占 `/data`：catalog.db 与
  `library/{slots.json,desc.bin,words.bin}` 三份记录的条数必须永远相等。再开一个
  进程写它们就是两个写者，而错位一位的后果是「识别命中后播的是别人的视频」。
* **串行，不并发。** 近重复闸门（`library.conflicts`）是拿新照片跟**库里已有的**
  比，而服务端是多线程的 —— 并发提交两张互为近重复的照片，两边都会看到对方还不在
  库里，于是两张都进去，然后**两张都永久识别不出来**（spec §8.2）。这是正确性问题，
  不是快慢问题。入库本身也是 CPU 密集的（arcoreimg + 自匹配 + 可能的转码），N5095
  上 3 核配额被单个请求就吃满了，并发并不会更快。
* **路径不做 realpath。** QTS 上 `/share/Photo` 往往是指向
  `/share/CACHEDEV1_DATA/Photo` 的符号链接，而容器里挂进去的是前者 —— 解析成后者
  会被路径白名单挡掉（403 path_denied）。宿主机路径与容器内路径确实不一致时，用
  `--map 宿主机前缀=容器前缀` 改写。

用法：

    export PHOTOAR_TOKEN=...

    # 目录配对：主文件名（不含扩展名）相同的照片与视频算一对
    tools/batch_ingest.py --base http://10.0.0.9:8964 \
        --photos /share/Photo/2019 --videos /share/Video/2019 --width-mm 152

    # 或者给一份清单（TSV：照片 <TAB> 视频 <TAB> 打印宽度mm <TAB> 标题，后两列可省）
    tools/batch_ingest.py --base http://10.0.0.9:8964 --manifest pairs.tsv

断点续跑：进度写在 `--state`（默认 `batch-ingest-state.json`）里，再跑一次会跳过
已经成功的、以及被**确定性**拒绝的那些（质量分不够 / 近重复 / 不在白名单 / 格式不
支持）。网络错误、超时、5xx 不记账，下次会重试。换过照片想重试被拒的那批，加
`--retry-rejected`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 与 server/fsbrowser.py 的两个集合保持一致。不 import 是为了让这个脚本能单独
# 拷到 NAS 上跑（那台机器上没有装 photoar 包）。对不上的后果只是白发一次请求，
# 服务端会回 415 —— 所以这里宁可宽一点。
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".m2ts"}

# 这些 code 换个时间点再试也是同一个结果，记进 state 就不再重试。
# 注意 `already_ingested` 也在里面：它是「这张已经好了」，算成功的一种。
TERMINAL_CODES = {
    "quality_too_low",
    "no_features",
    "near_duplicate",
    "path_denied",
    "ref_not_image",
    "ref_undecodable",
    "already_ingested",
    "bad_print_width",
}


class Fail(RuntimeError):
    """这一条失败了，但值得下次再试（网络 / 超时 / 5xx）。"""


def post_json(base: str, path: str, token: str, body: dict, timeout: float) -> tuple[int, dict]:
    """返回 (status, json)。HTTP 层面的错误也照样解 body —— 服务端的拒绝原因全在里面。"""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # 不要 gzip：响应都是几百字节的 JSON，解压的代码不值得写。
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _json_or_empty(resp.read())
    except urllib.error.HTTPError as exc:
        payload = _json_or_empty(exc.read())
        if exc.code >= 500:
            raise Fail(f"服务端 {exc.code}：{payload.get('message') or payload}") from exc
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Fail(f"{type(exc).__name__}: {exc}") from exc


def _json_or_empty(raw: bytes) -> dict:
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"message": raw[:200].decode("utf-8", "replace")}
    return out if isinstance(out, dict) else {"message": str(out)[:200]}


# ---- 待办列表 ----


def pairs_from_dirs(photos: Path, videos: Path | None, recursive: bool) -> list[dict]:
    """同名配对。视频目录省略时就在照片旁边找。"""
    it = photos.rglob("*") if recursive else photos.glob("*")
    imgs = sorted(p for p in it if p.is_file() and p.suffix.lower() in IMAGE_EXT)

    # 视频按「主文件名小写」建索引：IMG_0421.MOV 和 img_0421.jpg 是一对，而挨个
    # 去 glob 一次在上万张的规模下是 O(n²) 次 stat。
    vdir = videos or photos
    vit = vdir.rglob("*") if recursive else vdir.glob("*")
    by_stem: dict[str, Path] = {}
    for v in sorted(x for x in vit if x.is_file() and x.suffix.lower() in VIDEO_EXT):
        by_stem.setdefault(v.stem.lower(), v)

    out = []
    for img in imgs:
        v = by_stem.get(img.stem.lower())
        out.append({"ref": img, "video": v, "title": None, "width_mm": None})
    return out


def pairs_from_manifest(path: Path) -> list[dict]:
    out = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("\t")]
        if not cols[0]:
            continue
        width = None
        if len(cols) >= 3 and cols[2]:
            try:
                width = float(cols[2])
            except ValueError:
                sys.exit(f"{path}:{lineno} 第三列不是数字：{cols[2]!r}")
        out.append(
            {
                "ref": Path(cols[0]),
                "video": Path(cols[1]) if len(cols) >= 2 and cols[1] else None,
                "width_mm": width,
                "title": cols[3] if len(cols) >= 4 and cols[3] else None,
            }
        )
    return out


def remap(p: Path, maps: list[tuple[str, str]]) -> str:
    """宿主机路径 → 容器内路径。os.path.abspath 而不是 resolve：见模块开头第三条。"""
    s = os.path.abspath(str(p))
    for src, dst in maps:
        if s == src or s.startswith(src.rstrip("/") + "/"):
            return dst.rstrip("/") + s[len(src.rstrip("/")):]
    return s


# ---- 进度 ----


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"done": {}, "rejected": {}}
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.exit(f"进度文件 {path} 读不出来（{exc}）。删掉它会从头再跑一遍（已入库的会被 409 跳过）。")
    st.setdefault("done", {})
    st.setdefault("rejected", {})
    return st


def save_state(path: Path, st: dict) -> None:
    # 先写临时文件再 replace：断电时要么是旧的完整版本，要么是新的完整版本，
    # 不会留下半行 JSON 让下次直接退出。
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def fmt_eta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="批量把照片入库到 photo-ar-server（串行，可续跑）",
    )
    ap.add_argument("--base", required=True, help="服务地址，例：http://10.0.0.9:8964")
    ap.add_argument("--token", default=os.environ.get("PHOTOAR_TOKEN", ""),
                    help="默认取环境变量 PHOTOAR_TOKEN")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--photos", type=Path, help="照片目录")
    src.add_argument("--manifest", type=Path, help="TSV 清单")
    ap.add_argument("--videos", type=Path, help="视频目录，省略则在照片旁边找同名视频")
    ap.add_argument("--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--width-mm", type=float, default=152.0,
                    help="打印宽度（毫米）。6 寸 152 / 5 寸 127 / 4 寸 102。默认 152")
    ap.add_argument("--title-from-name", action="store_true",
                    help="拿主文件名当标题（清单里显式给了标题的以清单为准）")
    ap.add_argument("--map", action="append", default=[], metavar="宿主机前缀=容器前缀",
                    help="路径前缀改写，可重复")
    ap.add_argument("--state", type=Path, default=Path("batch-ingest-state.json"))
    ap.add_argument("--retry-rejected", action="store_true",
                    help="连上次被确定性拒绝的那批也重试")
    ap.add_argument("--skip-videos", action="store_true",
                    help="只入照片不带视频（快，之后再逐条 attach）")
    ap.add_argument("--limit", type=int, default=0, help="只做前 N 条（先小批试）")
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="单条超时（秒）。带视频要转码，N5095 上一条 30 秒的约 85s")
    ap.add_argument("--dry-run", action="store_true", help="只打印要提交什么，不发请求")
    args = ap.parse_args(argv)

    if not args.token and not args.dry_run:
        sys.exit("没有 token：用 --token 给，或 export PHOTOAR_TOKEN")

    maps: list[tuple[str, str]] = []
    for m in args.map:
        if "=" not in m:
            sys.exit(f"--map 要写成 宿主机前缀=容器前缀，收到 {m!r}")
        a, b = m.split("=", 1)
        maps.append((os.path.abspath(a), b))

    if args.manifest:
        todo = pairs_from_manifest(args.manifest)
    else:
        if not args.photos.is_dir():
            sys.exit(f"{args.photos} 不是目录")
        todo = pairs_from_dirs(args.photos, args.videos, args.recursive)

    st = load_state(args.state)
    queue = []
    skipped_done = skipped_rejected = 0
    for item in todo:
        key = remap(item["ref"], maps)
        if key in st["done"]:
            skipped_done += 1
            continue
        if key in st["rejected"] and not args.retry_rejected:
            skipped_rejected += 1
            continue
        item["key"] = key
        item["video_key"] = None if (item["video"] is None or args.skip_videos) else remap(item["video"], maps)
        queue.append(item)
    if args.limit:
        queue = queue[: args.limit]

    with_video = sum(1 for i in queue if i["video_key"])
    print(
        f"待入库 {len(queue)} 条（其中带视频 {with_video}）"
        f"；已完成跳过 {skipped_done}，上次被拒跳过 {skipped_rejected}"
    )
    if not queue:
        return 0

    counts = {"ok": 0, "dup": 0, "rejected": 0, "failed": 0}
    started = time.monotonic()
    for n, item in enumerate(queue, 1):
        body = {
            "refPath": item["key"],
            "printWidthMm": item["width_mm"] or args.width_mm,
        }
        if item["video_key"]:
            body["videoPath"] = item["video_key"]
        title = item["title"] or (item["ref"].stem if args.title_from_name else None)
        if title:
            body["title"] = title

        head = f"[{n:>5}/{len(queue)}]"
        if args.dry_run:
            print(f"{head} {json.dumps(body, ensure_ascii=False)}")
            continue

        t0 = time.monotonic()
        try:
            status, payload = post_json(args.base, "/v1/photo", args.token, body, args.timeout)
        except Fail as exc:
            counts["failed"] += 1
            print(f"{head} 失败   {item['ref'].name}：{exc}（下次会重试）", flush=True)
            continue
        dt = time.monotonic() - t0

        code = payload.get("error")
        if status == 201:
            counts["ok"] += 1
            st["done"][item["key"]] = payload.get("photoId", "")
            print(f"{head} 入库   {dt:5.1f}s  {payload.get('photoId', '')[:8]}  {item['ref'].name}", flush=True)
        elif code == "already_ingested":
            counts["dup"] += 1
            st["done"][item["key"]] = payload.get("photoId", "")
            print(f"{head} 已有   {item['ref'].name}", flush=True)
        elif code in TERMINAL_CODES:
            counts["rejected"] += 1
            st["rejected"][item["key"]] = {
                "status": status,
                "error": code,
                "message": payload.get("message", ""),
                "score": payload.get("score"),
            }
            extra = f" 质量分 {payload['score']}" if payload.get("score") is not None else ""
            print(f"{head} 拒绝   {status} {code}{extra}  {item['ref'].name}", flush=True)
        else:
            # 不认识的 4xx：不记账，让人自己看清楚了再决定。
            counts["failed"] += 1
            print(f"{head} 失败   {status} {code}：{payload.get('message', '')}  {item['ref'].name}", flush=True)

        # 每条都落盘。上万张的规模下，「跑了十小时断电重来」是不可接受的。
        save_state(args.state, st)

        done_n = counts["ok"] + counts["dup"] + counts["rejected"] + counts["failed"]
        if done_n and n % 20 == 0:
            per = (time.monotonic() - started) / done_n
            print(
                f"      … 平均 {per:.1f}s/条，剩 {len(queue) - n} 条，"
                f"预计还要 {fmt_eta(per * (len(queue) - n))}",
                flush=True,
            )

    if args.dry_run:
        return 0

    elapsed = time.monotonic() - started
    print(
        f"\n完成：入库 {counts['ok']}，已有 {counts['dup']}，拒绝 {counts['rejected']}，"
        f"失败 {counts['failed']}，共 {fmt_eta(elapsed)}"
    )
    if counts["rejected"]:
        print(f"被拒的清单在 {args.state} 的 rejected 里（含质量分和原因）。")
    # 失败是「值得重试」，退出码非 0 好让上层脚本知道要再跑一遍。
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
