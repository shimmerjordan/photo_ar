# 自建 AR 照片视频系统 — 设计文档

日期：2026-07-28（v2，基于实际部署环境重写）
状态：待评审

> v1 假设识别跑在 ECS、媒体走 Cloudflare R2 加密分层。在了解到实际部署（NAS 本机 cloudflared 直连、已有 CloudDrive2、已有 Tailscale + 自建 DERP、QNAP 为 N5095/8G）后，v2 把 ECS、R2、加密、分层存储全部砍掉。v1 的识别管线规格（§8）基本保留，那部分与部署环境无关。

---

## 1. 背景与目标

小米照片打印机曾提供：用 App 扫描打印出的实体照片，拍摄当时关联的视频贴合在照片上播放（AR）。同类商业产品（Artivive、EyeJack）全部闭源且按"可存储图片/视频数量"收费。

调研结论：商业服务卖的"数量上限"，本质是在卖 **云端图像检索算力** 与 **视频出网流量**。本项目两者都自有 —— 检索跑在 NAS，视频不出内网。

**目标**：在现有 QNAP NAS 上加一个 Docker 服务，配一个 Android App，实现无数量上限的等价系统。规模目标上万张照片。

### 1.1 已确定的需求约束

| 维度 | 结论 |
|---|---|
| 客户端 | Android 原生 APK，只给自己和家人。不做 iOS、不做 Web AR |
| 图库规模 | 上万张 |
| 照片来源 | 先有数字原图再打印。**且大量原图已存在 NAS 上，必须支持直接关联而非重新上传** |
| 媒体存储 | NAS 为唯一真源。网盘作为可选后端（经 CloudDrive2 挂载点，对后端就是普通路径） |
| 外网访问 | 多通道可配置 + 自动探活：LAN / Tailscale / DDNS+公网 / Cloudflare Tunnel |
| 加密 | **不需要**（媒体不出内网） |

### 1.2 现有环境（直接复用，不新建）

来自部署文档《ECS + NAS(QNAP) + fnOS 内网穿透与服务总文档（2026-07）》：

| 资源 | 现状 | 本项目如何用 |
|---|---|---|
| QNAP TS-464C2 | Celeron N5095（4C4T@2.9GHz），8GB DDR4 可扩 16GB，Container Station | 跑 `photo-ar-server`，含识别 |
| cloudflared `nas-adan` | 本机直连 Cloudflare，域名 `*.<你的域名>` 通配符已配 | **只需加一条 ingress**，DNS 侧零改动 |
| CloudDrive2 | 已在 19798 端口运行，`cd2.<你的域名>` | 网盘挂载点即普通路径，网盘支持零代码获得 |
| Tailscale | tailnet `<你的 tailnet>`，ECS 自建 DERP RegionID 901，**手机已装 App** | 媒体通道首选（外网时） |
| ECS <ECS 公网 IP> | 3M 带宽 | **本项目不使用**（除了它承载的 Tailscale DERP 是 Tailscale 基础设施的一部分） |

### 1.3 环境已知限制（必须遵守）

1. **Cloudflare 免费版请求体上限 100MB** — 经隧道上传超限会 413。因此从 App 上传原片必须走 LAN/Tailscale 通道，不能走隧道。
2. ECS 3M 带宽 — 与本项目无关，不使用。
3. 未备案域名 HTTP 必须走 Cloudflare Tunnel，不能直接暴露端口。
4. **Cloudflare Tunnel 走视频的 ToS 状态**：2023-05 新 ToS 措辞为"通过 CDN 服务视频，前提是内容托管在 Cloudflare 自家产品（Stream/Images/R2）"，故 Tunnel 转发 NAS 视频严格讲不在明文许可内；但 2026 年实际从未对个人媒体流执行，前提是**不开 CDN 缓存**（Tunnel 默认不缓存）。本项目预估流量约 0.9GB/月（见 §12.1），比同机已在运行的 Plex 小三个数量级，风险可忽略。隧道仍作为媒体通道的最后兜底，而非首选。

## 2. 非目标（YAGNI）

- iOS / ARKit、Web AR
- 多用户账号体系、分享给外人
- 3D 模型 / 特效等非视频叠加内容
- 同时跟踪多张照片（`maxTrack` 固定 1）
- 翻拍已打印老照片的入库管线
- 视频剪辑能力
- **云端对象存储、媒体加密、热冷分层**（v1 有，v2 砍掉）
- **ECS 参与任何数据路径**
- 直接对接网盘 API（阿里云盘 ToS 禁止；123云盘直链需会员）。网盘只经 CloudDrive2/OpenList 挂载点以文件路径形式接入

## 3. 术语

| 术语 | 含义 |
|---|---|
| 参考图 | 入库用的数字原图，特征从它提取。可以是 NAS 上已存在的文件 |
| 查询图 | App 从相机帧抽取、降采样后上传的图（约 50KB） |
| 目标库 / `.imgdb` | ARCore `AugmentedImageDatabase` 序列化文件。**每张照片一个单目标库** |
| 命中 | 查询图经词袋检索 + 几何校验，确定唯一对应的参考图 |
| Asset | NAS 上的一个文件引用（路径 + 指纹），**不复制内容** |
| Endpoint | 一条可达 `photo-ar-server` 的基址（LAN / Tailscale / DDNS / Tunnel） |

## 4. 总体架构

```
┌─ QNAP TS-464C2（N5095 / 8G）─────────────────────────────┐
│                                                           │
│  Docker: photo-ar-server        ← 本项目唯一新增服务      │
│    ├─ recognizer   fbow 倒排索引 + ORB/RANSAC 几何校验    │
│    ├─ catalog      SQLite 单库（无副本、无同步）          │
│    ├─ fs-browser   /v1/fs/*（白名单根目录内）             │
│    ├─ media-resolve 路径 → URL（策略链，见 §10）          │
│    ├─ ingest       arcoreimg + ffmpeg                     │
│    └─ file-serve   Range 支持的静态吐流                   │
│                                                           │
│  【复用】cloudflared nas-adan                             │
│      + ingress: arphoto.<你的域名> → :8964        │
│      （*.<你的域名> 通配符已存在，DNS 零改动）    │
│  【复用】CloudDrive2 :19798 挂载点 → 普通路径             │
│  【复用】Tailscale（NAS 与手机均已在 tailnet）            │
└──────────────────────────────────────────────────────────┘
       ▲ LAN            ▲ Tailscale        ▲ Cloudflare Tunnel
       │ 最快、无限     │ 外网、无限       │ 永远在线、有 HTTPS
       └────────────────┴───────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────┐
│  Android App                                              │
│    EndpointResolver                                       │
│      apiEndpoint   ← 独立探活（通常落 Tunnel）            │
│      mediaEndpoint ← 独立探活（通常落 LAN/Tailscale）     │
│    Flutter 外壳：照片管理 / NAS 文件浏览 / 历史 / 缓存    │
│    原生 PlatformView：ARCore + SceneView + ExoPlayer      │
└──────────────────────────────────────────────────────────┘
```

