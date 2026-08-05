# web-front：photo-ar 的网页版

举起手机对着一张打印出来的照片，视频贴在照片上播起来 —— 不装 App，浏览器里就能用。
支持 **Android / iOS / 鸿蒙**。

**识别与贴合全部在浏览器里跑。** 服务端只做两件事：把这个用户被授权的照片的 ORB 描述子
发下来（一次，几 MB）、以及吐视频流。识别热路径**一次网络都不走**。

```
┌── 浏览器（手机） ─────────────────┐   ┌── NAS：一个容器，一个端口 ───┐
│ getUserMedia → canvas → Worker    │   │ /       静态 + COOP/COEP     │
│   ORB 4000点 → 词汇树粗排          │←──┤ /api/lib  PARL 包（ETag）    │
│   → Top-20 精排 → RANSAC → 四角    │   │ /admin    反代 → 管理台      │
│   锁定后：光流跟踪，9.5ms/帧        │   │ /v1/*     反代（cookie、Range）│
│ WebGL：相机背景 + 透视变形的视频    │←──┤        ↑ 同进程内读 library/ │
└───────────────────────────────────┘   └──────────┬───────────────────┘
                                          photo-ar 后端（127.0.0.1:8965）
```

---

## 页面

底栏按角色分权（`navpolicy.js`）：**访客 2 个页签，管理员 5 个**。挡的不是安全
（服务端才是权限的真相），是可用性 —— 别把一条必然 403 的路摆在访客面前。

下面这张表的左列是**已下线的安卓客户端**里对应的那一屏。留着它不是为了兼容什么，
是因为右两列那些"为什么这么设计"的理由多半是从那次对照里来的 —— 删掉左列，右边就成了
一串没有出处的断言。（安卓客户端 2026-08-05 下线，见 `docs/decisions.md` §36。）

| 曾经的安卓页 | 这里 | 差别 |
|---|---|---|
| `ScanHome` | **扫一扫（首页）** | Android 让管理员落在照片库；这里两种角色都落在扫描页 —— 网页的入口是宾客扫码打开的链接，扫描就是它存在的理由 |
| `Photos` | 照片库 | 网格 + 「无视频」「参考图变了」两个徽标（各对应一种"扫了没反应"） |
| `Detail` | 照片详情 | 三段警示文案照搬（每种坏状态该去做什么都不一样）+ 删除 |
| `Play` | 试播 | 不开相机全屏播。它顺带把媒体那两步链路验一遍 |
| `Media` | 素材 | 挑照片+视频一次传完即映射，五步各自可提前结束 |
| `History` | 识别历史 | `ambiguous` 单独标红 —— 其余未命中是"这一帧没拍好"，它是"每一帧都这样" |
| `Admin` | 管理 | **不需要内嵌 WebView**：这里本来就是浏览器，`/admin` 同源同会话 |
| `AdminWeb` | — | 同上，直接开新标签 |
| `Cache` | 本机缓存 | **语义完全不同**，见下 |
| `Settings` | 设置 | **通道那一整节不存在**，见下 |

### 三处刻意不照搬

1. **没有通道配置**。Android 有多端点探活（LAN / Tailscale / Cloudflare）、每条声明
   「适合 api 还是 media」。网页是服务端发出来的，请求全走同源相对路径 —— 没有可选的
   通道，也就没有可配的东西。这不是"少做了"：那套的存在理由是 App 要自己找服务端。
2. **不需要内嵌管理台**。Android 那边要解释一堆代价（WebView 里点不动多层弹窗、
   浏览器里要再登一次、`<input type=file>` 默认什么都不做）。这些在网页上全部不存在。
3. **缓存页语义不同**。Android 管的是"把参考图与视频下到手机存储"，为了没网的现场。
   网页版的三层缓存（识别库包 / HTTP 缓存里的视频 / wasm code cache）**都不是我们能
   精确查询的** —— 那一页如实显示能查到的部分，并说清哪些查不到。编一个数字比不给更糟：
   用户会据此判断"是不是缓存坏了"。

### 切页时相机必须停

每个页面的 `mount()` 返回一个**卸载函数**，而它不是可选的礼节：扫描页持有相机流、
rAF 循环和 WebGL 上下文。不释放的话相机灯一直亮、电量哗哗掉，而用户以为已经离开
那一页了。`shell.js` 会把没返回卸载函数的页面在控制台点名。

**但 Worker 不停** —— 它持有 12MB 的 wasm 实例和解析好的识别库，每次进扫描页重建
就是每次重新加载。所以它归外壳长驻，切离时只发一次 `reset` 清跟踪状态。

## 外观：星露谷物语

整套界面是从游戏素材切出来的。**世界是暗的，菜单是暖木头的** —— 这是游戏自己的分法，
而这个应用刚好需要它：扫描页是取景器，屏幕必须暗到让相机预览和贴在照片上的视频是画面里
最亮的东西；其余八页是任务界面，亮一点更好读。

| | |
|---|---|
| 扫描页（取景器） | 全黑 + 底部一块深色木牌 HUD（游戏里快捷栏的位置），认出来时右上角钉一颗星 |
| 其余八页 | 桃色木框面板，标题压在面板外的深色底上 |
| 顶栏 / 底栏 | 深木色。当前页签换成桃色并且**顶出来 4px** —— 游戏里的页签就是这么动的 |
| 登录门 | 夜空底 + 一封信。宾客扫码打开这个页面，第一眼看到的就是这一屏 |

素材与字体都是**生成物**，规则写在生成器里而不是某个人的记忆里：

