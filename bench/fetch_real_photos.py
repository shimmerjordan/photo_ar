"""为里程碑 0d 取真实照片：Wikimedia Commons。

为什么是 Commons 而不是爬任意网站：

1. 许可清楚 —— Commons 只收自由许可（CC / 公有领域），不必猜版权。
2. 有正规 API，可以礼貌限速，不需要抓 HTML。
3. **分类结构天然提供相似组** —— 同一个地标/主题下有大量不同角度、不同
   光照、不同年份拍的照片。这正是最终审查指出合成语料测不到的那个性质：
   合成图是互相独立的随机纹理，而 Phase 0 要回答的是「上万张**高度自相似**
   的照片能否区分」。同主题的不同照片就是那个难例。

注意语义：同一地标的两张不同照片**应当被区分开**（它们是两张不同的实体
照片，各自关联不同的视频）。所以组内相似是难度来源，不是"应当合并"。

产物：<out>/photos/*.jpg 以及 <out>/groups.json（分类 -> 文件名列表），
后者让 0d 能分别统计组内混淆与跨组混淆。

只读外部 API，不写任何仓库内路径。
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia 的 UA 政策要求可辨识的 User-Agent。这里用描述性标识而不放个人
# 邮箱（把用户邮箱发给第三方不该由我替他决定），并用更保守的速率来补偿。
UA = ("photo-ar-phase0/0.1 (private offline image-retrieval benchmark; "
      "low-rate, cached, non-redistributive)")
MIN_INTERVAL = 0.8   # 秒/次下载。实测 0.1s 会被 429
MAX_RETRIES = 4

# 选主题时的取舍：优先"照片多、扫描件/地图/图表少"的具体地点与物件分类。
# 泛分类（如 Category:Eiffel Tower）混入大量书页扫描与工程图，所以下面的
# 过滤器还会按 MIME、尺寸和标题关键词再筛一遍。
CATEGORIES = [
    "Category:Tower Bridge", "Category:Colosseum", "Category:Neuschwanstein Castle",
    "Category:Golden Gate Bridge", "Category:Taj Mahal", "Category:Sagrada Família",
    "Category:Brandenburg Gate", "Category:Charles Bridge", "Category:Mount Fuji",
    "Category:Machu Picchu", "Category:Sydney Opera House", "Category:Chichén Itzá",
    "Category:St. Basil's Cathedral", "Category:Hagia Sophia", "Category:Angkor Wat",
    "Category:Petra", "Category:Stonehenge", "Category:Christ the Redeemer",
    "Category:Himeji Castle", "Category:Mont Saint-Michel", "Category:Alhambra",
    "Category:Pont du Gard", "Category:Rialto Bridge", "Category:Trevi Fountain",
    "Category:Duomo di Milano", "Category:Kinkaku-ji", "Category:Palace of Versailles",
    "Category:Schönbrunn Palace", "Category:Guggenheim Museum Bilbao",
    "Category:Chrysler Building", "Category:Flatiron Building", "Category:Space Needle",
    "Category:Atomium", "Category:Cologne Cathedral", "Category:Leaning Tower of Pisa",
    "Category:Château de Chambord", "Category:Bran Castle", "Category:Predjama Castle",
    "Category:Peles Castle", "Category:Hallstatt",
]

# 标题里出现这些词的基本不是"拍出来的照片"，排除以免污染语料
BAD_TITLE = re.compile(
    r"(scan|page \d|plan|map|karte|diagram|drawing|engraving|lithograph|"
    r"stamp|coin|logo|icon|svg|chart|graph|panorama|360|stereo)",
    re.IGNORECASE,
)


def api(params):
    params = dict(params, format="json", formatversion="2")
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def list_photos(category, want, min_width, thumb_width):
    """返回 [(title, thumburl)]，已按 MIME/尺寸/标题过滤。"""
    out, cont = [], None
    while len(out) < want:
        p = {
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": category,
            "gcmtype": "file",
            "gcmlimit": "50",
            "prop": "imageinfo",
            "iiprop": "url|size|mime",
            "iiurlwidth": str(thumb_width),
        }
        if cont:
            p["gcmcontinue"] = cont
        try:
            d = api(p)
        except Exception as e:
            print(f"  ! {category} 查询失败: {e}", flush=True)
            break

        for page in d.get("query", {}).get("pages", []):
            title = page.get("title", "")
            ii = (page.get("imageinfo") or [{}])[0]
            if ii.get("mime") != "image/jpeg":
                continue
            if ii.get("width", 0) < min_width:
                continue
            if BAD_TITLE.search(title):
                continue
            thumb = ii.get("thumburl")
            if not thumb:
                continue
            out.append((title, thumb))
            if len(out) >= want:
                break

        cont = d.get("continue", {}).get("gcmcontinue")
        if not cont:
            break
        time.sleep(0.2)  # 对 API 礼貌一点
    return out[:want]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-category", type=int, default=25)
    ap.add_argument("--min-width", type=int, default=1200,
                    help="原图最小宽度，滤掉小图")
    ap.add_argument("--thumb-width", type=int, default=1600,
                    help="下载的缩放宽度；1600 已远超 extract() 的 640 归一化")
    ap.add_argument("--max-categories", type=int, default=0,
                    help="只取前 N 个分类，0 = 全部（用于小规模试跑）")
    args = ap.parse_args()

    cats = CATEGORIES[: args.max_categories] if args.max_categories else CATEGORIES

    out = Path(args.out)
    photos = out / "photos"
    photos.mkdir(parents=True, exist_ok=True)

    groups, total, bytes_total = {}, 0, 0
    for ci, cat in enumerate(cats, 1):
        slug = re.sub(r"[^a-z0-9]+", "-", cat.replace("Category:", "").lower()).strip("-")
        items = list_photos(cat, args.per_category, args.min_width, args.thumb_width)
        saved = []
        for i, (title, url) in enumerate(items):
            dst = photos / f"{slug}_{i:03d}.jpg"
            if dst.exists():
                saved.append(dst.name)
                continue
            data = None
            for attempt in range(MAX_RETRIES):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=90) as r:
                        data = r.read()
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        # 指数退避。429 是对方在明确要求我们慢下来，必须听。
                        wait = MIN_INTERVAL * (4 ** attempt)
                        print(f"  · 429，等 {wait:.1f}s 重试 ({attempt + 1}/{MAX_RETRIES})",
                              flush=True)
                        time.sleep(wait)
                        continue
                    print(f"  ! 下载失败 {title}: HTTP {e.code}", flush=True)
                    break
                except Exception as e:
                    print(f"  ! 下载失败 {title}: {e}", flush=True)
                    break
            if data is None:
                continue
            if len(data) < 20_000:      # 太小的多半是占位图
                continue
            dst.write_bytes(data)
            saved.append(dst.name)
            bytes_total += len(data)
            time.sleep(MIN_INTERVAL)
        groups[slug] = {"category": cat, "files": saved}
        total += len(saved)
        print(f"[{ci}/{len(cats)}] {slug}: {len(saved)} 张"
              f"（累计 {total} 张 / {bytes_total / 1e6:.0f} MB）", flush=True)

    (out / "groups.json").write_text(
        json.dumps({"groups": groups, "total": total}, ensure_ascii=False, indent=2)
    )
    print(f"\n完成：{total} 张，{bytes_total / 1e6:.0f} MB，"
          f"{len(groups)} 个相似组 -> {out}", flush=True)
    print(f"组大小分布: {sorted(len(g['files']) for g in groups.values())}", flush=True)
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