### 4.1 为什么 API 与媒体是两条独立通道

这是 v2 的核心设计，直接来自环境约束：

| | API（识别、元数据、文件列表） | 媒体（视频、缩略图、上传原片） |
|---|---|---|
| 单次数据量 | 上行 50KB / 下行 <50KB | 1.5MB 下行；上传原片可达数百 MB |
| 首选通道 | **Cloudflare Tunnel** | **LAN → Tailscale** |
| 理由 | 永远在线、有合法 HTTPS 证书、不依赖 Tailscale 登录状态；小包完全在免费版舒适区 | 不占隧道流量、无 ToS 顾虑、**绕开 Cloudflare 100MB 上传上限** |
| 兜底 | LAN / Tailscale | Tunnel（1.5MB 视频毫无问题） |

两条通道**各自独立探活、独立降级**。任一条不可用不影响另一条。

## 5. 组件划分

### 5.1 `recognizer`（NAS，库）

- **做什么**：ORB 提取、fbow 索引构建/查询、几何校验判定。
- **接口**：`extract(image) -> Descriptors`；`query(Descriptors, topK) -> [photoId]`；`verify(Descriptors, photoId) -> {matched, inliers, homography}`
- **依赖**：OpenCV、fbow
- **为什么独立**：唯一需要反复调参的部分。独立成库后识别率回归测试不需启动任何服务、不需网络。

### 5.2 `catalog`（NAS，SQLite 单库）

- **做什么**：asset 引用、photo 元数据、识别日志。
- **单库无同步** —— v1 的 `catalog.db`/`events.db` 双库单向同步问题随 ECS 退出而消失。

### 5.3 `fs-browser`（NAS）

- **做什么**：在**白名单根目录**内列目录、出缩略图、算指纹。让 App 能直接挑选 NAS 上已有的图片/视频。
- **接口**：`list(path) -> [Entry]`；`thumb(path) -> JPEG`；`fingerprint(path) -> {sha256, bytes, mtime}`
- **安全边界**：路径必须规范化后落在白名单根目录内。拒绝 `..`、符号链接逃逸、绝对路径穿越。这是唯一直接暴露文件系统的组件，必须有针对性的路径穿越测试。

### 5.4 `media-resolve`（NAS）

- **做什么**：把一个 asset 路径解析成客户端可直接取用的 URL，按配置的策略链尝试。
- **接口**：`resolve(assetId, clientCtx) -> {url, via, supportsRange}`
- **策略见 §10**

### 5.5 `ingest`（NAS，CLI + API 双入口）

- **做什么**：把「参考图 asset + 视频 asset + 打印尺寸」变成可用的索引条目与 `.imgdb`。
- **CLI**：`ingest add --ref-path <nas路径> --video-path <nas路径> --print-width-mm 152`
- **API**：供 App 内关联流程调用
- **依赖**：`arcoreimg`、OpenCV、fbow、ffmpeg

### 5.6 `ar-view`（Android，原生 Kotlin）

- **做什么**：相机预览、抽帧、识别调用、单目标跟踪、视频贴图播放。
- **接口**：~~对 Flutter 的 MethodChannel/EventChannel~~ → 外壳同进程直接启 `ui.ArScanActivity`（Phase 3 改，见 §5.8）。状态机变迁与命中结果由 `ScanController` → `ScanEffects` 在进程内直接分发。
- **依赖**：ARCore、~~SceneView(Filament)~~ 手写 GLES 2.0（Phase 2 改，见 §17）、ExoPlayer(media3)

### 5.7 `EndpointResolver`（Android）

- **做什么**：维护候选 endpoint 列表，探活并分别选出 `apiEndpoint` 与 `mediaEndpoint`。
- **接口**：`api() -> Endpoint`；`media() -> Endpoint`；`refresh()`
- **见 §9**

### 5.8 `app-shell`（~~Flutter~~ → Kotlin + Jetpack Compose，Phase 3 改）

- **做什么**：照片列表、照片详情、NAS 文件浏览与关联、入库、扫描历史、缓存管理、endpoint 设置。
- **为什么不是 Flutter**：项目地基是 ARCore，只有 Android，Flutter 的跨平台价值为零；而代价是把 §7 契约实现两遍（Dart 侧重写请求与解析，或把每个 API 都包成 MethodChannel）；§5.7 已经把 `EndpointResolver` 放在原生侧，「当前走哪条通道」这个所有界面都要显示的状态本就在那边；缩略图要带 `Authorization` 头，`Image.network` 用不了，Flutter 的控件生态省事这一条也不成立。改 Compose 后外壳与 `:arview` 同进程直接调用，无桥、无第二份契约实现。
- **代价**：`@Composable` 层无法在 JVM 单测里跑，所以所有「差一位就错」的格式化与校验抠进不 import `android.*` 的 `Fmt.kt`（26 个测试）。

## 6. 数据模型

单个 SQLite 库，在 NAS 上。

