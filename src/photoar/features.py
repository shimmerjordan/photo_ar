"""ORB 特征提取。

本模块是全项目特征参数的唯一来源。其他模块 import 这里的常量，
不得复制字面量——两处不一致会让识别率静默腰斩。

尺度对齐硬约束：入库与查询都必须先经 resize_to_long_edge。
ORB 不具备尺度不变性，两侧分辨率不一致时召回率会大幅下降。
"""

from dataclasses import dataclass

import cv2
import numpy as np

LONG_EDGE = 640
N_FEATURES = 300
SCALE_FACTOR = 1.2
N_LEVELS = 8

DESC_BYTES = 32  # ORB 描述子固定 256 bit


@dataclass(frozen=True)
class Features:
    """一张图的 ORB 特征。pts 与 desc 的第 0 维一一对应。"""

    pts: np.ndarray  # (N, 2) float32，坐标在缩放后的图像坐标系里
    desc: np.ndarray  # (N, 32) uint8

    def __len__(self) -> int:
        return int(self.desc.shape[0])


def resize_to_long_edge(img: np.ndarray, long_edge: int = LONG_EDGE) -> np.ndarray:
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest == long_edge:
        return img
    scale = long_edge / longest
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return cv2.resize(img, (new_w, new_h), interpolation=interp)


def _detector(n_features: int) -> "cv2.ORB":
    return cv2.ORB_create(
        nfeatures=n_features,
        scaleFactor=SCALE_FACTOR,
        nlevels=N_LEVELS,
    )


def extract(
    img_bgr: np.ndarray,
    long_edge: int = LONG_EDGE,
    n_features: int = N_FEATURES,
) -> Features:
    small = resize_to_long_edge(img_bgr, long_edge)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small

    kps, desc = _detector(n_features).detectAndCompute(gray, None)
    if desc is None or len(kps) == 0:
        return Features(
            pts=np.zeros((0, 2), np.float32),
            desc=np.zeros((0, DESC_BYTES), np.uint8),
        )

    # 显式按 response 降序取前 n_features 个：ORB 的 nfeatures 只是目标值，
    # 这里把"取最强的 N 个"变成本模块的确定性契约。
    responses = np.array([k.response for k in kps], np.float32)
    order = np.argsort(-responses, kind="stable")[:n_features]
    pts = np.array([kps[i].pt for i in order], np.float32).reshape(-1, 2)
    return Features(pts=pts, desc=np.ascontiguousarray(desc[order]))
