import cv2
import numpy as np
import pytest


@pytest.fixture
def textured_image():
    """程序化生成高纹理图，保证 ORB 能稳定找到角点；不同 seed 产出可区分的图。

    不使用真实照片，测试才能确定性、可提交、不依赖用户隐私数据。
    """

    def _make(seed: int = 0, w: int = 1200, h: int = 800) -> np.ndarray:
        rng = np.random.default_rng(seed)
        # 低分辨率噪声上采样 -> 大尺度纹理
        base = rng.integers(0, 256, (max(2, h // 8), max(2, w // 8), 3), dtype=np.uint8)
        img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
        # 叠加高对比度矩形 -> 稳定角点
        for _ in range(40):
            x1 = int(rng.integers(0, w))
            y1 = int(rng.integers(0, h))
            x2 = min(w - 1, x1 + int(rng.integers(20, 120)))
            y2 = min(h - 1, y1 + int(rng.integers(20, 120)))
            color = tuple(int(c) for c in rng.integers(0, 256, 3))
            cv2.rectangle(img, (x1, y1), (x2, y2), color, -1)
        return img

    return _make
