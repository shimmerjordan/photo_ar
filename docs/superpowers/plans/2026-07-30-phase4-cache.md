# Phase 4：端侧缓存与离线识别（2026-07-30 代码完成）

出口条件（spec §15）：**常扫照片离线可用。**
结论：**未达成，也无法在此判定** —— 和 Phase 2/3 同一个理由，手上没有 Android
真机。这一阶段比前两个阶段更依赖真机：整条链路的最后一步是 ARCore 在真实相机帧里
认出一张由 640px 缩略图建成的目标，而那恰好是模拟器上跑不了的部分。交付的是
「离线这一路的全部判断逻辑写完、能被 JVM 单测覆盖的部分全绿、APK 编得出来」。

**对 spec 的偏离一处（三个方面）：离线识别不用 ORB，改走 ARCore 多图库。**
理由见下方《为什么不在端上做 ORB》。spec §11.3 加了修订小节、§17 加了一行、
§15 加了状态段。

## 交付内容

六批，从纯函数往上，每一批都是先测再接线：

| 批次 | 内容 | 位置 | 测试 |
|---|---|---|---|
| 4-1 | 索引类型 + JSON 编解码（`CacheIndex.kt`） | `:arview` | 27 |
| 4-2 | 缓存计划与 LRU 淘汰，全纯函数（`CachePlan.kt`） | `:arview` | 33 |
| 4-3 | 落盘（`PhotoCache.kt`）+ 同步执行（`CacheSync.kt`） | `:arview` | 23 + 26 |
| 4-4 | 状态机离线命中 + ARCore 多图库接线（`LocalTargetDb.kt`） | `:arview` | +11 |
| 4-5 | 缓存管理页（`CacheScreen.kt` / `CacheSettings.kt`） | `:app` | 7 |
| 4-6 | 全绿 + 本文档 + spec 状态 | — | — |

新增 8 个主代码文件（1632 行）+ 5 个测试文件（1535 行），改动 8 个已有主文件
+ 1 个已有测试文件。**没有新增任何第三方依赖** —— 这是「不用 OpenCV」那个决定
最直接的结果。

### 分层

和 Phase 2/3 同一条规矩：能不 `import android.*` 的都不 import。

| 层 | 文件 | 能不能 JVM 单测 |
|---|---|---|
| 纯类型与编解码 | `CacheIndex.kt`（`CachedPhoto` / `CacheStats` / `CacheIndexCodec`） | 能 |
| 纯决策 | `CachePlan.kt`（`CacheSpec` / `CachePlanner`：该下什么、该淘汰什么） | 能 |
| 纯钳位 | `CacheSettings.kt`（两个上限 → `CacheSpec`） | 能 |
| 落盘 | `PhotoCache.kt`（`java.io`，无 android） | 能（真临时目录真读写） |
| 编排 | `CacheSync.kt`（拉列表 → 算计划 → 执行，同步阻塞） | 能（假 client + 真临时目录） |
| ARCore | `LocalTargetDb.kt`（`AugmentedImageDatabase` / `BitmapFactory`） | **不能** |
| 界面 | `CacheScreen.kt` | **不能** |

所以「离线该下哪些、该淘汰谁、索引怎么落盘、同步半路断了怎么办」全部有测试；
没测试的只有「Bitmap 解出来喂给 ARCore」和「Compose 怎么画」这两件搬运工作。

## 为什么不在端上做 ORB

spec §11.3 第 3 条原话：「先查本地缓存索引（最近 200 张的 ORB 描述子，约 2MB）。
命中则完全跳过网络。」

照着做要付两笔：

1. **OpenCV 进 APK。** 每个 ABI 几十 MB 的 `.so`。当前 debug APK 一共 11.7MB。
2. **`recognizer.py` 的两阶段管线用 Kotlin 重写一份。** 而重写出来的那份**没有
   任何办法验证** —— §14.1 那套合成查询图回归测试（Phase 0 的生死线，20000 次
   查询）跑在 Python 侧，Kotlin 版只能靠肉眼看和真机试。两份识别实现给出不同
   答案，是那种查起来极贵的缺陷。

而 ARCore 本来就在**每帧连续**做图像识别 —— 那正是 Augmented Images 的定义。
所以改成：把缓存里那 200 张缩略图 `addImage(photoId, bitmap, printWidthM)` 建成一个
`AugmentedImageDatabase`，`serialize()` 到 `local.imgdb`，扫描开始时整库装进 session。

得到的：

- 抽帧 + 本地匹配那一层**整个不需要了**。离线时不抽帧、不算描述子、不比对。
- `AugmentedImage.name` 直接就是 `photoId`（Phase 2 已经这么用），命中即拿到 id。
- 物理宽度照旧从 `addImage` 传进去，§11.3 的「物理尺寸红利」一点没丢。
- 识别和跟踪合成一步：不存在「本地认出来了但 ARCore 跟不上」这种状态。

