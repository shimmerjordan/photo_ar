# Phase 1：photo-ar-server（2026-07-30 完成）

出口条件（spec §15）：**能用 curl 完成入库→识别→取流；路径穿越测试通过。**
结论：达成。`bench/e2e_curl.sh` 62 项全绿，`tests/server` 156 个测试全绿
（全仓库 398 个），镜像在本机 Docker 里真跑通了入库与识别。

## 交付内容

`src/photoar/server/`，14 个模块，零新增依赖（stdlib + 既有的 opencv/numpy）：

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `config.py` | JSON 配置 + 环境变量覆盖 | token 优先取 `PHOTOAR_TOKEN`，镜像里不留明文 |
| `safepath.py` | 白名单根目录内的路径解析 | 拒 `..`/相对路径/NUL/反斜杠；`realpath` 后再校验一次挡符号链接 |
| `db.py` | SQLite 单库（照片 / 素材 / 识别历史） | 素材按内容哈希去重；照片与素材是多对一 |
| `integrity.py` | §6.1 引用完整性 | 先比 mtime+bytes，只在不一致时才哈希；**不自动改绑** |
| `library.py` | 可增量的识别库 | 固定 vocab + 持久化词序列 + 每次 add 重建倒排 |
| `ingest.py` | 入库流水线 | 质量分闸门 → 近似重复闸门 → arcoreimg → 转码 |
| `mediaresolve.py` | §10 媒体 URL 策略链 | 直链绝不缓存（15 分钟就过期） |
| `ranges.py` | RFC 7233 Range | 多段请求 → 忽略并回 200 全量，不回只含第一段的 206 |
| `multipart.py` | 手写 multipart 解析 | 二进制体里出现 `\r\n--` 不能误判为边界 |
| `fsbrowser.py` | `/v1/fs/*` | 只列白名单内；缩略图长边可控 |
| `transcode.py`（Phase 0 已有） | faststart 检测与转码 | moov 在 mdat 之后就必须转 |
| `app.py` | 路由 / Bearer / §7 全部接口 | 纯 `handle(Request) -> Response`，不碰 socket |
| `httpd.py` | ThreadingHTTPServer 挂载 + CLI | 识别并发用信号量限到 3（N5095 只有 4 核） |

部署：`Dockerfile`（python:3.11-slim + ffmpeg + arcoreimg，815MB）、
`docker-compose.yml`、`deploy/README.md`（含 cloudflared ingress 的具体改法）、
`deploy/config.example.json`。

## 为什么是手写 HTTP 而不是 FastAPI/Flask

需要的功能只有路由、Bearer、multipart、Range 四样，全部手写不到 400 行。
换来的是 QNAP 上零依赖部署——容器里只有 opencv/numpy 两个第三方包，而这两个
本来就是识别管线的依赖。这与本仓库既有的 cc-trans、frps-panel 同一个取舍。
真正的难点在识别与路径安全，框架对这两件事一点帮助都没有。

## 三个设计决定

### 1. 固定 vocab + 持久化词序列，每次 add 重建倒排

`idf = log(n_docs / df)`：文档数一变，**每一篇**文档的归一化权重都跟着变。所以
「增量加一张」在数学上不存在，只能重建。重建的输入是每张照片的词序列
（`words.bin`，每张 1204 字节），而不是重新量化描述子——量化才是慢的那步。
一万张的 `words.bin` 约 12MB，重建是秒级。

vocab 本身固定不变、由 `photoar build` 预先训好。换 vocab 等于全库重建索引，
所以服务端不训练，只加载。

### 2. `n_docs == 1` 的退化必须专门处理

只有一篇文档时，每个词的 `df == n_docs`，于是 idf 全零，这篇文档的权重向量
是零向量——**它永远检索不到自己**。库里只有一张照片时识别永远失败，而日志里
一切正常。

处理：`n_docs <= TOP_K` 时直接几何校验全部照片；超过之后，把缓存的
`unretrievable_docs()`（权重全零的文档）并进候选集。`test_library.py` 里有一条
测试专门断言 `unretrievable_docs() == [0]` 且单张库仍能识别。

### 3. 去重的内点数下限故意低于识别的下限

`verify.MIN_INLIERS = 40`（识别）vs `verify.DEDUP_MIN_INLIERS = 25`（去重）。

查询时的内点数系统性地高于入库时两张不同照片之间的内点数，所以去重的闸门
必须**更松**。反过来（去重下限 ≥ 识别下限）的后果是：近似重复的照片对通不过
去重检查、双双留在库里，然后**两张都永远认不出来**——0d 上规模实测的现象。

## 出口条件的验证方式

### `bench/e2e_curl.sh`（62 项）

真起一个进程、真 curl、真 vocab（5000 张真实照片训出来的那棵）、真 ffmpeg
转码、真 arcoreimg。全部 mock 撤掉之后是否还成立，只有这么跑一遍才知道。

