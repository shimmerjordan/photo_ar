"""XFeat 局部特征（ONNX 推理）。

替代 ORB 的那一半：同样产出 `Features`（关键点 + 描述子），但描述子是 64 维
float32 而不是 32 字节二进制，匹配也因此从 Hamming 换成余弦（见 `photoar.match`）。

**为什么换**（决策依据，别在没读过这段之前调参数）：

* ORB 的二值描述子对印刷半色调网纹和翻拍模糊特别脆，而本项目的查询恰好全是
  「打印出来再用手机拍」。XFeat 用 MegaDepth + COCO 合成 warp 训练，论文报告对
  视角与光照变化的鲁棒性显著好于 ORB/SIFT，且在 ScanNet 上的泛化优于 DISK/ALIKE。
* XFeat 权重与代码都是 Apache-2.0，可直接用（SuperPoint/SuperGlue 的权重是
  non-commercial research only，SiLK 是 GPL——都排除了）。
* 它是唯一在低端 ARM 上还能过 1 FPS 的学习型检测器（论文实测 Cortex-A53 上
  480×360 = 1.8 FPS），所以同一个模型能放到手机上跑，服务端就不必在 N5095 上
  跑神经网络（那台机器没有 AVX/AVX2，只到 SSE4.2）。

**预处理契约（与 tools/export_models.py 里烘进图的那份必须逐条一致）**：

  1. BGR → RGB
  2. 缩到长边 = CANVAS（640），保持长宽比
  3. **镜像补边**（BORDER_REFLECT_101）到 CANVAS×CANVAS，只补右侧与下方
  4. HWC → NCHW，float32，值域 0..255（不除 255）
  5. 另传 size = [有效高, 有效宽]，图内用它掩掉补边区的关键点

第 3 步为什么是镜像而不是补黑：模型第一层是 InstanceNorm，按整张画布算均值方差。
补黑会把统计量拉偏，而参考图（3:2）与相机帧（16:9）补黑的面积不同 —— 同一处纹理
在两侧会拿到不同的描述子。镜像保留原图统计特性，且镜像边界连续、不造阶跃边。

第 4 步不除 255 是因为 InstanceNorm 逐样本归一化会抹掉全局尺度，0..255 与 0..1
等价；取 0..255 是因为两侧客户端拿到的原始像素就是这个范围，少一次约定少一处错。

关键点坐标在**画布坐标系**里，而补边只加在右下，所以它同时也是「缩放后图像」的
坐标系 —— 与 ORB 路径（`features.resize_to_long_edge` 之后的坐标）完全同一个约定，
下游 RANSAC 不需要任何坐标映射。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2
import numpy as np

from .features import Features

# 画布边长。与 features.LONG_EDGE 是同一个数，但**刻意各自声明**：那个是 ORB 路径的
# 缩放目标，这个是 ONNX 图的固定输入尺寸（导出时烘死了，改这里不会改模型）。
# 两者恰好相等是设计，不是耦合 —— 真要改，得重新导出模型。
#
# 而且**已经不再相等了**：ORB 路径的入库侧仍是 640，查询侧已经抬到
# `backend.QUERY_LONG_EDGE`(1280)。那一步是 ORB 召回率的主导变量（真机上「扫不出来」
# 就是查询侧还在 640 上处理），实测 640→1280 把「5 个视角全过」的照片占比门槛从
# 「一档都不全过」压到 0.4，粗排 Top-20 命中率 5/20 → 20/20。
#
# **这条路学不来那一手**，别去"对齐"：
#   * 画布是 ONNX 图的固定输入形状 (1,3,640,640)，改常量只会让 onnxruntime 报形状
#     不匹配；真要 1280 得用新的 canvas 重新导出模型，且描述子与全库不可比 —— 那是
#     一次重建全库，不是调一个数。
#   * 关键点数同理烘在图的输出形状 (1,512,...) 里，所以这条路没有"查询侧预算"这个
#     旋钮 —— 这正是 `backend.xfeat_backend` **不**设 `_extract_query` 的原因。
#   * `canvas_size` 把有效区长边**恒等于**归一到 CANVAS：大帧缩下来、小帧放大上去
#     （`scale = CANVAS / max(h, w)` 没有夹到 1）。所以客户端发多大的帧对这条路毫无
#     影响 —— 抬 `Frames.LONG_EDGE` 只有 ORB 那条路吃得到。
#
# 已知的坑（留给真要上 XFeat 的那天）：ORB 那个尺度失配 —— 入库时照片铺满画面、
# 手持时只占一小块 —— 对 XFeat 同样存在，只是学出来的特征比 ORB 抗一些。所以别把
# 「换成 XFeat」当成召回问题的解药，它的对应修法是重导一个更大画布的模型。
CANVAS = 640

# 关键点上限，与导出时的 top_k 一致。改这里没有用：图的输出形状是 (1, 512, ...)，
# 这个常量只是让调用方知道上限是多少。
TOP_K = 512

DESC_DIM = 64

MODEL_FILENAME = "xfeat.onnx"


class ModelMissing(RuntimeError):
    """模型文件不在。

    信息里必须写清楚去哪儿取 —— 这是部署时最常见的一条失败，而"文件不存在"
    本身不告诉用户任何可行动的东西。
    """


class XFeatUnavailable(RuntimeError):
    """onnxruntime 装不上或模型加载失败。调用方应当回退到 ORB 后端。"""


def canvas_size(h: int, w: int) -> tuple[int, int]:
    """原始尺寸 (h, w) → 缩放后的**有效区**尺寸 (nh, nw)。

    单独一个函数而不是留在 `prepare` 里，因为这个公式现在有**三份实现**：这里、
    Android 侧的 `XFeatPreprocess`、以及 `POST /v1/recognize/features` 收下端上
    关键点时用来验"坐标有没有落在有效区里"的那道检查。三份不一致不会报错，只会让
    关键点被判在补边区（或者反过来放过一个补边全错的客户端），所以服务端这两处
    至少要共用同一个名字。

    这个函数对"客户端已经先缩过一次"是**不敏感**的：只要长宽比没变，
    `canvas_size(720, 1280)` 与 `canvas_size(360, 640)` 都是 (360, 640)。所以那道
    坐标检查允许客户端上报原始帧尺寸或已缩过的尺寸，两者都对得上。
    """
    scale = CANVAS / max(h, w)
    nh = max(1, min(CANVAS, int(round(h * scale))))
    nw = max(1, min(CANVAS, int(round(w * scale))))
    return nh, nw


def prepare(img_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按契约把一张 BGR 图变成 (image, size) 两个输入张量。

    返回的 image 是 (1,3,CANVAS,CANVAS) float32，size 是 (2,) int64。
    """
    if img_bgr.ndim == 2:
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    h, w = rgb.shape[:2]
    scale = CANVAS / max(h, w)
    if scale < 1.0:
        interp = cv2.INTER_AREA
    else:
        interp = cv2.INTER_LINEAR
    nh, nw = canvas_size(h, w)
    small = cv2.resize(rgb, (nw, nh), interpolation=interp)

    if nh < CANVAS or nw < CANVAS:
        # BORDER_REFLECT_101 要求补边宽度 < 对应边长，1 像素的图会违反这条；
        # 这种图本来也提不出特征，直接退回 REPLICATE 不影响任何真实用例。
        border = (
            cv2.BORDER_REFLECT_101
            if nh > 1 and nw > 1 and CANVAS - nh < nh and CANVAS - nw < nw
            else cv2.BORDER_REPLICATE
        )
        canvas = cv2.copyMakeBorder(
            small, 0, CANVAS - nh, 0, CANVAS - nw, border
        )
    else:
        canvas = small

    nchw = np.ascontiguousarray(
        canvas.transpose(2, 0, 1)[None].astype(np.float32)
    )
    return nchw, np.array([nh, nw], np.int64)