```bash
npm run art:regen                      # 需要素材包 + PIL
npm run font:regen -- <融合像素字体的 zip>   # 需要 fonttools + brotli
```

产物提交进仓库（`public/art/`，17 张图 36KB + 字体 175KB）—— 部署机上既没有素材包也没有
Python 图像库。生成器另外产出 `tools/art-contact.png`（切图核对表，坐标错了只能靠眼睛看出来）
和 `tools/pixel-coverage.json`（字体覆盖范围，给测试用）。

### 三条约束，改这套皮时必须知道

**一、字号只能是 12 的整数倍。** 字体按 12×12 的格子画，写 13px 或 1.5em 会让字母的竖笔
一根 1px 一根 2px —— 那不是"有点糊"，是看着像坏了。所以整套界面只有两级字号：12 和 24，
层次靠颜色和间距分。踩过一次：`h2` 吃了浏览器默认的 1.5em（18px），一屏区块标题全是糊的，
而人会以为是屏幕的问题。`test/art.test.js` 现在盯着这件事。

**二、边框是九宫格贴图。** `border-image` 的每条边中段在原图里逐列完全相同（切图脚本里
验过），所以默认的 stretch 拉伸不产生插值痕迹，四角又是 1:1 显示的 —— 整套框在任何尺寸下
都逐像素精确。用九宫格的元素**必须 `background: none`**，否则元素自身的背景色会从木框圆角
外面漏出四个直角。

**三、前景色靠变量继承，不要一处处写。** 同一个 `.p.dim` 在深色底上和在桃色面板里需要完全
不同的颜色。一处处写 `.panel .p.dim { … }` 能 work，但每加一个组件就要补一条，而漏掉的那条
不会报错 —— 只会在某个底上变成一坨看不清的字。所以颜色走 `--fg` / `--fg-dim` / `--fg-bad`，
**由容器设定、被内容继承**。

### 版权

星露谷物语的美术资源版权属于 ConcernedApe，这里是私人场合自用，`public/art/` 里的 PNG
不要外传。字体是另一回事 —— 融合像素字体是 OFL，授权全文随包带着（`art/pixel-font-OFL.txt`）。

### 看一眼做成什么样了

```bash
npm run shot -- --base http://127.0.0.1:8964 --cookie "photoar_session=…" --out /tmp/shots
```

`tools/shot.mjs` 是个百来行的 CDP 客户端（Node 22 自带 WebSocket，依赖为零），按手机视口
逐页截图并把控制台里的异常一并收上来。这一套界面的失败模式**全都是看得见但测不出来的**，
所以拍照是必要工序而不是可选项 —— 上面那四条"踩过"，每一条都是截图先发现的。


## 为什么是这个架构

### Web 上没有 ARCore 的等价物

App 那边的首选贴合路是 ARCore Augmented Image（真 6DoF）。浏览器上**没有**：
WebXR 的 image tracking 不是标准（MDN 上没有 `XRImageTrackingResult` 这一页），
只在 Chrome 的 flag 后面；**iOS Safari 到 2026 年仍然完全不实现 WebXR AR**；鸿蒙也没有。

所以网页版只能走**第二条贴合路**：精排那步 RANSAC 拟合出的单应矩阵取逆作用到参考图四角，
就是照片此刻在画面里的四边形，视频按这四个角做透视变形贴上去。纯 2D，不需要物理尺寸，
**因此也不需要用户做任何动作**。

`public/render/screenquad.js` 的几何是从安卓那边的 `ScreenQuad.kt` 逐行搬过来的；
`public/recognize/verify.js` 里的 `normalizedQuad` 是 `photoar/quad.py` 的对译。

### 网页版在这条路上比 App 更强，不是更弱

Android 那条第二贴合路的四角来自**服务端**，真机实测往返 **1~2.5 秒**（每帧 93~103KB
上行、与视频抢管子），于是"四角一到手就已经用掉大半个过期窗口"——`ScreenQuad.TTL_MS`
被迫从 1.2s 一路放宽到 4s。

搬到浏览器之后那个往返变成一次本地计算：**检测 149ms、跟踪 16ms**（实测，见下表）。
四角的年龄从"到手即过期"降到十几毫秒，所以这里的 TTL 只需要 **1 秒**。

### 后端选 ORB 不选 XFeat —— 决定因素是下发体积

浏览器要自己识别，就得拿到参考侧描述子。按 `descstore` 的实测布局：

| 后端 | 每张 | 1000 张 | 结论 |
|---|---|---|---|
| **ORB** | 12,008 B | ≈ 16 MB（含词汇树） | ✅ |
| XFeat | 135,176 B | **135 MB** | ❌ 不可行 |

选 ORB 顺带拿到三样：它是**已过出口条件的基线**（命中 95.70% / 真实误识别 0.000%）、
不用下 4.31MB 模型、Web 与 App 共用同一个库目录。

### 抓帧不能在主线程上转 RGBA

真机实测（1280×960）：`drawImage + getImageData` 在主线程上花 **65ms**，抓帧因此占掉
主线程的 **55%**，22.5% 的帧迟到、其中 92% 正好跟在一次抓帧之后 —— 那就是"不丝滑"。

所以主线程只做 `createImageBitmap(video)`（19.4ms），RGBA 转换搬到 worker 的
OffscreenCanvas（10.2ms）。改完：迟到帧 **0/3062**、渲染 p95 帧间隔 16.5ms（一帧没掉）、
四角年龄从 91ms 降到 43ms（管线不再被抓帧堵住，跟踪节奏快了一倍）。

