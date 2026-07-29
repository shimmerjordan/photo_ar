# Phase 2：原生 Android 扫描视图（2026-07-30 代码完成）

出口条件（spec §15）：**真机 AR 体验可接受。**
结论：**未达成，也无法在此判定** —— 手上没有 Android 真机，ARCore 不能在
模拟器上跑 Augmented Images。本阶段交付的是「代码写完、编译通过、能被单测覆盖
的那部分全绿」，AR 跟踪质量、羽化贴合、视频对位这些必须真机验证的东西一项都
没验过。见文末《出口条件的实际状态》。

## 交付内容

`android/`，两个模块，Gradle 8.12 / AGP 8.7.3 / Kotlin 2.1.0 / compileSdk 35 /
minSdk 24（ARCore 的地板）：

- `:arview` —— library，扫描能力的全部实现，Activity 声明在**它自己的清单**里，
  Phase 3 的 Flutter 外壳把它 include 进来就自动有了。
- `:app` —— 独立装机壳，一个填三个值的表单。只为 Phase 2 能单独装到真机上
  验 AR，Phase 3 由 Flutter 外壳取代。

第三方依赖只有两个：`com.google.ar:core:1.54.0`、`androidx.media3:*:1.5.1`。
HTTP 用 `HttpURLConnection`、相机兜底用手写 Camera2、JSON 用 `org.json`（平台自带），
与本仓库既有的零依赖取舍一致。

### 分层

| 层 | 文件 | 能否 JVM 单测 |
|---|---|---|
| 纯逻辑 | `Frames.kt` `Geometry.kt` `Api.kt` `ScanController.kt` `net/PhotoArClient.kt` | ✅ 115 个测试 |
| ARCore | `ar/ArAvailability.kt` `ar/ArSessionHolder.kt` `ar/TargetLoader.kt` | ❌ 需真机 |
| GLES | `gl/GlUtil.kt` `gl/CameraBackground.kt` `gl/VideoQuad.kt` `gl/ArRenderer.kt` | ❌ 需真机 |
| 相机/播放 | `camera/FrameGrabber.kt` `camera/Camera2Source.kt` `media/VideoPlayer.kt` `media/VideoTexture.kt` | ❌ 需真机 |
| 接线 | `ScanRuntime.kt` `ui/ArScanActivity.kt` `ui/Notices.kt` | ❌ 需真机 |

这个划分是刻意的：**所有判断都在 `ScanController`（450 行，43 个测试）里**，
它不 import 任何 android 包，只通过 `ScanEffects` 接口对外发号施令。真机上剩下
要排查的就只有搬运和线程切换。

## 四个设计决定

### 1. 裸 ARCore + 手写 GLES 2.0，而不是 spec §17 选的 SceneView

**这是对 spec 的偏离，理由如下：**

- §11.8 要求视频四边形边缘羽化 + 淡入。这需要自定义 fragment shader；Filament
  的材质要用 `matc` 离线编译成 `.filamat` 二进制，等于在构建链里再加一个 Google
  的闭源工具（`arcoreimg` 已经是一个了）。手写 GLES 的话，羽化就是 shader 里
  三行 `smoothstep`。
- 整个场景只有**两个四边形**（相机背景 + 视频），完全用不上场景图、光照、PBR、
  glTF 加载。为这两个四边形背 ~10MB 的引擎不划算。
- `SurfaceTexture` → `GL_TEXTURE_EXTERNAL_OES` 是 ExoPlayer 出图最直的一条路；
  经过 Filament 的 `ExternalTexture` 反而多一层要对齐的抽象。

代价：`setDisplayGeometry`、`getTransformMatrix`、EGL 生命周期都得自己管对。
`gl/` 四个文件合起来 546 行，可控。

### 2. 目标库用服务端预建的 `.imgdb` `deserialize`，不用端上 `addImage`

一开始写错了，以为端上要拿缩略图 `addImage(name, bitmap, widthM)`。查服务端代码
（`quality.build_single_target_db`）才发现：入库时 `arcoreimg build-db` 的清单行是
`f"{name}|{image_path}|{print_width_m:.6f}"`，而 `ingest.py` 传的 `name=photo_id`。

所以：

- 物理宽度**已经烘进 `.imgdb`**（§11.7 的红利），端上 `deserialize` 出来自带正确
  尺寸，四边形一上来就是对的大小，不会在跟踪中忽大忽小。
- 跟踪到的 `AugmentedImage.name` **就是 photoId**，`trackedImage()` 直接按它比对，
  不需要另设常量。
- `addImage` 只留作 `.imgdb` 下不来时的缩略图降级路径（会给 `IMGDB_FALLBACK` 提示）。

`.imgdb` 单张约 4.3KB，走 api 通道下载并按 photoId 落盘缓存。

### 3. 没有 ARCore 的机型不砍功能，改走手写 Camera2

§13 要求退化成「识别后全屏播放」。问题是没有 ARCore 就没有
`Frame.acquireCameraImage()`，抽帧的来源就没了。所以写了 `Camera2Source`：
ImageReader 出 YUV_420_888，喂**同一个** `ScanController`，命中后第二个
SurfaceView 全屏播（按 `onVideoSizeChanged` 等比摆放，不拉伸）。

宁可多 190 行也不在不支持的机型上静默丢掉扫描能力。CameraX 能省这些代码，但
要多背一个依赖树，而这里只需要「一路预览 + 一路 YUV」。

