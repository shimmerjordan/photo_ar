# bench/ —— Phase 0 的测量脚本

`docs/superpowers/plans/phase0-results.md` 里每个数字都由这里的某个脚本产出。
放进仓库是因为那份结果文档声称"命令可复现"，而脚本原先躺在 gitignore 的临时
工作区里 —— 工作区一删，引用就成了空指针。

这些脚本不是产品代码，也不被 pytest 收集（`testpaths = ["tests"]`）。判定逻辑
一律放在 `src/photoar/` 下有测试的模块里，脚本只负责 IO、编排和进度输出 ——
散在脚本里的判断没人测得到。

跑之前先 `pip install -e ".[dev]"`，或者用 `PYTHONPATH=src`。

## `logs/` —— 原始运行输出

`logs/*.log` 是产出结果文档里那些数字的**那几次实际运行**的原始输出，一并入库
的理由和脚本相同：结果文档逐行引用它们（"日志：`bench/logs/measure-0b-br16.log`"），
而它们原先躺在 gitignore 的临时工作区里。共 64K。

这些是历史存档，**不要覆盖**：重跑请写到新文件。文件名保留了当时的连字符命名
（脚本搬进仓库时按 Python 模块惯例改成了下划线，日志没跟着改，以免结果文档里
的引用与实际产出这些数字的那次运行对不上号）。

## 里程碑测量

| 脚本 | 回答什么 | 数据 | 量级 |
|---|---|---|---|
| `measure_0a.py` | 几何校验本身的判别力（暴力检索，不含词汇表变量） | 合成 | 200 库 × 200 查询，约 2 分钟 |
| `measure_0b.py` | 两阶段检索的误识别率 / 召回率 / 延迟，含暴力对照 | 合成 | 1000 库，约 10 分钟 |
| `measure_0b_vocab_sweep.py` | 词表粒度（branching/depth）对粗排召回率的影响 | 合成 | 4 个配置 × 1000 库，约 40 分钟 |

`measure_0b.py` 支持用环境变量覆盖词表参数，不改模块默认值：

```bash
PHOTOAR_BRANCHING=16 PHOTOAR_DEPTH=4 python bench/measure_0b.py
```

⚠️ 合成图（`make(seed)` 的随机纹理）是**互相独立**的，不具备 Phase 0 真正要
检验的那个性质 —— "上万张**高度自相似**的照片能否区分"。所以 0a/0b 的数字是
乐观上界，不是预测。真实数字看 0d。

## 真实照片语料（里程碑 0d）

```bash
# 1) 取数：标准检索基准集，同一批地标的大量不同视角照片
python bench/fetch_dataset.py --dataset oxford5k --out ~/photoar-data

# 2) 必做的前置：剔除近似重复（不做的话下面的数字全是错的，见下）
python bench/dedup_scan.py --photos ~/photoar-data/photos \
    --out ~/photoar-data/dedup --out-clean ~/photoar-data/clean

# 3) 建语料 + 评估（这就是产品路径，不是特制脚本）
photoar build --photos ~/photoar-data/clean --out ~/photoar-data/corpus \
    --holdout-frac 0.1 --seed 1
photoar eval --corpus ~/photoar-data/corpus --samples 10 --limit 500 --seed 1
```

### 第 2 步不能跳过

`corpus.build_corpus` 的 `_photo_id` 是内容哈希，只挡得住**字节完全相同**的
重复。重新编码 / 裁切 / 不同分辨率导出的近似重复哈希不相等，会两份都入库，
然后互相触发 `verify.RATIO=1.5` 判 `ambiguous` —— **两份都永久漏检**。
0d 先导语料实测：不清理 93.75% / 库外假阳性 32.7%，清理后 98.96% / 0%。
判定逻辑在 `photoar.dedup`（有单元测试），`dedup_scan.py` 只是它的命令行外壳。

### 第 3 步必须限幅，并写明覆盖面

实测 **310 ms/查询**墙上时钟，其中 78% 是合成查询图生成（真实高分辨率图远贵
于合成图）—— 那不是产品路径的成本，但确实要等。外推：

| 照片数 | `--samples` | 查询数 | 预计墙上时钟 |
|---|---|---|---|
| 1000 | 20 | 2 万 | 1h43m |
| 10000 | 10 | 10 万 | 8h37m |
| 10000 | 20 | 20 万 | 17h14m |

所以必须用 `--limit` / `--samples` 限幅，**并在记录结果时写明覆盖了多少张、
每张多少样本** —— 否则那个数字来自一次覆盖未知的截断运行。
注意 `--limit` 只影响**评估的参考图张数**，不影响图库规模：图库仍是全部入库
照片，粗排要在全库上竞争，所以限幅不会让识别变简单。

## `fetch_real_photos.py`（已弃用，保留作记录）

原先从 Wikimedia Commons 取数。**不要再用**，两个原因：

1. Wikimedia 现在直接以 robot policy 拒绝，不只是限流：
   `HTTP 429: Your request does not comply with our robot policy`。
   这是明确的"不欢迎自动访问"，不该继续敲。
2. 那条 `categorymembers` 查询本身低效：实测 `Category:Trevi Fountain` 的
   `gcmtype=file` 返回 **0** 个文件（照片都在别处），所以 40 个分类只取到
   153 张，1.5 小时。`generator=search` 一次能返回 50 个合格 jpg，但绕不开
   第 1 条。

另记两条已验证的不可达：**huggingface.co 从本机超时**，`hf-mirror.com` 只有
首页通、实际路径超时 —— 所以 INRIA Holidays 的 HF 镜像这条路是断的。
github / zenodo / figshare / robots.ox.ac.uk 均正常。
