"""给 e2e_curl.sh 造一张"手机拍打印照片"的查询帧。

用 synth 的同一套扰动（透视 + 光照 + 模糊 + JPEG 重压），这样 curl 打过去的帧
与 Phase 0 全部实测数字同源 —— 出口条件量的是同一件事，不是另换一套更容易的
输入把自己糊过去。

    python bench/e2e_make_query.py <ref.jpg> <out.jpg> [seed]
"""

import sys
from pathlib import Path

import cv2

from photoar import synth

LONG_EDGE = 640  # spec §7：客户端发的帧是长边 640px 的 q70 JPEG
QUALITY = 70


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    ref, out = Path(argv[1]), Path(argv[2])
    seed = int(argv[3]) if len(argv) > 3 else 1

    img = cv2.imread(str(ref), cv2.IMREAD_COLOR)
    if img is None:
        print(f"读不出参考图：{ref}", file=sys.stderr)
        return 1
    query, _ = synth.generate(img, count=1, seed=seed)[0]

    h, w = query.shape[:2]
    scale = LONG_EDGE / max(h, w)
    if scale < 1.0:
        query = cv2.resize(
            query, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA
        )
    if not cv2.imwrite(str(out), query, [cv2.IMWRITE_JPEG_QUALITY, QUALITY]):
        print(f"写不出查询帧：{out}", file=sys.stderr)
        return 1
    print(f"{out} {query.shape[1]}x{query.shape[0]} {out.stat().st_size}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