覆盖：鉴权（含 `WWW-Authenticate`）→ 入库（含 409/400/422 各种拒绝）→ 识别
（命中 `inliers=83`、库外照片不误识别）→ media 解析 → 取流（全量字节比对、
206 的体真的从偏移 100 开始、416、多段 Range、HEAD）→ imgdb/thumb（ETag +
304）→ **14 项路径穿越** → 上传（含隧道 413）→ 历史 → 一致性检查 → 重启后
仍能识别同一张。

照片是用 `bench/e2e_pick_photos.py` 按 arcoreimg 质量分挑的（≥85）。第一版
随手取三张，其中一张只有 55 分被服务端正确地 422 拒了，后面每一步跟着失败
——那不是缺陷，但会把真问题埋掉。

### `tests/server`（156 个测试）

| 文件 | 数量 | 重点 |
|---|---|---|
| `test_app.py` | 63 | §7 全部接口、§14.4 的闭环、14 项穿越走完整 HTTP 层 |
| `test_ranges.py` | 17 | §14.3 的五类 Range + 416 + 多段 |
| `test_safepath.py` | 15 | `..`/绝对/相对/NUL/反斜杠/符号链接/嵌套根 |
| `test_library.py` | 18 | 增量、单张库、候选集与 Phase 0 逐位一致、断电对齐 |
| `test_multipart.py` | 13 | 体内含 `\r\n--`、裸 LF 客户端、缺尾边界 |
| `test_mediaresolve.py` | 11 | 直链绝不缓存、失败落下一条策略 |
| `test_httpd.py` | 11 | 真 socket：Content-Length、206 偏移、HEAD、keep-alive |
| `test_integrity.py` | 8 | mtime 变而内容未变、内容变则标 `ref_stale`、不自动改绑 |


`test_app.py` 直接调 `Server.handle`（不开 socket，快），`test_httpd.py` 在真
端口上跑那些「逻辑全对但 ExoPlayer 播不了」的位置：Content-Length 与实际字节
是否一致、206 的偏移、HEAD 不写体、keep-alive 的下一个请求会不会错位。

### Docker

本机 `docker build` + `docker run` 跑通：ping / 401 / 403 穿越 / 入库
（质量分 95、imgdb 8475 字节，说明容器里的 arcoreimg 能跑）/ 识别命中
（`inliers=83`）/ healthcheck healthy。

## 过程中发现并修掉的两个真缺陷

两个都是「测试写对了才发现」的类型，症状都不报错。

### `PhotoLibrary.add()` 的 slot 错位

`defer_reindex=True`（批量入库）时快照刻意不更新，而 `add()` 拿快照当基准续写
`slots.json`。于是每次 add 都从同一个旧列表加一条，`slots.json` 停在 1 条，而
`desc.bin`/`words.bin` 一直在长——**photo_id 与 slot 从此错位**。

症状：识别命中后播的是**别人的视频**，全程零报错。

修法：`add()` 从磁盘读 `slots.json` 当基准；再加 `_assert_aligned()`，三份记录
条数不等就抛 `LibraryCorrupt` 并让人跑 `reindex`。启动时和每次 append 前都查，
这样入库中途断电留下的半成品会变成「拒绝启动」而不是「静默错位」。

### 上传后 keep-alive 收尾把连接读死

收尾逻辑按「处理器读过请求体没有」判断要不要补读，而流式上传（`stream_to`
直接写文件）读完之后 `_body` 仍是 `None`，于是又去读一遍 `Content-Length`
——连接上已经没有字节了，服务端永久阻塞。

症状：`curl` 上传 20 万字节后一直挂着，文件其实已经完整落地，日志里没有任何
异常。这是 `e2e_curl.sh` 抓到的——`test_app.py` 不开 socket 碰不到收尾逻辑，
`test_httpd.py` 当时没有测上传。

修法：`Request.consumed` 记账，收尾只补 `content_length - consumed`。补了两条
真 socket 测试：上传成功后同一连接再发一个请求；以及处理器一个字节没读就
拒（重名 409）时残留的体要被读掉。

## 已知限制

| 限制 | 说明 |
|---|---|
| P95 未在 N5095 上复测 | 开发机上 67.7ms（Phase 0）。N5095 单核性能约为开发机的 40%，预计 P95 在 150ms 上下，仍在 §14.2 的可用区间，但没实测 |
| 一万张规模未实测 | 目前最大实测是 Phase 0 的离线 4385 张。服务端的增量入库路径只测到几十张 |
| 容器以 root 跑 | `/data` 产物属主 root。QTS 上是惯例，但宿主机上手动删要借容器 |
| 没有 HTTPS | 隧道那条由 Cloudflare 终止 TLS；LAN/Tailscale 是明文。Tailscale 自己有加密，LAN 上是明文 HTTP + Bearer |
| 单进程 | ThreadingHTTPServer 一连接一线程，识别并发限 3。够用（就一个用户），但不是横向可扩的架构 |

## 下一步（Phase 2）

纯原生 Android Activity：扫描 → 识别 → 单目标跟踪 → 播放。出口条件是
「真机 AR 体验可接受」，只能在真机上判定。
