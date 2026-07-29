# photo-ar Phase 0（识别管线离线验证）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立纯离线的照片识别管线与合成查询图回归测试，回答唯一的生死问题——上万张高度自相似的家庭照片，能否以「正确命中 ≥95%、误识别 ≤0.1%」被区分。

**Architecture:** 两阶段检索。粗排用二进制层次词汇树（k-majority）+ TF-IDF 倒排索引取 Top-20；精排对候选做 ORB 暴力汉明匹配 + RANSAC 单应矩阵，按三条判定决定是否命中。为了让粗排的召回率有可比基线，先实现**不含任何词汇表变量的暴力检索**（对全库逐个几何校验），它同时是最硬的 go/no-go 参考点。测试数据由合成查询图生成器程序化产出（透视/模糊/亮度/色温/高光/JPEG 压缩），全流程不需要真机、不需要网络、不需要 NAS。

**Tech Stack:** Python 3.10+、opencv-python、numpy、pytest。外部命令行工具：`arcoreimg`（ARCore SDK）、`ffmpeg`/`ffprobe`。

**本机实测环境（2026-07-28）：** Python 3.10.0、cv2 5.0.0、numpy 2.0.0 已全局可用。使用 `python3 -m venv --system-site-packages .venv` 建虚拟环境复用已装好的 cv2/numpy，再 `.venv/bin/pip install -e . --no-deps` 装本包。所有 `python`/`pytest`/`photoar` 命令都用 `.venv/bin/` 下的。`.venv/` 已在 `.gitignore` 里。

## Global Constraints

- **依赖白名单**：仅 `opencv-python`（或 headless 变体）、`numpy`、`pytest`。**禁止**引入 scipy、scikit-learn、torch、Pillow 等。需要的算法自己实现（词汇树、倒排索引、popcount）。
- **尺度对齐是硬约束**：入库提特征与查询提特征都必须先把图缩到长边 `LONG_EDGE = 640`。ORB 无尺度不变性，两侧不一致会让召回率腰斩。任何绕过 `resize_to_long_edge` 的代码路径都是 bug。
- **特征参数**（集中定义在 `src/photoar/features.py`，其他模块只 import，不得复制字面量）：`LONG_EDGE = 640`、`N_FEATURES = 300`、`SCALE_FACTOR = 1.2`、`N_LEVELS = 8`。
- **判定阈值**（集中定义在 `src/photoar/verify.py`）：`MIN_INLIERS = 25`、`DET_MIN = 0.05`、`DET_MAX = 20.0`、`RATIO = 1.5`、`RANSAC_REPROJ = 3.0`。
- **RANSAC 迭代上限**：`RANSAC_MAX_ITERS = 200`（2026-07-28 实测后加，见下）。估计器仍是 `cv2.RANSAC`。
- **一切随机性必须可复现**：只用 `numpy.random.default_rng(seed)`，禁止 `random` 模块的全局状态、禁止无 seed 的随机。测试必须是确定性的。
- **误识别率优先于漏检率**（spec §14.2）：调参时两者冲突，一律牺牲漏检保误识别。任何降低判定严格度的改动都要在提交信息里说明对误识别率的影响。
- **Phase 0 不引入 SQLite**。产物是文件（描述子库、索引、`.imgdb`、JSON manifest）。数据库是 Phase 1 随服务一起引入的。
- **Phase 0 不写任何服务、不写任何 Android 代码。**
- **git**：仓库已于 2026-07-28 初始化（默认分支 `main`），commit 步骤正常执行。**只本地 commit，永不 push**。commit message 里不得出现 `Co-Authored-By` 之类的署名。若 git 报缺少身份，用 `git -c user.name=xyz -c user.email=<你的邮箱> commit ...`。

## 与 spec 的偏离（已确认）

1. **spec §8.2 说先用 ORB-SLAM 现成 ORB 词汇表，不要自训。本计划改为：先做暴力检索基线（Task 5），再自训小词汇表（Task 6）。**
   理由：spec 的顾虑是"自训引入新变量导致归因困难"，而暴力检索连词汇表这个变量都不存在，是比借来的词汇表**更硬**的参考点，且它直接产出最重要的指标（误识别率）。有了暴力基线的 ground truth，Task 7 的粗排召回率才有可比对象。`ORBvoc.txt`（145MB 文本、1M 节点）保留为词汇表召回不达标时的备选，接口按可替换设计。
2. **spec §6 说描述子 300×32 = 9600 字节/张、1 万张 96MB。这个数字漏算了关键点坐标。** RANSAC 需要关键点坐标，每张还要 300×2×float32 = 2400 字节。实际每张 12008 字节（含头），**1 万张约 120MB**，不是 96MB。仍在预算内，但 spec §8.4 的数字应按 120MB 更新。
3. **spec §8.3 说 `findHomography(..., RANSAC, 3.0)`。实测后给它加了 `maxIters=RANSAC_MAX_ITERS`（200）的上限。** 估计器仍是 `cv2.RANSAC`，判定条件一个都没改。详见下节。

## RANSAC 迭代上限降到 200（2026-07-28 实测，已获用户裁决）

里程碑 0a 实测每次 `verify_pair` 约 21.7 ms，两阶段 Top-20 精排推算 434 ms，而 spec §8.4 的服务端目标是 80 ms。profile 之后发现瓶颈**不在** `BFMatcher`：

| 组件 | 耗时 |
|---|---|
| `BFMatcher(NORM_HAMMING, crossCheck=True)` 300×300 描述子 | 0.17 ms |
| `findHomography(..., cv2.RANSAC, 3.0)` | **20.74 ms** ← 占 99% |

**但真正的关键洞察是：那 20 ms 只花在假匹配上。真匹配恒定约 0.34 ms。** RANSAC 是自适应终止的 —— 一旦找到好模型就停，只有在**根本不存在好模型**（即照片对不匹配）时才会烧满默认的 2000 次迭代。两阶段 Top-20 里有 19 个是假匹配，390 ms 全在这 19 次上。

12 对真匹配 + 12 对假匹配的实测：

| `maxIters` | 真匹配内点（12 对） | 真匹配 | 假匹配 | 误判 |
|---|---|---|---|---|
| 2000（默认） | 110,140,93,171,115,87,211,98,108,112,108,93 | 0.34 ms | **20.56 ms** | 0/12 |
| 500 | **完全相同** | 0.33 ms | 5.23 ms | 0/12 |
| **200** | **完全相同** | 0.33 ms | **2.19 ms** | 0/12 |
| 100 | **完全相同** | 0.30 ms | 1.25 ms | 0/12 |
| 50 | **完全相同** | 0.33 ms | 0.70 ms | 0/12 |

降到 200：真匹配内点数逐个完全不变，假匹配内点数略降（更安全的方向），误判仍为 0，Top-20 精排从 390 ms 降到约 42 ms。**拒绝机制完全不变，所以现有 12 个测试全部照旧通过，里程碑 0a 也不必重跑。**

### 为什么没选 `USAC_ACCURATE`

`USAC_ACCURATE` 更快（Top-20 约 5 ms，真匹配内点差 ≤2，12/12 通过），但它**在估计器层面就拒绝纯反射** —— 镜像构造下返回 `inliers=0, det=0.0`，不产出反射矩阵。这会让 Task 3 的 `test_mirrored_match_is_rejected_by_determinant_sign_not_inlier_count` 失效，也就是把那个专门用来防 `abs(det)` 回归的守卫拆掉。加上它改变了拒绝机制（假匹配的 det 多为 0.0/-0.0/-877，靠 `DET_MIN` 而非内点数挡住），意味着 0a 必须重跑。42 ms 已达标，不值得付这个代价。

### `maxIters` 不是完全免费的

它限制的是**难例上花多少力气**。RANSAC 对 4 点模型需要约 `log(1-p)/log(1-w⁴)` 次迭代（`w` 为内点率）：200 覆盖约 `w ≥ 0.37`，500 覆盖约 `w ≥ 0.29`。真实照片里严重变形的边缘真匹配可能落在 0.3 附近，会变成漏检。这是可接受的方向（漏检代价比误识别低一个量级），而**里程碑 0d 是判断 200 是否太紧的依据**。若 0d 漏检偏多，就抬这个数 —— 而抬它只在假匹配上花时间。

## 词表粒度取舍数据（供 Task 8 / 里程碑 0b，不要据此现在拍阈值）

合成查询图的视觉词落在**源图**词表里的比例：

| 配置 | 词数 | 重叠均值 | 最小 |
|---|---|---|---|
| `branching=6, depth=3` | 216 | 0.743 | 0.660 |
| `branching=10, depth=4`（模块默认） | 5338 | **0.269** | 0.156 |

词表越细越有区分度，但越不抗噪。0.269 够不够让 TF-IDF 把正确照片排到第一，正是里程碑 0b 要实测回答的 —— IDF 加权很可能能扛住，因为共享的那些词恰好是稀有词。**若 0b 召回率不佳，第一个该动的旋钮是把词表调粗（降 `BRANCHING`/`DEPTH`），而不是加大 `TOP_K`。**

## 文件结构

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 包元数据、依赖、pytest 配置 |
| `src/photoar/features.py` | ORB 提取与尺度归一化。全项目特征参数的唯一来源 |
| `src/photoar/synth.py` | 合成查询图生成（把参考图变成"翻拍"风格） |
| `src/photoar/verify.py` | 单对几何校验 + 三条命中判定。全项目判定阈值的唯一来源 |
| `src/photoar/descstore.py` | 定长描述子/关键点文件，mmap 随机读 |
| `src/photoar/bruteforce.py` | 暴力检索器（无词汇表变量的参考实现） |
| `src/photoar/evaluate.py` | 指标计算与报告（正确命中/误识别/漏检/延迟） |
| `src/photoar/vocab.py` | 二进制层次 k-majority 词汇树：训练、序列化、量化 |
| `src/photoar/index.py` | TF-IDF 倒排索引，粗排 Top-K |
| `src/photoar/recognizer.py` | 两阶段编排（粗排 → 精排 → 判定） |
| `src/photoar/quality.py` | `arcoreimg` 封装：`eval-img` 质量分、`build-db` 生成 `.imgdb` |
| `src/photoar/transcode.py` | `ffprobe`/`ffmpeg` 封装：探测、判断是否需转码、转码 |
| `src/photoar/corpus.py` | 语料构建：从照片目录产出描述子库 + 索引 + manifest |
| `src/photoar/cli.py` | `photoar build` / `photoar eval` / `photoar ingest` |
| `tests/conftest.py` | 程序化生成有纹理测试图的 fixture |
| `tests/test_*.py` | 每个模块一个测试文件 |

`features.py` 与 `verify.py` 是常量的唯一来源，被其他所有模块 import。这两个文件的改动会直接改变识别指标，因此它们的测试必须锁住行为契约。

---

### Task 1: 项目骨架 + ORB 特征提取与尺度归一化

**Files:**
- Create: `pyproject.toml`
- Create: `src/photoar/__init__.py`
- Create: `src/photoar/features.py`
- Create: `tests/conftest.py`
- Test: `tests/test_features.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `LONG_EDGE: int = 640`、`N_FEATURES: int = 300`、`SCALE_FACTOR: float = 1.2`、`N_LEVELS: int = 8`
  - `resize_to_long_edge(img: np.ndarray, long_edge: int = LONG_EDGE) -> np.ndarray`
  - `Features` dataclass，字段 `pts: np.ndarray`（shape `(N,2)` float32）、`desc: np.ndarray`（shape `(N,32)` uint8）
  - `extract(img_bgr: np.ndarray, long_edge: int = LONG_EDGE, n_features: int = N_FEATURES) -> Features`
  - fixture `textured_image` -> `Callable[[int, int, int], np.ndarray]`，签名 `_make(seed=0, w=1200, h=800)`

- [ ] **Step 1: 创建 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "photoar"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "opencv-python>=4.9",
    "numpy>=1.26",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
photoar = "photoar.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: 写失败的测试**

创建 `tests/conftest.py`：

```python
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
```

创建 `tests/test_features.py`：

```python
import cv2
import numpy as np

from photoar import features as F


def test_resize_keeps_aspect_and_hits_long_edge(textured_image):
    img = textured_image(seed=1, w=1200, h=800)
    out = F.resize_to_long_edge(img, 640)
    h, w = out.shape[:2]
    assert max(h, w) == 640
    assert abs((w / h) - (1200 / 800)) < 0.02


def test_resize_upscales_small_images(textured_image):
    img = textured_image(seed=2, w=300, h=200)
    out = F.resize_to_long_edge(img, 640)
    assert max(out.shape[:2]) == 640


def test_extract_shapes_and_dtypes(textured_image):
    f = F.extract(textured_image(seed=3))
    assert f.desc.ndim == 2 and f.desc.shape[1] == 32
    assert f.desc.dtype == np.uint8
    assert f.pts.shape == (f.desc.shape[0], 2)
    assert f.pts.dtype == np.float32
    assert 0 < f.desc.shape[0] <= F.N_FEATURES


def test_extract_is_deterministic(textured_image):
    img = textured_image(seed=4)
    a, b = F.extract(img), F.extract(img)
    assert np.array_equal(a.desc, b.desc)
    assert np.array_equal(a.pts, b.pts)


def test_extract_is_scale_normalized(textured_image):
    """尺度对齐硬约束：同一张图放大 2 倍后提取，描述子应与原图高度一致。

    这个测试锁住的是全项目最容易被违反、后果最严重的约束。
    """
    img = textured_image(seed=5, w=1000, h=700)
    big = cv2.resize(img, (2000, 1400), interpolation=cv2.INTER_LINEAR)

    fa, fb = F.extract(img), F.extract(big)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(fa.desc, fb.desc)
    good = [m for m in matches if m.distance <= 32]
    assert len(good) >= 0.5 * min(len(fa.desc), len(fb.desc))


def test_extract_handles_blank_image():
    blank = np.full((400, 600, 3), 128, np.uint8)
    f = F.extract(blank)
    assert f.desc.shape == (0, 32)
    assert f.pts.shape == (0, 2)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_features.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar'`

- [ ] **Step 4: 实现 `src/photoar/features.py`**

创建空的 `src/photoar/__init__.py`，然后：

```python
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
```

- [ ] **Step 5: 安装并运行测试确认通过**

Run: `pip install -e '.[dev]' && python -m pytest tests/test_features.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/photoar/__init__.py src/photoar/features.py tests/conftest.py tests/test_features.py
git commit -m "feat: ORB 特征提取与尺度归一化，锁定尺度对齐约束"
```

---

### Task 2: 合成查询图生成器

**Files:**
- Create: `src/photoar/synth.py`
- Test: `tests/test_synth.py`

**Interfaces:**
- Consumes: `photoar.features.extract`（仅测试中用）
- Produces:
  - `SynthParams` frozen dataclass，字段 `corner_jitter: float`、`blur_sigma: float`、`brightness: float`、`warm_shift: float`、`glare: bool`、`jpeg_quality: int`
  - `sample_params(rng: np.random.Generator) -> SynthParams`
  - `apply(img_bgr: np.ndarray, p: SynthParams) -> np.ndarray`
  - `generate(img_bgr: np.ndarray, count: int, seed: int) -> list[tuple[np.ndarray, SynthParams]]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_synth.py`：