⚠️ **不用 `createImageBitmap` 自带的 resize**：那是另一个缩放算法，而缩放算法决定每个
像素、像素决定 FAST 角点、角点决定描述子的每一位。缩放留给 worker 里的 `drawImage`。
像素等价性验过：拿定住的一帧作源，两条路 4,915,200 字节全部相同。

### 四角要**预测**，不能只平滑

真机实测：四角画出去那一刻的陈旧度中位 88ms（改抓帧之后 43ms）。而原来那条 tau=60ms
的一阶低通在它之上又加一个等于 tau 的稳态滞后，**却只削掉 11% 的抖动**（静止时位置
标准差 1.01‰ → 0.90‰）—— 噪声的时间尺度与 tau 相当，一阶低通压不住。

`render/quadfilter.js` 按速度分档：静止时重平滑且**完全不外推**，运动时轻平滑并按实测的
`now - quadAt` 外推。仿真（`test/sim/predict.mjs`，噪声与延迟用真机值）：
**运动段滞后 -36%、静止段抖动 -31%**，两个都好。

### 单帧判定过不了门槛时要**跨帧累积**

真机实测：内点中位 30、最大 38，而门槛 40 —— 96 次检测一次都没锁上，而照片确实被匹配
上了（runner-up 个位数）。`recognize/streak.js` 与服务端 §35 同一套规则。
门槛恢复 40 后从"永不锁定"变成"锁上并稳定跟踪 82 秒"。

### 检测与跟踪必须分层

实测（桌面 headless Chrome，单线程 wasm）：

| 步骤 | 耗时 |
|---|---|
| 提特征（4000 点 @1280） | 56 ms |
| 单候选配对（4000×300 Hamming crossCheck） | 45.6 ms |
| 单候选 RANSAC | 9.7 ms |
| **全库检测（Top-20）** | **149 ms**（这份 golden 只有 1 个候选；20 候选按 1.2s 估） |
| **光流跟踪 83 点 + 重解单应** | **9.5~16 ms** |

也就是「每帧重跑识别」在浏览器里是 9 FPS，贴不住。所以命中那一帧把**内点**留下当光流
种子，之后每帧只做光流 + 重解单应 —— 实测快 **10.9 倍**，光流保住 83/83 个种子点。

---

## 实测：浏览器算的描述子与服务端的能不能配上

这是整个方案的前提，所以它是一道 gate（`test/golden/`，23 条断言）。同一段原始字节，
Python 的 `cv2 5.0.0` 与浏览器的 `opencv.js 5.0.0` 各提一次：

| 量 | 服务端 Python | 浏览器 wasm |
|---|---|---|
| 4000 个描述子逐位相同 | — | **3915 / 4000**（其余差 1~3 位 / 256 位） |
| 关键点集合 | 4000 | **完全一致**（0 个差异） |
| 配对数 | 195 | **195** |
| **内点数** | 83 | **83** |
| 行列式 | 0.523244067 | 0.523243877 |

那 85 个描述子的差异根因是 `angle`：wasm 与原生的 `fastAtan2` 有 **0.0095°** 的末位差异，
而 rBRIEF 的采样点要按 angle 旋转，于是落在整数像素边界上的少数采样点跳了一格。
**对识别结果零影响** —— 内点数逐个相同。

> ⚠️ 这些数字全部来自**桌面 headless Chrome + SwiftShader**。手机上按 2~4 倍慢外推，
> 也就是检测 0.3~0.6s（单候选）/ 2~5s（20 候选）、跟踪 25~50 FPS。**真机没有量过。**

---

## 三平台的能力边界（决定了没有第二种架构可选）

| | Android Chrome | iOS Safari | 鸿蒙 ArkWeb |
|---|---|---|---|
| WebXR image tracking | 实验性、非标准 | **没有** | 没有 |
| `getUserMedia` | ✅ | ✅ **仅 Safari 本体** | ✅ 需宿主声明 `ohos.permission.CAMERA` 并 `grant(['VIDEO_CAPTURE'])` |
| WASM SIMD | ✅ | ✅ | ✅ |

### ⚠️ 两条必须知道的限制

1. **iOS 微信/QQ 内置浏览器打不开相机。** Apple 只给 Safari 本体开放 WebRTC，第三方 App
   的 WKWebView 没有；微信官方明确表示内页 WebRTC「暂无计划」。这不是权限问题、也没有
   工程绕法 —— 只能引导用户「点右上角 ··· → 在浏览器中打开」。`public/camera.js` 会
   检测 UA 并直接说这句话。
2. **必须 HTTPS —— 但这不是障碍，现有的 Cloudflare Tunnel 就是。**
   `getUserMedia` 只在**安全上下文**里存在：

   | 地址 | 相机 |
   |---|---|
   | `https://任意域名`（公网、隧道、反代，都算） | ✅ |
   | `http://localhost` / `http://127.0.0.1` | ✅（仅开发） |
   | `http://192.168.1.10:8964` | ❌ |
   | `http://公网IP:8964` | ❌ |

   也就是说**广域网 HTTPS 是最标准的那一档**，宾客扫码打开 `https://...` 一切正常，
   手机上不装任何东西（前端零构建、wasm 是预编译的）。真正不能用的只有一条：
   **局域网 http 直连** —— 而那恰好是 App 版最快的那条路，所以排查时很容易被它误导，
   一直怀疑相机权限。

   要在现场也走局域网（省 CDN 流量，见下面「视频出口」）就得给局域网也配上真证书：
   真域名 + split-horizon DNS 解析到 NAS 内网 IP + Let's Encrypt。那是**优化**，
   不是让它能用的前提。

---

## 跑起来

