"""`POST /v1/recognize/features` 的请求体解析与校验。

端上（Android）跑与服务端**同一份** ONNX 图，把关键点和描述子直接传上来，服务端
因此完全不做推理。动机是实测数字，不是推测：3 CPU / 3GB 下 XFeat 的识别延迟
p50 800ms / p95 1101ms（ORB 是 64ms）。拆开看，提特征只占 26–30ms，**配对占
490ms**（20 个候选）—— 也就是说搬走推理省的是那 26–30ms 加上整个 ONNX 会话的内存，
而目标机 N5095 没有 AVX/AVX2（只到 SSE4.2），实测会更慢。真正的收益在上传量：
一张 JPEG 约 50KB → 512×64 的描述子（132KB 原始、base64 后约 180KB）……

⚠️ 上传量其实是**变大**的。这条路值得做的理由是另外两条：手机的大核比 N5095 强得
多（推理挪过去是净赚），以及服务端不必再常驻一个 ONNX 会话。带宽在局域网里不是瓶颈，
所以这个取舍是清楚的 —— 但别把它写成"省流量"，那是错的。

## 为什么校验这么严

这条路上每一种错都是**静默**的：描述子对不上不会抛异常，只会让识别率变低；而"识别
率变低"在一个家用部署里几乎不可能被归因到字节序或归一化上。所以凡是能在收下之前判
出来的一律判掉，并把原因如实写进 400 的响应体 —— 那条响应是客户端作者唯一的线索。
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import numpy as np

from .. import xfeat
from ..features import Features

# 每行描述子的 L2 范数允许偏离 1 多少。
#
# 取 1e-2 而不是 1e-6：ONNX 图里那步 `F.normalize` 是 float32 的，客户端把 64 个
# float32 摊平再 base64 回来是**精确**的（没有精度损失），所以真实偏差只来自
# normalize 自身的累加误差，量级 1e-7。而一个"没归一化"的描述子（原始特征图取值）
# 范数离 1 有好几个数量级，字节序搞反的 float32 更是直接飞到 1e30 或 NaN。
# 也就是说 1e-2 这条线两侧的间距有 4 个数量级以上，不存在"刚好卡在边界"的情形。
NORM_TOLERANCE = 1e-2

# 关键点坐标允许超出有效区多少像素。
#
# 图里那道 inside 掩码用的是整数比较（`cols < valid_w`），所以合法坐标最大是
# nw-1。给 1.0 的余量是为了吸收**客户端与服务端算 (nh, nw) 时的 ±1 舍入差**
# （两边都在算 round(h * 640 / max(h,w))，浮点在正好 .5 的边界上可能分道扬镳）。
#
# 这点余量不影响这道检查要抓的那些错：四边都补边会让坐标偏移几十到几百像素，
# 完全不缩放会让坐标大到上千，都远在余量之外。
COORD_SLACK = 1.0

# 上报的帧尺寸上限。手机相机原图最大约 8000×6000；给到 20000 只是挡住"负数/零/
# 一个天文数字"这三类明显非法值，真实值多大都无所谓（它只参与算有效区比例）。
MAX_FRAME_EDGE = 20_000


class FeaturesRejected(ValueError):
    """请求体不符合契约。HTTP 层整类映射到 400。

    `code` 分得比"bad_request"细，因为客户端作者要靠它区分"我少传了一个字段"和
    "我的字节序反了"—— 后者只看 message 也能懂，但机器可读的 code 让端上能针对
    特定错误做回退（见 Android 侧的 `FeaturePathPolicy`：拿到 `unsupported_backend`
    要记住别再试了，拿到 `bad_descriptors` 是自己的 bug，两种处理不一样）。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _int_field(doc: dict[str, Any], name: str) -> int:
    raw = doc.get(name)
    # 显式排除 bool：`isinstance(True, int)` 是 True，而 `{"width": true}` 应该被
    # 当成非法输入而不是 width=1。
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise FeaturesRejected("bad_size", f"{name} 必须是整数，收到 {raw!r}")
    if not (1 <= raw <= MAX_FRAME_EDGE):
        raise FeaturesRejected(
            "bad_size", f"{name}={raw} 不在 1..{MAX_FRAME_EDGE} 之内"
        )
    return raw


