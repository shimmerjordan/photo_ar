# Phase 3：App 外壳 + EndpointResolver（2026-07-30 代码完成）

出口条件（spec §15）：**可日常使用。**
结论：**未达成，也无法在此判定** —— 和 Phase 2 同一个理由，手上没有 Android
真机。这一阶段交付的是「六个界面写完、编译通过、能被 JVM 单测覆盖的那部分全绿」。
「日常使用」要求的东西（真机上点得通、扫得出、播得动）一项都没验过。

**对 spec 的偏离一处：外壳用 Kotlin + Jetpack Compose，不是 §5.8 写的 Flutter。**
理由见下方《为什么不用 Flutter》，spec §5.8 / §17 已同步改掉。

## 交付内容

四批，从底往上：

| 批次 | 内容 | 位置 |
|---|---|---|
| 3-1 | `EndpointResolver` —— §9.1 候选配置 + §9.2 探活与选择 + §9.3 那张表 | `:arview` |
| 3-2 | 客户端补齐 photos / detail / fs.list / fs.thumb / history / photo / attach | `:arview` |
| 3-3 | 配置持久化 + 真 `HttpProber` + 接进 `ScanRuntime` | `:arview` |
| 3-4 | 六页 Compose 外壳 + `Fmt` 纯函数层 | `:app` |

新增 17 个主代码文件（2736 行）+ 4 个测试文件（1268 行），另删掉 Phase 2 那个
临时装机壳 `SetupActivity`。第三方依赖只加了 Compose 一族
（`compose-bom:2024.12.01` → ui 1.7.6 / material3 1.3.1、`activity-compose:1.9.3`），
**没有加 Coil、Glide、navigation-compose、Retrofit、OkHttp 里的任何一个**，理由都在下面。

### 六个界面

| 页 | 文件 | 干什么 |
|---|---|---|
| 照片 | `PhotosScreen.kt` | 网格；标出「无视频」「参考图变了」；右下角「扫一扫」进 `ArScanActivity` |
| 详情 | `PhotoDetailScreen.kt` | §8.4 的三种引用完整性问题各一条横幅；换/配视频 |
| 浏览 | `BrowseScreen.kt` | NAS 文件树，一层一页；按用途过滤（选图 / 选视频） |
| 入库 | `CreateScreen.kt` | 打印尺寸预设 + 手输毫米 + 关联视频 + 结果卡（质量分、索引大小、耗时） |
| 历史 | `HistoryScreen.kt` | 命中与未命中都列，带 inliers / 延迟 / 走的哪条通道 |
| 设置 | `SettingsScreen.kt` | §9.1 那份配置可编辑，每条通道后面贴探活原文 |

导航是 `Shell.kt` 里一个 `mutableStateListOf<Route>`，加 `BackHandler`。

## 为什么不用 Flutter

spec §5.8 原本写的是 Flutter 外壳 + MethodChannel/EventChannel 对接 `:arview`。
实做时改掉了：

1. **跨平台价值为零。** 整个项目的地基是 ARCore，只有 Android。Flutter 唯一的
   卖点在这里换不到东西。
2. **代价是把 §7 契约实现两遍。** `:arview` 里已经有 `PhotoArClient` + `Api.kt`
   + `Catalog.kt`，配 30 + 27 + 30 个测试。Flutter 侧要么用 Dart 重写一份请求与
   解析（两份契约实现，改一处得改两处，测试也得写两份），要么把每个 API 都包成
   MethodChannel（六个界面的每次列目录、每次取缩略图都过一遍桥）。两条都是纯支出。
3. **spec 自己已经把 `EndpointResolver` 放在 Android 侧**（§5.7）。那么「当前走
   哪条通道」这个所有界面都要显示的状态，本来就在原生侧；外壳在 Flutter 就得再
   用 EventChannel 把它推过去。
4. **缩略图要带 `Authorization` 头。** 这在 Flutter 里同样是自己写 loader，`Image.network`
   直接用不了 —— 也就是说连「Flutter 的控件生态更省事」这条都不成立。

改成 Compose 之后，外壳与 `:arview` 在同一个 Kotlin 进程里直接调用，没有桥、没有
第二份契约实现，`Route`/`Draft` 这些状态就是普通 Kotlin 类。

代价记清楚：**换 iOS 的可能性归零**（本来也是零，ARCore 决定的），Compose 的
`@Composable` 层没法在 JVM 单测里跑（要 Robolectric 或真机），所以把所有「差一位
就错」的东西抠进了 `Fmt.kt`（见下）。

## 设计决定

### 1. `Fmt.kt`：一个不许 import android.* 的纯函数层