网页版与后端在**同一个容器、同一个端口**上（2026-08-05 合并的，那之前是两个）。所以
这里没有独立的 Dockerfile 与 compose —— 部署看仓库根目录的 `docker-compose.yml`，
拓扑与理由看 `docker/entrypoint.py` 的模块 docstring。

### 单独跑这一半（改前端时最快的循环）

```bash
cd web-front
PHOTOAR_UPSTREAM=http://127.0.0.1:8964 PHOTOAR_LIBRARY=../data/library node server/index.js
# → http://127.0.0.1:8964
```

上游填一个跑着的后端（本机容器就是 8964，那时这里要换个 PORT 免得撞）。

⚠️ `http://127.0.0.1` 是安全上下文（localhost 例外），所以本机开发能开相机。换成
**局域网 IP 的 http** 就不行 —— 但那不代表要 localhost 才能用：任何 **https** 地址
（包括公网、隧道）都算安全上下文，见上面那张表。

### 整套（容器）

```bash
cd ..                    # 仓库根目录
cp .env.example .env     # 只有 PHOTOAR_ROOTS 必须看一眼
docker compose up -d     # → 8964，一个端口
```

一个端口按 URI 分：`/` 这一半、`/api/*` 这一半自己的端点、`/admin` 管理台（反代）、
`/v1/*` API（反代）。识别库直接读容器里的 `${PHOTOAR_DATA}/library`。

### 让宾客能用（这一步就够了）

8964 默认是裸 http，前面要有一层 TLS。现有的 Cloudflare Tunnel 直接就能用 —— 那份本地
`config.yml` 里加一条 ingress：

```yaml
ingress:
  - hostname: ar.yourdomain.com     # 通配符 DNS 已经覆盖，不用新加 DNS 记录
    service: http://127.0.0.1:8964
  # ... 原有的那些条目
  - service: http_status:404        # 兜底那条永远留在最后
```

宾客扫码打开 `https://ar.yourdomain.com` → 输名字 → 举起手机对着照片。**手机上不装
任何东西**：前端是零构建的原生 ES modules，`opencv.js` 是预编译的 wasm，浏览器直接加载。

⚠️ 视频也会从这条隧道出去，量级与账号级风险见下面「没做的 → 视频出口」。

### 手机自测（不经隧道，走局域网 / Tailscale）

自测时手机前面没有隧道那一层，而 http 下相机**根本不存在**——所以
`https://100.110.121.64:8964` 之前"无响应"，是因为那个端口只说 http。让本进程自己说 https：

```bash
web-front/tools/gen-dev-cert.sh   # 自动把本机所有 IPv4 与 tailnet 域名写进 SAN
docker compose up -d              # 配好 WEBFRONT_TLS_CERT/KEY 后（见根目录 .env.example）
```

手机打开 `https://<局域网或 Tailscale IP>:8964` → 证书警告 → 「高级 → 继续」。

两个更省事的替代，按情况选：

| 场景 | 做法 |
|---|---|
| **iOS 自测**（Safari 对自签更严） | 去 Tailscale 后台 DNS 页面打开 **HTTPS Certificates**，然后 `tailscale cert <机器>.<tailnet>.ts.net` —— 那是 Let's Encrypt 真证书，无警告、不用装描述文件 |
| **安卓自测**（想连证书都不要） | `adb reverse tcp:8964 tcp:8964`，手机上打开 `http://localhost:8964` —— localhost 按规范就是安全上下文，一个字节的证书都不用。比改 `chrome://flags` 干净 |

⚠️ 自签证书的 SAN **必须**带 IP/DNS，`tools/gen-dev-cert.sh` 已经处理了。少了 SAN 现代
浏览器连「继续」都不给，只报 `ERR_CERT_COMMON_NAME_INVALID` —— 那看起来像证书生成失败，
而不像少了一个字段。有效期也钉在 825 天内，因为 Safari/iOS 拒绝比这更长的证书。

### 测试

```bash
npm test              # node:test，100 条：几何 25 + 导航 17 + 诊断 10 + 素材与配色 15 + 库 9 + 服务端 17 + 媒体票据 7
npm run test:golden   # 无头 Chrome，23 条：ORB 与服务端 cv2 的一致性 gate
npm run test:e2e      # 无头 Chrome，20 条：PARL → 检测 → 跟踪 → GL 顶点（纯函数层）
npm run test:worker   # 无头 Chrome，20 条：**真的把识别 Worker 起一遍**
npm run test:bench    # 无头 Chrome：光流跟踪成本
npm run test:smoke    # 无头 Chrome，44 条：**真的把页面跑一遍**（要先起服务）
npm run test:pages    # 无头 Chrome，20 条：**登录后把每一页都挂一遍**（要 cookie，见下）
npm run test:browser  # golden + e2e + worker 一起跑
```

`art.test.js`（在 `npm test` 里）盯的全是**不报错的失败**：少一张图、两处颜色表不同步、
某种前景色落进了不该落的底、字号不是 12 的整数倍。这些没有一个会抛异常，而它们全都是
一眼能看出来的 —— 所以它们必须有测试，否则只能靠每次都记得去看。

后两个与别的不同，而它们各自抓到过一个别人抓不到的 bug：

- **`test:worker`** 从 `new Worker` 一路走到 `type:'result'`。加它是因为
  **module worker 里禁止 `importScripts()`**（规范硬禁），而第一版 `orb.js` 给 Worker
  准备的正是那条路。症状是登录进去、库也拿到了，然后**永远停在"正在加载识别引擎"** ——
  HTTP 全 200、页面不崩、`test:e2e` 也全绿（它在主线程直接 new Pipeline，不经过 Worker）。
  顺带还逼出第二个：`typeof importScripts === 'function'` **在 module worker 里也是 true**
  （那个方法仍在原型上，只是调用时才抛），所以它根本不能用来做特性检测。
