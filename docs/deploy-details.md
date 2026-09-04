# 部署背后的取舍与实测数据

[deploy.md](deploy.md) 讲怎么做，[faq.md](faq.md) 讲出问题怎么办。**这份只讲为什么** ——
每个数字是怎么量出来的、每个取舍舍了什么。不必顺着读，按需要翻。

- [两条外网通道，各跑什么](#两条外网通道各跑什么)
- [隧道的三条硬限制](#隧道的三条硬限制)
- [证书不是可选项](#证书不是可选项--它同时管着相机和缓存)
- [CDN：该缓存什么、绝对不该缓存什么](#cdn该缓存什么绝对不该缓存什么)
- [转码与核显硬编](#转码与核显硬编)
- [入库为什么会被拒](#入库为什么会被拒)
- [打印那一侧：两件影响成败的事](#打印那一侧两件影响成败的事)
- [批量入库脚本的几个设计](#批量入库脚本的几个设计)
- [Cloudflare 加速：两篇博客的方法逐条过](#cloudflare-加速两篇博客的方法逐条过)
- [这台机器上量到的基线](#这台机器上量到的基线)
- [升级后为什么要 prune，以及为什么必须带过滤器](#升级后为什么要-prune以及为什么必须带过滤器)
- [在开发机上跑](#在开发机上跑)
- [不用 SSH 的那条路](#不用-ssh-的那条路)
- [不用 GHCR 镜像的两条老路](#不用-ghcr-镜像的两条老路)

---

## 两条外网通道，各跑什么

网页版只有**一个源、一个端口**（三条 URI 各是什么见 [deploy.md](deploy.md)），所以「走哪条
路」完全由用户打开哪个地址决定，没有客户端探活那一层。

| 通道 | 跑什么 | 什么时候用 |
|---|---|---|
| LAN | 全部 | 在家。最快，但**要真证书才能开相机**（见下面「证书不是可选项」） |
| Tailscale | 全部 | 自己和家里人在外面 |
| Cloudflare Tunnel | **全部，含视频** | 宾客。他们不会装 Tailscale |

**视频走 Cloudflare 是 2026-08-05 定下的**，推翻了之前"隧道只跑 API 小包"那条。理由很
直接：网页版的宾客不装任何东西，也就没有第二条路可走 —— 要么视频从隧道出去，要么宾客
根本看不到视频。风险是真的（见下面第三条），是一个被明确接受的取舍，不是被忽略的问题。

## 隧道的三条硬限制

| 限制 | 官方数字 | 对本项目的后果 |
|---|---|---|
| 请求体上限 | Free / Pro **100MB**，Business 200MB | 服务端见到 `CF-Ray` 头时把 `/v1/upload` 的上限收到 **95MiB**（`TUNNEL_MAX_UPLOAD_BYTES`），超了自己先 413 并说明原因，不等 Cloudflare 传到一半掐断。**没超就照传** —— 网页版的正常路径就是隧道。几百 MB 的原片仍要走 LAN 或 Tailscale |
| Proxy Read Timeout | **125 秒**，非 Enterprise 不可调，超时 524（Proxy Write Timeout 30 秒） | 带视频入库是同步请求，软编慢档必然 524。入库走 LAN |
| CDN 服务条款 | 非 Enterprise 套餐**不得**通过 CDN 提供视频或「不成比例的图片、音频、大文件」，要买 Stream / Images；Cloudflare 保留「停用或限制你使用 CDN」的权利，**且通知不保证提前** | 一条视频最大 16.24MiB、实测 14.72MiB。50 个宾客人均 3 条约 **2.2GB**。风险是**账号级**的：同一条 tunnel 上其它服务会一起没 |

第三条**已经被接受**（见上一节）。能降低暴露面的两件事，都在部署层：

- **视频只在婚礼那几个小时里可达。** 用完把那条 ingress 摘掉 —— 长期挂着一个能拉
  2GB 视频的公开地址，风险是按时间累积的。
- **别给 `/v1/*` 或 `/api/*` 加任何缓存规则。** 理由见下面「CDN：该缓存什么、绝对不该
  缓存什么」——那不只是流量问题，是会**跨用户串视频**。

关于 524：隧道超时的时候**照片其实已经入进去了**。写库是整个流程的最后一步（转码
完成之后才写 catalog 和识别库），你看到的只是响应超时；再提交一次会得到 409
`already_ingested` 并带上 photoId，不会重复入库。

## 证书不是可选项 —— 它同时管着相机和缓存

两件事都断在这上面，第二件是 2026-08-05 实测出来的，之前一直不知道：

1. **相机。** `getUserMedia` 只在安全上下文里存在，而局域网 http 不是 —— 完整的
   地址对照表见 [faq.md](faq.md#安全上下文)。
2. **浏览器的磁盘缓存。** Chromium 对**有证书错误的源整体禁用磁盘缓存** —— 自签证书、
   或者点过"继续访问"的那种，全都算。`Cache-Control: immutable` 写了也不生效。

第二条的代价是量过的。同一台手机（小米 M2012K11C / Edge for Android 150）、同一份代码，
只换地址：

| 地址 | 每次打开页面 | 界面可用 |
|---|---|---|
| `https://100.110.121.64:8964`（自签） | 引擎 **4.87MB**（预取 + 装配各一遍，一次都进不了缓存） | **16.0 秒** |
| `http://localhost:8964`（adb reverse，无 TLS） | 首次 2.43MB，**再进 0 字节** | **1.6 秒** |

也就是说自签证书的代价不是"有个警告要点一下"，是**每个宾客每次进页面都重下一遍引擎**。
拿真证书的两条路见 [deploy.md 第 7 步](deploy.md)。

## CDN：该缓存什么、绝对不该缓存什么

网页版有 **2.6MB 的静态资源**是所有宾客共享、且内容永不变的（引擎 2.43MB brotli、
字体 175KB、素材 37KB）。让它们停在 Cloudflare 边缘，宾客就不用每人从 NAS 拉一遍 ——
这是隧道那一段最值钱的一次优化。

### ⛔ 先说不该缓存的

**`/v1/*` 和 `/api/*` 一律不能缓存。** 它们是**按人授权**的：`/api/lib` 是这个用户能扫
的那些照片、`/api/stream/<票>` 是一次性票据换来的视频流。任何一条把它们缓存到边缘的
规则，都会把一个人的视频发给另一个人 —— 而且没有任何症状，你看到的是视频正常播放。

所以**不要开 "Cache Everything" 页规则**（那是老教程里最常见的一条）。要缓存就写成
按路径限定的 Cache Rule，见下面。

### ✅ 该缓存的：`/vendor/*` 与 `/art/*`

Cloudflare 的默认缓存是**按文件扩展名**的，一份固定名单（`.js` / `.css` / `.png` /
`.woff2` / `.mp4` …）。名单里**没有 `.wasm`** —— 而 2.43MB 里的 2.43MB 就是它。所以
默认状态下最大的那一块**不会**被边缘缓存，得自己加一条规则。

Cloudflare 后台 → Caching → Cache Rules，新建一条：

```
名字：photoar static
表达式：(http.host eq "arphoto.<你的域名>") and (starts_with(http.request.uri.path, "/vendor/")
        or starts_with(http.request.uri.path, "/art/"))
设置：
  Cache eligibility        → Eligible for cache
  Edge TTL                 → Use cache-control header if present（回退 1 天）
  Browser TTL              → Respect origin
  Cache key → Query string → Include all（默认；`?v=` 必须参与做键）
```

三件事要留意，都是踩过或差点踩的：

- **`?v=` 必须进缓存键。** 引擎的 URL 是 `/vendor/opencv.wasm?v=<内容哈希前12>`，
  版本号在 query 里（由 `tools/split-wasm.mjs` 写进 opencv.js）。把 query 从键里去掉的
  话，升级 OpenCV 之后边缘会一直发旧字节 —— 而新 js 配旧 wasm 的表现是"函数签名对不上"。
- **`Vary: Accept-Encoding` 已经在发了。** 服务端对 wasm 与 opencv.js 发预压的 brotli，
  同时带 `Vary` 和不同的 ETag。Cloudflare 会按编码分别存，别去动它。
- **`/vendor/opencv.js` 是 `no-cache` 的**，所以它不会被边缘缓存 —— 有意的：它的 URL 里
  没有版本号（一直叫 `opencv.js`），给它 immutable 是个哑雷。128KB（brotli 后 30KB）
  换一次条件请求，划算。

### 怎么确认它真的生效了

```bash
U="https://arphoto.<你的域名>/vendor/opencv.wasm?v=<从 opencv.js 里抄那个版本号>"
curl -sI -H 'Accept-Encoding: br' "$U" | grep -iE 'cf-cache-status|content-encoding|content-length|cache-control'
```

第一次多半是 `cf-cache-status: MISS`，**再打一次要变成 `HIT`**。
`DYNAMIC` = 这条根本没被判定为可缓存（规则没生效或表达式没命中）；
`BYPASS` = 有别的规则或 cookie 把它挡掉了。
`Content-Length` 应该是 2.5MB 左右而不是 11.9MB —— 那说明 brotli 也一路透传了。

## 转码与核显硬编

**为什么是 VAAPI 而不是 QuickSync。** N5095 是 Jasper Lake（Gen11）。Debian trixie 的
ffmpeg 链的是 oneVPL，而它的 GPU runtime（`libmfx-gen1.2`）只覆盖 Gen12+；Gen11 要靠
已弃用的 Media SDK，而 trixie 里 `intel-media-sdk` / `libmfx1` / `libmfxhw64` 三个包
都不存在（实测 `h264_qsv` 报 `MFX session: -9`）。iHD 驱动覆盖 Gen8+，`h264_vaapi`
是这台机器上**唯一走得通的硬编**。

**为什么回退是静默的。** 宁可慢也别让入库全线失败。代价是「以为在用硬编、其实全程
软编」只能靠掐表发现 —— 所以 deploy.md 第 4 步要显式验一次，验过之后把
`video_encoder` 写死成 `"h264_vaapi"`，把静默回退变成报错。

**软编到底有多慢**（本机 3 核配额，同一条 30s/1080p 源）：

| preset | 本机实测 | 折算 N5095（÷3.1） |
|---|---|---|
| `libx264 slow` | 89.2s | ≈ 4.6 分钟 |
| `libx264 veryfast`（默认） | 18.2s | ≈ 56 秒 |
| `h264_vaapi` | 预期再快一个量级 | 待实测 |

入库是同步 HTTP 请求，隧道 125 秒就断 —— 所以软编默认档是 `veryfast` 而不是 `slow`。
慢档换来的**只是同码率下的画质，不是体积**：高码率源下 `-maxrate` 先撞上，`-crf`
根本没约束到，两档产物体积几乎一样。

`resolve_encoder` 不查 `ffmpeg -encoders`（列得出来 ≠ 跑得动），它**真编一帧**。

**视频规格**：30 秒 / 1080p / 4Mbps，per-video 上限因此是 16.24MiB。**没有**文件大小
检查这回事（曾经以为有一个 2.85MB 的，那个误会归档在
[decisions.md 的「从使用者文档下沉的过程叙事」](decisions.md#52-从使用者文档下沉的过程叙事2026-09-03)）。

## 入库为什么会被拒

逐条对照表在 [faq.md](faq.md#入库被拒了)。这里只说**写库顺序**，因为它决定了失败时
库会处在什么状态：

特征 → 自匹配 / 近重复闸门 → 缩略图 → 素材 → 转码 → **最后**写 catalog 和识别库。

前面任何一步失败都不留半条记录。catalog 先于识别库是故意的：那样失败的形状是
「catalog 里有、识别不到」，`check` 能报出来、`reindex` 能修好。

## 打印那一侧：两件影响成败的事

**一、`printWidthMm` 要量，不要算。** 它是照片**画面**在现实里的实际宽度，AR 里视频
贴不贴得住全靠它。冲印店的「6 寸」不等于 152.0mm，同一批不同店能差两三毫米，而且很多
店留白边 —— **拿尺量画面本身，白边不算**。横竖也要对：填的是照片摆在你面前时**水平
方向**那条边，6 寸竖着放是 102mm。差 5% 的表现是视频比照片大一圈或小一圈、边缘对不
齐，**不是「认不出来」**，所以很容易被当成别的问题查半天。

**二、能不能认出来是照片本身决定的。** 密集且分布均匀的高对比纹理最好认；大片天空、
纯色墙面、逆光剪影、糊掉的老照片难认。**判据是实际扫一遍**：入库后拿手机对着屏幕上
的原图扫，几秒内锁定的就没问题。所以：

- **先入库、先试扫，再决定送哪几张。** 不要先把照片印好送出去了才发现认不出来
- 一张照片印两份送两个人可以（同一份文件 → 同一个 photoId → 播同一条视频）；但
  **同一场景连拍的两张**很可能互判近重复，那时两张都认不出来 —— 409 `near_duplicate`
  就是在拦这个，别绕过它

## 批量入库脚本的几个设计

- **进度写在 `batch-ingest-state.json`**（`--state` 可改）。断了、断电、Ctrl-C，再跑
  一次会跳过已入库的和被**确定性拒绝**的（质量分不够 / 近重复 / 不在白名单 / 格式不
  支持）；网络错、超时、5xx 不记账，下次自动重试。换过照片想重试被拒的那批加
  `--retry-rejected`
- **它故意不并发，没有 `--jobs` 这个选项。** 近重复闸门是拿新照片跟库里已有的比，
  而服务端是多线程的（实测 4 路、29.5 次/秒）：并发提交两张互为近重复的照片，两边都
  看到对方还不在库里 → 两张都进去 → **两张都永久识别不出来**。这是正确性，不是快慢
- **路径用 `abspath` 而不是 `realpath`**：`realpath` 会把 `/share/Photo` 解析成
  `/share/CACHEDEV1_DATA/Photo`，正好踩上白名单那个坑
- `--skip-videos` 先只入照片（快得多），之后逐条 `POST /v1/photo/<id>/video` 补视频。
  先看到「能认出来」再补「能播」，比一次全上更容易定位问题
- 入库入口是 HTTP 而不是服务端的子命令：客户端的「关联新照片」页调的是同一个接口，
  两条路走同一段代码

## Cloudflare 加速：两篇博客的方法逐条过

参考：[为 Cloudflare Tunnel 提速](https://blog.dalenull.work/2024/09/28/speed-up-your-cloudflare-tunnel/)、
[利用优选域名加速 Cloudflare tunnel 在中国的访问速度](https://jqtmviyu.github.io/post/cloudflare-cn-perf/)。
两篇讲的是**两段不同的链路**，别混为一谈：第一篇优化「Cloudflare 边缘 → cloudflared（NAS）」，
第二篇优化「手机/亲友 → Cloudflare 边缘」。逐条过完的结论：

| 手段 | 值不值得 | 依据 / 怎么做（一句） |
|---|---|---|
| `TUNNEL_EDGE_IP_VERSION=6` | ✅ 值得先试 | `edge-ip-version` 官方默认是 `4`（不是 `auto`），所以哪怕 NAS 有 v6 也不会用；`curl -s -6 --max-time 8 https://api64.ipify.org` 有输出就给 cloudflared 容器加上它 |
| 边缘 IP 优选（改 `/etc/hosts`） | ⚠️ 先量再说 | 用 `tools/cf_edge_probe.py`。为什么要扫 7844、两个会让人以为「优选没用」的坑、以及「本机实测只差 1.3ms 等于没收益」，全部在那个脚本的模块 docstring 里 |
| 固定 `TUNNEL_TRANSPORT_PROTOCOL=http2` | ⚠️ 有症状再改 | `protocol` 默认 `auto` = 先试 QUIC（UDP/7844）。国内线路对 UDP 限速或干扰时症状是**隧道能连上但抖**（偶发 502、延迟毛刺），怀疑就固定 http2 试一周；线路对 UDP 友好时 QUIC 的丢包恢复更好，只能实测 |
| SaaS 回源优选（分线路 DNS） | ⚠️ 最后手段 | 先实测：`for i in $(seq 10); do curl -o /dev/null -s -w '%{time_total}\n' -H "Authorization: Bearer $T" https://arphoto.<你的域名>/v1/ping; done`，中位数 < 400ms 就别折腾（客户端 2 秒超时、服务端 P95 约 180ms，余量很大）。要上：两个域名 + 支持分线路的 DNS（腾讯云 DNSPod 免费版的「境内 / 境外」够用）+ Cloudflare for SaaS（Free 可用、含 100 个 custom hostname，超出 $0.10/个）。⚠️「优选域名」是第三方，随时失效，且失效时**境内直接连不上而境外一切正常**，很难第一时间归因 |
| **把静态资源缓存到边缘**（Cache Rule） | ✅ **收益最大** | 2.6MB × 每个宾客，见上面[「CDN：该缓存什么、绝对不该缓存什么」](#cdn该缓存什么绝对不该缓存什么) |
| 把视频也挂到隧道上 | ⚠️ 已接受 | 违反 CDN 条款、风险是账号级的。2026-08-05 明确接受，理由见[「两条外网通道」](#两条外网通道各跑什么) |
| Argo Smart Routing（已并入 Smart Shield） | ❌ | 付费加购、官方没有公开的价格与提速数字，而且它优化的是 Cloudflare **网络内部**的路由，对「国内出口 → 最近边缘」那一段无能为力 —— 而那一段恰好是国内慢的主要原因 |
| Cloudflare China Network | ❌ | 唯一真正解决那一段的官方方案，但要 Enterprise 套餐 + 每个顶级域名的 ICP 备案 + 京东云境内节点，个人 NAS 不在射程内 |

另外两个默认值别动：`retries` 默认 5（1/2/4/8/16 秒指数退避，调大只会让故障期更长）；
`region` 目前只能填 `us`（把所有连接固定到美国，对我们只会更慢）。

## 这台机器上量到的基线

本地 Docker 按同样配额（`--cpus=3 --memory=3g`）跑出来的，用来对照 NAS 上的实际表现。
**绝对延迟不可比**（i9-11900K 单核约为 N5095 的 3.1 倍），但形状可比：

| 项目 | 本机（3 核配额） | 折算 N5095 |
|---|---|---|
| 识别 P95（库 300 张，单线程） | 70.7ms | ≈ 220ms |
| 识别 P95（4 路并发） | 177.0ms | ≈ 550ms |
| 并发吞吐 | 29.5 次/秒 | ≈ 9.5 次/秒 |
| 单张入库（含 20 次自匹配） | 1674ms | ≈ 5.2s（一万张 ≈ 14.5 小时） |
| 一条 30s 视频入库（含 `veryfast` 软编转码） | 27.3s | ≈ 85s |
| 转码产物 | 14.72MiB（上限 16.24） | 同 |
| 峰值内存 | 1061MB / 3g | 同（与 CPU 无关） |
| 误识别 | 0 / 200 | — |

完整报告在 `bench/logs/sim-qnap.json`，重跑：
`python3 bench/sim_qnap.py --photos 300 --queries 200`。

## 升级后为什么要 prune，以及为什么必须带过滤器

升级的命令在 [deploy.md 第 8 节](deploy.md#8-升级与回滚)。这里只讲那条 `prune` 为什么
是必须的、以及那一长串 `--filter` 为什么不能省。

**它不是卫生习惯。** `pull` 拿到新的 `latest` 时，docker 把这个 tag 挪到新镜像上，上一份
就**丢掉全部 tag 变成 `<none>`** —— 它没被删，只是没名字了，1.1GB 一个，随升级次数线性
堆积。这是 docker 移动 tag 的固有行为，compose 没有开关能关掉它（`pull` /
`up --pull always` / 换 compose 版本都一样）。

**为什么带那一长串 `--filter`**：不带过滤的 `docker image prune -f` 会清掉**这台机器
上所有服务**的无 tag 镜像。在这台 NAS 上那还有 CloudDrive2、Calibre、cloudflared、
explore_journal 的两个服务 —— 它们的孤儿镜像多半也是垃圾，但这是一条写进文档、每次
升级都跑的命令，让它伸手到项目外面去，出事的那次最难追。过滤用的是镜像自带的
`org.opencontainers.image.source` 标签（在 `Dockerfile` 里打，见那段注释），所以它
**只可能**命中 photo-ar 自己的镜像。

## 在开发机上跑

不碰 NAS，在自己机器上起同一套服务端，手机走 Tailscale 或 `adb reverse` 连过来。用的是
覆盖层 `deploy/compose.local.yml`，和 NAS 那份的差别只有两样：照片/视频目录、镜像本地构建。

```bash
cd photo-ar
mkdir -p local/photos/_inbox local/videos          # 素材放这儿，local/ 在 .gitignore 里
cp .env.example .env                                # 至少填 PHOTOAR_ADMIN_PASSWORD

# 把文件列表写进 .env，之后这个目录里直接 docker compose，覆盖层自动带上
echo 'COMPOSE_FILE=docker-compose.yml:deploy/compose.local.yml' >> .env

docker compose up -d --build
```

**别漏掉那个覆盖层，漏掉的后果不可逆**：主文件挂的是 NAS 的 `/share/Photo`，开发机上
没有那个路径，dockerd 会**以 root 在你的根目录下建出来**（空目录，不报错，服务还真去
索引它）。写进 `.env` 就没机会忘；合并规则见
[deploy/compose.local.yml](../deploy/compose.local.yml) 顶部。

起来之后 `http://127.0.0.1:8964/` 是网页版、`/admin` 是管理台，同一个端口。手机上要开
相机就得 https，或者 `adb reverse tcp:8964 tcp:8964` 后打开 `http://localhost:8964`
（不用证书，调试时最顺手，见 [faq.md](faq.md#局域网里自测没有隧道也没有真证书)）。

两个容易踩的点：

- **管理员口令写在 `.env` 里，不在 compose 里。** 这个仓库是公开的，固定口令写进
  compose 就等于发布出去了（见 [decisions.md 的「开发机上的固定口令为什么不写在 compose 里」](decisions.md#16-开发机上的固定口令为什么不写在-compose-里)）。留空就是 admin / admin + 首登强制改。
- **cpus / mem 刻意不放宽。** 验收条件之一是「在 NAS 的资源预算内跑得动」（N5095 四核）。
  开发机放开了怎么测都快，到 NAS 上才发现撞超时 —— 那就白测了。

## 不用 SSH 的那条路

Container Station →「应用程序」→「创建」，把 `docker-compose.yml` 整份贴进去。两个
代价：

- `PHOTOAR_TOKEN` 得直接写在 YAML 的 `environment:` 里（界面上没有「环境变量另填」
  的地方，也读不到 `.env`）
- 只能用 GHCR 上的镜像 —— 不过这正是部署 compose 的常态（`build:` 已经不在主文件里，
  它只住在开发机的 deploy/compose.local.yml 覆盖层）

而且**后面每一步验证命令都是 SSH 里跑的**，所以 SSH 早晚要开，建议直接走命令行。

## 不用 GHCR 镜像的两条老路

镜像里不含 `vocab.npz`（你自己的数据，入库后自己训）。如果你不想用 GHCR：

**A. 传源码，在 NAS 上构建**

```bash
git clone https://github.com/shimmerjordan/photo_ar /share/Container/photo-ar   # 在 NAS 上
docker compose build && docker compose up -d
```

N5095 上首次构建几分钟（光装 opencv 那层约 1 分钟），中途别 Ctrl-C。只改 `src/` 的话
重建很快 —— Dockerfile 把依赖单独放了一层。

**B. 在开发机上构建，把镜像搬过去**（NAS 上不装构建链，也不占 CPU）

```bash
docker build -t photo-ar-server:dev .
docker save photo-ar-server:dev | gzip -1 | ssh admin@<NAS> 'gunzip | docker load'
# 实测传输量约 330MB（镜像 815MB，gzip -1 之后）
```

然后在 NAS 的 `.env` 里写 `PHOTOAR_IMAGE=photo-ar-server:dev`，`up -d` 就会用它。

