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

| 脚本 | 回答什么 | 量级（Oxford5k 5063 张） |
|---|---|---|
| `fetch_dataset.py` | 取数（HTTP Range 六段并行，约 2.6 MB/s） | 1.9 GB，约 13 分钟 |
| `dedup_scan.py` | 哪些照片互为近似重复、该留哪一张 | 约 18 分钟 |
| `classify_fp.py` | 每条库外假阳性是"漏掉的近重复"还是"真实误识别" | 与假阳性条数成正比 |

```bash
# 1) 取数：标准检索基准集，同一批地标的大量不同视角照片
python bench/fetch_dataset.py --dataset oxford5k --out ~/photoar-data

# 2) 必做的前置：剔除近似重复（不做的话下面的数字全是错的，见下）
python bench/dedup_scan.py --photos ~/photoar-data/photos \
    --out ~/photoar-data/dedup --out-clean ~/photoar-data/clean

# 3) 建语料 + 评估（这就是产品路径，不是特制脚本）
photoar build --photos ~/photoar-data/clean --out ~/photoar-data/corpus \
    --holdout-frac 0.1 --seed 1
photoar eval --corpus ~/photoar-data/corpus --samples 10 --limit 500 --seed 1 \
    2>&1 | tee ~/photoar-data/eval.log

# 4) 库外假阳性不为 0 时必做：把每条假阳性归类
python bench/classify_fp.py --corpus ~/photoar-data/corpus \
    --eval-log ~/photoar-data/eval.log --out ~/photoar-data/fp.json
```

### 第 2 步不能跳过

`corpus.build_corpus` 的 `_photo_id` 是内容哈希，只挡得住**字节完全相同**的
重复。重新编码 / 裁切 / 不同分辨率导出的近似重复哈希不相等，会两份都入库，
然后互相触发 `verify.RATIO=1.5` 判 `ambiguous` —— **两份都永久漏检**。
0d 先导语料实测：不清理 93.75% / 库外假阳性 32.7%，清理后 98.96% / 0%。
判定逻辑在 `photoar.dedup`（有单元测试），`dedup_scan.py` 只是它的命令行外壳。

判据是 **ratio test**（`min(自匹配分) < RATIO × 互查内点数`），不是"内点数超过
某个绝对阈值"。差别不是调参级的：Oxford5k 5058 张上，`>= MIN_INLIERS` 判出
1801 对、ratio test 判出 358 对，配上错误的选择算法会多剔 3.9 倍照片
（14.3% vs 3.7%）。别照抄先导语料上"内点数直方图有空隙"的结论——那是 153 张
小语料的假象，5063 张上是连续谱。详见结果文档的"里程碑 0d 上规模"。

`dedup_scan.py` 只对比粗排 Top-K 候选对（O(N·K)，全对比在 5058 张上要约 9.5
小时），所以 `keep.txt` 保证的是"被测过的对里不冲突"，不是"任意两张都不冲突"。
漏掉的近重复会在 eval 里表现为库外假阳性——这就是第 4 步存在的原因。

### 第 3 步必须限幅，并写明覆盖面

实测 **310 ms/查询**墙上时钟，其中 78% 是合成查询图生成（真实高分辨率图远贵
于合成图）—— 那不是产品路径的成本，但确实要等。外推：

| 照片数 | `--samples` | 查询数 | 预计墙上时钟 |
|---|---|---|---|
| 1000 | 20 | 2 万 | 1h43m |
| 10000 | 10 | 10 万 | 8h37m |
| 10000 | 20 | 20 万 | 17h14m |

所以必须用 `--limit` / `--samples` 限幅，**并在记录结果时写明覆盖了多少张、
每张多少样本** —— 否则那个数字来自一次覆盖未知的截断运行。现在 `eval` 的报告
自己会打分母（`评估参考图 1000/4385（--limit 1000，等间距抽样）`），不用再靠
人记住当时传了什么。

`--limit` 的语义有两处容易记错：

- 它**等间距抽样**，不是取前 N 张。取前 N 张的话覆盖面由文件名排序决定：
  Oxford5k 的 manifest 按路径排序，前 500 张只落在 ashmolean / balliol /
  all_souls / bodleian 四个分组里，语料里最大也最自相似的 oxford(1502) /
  magdalen(685) / christ_church(543) 一张都没覆盖到——量出来的是"四个地标的
  识别率"，而文档里只会写着"评估了 500 张"。
- 它**同时**约束库内参考图和库外留出图。2026-07-29 之前只截断库内那一层，
  留出集是无上限遍历的：1 万张 + `--holdout-frac 0.1` + `--samples 10` 就是
  1 万次库外查询，`--limit 100` 照样跑满，限幅形同失效。

`--limit` 不影响图库规模：图库仍是全部入库照片，粗排要在全库上竞争，所以
限幅不会让识别变简单。

上面的命令用 `2>&1 | tee` 而不是只看 stdout，是因为两类信息走 stderr：进度行
（`[eval] 库内参考图 250/1000  已用 12.3min  预计还需 36.9min`）和库外误识别
命中详情（第 4 步要吃的就是它）。1000 张 × 20 样本这一档要跑约 1 小时，
没有进度行就只能靠 `ps` 猜它是在跑还是卡死了。

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