```sql
-- NAS 上的文件引用。核心原则：引用，不复制。
CREATE TABLE asset (
  id          TEXT PRIMARY KEY,   -- uuid v4
  kind        TEXT NOT NULL,      -- 'image' | 'video'
  nas_path    TEXT NOT NULL UNIQUE,-- 白名单根目录下的规范化绝对路径
  sha256      TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  mtime       INTEGER NOT NULL,
  width_px    INTEGER,            -- image / video 均记录
  height_px   INTEGER,
  duration_ms INTEGER,            -- video only
  missing     INTEGER NOT NULL DEFAULT 0,  -- 1 = 校验时发现文件已不在
  checked_at  INTEGER,
  created_at  INTEGER NOT NULL
);

CREATE TABLE photo (
  id              TEXT PRIMARY KEY,
  ref_asset_id    TEXT NOT NULL REFERENCES asset(id),  -- 参考图
  video_asset_id  TEXT REFERENCES asset(id),           -- NULL = 尚未关联视频
  playable_asset_id TEXT REFERENCES asset(id),         -- 转码后的播放版；等于 video_asset_id 时表示原片可直接播
  title           TEXT,
  print_width_m   REAL NOT NULL,   -- 打印物理宽度（米），跟踪精度关键
  quality_score   INTEGER NOT NULL,-- arcoreimg eval-img，0-100
  imgdb_path      TEXT NOT NULL,   -- 生成物，在服务自有数据目录下
  imgdb_bytes     INTEGER NOT NULL,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);

CREATE TABLE recognize_log (
  ts         INTEGER NOT NULL,
  photo_id   TEXT,               -- NULL = 未命中
  inliers    INTEGER,
  latency_ms INTEGER,
  via        TEXT,               -- 命中时客户端用的 api endpoint 名
  topk_json  TEXT
);

CREATE INDEX idx_asset_path ON asset(nas_path);
CREATE INDEX idx_photo_ref  ON photo(ref_asset_id);
CREATE INDEX idx_log_ts     ON recognize_log(ts);
```

ORB 描述子**不入 SQLite**。按 `photo.id` 存为定长二进制文件（300 × 32 字节 = 9600 字节/张），mmap 后按偏移随机读。1 万张 = 96MB 磁盘，内存零占用。

### 6.1 引用完整性

`asset` 记录的是 NAS 上他人（用户自己、CloudDrive2、其他 App）也会动的文件。因此：

- 每次 `resolve` 前校验 `mtime` + `bytes`。不一致则重算 `sha256`：
  - `sha256` 相同（仅 mtime 变化）→ 更新 `mtime`，正常继续
  - `sha256` 不同 → 文件内容被替换。若是参考图，标记该 `photo` 需重新入库（特征已失效）；若是视频，仅更新指纹
- 文件不存在 → `asset.missing = 1`，管理界面标红，识别仍能命中但播放时给出明确提示
- 每周一次全量校验任务

**不做**自动修复或路径追踪。文件被用户移动了就是失效，由用户在界面上重新指定 —— 猜测用户意图的自动重绑定风险更大。

## 7. API 契约

Bearer Token 鉴权（预共享静态 token，家庭自用不做 OAuth）。所有接口在所有 endpoint 上行为一致。

### `GET /v1/ping`

探活用。响应 `{"ok": true, "version": "...", "serverTime": 1753...}`。**必须极轻**，不查库。

### `POST /v1/recognize`

请求 `multipart/form-data`，字段 `frame`，JPEG（长边 640px，q70，约 50KB）。

命中（200）：
```json
{
  "matched": true,
  "photoId": "8f3c...",
  "inliers": 47,
  "printWidthM": 0.152,
  "refAspect": 1.5,
  "imgdbUrl": "/v1/photo/8f3c.../imgdb",
  "refThumbUrl": "/v1/photo/8f3c.../thumb",
  "latencyMs": 63
}
```

未命中（200）：`{"matched": false, "latencyMs": 41}`

未命中返回 200 而非 404 —— 扫描过程中未命中是正常状态，不是错误。客户端会连续调用，不应产生错误日志噪音。

**URL 一律返回相对路径**，由客户端按当前 `apiEndpoint` / `mediaEndpoint` 拼接。服务端不知道客户端走的是哪条通道，返回绝对 URL 会把客户端锁死在一条通道上。

### `GET /v1/photo/{id}/imgdb`

`application/octet-stream`，ARCore 序列化的单目标库。带 `ETag` 与 `Cache-Control: max-age=31536000, immutable`。

`.imgdb` 实际体积**需在 Phase 0 实测**，初始预算 30KB/张。若实测 >200KB，改为只下发缩略图、端上 `addImage()` 运行时构建。

### `GET /v1/photo/{id}/thumb`

参考图长边 640px 的 JPEG。仅 `.imgdb` 下载失败时的兜底路径会用。

### `GET /v1/photo/{id}/media`

```json
{
  "url": "/v1/asset/1a2b.../stream",
  "via": "nas_serve",
  "supportsRange": true,
  "bytes": 1548392,
  "durationMs": 12400,
  "missing": false
}
```

`via` 为 `"direct_link"` 时 `url` 是**绝对 URL**（网盘 CDN 地址，与本服务无关），客户端直接用不拼前缀。这是唯一的例外，由 `via` 字段明确区分。

### `GET /v1/asset/{id}/stream`

Range 支持的文件吐流。**必须实现 `Accept-Ranges: bytes` 与 206 响应**，否则 ExoPlayer 无法 seek。

### `GET /v1/fs/list?path=<路径>`

```json
{
  "path": "/share/Photos/2024",
  "parent": "/share/Photos",
  "entries": [
    {"name": "IMG_001.jpg", "isDir": false, "kind": "image", "bytes": 4823910, "mtime": 1753...},
    {"name": "clips", "isDir": true}
  ]
}
```

`path` 省略时返回配置的白名单根目录列表。路径不在白名单内返回 403。

### `GET /v1/fs/thumb?path=<路径>`

图片/视频首帧的缩略图，长边 320px。用于 App 里的文件选择器。带 `ETag`（基于 path+mtime）。

### `POST /v1/photo`

从 NAS 已有文件创建条目：
```json
{
  "refPath": "/share/Photos/2024/IMG_001.jpg",
  "videoPath": "/share/Videos/2024/VID_001.mp4",
  "printWidthMm": 152,
  "title": "外婆生日"
}
```

服务端做：创建/复用 asset → `eval-img` 质量分（<75 拒绝并返回分数与原因）→ 提特征入索引 → `build-db` → ffmpeg 转码出播放版 → 返回 photoId。

**转码产物写到服务自有数据目录**，不污染用户的照片/视频目录。

### `POST /v1/upload`（可选路径）

从手机上传新视频。**必须经 LAN/Tailscale 通道**（隧道有 100MB 上限）。客户端在走隧道时应隐藏此入口并说明原因。

## 8. 识别管线规格

全系统唯一的真风险点。这一节与部署环境无关，v1 的规格完整保留。

### 8.1 入库侧