### 4. 抽帧不旋转、不缩放

- **不旋转**：查了服务端 `recognizer.py`，它没有任何朝向处理，用的是旋转不变的
  局部特征 + 单应性校验。转一遍是纯浪费 CPU。原先 `toJpeg` 有个
  `rotationDegrees` 参数，结尾是 `if (rotationDegrees == 0) jpeg else jpeg` ——
  比没有这个参数更糟，删了。
- **不缩放**：`YuvImage.compressToJpeg` 只能裁，裁会改 FOV（照片可能被裁出画面）。
  所以尺寸在源头定死：ARCore 那条走 `Frames.targetSize` 保证长边 640，Camera2
  那条在 `bestSize` 里就挑最接近 640 的输出尺寸。

抽帧参数与 Phase 0 的全部基线完全一致：**400ms 一帧、长边 640、JPEG q70（约 50KB）**。

## 真机上会咬人的六个点（都已处理）

| 坑 | 处理 |
|---|---|
| `frame.acquireCameraImage()` 有并发上限，漏关一个之后**永远**拿不到帧 | `finally { image?.close() }` |
| `SurfaceTexture.getTransformMatrix` 不乘，视频上下颠倒 | vertex shader 里乘 `uStMatrix` |
| 视频隐藏时不抽纹理，SurfaceTexture 队列填满 → 解码器卡死成一张定格 | `onDrawFrame` 无条件 pump，只用 `showVideo` 控制画不画 |
| `session.resume()` 重复调会抛；`onResume` 与 `onGlReady` 两条路都会到 | `ArSessionHolder.resume()` 里 `if (!paused) return true`，两条路合并进 `resumeAndScan()` |
| `Config.augmentedImageDatabase` 的 setter **不接受 null**，没法「清空」 | 新建一个 `Config` 不设库就是空的 |
| `session.configure()` 与 GL 线程上的 `session.update()` 并发行为无保证 | 所有 `configure` 用 `glView.queueEvent` 排到 GL 线程 |

另外两个已知的语言/平台陷阱：

- **`Config.UpdateMode.BLOCKING`**：让 `update()` 卡到有新相机帧，
  `RENDERMODE_CONTINUOUSLY` 就被相机的 30fps 自然限住，不会 60fps 空转重画同一帧。
- **`org.json` 的双实现分歧**：Android 上 `optString(name, null)` 遇到 JSON null
  返回**字符串 `"null"`**，Maven 的 `org.json:json:20240303` 返回 fallback。单测跑在
  后者上，真机跑在前者上。全部改用基于 `isNull()` 的 `str()` 助手，并专门写了
  `"reason":null` 的测试钉住。

## 验证到哪一步

| 检查 | 结果 |
|---|---|
| `:arview:testDebugUnitTest` | **115 个全绿，0 失败** |
| `:app:assembleDebug` | BUILD SUCCESSFUL，`app-debug.apk` 4.77MB / 8 个 dex |
| 合并清单 | `SetupActivity` / `ui.ArScanActivity` / ARCore `InstallActivity` 三个都在 |
| 真机 | **一次都没跑过** |

115 个测试的分布：

| 套件 | 数量 | 覆盖 |
|---|---|---|
| `ScanControllerTest` | 43 | §11 状态机全部转移、§11.6 命中后停抽帧、丢失 10s 才恢复、退避、拉黑 |
| `ApiParseTest` | 27 | §7 两个响应的每个字段、缺字段、JSON null、非 JSON 体 |
| `PhotoArClientTest` | 18 | 通道分离（api/media）、超时常量、Bearer、每次调用重读端点、状态码到 `NetErrorKind` |
| `GeometryTest` | 15 | §11.7 印刷尺寸、§11.8 fill 裁切 uv |
| `FramesTest` | 12 | 长边 640、偶数化、NV21 逐字节（含 rowStride/pixelStride 补齐） |

`FramesTest` 里那个 `1440x1079` 的用例值得一提：长边缩到 640 时短边算出来是
479.6，取整成 479 是奇数，YUV 的 UV 平面就没法配对。所以 `targetSize` 强制偶数。

## 出口条件的实际状态

「真机 AR 体验可接受」——**未验证**。以下全部只是「代码看着对」，不是「跑起来对」：

| 项 | 状态 |
|---|---|
| ARCore 跟踪单张照片的稳定性 | 未验 |
| 视频四边形与实物照片的贴合精度 | 未验（`Geometry` 的算术有单测，但 `centerPose` → 模型矩阵那步没有） |
| 羽化 + 淡入的观感 | 未验（shader 从没在 GPU 上编译过） |
| 端到端延迟（对准 → 出画面） | 未验 |
| 覆膜反光 / 弯折 / 快速移动 / 跟踪丢失恢复 | 未验，§14.5 清单一条都没跑 |
| 无 ARCore 机型的全屏兜底 | 未验 |
| 真机上的 `org.json` 行为 | 未验（已按分歧写代码，但没在 Android 上确认过） |

拿到真机后要做的第一件事就是 §14.5 那份清单。在那之前，Phase 2 只能记作
**代码完成，出口条件挂起**。

## 下一步（Phase 3）

Flutter 外壳 + `EndpointResolver` + NAS 文件浏览与关联 + 历史。`EndpointResolver`
是 §9.3 那张表，纯逻辑、表驱动可测，正好补上 Phase 2 里 `requestEndpointRefresh()`
留的那个空实现。