代价，说清楚：640px 缩略图提出来的特征比服务端 `arcoreimg build-db` 用原图建的弱
一档，也就是已有的 `IMGDB_FALLBACK` 那种降级。所以一次本地命中是**「质量降一档，
但完全不用网络」**，界面上直说：「离线识别（本地缓存），贴合可能略有偏差」。
把这句话露给用户是刻意的 —— 贴合略偏时人得知道原因，否则会以为 App 坏了。

顺带，这正好是 spec §16 里「`arcoreimg` 是闭源二进制 → 极端情况改用端上
`addImage()` 运行时构建」那条缓解措施的提前落地：现在这条路是实跑的，不再是纸上的
备选。

## 另外两个偏离，都是被现实逼出来的

### 建库推迟到下次扫描启动

`AugmentedImageDatabase(session)` 的构造要一个 ARCore `Session`。而「缓存管理」页点
「现在同步」时相机没开，也可能连相机权限都还没给。为建库单独造一个 Session 是错的：
那要相机权限、要 ARCore 装着，而这两件事跟「把文件下下来」毫无关系 —— 用户点的是
「同步」，不该弹相机权限。

所以顺序反过来：

```
同步（任何时候，无相机）      下缩略图 → invalidate()：删掉 local.imgdb
下一次扫描启动（有 session）  后台线程 rebuildIfStale() → GL 线程 install()
```

`CacheSync` 收一个 `rebuildTargetDb` 回调，真机上传的是
`LocalTargetDb.deferredRebuild()` —— 它只 `invalidate()`，然后报
`accepted = usable.size`，含义是「将会进库」而不是「已进库」。这个区分在界面上照实
写成「可离线识别 N 张」。

代价是刚同步完的第一次扫描要多花几百毫秒建库。一次性的，且发生在用户举起手机对准
照片之前的那段时间里。

**建与装必须分成两个方法。** `rebuildIfStale` 只需要 Session 拿原生上下文，不碰
`configure()`，能和 GL 线程上的 `session.update()` 并发跑；只有 `install` 里那次
`session.configure()` 必须回 GL 线程。合成一个「确保装好」会把几百毫秒的特征提取压到
GL 线程上，表现为启动扫描时预览卡一下。

### 过期判定用文件 mtime，不另存 dirty 标记

`local.imgdb` 比 `index.json` 旧就是过期。多一个标记文件就多一个会和现实不一致的
状态（标记写成功而库没写成功，或反过来），而那种不一致的表现是离线识别**静默**失效。
文件 mtime 是内核维护的，不会漏。

## 先想到、再用测试或注释锁住的失败模式

这一阶段的缺陷几乎都属于同一类：**不报错，只是行为不对**。逐条记下来，因为它们
不是测出来的，是写的时候盯出来的。

| 失败模式 | 会怎么表现 | 怎么挡住 |
|---|---|---|
| `PhotoCache` 有两个实例（扫描页一个、缓存页一个） | 「刚同步完的 47 张，回到扫描页又变回 0 张」 | `OfflineCache` 进程内单例 |
| 缓存根目录放 `cacheDir` | 系统清缓存后离线就用不了了 —— 而那正是这份缓存唯一的存在理由 | 根目录改 `filesDir`（单张 `.imgdb` 那份短期缓存仍在 `cacheDir`，它丢了只是多一次下载） |
| 退出一张照片后没把多图库装回来 | 「退出第一张照片之后离线识别就没了」 | `releaseTarget` 里 `LocalTargetDb.reinstall`；`install` 除了看 mtime 还看 `holder.multiImageLoaded` |
| 本地命中也去 `loadTarget` | `configure()` 重置 session，把此刻正在跟踪的这张图丢掉 | 本地命中直接进 TRACKING，不经 `LOADING_TARGET` |
| `renderer.setTarget` 只在 `loadTarget` 里调 | 本地命中时 AR 四边形用的是**上一张照片**的物理尺寸 | 挪到 `Matched` 事件里（`ScanRuntime.emit`） |
| `trackedImage` 在 `loadedPhotoId == null` 时返回 null | 扫描阶段永远报不出跟踪 → 离线命中一次都不会发生 | 加 `multiImageLoaded`，这时认任何一张 |
| 「视频没缓存」和「视频放不了」用同一条提示 | 用户对着一个自己能解决的问题干等 | 分成 `VIDEO_NOT_CACHED`（联网就好）和 `VIDEO_UNPLAYABLE`（解决不了）；判据是 `HttpFailure.kind ∈ {TRANSPORT, TIMEOUT}` 或压根不是 `HttpFailure` |
| 「没缓存」这条提示会自己消失 | 消失之后人对着空白照片继续等 | `Notices.transient()` 里它归 false |
| prefs 里存了 0 或一个已下线的选项值 | `CacheSpec` 的 `require(maxPhotos > 0)` 抛在启动路径上 → **一升级就闪退** | `CacheSettings` 取最近一档，7 个测试专盯这个 |
| 2048MB × 1024 × 1024 用 Int 乘 | 溢出成负数 → `require(maxVideoBytes >= 0)` 抛 | `1024L`，且有测试 |

