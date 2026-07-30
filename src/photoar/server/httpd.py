"""把 `app.Server` 挂到 stdlib 的 ThreadingHTTPServer 上，以及命令行入口。

为什么是 stdlib：整个服务的难点在识别与路径安全，不在 HTTP。要的功能只有
路由、Bearer、multipart、Range 四样，全部手写不到四百行，换来的是 QNAP 上
零依赖部署（容器里只要 opencv/numpy）。这与本仓库既有的 cc-trans、frps-panel
同一个取舍。

线程模型：`ThreadingHTTPServer` 一连接一线程。识别是 CPU 密集的（ORB +
RANSAC），N5095 只有 4 核，所以真正的并发上限由 `_RECOGNIZE_SLOTS` 限住 ——
不限的话十几个并发识别会把每一个都拖慢到超时，还不如排队。
"""

import argparse
import contextlib
import json
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ..vocab import Vocab
from . import app, integrity
from .config import ServerConfig
from .db import Catalog
from .library import PhotoLibrary

# 同时在跑的识别请求数上限。N5095 4 核，留一核给 IO 与转码。
_RECOGNIZE_SLOTS = 3
_recognize_gate = threading.BoundedSemaphore(_RECOGNIZE_SLOTS)

# 排队等槽位的上限，超了直接 503。
#
# **不能无限期等**，而这一条不显然：客户端的 `RECOGNIZE_TIMEOUT_MS` 是 2 秒，
# 排队超过 2 秒的请求，客户端早已放弃并发下一帧了 —— 服务端却仍会老实地拿到
# 槽位、跑完整个 ORB + RANSAC、再往一条已经关掉的 socket 上写。那一次识别的
# CPU 完全白烧，而它本该给还有人在等的那个请求。
#
# 于是形成闭环：越积压越多请求作废，越多作废越没 CPU 处理新请求。单人扫描
# 撞不到（一次只有一个在途），几台手机同时扫、或网页版几十人同时进来就会 ——
# 表现是「所有人都认不出来」，而每一条日志看着都正常（200、耗时也不长）。
#
# 1 秒是这么定的：明显小于客户端的 2 秒超时，所以 1 秒内抢到槽位的请求还来得及
# 正常返回结果，不浪费；抢不到的直接让客户端丢帧（[ScanController.onRecognizeFailed]
# 对 5xx 就是静默丢帧、400ms 后重来），正是拥塞时想要的行为。
_RECOGNIZE_QUEUE_S = 1.0

# 单个连接上每次读/写的超时。
#
# stdlib 默认是 None（永久阻塞）—— 一条连上就不发数据、或者手机进了电梯直接
# 半开的连接，会占住一个线程直到 TCP keepalive 发现（默认两小时以上）。
# `ThreadingHTTPServer` 又不限线程数，攒几次就是线程泄漏，而服务本身「看起来
# 还活着」。这个口子还经 Cloudflare tunnel 暴露在公网上。
#
# 它是**每次系统调用**的超时而不是整个请求的，所以流式发一条十几 MB 的视频
# （§12 改档后一条上限 16.2MiB）不会被误杀：只要 30 秒内 socket 缓冲区腾出过
# 一次空间就继续。
_SOCKET_TIMEOUT_S = 30


