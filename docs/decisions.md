# 决策记录：识别管线、用户体系、一键部署

这一轮改动的每一个选择、依据、以及**代价**。写给三个月后想改这些参数的人。

凡是带数字的结论都注明了是**实测**还是**推断**。推断的地方明确说是推断，不掩饰。
实测环境统一说明一次：

- **本机**：16 核 x86-64（带 AVX2），限到 3 CPU / 3GiB 来模拟 NAS 配额
- **目标机**：QNAP TS-464C2，Intel Celeron N5095，4 核，容器限 3 CPU / 3GiB
- ⚠️ **N5095 没有 AVX/AVX2，只到 SSE4.2**。所有本机数字到目标机都会变慢，且神经网络
  推理这类纯 GEMM 负载受影响最重。**本轮没有任何数字是在真机上量的。**

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
| `TARGET_FIND_TIMEOUT_MS` | 4.0s | ARCore 在画面里真正认出这张图 |
| `MEDIA_TIMEOUT_MS` | 2.5s | 取媒体元信息，**与装库并行**，不串在预算上 |
| `DOWNLOAD_TIMEOUT_MS` | 3.0s | 单次分片下载；视频整体走 `CACHE_VIDEO_TIMEOUT_MS` 60s，不在热路径 |

**最坏路径 = 2.5s（识别）+ 6.0s（命中后总预算）≈ 8.5s**，留 1.5s 给帧捕获和调度抖动。
所以 10 秒是**兜得住的**，而且兜不住的时候有下文：任一段超时都会落到「识别后全屏播放」，
不会停在一个转圈的界面上。

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
