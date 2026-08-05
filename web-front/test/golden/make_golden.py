#!/usr/bin/env python3
"""生成跨语言 golden：服务端 `cv2` 的 ORB 输出，给浏览器侧逐位对答案。

## 这份 golden 挡住的是什么

Web 版在浏览器里自己提 ORB 特征，然后拿它去和库里 `desc.bin`（服务端 `cv2.ORB` 算的）
配对。两侧只要有一处不一致，**描述子就落在两个不可比的空间里** —— 而失败方式是识别率
归零，不是报错。没有这份 golden，那种失败只能靠"真机上扫不出来"发现，而那时候有十几个
可能的原因。

与项目里另一份跨语言 golden（XFeat 预处理契约，Python 的 `xfeat.prepare()` 对
Kotlin 的 `XFeatPreprocess`）是同一个手法。

## 为什么输入是**原始字节**而不是一张 PNG

浏览器解码 PNG 时可能应用嵌入的色彩配置（ICC profile），于是 `getImageData` 拿到的
像素和 `cv2.imread` 拿到的**可以不一样**。那种差异只有几个灰阶，却足以移动 FAST 的
角点、改变 Harris 排序、翻转 rBRIEF 的某几个比特 —— 也就是说，用 PNG 当 golden 输入
的话，**这份测试会在"两边都对"的情况下红，而在"两边都错"的情况下绿**。

所以输入是裸的 uint8 缓冲：`input-bgr.bin`（H×W×3，BGR 字节序）。两侧各自把它包成
Mat，测的就是 ORB 本身。色彩管理那一层的风险单独由 `case_rgba_equiv` 覆盖。

## 三个 case

- `orb_gray`   —— 灰度直接进 ORB。算法一致性的最小构造，A/B 定位用。
- `extract`    —— 走完整的 `features.extract` 契约：resize → BGR2GRAY → ORB →
                  按 response 降序取 top-N。这才是运行时真正跑的那条。
- `rgba_equiv` —— 同一张图从 RGBA 转灰度必须与从 BGR 转灰度**逐字节相同**。
                  浏览器手上只有 RGBA（canvas 的 `getImageData`），而服务端约定是 BGR；
                  这一条证明浏览器侧用 `COLOR_RGBA2GRAY` 不引入任何偏差，从而不必在
                  JS 里手工重排通道（那一步反而是新的出错点）。

用法：`python3 test/golden/make_golden.py`（写进本目录，产物进 git）
"""

from __future__ import annotations

import base64
import gzip
import json
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]  # web-front/test/golden -> photo-ar
sys.path.insert(0, str(REPO / "src"))

from photoar import backend as backend_mod  # noqa: E402
from photoar import features  # noqa: E402

# 图的尺寸刻意**不是**方的、也不是 640 的整数倍：`resize_to_long_edge` 的
# round 行为、以及 ORB 金字塔各层的边界处理，都只在非整数缩放比下才被走到。
H, W = 468, 708  # 与用户真实素材同一个尺寸（708×468 婚礼照，见 backend.QUERY_N_FEATURES）


