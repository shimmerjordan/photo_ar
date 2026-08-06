# 决策记录：识别管线、用户体系、一键部署

这一轮改动的每一个选择、依据、以及**代价**。写给三个月后想改这些参数的人。

凡是带数字的结论都注明了是**实测**还是**推断**。推断的地方明确说是推断，不掩饰。
实测环境统一说明一次：

- **本机**：16 核 x86-64（带 AVX2），限到 3 CPU / 3GiB 来模拟 NAS 配额
- **目标机**：QNAP TS-464C2，Intel Celeron N5095，4 核，容器限 3 CPU / 3GiB
- ⚠️ **N5095 没有 AVX/AVX2，只到 SSE4.2**。所有本机数字到目标机都会变慢，且神经网络
  推理这类纯 GEMM 负载受影响最重。
- **手机**：小米 M2012K11C / Edge for Android 150，走 Tailscale 或 adb reverse。
  第 12 轮起真机数字逐步补上了；早期条目里"本轮没有任何数字是在真机上量的"那句话
  只对它自己那一轮成立。

## 怎么读这份文件

**按轮次追加，条目编号（`§N`）永不重排** —— 代码注释里到处引用它们。某个决定后来被
推翻了，那一条也留在原地，推翻它的那一条写在后面并指回去（§0.1 推翻 §0、§35.2 推翻
§32 对遮挡的判断、§36 下掉了整个安卓客户端）。

所以**这份文件里有一批条目讲的是已经不存在的东西**。哪些、还剩多少参考价值，
列在 [README.md 的「读 decisions.md 之前」](README.md)那一节 —— 先看那张表，
能省掉读 §9.1 那 213 行 APK 打包的时间。

---

## 0. 与需求的对应关系

| 你提的 | 结论 |
|---|---|
| ① 需要 3D 位姿，视频要跟随视角 transform | Android 侧走 ARCore Augmented Image + 世界追踪（SLAM），真 6DoF 位姿，见 §9；**运行时打进我们自己的包**，宾客不用另装 App、不需要 Play 商店，见 §9.1 |
| ② 照片只是触发条件和画布，不必是视频里的一帧 | 数据模型本来就没耦合；本轮补上了**贴合模式**（裁切填满 / 完整放入），见 §7 |
| ③ 先完善安卓，把计算放到端上 | **识别与贴合已全部在端上**：服务端预建整库 ARCore 目标库下发，手机 deserialize 后本地识别，热路径不碰网络，见 §0.1。端上 XFeat + `/v1/recognize/features` 作为溢出兜底保留，见 §8 |
| 服务器只做索引/传输/管理 | **第二阶段的架构修正**，见 §0.1 |
| ④ 不要 token，改成用户 + 网页管理界面 | 已实现：访客只输名字、管理员加口令、按用户授权照片、零构建管理台，见 §6 |
| ⑤ 用开源预训练模型，不自己训 | 选了 **XFeat**（Apache-2.0），见 §1；粗排为什么仍然是词汇树见 §5 |
| ⑥ compose 一键部署，启动前配置进 compose，其余进管理台 | 已实现，见 §7 |
| ⑦ 完成后本地验证（限 NAS 配额） | 已在 3 CPU / 3GiB 下端到端验证，见 §10 |
| ⑧ 更新文档 | 本文件 + `README*.md` + `docs/deploy.md` + `bench/README.md` |

---

## 0.1 架构修正：服务端退出识别热路径

### 修正了什么

第一阶段我把提特征挪到了手机上，但**配对仍然在服务端**（`POST /v1/recognize/features`）。
而配对恰好是 XFeat 的成本所在（20 候选 490ms 中的绝大部分），所以那次搬迁**没有解决
任何性能问题**。

正确的划分是：

| | 职责 |
|---|---|
| **服务端** | 资源索引（建目标库、目录）、传输（下发目标库与视频）、管理（用户/权限/配置） |
| **手机** | 识别 + 贴合 + 播放。**热路径不碰网络** |

### 这件事的一半本来就已经做好了

`ScanController.onTracking` 在 SCANNING 状态下，ARCore 一报出被跟踪的目标就立刻
`tryLocalHit`，**不等那 400ms 一轮的服务端识别**。也就是说端上优先已经是既有语义。

缺口只有一个：本地 ARCore 多图库是**端上用 640px 缩略图现建的**（`LocalTargetDb` 调
`addImage`），代价三条：

- `addImage` **每张约 30ms**（ARCore 官方数字）→ 200 张约 **6 秒**
- 特征来自缩略图，跟踪质量比服务端用原图预建的低一档（`NoticeKind.LOCAL_HIT` 就是在
  提示这件事）
- 受端侧缓存条数上限约束（默认 200）

### 补法

`arcoreimg build-db` 的清单格式本来就是 **`名称|绝对路径|宽度(米)` 一行一个目标**，支持
一次建多目标 —— 既有的 `build_single_target_db` 只是没用这个能力。所以：

1. 服务端 `quality.build_multi_target_db()` 一次建整库；
2. `GET /v1/targets/db`（imgdb 字节，ETag/304）+ `GET /v1/targets/manifest`（元数据）；
3. 手机下载后 `AugmentedImageDatabase.deserialize` 装进 session ——
   **5MB 约 10–20ms**（官方数字），比现建快两个数量级，而且特征来自原图。

