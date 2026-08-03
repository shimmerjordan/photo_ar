#!/usr/bin/env python3
"""把 XFeat 官方权重导成 ONNX。**离线一次性步骤，服务端与 App 都只吃产物。**

为什么要自己导，而不是用现成的 ONNX：

* XFeat 上游**没有**官方 ONNX（导出 PR verlab/accelerated_features#5 至今未合并）。
* 社区导出版（DavideCatto/XFeat-ONNX）与官方权重是否数值等价没人验证过，而且它把
  关键点数导成动态维度 —— 图里因此带 `NonZero`，输出形状只能运行时确定，ORT 没法
  预分配缓冲，Android 上还会在 EP 之间来回回落。自己导可以钉死形状。

产物是**服务端与 Android 共用的同一个文件**：两侧必须用完全一致的预处理与后处理，
否则描述子对不上，而这种错不会报错、只会让识别率静默变低。所以这个图把预处理里最容
易两边写歪的部分（灰度化、归一化、有效区掩码、L2）全烘进图内，对外只留一条契约：

    输入 image: (1, 3, 640, 640) float32，RGB，0..255
         size:  (2,) int64，[有效高, 有效宽]（padding 之前的真实尺寸）
    输出 keypoints:   (1, 512, 2) float32，(x, y)，在 640×640 画布坐标系里
         descriptors: (1, 512, 64) float32，已 L2 归一化
         scores:      (1, 512) float32，**≤0 的槽位是填充，必须丢掉**

为什么输入尺寸是固定的 640×640，而不是动态 H/W：

* 动态轴要求归一化分母来自运行时形状。TorchScript 导出会把 `x.shape[-1]` 烘成常量，
  于是「动态」是假的 —— 换一个宽度就静默算错，而且不报错。
* 固定形状换来全静态输出：图里没有 NonZero、没有动态维，ORT 能预分配，Android 上
  不会出现 EP 反复回落。这正是社区导出版的主要毛病。
* 项目本来就有「入库与查询都先缩到长边 640」这条硬约束（见 photoar.features 的
  尺度对齐约束），所以两边的长边已经是 640，短边补到 640 只是多算一点。

补边用 **BORDER_REFLECT_101（镜像）而不是补黑**：模型内部第一步是 InstanceNorm，
它按整张画布算均值方差。补黑会把统计量拉偏，而参考图（3:2）与相机帧（16:9）补黑的
面积不同 —— 同一处纹理在两侧会拿到不同的描述子。镜像补边保留原图的统计特性，且镜像
边界是连续的，不会造出阶跃边。镜像区里确实会冒出关键点，所以图内用 `size` 掩掉：
掩在 topk 之前，512 个槽位就全给真实区域，不浪费。

    python tools/export_models.py --out data/models
    python tools/export_models.py --out data/models --xfeat-repo /path/to/accelerated_features

只需要 torch + onnx + onnxruntime（`pip install -e ".[export]"`）。服务端镜像里没有
torch，也不需要。

关于 DINOv2 / 全局描述子：**刻意没有用。** 曾经打算拿 DINOv2 ViT-S/14 做粗排（它是
Apache-2.0、权重有官方非 HF 直链、384 维、CPU 43ms），但这个场景的查询是「相机帧里
有一张打印照片 + 一大片背景」，整帧的全局向量会被背景主导，检索会退化。粗排必须对
杂乱鲁棒，所以留在「局部描述子 + 词汇树倒排」这条路上（见 photoar.floatvocab）——
它在本项目这个场景里已经实测过。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

XFEAT_REPO = "https://github.com/verlab/accelerated_features.git"

# 画布边长。与 photoar.features.LONG_EDGE 必须一致 —— 那个常量决定入库与查询缩到
# 多大，这个决定网络吃多大。两者不一致就等于两侧尺度不对齐。
CANVAS = 640

# 关键点上限。**这个数字是性能上的硬约束，不是随手取的。**
#
# 互近邻匹配的成本随关键点数**超线性**增长（N×N 相似度矩阵的 argmax 是内存瓶颈）。
# 实测（i9-11900K 限 3 线程，numpy）：256 点 0.09ms、512 点 0.46ms、1024 点 1.89ms、
# 2048 点 10.7ms、4096 点 116.7ms。精排要对 Top-K 个候选各做一次，K=20 时 512 点是
# 9.2ms，4096 点是 2334ms —— 后者一个查询就把预算烧光了。
#
# 512 是"够用"与"跑得动"的交点：一张 640px 长边的照片上 512 个点已经远超几何校验
# 需要的量（判定门槛是几十个内点）。
TOP_K = 512

# 检测阈值，与 XFeat 官方 `detectAndCompute` 的默认值一致。改这个值等于换一套关键点
# 集，库里已存的描述子会全部失配 —— 要改就得重建整个库。
DET_THRESHOLD = 0.05

# 候选超采样倍数。官方实现对**全部** NMS 峰值算可靠度再排序取 top_k；导出图为了拿到
# 静态输出形状，只能先按热图值取一批候选、再按可靠度重排。
#
# 这个近似是安全的：可靠度 = 热图值 × 可靠度图值，而后者过了 sigmoid 落在 (0,1)，
# 所以可靠度**恒不大于**热图值。真正落在 top_k 里的点，其热图值必然 ≥ 第 k 名的
# 可靠度，于是它一定在"按热图值排前面"的那批里。4 倍超采样把"那批"取得足够大。
# 是否真的等价不靠推理，靠 --verify 与官方输出逐点比。
OVERSAMPLE = 4

OPSET = 17  # GridSample 需要 ≥16；官方权重用 torch 2.2 存的，17 是稳妥下限


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clone(url: str, dst: Path) -> Path:
    if dst.exists():
        return dst
    print(f"[export] clone {url}")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", url, str(dst)], check=True
    )
    return dst


def _build_export_module(xfeat_repo: Path, top_k: int):
    """构造可导出的 XFeat 包装模块，并返回 (wrapper, net)。

    与官方 `XFeat.detectAndCompute` 的差异只有两处，都是为了拿到静态形状：
      1. 候选选择方式（见 OVERSAMPLE）；
      2. 有效区掩码（官方没有补边这回事）。
    其余每一步都刻意照抄，包括官方 `InterpolateSparse2d.normgrid` 里那个用 (size-1)
    作分母、却配 `align_corners=False` 的组合 —— 那是个半像素级的偏移，但描述子是在
    这个约定下训出来的，"修正"它等于换一套描述子。
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    sys.path.insert(0, str(xfeat_repo))
    from modules.model import XFeatModel  # noqa: E402

    net = XFeatModel().eval()
    net.load_state_dict(
        torch.load(xfeat_repo / "weights" / "xfeat.pt", map_location="cpu")
    )

    # 上游 `_unfold2d` 用 Tensor.unfold，TorchScript 导出直接报 unsupported
    # （symbolic_opset9.unfold 只支持静态尺寸）。pixel_unshuffle 的通道排布与它
    # **完全一致**：两者的输出通道下标都是 c*ws² + dy*ws + dx。下面 _assert_unfold
    # 会真的比一遍，不靠这段注释。
    def _unfold2d(x, ws=2):
        return F.pixel_unshuffle(x, ws)

    _assert_unfold(net, _unfold2d)
    net._unfold2d = _unfold2d

    class XFeatExport(nn.Module):
        def __init__(self, net, top_k: int):
            super().__init__()
            self.net = net
            self.top_k = int(top_k)
            self.cand = int(top_k) * OVERSAMPLE
            self.det_threshold = DET_THRESHOLD
            # 画布边长在图里是常量（输入形状固定），所以这两条坐标轴可以预先建好
            self.register_buffer("rows", torch.arange(CANVAS).view(1, 1, CANVAS, 1))
            self.register_buffer("cols", torch.arange(CANVAS).view(1, 1, 1, CANVAS))

        @staticmethod
        def _kpts_heatmap(logits):
            """官方 get_kpts_heatmap：65 通道 softmax 取前 64，重排回全分辨率。"""
            scores = F.softmax(logits, 1)[:, :64]
            b, _, h, w = scores.shape
            heat = scores.permute(0, 2, 3, 1).reshape(b, h, w, 8, 8)
            return heat.permute(0, 1, 3, 2, 4).reshape(b, 1, h * 8, w * 8)

        @staticmethod
        def _sample(x, pos, mode: str):
            """官方 InterpolateSparse2d，逐字节照抄它的归一化。

            分母用 CANVAS-1 这个常量而不是 x 的形状：热图与输入同分辨率
            （CANVAS×CANVAS），而 feats/reliab 虽然是 1/8 分辨率，grid_sample 的
            归一化坐标本来就与被采样张量的分辨率无关 —— 官方传进来的 H/W 也是
            输入分辨率，不是特征图分辨率。
            """
            denom = float(CANVAS - 1)
            grid = (2.0 * (pos / denom) - 1.0).unsqueeze(-2)
            out = F.grid_sample(x, grid, mode=mode, align_corners=False)
            return out.permute(0, 2, 3, 1).squeeze(-2)

        def forward(self, image, size):
            # image: (1,3,CANVAS,CANVAS) float32 RGB 0..255
            # size:  (2,) int64 = [有效高, 有效宽]
            #
            # 灰度化与 InstanceNorm 都在 net 内部做，所以 0..255 与 0..1 等价
            # （InstanceNorm 逐样本归一化，抹掉全局尺度）—— 契约取 0..255，因为两侧
            # 客户端拿到的原始像素就是这个范围，少一次约定就少一处错。
            feats, kpt_logits, reliab = self.net(image)
            feats = F.normalize(feats, dim=1)

            heat = self._kpts_heatmap(kpt_logits)  # (1,1,CANVAS,CANVAS)

            # NMS：5×5 局部极大 + 阈值。用等值比较取峰，不用 nonzero ——
            # nonzero 会让输出形状变成动态的，正是要避免的东西。
            local_max = F.max_pool2d(heat, kernel_size=5, stride=1, padding=2)
            peak = (heat >= local_max) & (heat > self.det_threshold)

            # 有效区掩码：镜像补边区域里的峰值全部抹掉，512 个槽位只给真实像素。
            valid_h = size[0:1].view(1, 1, 1, 1)
            valid_w = size[1:2].view(1, 1, 1, 1)
            inside = (self.rows < valid_h) & (self.cols < valid_w)
            peak_score = torch.where(peak & inside, heat, torch.zeros_like(heat))

            # 先按热图值取 cand 个候选（静态形状）
            flat = peak_score.reshape(1, -1)
            _, idx = torch.topk(flat, self.cand, dim=1)
            ys = torch.div(idx, CANVAS, rounding_mode="floor").to(torch.float32)
            xs = (idx % CANVAS).to(torch.float32)
            cand_pts = torch.stack([xs, ys], dim=-1)  # (1,cand,2)

            # 可靠度 = 热图最近邻 × 可靠度图双线性，与官方一致。乘回 inside 的掩码
            # 结果（用候选自身的热图值是否为 0 判断）—— 峰值不足时 topk 会把 0 分槽
            # 也取进来，它们必须保持 0 分，否则会被当成有效点。
            keep = (torch.gather(flat, 1, idx) > 0).to(torch.float32)
            near = self._sample(heat, cand_pts, "nearest").squeeze(-1)
            bil = self._sample(reliab, cand_pts, "bilinear").squeeze(-1)
            rel = near * bil * keep

            # 再按可靠度重排取 top_k
            rel_top, order = torch.topk(rel, self.top_k, dim=1)
            pts = torch.gather(cand_pts, 1, order.unsqueeze(-1).expand(-1, -1, 2))

            desc = self._sample(feats, pts, "bicubic")
            desc = F.normalize(desc, dim=-1)

            # 有效点不足 top_k 时，多出来的槽位分数为 0。**调用方必须按 scores > 0
            # 过滤**，这与官方 `valid = scores > 0` 是同一条规则。
            return pts, desc, rel_top

    return XFeatExport(net, top_k).eval(), net


