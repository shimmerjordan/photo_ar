"""留帧：把识别请求里那一帧原样存盘，供事后离线重放。

## 为什么需要

「对着照片扫了半天没反应」是这个产品最难查的一类问题，因为**唯一的证据在
HTTP 响应发出的那一刻就没了**。`recognize_log` 里留下的 `inliers=6` 只说明
「没匹配上」，说不出到底是：帧糊了、拍太斜了、光反了、客户端把帧编码坏了、
还是人拍的根本不是入过库的那张。这几种原因的修法完全不同，而靠日志区分不了。

留一份帧，问题的性质就变了：从「拿真机反复试、每次都要人举着手机」变成
「在电脑上重放一个文件」—— 可以反复跑、可以改阈值再跑、可以和 `bench/` 里
那套 synth 合成帧并排比，谁的差距在哪一目了然。

## 为什么默认关、且必须能热开关

开着就是每 400ms 往盘上写一个 ~50KB 的文件（扫一分钟约 9MB），而且写的是
用户家里的照片。所以它是「排查时临时开、查完关」的开关。

同时它必须**不用重启**就能开：现场能复现的时候往往只有那一次机会，而重启
服务会顺带打断正在复现的那个人。所以走 `app_config` 热配置（`debug.dump_frames`），
不走环境变量。

## 绝不能弄坏主路径

诊断功能把识别搞挂是不可接受的 —— 那等于为了看清问题反而制造了更大的问题。
所以 [FrameDump.save] 把所有异常都吞掉只记一条日志：盘满、目录被删、权限不对，
识别照旧返回。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

DIR_NAME = "debug_frames"

#: 目录里最多留这么多帧，超了删最旧的。
#:
#: 有上限是因为这个开关**一定会有人忘记关**（开的时候在查问题，查完了注意力
#: 已经跟着结论走了）。200 帧 × 50KB ≈ 10MB，忘一个月也就这么大；而 200 帧
#: 按每 400ms 一帧算是 80 秒连续扫描，比任何一次复现都长。
MAX_FILES = 200


class FrameDump:
    """把帧写进 `<data>/debug_frames/`。

    构造是廉价的（不建目录、不碰盘）：它在每个请求路径上都会被问一次
    "现在开着吗"，而绝大多数时候答案是"没开"。
    """

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir) / DIR_NAME

    @property
    def dir(self) -> Path:
        return self._dir

    def save(
        self,
        jpeg: bytes,
        *,
        matched: bool,
        inliers: int,
        reason: str | None,
        via: str | None,
    ) -> Path | None:
        """存一帧，返回落盘路径；出任何问题返回 None（**不抛**）。

        判定结果编进文件名而不是另写一个 sidecar：排查时第一个动作是
        `ls` 一眼看哪些帧差得离谱，多一个文件只会让目录读起来更费劲。
        """
        if not jpeg:
            return None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / self._name(matched, inliers, reason, via)
            path.write_bytes(jpeg)
            self._trim()
            return path
        except Exception:
            # 记 exception 而不是 warning：能走到这里的都是环境问题（盘满、
            # 权限、目录被人删了），栈是唯一能指出是哪一种的东西。
            log.exception("留帧失败（不影响识别）")
            return None

    def _name(
        self, matched: bool, inliers: int, reason: str | None, via: str | None
    ) -> str:
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        ms = int((now % 1) * 1000)
        verdict = "hit" if matched else "miss"
        tail = _slug(reason) if not matched and reason else ""
        parts = [f"{stamp}.{ms:03d}", _slug(via) or "-", verdict, f"in{inliers}"]
        if tail:
            parts.append(tail)
        return "_".join(parts) + ".jpg"

    def _trim(self) -> None:
        """超过 [MAX_FILES] 就删最旧的几个。

        按文件名排序而不是按 mtime：文件名前缀就是时间戳，字典序即时间序，
        而 mtime 要对每个文件多做一次 stat（这条路每帧都走一遍）。
        """
        files = sorted(p for p in self._dir.glob("*.jpg"))
        excess = len(files) - MAX_FILES
        for p in files[:excess]:
            p.unlink(missing_ok=True)


def _slug(v: str | None) -> str:
    """收窄成文件名安全的字符。

    `via` 是客户端**自己填的** HTTP 头（`X-PhotoAR-Endpoint`），直接拼进路径
    等于让请求方决定往哪写 —— `../../etc/x` 就是路径穿越。这里只放行
    字母数字和连字符，别的一律丢。
    """
    if not v:
        return ""
    keep = [c for c in v[:24] if c.isalnum() or c in "-"]
    return "".join(keep)
