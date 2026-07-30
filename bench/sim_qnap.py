#!/usr/bin/env python3
"""在本地 Docker 里按 QNAP 的资源配额跑真服务端，量四件事。

为什么要有这个脚本：spec §14.2 的 P95 一项写着「⚠️ 待复测」，Phase 1 的「未覆盖」
里写着「N5095 上的 P95 复测、一万张规模的服务端入库」。手上没有那台 NAS，但
`docker run --cpus/--memory` 能把**配额**限成一样，而配额恰好能验证三件真正会
出事的事：

1. **3GB 内存够不够**。docker-compose 里 `mem_limit: 3g` 是拍出来的数。不够会
   OOM-kill 整个容器 —— 那是「服务突然没了」，而不是「慢一点」。
2. **库规模长起来之后延迟怎么变**。倒排索引的候选数随库增长，这个斜率与 CPU
   快慢无关，本地量出来的形状对 NAS 成立。
3. **并发下会不会互相拖死**。单进程 + ThreadingHTTPServer，识别是 CPU 密集的，
   3 核配额下同时来 4 个请求会发生什么。

**不能**验证的：绝对延迟。`--cpus` 限的是 CPU 时间配额，不是单核速度 ——
i9-11900K 的单核比 N5095 快好几倍。所以本脚本报的 P95 是「本机受限」值，
外加一个按单线程性能比推的估算，估算部分明确标出来。

用法：
    python3 bench/sim_qnap.py --photos 300 --queries 200
可覆盖：
    --cpus/--memory  容器配额，默认与 docker-compose.yml 一致（3.0 / 3g）
    --keep           结束后保留工作目录与容器
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from photoar import synth  # noqa: E402
from photoar import transcode as T  # noqa: E402

IMAGE = "photo-ar-server:sim"
NAME = "photoar-sim"
PORT = 18964

# N5095 与 11900K 的单线程性能比，用来把本机延迟折算到 NAS。PassMark 单线程
# 分 ~1150 vs ~3550。**这是估算，不是实测** —— 报告里必须标出来。
SINGLE_CORE_RATIO = 3.1


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def log(msg: str) -> None:
    print(msg, flush=True)


@dataclass
class Sampler:
    """后台采 `docker stats`。峰值内存是这一轮最要紧的一个数。"""

    name: str
    peak_mem_mb: float = 0.0
    peak_cpu_pct: float = 0.0
    samples: int = 0
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            p = sh(["docker", "stats", "--no-stream", "--format",
                    "{{.MemUsage}}|{{.CPUPerc}}", self.name])
            if p.returncode == 0 and "|" in p.stdout:
                mem, cpu = p.stdout.strip().split("|", 1)
                self.peak_mem_mb = max(self.peak_mem_mb, _mem_mb(mem))
                self.peak_cpu_pct = max(self.peak_cpu_pct, _pct(cpu))
                self.samples += 1
            self._stop.wait(1.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)


def _mem_mb(s: str) -> float:
    # "123.4MiB / 3GiB"
    used = s.split("/")[0].strip()
    for suf, mul in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024.0), ("B", 1 / 1048576.0)):
        if used.endswith(suf):
            try:
                return float(used[: -len(suf)]) * mul
            except ValueError:
                return 0.0
    return 0.0


def _pct(s: str) -> float:
    try:
        return float(s.strip().rstrip("%"))
    except ValueError:
        return 0.0


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token

    def _req(self, path: str, data: bytes | None = None, ctype: str | None = None,
             timeout: float = 300.0) -> tuple[int, bytes]:
        r = urllib.request.Request(self.base + path, data=data)
        r.add_header("Authorization", "Bearer " + self.token)
        if ctype:
            r.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def ping(self) -> bool:
        try:
            code, _ = self._req("/v1/ping", timeout=4)
            return code == 200
        except OSError:
            return False

    def create(self, ref_path: str, print_width_mm: int, video_path: str | None,
               title: str) -> tuple[int, dict]:
        body = {"refPath": ref_path, "printWidthMm": print_width_mm, "title": title}
        if video_path:
            body["videoPath"] = video_path
        code, raw = self._req("/v1/photo", json.dumps(body).encode(), "application/json")
        try:
            return code, json.loads(raw)
        except json.JSONDecodeError:
            return code, {"raw": raw[:200].decode("utf-8", "replace")}

    def attach(self, photo_id: str, video_path: str) -> tuple[int, dict]:
        code, raw = self._req(f"/v1/photo/{photo_id}/video",
                              json.dumps({"videoPath": video_path}).encode(),
                              "application/json")
        try:
            return code, json.loads(raw)
        except json.JSONDecodeError:
            return code, {"raw": raw[:200].decode("utf-8", "replace")}

    def recognize(self, jpeg: bytes) -> tuple[int, dict, float]:
        boundary = "----photoarsim%d" % random.getrandbits(48)
        pre = (f"--{boundary}\r\n"
               'Content-Disposition: form-data; name="frame"; filename="f.jpg"\r\n'
               "Content-Type: image/jpeg\r\n\r\n").encode()
        post = f"\r\n--{boundary}--\r\n".encode()
        t0 = time.perf_counter()
        code, raw = self._req("/v1/recognize", pre + jpeg + post,
                              f"multipart/form-data; boundary={boundary}")
        wall = (time.perf_counter() - t0) * 1000
        try:
            return code, json.loads(raw), wall
        except json.JSONDecodeError:
            return code, {}, wall


def prepare(work: Path, n_photos: int, vocab: Path, photos_src: Path) -> tuple[Path, list[Path]]:
    """铺一个假 NAS：照片只读挂载，data 可写。"""
    nas = work / "nas"
    (nas / "photos").mkdir(parents=True)
    (nas / "videos").mkdir(parents=True)
    (work / "data").mkdir()

    pool = sorted(photos_src.glob("*.jpg"))
    # 多铺几倍：服务端会按 eval-img < 75 分把一部分照片 422 拒掉（数据集里不少
    # 只有 50 多分），铺够了才能撞到目标库规模。拒绝率本身也是个要报的数。
    #
    # 倍数是 3 而不是 2：`~/photoar-data/clean` 上实测通过率只有 **36%**（3030 次
    # 尝试入库 1093 张，61.8% 被判质量不足、2.1% 其它失败）。按 2 倍铺的那次跑，
    # 池子在到 1500 张之前就被吃干 —— 1500 那一档和第 4 步的单张耗时全都没量到，
    # 而报告里只留下一句「没有合格照片可测」。
    # +30 是给第 4 步（改回 self_score_samples=20 再量单张耗时）留的尾巴。
    want = min(len(pool), int(n_photos * 3) + 30)
    if want < n_photos:
        sys.exit(f"照片不够：要 {n_photos}，{photos_src} 里只有 {len(pool)}")
    # 固定种子：这一轮的数字要能和下一轮比
    random.Random(20260730).shuffle(pool)
    picked = pool[:want]
    for i, p in enumerate(picked):
        shutil.copy2(p, nas / "photos" / f"p{i:05d}.jpg")

    shutil.copy2(vocab, work / "data" / "vocab.npz")

    # 一条素材视频，用来量转码耗时与产物体积。
    #
    # 用 noise 而不是 testsrc：testsrc 是纯色块 + 直线，H.264 压得极狠，转出来
    # 0.3MB —— 那个数拿去算带宽账会低估一个量级。真实手机视频有传感器噪声、
    # 树叶、人脸细节，压缩比更接近噪声源这一端。这里等于取了体积的上界侧。
    video = nas / "videos" / "clip.mp4"
    # 时长刻意取 MAX_DURATION_MS + 4 秒：既超上限从而走一遍截断分支，也不至于
    # 白编几十秒。**不能写死 34**：规格改过一次（15s→30s，2026-07-30），写死的话
    # 下次一改就不再触发截断，而这里不会报错，只是悄悄少测一条路径。
    #
    # 码率同理跟着 MAX_BITRATE 走三倍：源码率必须显著高于目标，否则 x264 在
    # -crf 下直接输出一个比上限小得多的产物，量到的「上界侧体积」是假的。
    src_seconds = T.MAX_DURATION_MS // 1000 + 4
    src_bitrate = f"{int(T.MAX_BITRATE.removesuffix('k')) * 3}k"
    p = sh(["ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=size=1920x1080:rate=30:duration={src_seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={src_seconds}",
            "-vf", "noise=alls=28:allf=t+u",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", src_bitrate,
            "-c:a", "aac", "-shortest", str(video)])
    if p.returncode != 0 or not video.exists():
        sys.exit("造素材视频失败：" + p.stderr[-500:])

    cfg = {
        "bind": "0.0.0.0",
        "port": 8964,
        "roots": {"照片": "/nas/photos", "视频": "/nas/videos"},
        "data_dir": "/data",
        "vocab_path": "/data/vocab.npz",
        "arcoreimg": "arcoreimg",
        "media": {"strategies": ["nas_serve"]},
        # 入库耗时的大头是自匹配采样（每张约 1s × samples/20）。这一轮要的是
        # 「库规模长起来之后识别延迟怎么变」，降到 4 能把入库时间压掉 80%，
        # 而它完全不影响识别路径。单张入库耗时另外用默认 20 单独量。
        "self_score_samples": 4,
        "version": "sim",
    }
    (work / "config").mkdir()
    (work / "config" / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return nas, picked


def build_image() -> None:
    """每次都重建镜像。

    **不能省**：不建的话改完服务端源码跑这个脚本，量到的是上一次镜像里的旧代码，
    而且什么都不报 —— 「修完验证一遍」会得到一份看着合理、其实没验证任何东西的
    报告。Docker 的层缓存让只改 Python 的重建只有几秒。
    """
    log("   docker build（层缓存命中时几秒）…")
    p = sh(["docker", "build", "-t", IMAGE, "-f", str(REPO / "Dockerfile"), str(REPO)])
    if p.returncode != 0:
        sys.exit("docker build 失败：" + (p.stderr or p.stdout)[-2000:])


def run_container(work: Path, token: str, cpus: str, memory: str) -> None:
    sh(["docker", "rm", "-f", NAME])
    p = sh([
        "docker", "run", "-d", "--name", NAME,
        "--cpus", cpus, "--memory", memory,
        # 关掉 swap：QNAP 上容器超内存就是被杀，本地有 swap 会把 OOM 掩盖成「变慢」
        "--memory-swap", memory,
        "-p", f"127.0.0.1:{PORT}:8964",
        "-e", f"PHOTOAR_TOKEN={token}",
        "-e", "LANG=C.UTF-8",
        "-v", f"{work / 'data'}:/data",
        "-v", f"{work / 'config'}:/config:ro",
        "-v", f"{work / 'nas'}:/nas:ro",
        IMAGE,
    ])
    if p.returncode != 0:
        sys.exit("docker run 失败：" + p.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", type=int, default=300, help="入库多少张")
    ap.add_argument("--queries", type=int, default=200, help="测多少次识别")
    ap.add_argument("--cpus", default="3.0")
    ap.add_argument("--memory", default="3g")
    ap.add_argument("--concurrency", type=int, default=4, help="并发那一段开几个线程")
    ap.add_argument("--vocab", default=str(Path.home() / "photoar-data/corpus/vocab.npz"))
    ap.add_argument("--photos-dir", default=str(Path.home() / "photoar-data/clean"))
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    vocab, photos_src = Path(args.vocab), Path(args.photos_dir)
    for need in (vocab, photos_src):
        if not need.exists():
            sys.exit(f"缺少：{need}")

    token = "sim-token-%d" % random.getrandbits(64)
    work = Path(tempfile.mkdtemp(prefix="photoar-sim-"))
    log(f"工作目录 {work}")

    log(f"\n== 0. 铺素材（{args.photos} 张照片 + 1 条 15s 1080p 视频）==")
    nas, picked = prepare(work, args.photos, vocab, photos_src)
    raw_video = nas / "videos" / "clip.mp4"
    log(f"   源视频 {raw_video.stat().st_size / 1048576:.2f} MB")

    log(f"\n== 1. 起容器（--cpus {args.cpus} --memory {args.memory}，无 swap）==")
    build_image()
    run_container(work, token, args.cpus, args.memory)
    client = Client(f"http://127.0.0.1:{PORT}", token)
    for _ in range(60):
        if client.ping():
            break
        time.sleep(1)
    else:
        log(sh(["docker", "logs", NAME]).stdout[-3000:])
        log(sh(["docker", "logs", NAME]).stderr[-3000:])
        sys.exit("容器起不来")
    log("   ping 通了")

    sampler = Sampler(NAME)
    sampler.start()
    report: dict = {"cpus": args.cpus, "memory": args.memory, "photos": args.photos}

    try:
        # 分档：每到一个库规模就量一轮识别。绝对延迟在本机不可信（配额限的是
        # CPU 时间份额，不是单核速度），但**斜率**与 CPU 快慢无关 —— 倒排索引的
        # 候选数怎么随库长，在 N5095 上是同一个形状。这是这次模拟唯一能替真机
        # 回答的问题，所以它是主结果，不是附带。
        marks = [m for m in (50, 100, 200, 300, 500, 1000, 2000) if m < args.photos]
        marks.append(args.photos)
        log(f"\n== 2. 入库到 {args.photos} 张，在 {marks} 处各量一轮识别 ==")

        t0 = time.perf_counter()
        ok: list[tuple[str, Path]] = []
        rejected, failed = [], []
        ingest_ms: list[float] = []
        tried = 0
        curve = []
        next_mark = 0

        for i in range(len(picked)):
            if len(ok) >= args.photos:
                break
            # 两条路径不能混：给服务端的必须是**容器内**路径（roots 白名单按容器
            # 内路径配的，传宿主机路径会被正确地 403），造查询帧读的是宿主机路径。
            src = nas / "photos" / f"p{i:05d}.jpg"
            tried += 1
            t1 = time.perf_counter()
            code, body = client.create(f"/nas/photos/p{i:05d}.jpg", 152, None, f"p{i}")
            dt = (time.perf_counter() - t1) * 1000
            if code in (200, 201):
                ok.append((body.get("photoId"), src))
                ingest_ms.append(dt)
            elif code == 422:
                rejected.append((i, body.get("error"), str(body.get("message"))[:60]))
            else:
                failed.append((i, code, str(body)[:120]))

            if tried % 25 == 0:
                log(f"   试 {tried}  入库 {len(ok)} 拒 {len(rejected)} 失败 "
                    f"{len(failed)}  已用 {time.perf_counter() - t0:.0f}s  "
                    f"峰值内存 {sampler.peak_mem_mb:.0f}MB")

            if next_mark < len(marks) and len(ok) >= marks[next_mark]:
                size = len(ok)
                frames = build_frames(ok, args.queries, seed=4242)
                r = measure_recognize(client, frames, concurrency=1, log_every=0)
                r["index_size"] = size
                curve.append(r)
                log(f"   ▸ 库 {size} 张：P50 {r['p50']}ms  P95 {r['p95']}ms  "
                    f"命中 {r['matched']}/{r['n']}（认对 {r['correct']}，"
                    f"误识别 {r['false_positive']}）")
                next_mark += 1

        wall = time.perf_counter() - t0
        report["ingest"] = {
            "tried": tried,
            "accepted": len(ok), "rejected_422": len(rejected), "failed": len(failed),
            "reject_reasons": _tally(r[1] for r in rejected),
            # 非 422 的失败**必须进 JSON**，不能只打日志。第一次放量跑就吃过这个亏：
            # 3030 次尝试里 65 次这类失败，样本行确实打出来了，但被 stdout 的截断
            # 吃掉，而容器跑完就删了 —— 于是「2.1% 的入库失败」这件事有数字、
            # 没有任何原因，只能重跑一遍。日志会被截断，报告文件不会。
            "failed_codes": _tally(f[1] for f in failed),
            "failed_samples": [list(f) for f in failed[:5]],
            "wall_s": round(wall, 1),
            "per_photo_ms_median": round(statistics.median(ingest_ms), 1) if ingest_ms else None,
            "per_photo_ms_p95": round(pctl(ingest_ms, 95), 1) if ingest_ms else None,
        }
        report["scale_curve"] = curve
        log(f"   试 {tried} 张：入库 {len(ok)}，被 422 拒 {len(rejected)}，"
            f"失败 {len(failed)}，入库总耗时 {wall:.0f}s（含分档测量）")
        log(f"   拒绝原因：{report['ingest']['reject_reasons']}")
        for f in failed[:3]:
            log(f"   失败样本：{f}")
        if not ok:
            sys.exit("一张都没入库，后面没得测")

        log(f"\n== 3. 并发 {args.concurrency} 路识别（库内 {len(ok)} 张）==")
        frames = build_frames(ok, max(40, args.queries), seed=99)
        conc = measure_recognize(client, frames, concurrency=args.concurrency, log_every=0)
        report["recognize_concurrent"] = conc
        log(f"   P50 {conc['p50']}ms  P95 {conc['p95']}ms  max {conc['max']}ms  "
            f"吞吐 {conc['rps']}/s  命中 {conc['matched']}/{conc['n']}")

        log("\n== 4. 单张入库耗时（self_score_samples=20，spec 默认）==")
        d = ingest_default(client, work, nas, picked, tried)
        report["ingest_default_samples"] = d
        log(f"   {d}")

        log("\n== 5. 转码产物体积（web 版带宽账要用）==")
        # 用 attach 而不是再 create 一张：p00000 已经在库里，重复 create 会撞去重。
        t1 = time.perf_counter()
        code, body = client.attach(ok[0][0], "/nas/videos/clip.mp4")
        attach_s = time.perf_counter() - t1
        vid = probe_transcoded(work)
        report["video"] = {
            "source_mb": round(raw_video.stat().st_size / 1048576, 2),
            "transcoded": vid,
            "attach_status": code,
            "attach_s": round(attach_s, 1),
            "attach_body": body,
            # 实测一条只能说明这条素材。真正定带宽账的是转码参数决定的上限，
            # 直接引 transcode.MAX_PLAYABLE_BYTES —— 那也是服务端判断「原片能不能
            # 直接发」用的同一个数。以前这里是手抄的字面量，规格一改就对不上，
            # 而对不上只会让报告里的带宽账静默地错掉。
            "ceiling_mb": round(T.MAX_PLAYABLE_BYTES / 1048576, 2),
            "ceiling_note": (
                f"transcode.py: {T.TARGET_HEIGHT}p / {T.MAX_DURATION_MS // 1000}s / "
                f"maxrate {T.MAX_BITRATE} / aac {T.AUDIO_BITRATE} / crf {T.CRF}"
            ),
            # preset 也要进报告：attach 耗时几乎全是它决定的（同一条源
            # slow 89s / veryfast 18s，实测），不记下来的话下次看这份报告
            # 无法判断 attach_s 是哪个档位量出来的。
            "preset": T.SW_PRESET,
            # 问**容器**而不是本机：转码是在容器里跑的，本机有没有核显跟这份
            # 报告无关。容器没透 /dev/dri 时这里会是 libx264，正好说明产物体积和
            # 耗时是软编量出来的。
            "encoder": container_encoder(),
        }
        log(f"   attach {code}，耗时 {attach_s:.1f}s，{body}")
        log(f"   转码参数决定的**硬上限** {report['video']['ceiling_mb']} MB/条"
            f"（crf {T.CRF} 下实际通常低于上限）")
        for v in vid:
            log(f"   {v['name']}  {v['mb']} MB")

        log("\n== 6. 磁盘占用 ==")
        report["disk"] = disk_usage(work / "data")
        for k, v in report["disk"].items():
            log(f"   {k:14} {v}")
    finally:
        sampler.stop()
        report["peak_mem_mb"] = round(sampler.peak_mem_mb, 1)
        report["peak_cpu_pct"] = round(sampler.peak_cpu_pct, 1)
        report["stat_samples"] = sampler.samples
        insp = sh(["docker", "inspect", "-f", "{{.State.OOMKilled}}|{{.State.ExitCode}}|"
                   "{{.State.Status}}", NAME])
        report["container_state"] = insp.stdout.strip()
        logs = sh(["docker", "logs", "--tail", "40", NAME])
        (work / "server.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")

    log("\n" + "=" * 60)
    log(json.dumps(report, ensure_ascii=False, indent=2))
    est = report.get("recognize_serial", {}).get("p95")
    if est:
        log(f"\nN5095 上的 P95 **估算**：{est}ms × {SINGLE_CORE_RATIO}（单线程性能比，"
            f"非实测）≈ {est * SINGLE_CORE_RATIO:.0f}ms")
    log(f"容器状态（OOMKilled|ExitCode|Status）：{report['container_state']}")
    log(f"峰值内存 {report['peak_mem_mb']}MB / {args.memory}，峰值 CPU "
        f"{report['peak_cpu_pct']}%（配额 {float(args.cpus) * 100:.0f}%）")

    out = REPO / "bench" / "logs" / "sim-qnap.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n结果写入 {out}")

    if args.keep:
        log(f"容器与工作目录保留：{NAME} / {work}")
    else:
        sh(["docker", "rm", "-f", NAME])
        shutil.rmtree(work, ignore_errors=True)
    return 0


def ingest_default(client: Client, work: Path, nas: Path, picked: list[Path],
                   used: int, n: int = 5) -> dict:
    """把 self_score_samples 改回 spec 默认的 20，再入库几张，量真实单张耗时。

    改配置得重启容器（配置只在启动时读一次）。/data 是持久卷，索引不丢，所以
    重启后继续往同一个库里加 —— 量到的就是「库里已有几百张时再加一张要多久」，
    正是一万张规模下用户实际感受到的那个数。
    """
    cfg_path = work / "config" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["self_score_samples"] = 20
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    sh(["docker", "restart", NAME])
    for _ in range(60):
        if client.ping():
            break
        time.sleep(1)
    else:
        return {"error": "重启后起不来"}

    ms, rejected = [], 0
    for i in range(used, len(picked)):
        if len(ms) >= n:
            break
        t0 = time.perf_counter()
        code, _ = client.create(f"/nas/photos/p{i:05d}.jpg", 152, None, f"d{i}")
        dt = (time.perf_counter() - t0) * 1000
        if code in (200, 201):
            ms.append(dt)
        else:
            rejected += 1
    if not ms:
        return {"error": "没有合格照片可测", "rejected": rejected}
    return {
        "n": len(ms), "samples": 20,
        "median_ms": round(statistics.median(ms), 1),
        "min_ms": round(min(ms), 1), "max_ms": round(max(ms), 1),
        "est_10k_hours": round(statistics.median(ms) * 10000 / 3600_000, 1),
    }


def _tally(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out


def pctl(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def make_query(path: Path, seed: int) -> bytes:
    """造一张「手机拍打印照片」的查询帧。

    扰动用 `photoar.synth` —— 与 §14.1 回归测试、Phase 0 全部实测数字同源。
    换一套更容易的输入等于把自己糊过去。长边 640 / q70 是 spec §7 的客户端格式。
    """
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读不出：{path}")
    q, _ = synth.generate(img, count=1, seed=seed)[0]
    h, w = q.shape[:2]
    s = 640 / max(h, w)
    if s < 1.0:
        q = cv2.resize(q, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", q, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        raise RuntimeError("编码失败")
    return buf.tobytes()


def build_frames(ingested: list[tuple[str, Path]], n: int, seed: int) -> list[tuple[bytes, str]]:
    """造 n 张查询帧，只从**真入库了**的照片里取。

    这一条曾经错过：按下标从铺进去的文件里取，而入库循环会跳过被 422 拒掉的，
    于是一半查询帧对应的照片根本不在库里 —— 服务端正确地不命中，命中率却被读成
    「召回只有 40%」。取样错会让所有下游数字都失去意义，所以显式传入库清单。
    """
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        pid, path = ingested[rng.randrange(len(ingested))]
        out.append((make_query(path, seed=rng.randrange(1 << 30)), pid))
    return out


def measure_recognize(client: Client, frames: list[tuple[bytes, str]],
                      concurrency: int, log_every: int) -> dict:
    """打识别接口。

    查询帧由调用方**预先造好**：造一张要跑一遍 warp+glare+blur，在宿主机上也要
    几十毫秒，混在计时里量出来的就不是服务端延迟了。

    `correct` 与 `matched` 分开数：认出来了但认成**另一张**是误识别，和没认出来
    是两类完全不同的问题（§14.1 的误识别率是生死线，召回不是）。
    """
    lat: list[float] = []
    matched = correct = errors = 0
    lock = threading.Lock()
    n = len(frames)
    t0 = time.perf_counter()

    def work(chunk: list[tuple[bytes, str]]) -> None:
        nonlocal matched, correct, errors
        for jpeg, want in chunk:
            code, body, wall = client.recognize(jpeg)
            with lock:
                lat.append(wall)
                if code != 200:
                    errors += 1
                elif body.get("matched"):
                    matched += 1
                    if body.get("photoId") == want:
                        correct += 1
                if log_every and len(lat) % log_every == 0:
                    log(f"   {len(lat)}/{n}  命中 {matched}（对 {correct}）"
                        f"  最近 {wall:.0f}ms")

    if concurrency <= 1:
        work(frames)
    else:
        chunks = [frames[i::concurrency] for i in range(concurrency)]
        ts = [threading.Thread(target=work, args=(c,)) for c in chunks]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    wall = time.perf_counter() - t0

    return {
        "n": len(lat), "matched": matched, "correct": correct, "errors": errors,
        "false_positive": matched - correct, "concurrency": concurrency,
        "p50": round(pctl(lat, 50), 1), "p95": round(pctl(lat, 95), 1),
        "p99": round(pctl(lat, 99), 1), "max": round(max(lat), 1) if lat else 0,
        "wall_s": round(wall, 1), "rps": round(len(lat) / wall, 2) if wall else 0,
    }


def container_encoder() -> str:
    """容器里 auto 会选中哪个编码器。

    在容器里问，不在本机问：转码是容器干的活。开发机上探不探到核显与这份报告
    无关（本机那块还是 NVIDIA 的，`renderD128` 存在但 VAAPI 起不来）。
    """
    p = sh(["docker", "exec", NAME, "python", "-c",
            "from photoar import transcode as T;"
            "print(T.resolve_encoder(T.ENCODER_AUTO))"])
    return p.stdout.strip() if p.returncode == 0 else f"探测失败：{p.stderr.strip()[:200]}"


def probe_transcoded(work: Path) -> list[dict]:
    out = []
    for p in sorted((work / "data").rglob("*.mp4")):
        out.append({"name": str(p.relative_to(work / "data")),
                    "mb": round(p.stat().st_size / 1048576, 2)})
    return out


def disk_usage(data: Path) -> dict:
    def du(p: Path) -> str:
        r = sh(["du", "-sh", str(p)])
        return r.stdout.split()[0] if r.returncode == 0 and r.stdout else "?"

    out = {"data 总计": du(data)}
    for sub in sorted(data.iterdir()):
        if sub.is_dir():
            out[sub.name] = du(sub)
        elif sub.suffix in (".db", ".sqlite", ".sqlite3", ".npz"):
            out[sub.name] = f"{sub.stat().st_size / 1048576:.1f}M"
    return out


if __name__ == "__main__":
    raise SystemExit(main())