- **`test:smoke`** 需要一个**跑着的 web-front**（默认 `127.0.0.1:8964`，用 `WEBFRONT_BASE`
  改），把 index.html 装进 iframe 跑。`curl` 拿到 200 只证明服务器发出了字节，而加载链是
  index.html → app.js → 六个 ESM → `/api/config` → `/api/lib` → Worker → opencv.js，
  任何一步的语法错、路径错、MIME 错都会让页面停住而状态码全是 200。

`test/harness.js` 是自己写的 6KB 驱动器，**刻意不引 puppeteer** —— 那会带进一份自己
下载的 Chromium，而要测的正是本机这个 Chrome。它的 `--proxy` 把 `/api/*` 与 `/v1/*`
转给真服务，好让产品页面在同源下跑（跨源既读不到 iframe 的 DOM，也加载不了 ESM）。

⚠️ **前端改动必须重建镜像才生效**（`public/` 是 COPY 进去的，不是挂载）：
`docker build -t photoar-web-front:local . && docker compose up -d --force-recreate`。

`test:smoke` 与别的不同：它需要一个**跑着的 web-front**（默认 `127.0.0.1:8964`，
用 `WEBFRONT_BASE` 改）。存在的理由是 —— `curl` 拿到 200 只证明服务器发出了字节，而
加载链是 index.html → app.js → 六个 ESM → `/api/config` → `/api/lib` → Worker →
opencv.js，**任何一步的语法错、路径错、MIME 错都会让页面停在"正在准备…"，而 HTTP
状态码全是 200**。真机之前这是唯一能把"服务器活着"和"页面活着"分开的检查。

`test/harness.js` 是自己写的 6KB 驱动器，**刻意不引 puppeteer** —— 那会带进一份自己
下载的 Chromium，而要测的正是本机这个 Chrome。它的 `--proxy` 把 `/api/*` 与 `/v1/*`
转给真服务，好让产品页面在同源下跑起来（跨源既读不到 iframe 的 DOM，也加载不了 ESM）。

`test:pages` 需要一个真会话，凭证从命令行来、**不进任何文件**：

```bash
COOKIE=$(curl -sk -D- -o /dev/null -X POST https://127.0.0.1:8964/v1/auth/login \\
  -H 'Content-Type: application/json' -d '{"name":"admin","password":"…"}' \\
  | sed -n 's/^[Ss]et-[Cc]ookie: \\(photoar_session=[^;]*\\).*/\\1/p')
WEBFRONT_BASE=https://127.0.0.1:8964 WEBFRONT_COOKIE="$COOKIE" npm run test:pages
```

它已经抓到一个真 bug：`api.me()` 的路径写成了 `/v1/me`（真的是 `/v1/auth/me`），
404 被 app.js 那句「拿不到角色按访客处理」兜住 —— **管理员登进去只有两个页签，而没有
任何报错**。安全的默认掩盖了一个 404，而这类失败只有"真的登录进去看底栏有几格"才发现。

浏览器那几套需要本机有 `google-chrome` 和 photo-ar 的 Python 环境（重新生成 golden 时）。
`test/harness.js` 是自己写的 6KB 驱动器，**刻意不引 puppeteer** —— 那会带进一份自己下载的
Chromium，而要测的正是本机这个 Chrome。

---

## 目录

```
server/
  index.js     零依赖 Node：静态 + COOP/COEP + 反代 /v1/* + /api/lib + /api/config
  library.js   读 data/library/，按授权集裁剪成 PARL 包（含按子集重建倒排索引）
  npz.js       读 numpy 的 .npy/.npz（zip+deflate，只用 node:zlib）
public/
  index.html           外壳骨架 + 登录门（那封信）
  theme.css            整套界面系统。三条硬约束写在文件头，值得先读那一段
  app.js               引导：登录 → 取识别库 → 起识别 Worker → 装外壳
  shell.js             路由栈、底栏、页面生命周期（Android `Shell` 的对译）
  navpolicy.js         谁能看见哪几个页签（`NavPolicy` 的对译，纯函数可测）
  api.js               接口客户端（`PhotoArClient` 的对译）
  ui.js                共用小部件：三状态、区块、行、按钮、toast
  art.js               星露谷精灵：物件图与 Junimo
  pixelicons.js        11 张手画的 16×16 导航图标（`currentColor`，跟着页签状态换色）
  camera.js            getUserMedia + 抓帧。**三平台的坑全部关在这个文件里**
  pages/               九页，每页导出 `{title, mount(el, ctx) → teardown}`
  art/                 切好的星露谷素材 + 子集化的点阵字体（生成物，见下）
  recognize/
    consts.js          全部数值常量（有一条测试拿它和 Python 源码逐个比）
    pyparity.js        Python `round()` 语义与 resize_to_long_edge 的对译
    orb.js             提特征，与 features.extract 逐位等价
    verify.js          配对 + RANSAC + 三条判定 + 四角（verify.py / quad.py 的对译）
    library.js         PARL 解包 + 词汇树 words_of + 倒排 query
    pipeline.js        检测/跟踪状态机
    worker.js          识别 Worker（只处理最新一帧，不排队）
  render/
    screenquad.js      四角→NDC 顶点、图像坐标→NDC 的 cover 换算、视频 uv 的 v 轴翻转
    gl.js              WebGL：相机背景 + 透视插值的视频面片
  vendor/opencv.js     13.3MB，OpenCV 5.0.0 的 wasm 构建（见 vendor/README.md）
tools/
  extract-art.py       从星露谷素材包切图（`npm run art:regen`）
  make-font.py         点阵中文字体子集化（`npm run font:regen`）
  shot.mjs             给页面拍照的 CDP 客户端（`npm run shot`）
  split-wasm.mjs       把内联的 wasm 拆出来，好让浏览器的编译缓存生效
test/
  harness.js           无头 Chrome 驱动器（起 http 服务、收页面回报的 JSON）
  art.test.js          素材引用完整性、对比度、点阵字体的整数倍约束
  golden/              跨语言 golden：Python 生成期望值，浏览器对答案
```

