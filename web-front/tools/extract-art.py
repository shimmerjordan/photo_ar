#!/usr/bin/env python3
"""从星露谷物语的素材包里切出这个界面要用的那十几张图，输出到 `public/art/`。

## 为什么是一个脚本而不是手工切图

切出来的每一张都带着一组**必须记住的数字**：九宫格的切片宽度、精灵的原生倍率、
物件在图集里的索引。手工切图之后这些数字只活在切图人的脑子里，下次要改一张就得
重新量一遍。所以切图规则写在这儿，并且顺带产出 `manifest.json` 给 CSS 和测试用 ——
`--frame-slice` 那个数字错一格，边框就会在圆角处错位，而那是很难一眼看出来的。

## 三条素材本身的坑

1. **原生倍率不一样**。`DialogBoxGreen` / 木箭头 / 星星是按 4× 画的（每个"美术像素"
   占 4×4 个真实像素），而 `Cursors` 里的小图标、`Craftables` 里的物件是 1× 的。
   混着用会得到两种粗细的像素，一眼就露馅。所以 4× 的一律先除回 1×，再统一放大。
2. **`letterBG` 右下角挂着一小块碎片**（第 180 行往下只剩 24 个不透明像素，是另一个
   九宫格的角）。整张拿去做背景会在右下角冒出一个方块。
3. **Junimo 在图里是灰白的**，游戏是在代码里染色的。直接用会得到一只白团子。

## 版权

星露谷物语的美术资源版权属于 ConcernedApe。这里是私人场合（一场婚礼）自用，产出物
不随仓库公开分发；`public/art/` 里的 PNG 不要外传。字体是另一回事 —— 融合像素字体是
OFL，随包带了授权全文。

用法：
    python3 tools/extract-art.py [素材包根目录]
默认 ~/Downloads/proj_sources/Stardew valley
"""
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public" / "art"
DEFAULT_SRC = Path.home() / "Downloads" / "proj_sources" / "Stardew valley"

# ── 九宫格木框：整套界面的骨架 ────────────────────────────────────────────
#
# `DialogBoxGreen` 去掉透明边距之后是 160×160，按 4× 画的，也就是 40×40 个美术像素。
# 边框 6 格厚，圆角在 10 格之内收完 —— 所以切片取 10 格。
#
# 关键性质（值得记下来，否则会以为需要 image-rendering: pixelated）：
# **每条边的中段，每一列都完全相同**。于是 border-image 默认的 stretch 拉伸不会产生
# 任何插值痕迹，只要四个角是 1:1 显示的就够。
FRAME_SRC = "LooseSprites/DialogBoxGreen..png"
FRAME_CROP = (16, 16, 176, 176)   # 去掉透明边距
FRAME_ART = 4                      # 原生倍率
FRAME_CELLS = 40                   # 美术像素边长
FRAME_SLICE_CELLS = 10             # 九宫格切片（格）

# 原图六层环的配色，从外到内
WOOD = {
    "outer": (0x5D, 0x1A, 0x2F),
    "dark": (0x84, 0x33, 0x16),
    "mid": (0x9E, 0x47, 0x1F),
    "wood": (0xBC, 0x67, 0x00),
    "bevel": (0xC6, 0x89, 0x63),
    "face": (0xFF, 0xC6, 0x99),
}

# 夜色皮肤：同一副几何、把木头压暗、内胆换成夜空的靛蓝。
#
# 为什么不直接用一层半透明黑盖上去：那会把六层环压成一片糊，而这套边框全靠环与环之间
# 的明度差读出"厚度"。逐色重映射能保住层次。
NIGHT = {
    "outer": (0x1A, 0x0B, 0x14),
    "dark": (0x33, 0x1B, 0x14),
    "mid": (0x4A, 0x2C, 0x1C),
    "wood": (0x6B, 0x45, 0x22),
    "bevel": (0x5A, 0x4A, 0x50),
    "face": (0x1B, 0x1A, 0x2E),
}

# 金色皮肤：选中/焦点。游戏里高亮就是把木头往黄里推。
GOLD = {
    "outer": (0x5D, 0x2A, 0x00),
    "dark": (0xA0, 0x5D, 0x00),
    "mid": (0xC9, 0x84, 0x00),
    "wood": (0xFF, 0xC4, 0x2B),
    "bevel": (0xFF, 0xE3, 0x9B),
    "face": (0xFF, 0xEC, 0xC4),
}