def _assert_unfold(net, replacement) -> None:
    """证明 pixel_unshuffle 与上游 _unfold2d 逐元素相同。

    这是一次**行为替换**，不是重构：如果通道排布差一点，keypoint_head 收到的就是
    打乱过的输入，模型不会报错，只会安静地输出垃圾。所以在导出之前先钉死它。
    """
    import torch

    original = type(net)._unfold2d
    x = torch.randn(1, 1, 64, 96)
    for ws in (2, 8):
        a = original(net, x, ws=ws)
        b = replacement(x, ws=ws)
        if a.shape != b.shape or not torch.equal(a, b):
            raise SystemExit(
                f"[export] pixel_unshuffle 与 _unfold2d 在 ws={ws} 上不等价，"
                "不能替换（形状 "
                f"{tuple(a.shape)} vs {tuple(b.shape)}）"
            )


def export_xfeat(xfeat_repo: Path, out: Path, top_k: int, verify: bool) -> Path:
    import torch

    wrapper, net = _build_export_module(xfeat_repo, top_k)
    dummy_img = torch.rand(1, 3, CANVAS, CANVAS) * 255.0
    dummy_size = torch.tensor([CANVAS, CANVAS], dtype=torch.int64)

    out.mkdir(parents=True, exist_ok=True)
    dst = out / "xfeat.onnx"
    print(f"[export] XFeat -> {dst}（{CANVAS}²，top_k={top_k}）")
    with torch.inference_mode():
        torch.onnx.export(
            wrapper,
            (dummy_img, dummy_size),
            str(dst),
            input_names=["image", "size"],
            output_names=["keypoints", "descriptors", "scores"],
            # 没有 dynamic_axes：全静态是刻意的，理由见模块 docstring。
            opset_version=OPSET,
            do_constant_folding=True,
            dynamo=False,
        )
    print(f"[export]   {dst.stat().st_size / 1e6:.2f} MB  sha256={_sha256(dst)}")

    if verify:
        _verify(xfeat_repo, dst, wrapper, net, top_k)
    return dst