```python
import cv2
import numpy as np

from photoar import features as F
from photoar import synth


def test_sample_params_in_documented_ranges():
    rng = np.random.default_rng(0)
    for _ in range(200):
        p = synth.sample_params(rng)
        assert 0.0 <= p.corner_jitter <= 0.25
        assert 0.0 <= p.blur_sigma <= 1.5
        assert 0.7 <= p.brightness <= 1.3
        assert -0.15 <= p.warm_shift <= 0.15
        assert 50 <= p.jpeg_quality <= 85
        assert isinstance(p.glare, bool)


def test_apply_preserves_shape_and_dtype(textured_image):
    img = textured_image(seed=1)
    p = synth.sample_params(np.random.default_rng(7))
    out = synth.apply(img, p)
    assert out.shape == img.shape
    assert out.dtype == np.uint8


def test_generate_is_reproducible(textured_image):
    img = textured_image(seed=2)
    a = synth.generate(img, count=5, seed=42)
    b = synth.generate(img, count=5, seed=42)
    assert len(a) == 5
    for (ia, pa), (ib, pb) in zip(a, b):
        assert np.array_equal(ia, ib)
        assert pa == pb


def test_generate_varies_between_seeds(textured_image):
    img = textured_image(seed=3)
    a = synth.generate(img, count=3, seed=1)
    b = synth.generate(img, count=3, seed=2)
    assert not np.array_equal(a[0][0], b[0][0])


def test_synthetic_query_still_matches_its_source(textured_image):
    """核心契约：合成图必须仍然可被匹配回源图，否则生成器过于激进，
    测出来的低召回率是生成器的问题而不是识别管线的问题。
    """
    img = textured_image(seed=4, w=1000, h=700)
    ref = F.extract(img)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

    inlier_counts = []
    for query_img, _ in synth.generate(img, count=10, seed=11):
        q = F.extract(query_img)
        if len(q) < 4 or len(ref) < 4:
            inlier_counts.append(0)
            continue
        matches = bf.match(q.desc, ref.desc)
        if len(matches) < 4:
            inlier_counts.append(0)
            continue
        src = q.pts[[m.queryIdx for m in matches]]
        dst = ref.pts[[m.trainIdx for m in matches]]
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
        inlier_counts.append(0 if H is None else int(mask.sum()))

    assert sum(c >= 25 for c in inlier_counts) >= 8


def test_glare_brightens_a_region(textured_image):
    img = np.full((400, 600, 3), 100, np.uint8)
    p = synth.SynthParams(
        corner_jitter=0.0, blur_sigma=0.0, brightness=1.0,
        warm_shift=0.0, glare=True, jpeg_quality=85,
    )
    out = synth.apply(img, p)
    assert int(out.max()) > 130
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_synth.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.synth'`

- [ ] **Step 3: 实现 `src/photoar/synth.py`**

```python
"""把参考图变成"手机翻拍打印照片"风格的查询图。

这是 Phase 0 的测试数据来源。真实拍摄测试集是最终验收基线，
但调参迭代必须靠这个——它无需真机、无需网络、完全确定性。

各扰动的现实对应：
  corner_jitter  四角随机位移占图宽/高的比例，模拟斜视角。0.25 约对应 40°
  blur_sigma     高斯模糊，模拟手抖与失焦
  brightness     整体亮度增益，模拟不同光照
  warm_shift     蓝/红通道反向增益，模拟色温偏移
  glare          椭圆高光斑，模拟覆膜反光
  jpeg_quality   JPEG 压缩，模拟客户端上传前的编码损失
"""

from dataclasses import dataclass

import cv2
import numpy as np

MAX_CORNER_JITTER = 0.25
MAX_BLUR_SIGMA = 1.5
BRIGHTNESS_RANGE = (0.7, 1.3)
MAX_WARM_SHIFT = 0.15
JPEG_QUALITY_RANGE = (50, 85)
GLARE_PROBABILITY = 0.35


@dataclass(frozen=True)
class SynthParams:
    corner_jitter: float
    blur_sigma: float
    brightness: float
    warm_shift: float
    glare: bool
    jpeg_quality: int


def sample_params(rng: np.random.Generator) -> SynthParams:
    return SynthParams(
        corner_jitter=float(rng.uniform(0.0, MAX_CORNER_JITTER)),
        blur_sigma=float(rng.uniform(0.0, MAX_BLUR_SIGMA)),
        brightness=float(rng.uniform(*BRIGHTNESS_RANGE)),
        warm_shift=float(rng.uniform(-MAX_WARM_SHIFT, MAX_WARM_SHIFT)),
        glare=bool(rng.random() < GLARE_PROBABILITY),
        jpeg_quality=int(rng.integers(JPEG_QUALITY_RANGE[0], JPEG_QUALITY_RANGE[1] + 1)),
    )


def _warp(img: np.ndarray, jitter: float, rng: np.random.Generator) -> np.ndarray:
    h, w = img.shape[:2]
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    offsets = rng.uniform(-jitter, jitter, size=(4, 2)) * np.float32([w, h])
    dst = (src + offsets).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(
        img, M, (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _glare(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    h, w = img.shape[:2]
    cx = int(rng.integers(w // 4, 3 * w // 4))
    cy = int(rng.integers(h // 4, 3 * h // 4))
    rx = int(rng.integers(w // 8, w // 3))
    ry = int(rng.integers(h // 8, h // 3))

    mask = np.zeros((h, w), np.float32)
    cv2.ellipse(mask, (cx, cy), (rx, ry), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=max(rx, ry) / 3.0)
    strength = float(rng.uniform(60.0, 140.0))

    out = img.astype(np.float32) + mask[:, :, None] * strength
    return np.clip(out, 0, 255).astype(np.uint8)


def apply(img_bgr: np.ndarray, p: SynthParams) -> np.ndarray:
    # 用参数自身派生 rng，让 apply 对同一 params 也是确定性的
    seed = abs(hash((p.corner_jitter, p.blur_sigma, p.brightness,
                     p.warm_shift, p.glare, p.jpeg_quality))) % (2**32)
    rng = np.random.default_rng(seed)

    out = img_bgr
    if p.corner_jitter > 0:
        out = _warp(out, p.corner_jitter, rng)
    if p.blur_sigma > 0:
        out = cv2.GaussianBlur(out, (0, 0), sigmaX=p.blur_sigma)

    f = out.astype(np.float32) * p.brightness
    if out.ndim == 3:
        # BGR：warm_shift > 0 偏暖（红增蓝减）
        f[:, :, 2] *= 1.0 + p.warm_shift
        f[:, :, 0] *= 1.0 - p.warm_shift
    out = np.clip(f, 0, 255).astype(np.uint8)

    if p.glare:
        out = _glare(out, rng)

    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), p.jpeg_quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def generate(
    img_bgr: np.ndarray, count: int, seed: int
) -> list[tuple[np.ndarray, SynthParams]]:
    rng = np.random.default_rng(seed)
    return [(apply(img_bgr, p), p) for p in (sample_params(rng) for _ in range(count))]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_synth.py -v`
Expected: 6 passed

若 `test_synthetic_query_still_matches_its_source` 失败（内点达标数 < 8），说明扰动过强。**降低 `MAX_CORNER_JITTER` 到 0.20 而不是放宽断言** —— 断言保护的正是"生成器不能强到连源图都匹配不上"这个前提。

- [ ] **Step 5: Commit**

```bash
git add src/photoar/synth.py tests/test_synth.py
git commit -m "feat: 合成查询图生成器（透视/模糊/亮度/色温/高光/JPEG）"
```

---

### Task 3: 几何校验与三条命中判定

**Files:**
- Create: `src/photoar/verify.py`
- Test: `tests/test_verify.py`

**Interfaces:**
- Consumes: `photoar.features.Features`
- Produces:
  - 常量 `MIN_INLIERS = 25`、`DET_MIN = 0.05`、`DET_MAX = 20.0`、`RATIO = 1.5`、`RANSAC_REPROJ = 3.0`
  - `PairResult` frozen dataclass：`photo_id: str`、`inliers: int`、`det: float`、`ok: bool`
  - `verify_pair(query: Features, ref: Features, photo_id: str) -> PairResult`
  - `Decision` frozen dataclass：`matched: bool`、`photo_id: str | None`、`inliers: int`、`reason: str`
  - `decide(results: list[PairResult]) -> Decision`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_verify.py`：

```python
import numpy as np

from photoar import features as F
from photoar import synth
from photoar import verify as V


def _res(photo_id, inliers, ok=None, det=1.0):
    if ok is None:
        ok = inliers >= V.MIN_INLIERS and V.DET_MIN <= det <= V.DET_MAX
    return V.PairResult(photo_id=photo_id, inliers=inliers, det=det, ok=ok)


def test_verify_pair_matches_image_against_its_own_synthetic_query(textured_image):
    img = textured_image(seed=1, w=1000, h=700)
    ref = F.extract(img)
    query_img, _ = synth.generate(img, count=1, seed=5)[0]
    r = V.verify_pair(F.extract(query_img), ref, "p1")
    assert r.ok
    assert r.inliers >= V.MIN_INLIERS


def test_verify_pair_rejects_unrelated_images(textured_image):
    q = F.extract(textured_image(seed=1))
    ref = F.extract(textured_image(seed=999))
    r = V.verify_pair(q, ref, "p2")
    assert not r.ok


def test_verify_pair_handles_empty_features():
    empty = F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8))
    r = V.verify_pair(empty, empty, "p3")
    assert r.inliers == 0
    assert not r.ok


def test_verify_pair_rejects_mirrored_match(textured_image):
    """镜像的单应矩阵行列式为负。实体照片经相机成像永远不会镜像，
    因此负行列式必须判否——这比 spec 写的 abs(det) 更严格且更正确。

    注意：这个测试只验证"镜像图整体上会被拒"这个组合行为，它**不能**
    隔离出拒绝的机制——实测镜像图只有约 8 个内点，`not r.ok` 会直接
    短路。真正锁住签名行列式分支的是下面那个测试。
    """
    import cv2

    img = textured_image(seed=2, w=800, h=600)
    ref = F.extract(img)
    mirrored = F.extract(cv2.flip(img, 1))
    r = V.verify_pair(mirrored, ref, "p4")
    assert not r.ok or r.det > 0


def test_mirrored_match_is_rejected_by_determinant_sign_not_inlier_count(textured_image):
    """隔离签名行列式分支：构造一个内点远超阈值、但行列式为负的场景。

    做法：参考的描述子与查询**完全相同**（BFMatcher 会 1:1 零距离匹配），
    而参考的关键点是查询关键点的水平镜像。于是 findHomography 得到一个
    纯反射矩阵：约 300 个内点、行列式为负。此时 ok 只可能因符号检验为 False。

    三条断言缺一不可：
      - inliers >= MIN_INLIERS 证明拒绝**不是**因为内点不够（去掉这条，
        测试就会在构造哪天不再产生大量匹配时静默退化成空测试）
      - det < 0        证明确实处于镜像情形
      - not r.ok       被测行为本身

    镜像必须在**缩放后**的坐标系里做：extract() 先把图缩到长边 640 才提
    特征，所以关键点活在 640 长边的坐标系里，不是原始 800x600。
    """
    img = textured_image(seed=2, w=800, h=600)
    query = F.extract(img)
    resized_w = F.resize_to_long_edge(img).shape[1]

    mirrored_pts = query.pts.copy()
    mirrored_pts[:, 0] = (resized_w - 1) - mirrored_pts[:, 0]
    mirror_ref = F.Features(pts=mirrored_pts, desc=query.desc)

    r = V.verify_pair(query, mirror_ref, "mirror")
    assert r.inliers >= V.MIN_INLIERS
    assert r.det < 0
    assert not r.ok


def test_decide_returns_no_match_on_empty_results():
    d = V.decide([])
    assert not d.matched
    assert d.photo_id is None


def test_decide_rejects_when_best_fails_inlier_threshold():
    d = V.decide([_res("a", V.MIN_INLIERS - 1)])
    assert not d.matched
    assert d.reason == "weak"


def test_decide_accepts_clear_single_winner():
    d = V.decide([_res("a", 60), _res("b", 5)])
    assert d.matched
    assert d.photo_id == "a"
    assert d.inliers == 60


def test_decide_accepts_when_only_one_candidate():
    d = V.decide([_res("a", 40)])
    assert d.matched
    assert d.photo_id == "a"


def test_decide_rejects_ambiguous_pair():
    """第一名 30、第二名 25：30 < 1.5*25=37.5，判否。
    这一条是压住误识别率的关键，宁可漏检。
    """
    d = V.decide([_res("a", 30), _res("b", 25)])
    assert not d.matched
    assert d.reason == "ambiguous"


def test_ratio_test_counts_candidates_below_inlier_threshold():
    """第二名 24 分（自身未过 MIN_INLIERS）也必须参与比值检验：
    第一名 26 < 1.5*24=36，判否。只在通过者之间比会放过这类歧义。
    """
    d = V.decide([_res("a", 26), _res("b", 24)])
    assert not d.matched
    assert d.reason == "ambiguous"


def test_decide_rejects_out_of_range_determinant():
    d = V.decide([_res("a", 80, ok=False, det=0.001)])
    assert not d.matched
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_verify.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.verify'`

- [ ] **Step 3: 实现 `src/photoar/verify.py`**

