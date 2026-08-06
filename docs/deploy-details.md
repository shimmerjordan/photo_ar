# 部署背后的取舍、实测数据与排障

[deploy.md](deploy.md) 只讲怎么做。这份讲**为什么这么做**、数字是怎么量出来的、
以及出问题时怎么定位。不必顺着读，按需要翻。

- [三条通道为什么这么分](#三条通道为什么这么分)
- [隧道的三条硬限制](#隧道的三条硬限制)
- [客户端选通道的规则（和一个兜底）](#客户端选通道的规则和一个兜底)
- [要不要再加一层 Cloudflare Access](#要不要再加一层-cloudflare-access)
- [转码与核显硬编](#转码与核显硬编)
- [入库为什么会被拒](#入库为什么会被拒)
- [打印那一侧：两件影响成败的事](#打印那一侧两件影响成败的事)
- [批量入库脚本的几个设计](#批量入库脚本的几个设计)
- [Cloudflare 加速：两篇博客的方法逐条过](#cloudflare-加速两篇博客的方法逐条过)
- [排障](#排障)
- [这台机器上量到的基线](#这台机器上量到的基线)
- [升级、备份、恢复](#升级备份恢复)
- [不用 SSH 的那条路](#不用-ssh-的那条路)
- [不用 GHCR 镜像的两条老路](#不用-ghcr-镜像的两条老路)

---

## 两条外网通道，各跑什么

网页版只有**一个源、一个端口**（`/` 网页、`/admin` 管理台、`/v1/*` API），所以"走哪条
路"完全由用户打开哪个地址决定 —— 没有客户端探活那一层了（那是已下线的安卓客户端的
`EndpointResolver`）。

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

1. **相机。** `getUserMedia` 只在安全上下文里存在。`https://` 任意域名算，
   `http://localhost` 算，**`http://192.168.x.x` 与 `http://100.x.x.x` 都不算。**
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
规则，都会把一个人的视频发给另一个人 —— 而且没有任何症状，你看到的是"能播"。

所以**不要开 "Cache Everything" 页规则**（那是老教程里最常见的一条）。要缓存就写成
按路径限定的 Cache Rule，见下面。

### ✅ 该缓存的：`/vendor/*` 与 `/art/*`

Cloudflare 的默认缓存是**按文件扩展名**的，一份固定名单（`.js` / `.css` / `.png` /
`.woff2` / `.mp4` …）。名单里**没有 `.wasm`** —— 而 2.43MB 里的 2.43MB 就是它。所以
默认状态下最大的那一块**不会**被边缘缓存，得自己加一条规则。

Cloudflare 后台 → Caching → Cache Rules，新建一条：

```
名字：photoar static
表达式：(http.host eq "ar.你的域名") and (starts_with(http.request.uri.path, "/vendor/")
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
U="https://ar.你的域名/vendor/opencv.wasm?v=<从 opencv.js 里抄那个版本号>"
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

**视频规格**：30 秒 / 1080p / 4Mbps，per-video 上限因此是 16.24MiB。（曾经以为有个
2.85MB 的文件大小检查，其实从来没有 —— 那个数只是 `15s × (1500k + 96k) / 8` 算出来
的旧规格产物大小。）

## 入库为什么会被拒

| 状态码 | 原因 | 怎么办 |
|---|---|---|
| 422 `quality_too_low` | arcoreimg 质量分 < 75 | **这个比例很高**：本地拿 869 张真实照片实测，569 张被拒（65%）。换图，或给照片留一圈有纹理的边 |
| 409 `already_ingested` | 同内容已入库 | photoId 是内容哈希，同内容必然同 id |
| 409 `near_duplicate` | 与库中某张过于相似 | 会列出冲突对象。两张都留着的后果是**两张都永远认不出来** |
| 403 `path_denied` | 路径在 `roots` 白名单外 | 响应体不回显被拒路径（免得变成探测工具），看 NAS 日志 |

`path_denied` 最常见的成因是 `/share/Photo` 和 `/share/CACHEDEV1_DATA/Photo` 混用：
前者是符号链接。挂载时冒号两边写成一样、白名单写 `/share/Photo`，然后**别用 `find`
出来的 CACHEDEV 路径提交**。批量脚本的 `--map` 就是为已经不一致的情况准备的。

**写库顺序**是：质量分 → 特征 → 自匹配 / 近重复闸门 → imgdb / 缩略图 → 素材 → 转码 →
**最后**写 catalog 和识别库。前面任何一步失败都不留半条记录。catalog 先于识别库是
故意的：那样失败的形状是「catalog 里有、识别不到」，`check` 能报出来、`reindex`
能修好。

## 打印那一侧：两件影响成败的事

**一、`printWidthMm` 要量，不要算。** 它是照片**画面**在现实里的实际宽度，AR 里视频
贴不贴得住全靠它。冲印店的「6 寸」不等于 152.0mm，同一批不同店能差两三毫米，而且很多
店留白边 —— **拿尺量画面本身，白边不算**。横竖也要对：填的是照片摆在你面前时**水平
方向**那条边，6 寸竖着放是 102mm。差 5% 的表现是视频比照片大一圈或小一圈、边缘对不
齐，**不是「认不出来」**，所以很容易被当成别的问题查半天。

**二、能不能过质量闸门是照片本身决定的。** 869 张真实照片里 569 张（65%）被 422 拒掉。
这不是故障，是 ARCore 对增强图像的硬要求：密集且分布均匀的高对比纹理。大片天空、纯色
墙面、逆光剪影、糊掉的老照片基本过不去；人多、建筑、树叶、图案衣服、有细节背景的合影
通过率高。所以：

- **先入库，再决定送哪几张。** 不要先把照片印好送出去了才发现认不出来
- 没过的那批，`batch-ingest-state.json` 的 `rejected` 里有每张的分数（满分 100，
  门槛 75）。60 出头的换个裁切也许能过，个位数的别折腾
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

两篇讲的是**两段不同的链路**，别混为一谈：

```
手机/亲友 ──①──→ Cloudflare 边缘 ──②──→ cloudflared（NAS）
             第二篇优化这一段        第一篇优化这一段
```

**先看结论**：

| 手段 | 值不值得 | 前提 / 判据 |
|---|---|---|
| `TUNNEL_EDGE_IP_VERSION=6` | ✅ 值得先试 | NAS 有 v6 出口（`curl -6 api64.ipify.org` 有输出） |
| 边缘 IP 优选（脚本） | ⚠️ 先量再说 | 脚本报的「整段最快」比「DNS 最快」好几十毫秒才动手；本机实测只差 1.3ms |
| 固定 `http2` | ⚠️ 有症状再改 | 隧道抖、偶发 502 |
| SaaS 回源优选 | ⚠️ 最后手段 | 外网 `/v1/ping` 中位数明显超过 400ms |
| Argo / Smart Shield | ❌ | 优化不到国内出口那一段 |
| China Network | ❌ | 要 Enterprise + ICP 备案 |
| **把静态资源缓存到边缘**（Cache Rule） | ✅ **收益最大** | 2.6MB × 每个宾客，见上一节 |
| 把视频也挂到隧道上 | ⚠️ 已接受 | 违反 CDN 条款、风险是账号级的。2026-08-05 明确接受，理由见「两条外网通道」 |

### 让 cloudflared 走 IPv6

`edge-ip-version` **默认是 `4`**（官方文档明确的默认值，不是 `auto`），所以哪怕 NAS
有 v6 也不会用。很多家宽的 v6 不限速、不做 NAT、丢包更少。

```bash
curl -s -6 --max-time 8 https://api64.ipify.org; echo   # 先确认真有 v6 出口
```

有输出再给 cloudflared 容器加 `TUNNEL_EDGE_IP_VERSION: "6"`（或 `auto`），重启后用
`cloudflared tunnel info` 看连接是否落在 `2606:4700:a0::/48` / `a8::/48`。

### 边缘 IP 优选

`cloudflared` 主动连 `region1.v2.argotunnel.com` / `region2.v2.argotunnel.com` 的
**7844** 端口，而这两个域名各只解析出 20 个地址；同一网段里不同地址的线路质量能差一个
量级，DNS 给哪 20 个纯属运气。

```bash
python3 tools/cf_edge_probe.py            # 扫 v4
python3 tools/cf_edge_probe.py --v6       # NAS 有 v6 出口时
```

脚本整段扫 7844、按握手耗时排序，打印可直接粘进 `/etc/hosts` 的两行，并且**会告诉你
优选到底值不值** —— 它同时报「DNS 给的 20 个里最快多少」和「整段最快多少」。开发机上
2026-07-30 实测：

```
region1  198.41.192.0/24：254 个地址全部在 7844 上应答
    33.3 ms  198.41.192.229        ← 整段最快
    DNS 给的 20 个里最快 34.6 ms
region2  198.41.200.0/24：254 个全应答，整段最快 33.4 ms
```

**差 1.3ms，等于没有收益。** 所以正确用法是先在 NAS 上跑一次看那两个数差多少，差几十
毫秒或者 DNS 那批大面积超时才值得动 hosts（国内线路上这两种情况都不罕见）。

两个坑，都会让人以为「优选没用」：

- 两行必须来自**不同网段**（一个 `192.x`、一个 `200.x`）。cloudflared 默认建 4 条连接
  并要求分布在两个 region，两行填同一段会让 Tunnel 变成 **Degraded**，比不优选更差
- 测的必须是 **7844** 而不是 443。拿 443 挑出来的地址可能根本不在 7844 上服务，表现是
  启动慢、日志刷 `retrying`

脚本里写死的网段是 2026-07-30 用 `dig` 核过的（region1 → `198.41.192.0/24` +
`2606:4700:a0::/48`，region2 → `198.41.200.0/24` + `2606:4700:a8::/48`）。Cloudflare
换网段的话脚本会自己发现并提示 —— 它拿域名当前解析结果比对写死的网段。

### 协议：quic 还是 http2

`protocol` 默认 `auto` = 先试 QUIC（UDP/7844），不通再退 http2（TCP/7844）。国内很多
线路对 UDP 限速或干扰，症状是**隧道能连上但抖**（偶发 502、延迟毛刺）。怀疑就固定
`TUNNEL_TRANSPORT_PROTOCOL: "http2"` 试一周。反过来，线路对 UDP 友好时 QUIC 的丢包
恢复更好 —— 只能实测，没有普适答案。

顺带两个默认值：`retries` 默认 5（1/2/4/8/16 秒指数退避，别调大）；`region` 目前只能
填 `us`（把所有连接固定到美国，对我们只会更慢，别填）。

### SaaS 回源优选

优化的是**①那一段**：手机 → Cloudflare 边缘。做法是分线路 DNS：境内解析到「优选域名」，
境外解析到回退源，靠 Cloudflare for SaaS 的 Custom Hostname 让两条路的 `Host` 头都是
你的域名，边缘据此路由到同一个 tunnel。

查证过的成本与前提：

- Cloudflare for SaaS **Free 套餐可用**，含 100 个 custom hostname，超出 $0.10/个
  （Free 档不支持通配符 custom hostname、自定义证书、Non-SNI）
- 需要**两个域名**（一个对外、一个当回退源）
- 需要支持分线路的 DNS：腾讯云 DNSPod 免费版的基本线路里就有「境内 / 境外」，够用
- 「优选域名」是第三方（大厂域名或网友维护的解析），随时失效，且失效时的表现是
  **境内用户直接连不上而境外一切正常**，很难第一时间归因

值不值得取决于流量形状：隧道上只有 API 小包，优化的是握手和 RTT，不是吞吐。建议顺序：

1. 先在常用的外网环境（4G、公司 WiFi）实测：
   `for i in $(seq 10); do curl -o /dev/null -s -w '%{time_total}\n' -H "Authorization: Bearer $T" https://arphoto.<你的域名>/v1/ping; done`
2. 中位数 < 400ms → 别折腾（识别请求客户端 2 秒超时，服务端 P95 约 180ms，余量很大）
3. 明显更慢或大面积超时 → 再上这一套。它不改服务端，纯 DNS + 面板配置，随时可退

### 明确不值得的两条

- **Argo Smart Routing**（现已并入 Smart Shield）：付费加购，官方没有公开的价格与提速
  数字，而且它优化的是 Cloudflare **网络内部**的路由，对「国内出口 → 最近边缘」这一段
  无能为力 —— 而那一段恰好是国内慢的主要原因
- **Cloudflare China Network**：唯一真正解决①那段的官方方案，但要 Enterprise 套餐 +
  每个顶级域名的 ICP 备案 + 京东云境内节点，个人 NAS 不在射程内

## 排障

| 症状 | 先查什么 | 命令 |
|---|---|---|
| `/v1/ping` 通、识别一直未命中 | 词汇树和库对不上 | `photoar-server -c /config/config.json check`，必要时 `reindex --rebuild-words` |
| 入库全部 422 | 正常（实测 65% 会被拒），不是故障 | 看响应里的分数，换纹理更丰富的照片 |
| 转码特别慢 | 是不是回退软编了 | deploy.md 第 4 步那一行；`docker stats` 看 CPU 是否打满 |
| 隧道 524 | 请求超过 125 秒 | 带视频的入库走 LAN。注意照片其实已经入进去了 |
| 隧道 413 `upload_via_tunnel` | 单个文件超 95MiB | 那一个文件走 LAN 或 Tailscale。**小文件也报这个 = 镜像太老**（v0.1.0 及之前是"带 `CF-Ray` 就一律拒"，跟体积无关），升级镜像 |
| 隧道 502，但**容器完全健康** | ingress 写了 `http://` 而容器在说 TLS | 见下面「502 而容器是绿的」 |
| **发了新版但行为还是旧的**（接口 400、样式没生效） | Cloudflare 的 Browser Cache TTL 覆盖了源站的 `no-cache` | 见下面「新版发了，用户拿到的还是旧的」 |
| 隧道 502 / 偶发失败 | Tunnel 是否 Degraded | `cloudflared tunnel info <tunnel 名>`，看连接是否分布在两个 region |
| 扫描时整台 NAS 发木 | CPU 配额 | compose 里 `cpus: "3.0"` 是故意留一核给 cloudflared / QTS 的，别调到 4 |
| 容器被 OOM kill | 内存 | 本地实测峰值 1061MB / 3g 上限，还有两倍余量；真 OOM 说明库规模远超一万，调 `mem_limit` |
| 服务拒绝启动，说三份记录条数不齐 | 入库中途断电了 | 跑 `reindex`。**这个拒绝是故意的** —— 错位一位的后果是「识别命中后播的是别人的视频」 |
| 手机上所有通道都 401 | token 不一致 | 改了 `.env` 并重启了容器，但手机里还是旧的 |

### 502 而容器是绿的

2026-08-06 真踩到的，值得单列 —— 因为**所有常规检查都告诉你一切正常**：
`docker ps` 是 `Up (healthy)`、`docker logs` 干干净净、healthcheck 也过
（它在容器**内部**探，走的是回环，根本不经过隧道那条路）。

根因：容器配了 `WEBFRONT_TLS_CERT` / `WEBFRONT_TLS_KEY`，于是网页版在 8964 上说的是
**TLS**；而隧道的 ingress 写的是 `service: http://localhost:8964`。cloudflared 拿明文
去打一个 TLS 端口，连接被对端在握手阶段丢掉 → 502。

**一句话确诊**（在 NAS 上）：

```bash
curl -s  -o /dev/null -w 'http  %{http_code}\n'  http://127.0.0.1:8964/healthz   # 挂掉 / 000
curl -sk -o /dev/null -w 'https %{http_code}\n' https://127.0.0.1:8964/healthz   # 200
```

`http` 那条报 `curl: (52) Empty reply from server`、而 `https` 那条 200，就是它。

两条修法，**必须二选一并保持两侧一致**：

| | 容器侧 | ingress |
|---|---|---|
| **走隧道（推荐）** | 不设 `WEBFRONT_TLS_*` | `service: http://localhost:8964` |
| 要保住局域网直连开相机 | 设 `WEBFRONT_TLS_*` | `service: https://localhost:8964` + `originRequest: {noTLSVerify: true}` |

推荐第一条：走隧道时证书由 Cloudflare 提供，容器再包一层对公网访问**零收益**——浏览器
看到的是 Cloudflare 的证书，容器那层它根本看不见。代价是局域网直连
`http://192.168.x.x:8964` 不是安全上下文、**相机用不了**；但入库和管理台都不需要相机，
所以只影响「断网退回局域网给宾客扫」这个备份方案。

⚠️ 改 `WEBFRONT_TLS_*` 之后要 **`docker compose up -d`**，不能 `docker compose restart`
—— 后者不重新读 `.env`，容器还带着老环境变量跑。

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

## 升级、备份、恢复

**升级服务端**：`docker compose pull && docker compose up -d`（自己改了代码就
`build` 而不是 `pull`，依赖层有缓存，通常几十秒）。

镜像**只在手动跑 workflow 且勾了 publish 时**才发新的（Actions → server →
Run workflow，版本号在界面上填）——往 main 推代码、甚至打 git tag，都不会动镜像。
而 `latest` 还要再勾一次「同时更新 latest」才会挪，所以 `pull` 拿到的 `latest`
一定是某次特意发布**并特意指定**的版本。想钉死版本就在 `.env` 里写
`PHOTOAR_IMAGE=ghcr.io/shimmerjordan/photo-ar-server:0.2.0`。

什么时候需要动库，其余情况都不用：

| 改了什么 | 要做什么 | 不做的表现 |
|---|---|---|
| `vocab.npz` | `reindex --rebuild-words` | **识别率突然掉到底，而日志一切正常** |
| 特征提取参数（ORB / 描述子） | 全库重新入库 | 同上，且 `check` 也看不出来 |
| 只改了服务端逻辑 / 接口 | 什么都不用 | — |
| 照片原文件被移动或改名 | `verify` 看报告，重新关联 | 详情页 `refStale`，识别仍在（用的是入库时存的特征） |

**改了网页版**：`docker compose up -d --build`。前端没有构建步骤，但它在镜像里 ——
改了 `web-front/public/` 不重建镜像是不会生效的。宾客那边刷新一下页面就是新的
（HTML 与 js 都是 `no-cache`，只有 `vendor/` 和字体是 immutable）。

**备份**：值钱的只有 `data/`（每个文件的作用见 [deploy/README.md](../deploy/README.md)
的表）。`imgdb/`、`thumb/`、`playable/` 丢了只能重新入库再生成，所以别只备份
`catalog.db`。SQLite 正在被写时拷出来的文件可能是坏的，停一下再拷最省心：

```bash
docker compose stop
sudo tar czf /share/Backup/photo-ar-data-$(date +%F).tar.gz data/    # 属主是 root
docker compose start
```

一万张量级下 `data/` 的大头是 `playable/`（每条最大 16.24MiB）。空间紧的话可以只备份
`catalog.db` + `library/` + `imgdb/` + `thumb/`，排除 `playable/` —— 它能从原视频重新
转码出来（代价是每条几十秒）。

**恢复到一台新 NAS**：`data/` 拷回去、`vocab.npz` 用**同一份**、照片和视频原文件放回
**同样的路径**（`roots` 按路径存，路径变了要 `verify` 后重新关联）。

**换 token**：改 `.env` → `docker compose up -d` → 手机「设置」里改成新的。旧 token
立刻失效，客户端表现是所有通道 401（原因会写在卡片下面）。

## 不用 SSH 的那条路

Container Station →「应用程序」→「创建」，把 `docker-compose.yml` 整份贴进去。两个
代价：

- `PHOTOAR_TOKEN` 得直接写在 YAML 的 `environment:` 里（界面上没有「环境变量另填」
  的地方，也读不到 `.env`）
- `build: .` 在界面里没有构建上下文，只能用 GHCR 上的镜像（把 `build:` 那行删掉）

而且**后面每一步验证命令都是 SSH 里跑的**，所以 SSH 早晚要开，建议直接走命令行。

## 不用 GHCR 镜像的两条老路

镜像里不含 `arcoreimg`（闭源、不可再分发）和 `vocab.npz`（你自己的数据），两个都靠
bind mount 送进容器。如果你不想用 GHCR：

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

## 新版发了，用户拿到的还是旧的

**症状**：容器日志里版本号是新的，`curl` 拿到的文件是新的，边缘节点上的也是新的 ——
只有浏览器里是旧的。表现是接口报 400（旧客户端打新服务端）、改的样式没生效、
或者"这个功能明明修了啊"。**排查会全部指向别处**，因为三个能查的地方都显示正常。

**原因**：Cloudflare 的 **Browser Cache TTL** 会**覆盖源站的 `Cache-Control`**。

```
$ curl -sI https://<你的域名>/api.js | grep -iE 'cache-control|cf-cache-status'
cache-control: max-age=14400      ← 源站发的是 no-cache，被换掉了
cf-cache-status: REVALIDATED
```

`max-age=14400` = 四小时内浏览器**连问都不会问**。普通刷新（F5）也不会 —— 只有
"硬刷新"（长按刷新键 / Ctrl+Shift+R）才绕得过去，而那件事没人知道要做。

HTML 不受影响（Cloudflare 默认不缓存 HTML，`/` 是 DYNAMIC），所以页面框架是新的、
里面的 JS 是旧的 —— 这个组合让症状更难认。

### 修

**Cloudflare 控制台 → Caching → Configuration → Browser Cache TTL → `Respect Existing Headers`。**

源站已经发的是正确的头（`no-cache` + ETag：每次问一句，没变就 304，没有下载成本），
让 Cloudflare 别改它就行。改完再 `curl -I` 确认一次 —— 上面那行应该变成 `no-cache`。

`.wasm` / `.woff2` 那两个是 URL 带内容哈希的，源站给的是
`max-age=31536000, immutable`，Respect 之后它们仍然被长缓存，不受影响。

### 应用自己也会说

`web-front/public/staleguard.js`：`/staleguard.js` 带着一个**跟 JS 包一起被缓存**的
版本号，`/api/config`（`no-store`，CDN 不缓存）带着服务端**当下**的版本号。两个不等
就在第一屏停住并说明白，同时给一个能真正自救的按钮（`fetch(url, {cache:'reload'})`
逐个重取再刷新 —— 普通 `location.reload()` 对还在有效期内的资源不发任何请求）。

**它不能替代把 CDN 配对**：加这个探测的那一版自己救不了自己（用户手上如果是"还没有
这段代码"的旧包，这段代码就不会跑），从下一次部署开始才生效。
