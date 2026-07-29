"""挑几张 arcoreimg 质量分够高的真实照片给 e2e_curl.sh 用。

不能随手抓三张：Oxford 数据集里不少照片是大片天空或过曝的建筑立面，
arcoreimg 只给 50 多分，服务端会照规矩用 422 quality_too_low 拒掉 —— 那是
正确行为，但会把出口条件的其余部分全部连带失败，掩盖真问题。

    python bench/e2e_pick_photos.py <照片目录> <要几张> [最低分] [arcoreimg 路径]

每行输出 `<分数>\t<绝对路径>`。
"""

import sys
from pathlib import Path

from photoar import quality

MIN_SCORE = 85  # 比服务端阈值 75 留一档余量，别踩在边界上


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[1])
    want = int(argv[2])
    min_score = int(argv[3]) if len(argv) > 3 else MIN_SCORE
    arcoreimg = argv[4] if len(argv) > 4 else quality.ARCOREIMG

    # 排序后按固定步长取，避免总是命中数据集开头那批同一地点的连拍
    cands = sorted(p for p in root.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"})
    if not cands:
        print(f"目录里没有 jpg：{root}", file=sys.stderr)
        return 1
    step = max(1, len(cands) // 200)
    found = 0
    for p in cands[::step]:
        try:
            score = quality.eval_img(p, arcoreimg)
        except Exception as exc:  # noqa: BLE001 — 坏图跳过就好
            print(f"跳过 {p.name}：{exc}", file=sys.stderr)
            continue
        if score < min_score:
            continue
        print(f"{score}\t{p.resolve()}", flush=True)
        found += 1
        if found >= want:
            return 0
    print(f"只找到 {found}/{want} 张分数 ≥ {min_score} 的照片", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