1. `arcoreimg eval-img` 打质量分。**< 75 拒绝入库并说明原因**，不要留到扫不出来才发现。
2. 参考图缩放到**长边 640px**（`INTER_AREA`）。
3. OpenCV ORB 提取，`nfeatures=300`、`scaleFactor=1.2`、`nlevels=8`，取响应最强的 300 个。
4. 写定长描述子文件，同时喂入 fbow 数据库。
5. `arcoreimg build-db` 用**原图**（非 640px 版本）生成单目标 `.imgdb`。

**尺度对齐是硬约束**：入库提特征用 640px、查询图也是 640px。ORB 不具备尺度不变性，两侧分辨率不一致会让召回率大幅下降。违反这一条，后面所有调参都是白做。

### 8.2 词汇树

先直接用 ORB-SLAM 提供的现成通用 ORB vocabulary，**不要一开始自训**。用回归测试测出基线后，若召回不达标再用 fbow 训练工具基于自有照片集训专用词汇树。

理由：自训引入新变量，会让"识别率不达标"的归因变困难。先固定一个已知可用的 vocab 拿到基线。

### 8.3 查询侧（两阶段）

1. **粗排**：fbow 查 Top-20，预期 10-50ms。
2. **精排 + 几何校验**：对 Top-20 从 mmap 读描述子，ORB 暴力汉明匹配（`BFMatcher` + `crossCheck`），`findHomography(..., RANSAC, 3.0)`。
3. **命中条件（三条全部满足）**：
   - 内点数 ≥ **40**（初稿写的是 25，见下）
   - 单应矩阵可逆，行列式在 `[0.05, 20]` 内（排除极端剪切/退化）
   - 第一名内点数 ≥ 第二名的 1.5 倍（排除自相似照片的歧义命中）

三条缺一不可。上万张家庭照片自相似度极高（同场景连拍），仅靠词袋分数必然误判。

> **内点数下限 25 → 40（0d 上规模实测后改的，2026-07-29）。** 25 下库外真实误识别 0.349%，超 §14.2 的 0.1% 目标 3.5 倍。同一批 29740 次查询的**查询时**内点数分布显示两个群体几乎不重叠：真实误识别 34 条最大 39、p95=36，而库内真阳性 19284 条 p5=69。40 把真实误识别归零，代价是库内命中 96.42% → 95.70%（仍守住 ≥95%）。可行窗口 `[40, 47]`，取下界。`RATIO` 保持 1.5：抬它只压得住"同一被摄物体的不同照片"那类语料属性，对真实误识别零边际作用。
>
> 依据、复现命令与那 34 条的原始值见 `docs/superpowers/plans/phase0-results.md`「查询时的分布」「网格重放」两节。
>
> ⚠️ 40 拟合在 22 张留出图的 34 个事件上，样本很小；质量分闸门放开后、或换非 Oxford5k 语料，必须用 `bench/threshold_scan.py` 重测。
>
> 另有一个**不同的**内点数下限 `DEDUP_MIN_INLIERS = 25`，给近重复检测用，量的是两张原图之间的内点数而不是查询时的。它必须低于本条的 40，两者不能统一——理由见 `verify.py` 里那两段注释。

### 8.4 资源预算（QNAP N5095 / 8GB）

| 项 | 预算 |
|---|---|
| fbow 倒排索引（1 万张） | 约 200MB 常驻 |
| ORB 描述子 | 96MB 磁盘，mmap，内存零占用 |
| 单次查询峰值内存 | < 20MB |
| 服务端识别耗时（N5095 4 核） | 目标 < 80ms |
| 端到端识别延迟 | < 500ms |
| 扩展上限 | 8GB 内存下约 5 万张；扩到 16GB 可到 10 万张以上 |

N5095 是 4 核 2.9GHz，比 v1 假设的 ECS 2 核更宽裕，且不必与其他服务争抢 3M 带宽。

## 9. Endpoint 多通道解析

直接对应"怎么从外网访问 NAS"这个需求。

### 9.1 候选列表（App 内可编辑，带默认值）

```json
[
  {"name": "LAN",       "base": "http://192.168.1.20:8964",           "prefer": ["media", "api"]},
  {"name": "Tailscale", "base": "http://100.x.y.z:8964",              "prefer": ["media"]},
  {"name": "Tunnel",    "base": "https://arphoto.<你的域名>", "prefer": ["api"]},
  {"name": "DDNS",      "base": "",  "enabled": false,                "prefer": ["media", "api"]}
]
```

`DDNS` 项预留空位，将来 NAS 拿到公网 IP 后填入即可，无需改代码 —— 这就是"可配置的 URL 前缀"。

### 9.2 探活与选择

- App 启动、网络变化（`ConnectivityManager` 回调）、或用户手动刷新时触发
- **并行** `GET {base}/v1/ping`，超时 1.5s
- 在通的 endpoint 中，`api` 与 `media` **各自独立**选择：按该用途的 `prefer` 顺序取第一个通的；都不在 `prefer` 里则按列表顺序兜底
- 结果缓存到下次网络变化。请求连续失败 2 次立即重新探活

### 9.3 默认选择的预期结果

| 场景 | apiEndpoint | mediaEndpoint |
|---|---|---|
| 在家（同局域网） | LAN | LAN |
| 在外 + Tailscale 在线 | Tunnel | Tailscale |
| 在外 + Tailscale 未登录 | Tunnel | Tunnel |
| 断网 | 无（走本地缓存索引，Phase 4） | 无（走本地视频缓存） |

第三种场景下媒体走隧道 —— 1.5MB 短视频完全无问题，见 §1.3 第 4 条。

### 9.4 上传的特殊处理

`POST /v1/upload` 请求体可能达数百 MB，超过 Cloudflare 免费版 100MB 上限。因此：

- 上传**只允许**在 `mediaEndpoint` 非 Tunnel 时进行
- 走 Tunnel 时 App 隐藏上传入口，并显示"连回家庭网络或开启 Tailscale 后可上传"
- 不做分片上传绕过 —— 那是为了规避而增加复杂度，而正常用法（关联 NAS 已有文件）根本不需要上传

## 10. 媒体 URL 解析策略链

`media-resolve` 按配置顺序尝试，第一个成功的即返回。