def _verify(xfeat_repo: Path, onnx_path: Path, wrapper, net, top_k: int) -> None:
    """两层比较：ONNX vs PyTorch 包装、包装 vs 官方 detectAndCompute。

    缺了第二层的话，一个"忠实导出的错误实现"照样全绿。
    """
    import numpy as np
    import onnxruntime as ort
    import torch

    img, real_h, real_w = _sample_canvas(xfeat_repo)
    ten = torch.from_numpy(img.transpose(2, 0, 1)[None].astype("float32"))
    size = torch.tensor([real_h, real_w], dtype=torch.int64)

    with torch.inference_mode():
        t_pts, t_desc, t_sc = wrapper(ten, size)

    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    o_pts, o_desc, o_sc = sess.run(
        None, {"image": ten.numpy(), "size": size.numpy()}
    )

    # **只比有效槽位。** 逐槽位比是错的：有效峰值不足 512 时，剩下的槽位分数为 0，
    # 而 topk 在等值上的选择顺序是未定义的 —— PyTorch 与 ORT 各挑一批不同的零分位置，
    # 逐槽位比会看到几百像素的"差异"，那不是导出错了。
    t_valid, o_valid = t_sc.numpy()[0] > 0, o_sc[0] > 0
    if int(t_valid.sum()) != int(o_valid.sum()):
        raise SystemExit(
            f"[verify] 有效点数不一致：PyTorch {int(t_valid.sum())} / "
            f"ONNX {int(o_valid.sum())}"
        )
    n_common, pp = _match_points(t_pts.numpy()[0][t_valid], o_pts[0][o_valid])
    same = n_common / max(1, int(o_valid.sum()))
    d_desc = (
        float(
            np.abs(
                t_desc.numpy()[0][t_valid][pp[:, 0]] - o_desc[0][o_valid][pp[:, 1]]
            ).max()
        )
        if len(pp)
        else float("nan")
    )
    print(
        f"[verify] ONNX vs PyTorch：有效点 {int(o_valid.sum())}，同位重合 "
        f"{n_common}（{same:.1%}），描述子 max|Δ|={d_desc:.3e}"
    )
    if same < 0.999 or (not np.isnan(d_desc) and d_desc > 1e-3):
        raise SystemExit("[verify] ONNX 与 PyTorch 不一致，导出不可用")

    valid = o_valid
    print(f"[verify] 有效关键点 {int(valid.sum())}/{top_k}（画布 {CANVAS}²，"
          f"有效区 {real_h}×{real_w}）")
    if valid.sum() == 0:
        raise SystemExit("[verify] 一个有效关键点都没有")
    ob = o_pts[0][valid]
    if float(ob[:, 0].max()) >= real_w or float(ob[:, 1].max()) >= real_h:
        raise SystemExit("[verify] 有关键点落在镜像补边区域，掩码没生效")

    # 与官方实现比：把**未补边**的真实图喂给官方 detectAndCompute，比同位重合率
    sys.path.insert(0, str(xfeat_repo))
    from modules.xfeat import XFeat  # noqa: E402

    tight = img[:real_h, :real_w]
    tight_ten = torch.from_numpy(tight.transpose(2, 0, 1)[None].astype("float32"))
    official = XFeat(weights=net.state_dict(), top_k=top_k)
    ref = official.detectAndCompute(tight_ten, top_k=top_k)[0]
    r_pts = ref["keypoints"].cpu().numpy()
    r_desc = ref["descriptors"].cpu().numpy()

    inter, pairs = _match_points(r_pts, ob)
    overlap = inter / max(1, min(len(r_pts), len(ob)))
    # 用**余弦相似度**而不是 max|Δ| 衡量描述子差异。描述子已 L2 归一化，匹配用的就是
    # 余弦/内积，所以余弦才是"匹配还成不成立"的直接度量；单个分量差 0.2 完全可能对应
    # 0.99 的余弦，用 max|Δ| 设门槛会因为一个无关分量而误判。
    cos = (
        (r_desc[pairs[:, 0]] * o_desc[0][valid][pairs[:, 1]]).sum(axis=1)
        if len(pairs)
        else np.array([np.nan])
    )
    print(
        f"[verify] 与官方 detectAndCompute：官方 {len(r_pts)} 点 / 导出 {len(ob)} 点，"
        f"同位重合 {inter}（{overlap:.1%}），共同点描述子余弦 "
        f"均值={float(cos.mean()):.4f} 最小={float(cos.min()):.4f} "
        f"p1={float(np.percentile(cos, 1)):.4f}"
    )
    # 两个门槛都不要求与官方完全一致，因为输入本来就不同：官方吃紧图、导出吃镜像补边
    # 到 640² 的画布，InstanceNorm 的统计量因此略有差别。要证明的是"语义没走偏"，
    # 不是"逐位相同"—— 真正要求逐位相同的是上面 ONNX vs PyTorch 那一层。
    if overlap < 0.8:
        raise SystemExit("[verify] 关键点重合率过低，导出与官方实现不一致")
    if float(cos.mean()) < 0.95:
        raise SystemExit("[verify] 共同关键点上的描述子已偏离官方语义")