---

## 在手机上排查：页面内诊断日志

手机上没有控制台，所以日志在页面里。**打开的唯一入口是「设置 → 关于 → 连按版本号 7 下」**
（或者用 `?diag=1`）。开着的时候在扫描页那条读数上连点三下可以就地关掉 —— 调试时手机
就在手上，跑回设置页很烦。

⚠️ 连点扫描页那条读数**开不了**调试模式，只能关。以前它是"没开就开、开了就关"，
而它绑在宾客也看得到的那条字上：手快点三下就掉进调试模式，看到一屏内点数和毫秒数。
面板在顶部，带「复制」按钮——这块东西的用途就是被发出来给人看。

设计与 Android 那边的 `DiagLog` 一样，连折叠规则都一样：**连续相同的行折叠成 `×N`**。
那不是优化——贴不上时有些行每秒一条，不折叠的话十几行的窗口两秒就被它们填满，把
「视频 error code=4」那种只出现一次的关键行顶出去，而那一行恰恰是唯一有信息量的
（`test/diag.test.js` 有一条测试专门盯这个）。

「认出来了但视频没贴上」有五个互不相干的原因，日志把它们分开：

| 日志里看到 | 是哪一环 |
|---|---|
| `命中 xxx 但没有 mediaUrl` | 这张没配视频 |
| `视频 error code=2 NETWORK` | 传输断了：反代、Range、超时 |
| `视频 error code=3 DECODE` | 编码不支持或文件损坏 |
| `视频 error code=4 SRC_NOT_SUPPORTED` | **拿不到**：401 / 404 / Content-Type 不对 |
| `视频 play() 被拒 NotAllowedError` | 自动播放策略。**这一条不触发 error 事件**，不打点就完全看不见 |
| `几何 OK 但没画：视频 ready=NOTHING` | 四角算出来了，但视频还没有帧可当纹理 |
| `clipVertices 拒了这一帧` | 四边形退化或跨越无穷远线 |

底部那行也一起给出贴合链的实时状态：
`库 4 张 · 28 fps · 检测 340ms · 跟踪 22ms · 贴合中 18ms前 · 跟踪点 71 · 视频 1920×1080`

## 加载：11.4MB 的引擎怎么才不每次都下一遍

真机量过（小米 M2012K11C / Edge for Android 150，CDP 数每一次请求）。**结论先放这里：**

| 地址 | 每次打开页面传多少 | 界面可用 |
|---|---|---|
| 受信任证书（隧道域名 / `tailscale cert`） | 首次 **2.43MB**，再进来 **0 字节** | 首次几秒，**再进来 1.6 秒** |
| 自签证书（点过"继续访问"） | **每次 4.87MB** | **每次 16 秒** |
| 改进前（无 brotli、缓存判据坏的） | 每次 22.8MB | 每次 **71.5 秒** |

### 一、证书决定了缓存**存不存在**

**Chromium 对有证书错误的源整体禁用磁盘缓存。** 自签、或者点过"高级 → 继续访问"的，
全都算。`Cache-Control: public, max-age=31536000, immutable` 一个字都不生效。

这件事没有任何 API 能查，而症状是"手机好慢"—— 会一路怀疑手机、网络、wasm 太大，
不会怀疑证书。所以 `app.js` 里有个 `noteEngineFetch()`：用 `localStorage` 数"引擎连续
几次走了网络"，第二次还在走就把这条原因直接印在诊断日志上。笨，但这是唯一可靠的信号。

顺带解释了一个一直没想通的数字：进度条走完之后那句"正在装配"要 **34 秒**。那不是编译，
是 `instantiateStreaming` 在**重新下载**（预取那一遍进不了缓存）。真正的编译很快 ——
`http://localhost` 上整个启动（下载 + 编译 + 起 Worker + 解析识别库）才 1.6 秒。

### 二、预压 brotli：11.40MB → 2.43MB（21.3%）

`tools/split-wasm.mjs` 在**构建期**压好，四个产物（`.wasm` / `.js` / 各自的 `.br`）
一起提交进仓库。q=11 压一次 21 秒 —— 按请求压是不行的（21 秒 CPU × 每个宾客，
而 N5095 更慢）。服务端见到客户端接受 br 就发 `.br`，见 `serveStatic` 与 `pickEncoding`。

三个细节，每个都能单独把这件事做坏，都写在那两个函数上面：`Content-Type` 按**原文件**
的扩展名算；ETag 必须带编码 + 发 `Vary: Accept-Encoding`；进度条的分母要用
`X-Uncompressed-Length` 而不是 `Content-Length`（后者是压缩后的，而 reader 给出的是
解压后的字节，直接用会跑到 470%）。

还有一道守卫：**`.br` 比源文件旧就忽略它**。防的是"改了源文件、忘了重新压"——那时服务端
会发出旧代码，浏览器解压得到的是完全合法的旧 JS，没有任何报错，只是行为不对。