def decode(
    keypoints: np.ndarray, descriptors: np.ndarray, scores: np.ndarray
) -> Features:
    """把图的三个输出裁成有效部分。

    `scores <= 0` 的槽位是填充（有效峰值不足 TOP_K 时补上的），坐标是 topk 在等值
    上的任意选择，**必须丢掉**。这与官方实现的 `valid = scores > 0` 是同一条规则。
    """
    valid = scores.reshape(-1) > 0
    pts = np.ascontiguousarray(keypoints.reshape(-1, 2)[valid], np.float32)
    desc = np.ascontiguousarray(descriptors.reshape(-1, DESC_DIM)[valid], np.float32)
    return Features(pts=pts, desc=desc)


def default_model_path() -> Path:
    """模型的默认位置：`$PHOTOAR_MODELS/xfeat.onnx`，退回 `$PHOTOAR_DATA/models/`。

    两级是为了让容器里只需要挂一个数据卷：模型跟着数据走（它是几 MB 的运行时资产，
    不进镜像 —— 镜像里带模型会让每次发版都重传一遍，也让"换模型"必须重建镜像）。
    """
    explicit = os.environ.get("PHOTOAR_MODELS")
    if explicit:
        return Path(explicit) / MODEL_FILENAME
    data = os.environ.get("PHOTOAR_DATA", "data")
    return Path(data) / "models" / MODEL_FILENAME