def _match_points(a, b):
    """同位点配对（整数坐标相等）。返回重合数与索引对。"""
    import numpy as np

    key_a = {(round(float(x)), round(float(y))): i for i, (x, y) in enumerate(a)}
    pairs = [
        (key_a[k], j)
        for j, (x, y) in enumerate(b)
        if (k := (round(float(x)), round(float(y)))) in key_a
    ]
    return len(pairs), np.array(pairs, dtype=int).reshape(-1, 2)


def _sample_canvas(xfeat_repo: Path):
    """拿一张真实图片，按产品规则缩放 + 镜像补边成 CANVAS²。

    用随机噪声验证是自欺欺人：噪声图上处处是峰值，NMS 与超采样近似的差异恰好被
    掩盖掉。
    """
    import cv2

    for cand in sorted((xfeat_repo / "assets").glob("*")):
        if cand.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        bgr = cv2.imread(str(cand), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        scale = CANVAS / max(h, w)
        nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
        small = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = cv2.copyMakeBorder(
            small, 0, CANVAS - nh, 0, CANVAS - nw, cv2.BORDER_REFLECT_101
        )
        return canvas, nh, nw
    raise SystemExit(f"[verify] 在 {xfeat_repo / 'assets'} 里找不到可用的样图")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("data/models"))
    ap.add_argument("--xfeat-repo", type=Path)
    ap.add_argument("--top-k", type=int, default=TOP_K)
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过与 PyTorch/官方实现的比对。只在已经比对过、纯粹重新导出时用。",
    )
    args = ap.parse_args(argv)

    try:
        import torch  # noqa: F401
    except ImportError:
        print(
            '需要 torch 才能导出：pip install -e ".[export]"\n'
            "（服务端运行时不需要 torch，它只吃 ONNX）",
            file=sys.stderr,
        )
        return 2

    scratch = Path(tempfile.mkdtemp(prefix="photoar-export-"))
    try:
        repo = args.xfeat_repo or _clone(XFEAT_REPO, scratch / "xfeat")
        export_xfeat(repo, args.out, args.top_k, not args.no_verify)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"[export] 完成。产物在 {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