def _decode_floats(doc: dict[str, Any], name: str, per_row: int) -> np.ndarray:
    """取一个 base64 的 float32 小端数组，reshape 成 (N, per_row)。"""
    raw = doc.get(name)
    # 空串是**合法**的（0 个关键点），字段缺失不是。
    #
    # 分开的理由：对着一面白墙确实提不出关键点，那是"未命中"这个正常状态，不该变成
    # 400 让端上以为自己的管线坏了（进而触发回退，从此不再走这条路）。而字段整个
    # 没有是客户端的 bug，必须报出来 —— 如果两者都当 0 个特征处理，一个把字段名拼错
    # 的客户端会永远收到"未命中"，然后去查照片和阈值。
    if raw is None or not isinstance(raw, str):
        raise FeaturesRejected(
            "missing_field",
            f"需要 {name}（base64 编码的 float32 小端数组；0 个特征传空串）",
        )
    if not raw:
        return np.zeros((0, per_row), np.float32)
    try:
        # validate=True：默认的 b64decode 会**静默丢掉**字母表之外的字符，于是一段
        # 被截断或混入了 JSON 转义的载荷可能仍然解出一个长度刚好对得上的数组，
        # 然后被当成有效描述子用下去。这正是要避免的那种静默失败。
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FeaturesRejected("bad_base64", f"{name} 不是合法 base64：{exc}") from exc

    stride = per_row * 4
    if len(blob) % stride:
        raise FeaturesRejected(
            "bad_length",
            f"{name} 解出 {len(blob)} 字节，不是每行 {per_row} 个 float32"
            f"（{stride} 字节）的整数倍",
        )
    # dtype 显式写 "<f4"：契约说的是小端。用 np.float32 会跟着**服务端**的字节序走，
    # 在一台大端机器上会把每个 float 的字节读反 —— 而那台机器上不会有任何报错，
    # 只有识别率归零。
    return np.frombuffer(blob, dtype="<f4").reshape(-1, per_row)


def parse(doc: dict[str, Any]) -> Features:
    """请求体 → `Features`（坐标在 640×640 画布坐标系里，与 ORB 路径同一约定）。

    @raises FeaturesRejected 任何一条不符合契约。
    """
    height = _int_field(doc, "height")
    width = _int_field(doc, "width")

    pts = _decode_floats(doc, "keypoints", 2)
    desc = _decode_floats(doc, "descriptors", xfeat.DESC_DIM)

    if len(pts) != len(desc):
        raise FeaturesRejected(
            "count_mismatch",
            f"关键点 {len(pts)} 个、描述子 {len(desc)} 个，必须一一对应",
        )
    if len(pts) > xfeat.TOP_K:
        # 上限不是防御性的：库里每个 slot 就是 TOP_K 行（见 backend.XFEAT_LAYOUT），
        # 而互近邻的成本随点数超线性增长（512 点 0.46ms、4096 点 116.7ms）。收下更多
        # 点只会让一次查询把预算烧光。
        raise FeaturesRejected(
            "too_many",
            f"关键点 {len(pts)} 个超过上限 {xfeat.TOP_K}",
        )

    # 非有限值先挡掉。放在范数检查之前，因为 NaN 会让范数检查里的比较全部为 False ——
    # 也就是"检查通过"。而 NaN 描述子进了互近邻之后同样静默：与 NaN 的比较恒假，
    # argmax 会挑出一个任意下标，于是配对结果是随机的。
    if not np.isfinite(pts).all():
        raise FeaturesRejected("bad_keypoints", "关键点里有 NaN 或 Inf")
    if not np.isfinite(desc).all():
        raise FeaturesRejected(
            "bad_descriptors",
            "描述子里有 NaN 或 Inf（最常见的原因是 float32 的字节序反了）",
        )

    _check_norms(desc)
    _check_bounds(pts, height, width)

    # 必须真的 copy：`np.frombuffer` 给的是**只读**视图，而 `ascontiguousarray` 对
    # 一个本来就连续的数组是空操作 —— 只读属性会一路带到下游，然后在某个原地操作上
    # 抛 "assignment destination is read-only"。那个异常离这里很远，看起来像 numpy 的
    # 内部问题。在入口把所有权弄干净比在下游到处判 writeable 便宜。
    return Features(
        pts=np.array(pts, np.float32, copy=True, order="C"),
        desc=np.array(desc, np.float32, copy=True, order="C"),
    )