界面代码在真机上肉眼可验，有些东西不行 —— **打印宽度填错不会报错，只会让 AR 里
的视频一直飘**。这类东西全部收进 `Fmt`（131 行，26 个测试），文件里一个
`android.*` 都不许出现，于是能在普通 JVM 单测里钉住。

它管的是：打印宽度的解析与米/毫米换算、字节与时间格式、质量分档位、面包屑、
异常 → 人话。

三个真被测试逮住的点：

- **`0.089f * 1000.0 = 88.9999…`**。不先舍到 0.1mm，6寸 会显示成「151.9 mm」、
  3寸 显示成「89.0 mm」，看着像服务端把数据存坏了。
- **时间戳是毫秒**。查了服务端：`db.now_ms()` 和 `int(st.st_mtime * 1000)`，
  一个按秒解的地方都没有。按秒解会显示成 1970 年，而 1970 年看着像「数据坏了」
  而不是「代码错了」。
- **75 是质量分的底**，不是 0。§8.1 里 eval-img < 75 服务端直接拒绝入库，所以
  「不达标」这一档在库里根本不该存在，档位表必须从 75 起。

### 2. 打印尺寸预设按照片方向取长边还是短边

§17 要求「App 里给常用尺寸预设」。做起来有个容易错的地方：`print_width_m` 是
**参考图水平方向**的物理宽度（ARCore `addImage` 的第三个参数），所以 6寸相纸
（102×152）横着放时该填 152、竖着放时该填 102。

方向从缩略图解出来的像素尺寸判（`NetImage` 的 `onSize` 回调），图还没下下来时
按横向算 —— 绝大多数打印照片是横的。预设按钮上直接把毫米数写出来
（「6寸 · 152」），点了还能改。

手输的范围锁在 10–2000mm：小于名片、大于 A0 的都是笔误，而这个值填错不报错。

### 3. 缩略图不用 Coil / Glide

每张缩略图都要带 `Authorization: Bearer <token>`，而且 api 与 media 是两条会
各自变化的通道。图片库的 header 注入 + 自定义 OkHttp 客户端 + 缓存键要绕开 URL
（同一路径在不同通道上是同一张图）—— 配到能用为止的工作量不比自己写少。

所以走 `PhotoArClient.download()` / `fsThumb()` 拿字节，`BitmapFactory` 解，
`LruCache`（8MB，`sizeOf = byteCount`）兜住滚动。切换通道或改配置时
`Thumbs.clear()`。

### 4. 不用 navigation-compose

六个页面、没有深链、没有多返回栈需求。`mutableStateListOf<Route>` 就是回退栈，
`Route` 是 sealed interface 带参数（`Detail(photoId)` / `Browse(pick, dir)`），
类型安全，不用把参数编成字符串路由再解回来。

一个刻意的设计：**浏览目录时每进一层 push 一页**，于是系统返回键天然等于「上一级」，
不用自己管目录栈。

### 5. `Draft` 放在 `Shell` 上而不是入库页的 `remember` 里

入库页选视频要 push 一个浏览页，而 Compose 会把非栈顶页面的状态丢掉 —— 状态放
`remember` 里的话，选完视频回来会发现刚填的宽度和标题都没了。所以草稿挂在
`Shell` 上，跟着导航栈活。

### 6. `EndpointCenter` 是进程单例

§9.2 的四个触发时机（启动、网络变化、手动刷新、连续失败 2 次）分散在设置界面、
扫描状态机和 `MainActivity` 三处，它们必须看到同一份探活结果。各自 new 一个
resolver 的话，`MIN_INTERVAL_MS` 节流就形同虚设，弱网下变成三倍探活。

网络变化用 `registerDefaultNetworkCallback` 而不是广播：Wi-Fi ↔ 蜂窝切换时
`onAvailable` 会再来一次，这正是「回到家进了局域网」和「出门离开局域网」要的信号。
注册失败（某些定制系统会抛）只记一条日志 —— 还有另外三个触发时机，缺一个不该让
App 起不来。

### 7. 探活失败原因原文照抄，不归一

设置页每条通道后面贴 `Probed.error` 的原文。「令牌不对（401）」「这个地址上没有
photo-ar-server（404）」「不通」是三个完全不同的下一步动作，归成一句「连接失败」
等于把排查信息扔掉。同理 `Fmt.errText` 把 401 单拎出来，文案直接把人指到设置页
那一行 —— 其它错误重试有意义，令牌错了重试一万次也一样。

探活结果对回候选时**按 name+base 匹配，不按下标**：用户改了地址还没点保存时，
`Resolution` 里还是旧列表，按下标会把上一条的「通 23ms」显示到一个刚改过的地址上。

### 8. §9.4：上传入口按 `uploadAllowed()` 直接不给

