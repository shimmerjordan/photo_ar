#!/usr/bin/env python3
"""把融合像素字体子集化成 `public/art/pixel.woff2`。

## 为什么要装一个点阵中文字体

改造前这份代码里有一条注释明确写着"不塞点阵字体文件"，理由是"能用的点阵中文字体几乎
没有，而点阵中文在小字号下糊成一团"。**前半句在 2026 年已经不成立**（融合像素字体覆盖
全中日韩、OFL 开源），后半句在手机上也不成立 —— 那句话默认的是 96dpi 的桌面屏，而这个
界面只在手机上用，DPR 通常是 3，一个 12px 的字面落到 36 个物理像素上，边是硬的。

而它是"像素风"最大的单一杠杆：木头框配系统等宽字，看起来像给终端界面套了个皮；配点阵
字，整屏才是一件东西。

## 为什么必须子集化

整份 zh_hans 是 659KB。这个页面已经要下 12MB 的 wasm，再加 659KB 不是不能忍，但没必要 ——
子集到常用字之后只剩零头，而首屏就要用到它（登录门），压在关键路径上的每一 KB 都要算。

## 字符集怎么定

三部分并集，缺一不可：

1. **GB2312 全集**（6763 汉字）—— 覆盖动态内容：照片名、用户名、服务端错误文案。
   这些字符编译期不知道，只能按"常用字"兜。
2. **本仓库源码里出现的每一个字符** —— 界面写死的文案必须一个不漏。有些字（比如"擦除
   掩码"的"掩"）在 GB2312 里，有些生僻符号不在，靠这一条补。
3. ASCII + 常用标点与全角符号。

漏字的表现不是报错，是**同一屏出现两种字形** —— 缺的那个字回退到系统字体，在一行点阵字
里格外扎眼。所以宁可多带一点。

用法：
    python3 tools/make-font.py <融合像素字体-12px-monospaced-otf.woff2 的 zip 路径>

zip 从 https://github.com/TakWolf/fusion-pixel-font/releases 拿。不自动下载：
这个仓库要能离线构建，而产物（public/art/pixel.woff2）是提交进去的。
"""
import codecs
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public" / "art" / "pixel.woff2"
MEMBER = "fusion-pixel-12px-monospaced-zh_hans.otf.woff2"
LICENSE_MEMBER = "OFL.txt"


def gb2312_chars() -> set[str]:
    """GB2312 的全部汉字与符号。

    直接遍历区位码而不是找一份现成的字表：字表会过期、会有编码问题，而 codecs 就在标准库里。
    """
    out = set()
    dec = codecs.getdecoder("gb2312")
    for hi in range(0xA1, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                ch, _ = dec(bytes([hi, lo]))
            except UnicodeDecodeError:
                continue
            if ch and ch != "�":
                out.add(ch)
    return out


def source_chars() -> set[str]:
    """本仓库前端源码里出现过的每一个字符。"""
    out = set()
    for pat in ("public/**/*.js", "public/**/*.html", "public/**/*.svg", "server/**/*.js"):
        for f in ROOT.glob(pat):
            if "vendor" in f.parts:  # opencv.js 是 13MB 的机器生成代码，没有界面文案
                continue
            out |= set(f.read_text(encoding="utf-8", errors="ignore"))
    return out


EXTRA = set(
    "".join(
        [
            "".join(chr(c) for c in range(0x20, 0x7F)),  # ASCII 可打印
            "　、。〃々〈〉《》「」『』【】〔〕〖〗〝〞︰︳﹍﹏",
            "！？，．：；‘’“”…—–·～￥％＋－＝／＼｜（）［］｛｝＜＞",
            "０１２３４５６７８９",
            "←→↑↓■□▲△●○★☆✓✗⚠",  # 界面里可能用到的记号
            "   ",  # 各种空格：数字对齐要用
        ]
    )
)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    zip_path = Path(sys.argv[1])
    if not zip_path.exists():
        print(f"找不到 {zip_path}")
        return 1

    try:
        from fontTools import subset  # noqa: F401
        from fontTools.ttLib import TTFont
    except ImportError:
        print("需要 fonttools 与 brotli：pip install 'fonttools[woff]' brotli")
        return 1

    src = ROOT / "public" / "art" / "_pixel-full.woff2"
    src.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if MEMBER not in names:
            print(f"zip 里没有 {MEMBER}，有的是：{[n for n in names if n.endswith('.woff2')]}")
            return 1
        src.write_bytes(z.read(MEMBER))
        if LICENSE_MEMBER in names:
            (ROOT / "public" / "art" / "pixel-font-OFL.txt").write_bytes(z.read(LICENSE_MEMBER))

    wanted = gb2312_chars() | source_chars() | EXTRA
    wanted = {c for c in wanted if c.isprintable() or c in " \n"}
    wanted.discard("\n")

    font = TTFont(src)
    have = set()
    for table in font["cmap"].tables:
        have |= {chr(cp) for cp in table.cmap}
    font.close()
    missing = sorted(c for c in wanted if c not in have)
    covered = wanted & have

    opts = subset.Options()
    opts.flavor = "woff2"
    opts.desubroutinize = True
    opts.layout_features = []          # 点阵字体没有连字/字距调整可言
    opts.drop_tables += ["FFTM"]
    opts.name_IDs = ["*"]              # 保留字体名与授权字段：OFL 要求署名
    opts.name_legacy = True
    opts.notdef_outline = True         # 保留 .notdef：缺字时给一个可见的方框，比静默回退好定位
    f = subset.load_font(str(src), opts)
    sub = subset.Subsetter(options=opts)
    sub.populate(unicodes=[ord(c) for c in covered])
    sub.subset(f)
    subset.save_font(f, str(OUT), opts)
    f.close()
    src.unlink()

    # 把实际收进去的码位写成区间表。
    #
    # 为的是让 node 那边的测试能验"界面里写死的字一个都没漏" —— 直接解析 woff2 要
    # 实现 woff2 的表变换（不是普通 brotli），而那件事跟这个项目一点关系都没有。
    # 区间表由**真正做子集的这段代码**产出，所以它不会与产物脱节。
    ranges = []
    for cp in sorted(ord(c) for c in covered):
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1][1] = cp
        else:
            ranges.append([cp, cp])
    # 落在 tools/ 而不是 public/art/：这 50KB 只有 node 测试读，浏览器永远不会请求它，
    # 而 public/ 里的每一个字节都会被部署出去。
    (Path(__file__).resolve().parent / "pixel-coverage.json").write_text(
        json.dumps({"font": OUT.name, "count": len(covered), "ranges": ranges}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    size = OUT.stat().st_size
    print(f"字符集 {len(wanted)} 个，字体里有 {len(covered)} 个，缺 {len(missing)} 个")
    if missing:
        head = "".join(missing[:40])
        print(f"  缺的（前 40）：{head}")
    print(f"{OUT.relative_to(ROOT)}  {size} B（{size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
