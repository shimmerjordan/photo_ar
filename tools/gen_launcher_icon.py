#!/usr/bin/env python3
"""从 `PixelIcons.kt` 里那张位图生成 launcher 的 adaptive icon。

## 为什么要生成，而不是手画一个

界面里的图标和应用图标**必须是同一张图** —— 用户在桌面上点的那个东西，和他进来之后
在底栏看到的那个东西，长得不一样就是两个品牌。手维护两份的结果是改了一边忘了另一边，
而这件事没有任何自动检查会发现。

所以这个脚本读 `PixelIcons.kt` 里 `Scan` 那张 16×16 的位图（取景框 + 一张照片），
把每一个亮格子写成一个 `<path>` 矩形，输出 `ic_launcher_foreground.xml`。

## 为什么不在构建期跑

生成物**提交进仓库**，这个脚本只在改图时手动跑一次。理由：CI 的安卓构建里没有
Python（`android.yml` 只装 JDK），把 res 的生成挂到 gradle 上等于给构建加一个
新的外部依赖，而这份 XML 一年也改不了一次。

用法：

    python3 tools/gen_launcher_icon.py          # 写文件
    python3 tools/gen_launcher_icon.py --check   # 只校验是否与源图一致（CI 可用）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONS_KT = ROOT / "android/app/src/main/kotlin/app/photoar/standalone/pixel/PixelIcons.kt"
OUT_FG = ROOT / "android/app/src/main/res/drawable/ic_launcher_foreground.xml"
RES = ROOT / "android/app/src/main/res"

#: 旧机型（API 24-25）的兜底图标。
#:
#: **必须有。** `mipmap-anydpi-v26` 里那份 adaptive-icon 只在 API 26+ 生效，而
#: minSdk 是 24 —— 只放 v26 那一份的话，`@mipmap/ic_launcher` 在 24/25 上解析不到
#: 任何东西。AAPT **不会报错**（编译期它看得见那个名字），症状是装到老机器上
#: 桌面没有图标。所以这里连同 PNG 一起生成。
#:
#: 五档是 Android 的标准 dpi 阶梯（mdpi 48 → xxxhdpi 192）。像素画放大用最近邻，
#: 所以先在 16 的整数倍上画，再最近邻缩到目标尺寸 —— 任何插值都会把硬边糊掉。
LEGACY_DPI = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}

#: 桌面图标的底色，与 `PixelPalette.Ground` 同一个值。
BG = "#0B0C10"

#: adaptive icon 的画布是 108×108dp，而**只有中间 72×72 保证可见**（外圈是给系统
#: 做视差和各种形状裁切用的）。所以 16 格图要摆进中间那 72dp：一格 4dp，图占 64dp，
#: 四周再留 4dp 余量 —— 顶到 72 的话圆形裁切会啃掉四角的取景框。
CANVAS = 108.0
CELL = 4.0
GRID = 16
INSET = (CANVAS - CELL * GRID) / 2.0  # = 22.0

#: 前景色。与 `PixelPalette.Amber` 同一个值 —— 桌面图标和界面主色是同一个色。
FG = "#FFC46B"


def read_bitmap(name: str) -> list[str]:
    src = ICONS_KT.read_text("utf-8")
    m = re.search(
        r'val ' + re.escape(name) + r' = PixelBitmap\.of\(\s*"""\n(.*?)\n\s*"""\s*\)',
        src,
        re.S,
    )
    if not m:
        raise SystemExit(f"在 {ICONS_KT} 里找不到 {name} 那张位图")
    rows = [line.strip() for line in m.group(1).split("\n") if line.strip()]
    if len(rows) != GRID or {len(r) for r in rows} != {GRID}:
        raise SystemExit(
            f"{name} 不是 {GRID}×{GRID}：{len(rows)} 行、列宽 {sorted({len(r) for r in rows})}"
        )
    return rows


def to_vector(rows: list[str]) -> str:
    """一格一个 `<path>`。

    为什么不把同一行连续的格子合并成一个矩形：合并之后这份 XML 就不再是"位图的
    逐格转写"，而是一次优化的结果 —— 而 `--check` 要比对的正是"生成物是否等于源图"。
    256 个 path 里实际输出的只有亮着的那几十个，矢量图的解析成本可以忽略。
    """
    parts = []
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch not in "#1":
                continue
            x = INSET + c * CELL
            y = INSET + r * CELL
            parts.append(
                f'    <path android:fillColor="{FG}"\n'
                f'          android:pathData="M{x:g},{y:g}h{CELL:g}v{CELL:g}h-{CELL:g}z" />'
            )
    body = "\n".join(parts)
    return (
        "<!--\n"
        "  自动生成，别手改：`python3 tools/gen_launcher_icon.py`\n"
        "  源图是 PixelIcons.kt 里的 Scan（16×16 取景框 + 一张照片）——\n"
        "  桌面图标与界面里那个图标是同一份源，不会哪天只改了一边。\n"
        "-->\n"
        '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f'    android:width="{CANVAS:g}dp"\n'
        f'    android:height="{CANVAS:g}dp"\n'
        f'    android:viewportWidth="{CANVAS:g}"\n'
        f'    android:viewportHeight="{CANVAS:g}">\n'
        f"{body}\n"
        "</vector>\n"
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    a = ap.parse_args(argv)

    want = to_vector(read_bitmap("Scan"))
    if a.check:
        if not OUT_FG.is_file():
            print(f"缺文件：{OUT_FG}", file=sys.stderr)
            return 1
        if OUT_FG.read_text("utf-8") != want:
            print(
                f"{OUT_FG} 与 PixelIcons.kt 里的 Scan 不一致 —— "
                f"跑一次 `python3 tools/gen_launcher_icon.py`",
                file=sys.stderr,
            )
            return 1
        print("桌面图标与源图一致")
        return 0

    OUT_FG.parent.mkdir(parents=True, exist_ok=True)
    OUT_FG.write_text(want, "utf-8")
    lit = sum(1 for line in read_bitmap("Scan") for ch in line if ch in "#1")
    print(f"已写 {OUT_FG.relative_to(ROOT)}（{lit} 个格子）")
    write_legacy_png(read_bitmap("Scan"))
    return 0


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def write_legacy_png(rows: list[str]) -> None:
    """API 24-25 的兜底 PNG。理由见 [LEGACY_DPI]。"""
    import numpy as np
    import cv2

    fr, fg_, fb = _rgb(FG)
    br, bg_, bb = _rgb(BG)
    # 先按 1 格 1 像素画，再最近邻放大 —— 顺序反了（先放大再画）会在格子边界
    # 出现半像素，而那正是像素画唯一不能有的东西。
    #
    # 图占画布的 16/18：adaptive icon 那边 16 格摆在 108 里、四周留 22（≈4 格），
    # 这里按同样的比例留一格，桌面上两种机型的图标才一样大。
    pad = 1
    n = GRID + pad * 2
    base = np.zeros((n, n, 3), np.uint8)
    base[:, :] = (bb, bg_, br)  # cv2 是 BGR
    for r, line in enumerate(rows):
        for c, ch in enumerate(line):
            if ch in "#1":
                base[r + pad, c + pad] = (fb, fg_, fr)
    for folder, px in LEGACY_DPI.items():
        out_dir = RES / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        big = cv2.resize(base, (px, px), interpolation=cv2.INTER_NEAREST)
        target = out_dir / "ic_launcher.png"
        assert cv2.imwrite(str(target), big), target
        print(f"已写 {target.relative_to(ROOT)}（{px}×{px}）")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
