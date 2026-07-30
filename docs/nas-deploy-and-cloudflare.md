# 在 QNAP 上把 photo-ar 跑起来，并从外网访问

目标机器：QNAP TS-464C2（Celeron N5095，4C/4T，8GB），QTS + Container Station，
已经在跑 cloudflared（tunnel 名 `nas-adan`）与 CloudDrive2。

这份文档是**按顺序做完就能用**的流程；每一步都带验证命令，验证不过就不要往下走。
只讲「怎么做」和「怎么确认做成了」，设计上的理由在
[spec](superpowers/specs/2026-07-28-ar-photo-video-design.md)，日常维护命令在
[deploy/README.md](../deploy/README.md)。

## 0. 先说清楚：三条通道各管什么

这个设计里**媒体和 API 走的不是同一条路**，这不是可选项，是 Cloudflare 的条款和
限制决定的（§7 有依据）：

| 通道 | 跑什么 | 什么时候用 | 谁配 |
|---|---|---|---|
| LAN（`http://<NAS 内网 IP>:8964`） | API + 视频 | 在家。默认，最快 | 第 3 步 |
| Tailscale（`http://<tailscale IP>:8964`） | API + 视频 | 在外面看视频 | 第 6 步 |
| Cloudflare（`https://arphoto.<你的域名>`） | **只跑 API 小包** | 在外面、且没开 Tailscale | 第 7 步 |

客户端会同时探这几条，按可用性和延迟挑（`EndpointResolver`），所以**三条都配上**
是常态，不是三选一。视频永远不走 Cloudflare —— 原因见 §7.1，那一节请务必读。

## 1. 前置清单

- [ ] Container Station 已装，能 SSH 进 NAS 并执行 `docker`（QTS 的 docker 在
      `/share/ZFS530_DATA/.qpkg/container-station/bin/docker`，一般已在 PATH 里；
      `docker compose` 是 v2 插件形式）
- [ ] 照片、视频所在的共享文件夹路径记下来（例：`/share/Photo`、`/share/Video`、
      CloudDrive2 的挂载点 `/share/CloudDrive`）
- [ ] `arcoreimg`：ARCore SDK for Android 里的 `tools/arcoreimg/linux/arcoreimg`，
      闭源二进制不在仓库里，要自己拷到 `tools/arcoreimg` 并 `chmod +x`
- [ ] 词汇树 `vocab.npz`：**服务端不训练**，必须先在有 Python 环境的机器上
      `photoar build --photos <一批风格接近的照片> --out /tmp/corpus`，把
      `/tmp/corpus/vocab.npz` 拷到 NAS 的 `data/vocab.npz`
- [ ] 一个随机 token：`openssl rand -hex 24`

> 换过 `vocab.npz` 就必须 `reindex --rebuild-words`。不重建的表现是**识别率突然
> 掉到底而日志一切正常** —— 库里存的词序列是旧树量化出来的，倒排索引指向错误的桶。

## 2. 放在哪

```
/share/Container/photo-ar/          ← 整个仓库 clone 到这里
├── data/                           ← 持久卷：catalog.db / library / imgdb / thumb / playable
│   └── vocab.npz                   ← 第 1 步拷进来的
├── deploy/config.json              ← 从 config.example.json 复制后改
├── tools/arcoreimg                 ← 第 1 步拷进来的
└── docker-compose.yml
```

`deploy/config.json` 里必须改的只有 `roots`，且**必须写容器内路径**，与
`docker-compose.yml` 的 `volumes` 一一对应：

```json
"roots": {
  "照片": "/share/Photo",
  "视频": "/share/Video",
  "网盘": "/share/CloudDrive"
}
```

token **不写进配置文件**，用环境变量 `PHOTOAR_TOKEN` 传（compose 里已经接好）。

## 3. 起服务

```bash
cd /share/Container/photo-ar
export PHOTOAR_TOKEN=$(openssl rand -hex 24); echo "$PHOTOAR_TOKEN"   # 记下来，客户端要填
docker compose build
docker compose up -d
docker compose logs -f photo-ar-server      # 看到 listening on 0.0.0.0:8964 就行
```