### 三、`immutable` 只给 URL 里带版本号的

wasm 的 URL 是 `/vendor/opencv.wasm?v=<内容哈希前12>`，版本号由 `split-wasm.mjs` 写进
opencv.js —— 没有任何人需要记得去改它。`opencv.js` 自己**没有**版本号（它一直叫这个名字），
所以它是 `no-cache` + ETag：128KB 换一次条件请求，而给它 immutable 是个哑雷（升级之后
老浏览器抱着旧 js 配新 wasm，表现是"函数签名对不上"，且只在部分用户身上出现）。

⚠️ **`orb.js` 里那个 wasm URL 是从 opencv.js 正文里读出来的，不是猜的。** 按
`.js → .wasm` 猜会得到没有版本号的那个 —— 两个缓存键，冷启动付两次 2.43MB。
实测抓到过（一次加载传 4.87MB）。

### 四、编译缓存那条路：已经是浏览器原生的了

wasm 拆成独立文件之前，加载走的是 `WebAssembly.instantiate(bytes)` —— 那条路**没有
URL**，而浏览器的 wasm code cache 只认 URL，所以每次刷新都真的重新编译整个模块。
`split-wasm.mjs` 把它抽出来并把加载改成 `instantiateStreaming(fetch(url))` 之后，
**浏览器原生的 code cache 就生效了**，这才是"免去编译"的正确实现。

`wasmcache.js` 里那套往 IndexedDB 存 `WebAssembly.Module` 的代码**对当前 vendor 是
死路径**（只有单文件构建才会走到），留着是给"哪天换回内联构建"兜底。它现在实际做的
唯一一件事是**计时**：包住 `instantiateStreaming` 报出耗时，那是"这次到底编译了没有"
唯一的可观测量 —— 浏览器的 code cache 是隐式的，没有 API 能查。

> **想再小一截**只有一条路：用 emsdk 自己裁一份只含 core/imgproc/features2d/calib3d
> 的构建（估计 2~3MB 未压缩）。上游 `@techstark/opencv-js` 只发单文件版本。那要引入
> 构建步骤，见 `vendor/README.md`。

### 五、CDN 那一层

2.6MB 静态资源（引擎 + 字体 + 素材）是所有宾客共享、内容永不变的，**该停在
Cloudflare 边缘**。两个坑写在 [docs/deploy-details.md 的「CDN」那节](../docs/deploy-details.md)：
Cloudflare 的默认缓存按扩展名走，名单里**没有 `.wasm`**；而那条 Cache Rule
**必须按路径限定**，否则 `/api/stream/<票>` 被缓存到边缘就是把一个人的视频发给另一个人。

## 真机上视频播不了：两个互相独立的根因

在小米 M2012K11C / Edge for Android 150 上用 adb + CDP 逐个变量测出来的。两条都**不报错**，
表现一模一样：识别正常、四角贴合正常，视频永远 `readyState=0`，一声不响。

### 一、`<video>` 的请求拿不到会话 cookie（已修）

`<video>` 的请求**不是浏览器自己的网络栈发的**。同一个页面里两次请求打到服务端，
`User-Agent` 都不一样 —— 一个是浏览器，一个是安卓平台的媒体组件（MediaExtractor）。
而那个组件拿不到 `HttpOnly` 的会话 cookie，后端日志里是：

```
GET /v1/asset/<id>/stream -> 401 (0ms)     ← 每 3 秒一次，连续十次然后放弃
```

同一页里 `fetch()` 同一个地址是 `206`。

**修法：媒体票据。** 浏览器（带 cookie）先 `GET /api/ticket?path=…` 换一张短命的一次性票，
`<video>` 用 `/api/stream/<票>` 取流 —— 那个地址不需要 cookie，web-front 在服务端把真凭证
补上去转发。见 `server/index.js` 的「媒体票据」一节与 `api.playableUrl`。
**每一处喂给 `<video>` 的地址都必须过 `playableUrl`。**

试过但不行的两条：`blob:` URL（那个组件连 blob: 都不认，36ms 直接报
`EDGE_DEMUXER_ERROR_MEDIA_EXTRACTOR_FAILED`）；把 cookie 去掉 `HttpOnly`（拿会话安全
换一个浏览器的怪癖，不划算）。

### 二、平台媒体组件不认自签证书（已绕开）

它有独立的 TLS 栈，**不认浏览器里点的「继续访问」，也不认用户安装的 CA**
（安卓 7 起用户 CA 不在应用/平台组件用的信任库里），而手机没 root 就装不了系统 CA。

平台媒体组件有自己的 TLS 栈，**不认浏览器里点的「继续访问」，也不认用户安装的 CA**
（安卓 7 起用户 CA 不在应用/平台组件用的信任库里）。

对照实验，同一个文件、同一台手机：

| 传输 | 结果 |
|---|---|
| `http://…/real.mp4` | **播放正常**（00:07/00:30） |
| `https://…/real.mp4`（`local/dev.crt` 自签） | 「视频播放失败」 |

小米自带浏览器会先弹「该网站的安全证书有问题」，点继续之后**页面能开、视频仍然播不了** ——
那个例外只在浏览器内有效。

**绕法：`MediaSource`。** 既然那个组件信不过证书，就别让它碰网络 —— 页面自己 `fetch`
（浏览器的网络栈，证书与 cookie 都没问题），把字节喂给 `MediaSource`，那条路由 Chromium
自己的 ChunkDemuxer 解封装，全程不经过平台组件。见 `public/mp4stream.js`。