media 走隧道时 Cloudflare 有 100MB 请求体上限，服务端也会按 `cf-ray` 头回 413。
所以入口不是「点了报错」，而是不出现，设置页里写清楚为什么。

## 从服务端源码里核实的三件事

写界面时有三个判断没敢照 spec 猜，去读了服务端代码：

| 问题 | 服务端的事实 | 界面上的后果 |
|---|---|---|
| 时间戳是秒还是毫秒 | 毫秒（`db.now_ms()`、`int(st.st_mtime * 1000)`），无例外 | `Fmt.time` 按毫秒解，并写了钉住的测试 |
| `list_dir` 有没有排序 | 已排（目录在前，再按名字） | 客户端**不能**再排一遍 —— 否则同一个目录在 App 里和服务端日志里顺序不一样，排查时会怀疑数据错了 |
| 有没有删除照片的路由 | 没有。整张路由表里没有 DELETE | 「删除照片」不是漏做，是**不在 §7 契约范围内**。要做得先改服务端 |

## 刻意没做的

| 项 | 为什么 |
|---|---|
| `POST /v1/upload` 的 SAF 选文件流程 | §7 自己标的「可选路径」，而正常用法是关联 NAS 上已有的文件。且它只在非隧道通道可用（§9.4），做完也只有在家时能用 |
| 缓存管理入口（§5.8 列了） | 归 Phase 4 —— 那一阶段才有端侧缓存索引和视频 LRU，现在没有可管的东西 |
| 删除照片 | 服务端没有路由，见上 |
| 令牌上 Keystore | 明文存 SharedPreferences。这台设备的门槛是锁屏，拿到 root 的人一样能读出 Keystore 解出来的明文。设置页里写明了这一点 |

## 验证到哪一步

| 检查 | 结果 |
|---|---|
| `./gradlew testDebugUnitTest` | **239 个全绿，0 失败**（全仓库 637：Python 398 + Android 239） |
| `./gradlew assembleDebug` | BUILD SUCCESSFUL，`app-debug.apk` 11.7MB / 9 个 dex（Phase 2 是 4.6MB / 8 个，涨的是未压缩 debug 包里的 Compose） |
| `compileDebugKotlin --rerun-tasks` | 无 warning、无 error |
| 合并清单 | 只有 `standalone.MainActivity` 与 `arview.ui.ArScanActivity`，Phase 2 的 `SetupActivity` 已删 |
| 真机 | **一次都没跑过** |

239 个测试的分布：

| 套件 | 数量 | 覆盖 |
|---|---|---|
| `EndpointResolverTest` | 43 | §9.3 那张表的每一行、探活并发、节流、force、api/media 分别选、`uploadAllowed` |
| `ScanControllerTest` | 43 | Phase 2 原有 |
| `CatalogParseTest` | 30 | §7 的 photos / photo / fs.list / history 每个字段、缺字段、JSON null |
| `PhotoArClientTest` | 30 | 通道分离、Bearer、每次调用重读端点、状态码分流（Phase 2 的 18 + 新增 12） |
| `ApiParseTest` | 27 | Phase 2 原有 |
| `FmtTest` | 26 | 打印宽度换算与校验、毫秒时间戳、质量分 75 闸门、错误文案分流 |
| `GeometryTest` | 15 | Phase 2 原有 |
| `HttpProberTest` | 13 | 401/403 与 404 与连不上分三路、延迟测量（含时钟回拨）、探活刻意不解析响应体 |
| `FramesTest` | 12 | Phase 2 原有 |

## 出口条件的实际状态

「可日常使用」——**未验证**。下面全部只是「代码看着对」：

| 项 | 状态 |
|---|---|
| 六个界面在真机上的实际布局与手感 | 未验（`@Composable` 一行都没在设备上渲染过） |
| 走通一次真实入库（选图 → 填宽度 → 配视频 → 提交 → 出结果卡） | 未验 |
| 缩略图网格滚动时的内存与流畅度 | 未验（8MB LruCache 是拍的，没测过） |
| 真实的四通道探活（LAN / Tailscale / 隧道 / 公网）与切网重探 | 未验（逻辑有 43 个表驱动测试，但没在真网络上跑过） |
| 照片方向判定 → 打印宽度是否真的取对了边 | 未验（这是最要紧的一项：错了不报错，只会让 AR 一直飘） |
| Phase 2 那整份 §14.5 手测清单 | 依然一条未执行 |

**Phase 3 记作代码完成、出口条件挂起**，与 Phase 2 同一状态。拿到真机后
Phase 2 与 Phase 3 的验证要一起做 —— 它们的出口条件在同一次上手里就能一起判。

## 下一步（Phase 4）

端侧缓存索引（最近 200 张离线秒识别）+ 视频本地 LRU 缓存。它同时补上 §5.8 列的
「缓存管理」那个设置入口。
