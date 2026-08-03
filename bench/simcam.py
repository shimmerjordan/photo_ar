"""模拟真机扫描：量一张参考图在**真实取景条件**下还剩多少余量。

## 为什么不能用 `e2e_make_query.py`

那个脚本（以及它背后的 `synth`）有两个与真机不符的地方，两个都是往**乐观**
的方向偏：

1. **照片永远铺满整帧**。真机举着手机扫一张 300mm 的照片，照片在画面里只占
   一部分，周围是桌面、键盘、手。而入库是按长边 640px 提特征的，
   `features.py` 自己写了「ORB 不具备尺度不变性，两侧分辨率不一致时召回率会
   大幅下降」—— 这条硬约束只在照片铺满查询帧时才成立。
2. **退化加在参考图分辨率上，再整体缩到 640**。1600px 上 sigma=1.5 的高斯模糊，
   INTER_AREA 缩到 640 之后基本没了。真机的模糊/抖动发生在**传感器**上，
   是缩放之后才定型的。

结果是 synth 帧能打到 inliers 54~139，而同一张图真机拍出来只有 40 上下。
拿 synth 的数字当验收基线，就会在真机上撞墙 —— 这个脚本存在的意义就是把这
道墙提前搬到本机来撞。

## 它怎么测

按真机的成像顺序搭一遍：

    参考图 --透视/光照扰动--> 缩到目标占比 --贴进场景--> 传感器分辨率整帧
          --模糊+反光--> 缩到长边 640 --> JPEG q70

`--fill` 是**照片宽度占画面宽度的比例**，也就是刚才那条主导变量。扫一遍
0.4~1.0，就能看出这张图在什么取景距离下还认得出来。

默认完全离线（直接调 `photoar.features` + `photoar.verify` 算 inliers，
不需要服务端、不改任何库状态）；给了 `--post` 才额外走一遍真实 HTTP 路径，
用来确认服务端的判定与离线算的一致。

    # 只看余量曲线
    python bench/simcam.py local/photos/demo-a.jpg

    # 用真实拍摄的桌面当背景杂物，并同时打给服务端
    python bench/simcam.py local/photos/demo-a.jpg --scene /tmp/desk.jpg \\
        --post http://127.0.0.1:8964 --token "$TOKEN"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from photoar import backend, features, refprep, synth, verify  # noqa: E402

#: 客户端发帧的规格，与 `Frames.kt` 的 `LONG_EDGE`/`JPEG_QUALITY` 保持一致。
#: 640 是 spec §7 的原始规定，已在真机上被证伪（那一档一档都不全过门槛），
#: 客户端与这里都改到了 1280 —— 见 `Frames.LONG_EDGE` 的 KDoc 那张表。改这个
#: 常量必须同时改 `Frames.kt`，否则 bench 量的和真机发的不是一回事。
FRAME_LONG_EDGE = 1280
FRAME_QUALITY = 70

#: 合成时的"传感器"分辨率。取 1600 是因为退化必须加在缩放**之前**才像真机，
#: 而这个值又要明显高于 640，否则模糊会被后面的 INTER_AREA 吃掉。
SENSOR_LONG_EDGE = 1600

#: 画面宽高比。ARCore 给的 CPU 帧是 4:3（`FrameGrabber` 不裁不转）。
FRAME_ASPECT = 4 / 3

DEFAULT_FILLS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4)


def _scene(scene: Path | None, w: int, h: int) -> np.ndarray:
    """铺一张背景。

    没给 `--scene` 时用中性灰而不是纯黑：纯黑不产生任何角点，等于悄悄把
    "杂物抢特征预算"这个因素从实验里删掉，又一次偏向乐观。中性灰同样不产生
    角点，但至少不会让照片边缘变成全图最强的对比边。
    """
    if scene is None:
        return np.full((h, w, 3), 128, np.uint8)
    bg = cv2.imread(str(scene), cv2.IMREAD_COLOR)
    if bg is None:
        raise SystemExit(f"读不出场景图：{scene}")
    return cv2.resize(bg, (w, h), interpolation=cv2.INTER_AREA)


def make_frame(
    ref_bgr: np.ndarray,
    fill: float,
    *,
    seed: int,
    scene: Path | None,
    crop: float = 1.0,
    frame_long_edge: int = FRAME_LONG_EDGE,
) -> np.ndarray:
    """造一帧"手机在某个取景距离上拍这张照片"的查询帧。"""
    # 1) 视角/光照扰动：复用 synth 的同一套参数分布，这样与 Phase 0 的数字可比
    warped, _ = synth.generate(ref_bgr, count=1, seed=seed)[0]

    # 2) 缩到目标占比，贴进传感器分辨率的画面里
    cw = SENSOR_LONG_EDGE
    ch = int(round(cw / FRAME_ASPECT))
    frame = _scene(scene, cw, ch)

    tw = max(1, int(round(cw * fill)))
    th = max(1, int(round(tw * warped.shape[0] / warped.shape[1])))
    if th > ch:  # 竖向放不下时以高度为准，保持照片不被裁掉
        th = ch
        tw = max(1, int(round(th * warped.shape[1] / warped.shape[0])))
    photo = cv2.resize(warped, (tw, th), interpolation=cv2.INTER_AREA)

    x0 = (cw - tw) // 2
    y0 = (ch - th) // 2
    frame[y0 : y0 + th, x0 : x0 + tw] = photo

    # 3) 传感器端的模糊：加在整帧上、缩放之前 —— 这是与 synth 最关键的差别
    rng = np.random.default_rng(seed)
    sigma = float(rng.uniform(0.8, 2.2))
    frame = cv2.GaussianBlur(frame, (0, 0), sigmaX=sigma)

    # 4) 客户端中心裁剪（候选修法之一）：裁在缩放**之前**，所以裁掉的边角换成了
    #    照片本身的分辨率 —— 这正是它有可能把召回拉回来的原因。
    if crop < 1.0:
        h, w = frame.shape[:2]
        cw2, ch2 = int(round(w * crop)), int(round(h * crop))
        x, y = (w - cw2) // 2, (h - ch2) // 2
        frame = frame[y : y + ch2, x : x + cw2]

    # 5) 客户端编码：长边 + q70，与 `Frames.kt` 一致（长边可调，因为它正是候选修法之一）
    s = frame_long_edge / max(frame.shape[:2])
    if s < 1.0:
        frame = cv2.resize(
            frame,
            (round(frame.shape[1] * s), round(frame.shape[0] * s)),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), FRAME_QUALITY])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _post(base: str, token: str, path: Path) -> str:
    r = subprocess.run(
        [
            "curl", "-s",
            "-H", f"Authorization: Bearer {token}",
            "-H", "X-PhotoAR-Endpoint: simcam",
            "-F", f"frame=@{path};type=image/jpeg",
            f"{base.rstrip('/')}/v1/recognize",
        ],
        capture_output=True,
        text=True,
    )
    try:
        d = json.loads(r.stdout)
    except Exception:
        return f"HTTP 解析失败: {r.stdout[:120]}"
    if d.get("matched"):
        return f"命中 inliers={d.get('inliers')}"
    return f"未命中 {d.get('reason')}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ref", type=Path, help="参考图（入过库的那张原图）")
    ap.add_argument("--fill", default=",".join(str(f) for f in DEFAULT_FILLS),
                    help="照片宽度占画面宽度的比例，逗号分隔")
    ap.add_argument("--scene", type=Path, default=None,
                    help="背景图（真实拍摄的桌面最有说服力）；不给则用中性灰")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--repeat", type=int, default=3,
                    help="每个占比造几帧（换 seed），看的是分布不是单点")
    ap.add_argument("--out", type=Path, default=None, help="把帧存到这个目录")
    ap.add_argument("--post", default=None, help="服务端 base URL，给了才走 HTTP")
    ap.add_argument("--token", default=None)
    # 下面两个是候选修法的旋钮，放进 harness 是为了让"改了到底有没有用"这件事
    # 可以随时重量一遍，而不是改完代码上真机赌一把。
    # 默认值必须取**查询侧**的常量（`backend.QUERY_*`），不是入库侧的
    # `features.*` —— 后者一度是这里的默认值，于是不带参数跑 bench 量的是
    # 「处理 640 / 300 特征」，正是 `Frames.LONG_EDGE` 那张表里「一档都不全过」
    # 的一行。结果是对着已经修好的配置打印出「它不适合作为识别目标」。
    ap.add_argument("--query-features", type=int, default=backend.QUERY_N_FEATURES,
                    help=f"查询侧提多少特征（默认 {backend.QUERY_N_FEATURES}，"
                         f"与 backend.QUERY_N_FEATURES 一致）。"
                         "只影响查询侧，不需要重建索引")
    ap.add_argument("--ref-features", type=int, default=features.N_FEATURES,
                    help=f"入库侧提多少特征（默认 {features.N_FEATURES}）。"
                         "改它要重建索引，而且 DescStore 是定长槽位，"
                         "槽位大小 = 8 + N*8 + N*32 字节，直接决定磁盘占用")
    ap.add_argument("--ref-long-edge", type=int, default=features.LONG_EDGE,
                    help=f"入库侧缩到的长边（默认 {features.LONG_EDGE}）")
    ap.add_argument("--crop", type=float, default=1.0,
                    help="客户端中心裁剪比例，如 0.7 表示只发中间 70%%。"
                         "等效把照片占比放大 1/crop 倍")
    ap.add_argument("--frame-long-edge", type=int, default=FRAME_LONG_EDGE,
                    help=f"客户端发的帧长边（默认 {FRAME_LONG_EDGE}，与 Frames.LONG_EDGE 一致）。"
                         "抬它是唯一能真正**增加信息量**的旋钮：照片在帧里的实际像素数"
                         "跟着涨，而不是把已经丢掉的细节插值回来")
    ap.add_argument("--ref-pre", default="none",
                    help=f"参考图预处理档位（{','.join(refprep.VARIANTS)}）。"
                         "**只作用在参考图上**，合成出来的帧仍然来自原图 —— 这是"
                         "刻意的保守设定：真机上相机看到的是打印件经过 ISP 的样子，"
                         "高频比原图更强，所以这里测出来的是下界")
    ap.add_argument("--query-long-edge", default=None,
                    help="服务端提查询特征时缩到的长边，逗号分隔即多尺度阶梯，"
                         f"取各尺度中最好的一档（默认单档 {backend.QUERY_LONG_EDGE}，"
                         f"与 backend.QUERY_LONG_EDGE 一致）。"
                         "存在的理由：入库参考图是「照片铺满长边 640」，"
                         "而查询帧里照片只占 fill，两侧尺度对不上")
    a = ap.parse_args(argv[1:])

    ref = cv2.imread(str(a.ref), cv2.IMREAD_COLOR)
    if ref is None:
        print(f"读不出参考图：{a.ref}", file=sys.stderr)
        return 1
    # 帧合成用**原图** `ref`，参考特征用预处理后的 `ref_pre` —— 两者刻意分开，
    # 见 `--ref-pre` 的帮助文字。
    ref_pre = refprep.apply(ref, a.ref_pre)
    ref_f = features.extract(ref_pre, long_edge=a.ref_long_edge, n_features=a.ref_features)

    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)

    ladder = ([int(x) for x in a.query_long_edge.split(",")]
              if a.query_long_edge else [backend.QUERY_LONG_EDGE])

    print(f"参考图 {a.ref}  {ref.shape[1]}x{ref.shape[0]}  特征 {len(ref_f)} 个  "
          f"预处理 {a.ref_pre}")
    print(f"判定门槛 MIN_INLIERS={verify.MIN_INLIERS}  查询侧特征 {a.query_features}  "
          f"中心裁剪 {a.crop:.2f}  发帧长边 {a.frame_long_edge}  "
          f"查询尺度 {','.join(str(x) for x in ladder)}\n")
    print(f"{'占比':>6} {'照片像素宽':>10} {'inliers':>26}  {'过门槛':>6}  {'全过':>4}")

    worst_pass = None
    worst_all = None
    for fill in [float(x) for x in a.fill.split(",")]:
        scores = []
        for i in range(a.repeat):
            frame = make_frame(ref, fill, seed=a.seed + i, scene=a.scene, crop=a.crop,
                               frame_long_edge=a.frame_long_edge)
            # 多尺度阶梯取最好的一档 —— 服务端真要这么做的话也是取最好的
            best_in = 0
            for le in ladder:
                q = features.extract(frame, long_edge=le, n_features=a.query_features)
                best_in = max(best_in, verify.verify_pair(q, ref_f, "ref").inliers)
            scores.append(best_in)
            if a.out:
                p = a.out / f"fill{fill:.2f}_s{a.seed + i}_in{best_in}.jpg"
                cv2.imwrite(str(p), frame, [cv2.IMWRITE_JPEG_QUALITY, FRAME_QUALITY])
                if a.post and a.token:
                    print(f"       ·  HTTP {p.name}: {_post(a.post, a.token, p)}")
        ok = max(scores) >= verify.MIN_INLIERS
        # "全过"才是真机上的可用判据：手持角度是随机的，只有一个 seed 过说明
        # 用户得靠运气 —— 前面 demo-a 就是这么骗过我一次的
        all_ok = min(scores) >= verify.MIN_INLIERS
        if ok:
            worst_pass = fill if worst_pass is None else min(worst_pass, fill)
        if all_ok:
            worst_all = fill if worst_all is None else min(worst_all, fill)
        detail = " ".join(f"{s:3d}" for s in scores)
        px = int(min(a.frame_long_edge, a.frame_long_edge * fill / a.crop))
        print(f"{fill:6.2f} {px:10d} {detail:>26}  {'是' if ok else '否':>6}  "
              f"{'是' if all_ok else '否':>4}")

    print()
    if worst_pass is None:
        print("这张图在任何取景距离下都过不了门槛 —— 它不适合作为识别目标。")
    else:
        print(f"最小可用占比 ≈ {worst_pass:.2f}（至少一个角度过）"
              f"／ {worst_all if worst_all else '—'}（所有角度都过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