代价是**必须发分片 MP4**（`MediaSource` 只吃 fMP4；普通的 `moov+mdat` 喂进去是 12ms 一个
sourcebuffer 错误）。所以 `transcode.py` 改成产 fMP4，存量文件由
`tools/fragment_playable.py` 无损重封装过一遍。fMP4 仍是合法 MP4，App / Safari / 桌面
浏览器都照播。

**分片要切到 1 秒一片**（`-frag_duration 1000000`）。只靠 `frag_keyframe` 的话按关键帧切，
源的 GOP 可以很长 —— 实测首片 3.5MB，起播要等 10.3 秒；切到 1 秒之后首片 397KB，
起播 1.75 秒，而总体积只多 3.7KB。**首片多大就等于认出照片之后要等多久才出画。**

真机实测（`https://100.110.121.64:8964`，自签证书，Edge for Android 150）：
票据 → MSE → `readyState=4`、1920×1080、`paused=false`，1751ms 起播。

### 换真证书不是"建议"，是必须

自签证书的代价不是"点一次继续访问"。实测（见上面「加载」那节）：**Chromium 对有证书
错误的源整体禁用磁盘缓存**，于是每个宾客每次进页面都重下一遍引擎 —— 71.5 秒 vs 1.6 秒。
视频那一条能用 MediaSource 绕过去，缓存这一条**绕不过去**。

两条路拿真证书：

| 路 | 怎么做 | 注意 |
|---|---|---|
| **Cloudflare Tunnel**（宾客走这条） | 加一条 ingress 指到 8964，用你自己域名的通配符 DNS | 视频也从这条出去（2026-08-05 定的），代价见 [deploy-details](../docs/deploy-details.md#隧道的三条硬限制) |
| **Tailscale**（自己和家里人） | 后台 DNS 页面打开 **HTTPS Certificates**（默认关），再 `tailscale cert <机器>.<tailnet>.ts.net` | **必须用 MagicDNS 主机名** —— 公共 CA 不给 `100.64.0.0/10` 这样的 IP 签证书 |

自测又不想碰证书，就 `adb reverse tcp:8964 tcp:8964` 然后在手机上打开
`http://localhost:8964` —— localhost 按规范就是安全上下文，相机能开、缓存正常、
一个字节的证书都不用。上面那些真机数字里"1.6 秒"那一列就是这么量的。

## 已知限制与还没做的事

### 一定要记着的三条

1. **只在一台安卓真机上跑过**（小米 M2012K11C / Edge for Android 150）：识别、贴合、
   视频起播都验过。上面那些**耗时数字**仍然来自桌面 headless Chrome —— 真机的检测耗时
   没有系统量过。**iOS 与鸿蒙一次都没跑过**：iOS 的 video-as-texture、鸿蒙 ArkWeb 的
   相机权限都只有推断。
2. **`vendor/opencv.js` 是 13.3MB**（gzip 后约 3.5MB，`Cache-Control: immutable` 只下一次）。
   减小它要么等分离 `.wasm` 的构建、要么用 emsdk 自定义裁剪到 core+imgproc+features2d+calib3d
   （估计 2~3MB）—— 两条都要引入构建步骤，而这个仓库现在是零构建的。本机没有 `emcc`。
3. **粗排的 idf 按授权集重算**，与服务端按全库算**不是同一个排序**。这不是近似而是
   「在子集上检索」的正确定义，但两边的粗排 Top-20 可以不同（精排与判定完全一样）。
   授权集不超过 `top_k` 时这个差异根本不存在 —— 那时服务端自己也全查。

### 没做的

- ~~**把视频挪出 Cloudflare。**~~ **已决定：走。**（2026-08-05）量级是 50 个宾客 ×
  人均 3 条 × 单条最大 14.72MiB ≈ **2.2GB**，而非 Enterprise 套餐的 CDN 条款禁止通过
  CDN 提供视频、风险是**整个账号**。接受它的理由是没有第二条路：网页版的宾客不装任何
  东西。降低暴露面的两件事在部署层，见
  [deploy-details](../docs/deploy-details.md#隧道的三条硬限制)。
- **库缓存到 IndexedDB。** 现在每次进页面都重新 `GET /api/lib`（有 ETag，没变就 304，
  但包本身还是要重新解一次）。45 张的库是 49KB，所以这条的收益很小 —— 真正值钱的
  离线化是 Service Worker 那条路，那要等有一个受信任证书的固定域名（自签证书下
  Chromium 不给注册 Service Worker）。
- **超过 1000 张的溢出兜底**（服务端有 `/v1/recognize` 那条路，网页版没接）。
- **UI 只做到最小可用**：全屏相机 + 一条提示 + 一行元信息，沿用 App 的琥珀
  `#FFC46B` / 底色 `#0B0C10` / 等宽字族。要细化的话走 impeccable 那套流程。

### 跨语言的那些不变量（改一边必须改另一边）

- `consts.js` 的每个数 ↔ `photoar/{features,backend,verify}.py`（有测试直接读 Python 源码比对）
- PARL 包布局 ↔ `server/library.js` 的 `pack()`（Node 写、浏览器读，两边各一份实现，
  `unpack` 会校验总长度，拼错立刻抛）
- ~~`screenquad.js` ↔ `ScreenQuad.kt`~~ —— Kotlin 那一边随安卓客户端下线了（2026-08-05）。
  这里那 25 条几何测试**照旧留着**：它们盯的不是"两边一致"，是"这套几何本身对"，
  而那一条与另一边在不在无关。
- **换 OpenCV 版本 = 换特征空间**：必须重跑 `npm run test:golden`。它绿说明这次升级安全，
  它红说明全库 `desc.bin` 对新版本作废、要整库重建。