| 策略 | 何时用 | 返回 | 流量路径 |
|---|---|---|---|
| `direct_link` | asset 路径在配置的挂载点前缀下，且该挂载点开启了直链 | 网盘 CDN 的**绝对 URL** | 网盘 → 手机（一跳，不占 NAS 上行） |
| `nas_serve` | 默认 | `/v1/asset/{id}/stream` 相对路径 | NAS → 手机（含 CloudDrive2 挂载点时为 网盘→NAS→手机 两跳） |
| `custom_prefix` | 配置了外部 URL 前缀（未来的 CDN / 公网静态服务） | 前缀 + 相对路径 | 取决于配置 |

**默认只启用 `nas_serve`。** 这一条就能覆盖全部需求，包括网盘 —— 因为 CloudDrive2 已把网盘挂成本地路径，对后端就是普通文件。

`direct_link` 是纯优化，默认关闭。启用它需要注意：

- **阿里云盘不要启用**。其条款明确禁止"搭建图床、视频外链到视频网站播放等分发服务"，并禁止"账号被多IP访问"，违规冻结后**无法解除**。家人多台手机访问正好同时命中两条。
- **123云盘是唯一官方允许的**：官方直链功能明确面向"图床、音视频分发等 CDN 场景"，支持自定义域名与防盗链。免费送 2TB，但直链需会员，会员每月免费流量 100G。
- **CloudDrive2 的直链是付费会员功能**（需开"允许远程直链访问"开关，且两端均需有效会员），免费版只能挂 2 个网盘。
- 若要用 OpenList 取直链，**用 [OpenList](https://github.com/OpenListTeam/OpenList) 而不是 AList**：AList 于 2025-06 被原作者出售给贵州不够科技，新版本被发现含采集设备信息上传私有服务器的代码，核心开发者集体退出并 fork 出 OpenList。
- 直链通常有有效期（阿里云盘约 15 分钟，OneDrive 约 1 小时）。`media-resolve` 必须每次请求时现取，不得缓存 URL。

### 10.1 为什么 `nas_serve` 就够了

| 指标 | 数值 |
|---|---|
| 1 万条 15 秒 720p 视频总量 | 15-30GB（四盘位 QNAP 轻松容纳） |
| 家人每天扫 20 次的流量 | 20 × 1.5MB = 30MB/天 ≈ **0.9GB/月** |
| 对比：同机 Plex 单部电影 | 2-10GB |

存储和带宽两个瓶颈都不存在。网盘直链解决的是"存储不够"或"上行不够"，本项目两者皆不缺。

## 11. 客户端状态机

```
IDLE ──startScan──> SCANNING ──命中──> LOADING_TARGET ──> TRACKING ──> PLAYING
                       ▲                    │                │            │
                       │ 未命中(继续抽帧)   │ 失败(兜底)     │ 丢失跟踪   │
                       └────────────────────┴────────────────┴──> PAUSED ─┘
```

1. ARCore Session 启动时**不配置** `AugmentedImageDatabase`（此时还不知道要跟哪张）。
2. 每 400ms 抽一帧 → 长边 640px → JPEG q70。
3. **先查本地缓存索引**（最近 200 张的 ORB 描述子，约 2MB）。命中则完全跳过网络。
4. 未命中 → `POST {apiEndpoint}/v1/recognize`。
5. 命中后下载 `.imgdb` → `config.augmentedImageDatabase = db`、`maxTrack = 1` → `session.configure()`。
6. **命中后立即停止抽帧与识别请求**。恢复条件只有两个：用户主动退出当前照片，或**持续丢失跟踪超过 10 秒**（视为已转向另一张照片，回到 `SCANNING`）。
7. `TrackingState.TRACKING` 时，在图像平面放置与照片等大的四边形，尺寸由 `printWidthM` 与 `refAspect` 决定。
8. ExoPlayer 输出到 `ExternalTexture`，贴到四边形。边缘 1-2px 羽化 + 淡入，避免"贴纸感"。
9. 丢失跟踪 → 暂停并保留播放位置；恢复 → 续播。

**物理尺寸红利**：流程是"原图 → 打印"，打印尺寸入库时已知。`addImage(name, bitmap, 0.152f)` 传入准确物理宽度，跟踪精度显著优于让 ARCore 自行估算。这是 `print_width_m NOT NULL` 的原因。App 里给常用尺寸预设（3寸/5寸/6寸/A4），避免每次手输。

`session.configure()` 更换目标库会短暂重置 session。状态机必须显式建模 `LOADING_TARGET` 中间态，容忍这期间的 tracking 中断，不能误判为"丢失"。

## 12. 视频规格

```
ffmpeg -i in.mp4 \
  -vf scale=-2:720 \
  -c:v libx264 -preset slow -crf 26 -maxrate 1500k -bufsize 3000k \
  -c:a aac -b:a 96k \
  -movflags +faststart \
  out.mp4
```

- `+faststart` **必须有**，否则 moov box 在文件尾，无法边下边播
- 720p / ≤1.5Mbps → 15 秒约 1.5-3MB
- 时长上限 15 秒，超长入库时截断并警告
- `scale=-2:720` 而非 `-1:720`，保证宽度为偶数（H.264 要求）
- N5095 有 Intel QuickSync，可用 `-c:v h264_qsv` 硬件加速；但先用 libx264 保证画质与兼容性，转码是离线批处理，不追求速度

### 12.1 转码产物的存放

转码后的播放版写入**服务自有数据目录**（如 `/share/photo-ar/playable/`），不写回用户的照片/视频目录。原片保持原样不动。

`photo.playable_asset_id` 指向转码产物。若原片本身已满足规格（720p 以内、有 faststart、≤15 秒），跳过转码，`playable_asset_id = video_asset_id`。

## 13. 错误处理

| 情况 | 处理 |
|---|---|
| 所有 endpoint 均不可达 | 明确提示"无法连接 NAS"，列出各 endpoint 的探活结果，不要只说"网络错误" |
| 识别请求超时（> 2s） | 不阻塞相机预览，静默重试。连续 3 次失败 → 提示网络慢并触发重新探活 |
| 未命中（内点不足） | 静默继续下一帧。连续扫 5 秒无果 → 提示"请对准照片，避免反光和遮挡" |
| `.imgdb` 下载失败 | 降级：用 `refThumbUrl` 取缩略图，端上 `addImage()` 现场构建（较慢但可用） |
| `asset.missing = 1` | 播放时提示"关联的视频文件已不在 NAS 上"，给出原路径与"重新指定"入口 |
| 参考图 sha256 变化 | 标记该 photo 需重新入库，扫描时仍尝试命中但提示特征可能已过期 |
| Range 请求被中间层剥掉 | `resolve` 返回 `supportsRange`；为 false 时 ExoPlayer 禁用 seek 并提示 |
| 视频 404 / 解析失败 | 显示静态提示叠加层，保留 AR 跟踪框，**不崩** |
| 上传时走在 Tunnel 上 | 隐藏入口并说明原因（见 §9.4），不要让它先传 100MB 再 413 |
| 机型不支持 ARCore | 启动时 `ArCoreApk.checkAvailability()`。不支持则退化为"识别后全屏播放"，功能不丢 |
| 入库质量分 < 75 | **拒绝**，返回分数与建议 |
| `fs/list` 路径越界 | 403，且记录日志（正常客户端不会产生，出现即为异常） |

## 14. 测试策略

CV 部分绝不能靠真机手测，否则每次调参都是赌博。

### 14.1 合成查询图回归测试（最重要）

对参考图做随机变换自动生成查询样本：
- 四点透视变换（0-40°）
- 高斯模糊（σ 0-1.5，模拟手抖与失焦）
- 亮度 ±30% / 色温 ±500K
- 局部高光斑（模拟覆膜反光）
- JPEG 压缩 q50-85

每张参考图生成 20 个样本，全自动跑指标。**无需真机、无需网络。**

### 14.2 验收基线

对每个合成样本，结果必属于且仅属于以下三类之一，三者之和恒为 100%：

- **正确命中**：`matched=true` 且 `photoId` 等于来源照片
- **误识别**：`matched=true` 但 `photoId` 不等于来源照片
- **漏检**：`matched=false`

| 指标 | 目标 |
|---|---|
| 正确命中率（1 万张库） | ≥ 95% |
| 误识别率 | ≤ 0.1% |
| 漏检率 | ≤ 4.9% |
| 服务端查询延迟 P95（N5095 实测） | ≤ 80ms |

**误识别率比漏检率重要一个数量级**。漏检只是让用户多举一秒手机；播错视频是严重的体验事故（在家人面前扫出别人的视频）。§8.3 第三条判定就是专为压住这个指标而设。调参时若两者冲突，**一律牺牲漏检率保误识别率**。

### 14.3 单元测试

- `recognizer`：描述子提取的确定性、fbow 查询、几何校验的判定边界
- **`fs-browser` 路径穿越**：`..`、URL 编码的 `..`、符号链接指向白名单外、绝对路径、Windows 风格分隔符。这是唯一暴露文件系统的接口，必须专门测
- `asset` 引用完整性：mtime 变而 sha256 不变、sha256 变、文件消失、文件恢复
- `media-resolve` 策略链：各策略命中顺序、直链不缓存
- Range 请求：单区间、多区间、超界、`bytes=0-`、`bytes=-500`
- `ingest`：质量分拒绝、路径去重、时长截断、原片已合规时跳过转码

### 14.4 集成测试

- `EndpointResolver`：多 endpoint 各种通/不通组合下的 api/media 选择结果（表驱动，覆盖 §9.3 全部场景）
- 用 fake 文件树 + fake 挂载点测完整入库→识别→解析→取流闭环，不依赖真实 NAS 或网盘

### 14.5 真机手测清单

AR 渲染与跟踪只能真机验证：不同角度、不同光照、覆膜反光、照片弯折、快速移动、跟踪丢失恢复、视频续播、连续扫描不同照片、切换 WiFi/移动网络时的 endpoint 重新探活。

## 15. 分阶段交付

| 阶段 | 内容 | 出口条件 |
|---|---|---|
| **Phase 0** | `recognizer` + `ingest` + 合成查询图回归测试。**纯离线，不含服务、不含 App** | 达到 §14.2 全部基线。**这是生死线** —— 见下方状态 |
| Phase 1 | `photo-ar-server`（catalog + recognize + fs-browser + media-resolve + file-serve）+ 加一条 cloudflared ingress | 能用 curl 完成入库→识别→取流；路径穿越测试通过 |
| Phase 2 | 纯原生 Android Activity：扫描 → 识别 → 单目标跟踪 → 播放 | 真机 AR 体验可接受 |
| Phase 3 | ~~Flutter~~ Compose 外壳 + `EndpointResolver` + NAS 文件浏览与关联 + 历史 | 可日常使用 —— 见下方状态 |
| Phase 4 | 端侧缓存索引（最近 200 张离线秒识别）+ 视频本地 LRU 缓存 | 常扫照片离线可用 |

**Phase 0 必须先单独完成。** 它成本很小（纯离线代码 + 自动化测试，不碰 NAS 部署、不写 App），却能提前证伪整个项目最大的风险：上万张高度自相似的家庭照片能否被可靠区分。若识别率不达标，后续所有工作都是沉没成本。

### Phase 0 出口条件的实际达成情况（2026-07-29）

Oxford5k 4385 张入库、20000 次库内查询 + 9740 次库外查询，`MIN_INLIERS=40`：

| §14.2 指标 | 目标 | 实测 | |
|---|---|---|---|
| 正确命中率 | ≥ 95% | **95.70%** | ✅ |
| 误识别率（库内） | ≤ 0.1% | **0.010%** | ✅ |
| 漏检率 | ≤ 4.9% | **4.29%** | ✅ |
| 查询延迟 P95 | ≤ 80ms | 67.7ms（**开发机，非 N5095**） | ⚠️ 待复测 |

库外（不在库里的照片）误识别率是 §14.2 之外追加的一项，因为它才是"扫到别人的照片会不会播错视频"的直接度量。**3.963%**，逐对归类后 100% 属于"同一被摄物体的不同照片"（Oxford5k 就是同一批地标的大量不同视角照片），**真实误识别 0 条**。

所以出口条件的判读是：§14.2 四项里三项达标、P95 需要在 N5095 上复测；库外那一项在这份语料上不可能达标，但缺陷成分已归零。**尚未覆盖**：真正的域外输入（杂志页 / 屏幕截图 / 纯文字 / 手绘）、质量分闸门开启后的重测。详见 `docs/superpowers/plans/phase0-results.md`。

### Phase 1 出口条件的实际达成情况（2026-07-30）

出口条件「能用 curl 完成入库→识别→取流；路径穿越测试通过」：**达成**。

| 验证方式 | 结果 |
|---|---|
| `bench/e2e_curl.sh`（真进程 + 真 curl + 真 vocab/ffmpeg/arcoreimg） | 62 项全绿，含 14 项路径穿越 |
| `tests/server` | 156 个测试全绿（全仓库 398） |
| `docker build` + `docker run` | ping / 401 / 403 穿越 / 入库（质量分 95）/ 识别（inliers 83）/ healthcheck healthy |

**未覆盖**：N5095 上的 P95 复测、一万张规模的服务端入库。过程中修掉两个不报错
的真缺陷（识别库 slot 错位、上传后 keep-alive 读死连接）。详见
`docs/superpowers/plans/2026-07-30-phase1-server.md`。

### Phase 2 的实际状态（2026-07-30）

出口条件「真机 AR 体验可接受」：**未达成，且当前无法判定** —— 手上没有 Android
真机，ARCore 的 Augmented Images 不能在模拟器上跑。

| 验证方式 | 结果 |
|---|---|
| `:arview:testDebugUnitTest` | 115 个全绿（状态机 43 / 解析 27 / 客户端 18 / 几何 15 / 抽帧 12） |
| `:app:assembleDebug` | BUILD SUCCESSFUL，`app-debug.apk` 4.77MB |
| 真机 AR 体验 | **一次都没跑过**，§14.5 手测清单一条未执行 |

代码分层刻意把全部判断收进不 import android 的 `ScanController`（450 行），
ARCore / GLES / 相机 / 播放各层只做搬运。所以「未验证」的具体是：跟踪稳定性、
四边形贴合精度、羽化与淡入的观感、端到端延迟、无 ARCore 机型的兜底、真机上
`org.json` 的空值行为。**Phase 2 记作代码完成、出口条件挂起。** 详见
`docs/superpowers/plans/2026-07-30-phase2-arview.md`。

### Phase 3 的实际状态（2026-07-30）

出口条件「可日常使用」：**未达成，且当前无法判定** —— 与 Phase 2 同一个理由，
没有 Android 真机。**外壳改用 Kotlin + Jetpack Compose，不是 §5.8 原写的 Flutter**
（理由见 §5.8 与 §17）。

| 验证方式 | 结果 |
|---|---|
| `./gradlew testDebugUnitTest` | 239 个全绿（endpoint 43 / 状态机 43 / 目录解析 30 / 客户端 30 / API 解析 27 / 格式化 26 / 几何 15 / 探活 13 / 抽帧 12）。全仓库 637 |
| `./gradlew assembleDebug` | BUILD SUCCESSFUL，`app-debug.apk` 11.7MB / 9 个 dex |
| 真机 | **一次都没跑过**，§14.5 手测清单依然一条未执行 |

六个界面（照片 / 详情 / 浏览 / 入库 / 历史 / 设置）+ §9 全套探活都写完了，但
`@Composable` 一行都没在设备上渲染过。所以「未验证」的具体是：实际布局与手感、
一次真实入库的闭环、缩略图网格的内存与流畅度、真网络上的四通道探活与切网重探、
以及**照片方向 → 打印宽度是否真取对了边**（这一项错了不报错，只会让 AR 一直飘）。

**刻意没做**：`POST /v1/upload` 的 SAF 选文件流程（§7 自标「可选路径」，且按 §9.4
只在非隧道通道可用）、缓存管理入口（归 Phase 4）、删除照片（服务端整张路由表里
没有 DELETE，不在 §7 范围内）。

**Phase 3 记作代码完成、出口条件挂起**，与 Phase 2 同一状态 —— 两者的出口条件
在同一次真机上手里一起判。详见 `docs/superpowers/plans/2026-07-30-phase3-shell.md`。

## 16. 风险与已知限制

| 风险 | 影响 | 缓解 |
|---|---|---|
| **识别率不达标** | 项目不成立 | Phase 0 提前证伪。备选路径：自训词汇树 → 全局描述子粗排 → 最终兜底为照片背面印二维码 |
| 照片质量分低（大片天空、纯色背景、过曝） | 该照片无法可靠跟踪 | 入库即拒绝并提示；必要时给照片加细纹理边框 |
| NAS 上的文件被用户移动/删除 | asset 引用失效 | mtime+sha256 校验 + 界面标红 + 手动重新指定。不做自动重绑定 |
| Cloudflare Tunnel 的 ToS 灰区 | 理论上可能被要求整改 | 本项目流量约 0.9GB/月，且不开 CDN 缓存；媒体首选 LAN/Tailscale，隧道仅兜底。真被限制则把 media 通道切成 Tailscale-only，改配置即可，无需改代码 |
| Cloudflare 100MB 请求体上限 | 隧道上传原片会 413 | §9.4：上传只在非隧道通道开放。正常用法（关联 NAS 已有文件）不涉及上传 |
| Tailscale 依赖手机端登录状态 | 在外时 media 可能落回隧道 | 已在 §9.3 建模为正常场景，不是故障 |
| 实物照片退化（裁切、覆膜反光、弯折） | 跟踪抖动 | 无法根治，所有同类产品（含小米原版）皆如此。羽化淡入让抖动视觉上不明显 |
| ARCore 更换目标库导致 session 重置 | 状态机竞态 | 显式 `LOADING_TARGET` 中间态 |
| `arcoreimg` 是闭源二进制 | 依赖 Google 供给 | 已生成的产物可长期使用；极端情况改用端上 `addImage()` 运行时构建 |
| 网盘直链的账号风险 | 封号不可解除 | §10 已明确：阿里云盘禁用，仅 123云盘官方允许；且直链默认关闭 |
| 8GB 内存在 5 万张时不够 | 扩展上限 | 扩到 16GB（TS-464C2 支持）可到 10 万张以上 |

## 17. 技术选型依据

| 选择 | 替代方案 | 为什么这样选 |
|---|---|---|
| ARCore Augmented Images | MindAR(Web)、ARKit、Vuforia | 单库 1000 张但**库数量不限**，配合"云识别 + 单目标下发"即无上限；有 `arcoreimg` 离线预生成；跟踪质量优于 Web AR。MindAR 多目标会崩移动浏览器（[issue #22](https://github.com/hiukim/mind-ar-js/issues/22)）；ARKit 需 $99/年账号 |
| ~~[SceneView/sceneform-android](https://github.com/SceneView/sceneform-android)~~ → **裸 ARCore + 手写 GLES 2.0**（Phase 2 改） | SceneView、已归档的 Google Sceneform、裸 Filament | 原选 SceneView 是因为它原生支持 Augmented Images + `ExternalTexture`。Phase 2 实做时改掉了：§11.8 的羽化+淡入需要自定义 fragment shader，Filament 材质得用 `matc` 离线编译成 `.filamat`（等于再加一个 Google 闭源工具）；而整个场景只有两个四边形，用不上场景图/光照/PBR/glTF，为它背 ~10MB 不划算；`SurfaceTexture` → `GL_TEXTURE_EXTERNAL_OES` 也是 ExoPlayer 出图最直的路。代价是 EGL 生命周期、`setDisplayGeometry`、`getTransformMatrix` 全得自己管对，`gl/` 共 546 行 |
| ~~Flutter 外壳~~ → **Kotlin + Jetpack Compose**（Phase 3 改） | Flutter + MethodChannel/EventChannel、React Native、原生 View/XML | 地基是 ARCore，只有 Android，Flutter 的跨平台价值为零；代价却是把 §7 契约实现两遍（Dart 重写请求与解析，或把每个 API 都包成 MethodChannel，六个界面的每次列目录/取缩略图都过桥）。`EndpointResolver` 按 §5.7 本就在原生侧，「当前走哪条通道」这个全局状态还得再推过去；缩略图要带 `Authorization` 头，`Image.network` 用不了，"控件生态省事"也不成立。改 Compose 后同进程直接调用，无桥、无第二份契约。代价是 `@Composable` 层跑不了 JVM 单测，故把易错的格式化与校验抠进纯函数 `Fmt.kt` |
| **打印尺寸按纸张预设 + 手输毫米（10–2000）** | 只给手输、或从 EXIF/DPI 反推 | `print_width_m` 是参考图**水平方向**的物理宽度，横放取长边、竖放取短边（6寸 = 152 / 102），方向由缩略图像素比判定。填错**不会报错**，只会让 AR 里的视频一直飘 —— 所以既要预设降低出错率，也要范围校验挡住笔误。EXIF 里没有实物打印尺寸，反推不出来 |
| **缩略图自己解码 + `LruCache`** | Coil、Glide | 每张图要带 `Authorization: Bearer`，且 api/media 是两条会各自变化的通道，缓存键得绕开 URL。配图片库的 header 注入 + 自定义客户端 + 自定义键，工作量不比 `BitmapFactory` + 8MB `LruCache` 少，还多一棵依赖树 |
| [fbow](https://github.com/rmsalinas/fbow) / [DBoW3](https://github.com/rmsalinas/DBow3) | FAISS + CLIP、pHash | ORB 二进制描述子 + 层次词汇树，纯 CPU 万级库查询 10-50ms，ORB-SLAM 同款。pHash 抗不住透视与光照；CLIP 在 N5095 上偏慢且对裁切敏感 |
| **识别跑在 NAS** | ECS 2C2G | N5095 是 4C@2.9GHz / 8G 可扩 16G，明显强于 ECS；数据本就在 NAS，无需同步；识别包仅 50KB，走隧道毫无压力。**这一条让 ECS 完全退出，并消除了 v1 的双库同步问题** |
| **媒体存 NAS，不上云** | Cloudflare R2 + AES-CTR 加密 + 冷热分层 | 存储与带宽两个瓶颈都不存在（§10.1）。不出内网 → 不需要加密 → 不需要分层 → 不需要密钥体系。**v1 约 60% 的复杂度来自一个不存在的问题** |
| **API / 媒体双通道独立探活** | 单一固定地址 | 两者的数据量差 30 倍、约束完全不同（隧道有 100MB 上限、Tailscale 依赖登录态）。分开后各取所长，且"可配置 URL 前缀"天然是列表里的一项 |
| **asset 引用而非复制** | 导入时复制到自有目录 | 用户明确要求复用 NAS 已有文件。复制会让上万张照片占双倍空间，且原图更新后不同步 |
| **网盘经 CloudDrive2 挂载点接入** | 直接对接网盘 API / OpenList 302 | 用户已在跑 CloudDrive2 → 网盘文件就是普通路径，零代码、零会员、零 ToS 风险。直链保留为可选优化 |
| SQLite 单库 | Postgres、双库同步 | 单机单进程，无并发写压力。v1 的双库同步纯粹是 ECS 参与带来的负担 |

## 18. 参考资料

- [ARCore Augmented Images](https://developers.google.com/ar/develop/augmented-images) — 单库 1000 张、库数量不限、同时跟踪 20 张、支持运行时 `addImage()`
- [arcoreimg 工具](https://developers.google.com/ar/develop/augmented-images/arcoreimg) — `build-db` 离线构建、`eval-img` 质量分（建议 ≥75）
- [SceneView/sceneform-android](https://github.com/SceneView/sceneform-android)
- [ARCore + Sceneform 视频播放实践](https://medium.com/krootl/bring-images-to-life-with-arcore-and-sceneform-simple-video-playback-3fe2f909bfbc)
- [fbow](https://github.com/rmsalinas/fbow) / [DBoW3](https://github.com/rmsalinas/DBow3)
- [OpenList](https://github.com/OpenListTeam/OpenList) — AList 的社区分叉；[AList 出售与代码采集争议](https://github.com/orgs/OpenListTeam/discussions/73)
- [OpenList WebDAV 302 策略文档](https://doc.oplist.org/guide/drivers/webdav)
- [阿里云盘驱动的账号风险条款](https://doc.oplist.org/guide/drivers/aliyundrive_open)
- [123云盘官方直链功能](https://www.123pan.com/)
- [CloudDrive2](https://www.clouddrive2.com/download.html) — 直链为会员功能
- [Cloudflare ToS 更新（2023-05）](https://blog.cloudflare.com/updated-tos)
- [Cloudflare Tunnel 媒体流的实际执行情况](https://www.xda-developers.com/cloudflare-tunnels-are-great-but-never-use-them-for-media-streaming/)
- 内部文档：《ECS + NAS(QNAP) + fnOS 内网穿透与服务总文档（合并版 · 2026-07）》