验证（在 NAS 上或同网段任意机器）：

```bash
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" http://<NAS 内网 IP>:8964/v1/ping
# {"ok": true, "version": "phase1", "serverTime": 1753...}
```

没带 token 应该回 401 —— 顺手确认一下，这是唯一一层鉴权：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<NAS 内网 IP>:8964/v1/ping   # 401
```

## 4. 验证核显硬编（这一步不能跳）

**开发机上验证不了这条路径**：开发机只有 NVIDIA 卡
（`/sys/class/drm/renderD128/device/driver → nvidia`），Intel VAAPI 是在这台 NAS
上第一次真正跑到。所以下面三步请在 NAS 上实际执行。

配置里 `video_encoder` 默认是 `"auto"`：探测到核显用 `h264_vaapi`，探测不到
**静默回退 libx264**。静默是故意的（宁可慢也别让入库全线失败），但代价是
「以为在用硬编、其实全程软编」只能靠掐表发现 —— 所以要显式验一次。

**4.1 设备节点在不在**

```bash
ls -l /dev/dri/                                  # 至少要有 renderD128
stat -c '%g %a' /dev/dri/renderD128              # 记下 GID 和权限
```

`/dev/dri` 不存在 → BIOS 里核显被关了，或者 QTS 这个型号没暴露；后面只能软编。

**4.2 容器里驱动认不认这块核显**

```bash
docker compose exec photo-ar-server vainfo 2>&1 | head -20
```

要看到 `Driver version: Intel iHD driver` 和一串 `VAProfileH264...
VAEntrypointEncSlice`。有 `VAEntrypointEncSlice` 才是能**编**，只有
`VAEntrypointVLD` 是只能解。

如果报 `failed to open /dev/dri/renderD128: Permission denied`，把 4.1 里
`stat` 出来的 GID 填进 `docker-compose.yml` 已经写好的 `group_add`（取消注释）
再 `docker compose up -d`。

**4.3 让服务自己说它选了谁**

```bash
docker compose exec photo-ar-server python -c \
  "from photoar import transcode as T; print(T.resolve_encoder('auto'))"
# h264_vaapi  ← 想要的
# libx264     ← 回退了，回到 4.1/4.2 查
```

`resolve_encoder` 不是查 `ffmpeg -encoders`（那个列得出来 ≠ 跑得动），它是**真编
一帧**。所以这一行输出 `h264_vaapi` 就等于硬编真的能跑。

**4.4 想把回退变成报错**

确认硬编可用之后，把 `deploy/config.json` 的 `video_encoder` 从 `"auto"` 改成
`"h264_vaapi"`，重启。这样一旦哪天核显不可用（QTS 升级、设备号变化），入库会
**直接报错**而不是悄悄退回软编。为什么值得这么做，看这个数：

| preset | 本机 3 核实测（30s/1080p 一条） | 折算 N5095（÷3.1） |
|---|---|---|
| libx264 `slow` | 89.2s | ≈ 4.6 分钟 |
| libx264 `veryfast`（默认） | 18.2s | ≈ 56 秒 |
| h264_vaapi | 预期再快一个量级 | 待你实测 |

入库时的 attach 是**同步 HTTP 请求**，而 Cloudflare 的 Proxy Read Timeout 是
**125 秒**（§7.1）—— 软编 + 慢 preset 走隧道入库必然 524。这也是为什么软编默认档
是 `veryfast` 而不是 `slow`：产物体积几乎一样（高码率源下 `-maxrate` 先撞上，
`-crf` 根本没约束到），慢档换来的只是同码率下的画质。

## 5. 入库

Phase 1 只有 HTTP 入口。一张照片 + 一条视频：

```bash
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"refPath":"/share/Photo/2019/IMG_0421.jpg",
       "videoPath":"/share/Video/2019/IMG_0421.mov",
       "printWidthMm":152,
       "title":"外婆家院子"}' \
  http://<NAS 内网 IP>:8964/v1/photo