class XFeatExtractor:
    """一个进程内共享的 ONNX 会话。

    **线程安全**：`onnxruntime.InferenceSession.run` 本身是线程安全的，但这里仍然
    加了一把锁。理由是服务端是多线程的（每个请求一个线程），而 ORT 的 intra-op 线程
    池是会话级共享的：多个请求同时 run 会各自申请 3 个线程，在 `--cpus=3` 的配额下
    互相抢占，实测比串行更慢。锁把推理串行化，让线程池的并行度真正落在配额上。
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        intra_threads: int | None = None,
    ) -> None:
        path = Path(model_path) if model_path else default_model_path()
        if not path.is_file():
            raise ModelMissing(
                f"XFeat 模型不在 {path}。\n"
                "取法二选一：\n"
                "  1) 从本项目的 GitHub release 下载 xfeat.onnx 放进去；\n"
                '  2) 自己导出：pip install -e ".[export]" '
                "&& python tools/export_models.py --out <该目录>\n"
                "或者把识别后端切回 orb（web 管理台 → 识别设置）。"
            )
        try:
            import onnxruntime as ort
        except ImportError as e:  # pragma: no cover - 取决于部署环境
            raise XFeatUnavailable(
                "没有 onnxruntime，装不了 XFeat 后端：pip install onnxruntime"
            ) from e

        opts = ort.SessionOptions()
        # 显式设线程数。默认会按物理核数开，而容器里 `--cpus=3` 限的是 CPU 时间
        # 配额、不是可见核数 —— 默认值会开出远超配额的线程，实测比设成配额更慢。
        opts.intra_op_num_threads = int(intra_threads or _default_threads())
        opts.inter_op_num_threads = 1
        try:
            self._sess = ort.InferenceSession(
                str(path), opts, providers=["CPUExecutionProvider"]
            )
        except Exception as e:  # pragma: no cover
            raise XFeatUnavailable(f"加载 {path} 失败：{e}") from e
        self._lock = threading.Lock()
        self.model_path = path

    def extract(self, img_bgr: np.ndarray) -> Features:
        image, size = prepare(img_bgr)
        with self._lock:
            kpts, desc, scores = self._sess.run(
                None, {"image": image, "size": size}
            )
        return decode(kpts, desc, scores)


def cpu_quota() -> int | None:
    """容器的 CPU 配额（向下取整的核数），读不到返回 None。

    ⚠️ **cgroup v1 与 v2 都要读。** 这一条是实测踩出来的，不是防御性编程：
    只读 v2 的 `/sys/fs/cgroup/cpu.max` 时，在一台 cgroup v1 的宿主机上这个函数会
    静默落到 `sched_getaffinity`，返回**宿主机**的核数（实测 16），于是 ORT 在
    `--cpus=3.0` 的配额下开了 16 个 intra-op 线程 —— 正是这段代码存在的目的所要
    避免的那件事。而它不报错、不打日志，只表现为"推理比预期慢"。

    v1 与 v2 的布局完全不同，两条都得写出来：
      v2: `/sys/fs/cgroup/cpu.max`            内容是 "300000 100000"（配额 空格 周期），
                                              未限制时配额那一栏是字面量 "max"
      v1: `/sys/fs/cgroup/cpu/cpu.cfs_quota_us` 与 `cpu.cfs_period_us` 两个文件，
                                              未限制时 quota 是 **-1**
    v1 的路径还有一种变体：有些运行时把 cpu 控制器挂在
    `/sys/fs/cgroup/cpu,cpuacct/` 下面，所以两个都试。

    QNAP QTS 上的 Container Station 是 cgroup v1（内核 5.10 一线的默认），也就是说
    **目标机器正好落在原来那条读不到配额的路径上**。
    """
    # cgroup v2
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
        return None  # 明确说了不限制，别再往下猜
    except (OSError, ValueError):
        pass
    # cgroup v1
    for base in ("/sys/fs/cgroup/cpu", "/sys/fs/cgroup/cpu,cpuacct"):
        try:
            quota = int(Path(f"{base}/cpu.cfs_quota_us").read_text().strip())
            period = int(Path(f"{base}/cpu.cfs_period_us").read_text().strip())
            if quota > 0 and period > 0:
                return max(1, int(quota / period))
        except (OSError, ValueError):
            continue
    return None


def _default_threads() -> int:
    """容器配额优先，其次 CPU 亲和性，最后核数。

    `os.cpu_count()` 在容器里返回**宿主机**核数（16），而配额可能只有 3 —— 按 16
    开线程会让每个线程只拿到 1/5 的时间片，上下文切换的开销白付。
    """
    quota = cpu_quota()
    if quota is not None:
        return quota
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # pragma: no cover - 非 Linux
        return max(1, os.cpu_count() or 1)