def synth_bgr() -> np.ndarray:
    """一张确定性的、角点丰富的合成图。

    不用真实照片：素材不该进仓库，而且真实照片一换，golden 全部作废。
    不用纯噪声：ORB 在纯噪声上的 Harris 响应几乎等值，排序会被浮点末位决定 ——
    那会让这份 golden 变成一个随机失败的测试。所以是**结构 + 纹理**：多尺度方块给
    稳定的强角点，低幅噪声保证描述子的每个比特都被真的用到。
    """
    rng = np.random.default_rng(20260804)
    img = np.full((H, W, 3), 128, np.uint8)

    # 多尺度方块。三档边长，让 ORB 的 8 层金字塔每一层都有东西可检。
    for size, step, base in ((48, 96, 30), (24, 56, 210), (12, 28, 70)):
        for y in range(8, H - size, step):
            for x in range(8, W - size, step):
                shade = (base + 17 * ((x // step + y // step) % 5)) % 256
                img[y : y + size, x : x + size] = shade

    # 几条斜线：方块只给轴对齐的角点，斜线让 intensity-centroid 方向真的分散开。
    for i in range(0, W, 37):
        cv2.line(img, (i, 0), (i - H, H), (240, 240, 240), 2)

    noise = rng.integers(-18, 19, size=(H, W, 3), dtype=np.int16)
    return np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _f32(values) -> str:
    """float32 数组 → base64。

    **不用 JSON 数字**：`1067.904052734375` 这个 float32 要 16 位十进制才写得精确，
    而 `round(x, 6)` 会把它变成 `1067.904053` —— 于是浏览器侧那条"坐标必须精确相等"
    的断言拿一个已经被截断过的期望值去比精确值，**必然红，而且红的原因与被测代码无关**。
    第一版就是这么红的。base64 传原始 4 字节，两边比的是同一个数。
    """
    return base64.b64encode(np.asarray(values, np.float32).tobytes()).decode()


def kp_arrays(kps) -> dict:
    """关键点拆成六个平行数组，全部按位精确。

    拆成数组而不是对象列表：浏览器侧要拿它建「位置 → 描述子」的索引，
    而 TypedArray 上做这件事不需要先解一遍 JSON 对象。
    """
    return {
        "ptsB64": _f32([c for k in kps for c in k.pt]),
        "anglesB64": _f32([k.angle for k in kps]),
        "responsesB64": _f32([k.response for k in kps]),
        "sizesB64": _f32([k.size for k in kps]),
        "octaves": [int(k.octave) for k in kps],
    }


def case_orb_gray(bgr: np.ndarray) -> dict:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    orb = cv2.ORB_create(
        nfeatures=features.N_FEATURES,
        scaleFactor=features.SCALE_FACTOR,
        nlevels=features.N_LEVELS,
    )
    kps, desc = orb.detectAndCompute(gray, None)
    return {
        "nfeatures": features.N_FEATURES,
        "scaleFactor": features.SCALE_FACTOR,
        "nlevels": features.N_LEVELS,
        "count": len(kps),
        **kp_arrays(kps),
        "descB64": base64.b64encode(np.ascontiguousarray(desc)).decode(),
        "graySha": _sha_bytes(gray),
    }


def case_extract(bgr: np.ndarray) -> dict:
    """完整的查询侧管线：长边 1280 + 4000 特征（`backend.QUERY_*`）。

    参数取**查询侧**而不是入库侧：Web 版扮演的是查询端，而这两组数故意不同
    （见 `backend.QUERY_N_FEATURES` 的注释，那是识别率的主导变量）。
    """
    long_edge = backend_mod.QUERY_LONG_EDGE
    n = backend_mod.QUERY_N_FEATURES
    small = features.resize_to_long_edge(bgr, long_edge)
    feats = features.extract(bgr, long_edge=long_edge, n_features=n)

    # 在这里把 extract 的内部步骤再走一遍，为的是拿到它丢掉的 KeyPoint 对象
    # （浏览器侧要用 octave 一起当配对键）。**并当场断言复现结果与 extract 一致** ——
    # 否则这份 golden 就变成"给我这段复现代码对答案"，而不是给产品路径对答案。
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    kps, desc = cv2.ORB_create(
        nfeatures=n, scaleFactor=features.SCALE_FACTOR, nlevels=features.N_LEVELS
    ).detectAndCompute(gray, None)
    responses = np.array([k.response for k in kps], np.float32)
    order = np.argsort(-responses, kind="stable")[:n]
    picked = [kps[i] for i in order]
    assert np.array_equal(
        np.array([k.pt for k in picked], np.float32).reshape(-1, 2), feats.pts
    ), "复现的 extract 顺序与 features.extract 不一致"
    assert np.array_equal(desc[order], feats.desc), "复现的 extract 描述子与 features.extract 不一致"

    return {
        "longEdge": long_edge,
        "nfeatures": n,
        "resizedH": int(small.shape[0]),
        "resizedW": int(small.shape[1]),
        "resizedSha": _sha_bytes(np.ascontiguousarray(small)),
        "count": len(feats),
        **kp_arrays(picked),
        "descB64": base64.b64encode(feats.desc).decode(),
        # 检测出来的**全部**关键点数（截断前）。浏览器侧用它判断 top-N 那道截断是不是
        # 落在一堆 response 相同的点中间 —— 那正是两边可能取到不同子集的唯一原因。
        "detectedTotal": len(kps),
    }


def case_rgba_equiv(bgr: np.ndarray) -> dict:
    rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA)
    from_bgr = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    from_rgba = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
    identical = bool(np.array_equal(from_bgr, from_rgba))
    return {
        # 这一条在 Python 侧就先自证一遍。它要是在这里都不成立，浏览器侧那条断言
        # 就不是在测浏览器，而是在测一个错的前提。
        "pythonIdentical": identical,
        "maxAbsDiff": int(np.abs(from_bgr.astype(np.int16) - from_rgba.astype(np.int16)).max()),
        "graySha": _sha_bytes(from_bgr),
        "rgbaSha": _sha_bytes(rgba),
    }


def case_cross_match(bgr: np.ndarray) -> tuple[dict, bytes]:
    """**端到端**：库里那份参考描述子（服务端算的）配一帧手持查询（浏览器要自己算）。

    这一条才是真正的判据。前两个 case 测的是「两侧算同一张图得到同样的东西」，而运行时
    从来不是那样：**参考侧永远是服务端算的**（`desc.bin` 里躺着的那份），**查询侧才是
    浏览器算的**。要问的问题只有一个 —— 这两份能不能配上、内点数够不够过 `MIN_INLIERS`。

    所以这里：
      * `ref` 用**入库侧**参数（640 / 300），就是 `desc.bin` 里一条 slot 的内容；
      * 查询帧用 `bench/simcam.make_frame` 造，与 Phase 0 的数字同一套口径
        （视角/光照扰动 + 传感器模糊 + JPEG q70），`fill=0.4` 取的是出口条件那一档；
      * Python 侧跑完整 `verify.verify_pair` 并把 `inliers` / `det` / `ok` 存下来。

    浏览器侧拿到的是：参考侧的 pts+desc（**不自己算**）、查询帧的原始 BGR 字节、
    以及期望的内点数。它自己提特征、自己 BFMatcher、自己 RANSAC，然后对答案。

    第二个返回值是查询帧的原始字节（gzip 后落盘 —— 1280×960×3 是 3.7MB，
    而 gzip 能压掉一多半，且浏览器有原生 `DecompressionStream('gzip')`，不引依赖）。
    """
    sys.path.insert(0, str(REPO / "bench"))
    import simcam  # noqa: PLC0415

    from photoar import verify  # noqa: PLC0415

    ref = features.extract(bgr)  # 入库侧默认：640 / 300
    frame = simcam.make_frame(bgr, fill=0.4, seed=7, scene=None)
    query = features.extract(
        frame,
        long_edge=backend_mod.QUERY_LONG_EDGE,
        n_features=backend_mod.QUERY_N_FEATURES,
    )
    res = verify.verify_pair(query, ref, "golden")

    # 把配对也存下来。不是为了让浏览器抄答案 —— 是为了在内点数对不上时能分清
    # 「配对阶段就少了」和「配对一样多但 RANSAC 挑出的内点不同」。这两件事的修法
    # 毫不相干，而只看一个 inliers 数字是分不开的。
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(query.desc, ref.desc)

    return (
        {
            "frameH": int(frame.shape[0]),
            "frameW": int(frame.shape[1]),
            "frameSha": _sha_bytes(np.ascontiguousarray(frame)),
            "fill": 0.4,
            "seed": 7,
            # 参考侧：浏览器直接用这份，模拟从 desc.bin 拿到的那条 slot
            "refCount": len(ref),
            "refPtsB64": _f32(ref.pts.reshape(-1)),
            "refDescB64": base64.b64encode(ref.desc).decode(),
            "refLongEdge": features.LONG_EDGE,
            "queryLongEdge": backend_mod.QUERY_LONG_EDGE,
            "queryNFeatures": backend_mod.QUERY_N_FEATURES,
            "queryCount": len(query),
            # 期望
            "matchCount": len(matches),
            "inliers": int(res.inliers),
            "det": round(float(res.det), 9),
            "ok": bool(res.ok),
            "minInliers": int(verify.MIN_INLIERS),
            "ransacReproj": float(verify.RANSAC_REPROJ),
            "ransacMaxIters": int(verify.RANSAC_MAX_ITERS),
            "detMin": float(verify.DET_MIN),
            "detMax": float(verify.DET_MAX),
        },
        np.ascontiguousarray(frame).tobytes(),
    )


def _sha_bytes(arr: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(arr.tobytes()).hexdigest()[:16]


def main() -> int:
    bgr = synth_bgr()
    (HERE / "input-bgr.bin").write_bytes(np.ascontiguousarray(bgr).tobytes())

    cross, frame_bytes = case_cross_match(bgr)
    # gzip 而不是裸字节：1280×960×3 = 3.7MB 进 git 太重，而浏览器有原生
    # DecompressionStream('gzip')，解压这一步不引任何依赖。
    (HERE / "frame-bgr.bin.gz").write_bytes(gzip.compress(frame_bytes, 6))

    golden = {
        "_note": "由 test/golden/make_golden.py 生成，勿手改。改了要连 opencv.js 版本一起说明。",
        "cv2Version": cv2.__version__,
        "input": {"h": H, "w": W, "channels": 3, "order": "BGR", "sha": _sha_bytes(bgr)},
        "orb_gray": case_orb_gray(bgr),
        "extract": case_extract(bgr),
        "rgba_equiv": case_rgba_equiv(bgr),
        "cross_match": cross,
    }
    (HERE / "expect.json").write_text(json.dumps(golden, indent=1), encoding="utf-8")

    print(f"cv2 {cv2.__version__}")
    print(f"input-bgr.bin  {H}x{W}x3  sha16={golden['input']['sha']}")
    print(f"orb_gray       {golden['orb_gray']['count']} 个关键点")
    print(f"extract        {golden['extract']['count']} 个（长边 {golden['extract']['longEdge']}）")
    print(f"rgba_equiv     pythonIdentical={golden['rgba_equiv']['pythonIdentical']}")
    print(
        f"cross_match    参考 {cross['refCount']} × 查询 {cross['queryCount']} → "
        f"配对 {cross['matchCount']}，内点 {cross['inliers']}（门槛 {cross['minInliers']}）"
        f"，det={cross['det']}，ok={cross['ok']}"
    )
    print(f"frame-bgr.bin.gz  {(HERE / 'frame-bgr.bin.gz').stat().st_size} B")
    if not golden["rgba_equiv"]["pythonIdentical"]:
        print("⚠️ RGBA2GRAY 与 BGR2GRAY 在 Python 侧就不一致，浏览器侧不能直接用 RGBA")
        return 1
    if not cross["ok"]:
        # golden 本身必须是一次**成功**的识别。拿一次失败的识别当基准，浏览器侧
        # 「也失败」会被当成通过 —— 那就成了一个恒绿的测试。
        print("⚠️ cross_match 在 Python 侧就没过判定，这份 golden 不能用")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