```

- `printWidthMm` 是**打印出来的实际宽度**，不是像素宽度；跟踪精度直接依赖它，
  所以没有默认值。6 寸 152mm / 5 寸 127mm / 4 寸 102mm
- 先入库照片、之后再补视频：`POST /v1/photo/<photoId>/video`，body
  `{"videoPath": "..."}`
- **批量入库请在 LAN 上做**，不要走隧道：一张照片约 1.7 秒（实测中位数
  1674ms，N5095 上更慢），带视频的还要加转码时间

被拒的几种情况都带明确原因：

| 状态码 | 原因 | 怎么办 |
|---|---|---|
| 422 `quality_too_low` | arcoreimg 质量分 < 75 | 大片天空、纯色背景、过曝、模糊都会。**这个比例很高**：本地拿一批真实照片实测，869 张里 569 张被拒（65%）。换图，或给照片留一圈有纹理的边 |
| 409 `already_ingested` | 同内容已入库 | photoId 是内容哈希 |
| 409 `near_duplicate` | 与库中某张过于相似 | 两张都留着的后果是**两张都永远认不出来**，会列出冲突对象 |
| 403 `path_denied` | 路径在 `roots` 白名单外 | 响应体不回显被拒路径，看 NAS 日志 |

入库完自检一遍：

```bash
docker compose exec photo-ar-server photoar-server -c /config/config.json check
```

## 6. Tailscale：在外面也能看视频的那条路

Cloudflare 那条通道**不传视频**（§7.1），所以「在外面想看」靠 Tailscale。

NAS 上装好 Tailscale（QTS 有 QPKG，或跑官方容器）后，客户端把 mediaBase 填成
`http://<NAS 的 tailscale IP>:8964`。手机端装 Tailscale App 并登录同一个
tailnet 即可，不需要开 Funnel（Funnel 会把流量重新推到公网，等于又回到带宽和
条款的问题上）。

验证：手机连 4G、开 Tailscale，浏览器打开
`http://<tailscale IP>:8964/v1/ping` 应该要 401（没带 token）而不是连不上。

## 7. Cloudflare：加一条 ingress，不要新建 tunnel

`*.<你的域名>` 的通配符 CNAME 已经指向现有的 `nas-adan`，所以**不新建 tunnel，
也不改 DNS**，只在 cloudflared 的配置里加一条规则。

编辑 NAS 上 cloudflared 的 `config.yml`（QNAP 上通常在
`/share/Container/cloudflared/config.yml`），插在 **404 兜底那条之前**：

```yaml
ingress:
  # ...已有规则...

  - hostname: arphoto.<你的域名>
    service: http://127.0.0.1:8964
    originRequest:
      connectTimeout: 30s
      noHappyEyeballs: true

  - service: http_status:404      # 必须留在最后
```

```bash
docker restart cloudflared
```

验证（从任意外网环境，比如手机 4G）：

```bash
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" https://arphoto.<你的域名>/v1/ping
```

隧道健康状态：

```bash
docker exec cloudflared cloudflared tunnel info nas-adan
```

要看到 4 条连接、落在**两个不同的 region**。只落一个 region 会显示 `Degraded`，
表现是偶发 502 而不是彻底不通 —— 这一点在 §8.2 还会再提一次。

### 7.1 隧道上的三条硬限制（这决定了媒体为什么不走它）

| 限制 | 官方数字 | 对本项目的后果 |
|---|---|---|
| 请求体上限 | Free / Pro **100MB**，Business 200MB | `/v1/upload` 传原片会被 413 掉。客户端探测到走隧道时**隐藏上传入口**，服务端见到 `CF-Ray` 头的上传请求直接 413 并说明原因 |
| Proxy Read Timeout | **125 秒**（非 Enterprise 不可调），超时 524；Proxy Write Timeout 30 秒 | 带视频入库是同步请求，软编慢档必然 524（见 §4.4）。批量入库走 LAN |
| CDN 服务条款 | 非 Enterprise 套餐**不得**通过 CDN 提供视频或「不成比例的图片、音频、大文件」，要用 Stream / Images / Developer Platform；Cloudflare 保留「停用或限制你使用 CDN」的权利，且**通知不保证提前** | 一条视频最大 16.2MiB、实测 14.9MiB。真把媒体挂上去，赔的是整个账号（`nas-adan` 上其它服务一起没） |