# 危险皮肤：删除类动作。红木。
RED = {
    "outer": (0x3A, 0x0A, 0x12),
    "dark": (0x6E, 0x16, 0x1C),
    "mid": (0x94, 0x22, 0x24),
    "wood": (0xC8, 0x36, 0x30),
    "bevel": (0xE0, 0x84, 0x74),
    "face": (0xFF, 0xD6, 0xC8),
}


def load(src: Path, rel: str) -> Image.Image:
    p = src / rel
    if not p.exists():
        raise SystemExit(f"素材缺失：{p}")
    return Image.open(p).convert("RGBA")


def unscale(im: Image.Image, n: int) -> Image.Image:
    """把按 n× 画的精灵除回 1×。除不尽或者不是真的 n× 就直接报错 —— 静默缩放会糊。"""
    if im.width % n or im.height % n:
        raise SystemExit(f"{im.size} 不能被 {n} 整除")
    small = im.resize((im.width // n, im.height // n), Image.NEAREST)
    if small.resize(im.size, Image.NEAREST).tobytes() != im.tobytes():
        raise SystemExit(f"这张图不是 {n}× 画的，除回去会丢像素")
    return small


def recolor(im: Image.Image, mapping: dict[tuple, tuple]) -> Image.Image:
    out = im.copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            hit = mapping.get((r, g, b))
            if hit:
                px[x, y] = (*hit, a)
    return out


def frames(src: Path) -> dict:
    """四套皮肤 × 两个尺寸的九宫格木框。

    两个尺寸不是"大图缩小"，而是同一副美术按 1 格 = 2px 和 1 格 = 1px 两种落地：
    面板用大的（边框读得出木纹层次），按钮/输入框用小的（44px 高的按钮塞不下 20px 边框）。
    """
    base = unscale(load(src, FRAME_SRC).crop(FRAME_CROP), FRAME_ART)
    assert base.size == (FRAME_CELLS, FRAME_CELLS), base.size
    made = {}
    # 每套皮肤只出**用得上**的尺寸。金色和红色只出现在按钮、提醒框、toast 上，
    # 都是小号；给它们也生成一份大号等于往仓库里塞两张没人引用的图，而
    # art.test.js 有一条专门盯着这个。
    skins = (
        ("frame", WOOD, (2, 1)),
        ("frame-night", NIGHT, (2, 1)),
        ("frame-gold", GOLD, (1,)),
        ("frame-red", RED, (1,)),
    )
    for name, skin, scales in skins:
        mapping = {WOOD[k]: skin[k] for k in WOOD}
        art = recolor(base, mapping)
        for scale in scales:
            im = art.resize((FRAME_CELLS * scale, FRAME_CELLS * scale), Image.NEAREST)
            f = f"{name}{'' if scale == 2 else '-s'}.png"
            im.save(OUT / f)
            made[f] = {"slice": FRAME_SLICE_CELLS * scale, "size": im.size, "face": "#%02X%02X%02X" % skin["face"]}
    return made


def tint(im: Image.Image, rgb: tuple) -> Image.Image:
    """按亮度把灰度精灵染成一个色相。Junimo 在图集里是灰白的，游戏在代码里染色。"""
    out = im.copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            lum = (r * 299 + g * 587 + b * 114) / 255000
            px[x, y] = (
                min(255, round(rgb[0] * lum)),
                min(255, round(rgb[1] * lum)),
                min(255, round(rgb[2] * lum)),
                a,
            )
    return out


def sprites(src: Path) -> dict:
    made = {}

    def save(name: str, im: Image.Image, scale: int = 1, colors: int = 0, **meta):
        if scale != 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        if colors:
            # 存成调色板 PNG。这套图全是有限色的像素画，32 位真彩存它纯属浪费 ——
            # 而 quantize() 之后如果再 convert("RGBA")，等于量化了个寂寞（体积一点没掉）。
            im = im.convert("RGB").quantize(colors=colors, method=Image.MEDIANCUT)
        im.save(OUT / name, optimize=True)
        made[name] = {"size": im.size, **meta}

    cursors = load(src, "LooseSprites/Cursors..png")

    # 木箭头。原图是朝下的那一只（4× 画的），朝上那只是同一副翻转，所以只留一张，
    # 方向交给 CSS transform —— 四张图变一张。
    save("arrow.png", unscale(cursors.crop((76, 72, 116, 116)), 4), scale=2)
    # 星星：识别成功。
    save("star.png", unscale(cursors.crop((192, 128, 256, 192)), 4), scale=2)

    # Junimo 走路四帧。染成经典的绿。空态与加载态的吉祥物 —— 一只在原地蹦的小家伙，
    # 比一句「暂无数据」有用得多。
    ju = load(src, "Characters/Junimo..png")
    strip = Image.new("RGBA", (16 * 4, 16))
    for i in range(4):
        strip.alpha_composite(ju.crop((i * 16, 0, i * 16 + 16, 16)), (i * 16, 0))
    save("junimo.png", tint(strip, (0x7E, 0xE0, 0x5C)), scale=3, frames=4, frame=48)

    # 物件图。Craftables 是 16×32 一格、8 列。索引是游戏自己的编号。
    craft = load(src, "TileSheets/Craftables..png")

    def craftable(idx: int) -> Image.Image:
        c, r = idx % 8, idx // 8
        return craft.crop((c * 16, r * 32, c * 16 + 16, r * 32 + 32))

    save("scarecrow.png", craftable(8), scale=3)    # 稻草人：空空的照片库
    save("tv.png", craftable(21), scale=3)          # 电视：素材/视频
    save("chest.png", craftable(130), scale=3)      # 箱子：本机缓存

    # 对话气泡表情：警告 / 出错 / 不确定。
    #
    # **字体里没有 ⚠ ✓ ✗**（子集化时报了缺字），所以这三个记号只能是图 —— 写进文案里
    # 会回退到系统 emoji 字体，在一屏点阵字里像贴了张贴纸。
    # 图集每行是同一个表情的四帧放大动画，最后一列（x=48）才是长足的那一帧。
    emotes = load(src, "TileSheets/emotes..png")
    for name, y in (("bang", 64), ("cross", 144), ("query", 32)):
        save(f"{name}.png", emotes.crop((48, y, 64, y + 16)), scale=3)

    # 信纸：登录门。第 180 行往下是另一个九宫格的碎片，必须切掉。
    #
    # 量化到 24 色：原图是带噪点的羊皮纸，PNG 压不动（53KB，比其它所有图加起来还大），
    # 而这套界面本来就是有限调色板的像素画 —— 减色之后看不出差别，体积掉到零头。
    letter = load(src, "LooseSprites/letterBG..png").crop((0, 0, 320, 180))
    save("letter.png", letter, colors=24, slice=16)

    # 夜景：登录门的整屏底。
    #
    # 原图自带一圈木框（它在游戏里是挂在墙上的一幅画）。这里要当背景铺满，木框会被裁得
    # 七零八落，所以切掉，只留画心。
    #
    # 也不做平铺底纹：图里有一颗很大的星和一道山脊，平铺会露出接缝与重复。
    save("nightsky.png", load(src, "LooseSprites/nightbg..png").crop((8, 8, 120, 184)), scale=2, colors=32)
    return made


def contact_sheet(made: dict) -> None:
    """把切出来的每一张拼成一张核对图。切图脚本最容易错的是坐标，而坐标错了不报错。

    落在 `tools/` 而不是 `public/art/`：**public/ 里的每一个字节都会被部署出去**，
    而这张图只给人看一眼、确认坐标没错。
    """
    from PIL import ImageDraw

    items = sorted(made.items())
    cols = 6
    cell = 120
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell, rows * (cell + 14)), (24, 22, 34, 255))
    d = ImageDraw.Draw(sheet)
    for i, (name, meta) in enumerate(items):
        im = Image.open(OUT / name).convert("RGBA")
        if im.width > cell or im.height > cell:
            k = min(cell / im.width, cell / im.height)
            im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.NEAREST)
        x, y = (i % cols) * cell, (i // cols) * (cell + 14)
        sheet.alpha_composite(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        d.text((x + 2, y + cell + 1), f"{name} {meta['size'][0]}×{meta['size'][1]}", fill=(255, 210, 150, 255))
    sheet.convert("RGB").save(Path(__file__).resolve().parent / "art-contact.png")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src.exists():
        print(f"找不到素材包：{src}")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    made = {}
    made |= frames(src)
    made |= sprites(src)
    (OUT / "manifest.json").write_text(
        json.dumps({k: v for k, v in sorted(made.items())}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    contact_sheet(made)
    total = sum((OUT / n).stat().st_size for n in made)
    for n, m in sorted(made.items()):
        print(f"  {n:20s} {m['size'][0]:>4}×{m['size'][1]:<4} {(OUT / n).stat().st_size:>7} B")
    print(f"共 {len(made)} 张，{total} B（{total / 1024:.1f} KB）→ {OUT.relative_to(ROOT)}")
    print("核对图：tools/art-contact.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