```python
"""几何校验与命中判定。

本模块是全项目判定阈值的唯一来源。三条判定（spec §8.3）缺一不可：
  1. 内点数 >= MIN_INLIERS
  2. 单应矩阵行列式落在 [DET_MIN, DET_MAX]
  3. 第一名内点数 >= RATIO * 第二名内点数

第 3 条的比值检验在**全部候选**之间进行，不只在通过前两条的候选之间。
理由：若第二名 24 分（未过阈值）而第一名 26 分，二者其实无法区分，
只在通过者之间比会把它当作"唯一通过者"直接放行，制造误识别。

行列式用带符号值而非绝对值：负行列式意味着镜像变换，而实体照片经
相机成像永远不会镜像，因此负值必须判否。这比 spec 的 abs(det) 更严。
"""

from dataclasses import dataclass

import cv2
import numpy as np

from .features import Features

MIN_INLIERS = 25
DET_MIN = 0.05
DET_MAX = 20.0
RATIO = 1.5
RANSAC_REPROJ = 3.0
# RANSAC 迭代上限。默认 2000 只在假匹配上被烧满——真匹配靠自适应终止恒定约 0.34ms。
# 实测降到 200：真匹配内点数完全不变、假匹配内点数略降、误判仍为 0，
# 而假匹配耗时从 20.56ms 降到 2.19ms。只限制难例上的努力，不改任何判定条件。
RANSAC_MAX_ITERS = 200

MIN_MATCHES_FOR_HOMOGRAPHY = 4


@dataclass(frozen=True)
class PairResult:
    photo_id: str
    inliers: int
    det: float
    ok: bool  # 是否通过前两条判定（比值检验是 decide 的职责）


@dataclass(frozen=True)
class Decision:
    matched: bool
    photo_id: str | None
    inliers: int
    reason: str  # 'ok' | 'empty' | 'weak' | 'ambiguous'


def _fail(photo_id: str) -> PairResult:
    return PairResult(photo_id=photo_id, inliers=0, det=0.0, ok=False)


def verify_pair(query: Features, ref: Features, photo_id: str) -> PairResult:
    if len(query) < MIN_MATCHES_FOR_HOMOGRAPHY or len(ref) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(query.desc, ref.desc)
    if len(matches) < MIN_MATCHES_FOR_HOMOGRAPHY:
        return _fail(photo_id)

    src = query.pts[[m.queryIdx for m in matches]]
    dst = ref.pts[[m.trainIdx for m in matches]]
    H, mask = cv2.findHomography(
        src, dst, cv2.RANSAC, RANSAC_REPROJ, maxIters=RANSAC_MAX_ITERS
    )
    if H is None or mask is None:
        return _fail(photo_id)

    inliers = int(mask.sum())
    det = float(np.linalg.det(H))
    ok = inliers >= MIN_INLIERS and DET_MIN <= det <= DET_MAX
    return PairResult(photo_id=photo_id, inliers=inliers, det=det, ok=ok)


def decide(results: list[PairResult]) -> Decision:
    if not results:
        return Decision(matched=False, photo_id=None, inliers=0, reason="empty")

    ranked = sorted(results, key=lambda r: -r.inliers)
    top1 = ranked[0]
    if not top1.ok:
        return Decision(matched=False, photo_id=None, inliers=top1.inliers, reason="weak")

    runner_up = ranked[1].inliers if len(ranked) > 1 else 0
    if top1.inliers < RATIO * runner_up:
        return Decision(
            matched=False, photo_id=None, inliers=top1.inliers, reason="ambiguous"
        )

    return Decision(
        matched=True, photo_id=top1.photo_id, inliers=top1.inliers, reason="ok"
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_verify.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/photoar/verify.py tests/test_verify.py
git commit -m "feat: 几何校验与三条命中判定（比值检验覆盖全部候选，拒绝镜像）"
```

---

### Task 4: 定长描述子存储与 mmap 随机读

**Files:**
- Create: `src/photoar/descstore.py`
- Test: `tests/test_descstore.py`

**Interfaces:**
- Consumes: `photoar.features.Features`、`photoar.features.N_FEATURES`、`photoar.features.DESC_BYTES`
- Produces:
  - `SLOT_STRIDE: int`（每张照片占用的字节数，值为 12008）
  - `DescStoreWriter(path: str | Path, capacity: int)`，方法 `append(features: Features) -> int`（返回 slot 下标）、`close()`，支持 `with`
  - `DescStore(path: str | Path)`，方法 `read(slot: int) -> Features`、`__len__()`，支持 `with`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_descstore.py`：

```python
import numpy as np
import pytest

from photoar import features as F
from photoar.descstore import SLOT_STRIDE, DescStore, DescStoreWriter


def test_slot_stride_matches_documented_budget():
    # 4 字节计数 + 4 字节对齐填充 + 300*2*float32 + 300*32
    assert SLOT_STRIDE == 8 + F.N_FEATURES * 2 * 4 + F.N_FEATURES * F.DESC_BYTES
    assert SLOT_STRIDE == 12008
    # spec §6 写的 9600 字节/张漏算了关键点坐标，实际约 12KB/张
    assert SLOT_STRIDE * 10_000 < 130 * 1024 * 1024


def test_roundtrip_preserves_features(tmp_path, textured_image):
    path = tmp_path / "desc.bin"
    originals = [F.extract(textured_image(seed=s)) for s in range(3)]

    with DescStoreWriter(path, capacity=3) as w:
        slots = [w.append(f) for f in originals]
    assert slots == [0, 1, 2]

    with DescStore(path) as store:
        assert len(store) == 3
        for slot, orig in zip(slots, originals):
            got = store.read(slot)
            assert np.array_equal(got.desc, orig.desc)
            assert np.allclose(got.pts, orig.pts)


def test_handles_fewer_than_n_features(tmp_path):
    few = F.Features(
        pts=np.array([[1.0, 2.0], [3.0, 4.0]], np.float32),
        desc=np.arange(64, dtype=np.uint8).reshape(2, 32),
    )
    path = tmp_path / "few.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(few)
    with DescStore(path) as store:
        got = store.read(0)
        assert len(got) == 2
        assert np.array_equal(got.desc, few.desc)
        assert np.allclose(got.pts, few.pts)


def test_handles_empty_features(tmp_path):
    empty = F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8))
    path = tmp_path / "empty.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(empty)
    with DescStore(path) as store:
        assert len(store.read(0)) == 0


def test_truncates_when_over_capacity_features(tmp_path):
    """超过 N_FEATURES 的输入被截断而不是越界写坏邻居 slot。"""
    n = F.N_FEATURES + 17
    big = F.Features(
        pts=np.zeros((n, 2), np.float32),
        desc=np.zeros((n, 32), np.uint8),
    )
    path = tmp_path / "big.bin"
    with DescStoreWriter(path, capacity=2) as w:
        w.append(big)
        w.append(F.Features(np.ones((1, 2), np.float32), np.ones((1, 32), np.uint8)))
    with DescStore(path) as store:
        assert len(store.read(0)) == F.N_FEATURES
        second = store.read(1)
        assert len(second) == 1
        assert np.array_equal(second.desc, np.ones((1, 32), np.uint8))


def test_append_beyond_capacity_raises(tmp_path):
    path = tmp_path / "cap.bin"
    empty = F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8))
    with DescStoreWriter(path, capacity=1) as w:
        w.append(empty)
        with pytest.raises(IndexError):
            w.append(empty)