ARCore 的硬限制（[官方文档](https://developers.google.com/ar/develop/augmented-images)）：
一个库最多 **1000 张**、每条约 **6 KB**、同时最多跟踪 20 张、一个 session 只能有一个活动库。
1000 目标的库约 6MB，一次下载。

### 版本按内容哈希，不按用户

`version = sha256(照片 id 升序 | 打印宽度 | 参考图 sha256)`，截断 16 hex。三个好处：

- 授权集相同的两个用户**天然共用同一个文件**，不用按用户各存一份；
- 任何一张参考图内容变了、或授权集变了，版本自动变；
- ETag 直接用它，客户端的 304 判断是**精确**的（而不是靠 mtime 猜）。

刻意**不含** title / fitMode / hasVideo —— 它们不是 imgdb 的输入，放进去会让「改一个标题」
触发全体客户端重下整库。也刻意不在请求路径上重算文件哈希（1000 张原图是几百 MB 读盘），
用 catalog 里由 `integrity` 维护的 `asset.sha256`。

### 超过 1000 张怎么办

取 `created_at` 最新的 1000 张，其余计入 `overflow` 并在缓存管理页显示。**不需要任何新的
兜底逻辑**：端上未命中本来就会自然落回服务端 `/v1/recognize` 那条路。

### 构建是后台的，请求不阻塞

当初按「可能几十秒」设计：构建放后台守护线程，请求立刻回 **503 + `Retry-After`**；同一版本
只建一次；写临时文件再 `os.replace`；失败记 30 秒冷却并以 500 `targets_build_failed` 露出
（与「正在建」分开，否则运维看到的只是"一直在建"）。

**已实测**（真 `arcoreimg`，本机 3 CPU）：

| 项 | 数字 |
|---|---|
| 1000 张输入 | 保留 **983**，自动丢弃 **17** |
| 建库耗时 | **23.4s** |
| `.imgdb` 体积 | **5.11MB**（每目标 5453 B） |

三点值得记下来：

1. **23.4s 落在设计假设里**，后台线程 + 503 那套没白做 —— 但它也说明「入库后立刻扫」会
   连吃几次 503，提示文案必须说清「还在准备」而不是"失败"。
2. **`arcoreimg` 会自己丢目标**（17/1000，特征不足的），而且是静默丢。所以清单必须以
   **它的输出**为准回读，不能以我们的输入为准 —— 否则 manifest 里会有 17 条永远认不出来的
   照片，表现成"偶发漏检"，最难查的那一类。
3. 每目标 5.5KB → 1000 张 5MB，下发一次的量级可以忽略，不需要增量下发。

### 装库优先级与回退

手机侧 `LocalTargetDb.prepare()`（后台线程）：

```
服务端那份能装？→ 真的 deserialize 一遍试装
├─ 成功 → 用它，端上那份碰都不碰（省掉 6 秒）
└─ 失败 → 把**这个版本**记成拒绝（不删文件）→ 退回端上现建 → 留一条 notice
```

**试装排在建库之前、且在后台线程**：等到 GL 线程装库时才发现装不上，就只剩「在 GL 线程上
卡几秒建库」或「放弃离线识别」两条路。代价是成功路径多一次 10–20ms 的 deserialize。

装不上最常见的原因是服务端的 `arcoreimg` 比手机上的 ARCore 新（`loadTargetFromImgdb` 的
注释里早就记了这个坑）。已知不完美：**手机上的 ARCore 升级不会让版本号变**，那一份仍会被
跳过，出路是服务端下次入库自动换版本、或用户「全清」再同步（提示里写了）。

### 元数据两个来源怎么合并

`tryLocalHit` 要拿 `printWidthM` / `refAspect` / `mediaUrl`。预建库覆盖的照片（≤1000）
**可能比端侧缓存的多**（默认 200），所以 manifest 也成为一个来源：

1. 端侧缓存里有 → 用它（**它带本地视频路径**）
2. manifest 里有 → 用它；视频没缓存就走既有的 `VIDEO_NOT_CACHED`（那条提示的注释明确说
   这是"用户能自己解决"的情况），而**不是**当成没认出来
3. 都没有 → 落回服务端识别

### 这次修正顺带解决了什么

**XFeat 800ms 那个风险从 Android 热路径上整个消失了。** 服务端的识别管线（ORB/XFeat +
词汇树 + RANSAC）职责收缩为三件事：入库时的近重复闸门（这条不能少 —— 两张近重复都入库
会让**两张都永久漏检**）、超过 1000 张的溢出兜底、以及将来的网页版。

---

## 1. 识别特征：换成 XFeat，但 ORB 完整保留

### 选了什么

**XFeat**（Accelerated Features, CVPR 2024，verlab/accelerated_features）。代码与权重都是
**Apache-2.0**，512 个关键点，64 维 float32 描述子，配 **余弦互近邻** 匹配。

### 为什么是它

清权之后候选池比想象的小得多：

| 候选 | 权重许可 | 结论 |
|---|---|---|
| **XFeat** | Apache-2.0 | ✅ 选它 |
| ALIKED | BSD-3-Clause | 可用，但没有可靠的动态尺寸 ONNX，CPU 耗时无公开数字 |
| DISK | Apache-2.0 | ❌ CPU 上 1.2 FPS（833ms/帧），单帧就吃光预算 |
| **SuperPoint** | 🔴 **non-commercial research only** | ❌ 许可直接排除（SuperGlue 同一份许可） |
| **SiLK** | 🔴 GPL-3.0 | ❌ 传染，与本仓库不兼容 |
| DeDoDe | MIT | ❌ 107MB，太大 |

XFeat 还有一条别人没有的性质：论文实测它在 Cortex-A53（$28 的 Orange Pi Zero 3）上
480×360 能跑 **1.8 FPS**，是唯一过 1 FPS 的学习型检测器（SuperPoint 0.16、ALIKE 0.58）。
这条直接决定了「同一个模型能放到手机上跑」是否成立 —— 而那正是 §8 的前提。

### 为什么匹配器不是 LightGlue

LightGlue 更准，但 **CPU 上完全跑不动**：官方数字 0.31 pairs/sec @1200px（i7-6700K）
≈ **3.2 秒一对**，而精排要对 Top-K 个候选各算一次。LighterGlue 按作者「快约 3 倍」外推
也还在 1 秒量级。差两个数量级，不是优化能补的。

余弦互近邻在同一台机器上是 **0.46ms/对**（512 点，本机限 3 线程实测）。

### 为什么 ORB 不删

它是**已经通过出口条件的基线**：真实语料上命中 95.70%、真实误识别 0.000%、P95 67.7ms。
XFeat 在纸质翻拍上应当更强，但那是论文与间接证据支撑的**预期**，不是在这个项目的语料上
量过的结论。留着 ORB 是为了

1. 任何时候能一条配置退回已知可用的状态；
2. 让「换特征」这一步的收益与代价能被单独量出来，而不是把多个变量混在一起。

实现上是 `photoar/backend.py` 的 `Backend` 对象。**四件事必须成套换**：提特征、配对、
描述子存储布局、词表种类与阈值。所以它们是一个对象上的四个属性，不是四个独立开关。
两个后端的库文件互不兼容也不该兼容（换后端 = 全库描述子作废），因此落在不同目录：
`data/library`（orb）与 `data/library_xfeat`。

---

## 2. 模型为什么自己导出 ONNX

XFeat 上游**没有**官方 ONNX（导出 PR verlab/accelerated_features#5 至今未合并）。
社区导出版（DavideCatto/XFeat-ONNX）存在，但没用，两个理由：

1. 与官方权重是否数值等价**没人验证过**；
2. 它把关键点数导成动态维度，于是图里带 `NonZero`，输出形状只能运行时确定 ——
   ORT 没法预分配缓冲，Android 上还会在 EP 之间来回回落。

`tools/export_models.py` 从官方 Apache-2.0 权重自己导，换来**全静态输出形状**。

导出过程中踩到并解决的两件事，都记在这里免得重踩：

- **`Tensor.unfold` 不可导出**（`symbolic_opset9.unfold` 只支持静态尺寸）。用
  `F.pixel_unshuffle` 等价替换 —— 两者的输出通道下标都是 `c*ws² + dy*ws + dx`。
  这是一次**行为替换**而不是重构（排布差一点，模型不报错、只输出垃圾），所以导出脚本里
  有一步 `_assert_unfold` 真的逐元素比过才允许继续。
- **输入尺寸固定 640×640，不做动态 H/W**。TorchScript 导出会把 `x.shape[-1]` 烘成常量，
  于是「动态」是假的：换一个宽度就静默算错且不报错。固定形状是诚实的选择。

### 验证结果（实测）

```
ONNX vs PyTorch：有效点 512，同位重合 512（100.0%），描述子 max|Δ|=8.9e-07
与官方 detectAndCompute：同位重合 484/512（94.5%），描述子余弦 均值 0.9937 / 最小 0.806
```

第一行证明**导出忠实**；第二行的 5.5% 差异来自镜像补边改变了 InstanceNorm 的统计量，
是已知且可解释的。用**余弦**而不是 `max|Δ|` 当门槛，因为描述子已 L2 归一化、匹配用的就是
内积，单个分量差 0.2 完全可能对应 0.99 的余弦。

产物：`xfeat.onnx`，4.31 MB，sha256 `29a81cef…0455c652`，opset 17。

### 预处理契约（两侧必须逐条一致）

1. BGR → RGB
2. 缩到长边 640，保持长宽比
3. **BORDER_REFLECT_101 镜像补边**到 640×640，只补右侧与下方
4. HWC → NCHW，float32，值域 **0..255**（不除 255）
5. 另传 `size = [有效高, 有效宽]`，图内用它掩掉补边区的关键点

**第 3 步为什么是镜像而不是补黑**：模型第一层是 InstanceNorm，按整张画布算均值方差。
补黑会把统计量拉偏，而参考图（3:2）与相机帧（16:9）补黑的面积不同 —— 同一处纹理在两侧
会拿到不同的描述子。镜像保留原图统计特性，且镜像边界连续、不造阶跃边。

**第 4 步不除 255**：InstanceNorm 逐样本归一化会抹掉全局尺度，两者等价；取 0..255 是因为
客户端拿到的原始像素就是这个范围，少一次约定少一处错。

这条契约有两份实现（Python 的 `xfeat.prepare()` 与 Kotlin 的 `XFeatPreprocess`），用一份
**跨语言 golden**（合成图的张量总和与 7 个定点值，在两侧测试里逐字重复）钉住。改一边不改
另一边，两边各红一条。

---

## 3. 判定阈值：实测数字与它们的边界

ORB 的 `MIN_INLIERS = 40` 换成 XFeat 后**作废**：关键点 300→512，描述子 32 字节二值→64 维
浮点，配对 Hamming crossCheck→余弦互近邻（还先过一道 0.82 的余弦闸门）。真阳性的内点数
系统性更高，搬过来等于放松判定，而且不会报错。

`bench/xfeat_inlier_dist.py`，Oxford5k 抽 250 张 × 6 个扰动查询 = 1500 次，每次与 25 张
别人的图对比取最强者（与当年定 40 时同一份语料、同一个口径）：

| 后端 | 真阳性 p1 | 真阳性 p5 | 误识别 p95 | 误识别 p99 | 误识别最大 |
|---|---|---|---|---|---|
| ORB (300 点) | **9** | 53 | 8 | 11 | 213 |
| XFeat (512 点) | **71** | 97 | 13 | 66 | 163 |

**取 `XFEAT_MIN_INLIERS = 60`**，两条理由都是直接读出来的：

1. **真阳性 p1 = 71 > 60** —— 门槛卡在 60，真阳性损失不到 1%。对比 ORB 的 40：它的 p1 只有
   9、p5 是 53，也就是 40 会吃掉 1%~5% 的真阳性。**XFeat 在漏检这一侧反而更安全，这是换
   特征最实在的收益。**
2. **误识别 p95 = 13**，远在 60 之下。

⚠️ p99=66 与最大值 163 超过 60，但这是**语料属性不是缺陷**：Oxford5k 里有大量「同一建筑的
不同照片」，几何上本来就对得上。当年定 40 时遇到的是同一件事。

⚠️ **这仍是待复核的值**：只量了精排那一步（不含粗排），1500 次样本而当年 ORB 那轮是
29740 次。`XFEAT_DEDUP_MIN_INLIERS = 38` 更弱 —— 它是**按 ORB 那一对的比例推的**
（25/40 × 60 = 37.5），**没有独立量过**。保守方向是往低调。

---

## 4. 放弃了 DINOv2 全局检索（这是一次自我纠正）

原计划用 **DINOv2 ViT-S/14** 出 384 维全局向量做粗排，理由很有吸引力：Apache-2.0、权重有
官方非 HF 直链（`dl.fbaipublicfiles.com`，实测可达）、1 万张暴力余弦检索只要 0.17ms、
库只占 15MB，**完全不需要引入 faiss/hnswlib**。

**放弃的理由是这个场景的查询长什么样**：相机帧里是「一张打印照片 + 一大片桌面/墙面/人手」，
照片可能只占画面三成。整帧的全局向量会被背景主导，同一张照片在不同背景下拿到的向量彼此
不像 —— 检索直接退化。

局部描述子 + 倒排索引对杂乱天然鲁棒（背景的词只是投票噪声），而且这条路在本项目这个场景
里**已经实测过**。换掉特征提取器是有依据的改进，换掉整个检索结构不是。

附带收益：手机端只需要 2.56MB 的 XFeat，不必再塞一个 84MB 的 ViT —— §8 因此可行得多。

---

## 5. 粗排：XFeat 描述子上的浮点词汇树，以及「空词表」为什么合法

`photoar/floatvocab.py`：球面 k-means（余弦分配、簇内求和再归一化），分支 16 深度 4 ——
直接沿用二进制版扇扫出的结论「要提召回先提分支数，别提深度」，**没有在 XFeat 描述子上重扫**
（这是已声明的移植假设）。`words_of` 是**向量化**的（按节点分组批量算内积），不像二进制版
那样逐描述子 Python 循环 —— 那个循环是「重建索引很慢」的主要来源。

### 空词表（`NullVocab`）

全新部署时库是空的、没有词表，而服务端原先**强制要求** `vocab_path` 存在，于是根本起不来。
这与「一键部署」直接冲突。

解法不是给 idf 加平滑（那会改变已实测过的排序语义），而是利用一条既有性质：全零词表下每个
词的 `df == n_docs`，于是 `idf == 0`，每篇文档的 tf-idf 范数为 0，`unretrievable_docs()` 会把
**全部**文档列为「检索不到」，而这些文档会被无条件并进候选集 —— **行为是全量扫描：结果正确，
代价是 O(库大小)**。加上「库不超过 Top-K 时本来就跳过粗排」这条既有分支，空词表在小库上与
有词表**完全等价**。

⚠️ 这条推理里有一个洞，是验证时才发现的：`recog.top_k < n_docs <= 20` 时两个兜底分支都不
成立，候选集为空 → **每次识别必然未命中，而日志全是正常的 200**。已修（无条件计算
`extra_slots`，并把 `unretrievable_docs()` 换成 numpy 标记，因为它现在在入库热路径上）。

建词表：`photoar-server build-vocab` 或 `POST /v1/admin/rebuild-vocab`。实测把识别延迟从
124ms 降到 64ms（45 张库，ORB）。

---

## 6. 用户与权限：为什么这样设计

### 登录

- **访客只输名字**。不在册的名字**拒绝**而不是自动建号 —— 自动建号等于任何人都能进。
- **管理员必须带口令**，且**不带口令必须失败**（不能因为「访客只要名字」而让管理员也能只输名字）。
- 口令用 stdlib `hashlib.scrypt`，`n=2**14, r=8, p=1` → **16 MiB/次**。OWASP 建议的 `2**17`
  是 128 MiB/次，而 HTTP 是每请求一线程：8 人同时登录就 1 GiB，在 3GB 的 NAS 上转码进程会被
  OOM killer 挑走。`maxmem` 显式写出来，否则会落到 OpenSSL 的 32 MiB 上限、在真的有人登录时
  才抛异常。
- session token 只在库里存 **sha256**（明文泄库等于永久登录）。访客 30 天、管理员 12 小时。

### 名字的唯一性

存**两列**：`name` 原样（显示用）、`name_key` 存 `NFKC → 压缩空白 → casefold` 并建唯一索引。
不用表达式唯一索引，因为 SQLite 的 `lower()` 只处理 ASCII，且**没有任何办法**表达「内部连续
空白压成一个」—— 于是 SQLite 认为唯一的两行，登录查找会认为是同一个人。那是个只在非 ASCII
名字上暴露的静默 bug。

### 凭证怎么走

- App：`Authorization: Bearer <token>`
- 网页管理台：**HttpOnly cookie**。理由是 `<img>` / `<video>` 标签**没法带请求头**，缩略图和
  视频只能靠 cookie。token 一个字节都不进 `localStorage` —— 那里的 token 任何 XSS 都能整条
  读走，HttpOnly cookie 读不到。
- `Secure` 属性**默认关**：部署形态同时有局域网 http 直连与 Cloudflare https，写死 Secure 会
  让局域网登录一刷新就掉线。做成开关。
- 旧的 `PHOTOAR_TOKEN` 保留为**机器对机器**的运维凭证（批量入库脚本要用），身份是 admin。
  空 token **不是万能钥匙**：三处 legacy 分支都写成 `if self._legacy_token and …`，空串时整条
  分支禁用。这一条有一个走完整 HTTP 栈的测试钉住（放行判断分散在三处，只测中间一层不够）。

### 识别的 ACL 顺序（这条最容易写错）

**先按全局口径判定，再检查命中的那张是否对该用户授权**；没授权返回
`matched:false, reason:"forbidden"`（HTTP 仍 200，因为「没认出来」在这个 API 里是正常状态）。

**不能**改成「先把候选集过滤成该用户可见的再判定」：被过滤掉的照片就不再参与 ratio 检验，
而那道检验正是把真实误识别压到 0 的原因之一 —— 过滤候选会让误识别率上升，**且只对权限受限
的用户上升**，极难发现。

### 其它

- `/v1/asset/<id>/stream` 必须**反查 asset 属于哪张 photo** 再判授权，否则拿到 asset id 就能
  绕过整套授权。
- `/v1/fs/*` 与 `/v1/upload` 只给 admin：`fs/list` 能列 NAS 白名单目录，给访客等于开放文件浏览器。
- 管理员**不能**降级、停用或删除自己（UI 与后端双拦）。否则一次误操作就把管理入口锁死，只能进
  容器改库。
- 删用户会**级联**删掉他的授权，不可撤销。

---

## 7. 一键部署与配置分界线

### 分界线

| 在哪 | 放什么 | 为什么 |
|---|---|---|
| `docker-compose.yml` + `.env` | 数据目录、镜像、模型目录与下载源、白名单根目录、端口、绑定、引导管理员、识别后端、编码器、资源上限 | 改它需要重新决定进程启动时做过的事 |
| 网页管理台（`app_config` 表） | 阈值、Top-K、质量闸门、去重闸门、贴合模式、会话时长 | 运行时可改 |

`ServerConfig.from_env()` 让 `config.json` **完全可选** —— 一键部署的前提。`.env` 里**只有
`PHOTOAR_ROOTS` 必须看一眼**。

**热配置不是假开关**：数据层交付时曾经标注 `needs_restart=False` 但「改了不生效」（识别路径
还在直接读模块常量）。已接上 —— `_recognize` 走 `decide_with(min_inliers=配置值)`，并有一条
**双向**测试（改到实测内点数之上必须变 `matched:false / reason:"weak"`，调回去必须重新命中）
证明它真的走了配置值而不是「恰好失败了」。

### 换后端不能静默降级

XFeat 模型缺失时**不让服务起不来**，回退 ORB，但 `/v1/ping` 会报
`{"backend":"orb","backendDegraded":true}`。静默跑成另一个后端会让人以为换了特征却毫无变化。

### 照片 = 触发条件 + 画布

数据模型本来就没有「照片必须是视频某一帧」的耦合。本轮补的是 `photo.fit_mode`：
`fill`（居中裁切填满，默认）/ `fit`（完整放入留边）。视频内容与照片无关时，裁切会切掉视频
内容 —— 所以这个选择必须交给用户。

---

## 8. 端上提特征：现在是兜底，不是主路径

### 动机（实测）

服务端在 3 CPU / 3GiB 下的识别延迟：

| 后端 | 词表 | p50 | p95 |
|---|---|---|---|
| ORB | 训好 | **64ms** | 66ms |
| ORB | 空（全量扫描） | 124ms | 129ms |
| XFeat | 训好 | **800ms** | 1101ms |

拆开看（3 CPU）：

| | ORB | XFeat |
|---|---|---|
| 提特征 | 6ms / 300 点 | 26–30ms / 512 点 |
| 单候选配对 + RANSAC | 2.6ms | ~24ms |
| 20 候选合计 | ~58ms | **~800ms** |

**XFeat 的成本在配对而不是推理。** 把提特征挪到手机上只省掉 30ms —— 这就是为什么第一
阶段那次搬迁不够，见 §0.1：正确的做法是让**识别整件事**都在端上（ARCore 预建目标库），
服务端退出热路径。

这条 `/v1/recognize/features` 保留下来，因为它仍然有用：超过 1000 张的溢出兜底、
ARCore 装不上预建库时的降级、以及将来的网页版。

### 实现

`POST /v1/recognize/features`，请求体是 base64 的 float32 关键点与描述子。响应形状与
`/v1/recognize` **完全一致**（客户端解析共用），走同一套阈值、同一套 ACL、同一份历史记录。

描述子校验选了**拒绝**而不是重新归一化：ONNX 图最后一步就是 `F.normalize`，float32 经
base64 往返是精确的，真实偏差在 1e-7。范数不对说明客户端**没在跑这份图**（dtype / 字节序 /
步长解错了），而归一化一个解错的缓冲区只会得到「看起来合法、内容是垃圾」的单位向量 ——
它会正常参与匹配、正常通过或不通过那道闸门，最后表现为**识别率莫名偏低**。

另外加了一道服务端唯一能抓到「客户端预处理写歪了」的检查：关键点必须落在有效区内
（±1 像素余量）。它能抓四边都补边、忘了缩到 640、xy 反了这三类。

### 为什么默认关

端上推理在这里**无法真机验证**：ORT 能不能加载这份图、`floatBuffer` 的形状顺序、推理耗时、
内存占用，全未验证。所以默认走传 JPEG 那条路；模型下不来 / ONNX 加载失败 / 推理抛异常，
都静默回退并留一条 notice，不让扫描坏掉。

模型不打进 APK（4.31MB 且要能随服务端换），从 `GET /v1/model/xfeat` 下载并缓存。
模型准备放在**独立线程**上异步做 —— 排在网络线程上会让前几十帧全部超时
（模型 4.31MB、超时 2 分钟，而识别看门狗只有 4 秒），状态机会连报「网络不稳」并反复重探活。

---

## 9. 3D 位姿：架构是对的，但「一上来就是对的大小」这句话是错的

Android 侧走 **ARCore Augmented Image + 世界追踪（SLAM）**：位姿是真 6DoF，照片被手指
挡住一角、糊一帧、移出画面边缘，锚点还在。这部分没动过。

> ⚠️ 这一节原来还有一句：「`.imgdb` 里烘好了物理宽度，`deserialize` 出来的库自带正确
> 尺寸，视频四边形一上来就是对的大小」。**那句话被真机推翻了**，前半句成立（宽度确实
> 烘在库里）而后半句不成立 —— 它默认了烘进去的那个数是真的。它错在哪、以及尺度应该从
> 哪里取，见 [9.3](#93-贴合三个真实原因以及一次被实测推翻的论证) 和
> [13](#13-照片实际尺寸不知道printwidthmm-从必填改成可选)。

刻意**没有**实现「自建锚点」（用 solvePnP + `session.createAnchor` 绕开 ARCore 增强图像的
质量门槛，从而让被质量分拒掉的照片也能用）。它是一项有价值的增强，但**无法在这里验证**，
而现有路径功能上是对的。留给真机阶段。

（后续：世界锚点这条路又以另一个理由回到了台面上 —— 它是「静止不抖 + 运动不拖」唯一
的出路，见 9.3 末尾。）

顺手修掉一个现存 bug（`ArRenderer.kt`）：追踪变化检测只比布尔、不比目标名。扫描阶段
session 里装的是多图库，`trackedImage` 返回任意一张被跟踪的图 —— 先看到照片 A（上报一次）
而 A 不在缓存索引里或刚被拉黑，镜头转向 B 时布尔仍是 `true`，**B 的离线命中永远不触发**。
用户看到「这张扫不出来」，日志里一切正常。

---

## 9.1 ARCore 运行时打进我们自己的包（本轮新增）

### 为什么必须这么做

ARCore 的位姿计算不在 `com.google.ar:core` 那个客户端库里，它在一个**独立的 APK**
（`com.google.ar.core`，Google Play Services for AR）。原本 `ArCoreApk.requestInstall()`
会 deep-link 到 `market://details?id=com.google.ar.core` —— 而这个 App 的使用场景是婚礼现场
的宾客手机，**大陆机型大多没有 Play 商店**，那一步只会静默失败。

国内应用商店确实上架了这个组件，但**中国区机型白名单冻结在 2020 年前后**（小米 Mix 2S /
Mix 3 / 8 SE / 9 / 10 Ultra 之类），K40、Mi 11 都不在里面。所以「让宾客自己去装」这条路
在现场是不成立的。

结论：把那份运行时 APK 打进我们的包里，第一次用的时候本地装上。**代价是包体积**
（+72MB，实测 debug 包 151MB）—— 这是明确接受的取舍。

### 版本锁死是一条不变量，不是洁癖

客户端库和内置运行时**必须同版本**。脱钩会造出一个装不完的循环：
`SUPPORTED_APK_TOO_OLD` → 装我们那份更旧的 → 还是 `TOO_OLD` → 又装。

所以 `arview/build.gradle.kts` 里只有**一个**版本号，同时喂给 `com.google.ar:core:$v` 和
下载 URL；sha256 按版本登记在同一张表里，换版本忘了补哈希会**直接构建失败**并打印补登记
指引 —— 而不是静默下载一份没校验过的二进制。这样那个循环在源码层面就构造不出来。

### 构建期怎么把它弄进去

```
GitHub Releases（tag 是裸的 1.54.0，不是 v1.54.0）
  → ArcoreDownloadTask   @OutputFile → android/.arcore/（gitignored，clean 不掉）
  → ArcoreAssetTask      @InputFile  → AGP 指定的 generated assets 目录
  → variant.sources.assets.addGeneratedSourceDirectory(...)
```

三个坑，都踩过：

1. **不能用 `sourceSets["main"].assets.srcDir(taskProvider)`。** `AndroidSourceDirectorySet.srcDir(Object)`
   走 `Project.file()` 语义，**不保证**连上任务依赖。有保证的 API 只有
   `androidComponents { onVariants { ... addGeneratedSourceDirectory(...) } }`，而且输出目录由
   AGP 自己给，任务里**不能**手动 set。
2. **仓库的 `.gitignore` 本来就忽略 `*.apk`。** 如果把 asset 直接放 `src/main/assets/`，
   干净克隆里它会**悄悄消失、而构建照样成功** —— 出包时才发现 AR 全程走兜底。所以走
   「缓存 + 校验后拷进生成目录」，缓存目录另外显式写进 `.gitignore` 表明意图。
3. **`noCompress` 只有 app 级别有效。** asset 住在 `:arview` 里，但每个消费它的 app 模块都得
   自己写 `androidResources { noCompress.add("apk") }`。Phase 3 的 Flutter 外壳会再踩一次，
   已在两处注释里点名。压缩了的后果不是崩，是 `openFd()` 拿不到真实长度 ——
   `assetLength()` 返回 -1 时会打一条指名 `noCompress` 的日志，让这个失败自己说出原因。
4. **绝不改、绝不重签那份 APK。** 改了 Google 的签名就断了它后续被 Play 更新的路。
   实测抽包校验：sha256 与下载件逐字节一致，`apksigner` 打印 `O=Google Inc.` + source stamp。

### 运行期：装的决策是纯函数

`ArInstallPolicy.decide()` 不碰 `Context`，全部输入都在一个 data class 里，所以能在 JVM 单测
里穷举整个输入空间（20 个用例，其中一个跑完 `6 × 2⁵ × 4 = 768` 种组合）。这不是为了覆盖率：
这个界面上已经出现过两次卡死，两次都是「某个状态组合没人管」。

```
INSTALLED           → 开 AR
DEVICE_NOT_CAPABLE  → 兜底（唯一「装了也没用」的状态）
CHECKING            → 复查，上限 8 × 800ms ≈ 6.4s，超了就转去装
NOT_INSTALLED / TOO_OLD / UNKNOWN → 装内置那份，两条路依次试：
                      会话安装（不落盘、有回执）
                      → 被 ROM 拦掉就降级到老式 Intent（见下节，MIUI 上必然走到）
                      → 老式也不成才兜底
```

**`UNKNOWN`（含 `UNKNOWN_ERROR` / `UNKNOWN_TIMED_OUT` / 直接抛异常）选择「装」而不是
「兜底」** —— 这是本节最反直觉、也最重要的一条。查不出来的最常见原因就是连不上 Google 的
机型档案服务，**恰好就是没有 Google 框架的大陆手机**。装上本地运行时之后，这个查询能在
本机得到答案。把 `UNKNOWN` 当成「不支持」，就等于把我们最主要的目标用户全判死。

三个「只做一次」的闸门，各挡住一个转不出来的圈：

| 闸门 | 少了它会怎样 |
|---|---|
| `MAX_CHECKS = 8` | `CHECKING` 时只显示一句「正在准备」就 return，没人安排复查 —— **永久停在那句上**（这是本轮修掉的既有缺陷） |
| `sessionAttempted` / `legacyAttempted` | 装完仍不 READY → 又装 → 又不 READY。两个分开记，因为两条安装路要按顺序各走一次；判「该不该兜底」时**只看 `legacyAttempted`** —— 老式是最后一条路，走过它就没有下一条了 |
| `permissionAsked` | 送去设置页 → 用户按返回 → `onResume` → 又送去设置页，**一个退不出来的界面**，比没有 AR 糟得多 |

外加一个 60s 安装看门狗，只在 resumed 时上弦 —— 用户待在系统安装框里的时间不该算进超时，
`onResume` 里也会把 `arChecks` 归零，同理。

### 安装本身

`PackageInstaller` 会话：`openWrite()` 给的是 `OutputStream`，所以 72MB 直接从
`AssetManager` 流过去 —— 不落临时文件、不需要 FileProvider。几个必须写对的地方：

- `PendingIntent` 在 API 31+ 必须带 **`FLAG_MUTABLE`**，否则系统塞不进 `EXTRA_STATUS`，
  回执永远不来（然后就靠看门狗兜，但用户白等 60 秒）
- `STATUS_PENDING_USER_ACTION` 要取 `EXTRA_INTENT` + `FLAG_ACTIVITY_NEW_TASK` 再
  `startActivity`；取不到就当失败上报，**不能静默等**
- 清单里的 `<queries><package name="com.google.ar.core" /></queries>`：API 30 起不写这条，
  `checkAvailability()` 永远给不出 `SUPPORTED_INSTALLED`
- 授权用 `ACTION_MANAGE_UNKNOWN_APP_SOURCES` 带 `package:` URI（不带的话有些 ROM 只打开
  一个全局列表，用户得自己找我们）

### 兜底文案必须区分原因

`notice(action, state)` 收两个参数，就为了这一件事：只有硬件真不支持才能说
「这台设备不支持 AR」。用户拒了安装也说这句**就是在撒谎**，而且堵死了他重试的念头 ——
那种情况说「没装上 AR 组件，识别后将全屏播放」。两句都必须告诉他**还能看**：兜底不是报错。

### 构建期实测

```
:arview:generateDebugArcoreRuntimeAsset  BUILD SUCCESSFUL（sha256 校验通过）
:app:assembleDebug                       BUILD SUCCESSFUL
app-debug.apk                            159,167,463 B（151.8 MiB）
  assets/arcore.apk   75,341,088 B  Stored（未压缩 ✔，release 包同样是 Stored）
  抽出后 sha256 == 下载件，apksigner: O=Google Inc.
  内置版本 1.54.260890493 == com.google.ar:core:1.54.0 ✔
Kotlin 测试 623 passed（arview 580 + app 43），0 失败
```

### 包体积：模拟器那两套 ABI 只在 CI 里砍

每个 ABI 的原生库实测（未压缩）：`libonnxruntime.so` **19.4 MiB**（arm64 17.0 / v7a 11.9），
其余四个（`libarcore_sdk_c` / `libarcore_sdk_jni` / `libonnxruntime4j_jni` /
`libandroidx.graphics.path`）合计约 0.3 MiB。x86 + x86_64 加起来 **39.3 MiB 白搭**。

砍法是 `app/build.gradle.kts` 里的 `-Pphotoar.deviceAbiOnly`，**默认不开**：

| 命令 | 包里的 ABI | release 包大小 |
|---|---|---|
| `./gradlew :app:assembleRelease` | 四个 | 155,713,877 B |
| `… -Pphotoar.deviceAbiOnly=true` | arm64-v8a + armeabi-v7a | **114,533,262 B**（−39.3 MiB） |
| `… -Pphotoar.deviceAbiOnly=false` | 四个（显式关掉） | 155,713,877 B |

**默认全带是刻意的**：本机跑 emulator 验兜底路径是日常调试手段，为了发版把它废掉不值得。
判值而不是判「参数存在」，否则 `=false` 反而会触发过滤。`.github/workflows/android.yml`
只在打 tag / 手动触发时出包并传这个开关，出包后有一步硬校验守住两个「构建成功但包是坏的」
失败模式：`assets/arcore.apk` 必须在、`lib/x86*` 必须不在。

⚠️ CI 出的包和本地包**签名不同**（release 沿用 debug key，而 runner 上的 `debug.keystore`
每次现生成）。真机上两边互换必须先卸载。

### 真机实测：会话安装在 MIUI 上是死路，老式 Intent 是唯一退路

设备：Redmi K40 Pro（`haydn`，Android 12 / SDK 31，MIUI V13.0.13.0.SKKCNXM）。

先把「装不上」的原因钉死。MIUI 的安装器 `com.miui.packageInstaller.InstallStart.onCreate`
第一件事就是：**只要 `sessionId != -1` 且 `SDK_INT <= 34`，一律拒绝**（反编译确认，日志
`MIUIPI_InstallStart: blocked session install because sdk version too low`）。这跟我们的
targetSdk、未知来源授权、用户点不点**全都无关** —— `PackageInstaller` 会话这条路在这台机器上
无论怎么写都不可能成功。老式 `ACTION_VIEW` 安装的 `sessionId` 是 -1，正好跳过那段判断，
所以退路走 FileProvider + `ACTION_VIEW`（清单里那个 `${applicationId}.arcore.fileprovider`
就是为它存在的）。

`pm uninstall com.google.ar.core` 之后完整跑一遍，日志时间轴：

```
14:43:48.834  AR 决策：RECHECK               ← checkAvailability 第一次总是 CHECKING
14:43:49.638  AR 决策：INSTALL_BUNDLED       ← 会话安装，被 MIUI 当场拒
14:43:50.337  AR 决策：RECHECK（sessionAttempted）
14:43:50.348  AR 决策：INSTALL_BUNDLED_LEGACY ← 降级到老式 Intent
14:44:53.240  AR 决策：START_AR              ← 63s 后由复查定时器送达
              APK version code: 260890493 from package com.google.ar.core
              SDK 1.54.260760000 / Dynamite load ok / 317 symbols
```

装上的正是我们内置那份（`260890493` 与构建期抽包核对的版本号一致）。两个观察值得记下来：

- **那 63 秒不在 10s 承诺里**，是 MIUI 装完包之后自己的包扫描（`installProcess=[install_finish]`
  之后系统才让 `checkAvailability()` 看见新包）。老式安装没有回执，正是它逼出了
  `legacyAttempted && checks < MAX_CHECKS → RECHECK` 那条宽限期 —— 少了它会把一次**成功的**
  安装判成失败。
- 第二次装同一个包时 MIUI **没有再弹确认框**（`MIUIPI_installer: onInstallFinished success :0`），
  走了它自己的快速路径。别把「不弹框」当成常态来设计。

顺带一个坑：MIUI 安装器界面上那个显眼的「安装」按钮（`installBtn`）属于页面里塞的广告
（实测是一个 125.2MB 的短剧 App），我们的包对应的是 `start_button`。用 `uiautomator dump`
按 resource-id 找，别按坐标或文字找。

### 真机实测：会话建好了没人 resume（本轮修掉的第二个缺陷）

第一次跑通安装之后，界面在前台、屏幕是亮的，画面却是死的，日志里 **`AR_ERROR_SESSION_PAUSED`
每秒 121 次**，持续不停。

成因是一条只在**异步路径**上才成立的接线错误：`ScanRuntime.attachAr()` 只 `create()` 会话，
`resume()` 是 `onResume()` 里那句 `wantScanning = true` 带出来的。而 AR 的接入有三条路 ——
`onResume` 里同步决策出来的、复查定时器落地的、安装回调落地的 —— **后两条都发生在
`Activity.onResume` 跑完之后**，那时候没人再来 resume。于是 `wantScanning` 一直是 false，
GL 线程照样每帧 `update()` 一个 paused 的会话。而这两条恰恰是**常见**路径：
`checkAvailability()` 第一次几乎总是 `CHECKING`。

修法是 Activity 上一个 `resumed` 标志（`onResume` 置位、`onPause` 清零），`setup()` 接完线
末尾 `if (resumed) rt.onResume()`；`onResume()` 里改成只在「运行时早就存在」的分支才
resume，避免同步路径上 resume 两次。判 `resumed` 而不是无条件调，是因为 `onCreate` 里探相机
权限那条路会在 `onResume` 之前就走到 `setup()`。

在**原来会炸的那条**路径上复验（就是上面那份 63s 后才 `START_AR` 的时间轴）：

```
SESSION_PAUSED 次数: 0                      ← 修之前是 121 次/秒
CameraExtImplXiaoMi: initCameraDevice: 0    ← 相机真开了
vio_estimator.cc:1401 [VioEstimator] [PauseResume] HandleInitializationStage
  with feature tracks（约 10Hz 持续）        ← VIO 在跟踪
```

剩下 19 条 `native E` 与我们无关，确认是良性的：`SENSOR_TYPE_ACCELEROMETER` /
`GYROSCOPE_UNCALIBRATED` 的 `Callback list ... not found`（每 5s 一条），一条
`Initializer's SSBA failed to produce a valid output`（手机平放在桌上、没有视差），
以及 `NoSuchFieldException: No field requiredDisplayCategory`（ARCore 在 API 31 上反射
API 34 的字段）。

**这台机器验不到的那一半**：它装着 `com.android.vending` / `com.google.android.gms` /
`com.google.android.gsf`。也就是说「没有 Google 框架 → `checkAvailability()` 返回 `UNKNOWN`
→ 仍然选择装」这条**最主要的目标场景**，仍然只有单测覆盖，没有真机证据。要验它得找一台真
没刷 GMS 的机器，或者把 GMS 停用后重跑上面那条时间轴。另外 `haydn` 本身是 ARCore 支持机型
（`DEVICE_NOT_CAPABLE` 的分支同样没在真机上出现过）。

---

## 9.2 从识别到播放的时间预算：10 秒承诺怎么兑现

对用户的承诺是「对着照片举起手机 → 视频开始播，10 秒内」。这一节把那 10 秒拆开，并说明
每个超时值为什么是这个数。原来的值是按「宁可等也别放弃」定的，本轮**全部收紧**，因为在
现场「等 12 秒然后成功」的体验比「等 6 秒然后全屏播放」差得多 —— 宾客早就把手机放下了。

| 常量 | 值 | 管什么 |
|---|---|---|
| `CAPTURE_WATCHDOG_MS` | 1.5s | 一帧抓不下来就重来，不能挂死在相机上 |
| `RECOGNIZE_WATCHDOG_MS` | 2.5s | 服务端识别整个往返（socket 层 `RECOGNIZE_TIMEOUT_MS` 2.0s） |
| `HIT_TO_PLAY_BUDGET_MS` | **6.0s** | 命中之后到第一帧画面的**总**预算，下面几项都装在它里面 |
| `TARGET_LOAD_TIMEOUT_MS` | 4.0s | 装目标库（预建那份 deserialize 10–20ms，端上现建才吃满） |
| ~~`TARGET_FIND_TIMEOUT_MS`~~ | **已删** | ARCore 在画面里认出这张图 —— 现在没有上限，见 §33 |
| `MEDIA_TIMEOUT_MS` | 2.5s | 取媒体元信息，**与装库并行**，不串在预算上 |
| `DOWNLOAD_TIMEOUT_MS` | 3.0s | 单次分片下载；视频整体走 `CACHE_VIDEO_TIMEOUT_MS` 60s，不在热路径 |

**最坏路径 = 2.5s（识别）+ 6.0s（命中后总预算）≈ 8.5s**，留 1.5s 给帧捕获和调度抖动。
所以 10 秒是**兜得住的**，而且兜不住的时候有下文：任一段超时都会落到「识别后全屏播放」，
不会停在一个转圈的界面上。

> ⚠️ **§33 起这段承诺在「贴不上」时不再成立。** 「ARCore 找到图」那一档已经没有上限（既不
> 回扫描也不退全屏，一直等），出口是用户按「退出」。理由见 §33.1 —— 一句话版：那两种出口
> 都把贴合失败的原因盖住了，而那正是现在要查的东西。其余各段的账不变。

AR 组件的可用性检查（§9.1 的 `MAX_CHECKS × POLL_MS ≈ 6.4s`）**发生在这 10 秒之前**，
不占这个预算；单测里有一条断言把它钉在 7s 以内，防止后人慢慢加上去。

---

## 9.3 贴合：三个真实原因，以及一次被实测推翻的论证

真机上「不那么贴合」这一个现象底下有三个互不相干的原因。它们在画面上长得几乎一样，
这是最难的部分 —— 每次只能靠改一个变量再看一次来分开。

### 原因一：四边形的大小和位置来自两个不同的尺度（最主要的一个）

`ArRenderer` 画视频用的是 `projection · view · model`。`model` 来自 ARCore 的
`centerPose`，量纲由 SLAM 决定、是真的；而四边形的**大小**取的是入库申报的
`print_width_m`。这是两个来源。申报宽度偏大 N%，视频就在屏幕上比照片大 N%、边缘对不齐。

修法（`Geometry.quadSize`）：**尺度取 ARCore 自己量的 `extentX`，形状取参考图的
`refAspect`**。前者与 `centerPose` 天然自洽（同一次估计），后者精确且不受估算收敛影响。

拆开取值这一步用上了一个前提 —— **照片一定是矩形**。矩形 + 已知宽高比 = 形状完全确定，
所以收敛期最坏情况只是视频略大略小，**永远不会变形**。这很重要：人眼对人脸比例极其敏感，
形变比大小不对难看得多。

`extentX` 的可信区间比人填的宽（1cm–5m vs 2cm–2m）。两个方向的理由不同：上限放宽是因为
婚礼现场挂两米以上的大幅喷绘很正常，而这是**测量值**、没有打字错误这种失效模式；下限放宽
是因为 ARCore 估算尺寸的收敛期数值会偏小，卡太严会让开头几帧被丢掉、视频闪一下才出来。
落在区间外时**弃用而不是夹取** —— 夹取会破坏 `extentX` 与 `centerPose` 的自洽，那正是这套
设计要避免的事。

### 原因二：斜视被当成了丢失目标

`ArSessionHolder.trackedImage` 原来只认 `FULL_TRACKING`，理由是「PAUSED 时 ARCore 仍会用
上次的位姿继续报，拿它贴视频会贴在空气上」。那个担心对 PAUSED 是对的，但对
`LAST_KNOWN_POSE` 是错的，而这两件事被混成了一条判断。

`LAST_KNOWN_POSE` 的语义是「这一帧图案匹配不上，但我用 SLAM 知道它在哪」。照片钉在墙上，
所以只要相机的世界跟踪还正常，这个位姿就是**对的** —— 它本来就是 ARCore 为这种情况设计的
输出。而「图案匹配不上」在斜视时几乎必然发生（透视压缩 + 高光），于是原来那条判断把
**大角度**一律当丢失：暂停视频、弹一次 TRACKING_LOST。用户看到的就是「角度大一点就丢」。

现在两种 method 都接受，用 `Tracked.full` 区分，剩下的风险由渲染层两道闸挡：滑行窗口
（`ArRenderer.COAST_MS`，2 秒）有时限，且只在 `frame.camera.trackingState == TRACKING`
时滑行。世界跟踪自己丢了就不滑 —— 那才是原注释担心的情况。

`getUpdatedTrackables` 不保证每帧都带上同一张图，这个滑行窗口顺带盖住了那种空档。

### 原因三：我自己加的低通滤波拖了后腿（一次错误论证）

为压掉 ARCore 逐帧重估的毫米级抖动，加了 `PoseFilter`（1€ 自适应低通 + 四元数 slerp +
异常帧拒收）。第一版把时间常数设成 **0.32 秒**，依据是这样一段推理：

> 被滤的量是照片在**世界坐标系**里的位姿，照片钉在墙上不动，真值是常量，所以重低通几乎
> 不付延迟。

**这个论证是错的**，实机表现就是「贴合有延迟」。错在哪：

`view`（相机位姿）不滤，`model`（照片位姿）滤，而这两个量出自 ARCore 的**同一次优化**，
误差是**相关**的 —— ARCore 保证的正是两者互相自洽，这才是 AR 内容在世界坐标本身缓慢漂移
时依然钉得很稳的原因。只滤其中一个就把这个相关性拆了：手机一动，ARCore 立刻更新世界估计，
`view` 立刻跟上，`model` 却要等一个时间常数。拖的量正比于 `时间常数 × 运动速度`。

「真值是常量」这句话本身没错，错在它推不出「滤了不付代价」。

修正依据一个观察：**延迟只在动的时候看得见，抖动只在静止的时候看得见**。所以时间常数压到
约一帧（`FC_MIN_T` 0.5 → 5 Hz，τ 320ms → 32ms），速度增益调高（`BETA_T` 3 → 20）让真实
运动几乎不滤。

代价必须写明：抖动抑制从 4 倍多掉到 **1.7 倍（30fps）/ 2.2 倍（60fps）**。

**如果静止时抖动重新变明显，不要把时间常数调回去** —— 那只会把延迟换回来。因果滤波器在
这个信噪结构下拿不到两全：抖动伪造出的速度约 0.06 m/s，而人看照片时手部移动就在
0.05–0.3 m/s，两个区间**重叠**，速度这一个量分不开它们。（第一版 β 从 40 压到 3 正是因为
这个重叠 —— 抖动把滤波器自己撑开，压制比从 4 倍掉到 1.5 倍，是实测出来的。）

要两全得**换机制**：用 `session.createAnchor(centerPose)` 建世界锚点，让稳定性来自 ARCore
自己对相机位姿与锚点的联合优化。那条路不需要用延迟去换 —— 这也是 9 节末尾那个「留给真机
阶段」的增强重新变得有价值的原因。**没做。**

为防止有人（包括我自己）再把参数调回去，`PoseFilterTest` 里加了两条：阶跃输入 100ms 内
必须收敛到 90%（旧参数只走到约 27%），以及时间常数不得超过两帧。

### 帧率：一行把跟踪、渲染、离线识别一起锁在 30

`applyCameraConfig` 查询相机档位时写的是
`setTargetFps(EnumSet.of(TARGET_FPS_30))` —— 所有 60fps 档位在**查询阶段**就被过滤掉了。
而 `baseConfig` 里 `updateMode = BLOCKING`（渲染卡到有新相机帧才走），所以这一个数同时
决定了位姿更新率、渲染帧率、以及离线识别率（ARCore 每个相机帧做一次图案匹配）。

改成同时问 30 和 60。但**顺序不能反**：某些机型只在 640×480 上提供 60fps，而处理长边 640
实测「一档都不全过」—— 挑它换来的是一个跟得很稳但**永远认不出照片**的 AR。所以
`Frames.pickCameraOption` 先按尺寸（硬约束）定，再在**同尺寸内**取最高帧率。

提帧率暴露了一个连带的正确性问题：`PoseFilter` 的参数原本以「帧」为单位，
30 → 60 会让异常帧门限无声地松一倍（5cm/帧 = 1.5 m/s → 3 m/s）、拒收逃逸口的耐心砍一半
（4 帧 = 133ms → 67ms）。现在三处全部按时间表达 —— α 由 `dt` 算、门限是速度、逃逸口是
毫秒 —— 并有测试钉住「同一物理速度在 30/60 下判定一致」。

满足这三条之后提高帧率是纯赚：时间常数不变而采样点变多，同一时间窗里平均掉的噪声更多，
抖动抑制自己变好，不用动任何参数。

**未验证**：这台机器（M2012K11C / Android 12）在长边 ≥1280 的档位上到底有没有 60fps 档，
只能看 logcat 里 `ARCore CPU 图像尺寸 = … ｜帧率 = …` 那一行。不确认就不能说这次改动生效了。

### 循环播放：两处各自看都像对的

`ScanController.onPlaybackEnded` 的注释写着「AR 模式下播放器是循环的」，而 `VideoPlayer`
设的是 `REPEAT_MODE_OFF`，靠那个回调调 `playVideo()` 来兜。但 ExoPlayer 在 `STATE_ENDED`
时调 `play()` **什么都不会发生** —— 播放位置在末尾，不会自己回头。所以循环一直是坏的。

现在 AR 模式用 `REPEAT_MODE_ONE`（无缝，也不再触发 ENDED），同时把 `play()` 修成在 ENDED
时先 `seekTo(0)`，让那条兜底路径真的能兜住。全屏兜底模式**故意不循环** —— 它靠「播完」这个
事件退回扫描，那是它唯一的出口。

---


## 10. 本地验证（3 CPU / 3GiB）

真镜像、真 compose、真 Oxford5k 照片，两个后端各跑一遍：

```
docker build -t photo-ar-server:dev .          # tools/arcoreimg 缺席也构建成功
docker compose up -d                            # 无 config.json / 无词表 / 无 token / 无模型
                                                # 36 秒 healthy；日志打出引导管理员的随机口令
```

- 资源上限实测生效：`NanoCpus=3000000000`、容器内 `cpu.cfs_quota_us=300000/100000`
- 鉴权：无凭证 / 空 Bearer / 乱 token / 空 cookie 全部 401
- 入库 110 张真实照片 → 45 张通过，63 张 `quality_too_low`、2 张 `near_duplicate`
- 识别 15 次 → 14 命中、**0 认错**、1 次 `ambiguous`（同一建筑的不同照片）
- 授权：未授权时 `/v1/photo/<id>` 403、`/v1/photos` 0 张、扫到未授权那张返回
  `matched:false reason=forbidden`；授权一张后那张 200、别的仍 403
- 两个后端的库目录并存（`data/library` 与 `data/library_xfeat`）

**验证过程中挖出的一个真 bug**（我引入的）：`xfeat._default_threads()` 只读 cgroup **v2**，
而本机 Docker 与 **QNAP 的 Container Station 都是 cgroup v1** —— 它静默落到
`sched_getaffinity` 返回宿主机 16 核，于是 ORT 在 3 CPU 配额下开了 16 个自旋线程，把整个
进程饿住（连 cv2 的几何校验都从 1.4ms 变成 24ms）。**目标机器正好落在那条坏路径上。**
修完 XFeat 延迟从 1667ms 降到 800ms。

测试：Python **796 passed**、Kotlin **623 passed**（arview 580 + app 43）。既有测试一条没
削弱、没跳过。

---

## 11. 已知风险与下一步必须做的测量

按严重程度排序。

1. 🔴 **服务端预建目标库能不能被端上 ARCore 装载，完全没验过。** 这是现在最大的未知量，
   因为它是新主路径的前提。`arcoreimg` 是闭源二进制、不在仓库里，所有测试跑的都是假二进制；
   「1000 目标的库约 6MB、deserialize 10–20ms」全部来自官方文档，一个都没测。
   回退路径（装不上 → 退回端上现建）也只验了决策与记账，没验过 ARCore 真抛异常时的行为。
2. 🟠 **内置运行时已在真机上装通，但只在一台有 Google 框架的机器上**（Redmi K40 Pro /
   MIUI 13，完整时间轴见 §9.1）。已经有真机证据的：MIUI 必然拦掉 `PackageInstaller` 会话、
   老式 Intent 退路能装上、装的是我们内置那份（version code 260890493）、装完会翻成
   `SUPPORTED_INSTALLED` → `START_AR`、会话 resume 后 VIO 真的在跟踪。
   **仍然没有真机证据的恰好是最主要的目标场景**：没有 GMS 的机器上 `checkAvailability()`
   返回 `UNKNOWN` 之后那条「仍然选择装」的路，只有单测。`DEVICE_NOT_CAPABLE` 同理
   （`haydn` 本身是支持机型）。要补就得找一台没刷 GMS 的机器，或停用 GMS 后重跑。
3. 🔴 **`XFEAT_MIN_INLIERS = 60` 待复核**：只量了精排、样本 1500 次。
   `XFEAT_DEDUP_MIN_INLIERS = 38` 更弱，是按比例推的、没独立量过。
   要复核就跑 `bench/threshold_scan.py` 走完整两阶段管线。
4. 🟠 **`arcoreimg` 的建库耗时已实测（23.4s / 1000 张，见 §0.1）**，但 `RETRY_AFTER_S`
   还没按这个数校准 —— 现在的值是当初猜的。
5. 🟠 **内置分发的授权边界没有结论，这条只是记账**（打进包里是已定的决策，不再讨论）：
   ARCore 的附加服务条款里没有再分发条款；`com.google.ar:core` 的 Apache 2.0 只覆盖客户端库
   （google-ar issue #1538）；源码包里有文件写着 DO NOT REDISTRIBUTE。要对外发布之前，
   这件事需要一个明确答复。
6. 🟠 **XFeat 在真机上大概率超时** —— 但这条已经不在 Android 热路径上了（见 §0.1）。
   本机 3 CPU 下 p95 1101ms，N5095 没有 AVX2 会更慢。它现在只影响溢出兜底与网页版。
   缓解手段（都已存在）：把 `recog.top_k` 从 20 调小（配对成本线性于候选数，调到 5 约能砍到
   200ms），或者用 ORB。这个旋钮在管理台里，热改生效。
7. 🟠 **端上 XFeat 一行真机代码都没跑过。** 契约层面用跨语言 golden 钉住了，但「契约对了」
   不等于「识别率不掉」。真机第一件事应该是：同一张图，端上提特征 → 服务端匹配，与
   服务端自己提特征 → 匹配，两条路的内点数对照。
8. 🟠 **Compose UI 没跑过。** 登录表单、账号区、端上特征开关的渲染全未验证（业务逻辑抽成了
   纯类并有 23 条单测，Composable 里只剩渲染）。
9. 🟠 **管理台只在无头 Chrome 里点过**，没在真手机浏览器上点过；Safari / Firefox / 老
   WebView 的降级分支没实测；读屏与纯键盘没实听。
10. 🟡 **XFeat 的默认下载地址是 404**：它指向本项目自己的 GitHub release
    `models-v1/xfeat.onnx`，而那个 release **还不存在**。要么发布它，要么用
    `tools/export_models.py` 自己导出，要么用 `--url` 指别处。**刻意没有指向未验证的第三方
    地址。**
11. ✅ **已做**：模拟器那两套 ABI 在出包时剔除，实测省 39.3 MiB（155.7MB → 114.5MB）。
    开关是 `-Pphotoar.deviceAbiOnly`，**默认不开**（本地要跑 emulator），只有
    `.github/workflows/android.yml` 出包时传它，并在出包后硬校验 `lib/x86*` 确实不在。
    见 §9.1「包体积」。
12. 🟡 **转码与核显硬编本轮完全没碰也没测**（本机 `/dev/dri` 里没有 `renderD128`）。
13. 🟡 **视频关联与取流（Range/206）没有端到端验证**，本轮入库全是纯照片。
14. 🟡 **XFeat 的存储布局是 ORB 的 11.3 倍**（135,176 vs 12,008 字节/张，1 万张约 1.35GB）。
    仍然只在精排时 mmap 随机读，所以影响磁盘不影响内存 —— 但足以改变 NAS 上的容量规划。
15. 🟡 **`floatvocab` 的分支/深度是从二进制版移植的结论**，没在 XFeat 描述子上重扫。

## 12. 环境限制（会影响后续任何人复现）

- **huggingface.co 不可达**（实测 curl 超时）；`hf-mirror.com` 的 `/resolve/` 路径会 **308 跳回**
  被封的 huggingface.co，等于不可用。可达的是 `github.com`、
  `objects.githubusercontent.com`、`dl.fbaipublicfiles.com`。
  **所有只在 HF 发权重的模型（MegaLoc、各种预导出 ONNX、OpenCLIP 权重）在这个环境里直接淘汰。**
- **onnxruntime 与 Python 版本**：1.25.0 起要求 Python ≥ 3.11。镜像基底是 `python:3.11-slim`
  没问题，但本地 venv 是 **3.10**，最后支持它的是 **1.23.2**。`pyproject.toml` 的约束同时满足两者。
- **ONNX Runtime Android 依赖不在冷缓存里**：`onnxruntime-android:1.20.0` 是这次从 Maven
  Central 下的（27.9MB，4 个 ABI）。一台冷缓存 + 无网的机器构建会失败。解包后每个 ABI 的
  `libonnxruntime.so` 实测 19.4 MiB（arm64 17.0 / v7a 11.9），出包时用
  `-Pphotoar.deviceAbiOnly` 剔掉 x86 两套省 39.3 MiB（见 §9.1「包体积」）。
- **基底镜像不能按 `x86-64-v3` 编译** —— N5095 没有 AVX2，会直接跑不起来。

---

## 13. 照片实际尺寸不知道：`printWidthMm` 从必填改成可选

原来入库必须给 `printWidthMm`，缺了就 400 `missing_print_width`，理由写在错误文案里：
「跟踪精度依赖它，所以不给默认值」。

那个理由**半对半错**，而错的那半更要紧：实际照片尺寸经常就是不知道的（用户给的原话是
「照片实际尺寸是不一定的，但一定是矩形的」），强制必填的结果是有人随手填一个数 —— 而一个
**猜的**宽度比不填更糟。烘进 `.imgdb` 之后 ARCore 会当真并照它回显 `getExtentX`，端上按这个
错数字画四边形，位姿却来自量纲真实的 SLAM，两个尺度一错位视频就贴不上（见 9.3 原因一）。

对的那半保留了：**知道**真实宽度时填上确实更好 —— ARCore 不必估尺度，检测更快更稳。所以
字段留着，只是不再强制。

### 实测：省略宽度写进 `.imgdb` 的是 `-1.0`

`arcoreimg` 的清单格式里宽度本来就是可选的（`--help` 的示例第二行
`little dog|/path/to/dog_image.jpg` 就没有宽度）。同一张照片建两次库（tools/arcoreimg 1.2，
708×468 JPEG），两个产物都是 6406 字节，`cmp -l` 显示**只差 4 个字节**，偏移 0x9DC–0x9DF，
解成小端 float32：

    带 0.30 → 9a 99 99 3e = 0.30000001
    省略    → 00 00 80 bf = -1.0     ← 「未知，你自己量」的哨兵

这条实测同时排掉两个猜测：省略**不是**写 0（写 0 会让 ARCore 按 0 米宽算位姿，端上彻底
贴不上），这个字段也**不是**没用。整库（`GET /v1/targets/db`）里两张未知宽度的照片对应
2 个 `-1.0`、0 个 0.3，也验过。

### 库里 0 = 未知，不需要迁移

`photo.print_width_m` 仍是 `REAL NOT NULL`，0 是合法值。负数**仍然拒**：它不是「未知」，
是算错了或单位搞反了，静默当未知处理会把一个真实的 bug 藏起来。

### 客户端原来把「宽度未知」当坏数据丢掉

这是改动里最容易漏的一半。`Api.recognize` 抛 `ApiParseException`，
`CacheIndexCodec` / `ServerTargetsCodec` / `targetsManifest` 直接 `continue` 跳过整条记录。
理由都是同一句「那个值会被拿去贴视频，错了不报错只会让画面一直飘」—— 在尺度改成取
`extentX` 之后这个理由不成立了，而丢掉的代价是**这张照片永远进不了端侧库**，离线命中对它
彻底失效，每次都得往服务端跑一趟。

`AugmentedImageDatabase.addImage` 也必须分岔：宽度未知时走**不带宽度**的那个重载，
不能传 0（传了 ARCore 会当真）。

### 顺带

识别响应加了 `title`。它只有一个用途：客户端「保存到相册」拿它当文件名。没有的话相册里
全是 `photoar-603409ee.jpg`，一场婚礼存十几张之后谁也认不出哪张是哪张。

---

## 14. `arcoreimg` 会自己补 `.imgdb` 后缀 —— 整库构建一直是坏的

`arcoreimg build-db` 在 `--output_db_path` **不以 `.imgdb` 结尾**时会自己补上后缀，写到
`<给的路径>.imgdb`，而且退出码仍然是 0、stdout 里还如实打了真实路径
（`Image database generated at: …`）。

`server.targets` 建整库时按 `<版本>.imgdb.tmp-<pid>-<tid>` 命名临时文件（不以 `.imgdb`
结尾），于是每一次整库构建都以 `arcoreimg build-db 未产出 …` 失败。**而离线识别整条路就是
靠整库**，所以 Phase 4 的离线命中一次都没成功过 —— 表现只是「离线不生效」，没有任何报错
指向 arcoreimg。`/data/targets/` 里躺着的孤儿产物时间戳就是证据。

修在 `quality._build_db`：它是全工程唯一直接对接 build-db 的地方，契约的怪癖就该收在契约的
边界上，否则下一个想用临时文件名的调用方会再踩一遍。

**更要紧的是同时改了假 arcoreimg**（`tests/conftest.py`）：它原来老老实实写到给定路径，
和真件在这一点上分叉 —— 正是这个分叉让整库构建的 bug 在全绿的测试下活了下来。现在 fake
模仿真件的补后缀行为，并新增一条用 `targets.py` 真实临时文件名的回归测试。

教训与 bench 那次默认值漂移是同一条：**任何替身（fake / bench 默认值）与真件行为分叉的地方，
就是 bug 的藏身处。**

---

## 15. 保存照片与视频到相册

一个按钮，一次存原图 + 视频（照片进 `Pictures/PhotoAR/`，视频进 `Movies/PhotoAR/`）。

几个不显然的决定：

- **新增 `GET /v1/photo/<id>/ref`。** 客户端原来只拿得到 `refThumbUrl`（缩略图），存下来是
  糊的 —— 实测原图 356,950 字节 vs 缩略图 81,207 字节，而这个错误不报任何错，用户要打开相册
  才发现。用 photoId 进而不是暴露 `ref_asset_id`：asset 是跨照片共享的表，多一个可枚举的 id
  就多一条越权的路。
- **Content-Type 按扩展名给，不能一律 `image/jpeg`。** 入库允许 PNG/WebP，而客户端存相册是
  按 MIME 建条目的，标错了相册里就是一张打不开的图。
- **Android 10+ 不需要任何权限**（MediaStore + `RELATIVE_PATH`）。清单里那条
  `WRITE_EXTERNAL_STORAGE` 带 `maxSdkVersion="28"`，只给 API 24–28 用 —— 不写的话
  Android 10+ 用户会在应用信息里看到一个 App 从不使用的「存储」权限。
- **写入期间置 `IS_PENDING=1`**，写失败要把占位条目删掉。不然相册里会留一个 0 字节、永远
  pending 的幽灵条目，用户看不到也删不掉。
- **文件名带 photoId 前 8 位。** 同一场婚礼里「合照」这种标题会重复，而重复保存同一张照片
  应该是**同名**（语义上就是「这一张」），不是让 MediaStore 加个 `(1)`。
- **部分成功是正常结果，不是异常。** 只有照片没有视频的条目本来就存在；视频存失败时照片
  已经进相册了，回滚它对用户没有任何好处。所以提示是「已保存照片到相册；但视频：网络超时」，
  不是一句「保存失败」。

---

## 16. 开发机上的固定口令为什么不写在 compose 里

`deploy/compose.local.yml` 里刻意**没有**写死 `PHOTOAR_ADMIN_PASSWORD`，而是从仓库根目录的
`.env`（在 .gitignore 里）取。

理由：**这个仓库是公开的**。而 `app.Server._bootstrap_admin` 的注释里论证过为什么固定默认
口令等于没有口令 —— 「那个默认值就印在这份源码里」。写在 `compose.local.yml` 里就正好犯了
那一条，哪怕文件名带 `.local`、哪怕上面有一堆警告注释：任何人 clone 下来照着跑，得到的都是
一个 `admin/admin` 的服务。

开发机上的便利照旧（省得每次重建容器去翻日志找那个只打印一次的随机口令），只是那个值留在
本地 `.env` 里。`.env.example` 里只留空值和说明。

---

## 17. 登录蒙版与角色分流：改造前访客的界面本来就是坏的

### 17.1 这不只是加功能，是修一个已经存在的错

改造前：登录埋在设置页的账号区里，App 一打开落在照片库，底栏三个格子是「照片 / 历史 / 设置」。

问题在于 **`/v1/history` 在服务端是 admin only**（它是**全库**的识别记录，`recognize_log` 里
没有「谁扫的」这一列，不过滤就等于把全库照片的标题发给任何一个访客）。也就是说：一个访客账号
登进来，底栏那三个格子里有一个点进去只有 403，而他什么都没做错。

同时对宾客来说，App 一打开是一个空列表加一句「连不上」，而他要做的事（登录）在第三个页签里
往下滚两屏。没有人会找到那里。

所以这一轮做的两件事是**同一件事的两半**：把「你是谁」提到最前面（蒙版），后面的界面才有可能
按角色给对（分流）。

### 17.2 蒙版分两步：先地址，再账号

`NavPolicy.needsGate(hasUsableEndpoint, phase)` 有**两个**条件，缺一不可。

第一版只判凭证。那是错的：全新装机两样都没有，那时只弹一个用户名口令表单，人填对了也登不进去
—— 因为根本没有地址可以发那个请求，而错误信息会是「连不上」。用户唯一的出路是清数据重装。

分两步的另一个理由是别把四个输入框摆在一起：其中两个（地址）跟用户心里的「登录」毫无关系。
绝大多数人（地址由管理员配过）只会看到第二步。

「改地址」那个按钮**不能**靠 `hasUsableEndpoint` 退回第一步：地址已经存下来了，那个判断仍然
是 true，于是点了没反应 —— 而这个按钮存在的场景正是「地址填错了，每次登录都失败」。所以另有
一个显式的 `forceEndpoint` 标志。

### 17.3 哪些阶段**不**挡

`AuthPhase` 有五个取值，只有两个要挡（`LOGGED_OUT` / `EXPIRED`）。另外三个刻意放行：

- `EXPIRING_SOON`：还能用。挡住等于把一个好用的装机变成不能用的。设置页里有横幅提醒。
- `UNKNOWN_TOKEN`：Phase 3 手填令牌的老装机升上来就是这个状态。它仍然能扫。
- `ACTIVE`：显然。

这三条各有一个测试盯着，因为「顺手把它们也挡上」看起来更安全，实际是把能用的装机弄坏。

### 17.4 页签怎么分

| | 访客 | 管理员 |
|---|---|---|
| 底栏 | 扫一扫 / 设置 | 照片 / 素材 / 管理 / 设置 |
| FAB | **没有** | 扫一扫（底栏中间，76dp） |

访客的首页整页就是一颗 200dp 的「扫一扫」，所以**不给他 FAB** —— 同一个动作两个入口，而且
那颗 FAB 正好压在大按钮上。管理员反过来：他的四个页签都不是扫描，FAB 是他唯一的扫描入口
（也是第 2 轮那条「扫一扫要在底部中间、更醒目」的要求）。

首页底下那行「你有 N 张照片可扫」不是装饰。**N = 0 时尤其重要**：那时他扫什么都不会动，而
原因不在他这边（管理员还没授权），这句话是他唯一能据此去问人的线索。写成「暂无照片」等于
什么也没说。

### 17.5 为什么 `NavPolicy` 是一个单独的、有测试的纯逻辑文件

它管的是权限边界。写在 Composable 里的话唯一的验证方式是装机点一遍，而**「访客身上少挡了
一个页签」这种错在自己手机上（管理员账号）永远看不出来** —— 得拿一个访客账号登进去才会现形，
而那正是最容易忘的一步。

`tabAfterRoleChange` 那条尤其：同一台手机换人登录（管理员登出、家里人用访客身份进来），界面
还停在「素材」页，那一页上每个按钮都会 403。这条只在换人时触发，手测几乎不会走到。

### 17.6 客户端挡的不是安全

服务端才是权限的真相：`/v1/history`、`/v1/fs/list`、`/v1/admin/…` 全是 admin only。这里挡的
是**可用性** —— 少给访客一个点不动的入口，不是把风险挡在外面，是别把一条必然失败的路摆在
他面前。

---

## 18. 管理台内嵌进 App：token 就是 cookie 的值

### 18.1 为什么内嵌，而不是用 Compose 重写

管理台已经有 1300+ 行 JS 在做用户、授权、配置、映射、批量导入这五件事，而且它**跟着服务端
一起发版** —— 服务端加一个配置项，管理台立刻就有那一行，用户不需要更新 App。在 Compose 里
重写一遍换来的是「同一件事有两套实现」，而其中一套永远慢一个版本。

代价是这一页在离线时是白的。可以接受：管理台上每一个动作都要打服务端，离线时就算界面画出来
也一样什么都做不了。

### 18.2 单点登录不是绕过鉴权

App 手上是一个 Bearer token，管理台**只认 cookie**（它刻意不把 token 存进 localStorage，
理由在 `app.js` 的文件头）。看起来要登两次。

但服务端下发那个会话 cookie 时，写进去的**就是同一个 token**：

```python
# app.Server._session_cookie
f"{SESSION_COOKIE}={token}"
```

所以把 App 的 token 塞进 WebView 的 `CookieManager`，管理台一进去就是已登录状态。服务端那边
同一个 `_credential` 本来就认两条路（App 用 Bearer 头，浏览器用 cookie）—— 这是把同一份凭证
换了个带法，不是多开一个门。

两处硬约束，改一边必须改另一边：

- cookie 名 `photoar_session` 在 Kotlin 侧写死，与 `app.SESSION_COOKIE` 逐字对应。猜错的表现
  是「进去还要再登一次」，**没有任何错误信息**。
- **不能设 `Secure`**：这条链路可能是 http 的内网直连（服务端那边 `PHOTOAR_COOKIE_SECURE`
  默认也是关的）。设了的话 http 下 WebView 会直接丢掉 cookie，表现同上。

### 18.3 一个必须告诉用户的副作用

同一条 session 意味着**在管理台里点「登出」会把 App 的登录一起作废**。这不是 bug，是「同一份
凭证」的必然结果，但它看起来像 App 自己掉线了。所以「管理」页上那段文案里明写了这一条 ——
App 管不着 WebView 里的按钮，只能说清。

---

## 19. Excel 批量导入：零依赖手写 xlsx，以及「先算计划，再逐行执行」

### 19.1 为什么不用 openpyxl

要做的事只有两件：**写**一个只有文字的单页表、**读**回一个只有文字的单页表。xlsx 本质上就是
一包 XML 塞进 zip，stdlib 的 `zipfile` + `xml.etree` 正好够（`src/photoar/sheet.py`，约 300 行
含注释）。这个项目对每一个依赖都写过论证（见 pyproject 里 onnxruntime 那 15 行），加一个依赖去
换 300 行不划算。

产出用 `openpyxl` 与 `pandas` 交叉验证过（都不在项目依赖里，只用来验），`file(1)` 也认成
`Microsoft Excel 2007+` 而不是 `Zip archive`。

### 19.2 写与读的方向**不对称**，这是刻意的

- **写**：每个格子都是 `inlineStr`（字符串）。这张表里唯一像数字的东西是「打印宽度」和用户名，
  而**用户名恰恰是最容易被 Excel 当成数字的** —— 一个叫「007」的宾客存成数字之后读回来是 `7`，
  而那时人已经登录不上了。全字符串让这类静默变换不可能发生。
- **读**：Excel 存的数字**一定**是数字类型，要把它变回字符串。而且 Excel 会把 400 存成 `"400.0"`
  （取决于它内部怎么算的），直接 `str(float(...))` 会让下游 `int()` 拒绝它 —— 一个只在某些
  Excel 版本上出现的导入失败。

两个方向不对称，是因为「我们写的」和「人在 Excel 里改过的」不是同一份文件。

### 19.3 读 xlsx 最要紧的一条：靠 `r` 属性定位，不能按出现顺序

Excel **不写空格子**。所以「张三 | (空) | video.mp4」在 XML 里只有两个 `<c>`，第二个的
`r` 是 `"C2"`。按出现顺序读会把 `video.mp4` 放到第 2 列，也就是当成**照片路径**去入库 ——
不报任何异常，只会在入库时说「这不是图片」，而人会以为是自己的视频文件有问题。

`tests/test_sheet.py` 里有一整组用例是**手工拼出 Excel 真实产出的结构**（sharedStrings +
省略空格子 + 数字类型 + 富文本）来验这条。自己写自己读的往返测试一定过 —— 我们写的格式正好把
Excel 特有的三个坑全绕开了，往返绿灯等于什么都没验到。

### 19.4 「先算计划，再逐行执行」

`POST /v1/admin/import/parse` **一行都不写库**，只返回一份计划；由浏览器拿着它去逐行调既有的
`/v1/admin/users`、`/v1/photo`、`/v1/photo/<id>/video`、`/v1/admin/users/<id>/grants`。

不做成一个同步导入接口的三个理由：

1. 一次请求要几分钟（每张照片都要跑 arcoreimg + 特征，视频还可能转码），反向代理与 Cloudflare
   隧道那 125 秒超时都会先掐断它；
2. 第 37 行才发现路径写错，前 36 行已经落库了，而调用方只看到一个 500；
3. 想知道进度只能等它结束。

拆开之后，**预演（dry-run）是免费的**：一份写错的表在动手之前就全暴露了，包括「路径不在白名单
里」「文件不存在」「把视频填进了照片那一列」—— 后者是最难自己发现的一种错，两列都填了合法路径，
只是填反了。

重跑同一份表是安全的，这依赖两条**既有**行为而不是新写的逻辑：`user.name` 有 UNIQUE 约束
（重复建回 409），`ingest_photo` 对同一张参考图抛 409 `already_ingested` **并带上 photoId**
（所以浏览器还能接着用那个 id 去配视频、授权）。

### 19.5 两处设计上的自我纠正

**口令要回显。** 第一版刻意不回显，理由写的是「服务端没有理由把它再发一遍」。那个理由是错的：
执行者是浏览器，它建管理员账号时必须把口令放进 `POST /v1/admin/users` 的请求体 —— 不回显就等于
这套批量导入建不出管理员，模板里那一列变成一个填了也没用的摆设。而回显不多泄露任何东西：这个
口令几秒钟前就在**这位管理员自己上传的文件**里，走的是同一个连接、同一个会话。真正要防的是它
被缓存下来，所以那个响应带 `Cache-Control: no-store`。

**`nameKey` 要由服务端算。** 浏览器要把表里的「张三 」对上库里已有的「张三」。让 JS 自己实现
一遍 `normalize_name` 是错的 —— `casefold` 与 `toLowerCase` 对某些字符结果不同（ß → ss），
两套实现只要有一处不一致，表现就是**授权静默不生效**：用户建出来了、照片入库了，就是没关联上，
而界面上每一步都显示成功。所以 `/v1/admin/users` 与计划的每一行都带上服务端算好的 `nameKey`，
匹配由构造保证。

### 19.6 导出要能被导入吃回去

「用户」那份导出的表头与模板**逐字一致**，因为它的主要用途是「导出 → 在 Excel 里改 → 导回去」。
有一条测试专门盯这个往返。相关的两个细节：

- 没有任何授权的用户**仍然占一行**（只填名字）。少了那一行的话，往返会把没授权的用户从表里
  弄丢，而人不会注意到。
- 未知宽度导出成**空**而不是 `0`。导入侧两者都当未知，但 `0` 会让人以为库里真记着一个 0。

「映射」那份的表头**故意与模板不同**（第一列是 photoId）。硬凑成同一套会让「导出映射 → 直接
导入」看起来可行，实际上会把 photoId 当用户名去建用户。

---

## 20. 一个真 bug：`Content-Disposition` 里的中文名会崩掉服务端线程

导出接口第一版把中文文件名放进了 `filename="photoar-模板.xlsx"`。**全套测试绿**，但真实请求
一打过来，服务端线程直接：

```
UnicodeEncodeError: 'latin-1' codec can't encode characters in position 51-52
  File "http/server.py", line 526, in send_header
    ("%s: %s\r\n" % (keyword, value)).encode('latin-1', 'strict')
```

HTTP 头只能是 latin-1，而 `http.server.send_header` 是拿它硬编码的。讽刺的是那一行的注释里就
写着「只写 `filename=` 的话浏览器按 latin-1 解」，然后还是把原文塞了进去。

正确写法是两份都给：`filename=` 用**纯 ASCII**，中文走 RFC 5987 的 `filename*=UTF-8''` 百分号
编码。

### 20.1 测试为什么没抓到，以及这次怎么修的

根因是测试装置与真货的差异：`Env.request` 直接调 `Server.handle()`，拿到的是一个 `Response`
对象；真实路径上 `httpd._write` 才会把每个头交给 `send_header`。也就是说**「能构造出来」和
「能发出去」是两件事，而测试只验了前一件**。

这是本项目第三次踩到同一类问题（前两次：假 `arcoreimg` 写到给定路径而真工具会补后缀，让整库
构建的 bug 在全绿套件下活了很久；`simcam` 的默认值复制了入库侧常量而不是引用查询侧的）。规律
已经很清楚：**替身与真货行为不同的地方，就是 bug 的藏身处。**

所以这次不只修那一处，把检查加到了 `Env.request` 上 —— 每个响应的每个头都做一次 latin-1 编码。
放在那里而不是只在导出的用例里，是因为这一类 bug 与接口无关：任何一个把库里的中文（文件名、
用户名、照片标题）拼进响应头的地方都会踩到，包括以后新加的接口。

加完之后跑了全套 941 个用例，**没有翻出别处的既有问题** —— 只有导出这一处踩到。

---

## 21. 换参考图：为什么不是「删掉重建」

### 21.1 需求

「先拿手机拍的一张糊照片入了库，后来有了扫描件」——或者打印件重印了一版，裁切和颜色都
不一样。App 的「素材」页上那条**上传历史**要能在每一条上换照片、换视频，所以服务端需要一个
「换掉参考图但保住这张照片的身份」的操作。

### 21.2 「删掉重建」要付两笔代价

**授权全丢。** `photo_grant.photo_id` 是 `ON DELETE CASCADE`，删一张照片会连它的全部逐张
授权一起消失。一场婚礼几十个宾客各自被授权几张，重建之后要一张张重新勾。

**删除本身是识别库里最危险的一次改动。** `desc.bin` / `words.bin` / `slots.json` 是三份按
slot 一一对应的定长存储，删中间一张要把后面所有 slot 往前挪。那条路径出错的症状是「照片 A
的描述子挂在照片 B 的 id 上」——识别命中之后播的是别人的视频，而 `_assert_aligned` 查不出来
（它只比条数，而条数是对的）。

换参考图不用碰这些：slot **原地替换**，photo_id 不变，于是授权、配的视频、标题、打印宽度、
贴合模式全都留着。所以这一轮做了 `POST /v1/photo/<id>/ref`，而**没有**做删除照片。

### 21.3 原地替换不能真的「原地写」

第一版的想法是 `seek(slot * stride)` 然后写 12 KB。那是错的，而且错得很隐蔽。

`DescStore` 用的是 `np.memmap(mode="r")`，在 Linux 上那是 `MAP_SHARED`。另开一个文件句柄写
同一个文件，**这些字节会透过已有的 mmap 被看到**。于是一次正在进行的精排可能读到半新半旧的
槽：pts 来自旧图、desc 来自新图。不崩，只是匹配结果静默错。

追加（`library.add`）没有这个问题，是因为它写在旧快照 `count` **之外**，老读者永远不看那里。
原地替换没有这个便利，所以只能：

1. 把整份 `desc.bin` 复制到临时文件，其中那一槽换成新的；
2. `words.bin` 同样处理；
3. **两份都写成了**才开始 `os.replace`。

老快照继续持有旧 inode（已 unlink 但句柄还开着），把手上的请求跑完；新读者才看到新数据。
两份必须一起落地——只换一份就是错位，后果同 21.2 那一段。

代价是重写整个 `desc.bin`。实测每槽 12008 字节，2000 张 = 23 MiB，一次「换照片」重写它可以
忽略（真机实测整个替换 499 ms，含 arcoreimg 与 20 次扰动查询）。

`tests/server/test_library_replace.py` 里有一条 `test_在飞的读者看到的还是旧数据`：它自己开一份
`DescStore`（模拟「正在精排的请求」），替换之后断言那份 mmap 读出来的还是旧值。**把实现改回
原地写，这条会红** —— 已经实测验证过。

### 21.4 去重必须排除自己，而且不能靠「从 known 里删掉」

最主要的用法是「同一张照片重新扫一遍，换上更清楚的那份」。新图与库里的旧特征必然近重复，
所以去重闸门要排除这张照片自己。

第一版的写法是把它从 `known_self_scores` 里删掉。**那让情况更糟**：`conflicts` 对查不到分数的
照片按「极低」处理（注释里写着「宁可多报一次冲突」），于是 `min(s_new, 0) < ratio * m` 恒成立
—— 把「可能冲突」变成了「必然冲突」，这个接口对它最主要的用法 100% 失败。

正确做法是给 `conflicts` 加一个显式的 `exclude=` 参数，在候选循环里跳过那个 pid。

真机验证：拿 `wedding-01.jpg` 的一个略微裁切缩放版去换它自己 → 200，换完用新图识别得到
161 个内点、同一个 photoId，而**另一张照片仍然认得出**（170 个内点）。两条拒绝路径也验了：
拿库里另一张照片的文件去换 → 409 `near_duplicate`；拿一张随机噪声图去换 → 422
`quality_too_low`（真 arcoreimg 给 10 分）。两次都如实报出「原来那张没有被换掉」，而且原图
确实完好。

---

## 22. App 的「素材」页：上传即映射，历史里可换

### 22.1 两个独立的上传按钮是错的分解

改造前这一页有「传照片」「传视频」两个按钮，各自把文件传到 NAS 的收件目录。然后人要自己去
照片库入库、再去管理台把视频配给照片 —— 三个地方三步，而这三步之间**没有任何选择**：传上来
的这张照片和这段视频本来就是一对（那正是这个 App 存在的意义）。

现在一次操作走完：传照片 → 传视频 → `POST /v1/photo {refPath, videoPath}` → 一组映射。

视频允许留空（先入库、晚点在历史里补），但那条成功提示会明写「**还没配视频**，扫到它不会播
任何东西」—— 忘了配视频的后果要等到有人举着手机扫的时候才发现，而那时人已经不在电脑前了。

这一页刻意**不问打印宽度**，直接传 0（未知）：从手机相册传上来的照片，人几乎不可能知道它印出来
会是多宽，而一个猜的宽度比不填更糟（第 13 节）。

### 22.2 上传历史为什么存在本地

服务端的 `/v1/admin/mapping` 能列出**全库**的照片和它们配的视频。但那回答不了这一页要回答的
问题：「**我刚才**传的那几组现在怎么样了」。一场婚礼后台几百张，而人上传完想确认的是自己这
十几组。

而且本地这份还记着**原始文件名**（`IMG_2034.jpg` 传上去之后在服务端叫什么完全取决于相册给的
名字），那是人认出「哪条是哪条」的唯一线索 —— 服务端只有标题，而标题常常是空的。

一条记录里只有 photoId + 两个文件名 + 时间。缩略图、标题、当前配的视频都**现取**
`/v1/photo/<id>` —— 存下来就会和服务端不一致，而人分不清「界面显示的是旧的」和「服务端真的
还是旧的」。photoId 是唯一需要记住的东西，因为它是问服务端的钥匙。

`HistoryCodec` 是纯函数、有 11 条测试，重点全在**坏存档**上：解析失败绝不能抛。历史只是个便利
视图，为它把整个素材页搞崩是完全不值的交换。部分可读时保住能读的部分（整份丢掉会让人以为
「历史清空了」），没有 photoId 的条目直接丢掉（留着只会在界面上变成一个点了没反应的条目）。

---

## 23. 移除 App 里的「浏览 NAS」

`BrowseScreen` 与 `CreateScreen` 删掉了，`Route.Browse` / `Route.Create` / `Pick` / `Draft`
一并移除。

入库现在只有两条路，各自服务不同的素材来源：

| 素材在哪 | 走哪条 |
|---|---|
| 手机相册（当天刚拍的） | App「素材」页，挑一张照片 + 一段视频 |
| NAS 上已有的文件 | 管理台「批量」页，Excel 导入 |

两条都指向同一个 `POST /v1/photo`，区别只在素材从哪来。留着 App 里的文件浏览器就是同一件事的
第二个入口，而它还得自己处理白名单、类型判断、缩略图预览 —— 而管理台那条路上这些都有，还多了
执行前的逐行预演。

三个原来指向它的入口重指了：照片库右上角的「＋」切到「素材」页；照片详情页的「换视频」变成
一句指路（换照片/换视频都要先把文件从手机传上去，那些进度与隧道限制的处理都在素材页）；
素材页自己那个「浏览并入库」直接去掉。

---

## 24. 管理台：照片与映射合并，每个分区一个 URI

### 24.1 合并

「照片」页原来是只读的库清单（质量分、贴合模式、入库时间），「映射」页是照片↔视频。它们**本来
就是同一份数据的两种看法**，分开的结果是同一行信息在两处各显示一半，而人想问的是「这张照片
现在到底怎么样」。

合并之后「照片」页有两个方向的切换：**按照片**（这张配了吗）与**按视频**（这段视频影响谁）。
后者不是前者的转置视图 —— 一段迎宾视频往往配给很多张照片，改它之前要知道会牵动哪几张。

数据源统一到 `/v1/admin/mapping`（补了 `fitMode` / `refStale` / `createdAt`，一次拿齐）加
`/v1/admin/videos`。不再拉 `/v1/photos` —— 那个接口留给授权页，它需要的是「这个人能看到哪些」
的口径。

### 24.2 每个分区一个 URI

`/admin/users`、`/admin/photos`… 用**真实路径 + `history.pushState`**，不用 `#hash`。理由是这些
地址是要发给别人和收藏的（「配置在这儿：<地址>/admin/config」），而 hash 在很多聊天软件里会被
吞掉或者变成不可点的一段。

服务端要认这些路径，否则**刷新页面就白屏** —— 而「刷新」正是独立 URI 最主要的用途。
`_route_webui` 里加了一句：路径落在 `_WEBUI_TABS` 里就返回首页，由前端读 `location.pathname`
决定打开哪个分区。

**没有**做成「任何找不到的文件都回首页」的兜底：那种写法会把 `/admin/app.js` 拼错时的 404 变成
一份 HTML，而浏览器会拿 HTML 当 JS 解，报出来的是一句莫名其妙的语法错误。所以只认一张固定清单。

那张清单在两处（`app._WEBUI_TABS` 与 `app.js` 的 `TABS`），对不上的后果各有一种、都不好查：

- 服务端多一项：那个地址打得开，但前端不认，回落到默认分区（地址栏和内容对不上）。
- 服务端少一项：那个地址刷新时 404，而它在页面里点得到（前端 pushState 写得进去）。

所以 `tests/server/test_app_webui.py` 里有一条测试**直接从 `app.js` 里把 `TABS` 抠出来比**，
而不是在测试里硬编第三份清单（那样只是在重复服务端那份常量）。

`popstate` 的处理里有一条容易漏的：响应后退/前进时**不能再 pushState**，否则后退键会在两个
分区之间来回弹，永远退不出这个页面。

---

## 25. 重复上传不该是死胡同

### 25.1 两道墙，各拦一半

从手机相册第二次挑同一张照片，会连着撞两次：

1. `POST /v1/upload?name=IMG_2034.jpg` → 同名文件已经在落地目录里；
2. `POST /v1/photo {refPath}` → `already_ingested`。

第一道原来是 409 死胡同，第二道只回一句「这张参考图已经入库了」。两者都**判断正确**
（重复确实要拦），但都没回答用户此刻真正的问题：**那张照片现在配的是哪段视频。**

### 25.2 第一道墙：先看内容一不一样

同名**同内容** = 这个文件已经在服务端了，直接复用那条路径（200 + `reused: true`）。
同名**不同内容** = 仍然 409，但带上 `suggestedName`（`a.jpg` → `a-2.jpg`）。

后者必须还是拒绝：直接覆盖会悄悄换掉别人的素材，而已入库的照片指着那条路径。

比内容要先落到临时文件再比哈希（体可能是几百 MB 的视频），临时名带 pid + 线程 id，
两个管理员同时传同名文件不会互相踩，而且成功与失败两条路都在 `finally` 里删掉它。

### 25.3 第二道墙：新接口 `GET /v1/admin/lookup?path=`

回答「这个 NAS 路径在库里是什么身份」。**两个字段的基数刻意不一样**，因为那正是界面上
能给出什么动作的依据：

| 字段 | 基数 | 为什么 |
|---|---|---|
| `photo` | 最多一个 | 一张照片只有一个参考图 |
| `usedByPhotos` | 列表 | 一段视频可以被多张照片配（一段迎宾视频配给几十张是正常用法） |

这个不对称决定了两种「重复」的性质完全不同：

- 重复的**照片**是真冲突 —— 库里那张已经占了这个参考图，只能去改它（换视频）。
- 重复的**视频**根本不是冲突 —— 直接配给新照片就行，原来用它的那些照片一点不受影响。

有一个容易漏的坑：`photos_referencing_asset` 查的是三列（ref / video / playable），
不把「它是自己的参考图」那条排掉的话，**一张照片会出现在自己的 `usedByPhotos` 里**。

### 25.4 App 侧：`DuplicatePlan` 是纯逻辑

分支比看起来多：这张照片入过库没有 × 它现在配了视频没有 × 用户这次挑了视频没有 =
八种组合，而其中几种的正确说法完全相反：

| 库里那张 | 这次挑了视频 | 说什么 / 能做什么 |
|---|---|---|
| 有视频 | 没挑 | 说清现状，没有动作 |
| 没视频 | 没挑 | **指出它没配视频**（扫到不会播），并说去哪配 |
| 没视频 | 挑了 | 「补上」—— 不会覆盖任何东西 |
| 有别的视频 | 挑了 | 「替换」—— 要说清旧的那段不再关联 |
| 有同一段视频 | 挑了同一段 | **什么都不用做**（「我忘了传过没有」的典型情形） |

写在 Composable 里的唯一验证方式是把八种在手机上各走一遍，而最需要说清的正是最后两行。
所以它是 `app/.../DuplicatePlan.kt`，纯函数，11 条测试盯着文案里那几个必须出现的词
（「替换」「不受影响」「不删」「不会覆盖」）—— 那些词不是修饰，是用户敢不敢点那个按钮的
全部依据。

---

## 26. 素材挂载点：本机绝对路径 + WebDAV

### 26.1 为什么只有这两种

「常用文件传输协议」里能用 stdlib 做完的只有 WebDAV（`urllib` + `xml.etree`）和 FTP
（`ftplib`）。SMB 要 `smbprotocol`、SFTP 要 `paramiko`（带 `cryptography`，镜像 +约 40MB），
两者都会打破这个项目对依赖的一贯姿态（见 pyproject 里 onnxruntime 那 15 行，以及第 19 节
为什么手写 xlsx）。

FTP 没做：家用 NAS 上已经很少用，而且明文传口令。

**本机绝对路径覆盖的比听起来多**：SMB/NFS 在宿主机 `mount` 好、compose 里挂进容器之后，
在容器里就是一个普通路径。也就是说「我的 NAS 是 SMB」这个需求，通常由 local 类型 + 一条
compose 挂载解决，不需要一个 SMB 客户端。

### 26.2 local 与 webdav 的处理是两条路

| | local | webdav |
|---|---|---|
| 进白名单（`Roots`） | **是** | 不 |
| 浏览 | `fsbrowser.list_dir` | PROPFIND |
| 入库前 | **不拷贝**，直接读 | 先下载到落地目录 |

local 不拷贝是刻意的：文件本来就在服务端的文件系统上，拷一份只是白占一倍磁盘 —— 而这个
部署形态下磁盘就是 NAS 的磁盘。

两种的**浏览响应形状完全一样**（`{path, parent, entries:[{name,isDir,kind,bytes}]}`），
所以管理台上一个文件浏览器就能同时用在两者上。形状不同的话那边要写两套渲染，而它们看起来
该是一样的。

### 26.3 白名单是热重建的，而这件事有两个坑

`_rebuild_roots()` = `PHOTOAR_ROOTS` **叠加**启用中的 local 挂载点，整体替换 `self.roots`。

**坑一：不能只用挂载点。** 第一版差点写成那样，后果是删一个挂载点会把环境变量给的根一起
弄丢 —— 也就是整个库突然全部读不到。所以 `_env_roots` 单独留一份原始的。

**坑二：不能往现有的 `Roots` 里加。** `Roots` 构造时按路径长度降序排（嵌套根时 name 才是
确定的那个），原地追加会让那个排序失效。

名字冲突时**环境变量赢**：它是 compose 里写着的、部署时定下的东西；让运行期加的挂载点覆盖
它会让「改一下 compose 重启」变得不可预测。冲突的挂载点被跳过并记一行日志。

### 26.4 local 挂载点扩大了服务端愿意读的范围

一个管理员把 `/` 加进来，就能靠 `/v1/fs/thumb` 看到容器里任何文件。这是**接受**的：admin
本来就是最高权限（他能改配置、能建管理员、能重建词表），而容器只挂了它该看到的那几个卷。

但仍然：要求路径**已经存在且是目录**（自动创建会把一个打错的路径变成一个空目录，然后
「我的照片怎么一张都没有」），并且每次变动都在日志里记一行 —— 让「谁什么时候加了哪个根」
有据可查。

### 26.5 口令存明文

不好，但可选项更糟：加密要有密钥，而密钥只能放在同一台机器的同一个目录里（没有 KMS、
没有 TPM 可用），那只是把明文换成「明文 + 一层需要维护的仪式」。真正的边界是 `data/`
目录的文件权限与容器的 UID。

所以如实存、在文档里说清、并且**不通过任何接口回显**（`_mount_json` 只给一个
`hasPassword` 布尔）。有一条测试专门盯着响应体里不出现口令原文。

另外：`PATCH` 时 `password` 缺省 = **不动**，不是清空。两者必须分得开 —— 管理台的口令框
是空的（因为服务端不回显），混成一个的话改一次名字就会把口令抹掉，而下一次浏览才会失败。

### 26.6 WebDAV 客户端里那几个真实的坑

`tests/server/test_webdav.py` 的开头列了四条，都是照真实服务端的形状写的：

1. **一个 `<response>` 里有多个 `<propstat>`**（Nextcloud 就是），而 **404 那组可能排在
   前面**。只取第一个会读到空 prop → 所有条目都变成「不是目录、没有大小」→ 目录点不进去。
2. 命名空间前缀不固定（`D:` / `d:` / 默认命名空间）。
3. `displayname` 可能根本不返回，得从 href 最后一段解码。
4. **目录自己也在响应里**（Depth:1 的语义包含自身），不跳掉就有一个指向自己的条目。

还有一条是测试逼出来的：**「解得开 XML」不等于「是 WebDAV 响应」**。一个 HTML 登录页
（`<html><body>…</body></html>`）本身就是合法 XML，解析会成功，然后因为里面没有
`{DAV:}response` 而返回空列表 —— 于是「地址指到了 NAS 的登录页」在界面上显示成「这个目录
是空的」，而人会去 WebDAV 那边找自己的照片为什么不见了。所以加了根元素必须是
`{DAV:}multistatus` 的检查。

href 与 name 必须分开存：href 里的中文是百分号编码的，拿它当显示名会让整个列表变成一串
`%E5%A9%9A%E7%A4%BC`；而拿 name 去拼路径在名字含斜杠或服务端做过重写时是错的。

---

## 27. 「普通用户只能扫出结果」：这一条本来就成立，现在被钉住了

服务端一直是这样：`/v1/upload`、`/v1/photo`（入库）、`/v1/photo/*/ref`、`/v1/photo/*/video`、
`/v1/fs/*`、`/v1/history`、`/v1/admin/*` 全是 admin only。App 那边访客也只有「扫一扫 / 设置」
两个页签（第 17 节）。

所以这一轮没有「实现」什么，而是**把它变成一条会自己报警的测试**：
`test_访客能做的只有登录_识别_看自己被授权的那些` 枚举一批路径，断言「能通的正好是这张
白名单」。

这比逐个接口写断言强的地方在于：以后新加一个接口，如果忘了给它 `_require_admin`，这条测试
会立刻红。而逐个断言的写法对新接口一无所知 —— 它只会一直绿着。

访客的白名单是：`ping`、`auth/*`、`recognize` 与 `recognize/features`、`model/xfeat`、
`targets/*`、`photos` 与 `photo/<id>` 及其派生（thumb / ref / imgdb / media）、
`asset/*/stream`。这些全都过 `photo_filter` 或 `_photo_or_404`，只给他被授权的那些 ——
也就是「扫出结果，然后看到那张照片、播那段视频」，一个字不多。

---

## 28. 入库要 110 秒的真因：把 12MP 原图白 warp 了 6 次

### 28.1 症状与错误的假设

用户报的是：手机传完之后「一直卡在服务器在特征提取，然后报错服务器没回话（超时）」，
并推测「手机比 N5095 强，特征提取放端上会更快」。

那个推测的**前提是对的**（手机 SoC 确实比 N5095 强），但**归因错了**。逐阶段实测
（3 CPU 容器，一张 3000×4000 的真实手机照片）：

```
解码                  56 ms
arcoreimg 评分       185 ms
ORB 提特征            18 ms   ← 「特征提取」只占这么点
合成 6 个扰动     110842 ms   ← 99.6% 的时间在这
扰动样本提特征        99 ms
自匹配分 RANSAC        5 ms
arcoreimg 建库        95 ms
```

特征提取是 **18 毫秒**。把它挪到手机上最多省下 18 毫秒。

### 28.2 那 110 秒是纯浪费

`synth.apply` 对每个样本做：透视 warp、高斯模糊、**转 float32**（12MP × 3 通道 × 4 字节
= 一次 144 MB 分配）、逐通道乘、clip、转回 uint8、可选眩光、**JPEG 编码 + 解码**。
六七遍全图操作加一次 JPEG 往返，×6 个样本。

而下游的 `features.extract` **本来就会**把图缩到 `LONG_EDGE=640` 再提特征。也就是说
全分辨率 warp 出来的像素，97% 在下一步就被扔掉了。

样例素材是 708×468（0.3 MP），所以这个问题一直没露头；手机照片是 12 MP，40 倍像素。

### 28.3 上限定在 1280，以及为什么可以不做数据迁移

实测扫了一遍分辨率：

| 上限 | 12MP 手机照片 | 1.7MP | 0.3MP |
|---|---|---|---|
| 全分辨率 | self=154 / **111 s** | 109 / 2.3 s | 96 / 140 ms |
| 1920 | 150 (-4) / 6.4 s | — | — |
| **1280** | 144 (-10) / **1.4 s** | 108 (**-1**) / 1.0 s | — |
| 960 | 145 (-9) / 355 ms | 103 (-6) | — |
| 640 | 130 (**-24**) / 127 ms | 105 (-4) | 95 (-1) |

选 1280 的三个理由：640 会让分数掉 15.6%（太多），960 与 1280 已在噪声内（145 vs 144），
而 1280 有一个现成的锚 —— 它就是 App 喂给 ARCore 的 CPU 图像长边（`Frames.LONG_EDGE`），
于是自匹配分是在**相机真正给出的那个尺度**上量的。

`self_score` 会存库，而去重判据 `min(s_new, s_exist) < ratio * m` 要拿新旧两代比，所以
换算法本该配一次全库重算。**这里可以不重算**，理由是实测的：已经在库里的照片长边基本都
≤1600，在 1280 上限下分数只差 0～1（demo-a 1600px：109 → 108；wedding-01 708px：完全
不受影响）。真正会变的只有 12MP 手机照片，而它们在改之前**根本入不了库**（超时）。
而且变化方向是分数变低 = 去重更保守 = 宁可多报冲突，那是安全的方向。

### 28.4 真机口径的结果

改完之后，一张真实的 10.2 MP 手机照片跑完**整条流水线**（质量分 + 特征 + 合成 + 自匹配分
+ 去重 + imgdb + 缩略图）是 **3.5 秒**。视频转码另算（7.9 秒的 1080p 软编 2.7 秒）。

### 28.5 为什么最终没做「端上提特征入库」

即使抛开「只省 18 毫秒」，这条路还有两道硬墙：

1. **Android 侧没有 OpenCV**，只有 ONNX Runtime。也就是说端上只能算 XFeat，**算不了 ORB**
   —— 而库的默认后端是 ORB。要走这条路得先把整个库迁到 XFeat。
2. **`arcoreimg` 是闭源的 Linux 二进制**。质量分与 `.imgdb`（端上离线 AR 跟踪要用的目标库）
   都只能在服务端产出，这一步无论如何搬不走。

所以「管理员可以配置」落在了真正有用的地方：`ingest.synth_long_edge` 是一个热配置项，
默认 1280，想更快可以调到 960（355 ms），想更接近旧口径可以调到 1920。字段说明里带着
上面那张实测表。

---

## 29. 上传之前就告诉他重复了

原来的顺序是「传完 20 MB → 服务端说已存在」。手机上那是几十秒的等待换来一句「白等了」。

`POST /v1/upload/check {name, sha256, bytes}` 让客户端**先问**。请求几百字节，哈希在本地
算（一张 2.7 MB 的手机照片约 30 ms）。两条独立判断，都要报，因为下一步动作不同：

- **按内容**（sha256）：这份内容库里已经有了 → 直接说出它是哪个文件、在库里是什么身份。
  这一条比按名字有用得多 —— 相册第二次导出同一张照片，**文件名可能变了，内容不会变**。
- **按名字**：落地目录里有同名文件 → 内容一样就是「可以复用」（一个字节都不用传），
  不一样就给建议名。

App 侧：`uploadOne` 先算哈希问一次，`reusablePath` 非空就整个跳过上传；撞名但内容不同时
自动用服务端给的建议名，不让用户去改相册里的文件名。**校验失败时继续传**而不是中止 ——
那一步只是优化，服务端在真正落地时还会再挡一道；因为一次可选的优化让整条上传路径失败是错的。

`sha256` 是可选参数：老版本 App 不会算哈希，那时只做按名字那一半。「少一半信息」比
「整个接口用不了」好。

身份那部分（`_identity_of_asset`）与 `/v1/admin/lookup` **共用一个函数**：用户在上传前
看到「这是某张照片的参考图」，传完之后在别处看到不一样的说法，比不说更糟。有一条测试
盯着两处输出相等。

---

## 30. 「我传上去了，但哪儿都找不到」

手机传上来的文件先落到 `PHOTOAR_UPLOAD_DIR`，然后才入库。中间任何一步断了（入库超时、
质量分不过、近重复被拒、或者人挑完视频就退出了），那个文件就躺在那儿 —— 而**管理台上
任何一处都看不到它**：照片列表只列已入库的，挂载点浏览器要人自己去翻目录。

`GET /v1/admin/inbox` 列出落地目录里**还没有被任何 asset 用起来**的图片与视频。管理台的
照片页把它画在列表下面（同一个问题的另一半），图片可以直接入库、视频可以挑几张照片配上去。

只看一层不递归（落地目录是平的，`/v1/upload` 只允许纯文件名），并且跳过既不是图也不是
视频的东西（`.upload-xxx` 临时文件、`.DS_Store`）—— 列出来只是噪声，用户对它们无事可做。

### 30.1 管理台从不自动刷新，而这解释了另一半困惑

用户报「手机端添加完照片、web 的照片列表里没有更新出来」。查下来数据**是在的** ——
问题是这一页只在第一次进入时取数据，之后要手点「刷新」，而人没有理由知道这一点：他会
以为是手机那边没成功。

现在照片页在**回到页面时**（`visibilitychange`，切回标签页 / 从别的应用切回浏览器）自动
重取。这正对应他的心理时刻：刚在手机上做完一件事，转回电脑来看结果。

只刷这一页、只在这个时机刷：`config` 与 `grants` 页上有**正在编辑的状态**（勾了一半的
授权、改了没保存的字段），定时轮询会把它刷掉；而照片页没有编辑态。刷新时不显示骨架屏 ——
把已经画好的表格换成一排灰条会让人以为出问题了。

---

## 31. WebView 里的 `<input type="file">` 默认什么都不做

用户报「直接在手机嵌套 web 的管理页面是不行的，很多弹窗点不开来，比如添加照片、选文件」。

「选文件」那一半是一个确定的缺陷：**WebView 不像浏览器那样自带文件选择器**。宿主 App 不
实现 `WebChromeClient.onShowFileChooser` 的话，点下去连一点动静都没有，也不报错。管理台
「批量」页那个「选文件…」就是这么哑掉的。现在接上了系统的文件选择器。

有一个容易漏的约束：**每个 callback 必须被调用一次**，哪怕用户取消了（那时给 `null`）。
漏掉的话那个 `<input>` 从此再也打不开 —— 它一直等着上一次的结果。

### 31.1 但也加了逃生口，因为这不是全部

即使文件选择器修好了，管理台是按鼠标和大屏设计的，塞进手机 WebView 之后有些东西天生不
好用（多层弹窗、很宽的表格要横向滚动）。所以「管理」页上现在是两个按钮：「在 App 里打开」
与**「在浏览器里打开」**。

系统浏览器有地址栏、有完整的文件选择器、有密码管理器 —— 遇到 WebView 里点不动的东西时那
是唯一的出路，而没有这个按钮，用户只能去电脑上做。代价是浏览器里要**再登一次**（那是另一
个应用，拿不到 App 的会话 cookie），文案里写清了。

---

## 32. 「认出来了，但没在画面里找到」

### 32.1 根因就写在我们自己的注释里

用户报的是这句提示反复出现，并指出真实场景：**一只手拿着照片（手指压在边缘）、有覆膜反光、
光线不一**。

而根因在 `ArSessionHolder.loadTargetFromBitmap` 的注释里已经写着了：

> 不带宽度是它专门支持的用法 —— 自己从 SLAM 量出物理尺寸，**代价是要用户稍微动一下手机
> 才收敛**

而库里**每一张照片的打印宽度都是 0（未知）** —— 那是第 5 轮我把 App 上传默认成 0 的结果。
于是：ARCore 必须靠视差自己量尺度 → 需要移动手机 → 一个举着手机对准照片的人不会自发这么做
→ `trackingState` 一直是 PAUSED → 4 秒超时 → 报错并**丢掉整次命中**。

而那句提示说的是「**再对准一下**」—— 鼓励他拿得更稳，正好是反方向。

### 32.2 四处改动

**A. 认出来了就一定要播。** 超时之后不再 `resetTarget()` 回到扫描，而是**退到全屏播放**。
识别是对的，用户要的那段视频就在手上，只是没能贴到照片上；AR 是加分项，不该因为它失败就
连视频一起不给。原来那条路的结果是「扫到了、认出来了、什么都没发生」，而再扫一遍也一样 ——
失败原因不是这一帧没对准。

**B. 提示改成能照着做的。** 1.5 秒时给：「轻轻左右晃一下手机（它在量照片有多大）；手指别
压住边缘，避开反光」。三句都对应用户描述的那三件事，而且必须在超时**之前**给、留时间让他
照着做。

**C. 两个时间常数放宽。**
- `TARGET_FIND_TIMEOUT_MS` 4s → 8s。原来那 4 秒的理由是「放弃的代价很低（400ms 后再试）」，
  **那个理由错在前提上**：再试不会更容易成功。
- `ArRenderer.COAST_MS` 2s → 3s。原来按「斜视空档」（几十到几百毫秒）定的，而手指遮挡 +
  反光带来的空档是好几百毫秒到一两秒，2 秒窗口会反复到点，表现是视频每隔几秒暂停一下。

**D. 打印宽度输入回来了（可选，带相纸预设）。**

### 32.3 D 是一次「旧理由已经失效」的纠正

第 5 轮去掉这个输入框，理由是「一个**猜的**宽度比不填更糟」—— 那时四边形大小按申报宽度算，
填错百分之几视频就大百分之几。

但**同一轮之后**四边形尺寸改成取 ARCore 自己量的 `extentX` 了（`Geometry.quadSize` 优先用
它）。也就是说那个反对理由**已经不成立**：填一个稍微不准的宽度不再影响贴合精度。

而它还剩下的那个作用恰恰是关键的：**帮 ARCore 检测**。填了宽度，ARCore 一认出图案就能直接
给位姿，不需要用户晃手机 —— 也就是不会走到 32.1 那条链上。

所以现在是七个预设标签（不知道 / 6寸横竖 / 5寸横竖 / A4 横竖），点一下就好。默认仍然是
「不知道」，但那一条的提示明写代价（「扫的时候要轻轻晃一下手机才贴得上」）。

用一排标签而不是下拉框：下拉框会把选项藏起来，于是大多数人根本不知道有得选，也就永远走那条
要晃手机的路。横竖两个方向都列出来，是因为同一张 6 寸照片横放宽 152、竖放宽 102 —— 让我们
去猜方向的话，猜错就是宽高对调。

管理台的「添加照片」也问同一个问题，两侧的预设数字有一致性检查（`PrintSizeTest` 里那几条
盯着毫米数与「横放一定比竖放宽」）。

### 32.4 全屏退路为什么画在 GL 里，而不是换一套 View

全屏那条路（`ScanRuntime.attachFallback`）用的是两个 `SurfaceView`，AR 用的是
`GLSurfaceView`，两者在 Activity 建立时就定了。中途切换要动 Surface 的生命周期，而那是在
视频**正在播**的时候动 —— 画面会闪、播放位置可能跳。

而视频纹理本来就已经在那个 GL 上下文里流着。所以做法是把那块四边形设成 2×2（NDC 的整个可见
范围）、三个矩阵全给单位阵，裁切比例换成**视口**的 —— 一组矩阵的事，不动任何 Surface。
相机背景照旧画在下面，所以退成全屏之后画面仍然是「取景 + 一块视频」，而不是突然黑底
（后者会让人以为崩了）。

### 32.5 两个「本次命中」的属性

`ScanController.arGaveUp` 与 `ArRenderer.fullscreenVideo` 都必须在退出目标 / 新命中时清掉。
做成装机属性（比如把 `arAvailable` 改成 var）的话，**一次贴合失败会让这台手机之后永远不再
尝试 AR** —— 而下一张照片的 AR 可能完全正常。有测试盯着这一条。

另外：退到全屏之后 ARCore 又跟上了也**不打断**正在播的视频。打断很诱人（「AR 终于成了」），
但那会在用户已经在看的视频上突然换渲染路径。他此刻要的是看完这段。

### 32.6 这次刻意超出了 10 秒的承诺

`TARGET_FIND_TIMEOUT_MS`（8s）现在比 `HIT_TO_PLAY_BUDGET_MS`（6s）长，最坏情况出画会晚到
约 12 秒，超出「对准到播放 10 秒」那个承诺。

这是刻意的取舍：原来 4 秒到点把整次命中丢掉，用户在 10 秒内得到的是**什么都没有**；现在
8 秒到点退成全屏，他拿到的是那段视频。**「晚 2 秒看到」比「按时看不到」好。**

那条预算的提示不会在这段里误报 —— `tickPlayBudget` 第二行 `if (!everTracked) return` 挡住了，
这一段由 `TRACKING_HELP` 负责，那句话更准（卡住的不是视频）。

---

## 33. 第 10 轮：撤掉全屏退路、贴合放宽、把「卡在哪」变成能看的东西

用户四条：① 去掉贴合 / 全屏播那套逻辑；② 调试模式在左上角多打日志，看看卡在哪、为什么贴不
上；③ 其实不需要完全贴合 —— 视频和照片的比例本来就可能不一致，差不多就行，只要保证在**一个
平面**上；④ 设置页和相机都很卡顿，为什么。

### 33.1 全屏退路撤掉了 —— 它是上一轮加的，加错在哪

§32.1 那条退路（贴合 8 秒没成 → 全屏播）**保证了「有视频看」，代价是把「为什么没贴上」盖住
了**：屏幕上出现一段全屏视频，与「AR 完全正常」在观感上无从区分。第 9 轮我给了一行 ARCore
诊断想解决这个，但那一行只覆盖最后一步，而这条链有七步。

于是这一档在三轮里变了三次，三条理由都留着：

| 版本 | 贴不上时的动作 | 为什么废掉 |
|---|---|---|
| 最早 | 4 秒回扫描 | 扫到了、认出来了、什么都没发生；再扫一遍也一样（失败原因不是这一帧没对准） |
| §32 | 8 秒退全屏播 | 有视频看，但贴合失败被盖住，两轮排查只能靠外部现象反推 |
| **现在** | **一直等** | 出口是用户按「退出」；同时给可执行提示 + 调试日志 |

删掉的东西：`NoticeKind.AR_FALLBACK_FULLSCREEN`、`ScanEvent.ArFallback`、
`ScanController.arGaveUp`、`ArRenderer.fullscreenVideo` / `drawFullscreenVideo` /
`FULLSCREEN_QUAD`、`TARGET_FIND_TIMEOUT_MS`。§32.4 和 §32.5 记的是这套东西的实现细节，
连同它们一起作废 —— 留着那两节是为了记住「为什么曾经这么做」。

**没有 ARCore 的机型那条全屏路一点没动**（`ScanRuntime.attachFallback`）。那是「这台机器
根本不支持 AR」，和「支持但这次没贴上」是两件事，混掉的话前者会没视频看。

已经贴上过之后丢失跟踪那一档（`LOST_GIVEUP_MS` 10s）也没动：那时候必须放手，否则下一张
照片永远扫不进来。有测试把这两段的差别钉住。

### 33.2 「差不多就行」= 停止裁切，改成把视频装进去

原来是 `Geometry.fillCropUv`：把视频居中裁掉一部分，正好填满照片的矩形，理由写的是「照片
区域里出现黑边看起来就是坏了」。

**那个理由把一件不存在的事当成了前提 —— 这里没有黑边。** 视频四边形是贴在相机画面上的一块
半透明贴图，它比照片小的时候露出来的是**照片本身**。

而裁切的代价是实打实的：竖屏拍的视频配横着的 6 寸照片（16:9 对 3:2），裁到填满要切掉左右
各三成 —— 人像视频被切掉的正好是人。

现在是 `Geometry.videoQuad`：等比缩放到能放进照片矩形的最大尺寸，居中，纹理整张用
（`FULL_UV`）。视频完整、不变形，两条边和照片对齐、另两条留出一点照片的底，而它仍然贴在
照片那个平面上 —— 那是这件事唯一的硬要求。测试里有一条遍历九种比例，断言「永远装得进」
且「比例永远是视频的」（不变形是不能违反的那一条：人眼对人脸比例极其敏感）。

### 33.3 调试日志：从一行变成左上角一块

`DiagLog`（纯 Kotlin，16 行滚动窗口）+ 各层打点。喂进来的东西：

- 状态迁移（`StateChanged`）—— 这是骨架，其余每一行都要靠「当时在哪个状态」才读得懂
- 每条提示（界面上只显示最新一句，**被下一句盖掉的往往才是关键的**）
- 抽帧 / 识别：编号、包大小、走的哪条路、往返毫秒、命中还是没命中（没命中带服务端的 reason）
- 装目标：imgdb 还是缩略图、多少字节、失败原因
- 播放器：prepare（只记本地/网络，不记完整 URL —— 这块日志是要被截图发出来的）、就绪、视频尺寸
- 贴上了：申报宽度 vs ARCore 量到的
- 贴不上：ARCore 的原话，每秒一次（`ArSessionHolder.diagnose`）
- **GL 帧耗时**：fps / 均 / 峰 / 卡帧数，抓帧单独一档

**连续相同的行折叠成 `×N`，并把时间戳更新成最后一次。** 这不是优化：贴不上时 ARCore 那行
每秒一条，不折叠的话十几行的窗口两秒就被它填满，把「装目标失败」那种只出现一次的关键行顶
出去 —— 而那一行恰恰是唯一有信息量的。有一条测试专门盯这个。

关着的时候**一个字符串都不拼**（`diag { }` 是 inline + lambda，`ArRenderer.diagnostics` 是
`@Volatile` 布尔）。理由不是省内存：打点密度是每 400ms 一次识别 + 每帧一次渲染，而后者在 GL
线程上，每 16.7ms 就要交一帧。

`onDrawFrame` 拆成「计时」和 `drawFrame`（正事）两个方法：正事里有六处提前 return，而计时
要每条路都算上 —— 恰恰是「早早 return 的那些帧」能证明慢的不是渲染。用 `try/finally` 而不是
在每个 return 前补一句：**补漏一处那一路就从统计里消失，而消失的方向永远是「看起来更快」。**

### 33.4 卡顿：一条确认的、一条量过否掉的、其余交给仪表

这一节刻意分开「已经证实的」和「还在猜的」。

**已确认：装的是 debuggable 包。**
```
$ adb shell dumpsys package app.photoar | grep pkgFlags
    pkgFlags=[ DEBUGGABLE HAS_CODE ALLOW_CLEAR_USER_DATA ]
```
这一条能解释**设置页也卡** —— 那一页没有相机、没有 AR、没有轮询，屏幕上就是几张 Compose
卡片，它慢不可能慢在业务代码上。debuggable 会关掉 ART 的一部分优化，而 Compose 在 debug 下
本来就比 release 慢得多（Google 自己的文档要求性能一律在 release 上量）。

**所以这一轮出的是 release 包**：151.4 MB（`-Pphotoar.deviceAbiOnly`，去掉两套模拟器 ABI），
`pkgFlags` 里 DEBUGGABLE 已经没了。`release` 沿用 debug 签名（`app/build.gradle.kts`
早就这么配的），所以真机照样装得上、原地升级。**调试日志不受影响** —— 那个开关读的是
SharedPreferences，不是 `BuildConfig.DEBUG`（§9 记过为什么）。

出 release 包顺手炸出一个只在 release 才发生的构建失败：
```
Zip file ... already contains entry 'assets/dexopt/baseline.prof'
```
ARCore 运行时 APK 自己带了一份 `assets/dexopt/baseline.prof`，而 AGP 在 release 变体里往
**同一个路径**生成我们自己的那一份。`ArcoreUnpackTask` 现在把它跳过：那份画像描述的是
ARCore 自己 dex 里的热方法，而我们是用 `DexClassLoader` 在运行期加载那个 dex 的，
`assets/dexopt` 那条路对它本来就不适用。debug 变体不生成这个文件，所以这个冲突要到出第一个
release 包才会暴露。

**量过之后否掉的：`Frames.toNv21` 不是卡顿的成因。**

它原来是三层循环里逐字节 `ByteBuffer.get(index)`，1280×960 就是 184 万次带边界检查的调用 ——
看起来很像元凶。**先量再改**：

```
逐字节  883 µs/帧
批量    392 µs/帧      ← 每行一次 get(byte[],off,len)，字节输出完全一致
```

（桌面 JVM；HotSpot 把逐字节那版优化得相当好，ART 上差距会更大。）0.9ms、每 400ms 一次，
摊到帧上看不见 —— **它不是卡顿的成因**。改了是因为那是白扔的 CPU，顺手收掉，不是因为它能
解决问题。这一步的价值在于：如果不量就改，接下来会把「卡顿已修」写进报告，而下一轮又要从头
开始。

新增的唯一失效模式是「最后一个色度行没有尾部补齐」（按 rowStride 整行批量读会
`BufferUnderflowException`），有测试钉住。三个平面各 `duplicate()` 一份再读，所以调用方的
buffer position 不被动 —— 那条测试本来就在。

**其余交给仪表。** 剩下的候选彼此不相干、修法也不相干：GL 线程自己慢 / 相机档位挑到 60fps
而 SLAM 跟不上 / 端上 ONNX 推理抢 CPU / 整机降频。`FrameStats` 报出来的那一行能直接分开
第一条与其余（均 6ms 峰 12ms = 渲染健康，卡在别处；均 30ms = 确实在这里），而相机档位与
帧率在同一块日志的第一行。**不猜，等那一行。**

### 33.5 一个哨兵的教训

`FrameStats` 第一版拿 `windowStartNs == 0L` 当「还没开始」。`System.nanoTime()` 的零点没有
任何保证，它可以恰好返回 0 —— 于是那一帧不断把窗口起点往后推，报得越来越晚，**而报出来的
fps 偏高**（分母被截短）。改成单独一个 `started` 布尔。

是我自己写的四条测试炸出来的（它们用 `nowNs = 0` 起头），而那正是这类哨兵的典型失效方式：
在生产环境里它只是让数字偏乐观，不会报错。

---

## 34. 第 11 轮：那张海报为什么扫不出来（根因）、贴合的精确定义、CI、像素风

用户四条：① 那张照片还是识别不出来，仔细排查；顺便把「差不多」说清楚了 —— **至少一个
维度贴满照片，按视频完整最大化为准**；② 跟随帧率再高一倍；③ 整个 App 改像素风、图标
原创；④ 参照 explore_journal 补全 CI，并保证不会因为包名/签名撞车装不上。

### 34.1 识别不出来的根因：同一张海报在库里有两份

这一条是本轮唯一真正的 bug，而它**不在识别算法里**。

排查顺序（每一步都是量出来的，不是推的）：

| 查了什么 | 结果 | 结论 |
|---|---|---|
| `bench/simcam.py` 离线跑那张海报 | 内点 123~235，门槛 40，`fill` 低到 0.40 仍全过 | 素材没问题 |
| 同一批帧走**真实 HTTP** | 9/9 命中 | 服务端、库、粗排都没问题 |
| 模拟 640×480 的 CPU 图像 | 仍然 131~213 | 分辨率不是瓶颈 |
| 翻服务端的 `recognize_log` | **941 条真机帧只命中 44 条**，其中 897 条内点 160~229 | 阈值不是瓶颈 —— 是判定 |
| 对比库里两张新照片的缩略图 | **同一张 Bingo 海报，拍了两次，两次都入了库** | 找到了 |

机理正是 `library.conflicts` 自己 docstring 上预言的那句：两份近重复在识别时互相触发
`RATIO=1.5` 判 `ambiguous`，**两份都永久漏检**，而用户看到的现象是「识别器坏了」。
离线复现（对两张各自扰动出第三视角）：top1/top2 = 1.13 / 1.42 / 1.43 / 1.44 / 1.56 /
1.85 —— 6 次里 4 次卡在 1.5 那条线下面。

### 34.2 去重闸门为什么没拦住：`m` 量错了口径

判据是 `m >= 25 且 min(s_new, s_exist) < ratio * m`。那一对图的实测：

```
入库口径 m =  63     ← 闸门用的（两张都按 300 特征 / 640px 提）
查询口径 m = 123     ← 识别时真正发生的（查询边 4000 特征 / 1280px）
两张的 min(self_score) = 149
原判据：149 >= 1.5×63  = 94.5  → 放行  ← 就是这次放进来的原因
改后：  149 <  1.5×123 = 184.5 → 拦下
```

所以修的不是「把闸门调紧」，是**让 `m` 量它名字所指的那个东西**：识别永远是「查询侧
特征 vs 库里存的入库侧参考图」，那才是 `m` 该复现的方向。实现上是 `conflicts` 多收一个
可选的 `query_features`，把这个方向加进 `max`。

`self_score` **保持入库口径不变**（它是存进 catalog 的历史值，换口径要连全库一起重算，
那是数据迁移）。它因此是 `i1` 的低估，而低估分子只会让闸门更严 —— 对一个失效方式是
「两张永久扫不出来」的闸门，这是安全的方向。

误拦风险：同一批实测里 5 对**内容无关**的照片，`m` 从 5~7 只涨到 7~8（噪声量级），而
`1.5×8 = 12` 远低于它们的 self_score（104~107）—— 真负样本这边有 15 倍余量。
⚠️ **那 5058 张的语料已经不在盘上了**，所以这条余量只在 5 对负样本上复核过，
不是原来那个规模。

### 34.3 两个诊断缺口，都是「失败完全看不见」

**一、`recognize_log` 只有 `inliers`。** 941 条记录里 897 条内点 160~229 却判未命中，
而光凭这张表分不出挡住它们的是 `weak`（det 越界，要改取景）还是 `ambiguous`（库里有
重复，要清库）—— 那两件事的修法毫不相干。现在补了 `reason` / `runner_up` / `topk_json`
三列（`topk_json` 那一列**早就存在，但从来没被写过**），并在历史接口和 App 的历史页
暴露出来；`ambiguous` 那一类单独标红，因为其余未命中是「这一帧没拍好」（下一帧就好了），
而它是「不处理的话每一帧都这样」。

**二、没有删除照片的路。** 闸门现在能拦住新的重复，但**已经进去的那一对拦不住** ——
而在此之前解开它的唯一办法是重建整个库。所以加了 `DELETE /v1/photo/{id}`。

删除的实现是**墓碑**，不是真删：slot 是 `desc.bin`/`words.bin` 里的下标，摘掉一项就要
把后面每一条往前挪，而那会让 photo_id ↔ slot 整体平移 —— 错位不报错，命中之后播的是
别人的视频。所以 `slots.json` 里那一格换成空串，条数不变、下标不动，读侧多一句
「墓碑跳过」，重建索引时把那一格的词序列清空（否则一张已经不存在的照片会继续压低
它含有的每一个词的 idf）。代价是磁盘不回收 —— 一格约 10KB，删一千张才 10MB。

### 34.4 「差不多」的精确定义

用户的原话：「至少有一个维度（长或者宽）是贴合图片的，按视频最大化完整显示为准」。

上一轮实现的 `Geometry.videoQuad` **已经就是这条规则** —— 等比缩放到能放进照片矩形的
最大尺寸，于是恰好有一条边严丝合缝、另一条不超出、视频完整不变形。这一轮只是把这三条
性质写成了一条遍历 8 种比例的测试（`恰好有一个维度和照片贴满，另一个不超出`），
免得以后有人"顺手"把它改成居中缩小或裁切填满。

### 34.5 跟随帧率：能翻的那一半翻了，另一半翻不了

**能翻的**：`Frames.pickCameraOption` 原来是「先取达标尺寸里**最小**的那个，再在**同尺寸**
内取最高帧率」。那一版在一类很常见的机型上白丢一半帧率 —— 1280×960 只有 30fps，而
1920×1440 有 60fps，两个尺寸都达标，但规则先把尺寸锁死了。改成「达标 → 最高帧率 →
同帧率里最小的尺寸」。尺寸仍然是硬闸门（长边 ≥ 1280），所以不会为了帧率牺牲识别。

**翻不了的**：ARCore 的 `CameraConfig.TargetFps` 枚举**只有 30 和 60**，60 已经是它给的
上限。想再翻一倍到 120 得等它加那个值。而「渲染得更快」不等于「跟随更快」：
`updateMode = BLOCKING` 下渲染跟着相机走，把渲染解绑到显示刷新率只会用同一个位姿多画
几遍 —— ARCore 不在相机帧之间插值位姿。所以那条路是假的，没走。

顺手把实际生效的帧率暴露成 `ArSessionHolder.cameraFps` 并打进调试日志第一行：
「到底跑没跑到 60」在真机上原来无从判断，而它是这件事唯一的判据。

### 34.6 签名：一把提交进仓库的固定密钥

`release` 原来沿用 `debug` key，而那把钥匙是 AGP 在 `~/.android/debug.keystore` 里
**每台机器各自生成**的。后果两条，`android.yml` 的注释里其实早就写着：

- CI runner 上没有那个文件，AGP 每次现生成一把新的 → 每次 CI 出的包签名都不同
- 本地包与 CI 包签名不同 → 真机上两边换着装必须先卸载，数据全丢

现在 `android/keystore/photoar-release.jks` 提交进仓库，**所有变体**（debug / release /
androidTest）通过 `buildTypes.configureEach` 用同一把。口令写在 `build.gradle.kts` 里
不是疏忽：这把钥匙的用途就是「让任何人构建出的包能互相覆盖安装」，它保护的东西是零；
上应用市场要另配一把（`android/key.properties`，优先级更高），那把才需要保密。

验证方式：`./gradlew :app:signingReport`，所有变体的 SHA1 必须一样（实测
`B5:5E:A6:79:…:1A:2E`）。CI 里加了一步 `apksigner verify` 对着 jks 的指纹比 —— 签名
回退**不会让构建失败**，只会让真机上「覆盖安装」变成「必须先卸载」，那时候数据已经没了。

⚠️ **有且仅有一次例外**：手机上装的还是这次改动之前的包（旧的 debug 密钥），
这一次必须先卸载。之后历次升级都不用再卸载。

### 34.7 CI 补了什么

- **新 `server.yml`**：仓库里 1000+ 个 Python 用例**一次都没在 CI 上跑过**（原来只有
  安卓那条和 tag 触发的发镜像那条，而后者只 `docker build`，不跑测试也不把镜像起来看
  一眼）。现在三层：单元测试 → 用线上那份 Dockerfile 编镜像 → 把镜像当生产环境跑起来
  打接口（含重启后 `/data` 是否持久）。

  这个 workflow 是**在本机真跑过一遍**才提交的，跑出两个只在真容器里才暴露的问题：
  `PHOTOAR_ROOTS` 是必填的（不给就 2 秒 unhealthy），以及 YAML 字面块会把 heredoc 的
  结束标记一起缩进、于是 `<<'PY'` 永不终止（`<<-` 只吃 tab 不吃空格）。两条都写进注释了。

- **`android.yml` 的产物校验是坏的。** 它 `grep 'assets/arcore.apk'`，而那条路
  （整包塞进 assets、运行期再解）早就换成了 `ArcoreUnpackTask` 拆开的
  `assets/arcore_rt/dex.jar` + `assets/packed_profiles/` + `lib/<abi>/*.so`。也就是说
  这个「守着构建成功但包是坏的」的检查**恒定失败** —— 而恒假和恒真一样没用。改成查
  拆开之后那三样，另加一步签名校验，以及 tag 上顺手发一个 GitHub Release（artifact
  只有登录 GitHub 的人能下，而这个包是要发给宾客装的）。

### 34.8 像素风：换表达，不换结构

界线是刻意的。换掉的只有三样：**形状**（圆角 → 直角）、**字族**（无衬线 → 等宽）、
**配色**（一组有限的饱和色）。而 Material 的色角色、排版角色、组件、48dp 触摸目标、
系统返回手势一样都不碰 —— 自己画一套控件会失掉无障碍（TalkBack 认 Material 的语义）、
系统设置（字号缩放、去动画）和「安卓用户一眼知道怎么用」，像素风的代价不该由这些来付。

几个具体决定：

- **`Shapes` 全部换成 0dp 圆角**，一行改了整个 App —— 每个 Card / Button / TextField /
  Chip / Dialog / Snackbar 都跟着变直角，**包括我没有逐个改过的那些屏**。用
  `RoundedCornerShape(0.dp)` 而不是 `RectangleShape`：`Shapes` 的字段类型是
  `CornerBasedShape`（组件内部要按角半径插值），后者给不进去。
- **主色保留原来那个琥珀 `#FFC46B`。** 换风格不等于换品牌。而且「顺手换个更像素的绿」
  （Game Boy 豆绿 / 终端绿）恰好是所有像素风 UI 的第一反射，撞上去反而更没有辨识度。
  底色从 `#121316` 压到 `#0B0C10`：这套风格靠 2dp 硬边框而不是阴影分层，边框要读得出来
  就得有落差。
- **斜面代替阴影。** 上/左亮边 + 下/右暗边 = 凸起，按下时翻转、内容跟着挪 2dp。阴影是
  **模糊**的，而像素画里不存在模糊。按下要变形状而不是只变颜色 —— 直角界面上只变颜色
  几乎看不出来，而「点了没反应」最容易被当成卡顿。
- **不用 ripple。** 它是一圈扩散的**圆**，在直角像素界面上是最突兀的一处。
- **图标全是自己画的 16×16**（`PixelIcons`）。16 格的三条理由写在那个文件里。
  改造前底栏的「扫一扫」和「照片」用的是**同一个** `Icons.Filled.Home` —— 两个页签
  长得一样，只能靠文字区分；现在有一条测试盯着「没有两张图标是一样的」。
- **等宽字族，但不塞点阵字体文件。** 两个理由：许可（能用的点阵中文字体几乎没有），
  以及**中文** —— 点阵中文在 12sp 下糊成一团，那是拿可读性换风格。字号一个不改，
  跟随系统缩放这件事不能丢。
- **桌面图标是从界面里那张图生成的**（`tools/gen_launcher_icon.py`）。手维护两份的结果
  是改了一边忘了另一边，而没有任何检查会发现。`--check` 能验生成物是否还等于源图。
  同时生成 API 24-25 的兜底 PNG：`mipmap-anydpi-v26` 只在 26+ 生效，而 minSdk 是 24 ——
  只放那一份的话老机器上桌面**没有图标**，而 AAPT 不会报错。

一个写进注释的遗憾：扫描界面（`ArScanActivity`）是原生 View，而它在 `:arview`、
在 `:app` 下层，引不到 `pixel` 那个包（引了会成环）。所以那六个颜色值是**手抄**的，
改配色要一起改，**编译期查不出来**。

### 34.9 一个哨兵的教训（第二次）

`PixelBitmap.of` 第一版没有 `trimIndent()`。源码里的图缩进 8 个空格，于是 16 格的图变成
24 列，而 `PixelIcon` 按 `min(宽/列, 高/行)` 算格子 —— 格子小三分之一、图还偏右。
**一个字符都不报错**，症状只是「摆在一排时有的图标看起来不一样大」。

和上一轮 `FrameStats` 拿 0 当哨兵是同一类：**默认值恰好是合法值**。有测试钉住了。

---

## 35. 第 12 轮：两个用户报的问题，各自的根因都不是当初猜的那个

用户报两件事：**识别照片难（手指遮挡四角就认不出）**、**AR 跟踪延迟特别高**。

两件事都先量后改，而两次量出来的根因都和「读一遍代码得出的第一直觉」不一样。

### 35.1 延迟：一个常量同时管了两件时长差一个数量级的事

`ArRenderer.COAST_MS` 决定 FULL_TRACKING 断了之后的行为，而它同时决定两件事：

1. 视频还画不画（画的是**最后一次 FULL 的位姿**）
2. 还要不要向状态机报「在跟踪」

第 2 件必须给到秒级 —— 手持照片时 ARCore 会连续好几百毫秒认不出图案（手指压在边缘 +
覆膜反光），这一段报丢失就会暂停视频、弹一句「照片离开画面」，下一秒又认回来，表现是
每隔几秒暂停一下。[32.2](#322-四处改动) 把它从 2s 调到 3s 正是为了压住这个。

第 1 件必须是几百毫秒 —— 滑行期间贴的是照片**过去**所在的世界位置。而这套设计的前提
（写在 `PoseFilter` 类注释里）是「照片钉在墙上不动，所以那个位置仍然是对的」，**而真实
用法是手持**：照片在手里以 0.05~0.3 m/s 移动，错位量 = 手速 × 滑行时长。3 秒足够让视频
完全滑出照片之外。

合成一个常量之后只能取长的那个，于是「压住闪」直接换来了用户报的延迟。COAST_MS 自己的
注释里写着「还有『一动就闪』就往大调；出现『视频黏在空气里』就往小调」—— 那句话把它当成
一个一维旋钮，而它是两个。

**修法：拆成两个窗口**（`CoastPolicy`，纯 Kotlin、JVM 可测）。`PAINT_COAST_MS`(400ms)
之后停止绘制，`HOLD_COAST_MS`(3s) 之后才判定丢失。中间那一段视频不画、但仍在播、不弹
提示，ARCore 一认回来立刻贴回正确位置。用户看到的从「视频黏在错的地方」变成「跟丢时
淡出、跟回来立刻贴上」—— 后者与真相一致，前者不是。

400ms 不是手感调出来的，是量纲倒推的：0.3 m/s × 0.4s = 12cm，仍在一张六寸照片（横放
152mm）的宽度之内，也就是最坏情况下视频还压在照片上。`CoastPolicyTest` 里有一条测试
盯着这个量纲，另一条挡住「把画窗口调回 3 秒」。

滑行期间还按可信度**淡出**（`COAST_MIN_ALPHA` 0.35）：位姿的可信度是连续下降的，而
画不画是二值决定，硬切会让「刚断一帧」和「断了三百毫秒」看起来一样。前 120ms
（`FADE_GRACE_MS`）不降 —— 那一段是 `getUpdatedTrackables` 的空档，位姿一点没过期。

### 35.2 遮挡：**不是**识别失败的主因，量出来才知道

用户报的头号现象是手指遮挡，而这个变量在整个 bench 里从来不存在。补上
（`simcam.py` 的 `--occlude`／`--occlude-corners`，`occlude_corners` 有单测）之后：

| 每角遮 | 照片总面积 | 最小全过占比 | 相当于 |
|---|---|---|---|
| 0% | 0% | 0.40 | 对照组 |
| 2% | 8% | 0.40 | 两根手指压两角的两倍还多 |
| 5% | 20% | 0.40 | 抓得很满 |
| 10% | 40% | 0.80 | 极端 |

一个指尖压在六寸照片角上覆盖约 15×15mm ≈ 照片面积的 **1.5%**。也就是说**真实的手指
遮挡落在「几乎没影响」那一档**。遮挡是放大器不是原因：10% 那一档失败的正是无遮挡时
分数最低的那个视角（64 → 28~36，门槛 40）。

日志：`bench/logs/simcam-occlusion-2026-08-04.log`。

### 35.3 副产物比结论更重要：`repeat=3` 是虚假的精度

`backend.py` 的 `QUERY_N_FEATURES`／`QUERY_LONG_EDGE` 与 `Frames.kt` 的 `LONG_EDGE` 里
那三张「全过的最小占比」表**都是 3 个视角抽出来的**。同一张图把视角数抬到 20（无遮挡、
fill=0.4）：

```
64 138 149 53 46 111 105 102 104 85 123 132 39 50 79 111 54 125 97 150
```

跨度 **4 倍**（39~150），门槛是 40 —— 有 **1/20 落在 39**，差 1 分。「0.40 全过」是那 3 个
视角恰好都过，不是这一档真的稳。这与 `verify.MIN_INLIERS` 那段自己写的真阳性 p1=9／
p5=53 一致：**门槛 40 天生要吃掉 1%~5% 的真阳性**，而这里量到的 5%（1/20）正是那个数。

三处表格旁边都补了这条警告。

### 35.4 识别难的真根因：单帧独立判定，而分数分布的下沿贴着门槛

比 bench 更硬的证据是**真机日志**（`recognize_log`，1633 条）。194 条 `weak` 的 inliers 分布：

| inliers | 条数 | 占比 | 是什么 |
|---|---|---|---|
| 0–9 | 116 | 59.8% | 画面里根本没照片（举手机过程中的帧），**正常** |
| 10–29 | 53 | 27.3% | 看到了但太糊／太斜 |
| **30–39** | **22** | **11.3%** | **看到了，就差几分** |

而那 22 条的 `runner_up` 全是 6~9 —— 比值 3.3 倍以上，**top1 是毫无争议的第一名，只是
没到绝对门槛 40**。它们每一帧都被单独扔掉了，而客户端每 400ms 才有一次机会。

**修法：跨帧证据累积**（`photoar.streak`）。连续 3 帧的第一名都是同一张、每帧内点数
≥30、每帧比值 ≥2.0（比单帧的 1.5 严）→ 判命中。**`MIN_INLIERS` 一个字没动** —— 那个 40
是拟合在 34 个真实误识别事件上的，动它要重跑 `threshold_scan.py`。

### 35.5 这条路新增了误识别面，代价没量，所以必须能事后量

必须写明：单帧门槛 40 原本挡住了真实误识别（p95=36、**最大 39**），而累积把 30~39 这
一段放进来了。挡住它的不是门槛而是「连续 + 比值」，所以**挡不住能稳定误配的那一类**
（库外照片与库内某张几何上真的相似 —— `verify` 里说的「Oxford5k 的语料属性」就是它）。

仍然做，因为两侧量级不对称：漏检每次扫描都在发生（真机 11.3%），而稳定误配要求库里
恰好有一张几何高度相似的照片，而入库路径本来就有 `dedup` 闸门在挡这一类。

两条配套措施，缺一条这个取舍就不成立：

- 累积命中的 reason 是 **`streak`** 而不是 `ok`。混进 `ok` 里的话，这条路带来的误识别会
  和单帧命中混在一起，**永远量不出来**。要量就 `select ... where reason = 'streak'`。
- `recog.streak_need` 填 **0 就整条关掉**，退回纯单帧判定。

`tests/test_streak.py` 里有一条测试专门把「稳定误配会被放行」这个已知代价钉成可执行的
文档 —— 哪天要收紧，先看得到现在放行了什么，而不是从一次线上误识别倒推。

### 35.6 为什么累积的状态在服务端

另一个方案是未命中响应带上 top1，客户端自己累积（判定逻辑放 `ScanController`，JVM 可测，
更符合本项目的既有架构约定）。**没选它，因为照片是分权的**：识别路径上有 `forbidden`
分支，而 `weak` 那一支不跑授权检查 —— 直接把 photoId 回给客户端就是一次信息泄漏，
而且在界面上没有任何症状。

放服务端则累积命中**原地替换 `decision`**，于是留帧、记历史、orphan 判断、授权检查、
响应字段全部与单帧命中走同一条路，一个新暴露面都没有。`test_app_streak.py` 里有一条
安全测试盯着这件事（无授权访客通过累积也只能拿到 `forbidden`）。

代价是服务端多了一个内存字典。可以接受：它是缓存不是数据，重启丢了最多少一次累积
（下一次扫描 1.2 秒内又攒回来），不落盘、不进数据库、按 LRU 有上界。

### 35.7 还没查的两处

真机日志里另有两处异常，这一轮**没动**：

- **3 条 `inliers >= 40` 却判 `weak`。** 只可能是行列式越界（`DET_MIN`/`DET_MAX`），
  但也可能是真 bug。
- **144 条 `empty`（粗排零候选）。** 占比不低。是词表退化（`idf` 全 0 那类，见
  `index.unretrievable_docs`）还是画面里真的什么都没有，分不出来 —— 而这两件事一件要
  重训词表、一件不用管。

## 36. 第 13 轮：安卓客户端下线，前后端合成一个容器

两个决定，同一件事的两半：**产品只剩网页版**，于是**部署只该剩一个容器**。

### 36.1 为什么下掉安卓客户端

不是因为它做得不好 —— ARCore 的 6DoF 贴合比网页那条纯 2D 单应矩阵的路**更稳**，
这一点没有争议。下掉它的理由是三条与质量无关的：

- **装不了。** release 包 114.5MB，而且签名是仓库里那把 sideload 密钥 ——
  给亲友用意味着"先允许未知来源、下 115MB、装一个来路不明的包"。婚礼现场没人会做这件事。
  网页版是打开一个链接。
- **只覆盖一半人。** ARCore 只有安卓。iOS 与鸿蒙一个都跑不了，而它们占了在场手机的一半。
  网页版三个平台同一份代码（`camera.js` 是唯一有平台分支的文件）。
- **两份实现的同步成本是真的。** 相纸尺寸的毫米数手抄两份、导航策略两份、像素图标两份，
  每一处都靠一条"两边对得上"的测试兜着。第 11 轮那次像素风改造在两边各做了一遍。

而"更稳"这个优势在实际场景里没兑现：网页版第 12 轮之后（累积命中 + coast）在真机上
认得出、贴得住，30 秒的视频看完不掉。**够用**打败**更好**，因为够用的那个能到人手上。

代码在 git 历史里（`android/`，最后一次提交带着 `arview` 的 20 个 JVM 单测）。留着那个
目录的代价不是磁盘 —— 是每次改协议、改文案、改一个阈值都要问一遍"安卓那边要不要跟"，
而答案永远是"不跟，反正没人装"。

**没跟着删的：** 服务端那套 `.imgdb` / `/v1/targets/*`（ARCore 的整库目标库）与
`arcoreimg` 的质量分。前者是纯 ARCore 产物、现在没有消费者，但它挂在 `photo` 表两个
NOT NULL 的列上（`imgdb_path` / `imgdb_bytes`），拆掉要一次数据库迁移；后者是入库唯一
的纹理质量闸门，网页版同样受益于"太糊的照片别入库"。两件都是**可以做但要单独做**的事，
在一次"清理安卓"里顺手带上，赌的是一个正在用的库不出问题。`print_width_m` 同理保留，
界面上降级成"只是记下来"（见 `printsize.js` 的模块注释）。

### 36.2 为什么把两个容器合成一个

合并之前是 photo-ar（8964）+ web-front（48082）两个容器两个端口。那不是洁癖问题，
它有一个**用户能撞到**的后果：网页版里「打开管理台」那个按钮写的是
`window.open('/admin')` —— 它一直假设管理台与页面同源，而那时候管理台在另一个端口上，
所以那个按钮点开是 404。这个 bug 存在了一整轮没被发现，因为开发时总是直接敲管理台的地址。

同源不是可选项，理由在 `web-front/server/index.js` 顶部：会话是 HttpOnly cookie
（必须是 cookie，`<img>` 和 `<video>` 带不了请求头）、COEP `require-corp` 会拦跨源资源、
视频的 Range 要原样透传。跨源要 SameSite=None + Secure + 全套 CORS，而 Safari 的 ITP
还会拦第三方 cookie。

合并之后一个端口按 URI 分：`/` 网页版、`/api/*` 网页版自己的端点、`/admin` 管理台、
`/v1/*` API。前两个由 Node 直接处理，后两个反代给 Python。

**Node 在前、Python 退到 `127.0.0.1:8965`**，而不是反过来。反过来要把 web-front 那
596 行（静态、识别库打包、媒体票据、TLS）用 Python 重写一遍，还要连着它的四套测试一起
搬 —— 而合并要的是"少一个容器"，不是"换一种语言写前端"。

代价是**一个容器里两个进程**，于是 `docker/entrypoint.py` 从"exec 一下就消失"变成了
一个真正的进程管理器。规则只有一条：**谁死了都一起死，交给 `restart: unless-stopped`
把两个一起拉起来。** 让活着的那一半继续跑得到的是"容器 healthy、功能坏了" —— 编排
系统对这种状态无能为力。不用 s6/supervisord 是因为它们要么加一个基础镜像依赖、要么加
一份配置文件语法，而这里要管的进程只有两个。

`tests/test_entrypoint.py` 起真进程、发真信号地测它（信号转发、级联收尾、赖着不走要
SIGKILL、孤儿只收尸不算"一半死了"）—— 这四条全都只在容器里出错，而且出错的样子都不响。

镜像层面：Node 是**从 `node:22-trixie-slim` 里抠出来的那一个二进制**（120MB），不是
apt 装的 —— Debian trixie 的 `nodejs` 是 20.x，而 web-front 声明 `engines: node >=22`。
抠二进制的前提是两个基底同一个发行版，所以换 Python 基底的大版本时要连那行 tag 一起换。

**留了一条退路：** `PHOTOAR_WEB=0` 只起后端，直接监听公开端口 —— 回到合并之前的形态。
它存在的理由不是灵活性，是"婚礼当天网页那一半出问题时，改一个环境变量就回到已知状态，
不用回滚镜像"。

### 36.3 顺手修的一个无限重转码

改成分片 MP4 之后（上一轮，为了 MediaSource），`-t 30.000` 切出来的东西 ffprobe 报
**30067ms** —— 分片容器的时长按片对齐，最后一片带一点尾巴。而 `needs_transcode` 的
判据是 `> 30000`，于是"我们自己刚转好的片子仍然需要转码"，每次入库、每次 verify 都
会把同一条视频重转一遍。

这个循环**不报错**，只让 NAS 一直在转码 —— 唯一的症状是风扇。加了 500ms 余量
（`DURATION_SLACK_MS`）。分片是 1 秒一片，最坏情况的尾巴远小于它。

## 37. 第 14 轮：为什么每次进页面都要重下 11.4MB（根因是证书）

用户报的是"EDGE 没办法本地缓存吗？每次都要重新拉取与装配"。**这一条的根因和"缓存"
本身无关，也和 Edge 无关。**

### 37.1 量出来的：同一份代码，71.5 秒 vs 1.6 秒

同一台手机（小米 M2012K11C / Edge for Android 150）、同一个容器、同一份代码，
只换访问地址，用 CDP 的 Network 域数每一次请求：

| 地址 | 每次打开页面 | 界面可用 |
|---|---|---|
| `https://100.110.121.64:8964`（自签证书，点过"继续访问"） | opencv.wasm **两次请求共 22.8MB**，`fromDiskCache` 都是 false | **71.5 秒** |
| `http://localhost:8964`（adb reverse，无 TLS） | 首次 11.4MB，**第二次进来 0 字节全部命中磁盘缓存** | **1.6 秒** |

根因：**Chromium 对有证书错误的源整体禁用磁盘缓存。** 自签证书、点过"高级 → 继续
访问"的那种，全都算。`Cache-Control: public, max-age=31536000, immutable` 一个字都不
生效。这件事**没有任何 API 能查**，而它的症状是"手机好慢"—— 所有人都会去怀疑手机、
怀疑网络、怀疑 wasm 太大，没人会怀疑证书。

顺带解释了另一个一直没想通的数字：进度条走完之后那句"正在装配"要 **34 秒**。那不是
编译，是 `instantiateStreaming` 在**重新下载**（预取那一遍进不了缓存）。11.4MB ÷ 34s
= 0.33MB/s，与第一遍下载的速率一模一样。真正的编译很快 —— localhost 上整个启动
（下载 + 编译 + 起 Worker + 解析识别库）才 1.6 秒。

这也把上一轮"自签证书唯一的代价是点一次继续"的判断纠正了：它的代价是**每个宾客每次
进页面都重下一遍引擎**，而且和视频那条 MediaSource 的绕法不同，这一条绕不过去。

### 37.2 三个真 bug，都是被这次测量顺出来的

**一、`fromCache` 的判据本来就是错的。** `orb.js` 在 fetch **之前**查
`performance.getEntriesByName(...)`，拿"时间线上已经有过这条"当缓存命中。可 Worker
每次加载都是新的，时间线是空的 —— 于是它恒为 false，"引擎从缓存读取"那句文案永远
不会出现。**判据坏掉之后，那 71 秒看起来就只是"手机慢"。** 现在改成 fetch 之后看
`transferSize === 0 && encodedBodySize > 0`（Resource Timing L2 的规定），并且在
连续两次都走网络时把证书这条原因直接印在诊断日志上。

**二、`immutable` 给错了对象。** `/vendor/` 整个目录都在发 `immutable, max-age=1年`，
而 `opencv.js` 的 URL 里**没有版本号**（它一直叫 `opencv.js`）。升级 OpenCV 之后已经
访问过的浏览器会抱着旧的 128KB 配新 wasm 跑，表现是"函数签名对不上"，而且**只在部分
用户身上出现**。现在：wasm 的 URL 带 `?v=<内容哈希前12>`（由 `split-wasm.mjs` 写进
opencv.js，不需要任何人记得去改）→ immutable；opencv.js 改成 `no-cache` + ETag，
128KB 换一次条件请求。

**三、预取和真正的加载取的是两个 URL。** 加上 `?v=` 之后，`orb.js` 那个
`.js → .wasm` 的猜法得到的是**没有版本号**的那个 —— 两个缓存键，冷启动付两次
2.43MB、缓存里也存两份（实测抓到过一次加载传 4.87MB）。现在改成从 opencv.js 正文里
正则读出 `findWasmBinary` 的返回值，**不猜**。那 128KB 本来就要下，所以不是多一次请求。

### 37.3 预压 brotli：11.40MB → 2.43MB

`Content-Encoding` 这一层之前完全没用上，11.4MB 是裸传的。实测：

| | 体积 | 占比 | 压一次要 |
|---|---|---|---|
| 原始 | 11.40MB | 100% | — |
| brotli q=5 | 2.90MB | 25.4% | 0.3s |
| **brotli q=11** | **2.43MB** | **21.3%** | 21s |
| gzip -9 | 3.36MB | 29.5% | 0.2s |

q=11、**构建期压好提交进仓库**（`split-wasm.mjs` 一次写出四个文件：.wasm/.js/各自
的 .br）。按请求压是不行的：21 秒 CPU × 每个宾客，而 N5095 更慢。

三个细节，每个都能单独把这件事做坏：

- **`Content-Type` 按原文件的扩展名算，不是 `.br` 的。** 浏览器解压之后要看到
  `application/wasm` 才肯走 `instantiateStreaming`。
- **ETag 必须带编码、并且发 `Vary: Accept-Encoding`。** 同一个 URL 两种字节用同一个
  ETag，链路上任何共享缓存都可能把压过的字节发给不接受 br 的客户端。
- **进度条的分母要用解压后的长度。** `Content-Length` 是压缩后的 2.43MB，而
  `res.body.getReader()` 给出的是解压后的 11.4MB —— 直接拿它当分母，进度跑到 470%。
  服务端为此额外发一个 `X-Uncompressed-Length`。

还有一道守卫：**`.br` 比源文件旧就忽略它**。防的是"改了源文件、忘了重新压"——那时
服务端会发出旧代码，而浏览器解压得到的是完全合法的旧 JS，没有任何报错，只是行为不对。
所以只给 `split-wasm.mjs` 的产物预压（它们是生成物，不存在"手改了源文件"这回事），
不给每个 `.js` 都放一个 `.br`。

**效果**（真机，自签证书那条最差的路上）：22.8MB / 71.5s → **4.87MB / 16.0s**。
换成受信任的证书之后：首次 2.43MB、**再进来 0 字节 / 1.6 秒**。

### 37.4 视频改走 Cloudflare，静态资源改走边缘缓存

**视频走 Cloudflare 是用户 2026-08-05 明确接受的**，推翻了之前"隧道只跑 API 小包"
那条硬规定。理由不是风险变小了，是网页版的宾客不装任何东西、**没有第二条路**：
要么视频从隧道出去，要么宾客看不到视频。CDN 条款那条风险（账号级）照旧存在，写在
deploy-details 里，配两条降低暴露面的措施（用完摘掉 ingress、绝不给 `/v1`、`/api`
加缓存规则）。

顺着这条把 CDN 缓存补上了 —— 那是隧道那一段最值钱的一次优化：2.6MB 静态资源
（引擎 2.43MB + 字体 175KB + 素材 37KB）是所有宾客共享且内容永不变的。两个坑：

- **Cloudflare 的默认缓存是按扩展名的，名单里没有 `.wasm`。** 也就是最大的那一块
  默认**不会**被边缘缓存，必须自己加一条 Cache Rule。
- **那条规则必须按路径限定成 `/vendor/` 与 `/art/`。** `/v1/*` 与 `/api/*` 是按人
  授权的（`/api/lib` 是这个用户能扫的照片、`/api/stream/<票>` 是一次性票据换来的
  视频流）—— 把它们缓存到边缘就是把一个人的视频发给另一个人，而且**没有任何症状**，
  你看到的是"能播"。所以老教程里那条 "Cache Everything" 页规则在这里是个安全洞。

## 38. 第 15 轮：贴合不吻合、不丝滑、跟随有误判和延迟 —— 四个根因，全部量出来

用户报三件事：**贴合不吻合、不丝滑、跟随有误判和很大的延迟**。手机架住对着一张打印
照片，人离开现场，全程自动化排查（CDP + 数值化打桩 + 离线分析 + 仿真）。

四个根因互相独立，而**没有一个是当初读代码能猜到的**。

### 38.0 先做打桩，再改任何东西

`public/trace.js`：定长环形缓冲 + 纯数字，每 render 帧与每条 worker 结果各一条记录，
`window.__trace` 暴露给 CDP。为什么不用现成的诊断日志：那块是给人看的（中文、折叠、
按关键度分区），回答"刚才发生了什么"，但回答不了"四角抖了多少"、"贴合比真实姿态晚
多少毫秒" —— 那些要能求方差和分位数的数组，从中文日志里正则抠出来是自找麻烦。

**手机架住不动是测抖动的理想装置**：真值就是"四角应该完全不动"，所以轨迹里任何变化
都是噪声，不需要另外标注。

### 38.1 「认不出来」：内点分布整个贴在门槛下面

第一段轨迹（96 次检测）：内点**中位 30、最大 38**，而门槛是 40 —— **一次都没到过**。
分布是 `20-29: 36 帧, 30-39: 53 帧, 40+: 0 帧`。照片确实被匹配上了（runner-up 只有
个位数），只是视角偏、照片在画面里偏小，永远差那几分。

这正是 §35 在服务端用跨帧累积解决的那个问题，而**浏览器这条管线从来没有那一层**。
`streak.py` 的 docstring 里解释过"为什么状态在服务端"，理由是信息泄漏（客户端累积
要求服务端把未命中时的最佳猜测回给客户端，而 `weak` 那一支不跑授权检查）。
**那条理由在网页版不成立**：识别整个在浏览器里，而它手上的库（`/api/lib`）本来就只有
这个用户被授权的照片 —— 它能猜到的每个 photoId 都是它已经有权看的。

所以 `public/recognize/streak.js`：与服务端同一套规则（软门槛 30 / 要 3 帧 / 每帧比值
≥2.0 / 间隔窗口 2s / **不算证据时链要断掉**）。已知代价与服务端一样，`test/streak.test.js`
里有一条测试专门把它钉住。

**验证是决定性的**：门槛恢复 40 之后，同一台手机同一个视角，从"96 次检测全 weak、
一次都没锁上"变成"锁上并稳定跟踪 82 秒、97.5% 的帧有四角"。

### 38.2 管理台的识别阈值**从来没到过浏览器**

查上面那件事时发现的：`/api/config` 那段转换代码写的是 `cfg['recog.min_inliers']?.value`，
而服务端返回的是 `{fields: [{key, value, …}], values: {键: 值}}` —— 两层都不是那个形状。
于是每个 `pick()` 都是 undefined，浏览器**一直在用 `consts.js` 里的源码默认值**。

后果不是"少了个功能"：管理台上那三个识别参数对网页版完全无效，改了没有任何反应；
而设置页那一节还标着"服务端热配置"，显示的却是源码默认值。

**这个 bug 能活下来是因为测试里的假上游返回了一个真服务端从来没产出过的形状。**
断言不是漏了，是断言在一个不存在的世界里。假上游现在换成从跑着的容器上抄下来的
真实形状，并加了一条"非数值字段不往下传"。

### 38.3 「很大的延迟」：一阶低通几乎没削抖动，却付了三分之一的滞后

真机实测（锁定后 82 秒）：**四角画出去那一刻的陈旧度中位 88ms**（跟踪 44ms + 送帧
节流 + rAF 相位）。而在这之上，那条 tau=60ms 的一阶低通还要再加一个稳态相位延迟
（一阶低通对匀速输入的滞后正好等于 tau）—— 合计约 148ms。手持转动 30°/s 就是 4.4°
的角误差，屏幕上就是"视频落在照片后面"。

而它换来了什么：静止时原始四角的位置标准差 1.01‰ 画幅，平滑后 0.90‰ ——
**只削掉 11%**。因为噪声的时间尺度（每 51ms 一个新观测）与 tau 相当，而一阶低通
只压得住比 tau 快得多的成分。**这是个很糟的交易。**

`test/sim/predict.mjs`（噪声、延迟、节奏全用真机实测值）证明单参数无解：滞后与抖动
沿一条帕累托前沿换。原因是这两个指标**只在不同场景里可见** —— 静止时看得见抖动、
看不见滞后；运动时反过来。所以拿一个固定参数去平衡它们，怎么调都是在前沿上左右挪。

`public/render/quadfilter.js`：按**速度**分档。静止时重平滑（tau 180ms）且**完全不外推**
（那时速度全是噪声，外推会把速度噪声乘上 lead 直接注入位置）；运动时轻平滑（tau 25ms）
并按实测的 `now - quadAt` 外推。仿真结果：**运动段滞后 -36%，静止段抖动 -31%** ——
两个都好，不是交换。

三个细节：
- **lead 不写死。** 它 = 这个观测有多老（渲染循环里本来就在算）+ 平滑器自身的 tau。
  写死 88 的话换台快手机会补过头，而**补过头比补不够更难看**（果冻感）。
- **刻意欠补偿**（`LEAD_SCALE = 0.7`）：速度估计自己有滞后，补满在加速段会过冲。
  仿真里 0.7 优于 1.0。
- **`grabbedAt` 必须从抓帧那一刻带过来**，不能用"收到结果减去计算耗时" —— 帧会在
  worker 的 `pending` 里排队（跟踪 44ms > 送帧间隔 33ms 时必然发生），排队那一段
  不在 `ms` 里。

### 38.4 「不丝滑」：`getImageData` 吃掉主线程的一半

给抓帧加计时之后：**抓帧 7.5 次/秒、每次中位 71.7ms，主线程 55% 都花在它上面**，
22.5% 的帧迟到、其中 92% 正好跟在一次抓帧之后。`getImageData` 比跟踪本身（46ms）还贵,
而管线因此是**抓帧受限**而不是计算受限。

（这里踩过一个自己造的坑：`lastGrabMs` 忘了清零，于是它在每一帧上都非零，
"迟到的帧里有多少跟在抓帧后"这个统计恒等于 100% —— 那是统计方法造出来的结论。）

改法：主线程只做 `createImageBitmap(video)`，RGBA 转换搬到 worker 的 OffscreenCanvas。
真机实测三条：老路 65.1ms、`createImageBitmap` 19.4ms、bitmap→ImageData 10.2ms。

**像素等价性是验过的，不是推的**：拿一张定住的帧作源，`getImageData` 与
`createImageBitmap → OffscreenCanvas → getImageData` 逐字节比较，**4,915,200 字节
全部相同**。定住源这一步是关键 —— 第一次直接拿会动的 video 作源，量到"61% 像素不同"，
那是 `createImageBitmap` 的 await 期间相机又推了一帧，是帧差异不是路径差异。

⚠️ **不用 `createImageBitmap` 自带的 resize**：那是另一个缩放算法，而缩放算法决定每个
像素、像素决定 FAST 角点、角点决定描述子的每一位。换算法就是换特征空间，而库里的
`desc.bin` 是按现在这条算的。所以只做转换、不缩放，缩放留给 worker 里的 `drawImage`。

⚠️ **ImageBitmap 必须 close()**：它持有 GPU 内存。被顶掉的那一帧（跟踪比送帧慢时每秒
都在发生）漏一个 close 就是每秒泄漏一张 1280×960 的纹理。

效果：

| | 之前 | 之后 |
|---|---|---|
| 抓帧主线程耗时 | 中位 71.7ms | **中位 10.1ms** |
| 主线程被抓帧占掉 | 55% | **11%** |
| 迟到帧（间隔>33ms） | 671/2981 = 22.5% | **0/3062 = 0%** |
| 渲染 p95 帧间隔 | 66.1ms | **16.5ms**（一帧没掉） |
| 四角年龄中位 | 91ms | **43ms** |
| 抓帧频率 | 7.5/s | 10.7/s |

四角年龄减半是白拿的：管线不再被抓帧堵住，跟踪的节奏就快了一倍。

### 38.5 还没做的

- **抖动那一侧只有相对证据。** 滤波器的改善由仿真（-31%）与单元测试保证；真机上
  静止时的绝对抖动在不同段之间差好几倍（自动曝光/白平衡的慢漂移），所以两段轨迹的
  绝对值不可直接比。要真机验抖动，得先把相机的自动曝光锁住。
- **跟踪耗时 41ms 还是偏高**（桌面 9.5ms）。光流 83 点 + 重解单应，减点数会降精度。
  没量过"多少点够用"。
- **一次检测在真机上 560ms**（有词表）。开发机那个库只有 7 张照片、词表退化
  （2100 描述子 → 1564 词），真实部署的库大得多、词表也真，这个数不能外推。

## 39. 第 16 轮：把「怎么部署、从哪访问」写到 CI 的运行页上

用户要的是一件小事：**在 GitHub CI 的 Docker Build summary 里写清楚这个 server 怎么用、
怎么部署、怎么访问。** 做的时候撞出一个一直在的缺陷和一条缓存上的坑。

### 39.1 摘要该写什么、不该写什么

部署流程本来就在仓库里（`docs/deploy.md`），而这个仓库刚因为"同一套流程写在两处"
删过一次重复文件（`deploy/README.md` 顶部那段：**一份过时的速查比没有速查更坏**）。
所以第一个要定的不是文案，是**分工**：

| 类别 | 处理 | 为什么 |
|---|---|---|
| 只有运行页才知道的 —— tag、版本号、推没推、这次验过什么 | 写全 | 仓库里的任何文档都写不出来。而你刚看着构建变绿，缺的恰好就是这几样 |
| 稳定的架构事实 —— 一个端口按 URI 分、`PHOTOAR_ROOTS` 必填、相机要安全上下文 | 写上 | 不写就不成"照着能起来"，而这些不是按月漂移的那一类 |
| 会漂的 —— 完整环境变量表、NAS 上的资源与设备透传、维护命令 | 只给链接，**链接钉在这次构建的 commit 上** | 几个月后回看一次旧构建，钉 main 的链接点进去是改过的文档 |

而"它凭什么不会变成一份跑不起来的说明"不靠自觉：摘要声称的每条路径
（`/` `/admin` `/v1/*` `/healthz`）都是同一个 job 上面那一步用 curl 真打过的，
它给的那条最小 `docker run` 与 CI 里跑通的那条是**同一组必填项** ——
`tests/test_ci_summary.py` 直接从 workflow 里把那条命令抠出来做比对，而不是把期望值
再抄一遍（抄一遍就是把漂移搬了个地方）。

### 39.2 构建失败时**不给**部署说明

这条流水线的设计是"推镜像排在冒烟之后，所以出不去的正是出了问题的那一版"。而在一个
绿了一半的运行页上印一份"怎么部署"，读的人会以为 registry 上有东西可拉 —— 那就把上面
那个设计抵消了。所以失败分支只说清两件事：没有可部署的东西、先看哪一步的日志。
`docker pull` / `docker run` / `docker compose up` 三个字样一个都不出现（有测试钉着）。

### 39.3 CI 出的每一个镜像都写着 `-dev`（一直如此）

第 14 轮加的版本号（设置页「关于」那一行，连按 7 下进调试模式）走的是
`Dockerfile` 的 `ARG PHOTOAR_VERSION`，注释里也写着"CI 填 tag 或短 sha"——
**而 workflow 从来没传过这个 build-arg。** 于是网页版一路退回 `x.y.z-dev`，
那个后缀的语义本来是"不是 CI 出的镜像"，结果**在真正会被部署的那些镜像里恰好是没用的**。

它能活这么久是因为**没有任何症状**：那一行读数看起来完全正常，只是内容不对。所以这次
补的不只是 build-arg，还有一条断言 —— 起容器之后 `GET /api/config` 回的 `version` 必须
逐字符等于要注入的那个值。只打印不比对的话下次照样发现不了。

顺带定下版本号与镜像 tag 的对应关系，这样"设置页上那串字"能反查到镜像：
打 tag → `v0.2.0`（镜像 tag 是 `0.2.0`，差一个 v），其它 → `sha-1a2b3c4`，
与 `type=sha,format=short` 产出的镜像 tag 逐字符相同。

### 39.4 `ARG` 的位置有缓存含义

`ARG PHOTOAR_VERSION` / `ENV` 原来在 `COPY --from=nodert /usr/local/bin/node` **之前**。
它的值每次提交都变（短 sha），而 `ENV` 会让后面所有层失效 —— 放在那个位置等于每次构建
都重拷一遍 120MB 的 node 二进制并重新导出层缓存。移到所有 `COPY` 之后，失效的只剩
下面几行元数据。本地实测：带 build-arg 重新构建，14 步**全部 CACHED**。

### 39.5 `PHOTOAR_BIND` 在合并形态下是空转的

CI 那条 `docker run` 里有 `-e PHOTOAR_BIND=0.0.0.0`。读 `entrypoint.py` 才发现它什么也
不做：起了网页版时 entrypoint 把前端的 `HOST` 写死成 `0.0.0.0`、把后端**强制**按到回环上
（那条有安全含义 —— 否则同一个 docker 网络里的别人能绕过前面那层直接打 `/v1`），
所以外面设它不改变任何事。删掉了：留着会让照抄那段的人以为它是必需的，而"最小命令"
的价值全在于它真的最小。删完在真容器上验过（只给 `PHOTOAR_ROOTS` + `/data` + `-p`
就起得来，`/` 200、`/admin` 200、`/healthz` 200、`/v1/ping` 401）。

### 39.6 顺手把 tag 名从 shell 正文里拿出来

`${{ github.ref_name }}` 直接展开进 `run:` 的脚本正文等于把外部可控的字符串拼进 shell
源码。这里的攻击面只对能推 tag 的人开着，但改成走 `env:` 一样短 —— 没有理由留着
另一条路。

## 40. 第 17 轮：把随机初始口令改成写死的 `admin`（推翻 §16 的一半）

用户在 NAS 上部署完，**找不到管理员口令**，进不去管理台。要求：写死成 `admin`，
第一次登录强制改。

### 40.1 原来那个设计错在哪 —— 不是安全性，是恢复性

`_bootstrap_admin` 原来的逻辑：没配 `PHOTOAR_ADMIN_PASSWORD` 就 `secrets.token_urlsafe(12)`
生成一个随机口令，**在启动日志里打印一次**。那段 docstring 里论证得很清楚：

> **不能用固定默认口令**。"先给个 admin/admin，提示用户改"在一个挂在 Cloudflare
> 隧道后面的服务上等于没有口令：没人会去改，而那个默认值就印在这份源码里。

**这个论证到今天依然成立。** 改掉它不是因为它错，而是因为它有一个**更糟的失败模式**，
而那个模式在真实部署上发生了：

那行只在**真的建出账号**时打印（`ensure_bootstrap_admin` 返回 None = 已有 admin →
一个字都不输出）。而 `docker compose up -d` 重建容器会**清空旧容器的日志**。于是
「库里已有 admin」+「日志被清」这个组合一出现，口令就**永久拿不回来**：

- 设 `PHOTOAR_ADMIN_PASSWORD` 不管用（它只在没有 admin 时生效）；
- `photoar-server` 没有重置口令的子命令（只有 serve / reindex / build-vocab / verify / check）；
- 唯一出路是进容器 `sqlite3` 手动 `delete from user where name='admin'` 再重启。

一个「正常操作（重建容器）→ 永久锁死 → 只能手改数据库」的路径，比一个「有暴露窗口
但能自己关上」的路径更坏。**可恢复性也是安全属性的一部分**。

### 40.2 代价用强制改密抵，但它**只拦前端**

服务端在 `/auth/login` 与 `/auth/me` 上都回 `mustChangePassword`，管理台见到就把界面
锁成只剩一个改密表单（`webui` 里新增的 `#mustchg` 面板）。

**两个接口都要带是必需的**：管理台进主界面走的是 `/auth/me`，只在登录响应里给的话，
刷新一次页面就绕过去了。

判定方式是**现算**（`verify_password(DEFAULT_ADMIN_PASSWORD, …)`），不是在库里存
`must_change` 标记：

- 存标记要加一列、写迁移，而且标记与真相会**漂** —— 从库里直接改口令、或者把口令又
  改回 `admin`，标记都不跟着动；
- 现算的语义就是字面意思「你此刻的口令等于那个公开默认值」，不可能不同步。

代价是一次 hash 校验（~80ms），只在这两个低频接口上、且**只对 admin 算**（viewer
没有管理台可进）。

### 40.3 为什么**没有**在服务端也拦

看起来像漏了一层，其实加上**不提供任何保护**：抢先用 `admin/admin` 登进来的人，
第一件事就是改密 —— 而那恰好是唯一被允许的操作。拦完的结果是他把口令改成自己的、
真正的主人被锁在外面，比不拦更糟。

所以这一层的定位是**防遗忘，不是防攻击**。真正的防线只有两条：尽快改掉，或者一开始
就设 `PHOTOAR_ADMIN_PASSWORD`（设了就完全不走默认值那条路，也不会弹改密页）。
这句话写进了 `DEFAULT_ADMIN_PASSWORD` 的 docstring —— 免得后来人以为那个前端锁是安全措施。

### 40.4 那条测试被**反过来**了

`test_bootstrap_generates_a_usable_random_password_and_prints_it_once` 原来断言的正是
"口令必须是随机的，不能是固定默认值"。现在它变成
`test_bootstrap_uses_the_fixed_default_password_and_says_so_loudly`，并在 docstring
里写明**为什么反转**、以及原来那个理由依然成立。

一条断言被删掉时，删它的理由必须留在原地 —— 否则半年后有人看到"固定默认口令"会以为
是疏忽，把它改回随机，然后再撞一次同样的锁死。

新增四条：登录/`me` 两处都带标记、配了真口令时不弹、改掉后标记消失、**把口令改回
`admin` 标记会重新出现**（最后这条钉住"现算"这个实现选择）。

## 41. 第 18 轮：网页上传从来没成功过 —— 三层错叠在一起

用户在 NAS 上传照片＋视频（合计不到 50MB），报 413。查下来是**三个独立的错叠在一起**，
每一层都把下一层挡住了，所以看起来只有一个症状。

### 41.1 第一层：413 按「来路」拒，不按体积

`_upload` 原来的规则是：请求头带 `CF-Ray`（cloudflared 必加）就一律 413，**完全不看
Content-Length**。已有的那条测试就是拿 `body=b"x"` 断言 413 的 —— 一个字节也拒。

这条规则的前提是 App 时代的：上传的都是几百 MB 的原片，走隧道必挂，所以"隐藏入口"是对的。
**网页版把前提推翻了** —— 网页的正常访问路径就是隧道，一律拒等于素材页整个不存在。

而且报错文案写着「有 100MB 请求体上限」，于是它是一句**误导性的话**：用户传 5MB 也看到
这句，只会以为自己文件太大。

改成按体积：`TUNNEL_MAX_UPLOAD_BYTES = 95MiB`（≈99.6MB，卡在 Cloudflare 那个"100MB"的
两种读法之下）。**仍然由我们拒而不是让 Cloudflare 拒** —— 它掐断时返回的是一张没有上下文
的错误页，而那时文件已经传了一半。

文案里体积保留一位小数：整数会把 95.4MB 印成「95MB 超过 95MB 上限」，一句自相矛盾的话
（实测撞到过）。

### 41.2 第二层：`upload()` 发的报文形状整个不对

413 修掉之后露出 400 `missing_name`。`api.js` 的 `upload()` 发的是 `FormData`、
不带 `?name=`；而服务端要的是 `?name=<纯文件名>` ＋ **原始字节**（它直接
`stream_to(dst)` 把请求体写进目标文件）。

两个后果，第二个更隐蔽：名字没给 → 400；**就算补上名字，落地的也是一个把 multipart
边界和头一起写进去的坏文件**，而 HTTP 是 201。

文件名的清洗责任在客户端，因为服务端在这条路上是**拒**不是洗（`_upload` 与
`_safe_upload_name` 的注释写明了这是刻意的两种策略：这条路上名字由客户端指定，静默改名
会让文件落在一个客户端不知道的名字上，而客户端下一步正要拿这条路径去入库）。所以新增
`uploadName()`：切掉正反斜杠路径成分、去掉开头的点、空了给兜底名。

### 41.3 第三层：`uploadCheck()` 打错了路由，而空 `catch` 把它吞了

客户端发 `GET /v1/upload/check?sha256=&bytes=`，服务端那条路由是 **POST ＋ JSON body**
且必须带 `name`。响应字段也全对不上：客户端读 `known.exists` / `known.nasPath`，
服务端返回的是 `{nameTaken, sameContent, existingPath, suggestedName, knownContent, matches}`。

**它从来没报过错，也从来没工作过** —— 调用方把它包在 `try {} catch {}` 里，注释写着
"check 失败不该阻止上传"。那个判断是对的，**空的 catch 是错的**：降级要说出来，
否则一个整层失效的功能在界面上和正常完全一样。现在 catch 里会写一行
`判重没做成（原因），直接传`。

顺带把三条分支接对了：内容已在库里（名字可能不同）→ 复用那条路径；同名同内容 → 复用；
同名不同内容 → 改用服务端给的 `suggestedName` 传，不然服务端要把整个文件收下来、
落临时文件比完哈希才 409，几十 MB 白传。

### 41.4 为什么三条都活了这么久

三条全是"客户端自洽、服务端自洽、合起来对不上"，**单看任何一边都发现不了**。
Python 那边有 `test_upload_*` 覆盖服务端行为，JS 那边一条都没有。

所以新增的 `web-front/test/upload.test.js` 拦的不是逻辑，是**报文形状**：方法、路径、
query、body 的类型。12 条。另外拿 `api.js` 里真正那两个函数打了一次真实服务端，
断言落地文件与原文件 **sha256 逐字节一致** —— 那才是 raw body 与 multipart 的分水岭。
