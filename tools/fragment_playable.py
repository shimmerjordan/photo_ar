#!/usr/bin/env python3
"""把 `data/playable/` 里已有的视频无损重封装成分片 MP4（fMP4）。

## 为什么要跑这一次

`transcode.py` 从现在起产出的是 fMP4，理由写在那里：**网页版要靠 MediaSource 播**，
而 MediaSource 只吃分片 MP4。但**已经转好的片子还是老格式**，它们不会自动重转
（`needs_transcode()` 看的是分辨率/时长/体积/faststart，这几项它们都合规）。

所以要手动跑一次。`-c copy` 是**无损重封装**：不重新编码，只改容器的组织方式，
几秒钟一个文件，画质与体积都不变。

## 安全

- 先写临时文件，`ffprobe` 验过再原子替换（`os.replace`）。中途出错不会留下半个文件。
- 原文件备份到 `<name>.mp4.bak`，确认没问题之后自己删。
- 已经是分片的会跳过 —— 重复跑无害。

用法：
    python3 tools/fragment_playable.py <playable 目录> [--apply]
不给 `--apply` 只报告要动哪些文件，不改任何东西。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

FRAG_FLAGS = "+frag_keyframe+empty_moov+default_base_moof"
# 1 秒一片。理由与 transcode.py 里那条一样：只按关键帧切的话首片能到 3.5MB，
# 而首片多大就等于「认出照片之后要等多久才出画」。
FRAG_DURATION_US = "1000000"


def is_fragmented(path: Path) -> bool:
    """头部有没有 `moof`。

    分片 MP4 的布局是 `ftyp | moov | moof | mdat | moof | mdat …`，第一个 moof 紧跟
    在 moov 之后，所以只读前面一小段就够 —— 不必为了判断一个格式去读十几 MB。
    """
    with path.open("rb") as f:
        head = f.read(1 << 20)
    return b"moof" in head


def duration_of(path: Path) -> float | None:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def fragment(path: Path) -> str:
    tmp = path.with_suffix(".frag.tmp.mp4")
    before = duration_of(path)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path),
         "-c", "copy", "-movflags", FRAG_FLAGS, "-frag_duration", FRAG_DURATION_US, str(tmp)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        return f"失败：ffmpeg {proc.stderr.strip()[:120]}"
    after = duration_of(tmp)
    # 时长对不上说明重封装出了岔子。**宁可不换** —— 换上一个坏文件的表现是
    # "扫到照片但视频播一半没了"，比现在这个问题更难查。
    if before and after and abs(before - after) > 0.25:
        tmp.unlink(missing_ok=True)
        return f"失败：时长对不上（{before:.2f}s → {after:.2f}s）"
    if not is_fragmented(tmp):
        tmp.unlink(missing_ok=True)
        return "失败：重封装之后头部仍然找不到 moof"
    shutil.copy2(path, path.with_suffix(".mp4.bak"))
    os.replace(tmp, path)
    return f"好了（{before:.2f}s，备份在 {path.name}.bak）"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    apply = "--apply" in sys.argv
    if not root.is_dir():
        print(f"不是目录：{root}")
        return 1
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("需要 ffmpeg 与 ffprobe")
        return 1

    todo = []
    for f in sorted(root.glob("*.mp4")):
        if f.name.endswith(".bak.mp4"):
            continue
        todo.append((f, is_fragmented(f)))

    for f, frag in todo:
        mark = "已是分片，跳过" if frag else ("要处理" if not apply else fragment(f))
        print(f"  {f.name}  {f.stat().st_size:>10} B  {mark}")
    n = sum(1 for _, frag in todo if not frag)
    print(f"共 {len(todo)} 个，{n} 个需要处理" + ("" if apply else "（加 --apply 才真的改）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