class _Handler(BaseHTTPRequestHandler):
    server_version = "photoar"
    sys_version = ""
    protocol_version = "HTTP/1.1"  # Range/206 与 keep-alive 都需要 1.1
    timeout = _SOCKET_TIMEOUT_S  # 见那个常量：不设就是线程泄漏

    @property
    def _app(self) -> app.Server:
        return self.server.photoar_app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        # 默认实现写 stderr 且格式带一堆引号，容器日志里不好读
        print(f"[photoar] {self.address_string()} {fmt % args}", flush=True)

    def _build_request(self) -> app.Request:
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        return app.Request(
            method=self.command,
            raw_path=self.path,
            headers=headers,
            rfile=self.rfile,
            content_length=length,
            client=self.client_address[0] if self.client_address else "-",
        )

    def _serve(self) -> None:
        req = self._build_request()
        t0 = time.perf_counter()
        gated = req.method == "POST" and req.path == "/v1/recognize"
        held = gated and _recognize_gate.acquire(timeout=_RECOGNIZE_QUEUE_S)
        if gated and not held:
            # 排太久了，客户端多半已经放弃。理由见 _RECOGNIZE_QUEUE_S。
            resp = app.json_response(
                503,
                {"error": "busy", "message": "识别忙，排队超时"},
                **{"Retry-After": "1"},
            )
            self.log_message("POST /v1/recognize -> 503（排队 >%.1fs）", _RECOGNIZE_QUEUE_S)
            self._drain(req)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
                self._write(req, resp)
            return
        try:
            resp = self._app.handle(req)
        except TimeoutError as exc:
            # socket 超时（见 _SOCKET_TIMEOUT_S）。客户端半路走了属于正常现象 ——
            # 打栈会把日志刷满，而且 500 也写不出去了。关连接就是全部处理。
            self.log_message("%s %s 读写超时：%s", req.method, req.path, exc)
            self.close_connection = True
            return
        except Exception as exc:  # noqa: BLE001
            # 任何未预料的异常都要变成 500 而不是断连接：客户端在扫描循环里，
            # 断连和超时对它是同一种表现，看不出服务端出了什么问题。
            import traceback

            traceback.print_exc()
            resp = app.json_response(
                500, {"error": "internal", "message": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            if held:
                _recognize_gate.release()

        # 请求体没读完就回响应，keep-alive 的下一个请求会从残留字节开始解析。
        # 主动读干净剩下的部分。
        self._drain(req)

        try:
            self._write(req, resp)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # 扫描时用户挪开手机，客户端会取消在途请求。属于正常现象，不打栈。
            # TimeoutError 同一类：慢到 30 秒没挪动一个字节（见 _SOCKET_TIMEOUT_S），
            # 对面基本等于已经走了。不接住的话 socketserver 会为每次弱网中断的
            # 视频下载打一整个栈。
            self.close_connection = True
            return
        ms = (time.perf_counter() - t0) * 1000
        self.log_message("%s %s -> %d (%.0fms)", req.method, req.path, resp.status, ms)

    def _drain(self, req: app.Request) -> None:
        """把请求体没被处理器读走的那部分读掉。

        必须按 `req.consumed` 记账，不能按"处理器有没有读过"判断：上传是流式
        写文件的，读完之后 `_body` 仍是 None，若据此再读一遍 content_length，
        连接上早已没有字节可读，会永久阻塞在这里 —— 客户端看到的是上传卡死。
        """
        remaining = req.content_length - req.consumed
        if remaining <= 0:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(1 << 16, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _write(self, req: app.Request, resp: app.Response) -> None:
        length = resp.content_length
        self.send_response(resp.status)
        for key, value in resp.headers.items():
            self.send_header(key, value)
        if resp.status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="photoar"')
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if req.method == "HEAD" or resp.status in (204, 304):
            return
        if resp.file is not None:
            app.send_file(resp, self.wfile)
        elif resp.body:
            self.wfile.write(resp.body)

    do_GET = _serve
    do_HEAD = _serve
    do_POST = _serve
    do_PUT = _serve
    do_DELETE = _serve


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET
    request_queue_size = 64


def make_server(cfg: ServerConfig, srv: app.Server) -> ThreadedHTTPServer:
    httpd = ThreadedHTTPServer((cfg.bind, cfg.port), _Handler)
    httpd.photoar_app = srv  # type: ignore[attr-defined]
    return httpd


# ---- 命令行 ----


def _load(cfg_path: str) -> ServerConfig:
    return ServerConfig.load(cfg_path)


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    srv = app.Server.create(cfg)
    problems = srv.check_consistency()
    if problems:
        print(
            f"[photoar] ⚠️ catalog 与识别库不一致（{len(problems)} 处），"
            f"服务仍会启动：",
            flush=True,
        )
        for line in problems[:20]:
            print(f"[photoar]   - {line}", flush=True)
        if len(problems) > 20:
            print(f"[photoar]   …还有 {len(problems) - 20} 处", flush=True)
    httpd = make_server(cfg, srv)
    print(
        f"[photoar] 监听 {cfg.bind}:{cfg.port}｜照片 {len(srv.library)} 张｜"
        f"白名单根 {len(srv.roots.roots)} 个｜media 策略 {list(cfg.media_strategies)}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[photoar] 收到 Ctrl-C，退出", flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """重建倒排索引。换了词汇树、或 words.bin 疑似损坏时用。"""
    cfg = _load(args.config)
    lib = PhotoLibrary(cfg.library_dir, Vocab.load(cfg.vocab_path))
    t0 = time.perf_counter()
    lib.reindex(rebuild_words=args.rebuild_words)
    print(
        f"[photoar] 重建完成：{len(lib)} 张，"
        f"{(time.perf_counter() - t0):.1f}s"
        + ("（含重新量化词序列）" if args.rebuild_words else ""),
        flush=True,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """全库素材完整性校验（spec §6.1）。只报告，不自动改绑。"""
    cfg = _load(args.config)
    catalog = Catalog(cfg.db_path)
    results = integrity.verify_all(catalog)
    buckets: dict[str, list] = {}
    for r in results:
        buckets.setdefault(r.status, []).append(r)
    for status in (
        integrity.STATUS_OK,
        integrity.STATUS_RESTORED,
        integrity.STATUS_MTIME_ONLY,
        integrity.STATUS_CONTENT_CHANGED,
        integrity.STATUS_MISSING,
    ):
        rows = buckets.get(status, [])
        print(f"[photoar] {status:16s} {len(rows)}", flush=True)
        if status in (integrity.STATUS_CONTENT_CHANGED, integrity.STATUS_MISSING):
            for r in rows:
                extra = (
                    f" 影响照片 {r.stale_photo_ids}" if r.stale_photo_ids else ""
                )
                print(f"[photoar]     {r.nas_path}{extra}", flush=True)
    hashed = sum(1 for r in results if r.hashed)
    print(f"[photoar] 共 {len(results)} 个素材，其中 {hashed} 个做了哈希", flush=True)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load(args.config)
    srv = app.Server.create(cfg)
    problems = srv.check_consistency()
    print(
        json.dumps(
            {
                "photosInCatalog": srv.catalog.count_photos(),
                "photosInLibrary": len(srv.library),
                "problems": problems,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="photoar-server", description="照片 AR 服务端（spec §5）"
    )
    parser.add_argument(
        "-c", "--config", default="config.json", help="配置文件路径"
    )
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="启动 HTTP 服务")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("reindex", help="重建倒排索引")
    p.add_argument(
        "--rebuild-words",
        action="store_true",
        help="连词序列一起重算（换了词汇树时必须加）",
    )
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("verify", help="校验素材完整性")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("check", help="检查 catalog 与识别库是否一致")
    p.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_serve
        args.cmd = "serve"
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"[photoar] 启动失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