def _check_norms(desc: np.ndarray) -> None:
    """每行必须已 L2 归一化。**选择拒绝，而不是在服务端重新归一化。**

    理由是"能不能修得对"，不是"哪个更礼貌"：

    - 导出的 ONNX 图最后一步就是 `F.normalize(desc, dim=-1)`，所以一个真正在跑这份
      契约的客户端**不可能**产出非单位范数的行。范数不对 = 它没在跑这份图，或者
      dtype/字节序/行列步长解错了。
    - 重新归一化治不了那些病。归一化一个解错的缓冲区，只会得到一批"看起来合法、
      内容是垃圾"的单位向量 —— 它们会正常参与余弦互近邻，正常通过或不通过那道 0.82
      的闸门，最后表现为识别率莫名偏低。这恰恰是这个模块存在的意义所要消灭的失败
      方式：把"客户端管线坏了"变成"识别效果不太好"。
    - 400 + 具体行号和范数，是客户端作者能直接下手的信息。

    代价：真有客户端故意传未归一化的描述子会被拒。那不是代价，那是本意 ——
    `match.mnn_matches` 的文档里写着"没归一化的话内积不是余弦，那道闸门会静默
    失效"，我们不该把那句话变成一条服务端偷偷兜住的路。
    """
    # 在 float64 上算范数。不是为了精度：一个字节序反了的 float32 缓冲区里全是 1e30
    # 量级的数，在 float32 上平方会溢出成 inf 并打出一条 RuntimeWarning —— 那条警告会
    # 出现在**服务端日志**里（而不是响应里），看起来像服务端自己算错了。
    norms = np.linalg.norm(desc.astype(np.float64), axis=1)
    bad = np.flatnonzero(np.abs(norms - 1.0) > NORM_TOLERANCE)
    if bad.size == 0:
        return
    first = int(bad[0])
    raise FeaturesRejected(
        "bad_descriptors",
        f"描述子必须逐行 L2 归一化（余弦互近邻的 0.82 闸门依赖这一点，"
        f"不归一化它会静默失效）。共 {bad.size} 行不合格，"
        f"第 {first} 行的范数是 {float(norms[first]):.6g}，"
        f"允许偏离 {NORM_TOLERANCE}。",
    )


def _check_bounds(pts: np.ndarray, height: int, width: int) -> None:
    """关键点必须落在有效区内。

    这是服务端唯一能抓到**客户端预处理写歪了**的检查，所以它值得存在：图内那道
    inside 掩码保证了合法关键点都在 [0, nw) × [0, nh) 里，于是坐标越界就等于
    "补边补错了方向"（四边都补 → 内容整体右下平移）、"没缩到长边 640"（坐标到上千）
    或"xy 反了"（竖图上立刻越界）。这三种都不会在别处报错。
    """
    if len(pts) == 0:
        return
    nh, nw = xfeat.canvas_size(height, width)
    xs, ys = pts[:, 0], pts[:, 1]
    lo = -COORD_SLACK
    if xs.min() < lo or ys.min() < lo:
        raise FeaturesRejected(
            "bad_keypoints",
            f"关键点有负坐标（x 最小 {float(xs.min()):.1f}、"
            f"y 最小 {float(ys.min()):.1f}）",
        )
    if xs.max() > nw + COORD_SLACK or ys.max() > nh + COORD_SLACK:
        raise FeaturesRejected(
            "bad_keypoints",
            f"关键点越出有效区：上报帧 {width}×{height} 对应有效区 {nw}×{nh}，"
            f"但收到 x 最大 {float(xs.max()):.1f}、y 最大 {float(ys.max()):.1f}。"
            f"最可能的原因是端上预处理没按契约做（补边应当只补右侧与下方，"
            f"且先缩到长边 {xfeat.CANVAS}）。",
        )


def encode(features: Features, height: int, width: int) -> dict[str, Any]:
    """反向：`Features` → 请求体。给测试与 `tools/` 里的探测脚本用。

    与 `parse` 放在一起是为了让两者的字段名、dtype、字节序只有一处定义 —— 分开写
    的话，测试里那份编码器会跟着解析器一起被改对，于是一个真实客户端才会踩的错
    （比如字节序）测不出来。
    """
    return {
        "width": int(width),
        "height": int(height),
        "keypoints": base64.b64encode(
            np.ascontiguousarray(features.pts, "<f4").tobytes()
        ).decode("ascii"),
        "descriptors": base64.b64encode(
            np.ascontiguousarray(features.desc, "<f4").tobytes()
        ).decode("ascii"),
    }