所以隧道上只跑 API：一次识别上行约 50KB（JPEG 帧）、下行 <2KB。这个量级下，
下面所有「加速」手段的收益都要打折看 —— 见 §8.6。

### 7.2 要不要再加一层 Cloudflare Access

服务端唯一的鉴权是 Bearer token（`hmac.compare_digest` 定长比较，没有时序泄漏），
token 一泄漏就等于全开。要再加一层的话：

- **Access + Service Token**：客户端每个请求多带
  `CF-Access-Client-Id` / `CF-Access-Client-Secret` 两个头。安全性实打实提高，
  代价是 Android 客户端要改（现在只发 `Authorization`），而且 token 轮换要两边一起换
- **WAF 规则限国家/ASN**：零改动，能挡掉绝大多数扫描流量，但挡不住定向
- **什么都不加**：路径是随机子域名 + 32 字符 token，扫不到也猜不出

Phase 1 的建议是第二条（挡噪声）+ 把 token 当密码管。等做 web 版给亲友用时，
Access 的 email OTP 才是对的工具 —— 那时才有「按人授权」的需求。

## 8. 加速：两篇博客的方法逐条过一遍

参考的两篇：
[为 Cloudflare Tunnel 提速](https://blog.dalenull.work/2024/09/28/speed-up-your-cloudflare-tunnel/)、
[利用优选域名加速 Cloudflare tunnel 在中国的访问速度](https://jqtmviyu.github.io/post/cloudflare-cn-perf/)。

两篇讲的是**两段不同的链路**，不要混为一谈：

```
手机/亲友 ──①──→ Cloudflare 边缘 ──②──→ cloudflared（NAS）
             第二篇优化这一段        第一篇优化这一段
```

### 8.1 让 cloudflared 走 IPv6（第一篇，第 1 招）

`cloudflared` 的 `edge-ip-version` **默认是 `4`**（官方文档明确写的默认值，不是
`auto`），所以哪怕 NAS 有 v6 也不会用。很多家宽的 v6 不限速、不做 NAT、丢包更少，
换过去有实际收益。

```bash
# 先确认 NAS 真有 v6 出口
curl -s -6 --max-time 8 https://api64.ipify.org; echo
```

有输出再配。cloudflared 容器加环境变量：

```yaml
environment:
  TUNNEL_EDGE_IP_VERSION: "6"     # 或 auto，让它按 DNS 结果自己挑
```

改完 `docker restart cloudflared`，用 `cloudflared tunnel info` 看连接是不是落在
`2606:4700:a0::/48` / `a8::/48` 上。

> 开发机上这条**没法验**：`curl -6` 无输出，本机没有 v6 出口。你在 NAS 上跑
> 上面那条 curl 就知道该不该配。

### 8.2 边缘 IP 优选（第一篇，第 2 招）

`cloudflared` 主动连 `region1.v2.argotunnel.com` / `region2.v2.argotunnel.com`
的 **7844** 端口，而这两个域名各只解析出 20 个地址；同一网段里不同地址到你这条
线路的质量能差出一个量级，DNS 给的是哪 20 个纯属运气。

仓库里有个只用标准库的脚本干这件事：

```bash
# 在 NAS 上跑（容器里跑也行：docker compose exec photo-ar-server \
#   python /app/tools/cf_edge_probe.py）
python3 tools/cf_edge_probe.py            # 扫 v4
python3 tools/cf_edge_probe.py --v6       # NAS 有 v6 出口时
```

它整段扫 7844、按握手耗时排序，最后打印两行可直接粘进 `/etc/hosts` 的记录，并且
**会告我们优选到底值不值** —— 它同时报「DNS 给的 20 个里最快多少」和「整段最快
多少」。开发机上今天（2026-07-30）实测：

```
region1  198.41.192.0/24：254 个地址里 254 个在 7844 上应答
    33.3 ms  198.41.192.229        ← 整段最快
    DNS 给的 20 个里最快 34.6 ms
region2  198.41.200.0/24：254 个地址里 254 个都应答，整段最快 33.4 ms
```

**差 1.3ms，等于没有收益** —— 这条线路上 DNS 给的地址已经足够好，优选纯属折腾。
所以这一招的正确用法是：**先在 NAS 上跑一次看那两个数差多少**，差几十毫秒或者
DNS 那批大面积超时才值得动 hosts。国内线路上这两种情况都不罕见。

两个坑，都会让人以为「优选没用」：

- 两行必须来自**不同网段**（一个 `192.x`、一个 `200.x`）。cloudflared 默认建 4 条
  连接并要求分布在两个 region，两行填同一段会让 Tunnel 变成 **Degraded**，比不优选更差
- 测的必须是 **7844**，不是 443。拿 443 的延迟挑出来的地址可能根本不在 7844 上
  服务，表现是启动慢、日志刷 `retrying`

`REGIONS` 里的网段是 2026-07-30 用 `dig` 核过的（region1 → `198.41.192.0/24` +
`2606:4700:a0::/48`，region2 → `198.41.200.0/24` + `2606:4700:a8::/48`）。
Cloudflare 换网段的话脚本会自己发现并提示 —— 它拿域名当前解析结果比对写死的网段。

### 8.3 协议：quic 还是 http2

`protocol` 默认 `auto` = 先试 QUIC（UDP/7844），UDP 不通再退 http2（TCP/7844）。
国内很多线路对 UDP 做限速或干扰，症状是**隧道能连上但抖**（偶发 502、延迟毛刺）。
怀疑就固定成 TCP 试一周：

```yaml
environment:
  TUNNEL_TRANSPORT_PROTOCOL: "http2"
```

反过来，线路对 UDP 友好时 QUIC 的丢包恢复更好。这个只能实测，没有普适答案。
顺带两个默认值：`retries` 默认 5（1/2/4/8/16 秒指数退避，别调大）、`region` 目前
只能填 `us`（把所有连接固定到美国，对我们只会更慢，别填）。

### 8.4 SaaS 回源优选（第二篇）

这一篇优化的是**①那一段**：手机 → Cloudflare 边缘。做法是分线路 DNS —— 境内解析
到「优选域名」（某些在国内速度好的 Cloudflare IP），境外解析到回退源，靠
Cloudflare for SaaS 的 Custom Hostname 让两条路的 `Host` 头都是你的域名，边缘据此
路由到同一个 tunnel。

查证过的成本与前提：

- Cloudflare for SaaS **Free 套餐可用**，含 100 个 custom hostname，超出 $0.10/个
  （Free 档不支持通配符 custom hostname、自定义证书、Non-SNI）
- 需要**两个域名**（一个对外、一个当回退源）
- 需要支持分线路的 DNS：腾讯云 DNSPod **免费版**的基本线路里就有「境内 / 境外」，
  够用
- 「优选域名」是第三方（大厂域名或网友维护的解析）—— 随时失效，且失效时的表现是
  **境内用户直接连不上**，而境外一切正常，很难第一时间归因

值不值得，取决于本项目的流量形状：**隧道上只有 API 小包**（上行 ~50KB、下行 <2KB），
优化的是握手和 RTT，不是吞吐。如果你在外网实测 `/v1/ping` 的延迟能接受
（识别请求客户端 2 秒超时，服务端 P95 约 180ms，留给网络的余量很大），这一整套
就没必要。建议顺序：

1. 先在**常用的外网环境**（4G、公司 WiFi）实测：
   `for i in $(seq 10); do curl -o /dev/null -s -w '%{time_total}\n' \
   -H "Authorization: Bearer $TOKEN" https://arphoto.<你的域名>/v1/ping; done`
2. 中位数 < 400ms → 别折腾，Tailscale 那条路本来就更快
3. 明显更慢或大面积超时 → 再上这一套。它不改服务端，纯 DNS + 面板配置，随时可退

### 8.5 明确不值得的两条

- **Argo Smart Routing**：付费加购（现已并入 Cloudflare 的 Smart Shield），官方文档
  里没有公开的价格与提速数字，且它优化的是 Cloudflare **网络内部**的路由，对
  「国内出口 → 最近边缘」这一段无能为力 —— 而那一段恰好是国内慢的主要原因
- **Cloudflare China Network**：唯一真正解决①那段的官方方案，但要 **Enterprise
  套餐 + 每个顶级域名的 ICP 备案 + 京东云境内节点**，个人 NAS 不在射程内

### 8.6 一句话结论

| 手段 | 值不值得 | 前提 / 判据 |
|---|---|---|
| `TUNNEL_EDGE_IP_VERSION=6` | ✅ 值得先试 | NAS 有 v6 出口（`curl -6 api64.ipify.org` 有输出） |
| 边缘 IP 优选（脚本） | ⚠️ 先量再说 | 脚本报的「整段最快」比「DNS 最快」好几十毫秒才动手；本机实测只差 1.3ms |
| 固定 `http2` | ⚠️ 有症状再改 | 隧道抖、偶发 502 |
| SaaS 回源优选 | ⚠️ 最后手段 | 外网 `/v1/ping` 中位数明显超过 400ms |
| Argo / Smart Shield | ❌ | 优化不到国内出口那一段 |
| China Network | ❌ | Enterprise + ICP |
| **把媒体也挂到隧道上** | ❌❌ | 违反 CDN 条款，赔的是整个账号（§7.1） |

## 9. 排障

| 症状 | 先查什么 | 命令 |
|---|---|---|
| `/v1/ping` 通、识别一直未命中 | 词汇树和库对不上 | `photoar-server -c /config/config.json check`，必要时 `reindex --rebuild-words` |
| 入库全部 422 | 正常（实测 65% 会被拒），不是故障 | 看响应里的分数；换纹理更丰富的照片 |
| 转码特别慢 | 是不是回退软编了 | §4.3 那一行；`docker stats` 看 CPU 是否打满 |
| 隧道 524 | 请求超过 125 秒 | 带视频的入库走 LAN，别走隧道 |
| 隧道 413 | 请求体超 100MB | 上传走 LAN |
| 隧道 502 / 偶发失败 | Tunnel 是否 Degraded | `cloudflared tunnel info nas-adan`，看连接是否分布在两个 region（§8.2） |
| 扫描时整台 NAS 发木 | CPU 配额 | compose 里 `cpus: "3.0"` 是故意留一核给 cloudflared / QTS 的，别调到 4 |
| 容器被 OOM kill | 内存 | 本地实测峰值 1061MB / 3g 上限，还有两倍余量；真 OOM 说明库规模远超一万，调 `mem_limit` |

## 10. 这台机器上量到的基线

本地 Docker 按同样配额（`--cpus=3 --memory=3g`）跑出来的数，用来对照 NAS 上的
实际表现。**绝对延迟不可比**（i9-11900K 单核约为 N5095 的 3.1 倍），但形状可比：

| 项目 | 本机（3 核配额） | 折算 N5095 |
|---|---|---|
| 识别 P95（库 300 张，单线程） | 70.7ms | ≈ 220ms |
| 识别 P95（4 路并发） | 177.0ms | ≈ 550ms |
| 并发吞吐 | 29.5 次/秒 | ≈ 9.5 次/秒 |
| 单张入库（含 20 次自匹配） | 1674ms | ≈ 5.2s（一万张 ≈ 14.5 小时） |
| 一条 30s 视频入库（含转码，`veryfast` 软编） | 27.3s | ≈ 85s |
| 转码产物 | 14.72MiB（上限 16.24） | 同 |
| 峰值内存 | 1061MB / 3g | 同（与 CPU 无关） |
| 误识别 | 0 / 200 | — |

完整报告在 `bench/logs/sim-qnap.json`，重跑：
`python3 bench/sim_qnap.py --photos 300 --queries 200`。