## 缓存下载走 media 通道，超时 60 秒

`PhotoArClient.downloadMedia` 是这一阶段给客户端加的唯一一个方法。两个决定：

- **走 media 通道而不是 api。** 一条视频 1.5–3MB，从隧道拉是给 Cloudflare 白送流量，
  而在家时 `mediaBase` 就是局域网直连（§9 / §10.1）。`MediaInfo.absolute` 为真时
  （`via == "direct_link"`）那条 URL 自带签名，不该再带 `Authorization` —— 所以请求头
  按 `absolute` 分岔。
- **超时 60 秒，不用 `DOWNLOAD_TIMEOUT_MS` 的 10 秒。** 3MB 在 1Mbps 上行下要 24 秒，
  10 秒会把「网慢」判成失败。缓存是后台活儿，慢一点没人等着。

播放侧则是**缓存优先**：`fetchMedia` 先看本地有没有这条视频的字节，有就直接给
本地 `file://`，连媒体元数据那一次往返都省掉。缓存里的字节和联网时拉的是同一个 URL
的同一份内容，画质无差别；而离线时它是唯一能播的东西。

## 「最近 200 张」是哪 200 张

排序键：本地 `lastSeenAt` 倒序 → 服务端 `createdAt` 倒序 → `photoId`。

用「本地最后扫到的时间」而不是入库时间，是因为墙上那张天天扫的照片不该被刚打印的
一批顶掉。代价是每次命中都要 `markSeen`（联网命中也要，否则联网用得越多、离线越
不准），而 `markSeen` 自己不写盘 —— 扫描时每帧都可能命中，落盘放在 `onPause`。

淘汰分两级：视频超预算就只删视频、不动缩略图（缩略图是离线识别的地基，几百 KB 一张；
视频一条 3MB）；条数超上限才整条移出。缓存页上「只清视频」是主按钮、「全清」是文字
按钮，同一个道理，并且「全清」带确认框。

## 测试

```
./gradlew testDebugUnitTest   →  366 全绿
  :arview 333   endpoint 43 / 状态机 54 / CachePlan 33 / 客户端 30 / 目录解析 30 /
                CacheIndex 27 / API 解析 27 / CacheSync 26 / PhotoCache 23 /
                几何 15 / 探活 13 / 抽帧 12
  :app     33   格式化 26 / CacheSettings 7
.venv/bin/python -m pytest    →  398 全绿
./gradlew :app:assembleDebug  →  BUILD SUCCESSFUL，app-debug.apk 11.8MB
```

全仓库 764。状态机那 54 个里有 11 个是这一阶段加的离线命中用例，逐条对着上面那张
失败模式表：离线命中不去装单张目标库 / 不会误报「认出来但没找到」/ 期间不再抽帧 /
参考图过期照样提示 / 视频没缓存时给出能自己解决的提示并继续跟踪 / 已在跟踪别的照片
时不再接受新的离线命中 / 没跟踪上的报告不触发离线命中 / 装库失败被拉黑的照片也不走
离线命中。

`CacheSyncTest` 用假 client + 真临时目录，覆盖的是「半路断网」这类状态：401 或没网
时 `stoppedBy` 非空、`plan` 里剩下的下一轮重来 —— 所以「部分完成」不需要额外记状态。

## 没做的

- **后台自动同步。** 同步只在用户按「现在同步」时跑。「什么时候用流量」该由人决定，
  而缓存过期一天的代价只是扫到新照片时要联网。要加也不该是这一阶段。
- **缩略图的增量校验。** 现在靠字节数比对判「要不要重下」（`printWidthM` / `refStale` /
  `hasVideo` 变了就重下）。`/v1/photos` 不返回 `updatedAt`，所以没有更细的判据；
  真需要的话是服务端先加字段。
- **离线时的照片库浏览。** 缓存页只报数字，不给缩略图墙。断网时照片库那一页仍然是
  空的 —— 离线支持的范围是**扫描**，不是整个 App。

## 未验证的具体项

真机上手时按这个顺序看：

1. **640px 缩略图建出来的库，在真实相机下认不认得出。** 这是整条链路的成败。
2. 认出来之后跟踪比联网时差多少 —— 差多少决定那句「可能略有偏差」够不够诚实。
3. **ARCore 会拒掉多少张自家照片**（纯色、糊、大片天空）。这一项决定「可离线识别
   N 张」和「缓存 N 张」差多远，只能拿真照片量。
4. 200 张建库在真机上到底是几百毫秒还是几秒。超过一秒就得把它挪到进入扫描页之前，
   或者加个「正在准备离线识别」的提示。
5. 缓存页的实际观感、`filesDir` 上几百兆的读写速度。
6. 断网时「视频没缓存」这条提示会不会太频繁 —— 如果常见，说明视频预算的默认 512MB
   偏小。