def test_read_out_of_range_raises(tmp_path):
    path = tmp_path / "oob.bin"
    with DescStoreWriter(path, capacity=1) as w:
        w.append(F.Features(np.zeros((0, 2), np.float32), np.zeros((0, 32), np.uint8)))
    with DescStore(path) as store:
        with pytest.raises(IndexError):
            store.read(1)
        with pytest.raises(IndexError):
            store.read(-1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_descstore.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.descstore'`

- [ ] **Step 3: 实现 `src/photoar/descstore.py`**

```python
"""定长 slot 的描述子/关键点存储，用 mmap 随机读。

每张照片占固定 SLOT_STRIDE 字节，slot 下标即偏移，因此精排阶段只需
按 Top-K 的下标随机读 K 个 slot，无需把全库描述子常驻内存。

slot 布局（小端）：
  offset 0   uint32  count      实际特征数（<= N_FEATURES）
  offset 4   uint32  _pad       对齐填充，保证 float32 数组 8 字节对齐
  offset 8   float32[N_FEATURES*2]  关键点 xy
  offset ..  uint8[N_FEATURES*32]   描述子

spec §6 给的 9600 字节/张只算了描述子，漏了 RANSAC 必需的关键点坐标。
实际每张 12008 字节，1 万张约 120MB（仍在预算内）。
"""

from pathlib import Path

import numpy as np

from .features import DESC_BYTES, N_FEATURES, Features

_HEADER_BYTES = 8
_PTS_BYTES = N_FEATURES * 2 * 4
_DESC_BYTES_TOTAL = N_FEATURES * DESC_BYTES
SLOT_STRIDE = _HEADER_BYTES + _PTS_BYTES + _DESC_BYTES_TOTAL

_PTS_OFFSET = _HEADER_BYTES
_DESC_OFFSET = _HEADER_BYTES + _PTS_BYTES


class DescStoreWriter:
    """顺序写入固定容量的描述子库。"""

    def __init__(self, path: str | Path, capacity: int) -> None:
        self._path = Path(path)
        self._capacity = int(capacity)
        self._next = 0
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._map = np.memmap(
            self._path, dtype=np.uint8, mode="w+",
            shape=(self._capacity * SLOT_STRIDE,),
        )

    def append(self, features: Features) -> int:
        if self._next >= self._capacity:
            raise IndexError(
                f"描述子库容量已满（capacity={self._capacity}）"
            )
        slot = self._next
        self._next += 1

        count = min(len(features), N_FEATURES)
        base = slot * SLOT_STRIDE
        raw = self._map[base : base + SLOT_STRIDE]
        raw[:] = 0

        raw[0:4].view(np.uint32)[0] = count
        if count:
            pts = np.ascontiguousarray(features.pts[:count], np.float32)
            raw[_PTS_OFFSET : _PTS_OFFSET + count * 8].view(np.float32)[:] = pts.ravel()
            desc = np.ascontiguousarray(features.desc[:count], np.uint8)
            raw[_DESC_OFFSET : _DESC_OFFSET + count * DESC_BYTES] = desc.ravel()
        return slot

    def close(self) -> None:
        self._map.flush()
        del self._map

    def __enter__(self) -> "DescStoreWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class DescStore:
    """只读随机访问描述子库。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        size = self._path.stat().st_size
        if size % SLOT_STRIDE:
            raise ValueError(
                f"{self._path} 大小 {size} 不是 slot 步长 {SLOT_STRIDE} 的整数倍"
            )
        self._count = size // SLOT_STRIDE
        self._map = np.memmap(self._path, dtype=np.uint8, mode="r", shape=(size,))

    def __len__(self) -> int:
        return self._count

    def read(self, slot: int) -> Features:
        if slot < 0 or slot >= self._count:
            raise IndexError(f"slot {slot} 超出范围 [0, {self._count})")
        base = slot * SLOT_STRIDE
        raw = self._map[base : base + SLOT_STRIDE]
        count = int(raw[0:4].view(np.uint32)[0])
        if count == 0:
            return Features(
                np.zeros((0, 2), np.float32), np.zeros((0, DESC_BYTES), np.uint8)
            )
        pts = (
            raw[_PTS_OFFSET : _PTS_OFFSET + count * 8]
            .view(np.float32)
            .reshape(count, 2)
            .copy()
        )
        desc = (
            raw[_DESC_OFFSET : _DESC_OFFSET + count * DESC_BYTES]
            .reshape(count, DESC_BYTES)
            .copy()
        )
        return Features(pts=pts, desc=desc)

    def close(self) -> None:
        del self._map

    def __enter__(self) -> "DescStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_descstore.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/photoar/descstore.py tests/test_descstore.py
git commit -m "feat: 定长 slot 描述子库与 mmap 随机读（含关键点，12008B/张）"
```

---

### Task 5: 暴力检索基线与指标报告 —— 第一个 go/no-go 数字

这是整个 Phase 0 最重要的任务。暴力检索不含词汇表、不含粗排，因此它测出的**误识别率是几何校验本身的判别力上限**。如果它在小库上就误判，后面的 BoW 再好也救不回来。

**Files:**
- Create: `src/photoar/bruteforce.py`
- Create: `src/photoar/evaluate.py`
- Test: `tests/test_bruteforce.py`
- Test: `tests/test_evaluate.py`

**Interfaces:**
- Consumes: `photoar.features.extract`、`photoar.descstore.DescStore`、`photoar.verify.{verify_pair, decide, Decision}`、`photoar.synth.generate`
- Produces:
  - `BruteForceRecognizer(store: DescStore, photo_ids: list[str])`，方法 `recognize(img_bgr: np.ndarray) -> Decision`
  - `Metrics` frozen dataclass：`total: int`、`correct: int`、`wrong: int`、`missed: int`、`latencies_ms: list[float]`，属性 `correct_rate`、`wrong_rate`、`missed_rate`、`p95_latency_ms`，方法 `as_report() -> str`
  - `evaluate(recognizer, refs: dict[str, np.ndarray], samples_per_ref: int, seed: int) -> Metrics`
  - `Recognizer` 协议：任何具备 `recognize(img_bgr) -> Decision` 的对象

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_bruteforce.py`：

```python
from photoar import features as F
from photoar import synth
from photoar.bruteforce import BruteForceRecognizer
from photoar.descstore import DescStore, DescStoreWriter


def _build_store(tmp_path, images):
    path = tmp_path / "desc.bin"
    with DescStoreWriter(path, capacity=len(images)) as w:
        for img in images:
            w.append(F.extract(img))
    return DescStore(path)


def test_recognizes_synthetic_query_of_a_known_photo(tmp_path, textured_image):
    images = [textured_image(seed=s, w=1000, h=700) for s in range(5)]
    ids = [f"p{i}" for i in range(5)]
    with _build_store(tmp_path, images) as store:
        rec = BruteForceRecognizer(store, ids)
        query, _ = synth.generate(images[2], count=1, seed=3)[0]
        d = rec.recognize(query)
    assert d.matched
    assert d.photo_id == "p2"


def test_rejects_photo_not_in_library(tmp_path, textured_image):
    images = [textured_image(seed=s) for s in range(5)]
    with _build_store(tmp_path, images) as store:
        rec = BruteForceRecognizer(store, [f"p{i}" for i in range(5)])
        d = rec.recognize(textured_image(seed=12345))
    assert not d.matched


def test_rejects_blank_query(tmp_path, textured_image):
    import numpy as np

    images = [textured_image(seed=s) for s in range(3)]
    with _build_store(tmp_path, images) as store:
        rec = BruteForceRecognizer(store, ["a", "b", "c"])
        d = rec.recognize(np.full((400, 600, 3), 128, np.uint8))
    assert not d.matched


def test_id_count_must_match_store_size(tmp_path, textured_image):
    import pytest

    with _build_store(tmp_path, [textured_image(seed=0)]) as store:
        with pytest.raises(ValueError):
            BruteForceRecognizer(store, ["a", "b"])
```

创建 `tests/test_evaluate.py`：

```python
import numpy as np

from photoar import evaluate as E
from photoar.verify import Decision


class _FakeRecognizer:
    """按预设脚本返回结果，让指标计算的测试不依赖真实 CV。"""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def recognize(self, img_bgr):
        d = self._script[self._i % len(self._script)]
        self._i += 1
        return d


def _hit(pid):
    return Decision(matched=True, photo_id=pid, inliers=50, reason="ok")


def _miss():
    return Decision(matched=False, photo_id=None, inliers=0, reason="weak")


def test_classifies_correct_wrong_missed(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0"), _hit("pX"), _miss(), _hit("p0")])
    m = E.evaluate(rec, refs, samples_per_ref=4, seed=1)
    assert m.total == 4
    assert (m.correct, m.wrong, m.missed) == (2, 1, 1)


def test_rates_sum_to_one(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0"), _hit("pX"), _miss()])
    m = E.evaluate(rec, refs, samples_per_ref=3, seed=1)
    assert abs(m.correct_rate + m.wrong_rate + m.missed_rate - 1.0) < 1e-9


def test_p95_latency_is_reported(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0")])
    m = E.evaluate(rec, refs, samples_per_ref=5, seed=1)
    assert len(m.latencies_ms) == 5
    assert m.p95_latency_ms >= 0.0


def test_report_contains_all_four_headline_numbers(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    rec = _FakeRecognizer([_hit("p0"), _hit("pX"), _miss()])
    text = E.evaluate(rec, refs, samples_per_ref=3, seed=1).as_report()
    for token in ("正确命中", "误识别", "漏检", "P95"):
        assert token in text


def test_report_flags_pass_or_fail_against_baseline(textured_image):
    refs = {"p0": textured_image(seed=0, w=400, h=300)}
    good = E.evaluate(_FakeRecognizer([_hit("p0")]), refs, samples_per_ref=20, seed=1)
    assert good.meets_baseline

    bad = E.evaluate(_FakeRecognizer([_hit("pX")]), refs, samples_per_ref=20, seed=1)
    assert not bad.meets_baseline


def test_zero_samples_does_not_divide_by_zero():
    m = E.evaluate(_FakeRecognizer([_miss()]), {}, samples_per_ref=0, seed=1)
    assert m.total == 0
    assert m.correct_rate == 0.0
    assert not m.meets_baseline
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_bruteforce.py tests/test_evaluate.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.bruteforce'`

- [ ] **Step 3: 实现 `src/photoar/bruteforce.py`**

```python
"""暴力检索：对全库逐个做几何校验。

存在的意义不是上生产（O(N) 太慢），而是提供一个**不含词汇表变量**的
参考基线。它测出的误识别率就是几何校验本身的判别力上限；BoW 粗排的
召回率也以它的结果为 ground truth。
"""

import numpy as np

from .descstore import DescStore
from .features import extract
from .verify import Decision, decide, verify_pair


class BruteForceRecognizer:
    def __init__(self, store: DescStore, photo_ids: list[str]) -> None:
        if len(photo_ids) != len(store):
            raise ValueError(
                f"photo_ids 数量 {len(photo_ids)} 与描述子库 slot 数 {len(store)} 不一致"
            )
        self._store = store
        self._ids = list(photo_ids)

    def recognize(self, img_bgr: np.ndarray) -> Decision:
        query = extract(img_bgr)
        results = [
            verify_pair(query, self._store.read(slot), pid)
            for slot, pid in enumerate(self._ids)
        ]
        return decide(results)
```

- [ ] **Step 4: 实现 `src/photoar/evaluate.py`**

```python
"""指标计算与报告。

三分类互斥且穷尽（spec §14.2）：
  正确命中  matched 且 photo_id 等于来源
  误识别    matched 但 photo_id 不等于来源
  漏检      not matched

误识别率比漏检率重要一个数量级——漏检只是让用户多举一秒手机，
播错视频是在家人面前的事故。
"""

import time
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from . import synth
from .verify import Decision

BASELINE_CORRECT_RATE = 0.95
BASELINE_WRONG_RATE = 0.001
BASELINE_P95_LATENCY_MS = 80.0


class Recognizer(Protocol):
    def recognize(self, img_bgr: np.ndarray) -> Decision: ...


@dataclass(frozen=True)
class Metrics:
    total: int
    correct: int
    wrong: int
    missed: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def correct_rate(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def wrong_rate(self) -> float:
        return self.wrong / self.total if self.total else 0.0

    @property
    def missed_rate(self) -> float:
        return self.missed / self.total if self.total else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return float(np.percentile(np.asarray(self.latencies_ms), 95))

    @property
    def meets_baseline(self) -> bool:
        return (
            self.total > 0
            and self.correct_rate >= BASELINE_CORRECT_RATE
            and self.wrong_rate <= BASELINE_WRONG_RATE
        )

    def as_report(self) -> str:
        verdict = "达标" if self.meets_baseline else "未达标"
        latency_note = (
            "达标"
            if self.p95_latency_ms <= BASELINE_P95_LATENCY_MS
            else f"超出目标 {BASELINE_P95_LATENCY_MS:.0f}ms"
        )
        return "\n".join(
            [
                f"样本总数    {self.total}",
                f"正确命中    {self.correct:6d}  {self.correct_rate:7.2%}  "
                f"（目标 >= {BASELINE_CORRECT_RATE:.0%}）",
                f"误识别      {self.wrong:6d}  {self.wrong_rate:7.3%}  "
                f"（目标 <= {BASELINE_WRONG_RATE:.1%}）",
                f"漏检        {self.missed:6d}  {self.missed_rate:7.2%}",
                f"P95 延迟    {self.p95_latency_ms:.1f} ms  （{latency_note}）",
                f"结论        {verdict}",
            ]
        )


def evaluate(
    recognizer: Recognizer,
    refs: dict[str, np.ndarray],
    samples_per_ref: int,
    seed: int,
) -> Metrics:
    correct = wrong = missed = 0
    latencies: list[float] = []

    for offset, (photo_id, img) in enumerate(sorted(refs.items())):
        for query_img, _ in synth.generate(img, samples_per_ref, seed + offset):
            t0 = time.perf_counter()
            d = recognizer.recognize(query_img)
            latencies.append((time.perf_counter() - t0) * 1000.0)

            if not d.matched:
                missed += 1
            elif d.photo_id == photo_id:
                correct += 1
            else:
                wrong += 1

    return Metrics(
        total=correct + wrong + missed,
        correct=correct,
        wrong=wrong,
        missed=missed,
        latencies_ms=latencies,
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_bruteforce.py tests/test_evaluate.py -v`
Expected: 10 passed

- [ ] **Step 6: 跑一次小规模真实数字并记录**

```bash
python - <<'PY'
import numpy as np, cv2, tempfile, pathlib
from photoar import features as F, evaluate as E
from photoar.descstore import DescStore, DescStoreWriter
from photoar.bruteforce import BruteForceRecognizer

# 用合成纹理图当作 200 张"照片"，测几何校验在小库上的判别力
def make(seed, w=1000, h=700):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (h//8, w//8, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
    for _ in range(40):
        x1, y1 = int(rng.integers(0,w)), int(rng.integers(0,h))
        x2 = min(w-1, x1+int(rng.integers(20,120)))
        y2 = min(h-1, y1+int(rng.integers(20,120)))
        cv2.rectangle(img,(x1,y1),(x2,y2),tuple(int(c) for c in rng.integers(0,256,3)),-1)
    return img

N = 200
imgs = {f"p{i}": make(i) for i in range(N)}
d = pathlib.Path(tempfile.mkdtemp())
with DescStoreWriter(d/"desc.bin", capacity=N) as w:
    for k in sorted(imgs): w.append(F.extract(imgs[k]))
with DescStore(d/"desc.bin") as store:
    rec = BruteForceRecognizer(store, sorted(imgs))
    m = E.evaluate(rec, dict(list(sorted(imgs.items()))[:20]), samples_per_ref=10, seed=1)
print(m.as_report())
PY
```

把输出粘进 `docs/superpowers/plans/phase0-results.md`，标题写 `## 里程碑 0a：几何校验判别力（200 张合成库，暴力检索）`。

**这是第一个决策点。** 合成纹理图比真实照片更容易区分，所以这里的数字是**乐观上界**。判读标准：
- 误识别率 > 0 → 几何校验判别力不足。**先解决它再往下做**（提高 `MIN_INLIERS`、提高 `RATIO`），不要开始 Task 6。
- 正确命中率 < 95% → 检查是不是合成扰动过强（回看 Task 2 的 `test_synthetic_query_still_matches_its_source`）。

- [ ] **Step 7: Commit**

```bash
git add src/photoar/bruteforce.py src/photoar/evaluate.py \
        tests/test_bruteforce.py tests/test_evaluate.py \
        docs/superpowers/plans/phase0-results.md
git commit -m "feat: 暴力检索基线与指标报告，记录里程碑 0a 结果"
```

---

### Task 6: 二进制层次 k-majority 词汇树

**Files:**
- Create: `src/photoar/vocab.py`
- Test: `tests/test_vocab.py`

**Interfaces:**
- Consumes: `photoar.features.DESC_BYTES`
- Produces:
  - 常量 `BRANCHING = 10`、`DEPTH = 4`（叶子上界 10^4 = 10000 词）
  - `hamming_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray`，输入 `(N,32)`/`(M,32)` uint8，返回 `(N,M)` uint16
  - `Vocab`，方法 `words_of(desc: np.ndarray) -> np.ndarray`（`(N,)` int32）、`n_words: int`（property）、`save(path)`、类方法 `load(path) -> Vocab`
  - `train(descriptors: np.ndarray, branching: int = BRANCHING, depth: int = DEPTH, seed: int = 0) -> Vocab`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_vocab.py`：

```python
import numpy as np
import pytest

from photoar import features as F
from photoar import vocab as V


def _random_desc(n, seed=0):
    return np.random.default_rng(seed).integers(0, 256, (n, 32), dtype=np.uint8)


def test_hamming_matrix_shape_and_known_values():
    a = np.zeros((2, 32), np.uint8)
    b = np.zeros((3, 32), np.uint8)
    b[1, 0] = 0b00000111  # 3 bit 不同
    b[2, :] = 0xFF        # 256 bit 全不同
    d = V.hamming_matrix(a, b)
    assert d.shape == (2, 3)
    assert d[0, 0] == 0
    assert d[0, 1] == 3
    assert d[0, 2] == 256


def test_hamming_matrix_is_symmetric_in_argument_order():
    a, b = _random_desc(4, 1), _random_desc(5, 2)
    assert np.array_equal(V.hamming_matrix(a, b), V.hamming_matrix(b, a).T)


def test_train_produces_words_within_range():
    voc = V.train(_random_desc(3000, 3), branching=4, depth=3, seed=0)
    words = voc.words_of(_random_desc(200, 4))
    assert words.shape == (200,)
    assert words.dtype == np.int32
    assert words.min() >= 0
    assert words.max() < voc.n_words


def test_train_is_deterministic_given_seed():
    d = _random_desc(2000, 5)
    a = V.train(d, branching=4, depth=3, seed=7)
    b = V.train(d, branching=4, depth=3, seed=7)
    q = _random_desc(100, 6)
    assert np.array_equal(a.words_of(q), b.words_of(q))


def test_identical_descriptors_map_to_same_word():
    voc = V.train(_random_desc(2000, 8), branching=4, depth=3, seed=0)
    d = _random_desc(1, 9)
    assert voc.words_of(np.repeat(d, 5, axis=0)).tolist() == [voc.words_of(d)[0]] * 5


def test_near_duplicate_descriptors_usually_share_word():
    """翻转 2 bit 的描述子应大多落在同一个词——这是粗排召回率的前提。"""
    voc = V.train(_random_desc(6000, 10), branching=6, depth=3, seed=0)
    base = _random_desc(300, 11)
    noisy = base.copy()
    noisy[:, 0] ^= 0b00000011
    same = (voc.words_of(base) == voc.words_of(noisy)).mean()
    assert same >= 0.8


def test_words_of_empty_input():
    voc = V.train(_random_desc(1000, 12), branching=4, depth=2, seed=0)
    assert voc.words_of(np.zeros((0, 32), np.uint8)).shape == (0,)


def test_save_load_roundtrip(tmp_path):
    voc = V.train(_random_desc(2000, 13), branching=4, depth=3, seed=0)
    path = tmp_path / "voc.npz"
    voc.save(path)
    loaded = V.Vocab.load(path)
    assert loaded.n_words == voc.n_words
    q = _random_desc(150, 14)
    assert np.array_equal(loaded.words_of(q), voc.words_of(q))


def test_train_rejects_empty_descriptors():
    with pytest.raises(ValueError):
        V.train(np.zeros((0, 32), np.uint8))


def test_train_on_real_orb_descriptors(textured_image):
    descs = np.vstack([F.extract(textured_image(seed=s)).desc for s in range(30)])
    voc = V.train(descs, branching=6, depth=3, seed=0)
    words = voc.words_of(descs)
    # 真实 ORB 描述子应铺开到多个词上，而不是全挤进一个
    assert len(np.unique(words)) >= 20
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_vocab.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.vocab'`

- [ ] **Step 3: 实现 `src/photoar/vocab.py`**

```python
"""二进制层次词汇树（k-majority）。

ORB 描述子是 256 bit 二进制，普通 k-means（欧氏均值）不适用，需要用
Hamming 距离分配 + 逐 bit 多数表决更新中心，即 k-majority。

分层的目的是把量化代价从 O(n_words) 降到 O(branching * depth)：
10000 词的扁平词表要比 10000 次，4 层 10 分支只要比 40 次。

spec §8.2 原本要求先用 ORB-SLAM 的现成 ORBvoc.txt。本项目改为自训小
词汇表，因为 Task 5 的暴力检索已经提供了一个不含词汇表变量的更硬基线；
ORBvoc 保留为召回不达标时的备选，届时只需另写一个提供 words_of 的类。
"""

from pathlib import Path

import numpy as np

from .features import DESC_BYTES

BRANCHING = 10
DEPTH = 4
KMAJORITY_ITERS = 8
MIN_DESC_PER_NODE = 8  # 少于此数不再细分

_POPCOUNT = (
    np.unpackbits(np.arange(256, dtype=np.uint8)[:, None], axis=1)
    .sum(axis=1)
    .astype(np.uint16)
)


def hamming_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """成对 Hamming 距离。a:(N,32) b:(M,32) -> (N,M) uint16。

    调用方需保证 N*M 不会大到爆内存；本项目里 M 恒等于 branching（<=10）。
    """
    if a.shape[0] == 0 or b.shape[0] == 0:
        return np.zeros((a.shape[0], b.shape[0]), np.uint16)
    xor = np.bitwise_xor(a[:, None, :], b[None, :, :])
    return _POPCOUNT[xor].sum(axis=2).astype(np.uint16)


def _majority(descs: np.ndarray) -> np.ndarray:
    """逐 bit 多数表决，得到一个 (32,) uint8 的中心。"""
    bits = np.unpackbits(descs, axis=1)
    return np.packbits((bits.mean(axis=0) >= 0.5).astype(np.uint8))


def _kmajority(
    descs: np.ndarray, k: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (centers:(k',32) uint8, labels:(N,) int)。k' 可能小于 k。"""
    n = descs.shape[0]
    k = min(k, n)
    centers = descs[rng.choice(n, size=k, replace=False)].copy()

    labels = np.zeros(n, np.int64)
    for _ in range(KMAJORITY_ITERS):
        labels = hamming_matrix(descs, centers).argmin(axis=1)
        moved = False
        for c in range(centers.shape[0]):
            members = descs[labels == c]
            if members.shape[0] == 0:
                # 空簇：重新播种到离自己最远的那个描述子
                far = hamming_matrix(descs, centers[c : c + 1])[:, 0].argmax()
                new_center = descs[far].copy()
            else:
                new_center = _majority(members)
            if not np.array_equal(new_center, centers[c]):
                centers[c] = new_center
                moved = True
        if not moved:
            break
    labels = hamming_matrix(descs, centers).argmin(axis=1)
    return centers, labels


class Vocab:
    """层次词汇树。内部用扁平数组存节点，便于序列化。

    centers[i]   第 i 个节点的中心描述子
    children[i]  第 i 个节点的子节点下标数组；空数组表示叶子
    leaf_id[i]   叶子节点的词 id；非叶子为 -1
    """

    def __init__(
        self,
        centers: np.ndarray,
        children: list[np.ndarray],
        leaf_id: np.ndarray,
        root_children: np.ndarray,
        n_words: int,
    ) -> None:
        self._centers = centers
        self._children = children
        self._leaf_id = leaf_id
        self._root_children = root_children
        self._n_words = int(n_words)

    @property
    def n_words(self) -> int:
        return self._n_words

    def words_of(self, desc: np.ndarray) -> np.ndarray:
        if desc.shape[0] == 0:
            return np.zeros((0,), np.int32)
        out = np.empty(desc.shape[0], np.int32)
        for i in range(desc.shape[0]):
            row = desc[i : i + 1]
            candidates = self._root_children
            node = -1
            while candidates.size:
                d = hamming_matrix(row, self._centers[candidates])[0]
                node = int(candidates[int(d.argmin())])
                candidates = self._children[node]
            out[i] = self._leaf_id[node] if node >= 0 else 0
        return out

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        flat = np.concatenate(self._children) if self._children else np.zeros(0, np.int32)
        lengths = np.array([c.size for c in self._children], np.int32)
        np.savez_compressed(
            path,
            centers=self._centers,
            children_flat=flat.astype(np.int32),
            children_len=lengths,
            leaf_id=self._leaf_id,
            root_children=self._root_children.astype(np.int32),
            n_words=np.array([self._n_words], np.int64),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Vocab":
        z = np.load(Path(path))
        lengths = z["children_len"]
        flat = z["children_flat"]
        children, cursor = [], 0
        for length in lengths:
            children.append(flat[cursor : cursor + int(length)])
            cursor += int(length)
        return cls(
            centers=z["centers"],
            children=children,
            leaf_id=z["leaf_id"],
            root_children=z["root_children"],
            n_words=int(z["n_words"][0]),
        )


def train(
    descriptors: np.ndarray,
    branching: int = BRANCHING,
    depth: int = DEPTH,
    seed: int = 0,
) -> Vocab:
    if descriptors.shape[0] == 0:
        raise ValueError("训练词汇树需要至少一个描述子")
    if descriptors.shape[1] != DESC_BYTES:
        raise ValueError(f"描述子宽度应为 {DESC_BYTES}，收到 {descriptors.shape[1]}")

    rng = np.random.default_rng(seed)
    centers_list: list[np.ndarray] = []
    children: list[np.ndarray] = []
    leaf_id_list: list[int] = []
    next_word = 0

    def build(subset: np.ndarray, level: int) -> np.ndarray:
        """在 subset 上建一层，返回本层新建节点的下标数组。"""
        nonlocal next_word
        if subset.shape[0] == 0:
            return np.zeros(0, np.int32)

        centers, labels = _kmajority(subset, branching, rng)
        node_ids = []
        for c in range(centers.shape[0]):
            node_id = len(centers_list)
            centers_list.append(centers[c])
            children.append(np.zeros(0, np.int32))
            leaf_id_list.append(-1)
            node_ids.append(node_id)

            members = subset[labels == c]
            can_split = level + 1 < depth and members.shape[0] >= MIN_DESC_PER_NODE
            if can_split:
                kids = build(members, level + 1)
                children[node_id] = kids
            if children[node_id].size == 0:
                leaf_id_list[node_id] = next_word
                next_word += 1
        return np.array(node_ids, np.int32)

    root_children = build(descriptors, 0)
    return Vocab(
        centers=np.array(centers_list, np.uint8).reshape(-1, DESC_BYTES),
        children=children,
        leaf_id=np.array(leaf_id_list, np.int32),
        root_children=root_children,
        n_words=max(1, next_word),
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_vocab.py -v`
Expected: 10 passed

若 `test_near_duplicate_descriptors_usually_share_word` 未达 0.8：把 `KMAJORITY_ITERS` 提到 16，或把 `depth` 降到 3（更浅的树对噪声更宽容）。**不要放宽断言** —— 这个比例直接决定粗排召回率上限。

- [ ] **Step 5: Commit**

```bash
git add src/photoar/vocab.py tests/test_vocab.py
git commit -m "feat: 二进制层次 k-majority 词汇树（自训，ORBvoc 保留为备选）"
```

---

### Task 7: TF-IDF 倒排索引与粗排

**Files:**
- Create: `src/photoar/index.py`
- Test: `tests/test_index.py`

**Interfaces:**
- Consumes: `photoar.vocab.Vocab`
- Produces:
  - `InvertedIndexBuilder(n_words: int)`，方法 `add(words: np.ndarray) -> int`（返回 doc 下标）、`build() -> InvertedIndex`
  - `InvertedIndex`，方法 `query(words: np.ndarray, top_k: int) -> list[tuple[int, float]]`（doc 下标与分数，降序）、`n_docs: int`（property）、`save(path)`、类方法 `load(path) -> InvertedIndex`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_index.py`：

```python
import numpy as np
import pytest

from photoar.index import InvertedIndex, InvertedIndexBuilder


def _build(docs, n_words=50):
    b = InvertedIndexBuilder(n_words)
    for d in docs:
        b.add(np.asarray(d, np.int32))
    return b.build()


def test_add_returns_sequential_doc_indices():
    b = InvertedIndexBuilder(10)
    assert b.add(np.array([1, 2], np.int32)) == 0
    assert b.add(np.array([3], np.int32)) == 1


def test_query_ranks_exact_match_first():
    idx = _build([[1, 2, 3], [4, 5, 6], [1, 2, 7]])
    top = idx.query(np.array([1, 2, 3], np.int32), top_k=3)
    assert top[0][0] == 0
    assert top[0][1] > 0


def test_query_respects_top_k():
    idx = _build([[1], [2], [3], [4], [5]])
    assert len(idx.query(np.array([1], np.int32), top_k=2)) == 2


def test_query_top_k_larger_than_corpus():
    idx = _build([[1], [2]])
    assert len(idx.query(np.array([1], np.int32), top_k=10)) == 2


def test_scores_are_descending():
    idx = _build([[1, 2, 3], [1, 2, 9], [1, 8, 9], [7, 8, 9]])
    scores = [s for _, s in idx.query(np.array([1, 2, 3], np.int32), top_k=4)]
    assert scores == sorted(scores, reverse=True)


def test_idf_downweights_ubiquitous_words():
    """词 0 出现在所有文档里，应几乎不贡献区分度；
    只共享词 0 的文档不应排在共享稀有词 5 的文档之前。
    """
    idx = _build([[0, 5], [0, 6], [0, 7], [0, 8]])
    top = idx.query(np.array([0, 5], np.int32), top_k=4)
    assert top[0][0] == 0


def test_query_on_empty_words_returns_empty():
    idx = _build([[1, 2]])
    assert idx.query(np.zeros((0,), np.int32), top_k=5) == []


def test_empty_corpus_query_returns_empty():
    idx = InvertedIndexBuilder(10).build()
    assert idx.n_docs == 0
    assert idx.query(np.array([1], np.int32), top_k=5) == []


def test_word_out_of_range_rejected():
    b = InvertedIndexBuilder(5)
    with pytest.raises(ValueError):
        b.add(np.array([7], np.int32))


def test_save_load_roundtrip(tmp_path):
    idx = _build([[1, 2, 3], [2, 3, 4], [5, 6, 7]])
    path = tmp_path / "idx.npz"
    idx.save(path)
    loaded = InvertedIndex.load(path)
    q = np.array([1, 2, 3], np.int32)
    assert loaded.n_docs == idx.n_docs
    assert loaded.query(q, top_k=3) == idx.query(q, top_k=3)


def test_recall_at_k_on_many_docs():
    """1000 篇文档，查询取自其中一篇并扰动 20% 的词，Top-20 必须包含它。
    这是粗排召回率的最小保证；不满足则两阶段检索的第一阶段就是瓶颈。
    """
    rng = np.random.default_rng(0)
    docs = [rng.integers(0, 500, 60).astype(np.int32) for _ in range(1000)]
    idx = _build(docs, n_words=500)

    hits = 0
    for target in range(0, 1000, 50):
        q = docs[target].copy()
        mutate = rng.choice(len(q), size=len(q) // 5, replace=False)
        q[mutate] = rng.integers(0, 500, len(mutate))
        if target in [d for d, _ in idx.query(q, top_k=20)]:
            hits += 1
    assert hits >= 18  # 20 次里至少 18 次
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_index.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.index'`

- [ ] **Step 3: 实现 `src/photoar/index.py`**

```python
"""TF-IDF 倒排索引，用于两阶段检索的粗排。

不用 scipy.sparse：build() 时把倒排表压成两个扁平数组 + 偏移量，
query() 在一个 float32 累加器上做散射加法。万级文档下这已经足够快，
且零额外依赖。

评分：文档与查询都用 L2 归一化的 tf-idf 向量，分数即余弦相似度。
idf = log(n_docs / df)，df 为 0 的词权重记为 0。
"""

from collections import Counter
from pathlib import Path

import numpy as np


class InvertedIndexBuilder:
    def __init__(self, n_words: int) -> None:
        self._n_words = int(n_words)
        self._docs: list[Counter] = []

    def add(self, words: np.ndarray) -> int:
        if words.size and (int(words.min()) < 0 or int(words.max()) >= self._n_words):
            raise ValueError(
                f"词 id 超出范围 [0, {self._n_words})："
                f"min={int(words.min())} max={int(words.max())}"
            )
        self._docs.append(Counter(int(w) for w in words))
        return len(self._docs) - 1

    def build(self) -> "InvertedIndex":
        n_docs = len(self._docs)
        n_words = self._n_words

        df = np.zeros(n_words, np.int64)
        for tf in self._docs:
            for w in tf:
                df[w] += 1

        idf = np.zeros(n_words, np.float32)
        nonzero = df > 0
        if n_docs:
            idf[nonzero] = np.log(n_docs / df[nonzero]).astype(np.float32)

        # 每篇文档的 L2 归一化 tf-idf 权重
        per_word: list[list[tuple[int, float]]] = [[] for _ in range(n_words)]
        for doc_idx, tf in enumerate(self._docs):
            weights = {w: c * idf[w] for w, c in tf.items()}
            norm = float(np.sqrt(sum(v * v for v in weights.values())))
            if norm == 0.0:
                continue
            for w, v in weights.items():
                per_word[w].append((doc_idx, v / norm))

        offsets = np.zeros(n_words + 1, np.int64)
        for w in range(n_words):
            offsets[w + 1] = offsets[w] + len(per_word[w])
        total = int(offsets[-1])

        doc_ids = np.zeros(total, np.int32)
        weights_flat = np.zeros(total, np.float32)
        for w in range(n_words):
            start = int(offsets[w])
            for i, (doc_idx, weight) in enumerate(per_word[w]):
                doc_ids[start + i] = doc_idx
                weights_flat[start + i] = weight

        return InvertedIndex(n_docs, idf, offsets, doc_ids, weights_flat)


class InvertedIndex:
    def __init__(
        self,
        n_docs: int,
        idf: np.ndarray,
        offsets: np.ndarray,
        doc_ids: np.ndarray,
        weights: np.ndarray,
    ) -> None:
        self._n_docs = int(n_docs)
        self._idf = idf
        self._offsets = offsets
        self._doc_ids = doc_ids
        self._weights = weights

    @property
    def n_docs(self) -> int:
        return self._n_docs

    def query(self, words: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self._n_docs == 0 or words.size == 0 or top_k <= 0:
            return []

        qtf = Counter(int(w) for w in words)
        qw = {w: c * float(self._idf[w]) for w, c in qtf.items()}
        qnorm = float(np.sqrt(sum(v * v for v in qw.values())))
        if qnorm == 0.0:
            return []

        scores = np.zeros(self._n_docs, np.float32)
        for w, v in qw.items():
            start, end = int(self._offsets[w]), int(self._offsets[w + 1])
            if start == end:
                continue
            np.add.at(scores, self._doc_ids[start:end], self._weights[start:end] * (v / qnorm))

        k = min(top_k, self._n_docs)
        cand = np.argpartition(-scores, k - 1)[:k]
        cand = cand[np.argsort(-scores[cand], kind="stable")]
        return [(int(d), float(scores[d])) for d in cand]

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            n_docs=np.array([self._n_docs], np.int64),
            idf=self._idf,
            offsets=self._offsets,
            doc_ids=self._doc_ids,
            weights=self._weights,
        )

    @classmethod
    def load(cls, path: str | Path) -> "InvertedIndex":
        z = np.load(Path(path))
        return cls(
            n_docs=int(z["n_docs"][0]),
            idf=z["idf"],
            offsets=z["offsets"],
            doc_ids=z["doc_ids"],
            weights=z["weights"],
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_index.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/photoar/index.py tests/test_index.py
git commit -m "feat: TF-IDF 倒排索引与 Top-K 粗排（零 scipy 依赖）"
```

---

### Task 8: 两阶段检索编排与全链路指标

**Files:**
- Create: `src/photoar/recognizer.py`
- Test: `tests/test_recognizer.py`

**Interfaces:**
- Consumes: `photoar.features.extract`、`photoar.vocab.Vocab`、`photoar.index.InvertedIndex`、`photoar.descstore.DescStore`、`photoar.verify.{verify_pair, decide, Decision}`
- Produces:
  - 常量 `TOP_K = 20`
  - `TwoStageRecognizer(vocab: Vocab, index: InvertedIndex, store: DescStore, photo_ids: list[str], top_k: int = TOP_K)`，方法 `recognize(img_bgr: np.ndarray) -> Decision`、`candidates(img_bgr: np.ndarray) -> list[str]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_recognizer.py`：

```python
import numpy as np
import pytest

from photoar import features as F
from photoar import synth
from photoar import vocab as V
from photoar.bruteforce import BruteForceRecognizer
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TOP_K, TwoStageRecognizer


@pytest.fixture
def corpus(tmp_path, textured_image):
    """40 张合成图的完整语料：描述子库 + 词汇树 + 倒排索引。"""
    n = 40
    images = [textured_image(seed=s, w=900, h=650) for s in range(n)]
    ids = [f"p{i}" for i in range(n)]
    feats = [F.extract(img) for img in images]

    path = tmp_path / "desc.bin"
    with DescStoreWriter(path, capacity=n) as w:
        for f in feats:
            w.append(f)

    voc = V.train(np.vstack([f.desc for f in feats]), branching=6, depth=3, seed=0)
    builder = InvertedIndexBuilder(voc.n_words)
    for f in feats:
        builder.add(voc.words_of(f.desc))
    index = builder.build()

    store = DescStore(path)
    yield images, ids, voc, index, store
    store.close()


def test_recognizes_synthetic_query(corpus):
    images, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids)
    query, _ = synth.generate(images[7], count=1, seed=4)[0]
    d = rec.recognize(query)
    assert d.matched
    assert d.photo_id == "p7"


def test_rejects_photo_outside_library(corpus, textured_image):
    _, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids)
    assert not rec.recognize(textured_image(seed=98765)).matched


def test_candidates_are_capped_at_top_k(corpus):
    images, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids, top_k=5)
    assert len(rec.candidates(images[3])) <= 5


def test_coarse_stage_recalls_the_right_photo(corpus):
    """粗排召回率：Top-20 候选必须包含正确答案，否则精排再准也没用。"""
    images, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids, top_k=TOP_K)

    hits = 0
    for i in range(0, len(images), 4):
        query, _ = synth.generate(images[i], count=1, seed=100 + i)[0]
        if ids[i] in rec.candidates(query):
            hits += 1
    total = len(range(0, len(images), 4))
    assert hits >= total - 1


def test_agrees_with_bruteforce_on_matched_ids(corpus):
    """两阶段与暴力检索在"命中的是哪张"上必须一致。
    不断言两者的 matched 完全相同——粗排漏召回会让两阶段更保守，
    那是可接受的（漏检），但绝不允许指向不同的照片（误识别）。
    """
    images, ids, voc, index, store = corpus
    two = TwoStageRecognizer(voc, index, store, ids)
    brute = BruteForceRecognizer(store, ids)

    for i in range(0, len(images), 5):
        query, _ = synth.generate(images[i], count=1, seed=200 + i)[0]
        a, b = two.recognize(query), brute.recognize(query)
        if a.matched and b.matched:
            assert a.photo_id == b.photo_id


def test_blank_query_is_rejected(corpus):
    _, ids, voc, index, store = corpus
    rec = TwoStageRecognizer(voc, index, store, ids)
    assert not rec.recognize(np.full((400, 600, 3), 128, np.uint8)).matched


def test_id_count_must_match_index_and_store(corpus):
    _, ids, voc, index, store = corpus
    with pytest.raises(ValueError):
        TwoStageRecognizer(voc, index, store, ids[:-1])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_recognizer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.recognizer'`

- [ ] **Step 3: 实现 `src/photoar/recognizer.py`**

```python
"""两阶段检索编排（spec §8.3）。

  粗排：词汇树量化 -> 倒排索引取 Top-K
  精排：对候选逐个 ORB 匹配 + RANSAC，再由 verify.decide 做三条判定

只对 Top-K 候选从 mmap 随机读描述子，所以内存占用与图库大小无关。
"""

import numpy as np

from .descstore import DescStore
from .features import extract
from .index import InvertedIndex
from .verify import Decision, decide, verify_pair
from .vocab import Vocab

TOP_K = 20


class TwoStageRecognizer:
    def __init__(
        self,
        vocab: Vocab,
        index: InvertedIndex,
        store: DescStore,
        photo_ids: list[str],
        top_k: int = TOP_K,
    ) -> None:
        if not (len(photo_ids) == len(store) == index.n_docs):
            raise ValueError(
                f"三者数量必须一致：photo_ids={len(photo_ids)}、"
                f"store={len(store)}、index={index.n_docs}"
            )
        self._vocab = vocab
        self._index = index
        self._store = store
        self._ids = list(photo_ids)
        self._top_k = int(top_k)

    def _coarse(self, img_bgr: np.ndarray) -> list[int]:
        words = self._vocab.words_of(extract(img_bgr).desc)
        return [doc for doc, _ in self._index.query(words, self._top_k)]

    def candidates(self, img_bgr: np.ndarray) -> list[str]:
        return [self._ids[d] for d in self._coarse(img_bgr)]

    def recognize(self, img_bgr: np.ndarray) -> Decision:
        query = extract(img_bgr)
        words = self._vocab.words_of(query.desc)
        docs = [doc for doc, _ in self._index.query(words, self._top_k)]
        results = [
            verify_pair(query, self._store.read(doc), self._ids[doc]) for doc in docs
        ]
        return decide(results)
```

注意 `recognize` 没有复用 `_coarse`：那样会把 `extract` 跑两遍。`candidates` 只用于测试与诊断，多一次提取无所谓；`recognize` 是热路径，必须只提取一次。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_recognizer.py -v`
Expected: 7 passed

- [ ] **Step 5: 跑全链路数字并记录**

复用 Task 5 Step 6 的脚本，把 `BruteForceRecognizer` 换成 `TwoStageRecognizer`，库规模提到 1000 张：

```bash
python - <<'PY'
import numpy as np, cv2, tempfile, pathlib, time
from photoar import features as F, evaluate as E, vocab as V
from photoar.descstore import DescStore, DescStoreWriter
from photoar.index import InvertedIndexBuilder
from photoar.recognizer import TwoStageRecognizer

def make(seed, w=900, h=650):
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (h//8, w//8, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
    for _ in range(40):
        x1, y1 = int(rng.integers(0,w)), int(rng.integers(0,h))
        x2 = min(w-1, x1+int(rng.integers(20,120)))
        y2 = min(h-1, y1+int(rng.integers(20,120)))
        cv2.rectangle(img,(x1,y1),(x2,y2),tuple(int(c) for c in rng.integers(0,256,3)),-1)
    return img

N = 1000
ids = [f"p{i}" for i in range(N)]
imgs = {ids[i]: make(i) for i in range(N)}
feats = [F.extract(imgs[k]) for k in ids]

d = pathlib.Path(tempfile.mkdtemp())
with DescStoreWriter(d/"desc.bin", capacity=N) as w:
    for f in feats: w.append(f)

t0 = time.perf_counter()
voc = V.train(np.vstack([f.desc for f in feats]), seed=0)
print(f"词汇树训练耗时 {time.perf_counter()-t0:.1f}s，词数 {voc.n_words}")

b = InvertedIndexBuilder(voc.n_words)
for f in feats: b.add(voc.words_of(f.desc))
index = b.build()

with DescStore(d/"desc.bin") as store:
    rec = TwoStageRecognizer(voc, index, store, ids)
    sample = {k: imgs[k] for k in ids[:50]}
    print(E.evaluate(rec, sample, samples_per_ref=10, seed=1).as_report())
PY
```

追加到 `docs/superpowers/plans/phase0-results.md`，标题 `## 里程碑 0b：两阶段检索（1000 张合成库）`。

**这是第二个决策点。** 对照 Task 5 的 0a 数字判读：
- 误识别率上升 → 粗排引入了新的混淆。检查 Top-K 是否太大（把不相似的照片塞进候选反而制造歧义）。
- 漏检率大幅上升而误识别不变 → 粗排召回不足。先把 `TOP_K` 提到 30 试；仍不够则改用 ORBvoc.txt（见 Task 6 的 docstring）。
- 两者都持平 → 粗排是无损的，可以继续。

- [ ] **Step 6: Commit**

```bash
git add src/photoar/recognizer.py tests/test_recognizer.py docs/superpowers/plans/phase0-results.md
git commit -m "feat: 两阶段检索编排，记录里程碑 0b 结果"
```

---

### Task 9: arcoreimg 封装（质量分与 .imgdb 体积实测）

**Files:**
- Create: `src/photoar/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Consumes: 无（只依赖外部二进制）
- Produces:
  - 常量 `MIN_QUALITY_SCORE = 75`
  - `ArcoreimgMissing(RuntimeError)`、`QualityTooLow(ValueError)`（属性 `score: int`、`path: str`）
  - `eval_img(image_path: str | Path, arcoreimg: str = ARCOREIMG) -> int`
  - `build_single_target_db(image_path, name, print_width_m, out_path, arcoreimg=ARCOREIMG) -> int`（返回 `.imgdb` 字节数）
  - `assert_quality(image_path, arcoreimg=ARCOREIMG) -> int`（低于阈值抛 `QualityTooLow`）
  - 常量 `ARCOREIMG = "arcoreimg"`（默认值；仓库内已放置可用二进制于 `tools/arcoreimg`）

- [ ] **Step 1: 外部工具的真实接口（已实测，2026-07-28）**

二进制已获取并放在仓库内 `tools/arcoreimg`（已加入 `.gitignore`，不入库）：

- 来源：`https://raw.githubusercontent.com/google-ar/arcore-android-sdk/master/tools/arcoreimg/linux/arcoreimg`
- 5273584 字节，sha256 `2585423461c77c02d034ed5333c5054384a5d19ad212f581ad0274198ace60c0`
- ELF 64-bit x86-64，已 `chmod +x`

**实测到的接口（下面的实现必须匹配这个，而不是本计划早先版本里的猜测）：**

```
$ arcoreimg
Available actions: help, version, build-db, eval-db, eval-img

$ arcoreimg eval-img --help
Usage: arcoreimg eval-image --input_image_path=<some_file_path>
  --input_image_path:  Path of image to be evaluated. Currently only supports *.png, *.jpg and *.jpeg.

$ arcoreimg build-db --help
Usage: arcoreimg build-db --input_images_directory=<dir>|--input_image_list_path=<file> --output_db_path=<file>
  --input_image_list_path:
    Path of a text file where every line consists of the name, the absolute path and the
    width in meters (optional) of an image, separated by a '|'. e.g.:
        cat|path/to/cat_image.png|0.1
        little dog|/path/to/dog_image.jpg
  --input_images_directory:  所有图都用来建库
  --output_db_path:          输出库文件路径
```

**⚠️ 计划早先版本猜错了一处**：`build-db` **没有** `--input_image_path` 参数。必须走 `--input_image_list_path`，写一个临时清单文件，每行 `名称|绝对路径|物理宽度(米)`。

**这带来一个设计改进**：打印物理宽度是在**建库时烘进 `.imgdb`** 的，不需要客户端运行时用 `addImage(name, bitmap, widthInMeters)` 再传一遍。所以 `build_single_target_db` 必须接收 `print_width_m` 参数并写进清单行。

`eval-img` 的输出就是一个裸数字（例如 `100`），没有前缀文字。

**已实测的 `.imgdb` 体积（即里程碑 0c，见 `phase0-results.md`）**：单目标约 **4.2-4.4 KB**，3 目标库 12301 字节（≈4.1KB/目标，线性）。远低于原估的 30KB，也远低于 200KB 的「改架构」阈值 —— 所以下发 `.imgdb` 的方案成立，且 spec §4 的带宽估算可以往下修一个量级。

**同时实测到一个反直觉行为，必须记住**：同一张图内容，分辨率越高 `eval-img` 分数越低（1200×800→100、2400×1600→20、4000×3000→0）。这是合成纹理图的产物（噪声从 1/8 尺寸上采样，放大后只剩低频），说明**合成图不能用来验证质量分闸门**，只有真实照片能。因此本任务的单元测试一律走 fake 脚本，质量分闸门的真实行为留到里程碑 0d 验证。

- [ ] **Step 2: 写失败的测试**

创建 `tests/test_quality.py`：

```python
import os
import stat
import textwrap

import cv2
import numpy as np
import pytest

from photoar import quality as Q


@pytest.fixture
def fake_arcoreimg(tmp_path):
    """造一个假的 arcoreimg，让测试不依赖真实二进制。

    行为：eval-img 打印固定分数；build-db 写出一个固定大小的文件。
    """

    def _make(score: int = 85, db_bytes: int = 4_300, exit_code: int = 0):
        script = tmp_path / "arcoreimg"
        script.write_text(
            textwrap.dedent(f"""\
            #!/usr/bin/env python3
            # 模拟真实 arcoreimg 的接口（已实测，见计划 Task 9 Step 1）：
            #   eval-img --input_image_path=<path>        -> 打印裸数字
            #   build-db --input_image_list_path=<file> --output_db_path=<file>
            # 清单文件每行: 名称|绝对路径|物理宽度(米)
            import sys, pathlib
            argv = sys.argv[1:]
            if {exit_code} != 0:
                sys.stderr.write("boom\\n"); sys.exit({exit_code})

            def opt(prefix):
                for i, a in enumerate(argv):
                    if a.startswith(prefix):
                        return a.split("=", 1)[1] if "=" in a else argv[i + 1]
                return None

            if argv and argv[0] == "eval-img":
                if not opt("--input_image_path"):
                    sys.stderr.write("missing --input_image_path\\n"); sys.exit(2)
                print({score})
                sys.exit(0)

            if argv and argv[0] == "build-db":
                listing = opt("--input_image_list_path")
                out = opt("--output_db_path")
                if not listing or not out:
                    sys.stderr.write("missing required option\\n"); sys.exit(2)
                # 真实工具会因清单格式错误而失败；这里也校验，否则测试测不到格式
                for line in pathlib.Path(listing).read_text().splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("|")
                    if len(parts) not in (2, 3):
                        sys.stderr.write(f"bad list line: {{line}}\\n"); sys.exit(2)
                    if not pathlib.Path(parts[1]).is_absolute():
                        sys.stderr.write(f"path not absolute: {{parts[1]}}\\n"); sys.exit(2)
                pathlib.Path(out).write_bytes(b"X" * {db_bytes})
                sys.exit(0)

            sys.exit(2)
            """)
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    return _make


@pytest.fixture
def image_file(tmp_path, textured_image):
    path = tmp_path / "ref.jpg"
    cv2.imwrite(str(path), textured_image(seed=1))
    return path


def test_eval_img_parses_score(image_file, fake_arcoreimg):
    assert Q.eval_img(image_file, arcoreimg=fake_arcoreimg(score=88)) == 88


def test_eval_img_raises_when_binary_missing(image_file):
    with pytest.raises(Q.ArcoreimgMissing):
        Q.eval_img(image_file, arcoreimg="definitely-not-a-real-binary-xyz")


def test_eval_img_raises_on_nonzero_exit(image_file, fake_arcoreimg):
    with pytest.raises(RuntimeError):
        Q.eval_img(image_file, arcoreimg=fake_arcoreimg(exit_code=3))


def test_assert_quality_accepts_good_image(image_file, fake_arcoreimg):
    assert Q.assert_quality(image_file, arcoreimg=fake_arcoreimg(score=80)) == 80


def test_assert_quality_rejects_low_score(image_file, fake_arcoreimg):
    with pytest.raises(Q.QualityTooLow) as exc:
        Q.assert_quality(image_file, arcoreimg=fake_arcoreimg(score=40))
    assert exc.value.score == 40
    assert str(Q.MIN_QUALITY_SCORE) in str(exc.value)


def test_build_single_target_db_returns_size(tmp_path, image_file, fake_arcoreimg):
    out = tmp_path / "p1.imgdb"
    size = Q.build_single_target_db(
        image_file, name="p1", print_width_m=0.152, out_path=out,
        arcoreimg=fake_arcoreimg(db_bytes=4_312),
    )
    assert out.exists()
    assert size == 4_312


def test_build_single_target_db_rejects_nonascii_name(tmp_path, image_file, fake_arcoreimg):
    """arcoreimg 只支持 ASCII 文件名/目标名，提前拦住而不是让它神秘失败。"""
    with pytest.raises(ValueError):
        Q.build_single_target_db(
            image_file, name="外婆生日", print_width_m=0.152,
            out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
        )


def test_build_single_target_db_rejects_name_with_pipe(tmp_path, image_file, fake_arcoreimg):
    """清单文件用 '|' 分隔，名称里带 '|' 会把行结构破坏掉。"""
    with pytest.raises(ValueError):
        Q.build_single_target_db(
            image_file, name="a|b", print_width_m=0.152,
            out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
        )


def test_build_single_target_db_rejects_nonpositive_width(tmp_path, image_file, fake_arcoreimg):
    for bad in (0.0, -0.1):
        with pytest.raises(ValueError):
            Q.build_single_target_db(
                image_file, name="p1", print_width_m=bad,
                out_path=tmp_path / "x.imgdb", arcoreimg=fake_arcoreimg(),
            )


def test_build_single_target_db_writes_absolute_path_in_list(tmp_path, fake_arcoreimg, textured_image):
    """物理宽度是建库时烘进 .imgdb 的，清单行必须是 名称|绝对路径|宽度。

    fake 脚本会校验行格式与路径是否为绝对路径并在不合规时退出码非 0，
    所以这个测试真的能测到清单的写法，而不是只测到"没抛异常"。
    """
    import os

    sub = tmp_path / "photos"
    sub.mkdir()
    img_path = sub / "rel.jpg"
    cv2.imwrite(str(img_path), textured_image(seed=3))

    cwd = os.getcwd()
    os.chdir(tmp_path)  # 用相对路径调用，验证实现会自己转成绝对路径
    try:
        size = Q.build_single_target_db(
            "photos/rel.jpg", name="rel", print_width_m=0.089,
            out_path=tmp_path / "rel.imgdb", arcoreimg=fake_arcoreimg(),
        )
    finally:
        os.chdir(cwd)
    assert size > 0
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_quality.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.quality'`

- [ ] **Step 4: 实现 `src/photoar/quality.py`**

顶部 docstring 里放 Step 1 实测到的 `--help` 原文。下面是骨架，**参数要按实测修正**：

```python
"""arcoreimg 封装：参考图质量评分与单目标 .imgdb 生成。

外部工具契约（由 `arcoreimg --help` 实测，Step 1 记录）：
    <<< 把实测的 --help 输出原文粘贴在这里 >>>

只支持 PNG/JPEG，且文件名与目标名只支持 ASCII 字符。
质量分低于 MIN_QUALITY_SCORE 的照片在入库阶段就拒绝——留到扫不出来
才发现的代价高得多。
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MIN_QUALITY_SCORE = 75
ARCOREIMG = "arcoreimg"  # 仓库内已放置可用二进制于 tools/arcoreimg

_SCORE_RE = re.compile(r"(\d{1,3})")


class ArcoreimgMissing(RuntimeError):
    pass


class QualityTooLow(ValueError):
    def __init__(self, path: str, score: int) -> None:
        super().__init__(
            f"{path} 的 arcoreimg 质量分为 {score}，低于阈值 {MIN_QUALITY_SCORE}。"
            f"画面纹理不足（大片天空/纯色背景/过曝），考虑换图或加细纹理边框。"
        )
        self.path = path
        self.score = score


def _run(arcoreimg: str, args: list[str]) -> str:
    if shutil.which(arcoreimg) is None and not Path(arcoreimg).is_file():
        raise ArcoreimgMissing(
            f"找不到 arcoreimg（{arcoreimg}）。从 ARCore SDK for Android 的 "
            f"tools/arcoreimg/linux/ 取，或用 arcoreimg= 参数指定路径。"
        )
    proc = subprocess.run(
        [arcoreimg, *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"arcoreimg {' '.join(args)} 退出码 {proc.returncode}：{proc.stderr.strip()}"
        )
    return proc.stdout


def eval_img(image_path: str | Path, arcoreimg: str = ARCOREIMG) -> int:
    out = _run(arcoreimg, ["eval-img", f"--input_image_path={Path(image_path)}"])
    scores = _SCORE_RE.findall(out)
    if not scores:
        raise RuntimeError(f"无法从 arcoreimg 输出中解析质量分：{out!r}")
    return int(scores[-1])


def assert_quality(image_path: str | Path, arcoreimg: str = ARCOREIMG) -> int:
    score = eval_img(image_path, arcoreimg)
    if score < MIN_QUALITY_SCORE:
        raise QualityTooLow(str(image_path), score)
    return score


def build_single_target_db(
    image_path: str | Path,
    name: str,
    print_width_m: float,
    out_path: str | Path,
    arcoreimg: str = ARCOREIMG,
) -> int:
    """建一个只含这一张参考图的 .imgdb，并把打印物理宽度烘进去。

    物理宽度写在清单行里，所以客户端不需要在运行时再用
    addImage(name, bitmap, widthInMeters) 传一遍——库里已经带着它了。

    实测：单目标 .imgdb 约 4.2-4.4 KB（见 phase0-results.md 里程碑 0c）。
    """
    if not name.isascii():
        raise ValueError(f"arcoreimg 只支持 ASCII 目标名，收到 {name!r}")
    if "|" in name or "\n" in name:
        raise ValueError(f"目标名不能含 '|' 或换行（清单以 '|' 分隔），收到 {name!r}")
    if not print_width_m > 0:
        raise ValueError(f"打印物理宽度必须为正数（米），收到 {print_width_m!r}")

    image_path = Path(image_path).resolve()  # 清单要求绝对路径
    out_path = Path(out_path)
    if not image_path.name.isascii():
        raise ValueError(f"arcoreimg 只支持 ASCII 文件名，收到 {image_path.name!r}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 清单文件是临时产物，不留在用户目录里
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "targets.txt"
        listing.write_text(f"{name}|{image_path}|{print_width_m:.6f}\n")
        _run(
            arcoreimg,
            [
                "build-db",
                f"--input_image_list_path={listing}",
                f"--output_db_path={out_path}",
            ],
        )

    if not out_path.exists():
        raise RuntimeError(f"arcoreimg build-db 未产出 {out_path}")
    return out_path.stat().st_size
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_quality.py -v`
Expected: 7 passed

- [ ] **Step 6: 用真实 arcoreimg 实测 .imgdb 体积并记录**

```bash
python - <<'PY'
import cv2, numpy as np, tempfile, pathlib
from photoar import quality as Q

d = pathlib.Path(tempfile.mkdtemp())
rng = np.random.default_rng(0)
for i, (w, h) in enumerate([(1200, 800), (2400, 1600), (4000, 3000)]):
    base = rng.integers(0, 256, (h//8, w//8, 3), dtype=np.uint8)
    img = cv2.resize(base, (w, h), interpolation=cv2.INTER_LINEAR)
    for _ in range(60):
        x1, y1 = int(rng.integers(0,w)), int(rng.integers(0,h))
        x2, y2 = min(w-1, x1+int(rng.integers(40,240))), min(h-1, y1+int(rng.integers(40,240)))
        cv2.rectangle(img,(x1,y1),(x2,y2),tuple(int(c) for c in rng.integers(0,256,3)),-1)
    p = d / f"ref{i}.jpg"
    cv2.imwrite(str(p), img)
    score = Q.eval_img(p)
    size = Q.build_single_target_db(p, name=f"ref{i}", out_path=d / f"ref{i}.imgdb")
    print(f"{w}x{h}  质量分={score}  imgdb={size} 字节")
PY
```

追加到 `phase0-results.md`，标题 `## 里程碑 0c：单目标 .imgdb 实测体积`。

**这是第三个决策点**（spec §7 要求的实测）：
- 体积 ≤ 60KB → 按原计划下发 `.imgdb`
- 体积 > 200KB → 改为只下发缩略图、端上 `addImage()` 运行时构建，并回头更新 spec §4 的带宽估算

- [ ] **Step 7: Commit**

```bash
git add src/photoar/quality.py tests/test_quality.py docs/superpowers/plans/phase0-results.md
git commit -m "feat: arcoreimg 封装（质量分闸门 + 单目标 imgdb），记录体积实测"
```

---

### Task 10: ffmpeg 转码封装

**Files:**
- Create: `src/photoar/transcode.py`
- Test: `tests/test_transcode.py`

**Interfaces:**
- Consumes: 无（只依赖外部二进制）
- Produces:
  - 常量 `TARGET_HEIGHT = 720`、`MAX_DURATION_MS = 15_000`、`MAX_BITRATE = "1500k"`
  - `FfmpegMissing(RuntimeError)`
  - `VideoInfo` frozen dataclass：`width: int`、`height: int`、`duration_ms: int`、`faststart: bool`
  - `probe(path: str | Path, ffprobe: str = "ffprobe") -> VideoInfo`
  - `has_faststart(path: str | Path) -> bool`
  - `needs_transcode(info: VideoInfo) -> bool`
  - `transcode(src, dst, ffmpeg: str = "ffmpeg", max_duration_ms: int = MAX_DURATION_MS) -> None`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_transcode.py`：

```python
import shutil
import subprocess

import pytest

from photoar import transcode as T

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="需要 ffmpeg/ffprobe",
)


@pytest.fixture
def sample_video(tmp_path):
    """用 ffmpeg 自带的 testsrc 造视频，不依赖任何素材文件。"""

    def _make(name="in.mp4", w=1920, h=1080, seconds=20, faststart=False):
        path = tmp_path / name
        args = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
        ]
        if faststart:
            args += ["-movflags", "+faststart"]
        args.append(str(path))
        subprocess.run(args, check=True, capture_output=True)
        return path

    return _make


def test_probe_reads_dimensions_and_duration(sample_video):
    info = T.probe(sample_video(w=1280, h=720, seconds=3))
    assert (info.width, info.height) == (1280, 720)
    assert 2500 <= info.duration_ms <= 3500


def test_probe_raises_when_ffprobe_missing(sample_video):
    with pytest.raises(T.FfmpegMissing):
        T.probe(sample_video(seconds=1), ffprobe="not-a-real-ffprobe-xyz")


def test_has_faststart_detects_both_cases(sample_video):
    assert T.has_faststart(sample_video("fs.mp4", seconds=2, faststart=True))
    assert not T.has_faststart(sample_video("nofs.mp4", seconds=2, faststart=False))


def test_needs_transcode_for_oversized_video():
    assert T.needs_transcode(T.VideoInfo(1920, 1080, 8_000, True))


def test_needs_transcode_for_overlong_video():
    assert T.needs_transcode(T.VideoInfo(1280, 720, 20_000, True))


def test_needs_transcode_without_faststart():
    assert T.needs_transcode(T.VideoInfo(1280, 720, 8_000, False))


def test_no_transcode_when_already_compliant():
    assert not T.needs_transcode(T.VideoInfo(1280, 720, 8_000, True))


def test_transcode_produces_compliant_output(tmp_path, sample_video):
    src = sample_video(w=1920, h=1080, seconds=20, faststart=False)
    dst = tmp_path / "out.mp4"
    T.transcode(src, dst)

    info = T.probe(dst)
    assert info.height == T.TARGET_HEIGHT
    assert info.width % 2 == 0, "H.264 要求宽度为偶数，故用 scale=-2:720"
    assert info.duration_ms <= T.MAX_DURATION_MS + 500
    assert T.has_faststart(dst)
    assert not T.needs_transcode(info)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_transcode.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.transcode'`

- [ ] **Step 3: 实现 `src/photoar/transcode.py`**

```python
"""ffmpeg/ffprobe 封装：视频探测与转码到播放规格（spec §12）。

+faststart 是硬要求：没有它 moov box 在文件尾部，客户端无法边下边播。
scale=-2:720 而非 -1:720，保证宽度为偶数（H.264 的要求）。
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

TARGET_HEIGHT = 720
MAX_DURATION_MS = 15_000
MAX_BITRATE = "1500k"
BUF_SIZE = "3000k"
CRF = "26"
AUDIO_BITRATE = "96k"

_FASTSTART_PROBE_BYTES = 128 * 1024


class FfmpegMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration_ms: int
    faststart: bool


def _require(binary: str) -> None:
    if shutil.which(binary) is None and not Path(binary).is_file():
        raise FfmpegMissing(f"找不到 {binary}，请安装 ffmpeg 套件或用参数指定路径")


def has_faststart(path: str | Path) -> bool:
    """检查 moov 是否出现在 mdat 之前。

    直接读文件头判断，比解析 ffprobe 的 trace 输出稳得多。
    """
    head = Path(path).read_bytes()[:_FASTSTART_PROBE_BYTES]
    moov, mdat = head.find(b"moov"), head.find(b"mdat")
    if moov == -1:
        return False  # 头部没有 moov，说明它在后面
    return mdat == -1 or moov < mdat


def probe(path: str | Path, ffprobe: str = "ffprobe") -> VideoInfo:
    _require(ffprobe)
    path = Path(path)
    proc = subprocess.run(
        [
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{proc.stderr.strip()}")

    data = json.loads(proc.stdout)
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video is None:
        raise RuntimeError(f"{path} 里没有视频流")

    duration_s = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    return VideoInfo(
        width=int(video["width"]),
        height=int(video["height"]),
        duration_ms=int(round(duration_s * 1000)),
        faststart=has_faststart(path),
    )


def needs_transcode(info: VideoInfo) -> bool:
    return (
        info.height > TARGET_HEIGHT
        or info.duration_ms > MAX_DURATION_MS
        or not info.faststart
        or info.width % 2 != 0
    )


def transcode(
    src: str | Path,
    dst: str | Path,
    ffmpeg: str = "ffmpeg",
    max_duration_ms: int = MAX_DURATION_MS,
) -> None:
    _require(ffmpeg)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error",
            "-i", str(src),
            "-t", f"{max_duration_ms / 1000:.3f}",
            "-vf", f"scale=-2:{TARGET_HEIGHT}",
            "-c:v", "libx264", "-preset", "slow", "-crf", CRF,
            "-maxrate", MAX_BITRATE, "-bufsize", BUF_SIZE,
            "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(dst),
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败：{proc.stderr.strip()}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_transcode.py -v`
Expected: 8 passed（若本机无 ffmpeg 则全部 skip —— 那样必须先装 ffmpeg 再跑，不能把 skip 当通过）

- [ ] **Step 5: Commit**

```bash
git add src/photoar/transcode.py tests/test_transcode.py
git commit -m "feat: ffmpeg 转码封装（720p/1.5Mbps/15s/faststart）"
```

---

### Task 11: 语料构建与 CLI 串联

**Files:**
- Create: `src/photoar/corpus.py`
- Create: `src/photoar/cli.py`
- Test: `tests/test_corpus.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: 前面所有模块
- Produces:
  - `CorpusPaths` frozen dataclass：`root: Path`、`desc: Path`、`vocab: Path`、`index: Path`、`manifest: Path`、`imgdb_dir: Path`，类方法 `at(root) -> CorpusPaths`
  - `PhotoEntry` frozen dataclass：`photo_id: str`、`ref_path: str`、`quality_score: int`、`imgdb_bytes: int`
  - `build_corpus(image_paths: list[Path], out_root: Path, seed: int = 0, arcoreimg: str | None = None) -> list[PhotoEntry]`
  - `load_corpus(root: Path) -> tuple[TwoStageRecognizer, list[PhotoEntry]]`
  - `main(argv: list[str] | None = None) -> int`，子命令 `build` / `eval`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_corpus.py`：

```python
import json

import cv2
import pytest

from photoar import synth
from photoar.corpus import CorpusPaths, build_corpus, load_corpus


@pytest.fixture
def photo_dir(tmp_path, textured_image):
    d = tmp_path / "photos"
    d.mkdir()
    paths = []
    for i in range(12):
        p = d / f"img{i:03d}.jpg"
        cv2.imwrite(str(p), textured_image(seed=i, w=900, h=650))
        paths.append(p)
    return d, paths


def test_build_corpus_writes_all_artifacts(tmp_path, photo_dir):
    _, paths = photo_dir
    out = tmp_path / "corpus"
    entries = build_corpus(paths, out, seed=0, arcoreimg=None)

    p = CorpusPaths.at(out)
    assert p.desc.exists() and p.vocab.exists() and p.index.exists() and p.manifest.exists()
    assert len(entries) == len(paths)
    assert len({e.photo_id for e in entries}) == len(paths)


def test_manifest_is_valid_json_with_stable_order(tmp_path, photo_dir):
    _, paths = photo_dir
    out = tmp_path / "corpus"
    entries = build_corpus(paths, out, seed=0, arcoreimg=None)
    data = json.loads(CorpusPaths.at(out).manifest.read_text())
    assert [e["photo_id"] for e in data["photos"]] == [e.photo_id for e in entries]


def test_loaded_corpus_recognizes_its_own_photos(tmp_path, photo_dir):
    d, paths = photo_dir
    out = tmp_path / "corpus"
    build_corpus(paths, out, seed=0, arcoreimg=None)

    rec, entries = load_corpus(out)
    target = entries[5]
    img = cv2.imread(target.ref_path)
    query, _ = synth.generate(img, count=1, seed=3)[0]
    d_ = rec.recognize(query)
    assert d_.matched
    assert d_.photo_id == target.photo_id


def test_build_corpus_skips_unreadable_files(tmp_path, photo_dir):
    d, paths = photo_dir
    bad = d / "broken.jpg"
    bad.write_bytes(b"not an image")
    out = tmp_path / "corpus"
    entries = build_corpus(paths + [bad], out, seed=0, arcoreimg=None)
    assert len(entries) == len(paths)


def test_build_corpus_rejects_empty_input(tmp_path):
    with pytest.raises(ValueError):
        build_corpus([], tmp_path / "corpus", seed=0, arcoreimg=None)


def test_quality_gate_is_skipped_when_arcoreimg_is_none(tmp_path, photo_dir):
    """arcoreimg=None 时质量分记 -1，表示未评估；不应因缺少二进制而失败。"""
    _, paths = photo_dir
    entries = build_corpus(paths, tmp_path / "c", seed=0, arcoreimg=None)
    assert all(e.quality_score == -1 for e in entries)
    assert all(e.imgdb_bytes == 0 for e in entries)
```

创建 `tests/test_cli.py`：

```python
import cv2
import pytest

from photoar.cli import main


@pytest.fixture
def photo_dir(tmp_path, textured_image):
    d = tmp_path / "photos"
    d.mkdir()
    for i in range(10):
        cv2.imwrite(str(d / f"img{i:03d}.jpg"), textured_image(seed=i, w=900, h=650))
    return d


def test_build_then_eval_prints_report(tmp_path, photo_dir, capsys):
    corpus = tmp_path / "corpus"
    assert main(["build", "--photos", str(photo_dir), "--out", str(corpus)]) == 0
    capsys.readouterr()

    rc = main(["eval", "--corpus", str(corpus), "--samples", "3", "--limit", "5"])
    out = capsys.readouterr().out
    assert "正确命中" in out and "误识别" in out and "结论" in out
    assert rc in (0, 1)  # 0 = 达标, 1 = 未达标；两者都算命令成功执行


def test_eval_exit_code_signals_baseline(tmp_path, photo_dir, capsys):
    """eval 的退出码必须能被 CI 用：达标 0、未达标 1。"""
    corpus = tmp_path / "corpus"
    main(["build", "--photos", str(photo_dir), "--out", str(corpus)])
    capsys.readouterr()
    rc = main(["eval", "--corpus", str(corpus), "--samples", "3", "--limit", "5"])
    out = capsys.readouterr().out
    assert (rc == 0) == ("达标" in out and "未达标" not in out)


def test_build_on_empty_directory_errors(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["build", "--photos", str(empty), "--out", str(tmp_path / "c")]) == 2
    assert "没有找到" in capsys.readouterr().err


def test_eval_on_missing_corpus_errors(tmp_path, capsys):
    assert main(["eval", "--corpus", str(tmp_path / "nope")]) == 2
    assert capsys.readouterr().err


def test_no_subcommand_shows_usage(capsys):
    assert main([]) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_corpus.py tests/test_cli.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'photoar.corpus'`

- [ ] **Step 3: 实现 `src/photoar/corpus.py`**

```python
"""语料构建与加载。

Phase 0 的产物是纯文件，不引入 SQLite —— 数据库是 Phase 1 随服务
一起引入的。产物布局：
    <root>/desc.bin        定长描述子库
    <root>/vocab.npz       词汇树
    <root>/index.npz       倒排索引
    <root>/manifest.json   photo_id 顺序与元数据（顺序即 slot/doc 下标）
    <root>/imgdb/<id>.imgdb  单目标库（仅在提供 arcoreimg 时生成）

manifest 里 photos 的顺序就是描述子库 slot 下标与倒排索引 doc 下标，
三者必须始终一致。
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from . import quality as Q
from . import vocab as V
from .descstore import DescStore, DescStoreWriter
from .features import extract
from .index import InvertedIndexBuilder, InvertedIndex
from .recognizer import TwoStageRecognizer

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


@dataclass(frozen=True)
class CorpusPaths:
    root: Path
    desc: Path
    vocab: Path
    index: Path
    manifest: Path
    imgdb_dir: Path

    @classmethod
    def at(cls, root: str | Path) -> "CorpusPaths":
        root = Path(root)
        return cls(
            root=root,
            desc=root / "desc.bin",
            vocab=root / "vocab.npz",
            index=root / "index.npz",
            manifest=root / "manifest.json",
            imgdb_dir=root / "imgdb",
        )


@dataclass(frozen=True)
class PhotoEntry:
    photo_id: str
    ref_path: str
    quality_score: int  # -1 = 未评估（未提供 arcoreimg）
    imgdb_bytes: int  # 0 = 未生成


def _photo_id(path: Path) -> str:
    """由内容指纹派生 id，同一张图重复入库得到同一个 id。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def build_corpus(
    image_paths: list[Path],
    out_root: str | Path,
    seed: int = 0,
    arcoreimg: str | None = None,
) -> list[PhotoEntry]:
    if not image_paths:
        raise ValueError("build_corpus 需要至少一张图片")

    paths = CorpusPaths.at(out_root)
    paths.root.mkdir(parents=True, exist_ok=True)

    entries: list[PhotoEntry] = []
    feats = []
    for path in sorted(Path(p) for p in image_paths):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            continue
        f = extract(img)
        if len(f) == 0:
            continue

        photo_id = _photo_id(path)
        score, imgdb_bytes = -1, 0
        if arcoreimg is not None:
            try:
                score = Q.assert_quality(path, arcoreimg=arcoreimg)
                imgdb_bytes = Q.build_single_target_db(
                    path, name=photo_id,
                    out_path=paths.imgdb_dir / f"{photo_id}.imgdb",
                    arcoreimg=arcoreimg,
                )
            except Q.QualityTooLow:
                continue

        feats.append(f)
        entries.append(
            PhotoEntry(
                photo_id=photo_id,
                ref_path=str(path),
                quality_score=score,
                imgdb_bytes=imgdb_bytes,
            )
        )

    if not entries:
        raise ValueError("没有任何图片通过入库（可能全部不可读或质量分不达标）")

    with DescStoreWriter(paths.desc, capacity=len(feats)) as w:
        for f in feats:
            w.append(f)

    voc = V.train(np.vstack([f.desc for f in feats]), seed=seed)
    voc.save(paths.vocab)

    builder = InvertedIndexBuilder(voc.n_words)
    for f in feats:
        builder.add(voc.words_of(f.desc))
    builder.build().save(paths.index)

    paths.manifest.write_text(
        json.dumps(
            {"version": 1, "photos": [asdict(e) for e in entries]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return entries


def load_corpus(root: str | Path) -> tuple[TwoStageRecognizer, list[PhotoEntry]]:
    paths = CorpusPaths.at(root)
    for required in (paths.desc, paths.vocab, paths.index, paths.manifest):
        if not required.exists():
            raise FileNotFoundError(f"语料不完整，缺少 {required}")

    data = json.loads(paths.manifest.read_text())
    entries = [PhotoEntry(**e) for e in data["photos"]]
    rec = TwoStageRecognizer(
        vocab=V.Vocab.load(paths.vocab),
        index=InvertedIndex.load(paths.index),
        store=DescStore(paths.desc),
        photo_ids=[e.photo_id for e in entries],
    )
    return rec, entries
```

- [ ] **Step 4: 实现 `src/photoar/cli.py`**

```python
"""photoar 命令行入口。

    photoar build --photos <目录> --out <语料目录> [--arcoreimg <路径>]
    photoar eval  --corpus <语料目录> [--samples 20] [--limit N] [--seed 1]

eval 的退出码：0 = 达到 spec §14.2 基线，1 = 未达标，2 = 用法或环境错误。
退出码可直接被 CI 使用。
"""

import argparse
import sys
from pathlib import Path

import cv2

from .corpus import IMAGE_SUFFIXES, build_corpus, load_corpus
from .evaluate import evaluate


def _cmd_build(args: argparse.Namespace) -> int:
    photo_dir = Path(args.photos)
    paths = sorted(
        p for p in photo_dir.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES
    )
    if not paths:
        print(f"在 {photo_dir} 下没有找到图片（支持 {sorted(IMAGE_SUFFIXES)}）",
              file=sys.stderr)
        return 2

    entries = build_corpus(paths, args.out, seed=args.seed, arcoreimg=args.arcoreimg)
    print(f"入库 {len(entries)} 张，语料写入 {args.out}")
    if args.arcoreimg:
        sizes = [e.imgdb_bytes for e in entries if e.imgdb_bytes]
        if sizes:
            print(
                f".imgdb 体积  最小 {min(sizes)}  中位 {sorted(sizes)[len(sizes)//2]}  "
                f"最大 {max(sizes)} 字节"
            )
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    try:
        rec, entries = load_corpus(args.corpus)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    chosen = entries[: args.limit] if args.limit else entries
    refs = {}
    for e in chosen:
        img = cv2.imread(e.ref_path, cv2.IMREAD_COLOR)
        if img is not None:
            refs[e.photo_id] = img
    if not refs:
        print("参考图都读不出来，检查 manifest 里的 ref_path 是否还有效",
              file=sys.stderr)
        return 2

    metrics = evaluate(rec, refs, samples_per_ref=args.samples, seed=args.seed)
    print(f"图库规模    {len(entries)}")
    print(f"评估参考图  {len(refs)}")
    print(metrics.as_report())
    return 0 if metrics.meets_baseline else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="photoar")
    sub = parser.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="从照片目录构建识别语料")
    b.add_argument("--photos", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--arcoreimg", default=None,
                   help="arcoreimg 路径；省略则跳过质量分与 .imgdb 生成")
    b.set_defaults(func=_cmd_build)

    e = sub.add_parser("eval", help="用合成查询图评估识别率")
    e.add_argument("--corpus", required=True)
    e.add_argument("--samples", type=int, default=20)
    e.add_argument("--limit", type=int, default=0, help="只评估前 N 张，0 = 全部")
    e.add_argument("--seed", type=int, default=1)
    e.set_defaults(func=_cmd_eval)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_usage(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 运行全部测试确认通过**

Run: `python -m pytest -v`
Expected: 全部 passed（`test_transcode.py` 在无 ffmpeg 时 skip）

- [ ] **Step 6: 用真实照片跑最终验收数字**

这一步需要用户提供一个真实照片目录（**只读，不修改**）。理想是上万张，先用能拿到的最大规模：

```bash
photoar build --photos /path/to/real/photos --out /tmp/photoar-corpus --arcoreimg arcoreimg
photoar eval  --corpus /tmp/photoar-corpus --samples 20
echo "退出码: $?"
```

把完整输出追加到 `docs/superpowers/plans/phase0-results.md`，标题 `## 里程碑 0d：真实照片最终验收`，并注明照片张数与来源目录。

**这是 Phase 0 的终点，也是整个项目的 go/no-go：**
- 退出码 0 → Phase 0 通过，可以开始 Phase 1
- 退出码 1 且**误识别率超标** → 优先调 `MIN_INLIERS`、`RATIO`（往严的方向），重跑
- 退出码 1 但只是**正确命中率不足、误识别为 0** → 调 `TOP_K`（往大）、词汇树 `depth`（往小），或换用 ORBvoc.txt
- 反复调不上去 → 回到 spec §16 的备选路径，与用户讨论"照片背面印二维码"的兜底方案

- [ ] **Step 7: Commit**

```bash
git add src/photoar/corpus.py src/photoar/cli.py tests/test_corpus.py tests/test_cli.py \
        docs/superpowers/plans/phase0-results.md
git commit -m "feat: 语料构建与 CLI，记录里程碑 0d 真实照片验收结果"
```

---

## Phase 0 完成标准

1. `python -m pytest` 全绿（ffmpeg 相关可 skip 但需在有 ffmpeg 的机器上跑过一次）
2. `docs/superpowers/plans/phase0-results.md` 含四个里程碑的数字：
   - 0a 几何校验判别力（暴力检索，小库）
   - 0b 两阶段检索（1000 张合成库）
   - 0c 单目标 `.imgdb` 实测体积
   - 0d 真实照片最终验收
3. `photoar eval` 在真实照片上退出码为 0
4. 回头更新 spec 的两处数字：§6/§8.4 的描述子存储从 96MB 改为 120MB（漏算了关键点坐标）；§7 的 `.imgdb` 30KB 估算改为 0c 的实测值

## 自查记录

对照 spec 逐节检查覆盖情况：

| spec 章节 | 覆盖 |
|---|---|
| §8.1 入库侧（质量分、640px、300 ORB、build-db） | Task 1（640px/300 ORB）、Task 9（质量分/build-db）、Task 11（串联） |
| §8.2 词汇树 | Task 6（自训，偏离已在文档开头说明并给出理由与回退路径） |
| §8.3 两阶段 + 三条判定 | Task 3（判定）、Task 8（两阶段） |
| §8.4 资源预算 | Task 4（描述子存储，并修正 96MB → 120MB）、Task 8 Step 5（延迟实测） |
| §12 视频规格 | Task 10 |
| §14.1 合成查询图 | Task 2 |
| §14.2 验收基线与三分类定义 | Task 5（`Metrics`/`meets_baseline`） |
| §14.3 单元测试（识别相关部分） | Task 1/3/4/6/7 |
| §7 `.imgdb` 体积实测要求 | Task 9 Step 6 |

**Phase 0 范围外**（属于后续 Phase，本计划不覆盖）：§5.3 `fs-browser`、§5.4 `media-resolve`、§6 SQLite schema、§7 其余 HTTP 接口、§9 endpoint 多通道、§10 媒体策略链、§11 客户端状态机、§13 错误处理的客户端部分、§14.3 路径穿越测试、§14.4 集成测试。这些在 Phase 1-3。
