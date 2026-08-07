# 部署

目标机器：QNAP TS-464C2（N5095，x86-64）+ Container Station。其它 x86-64 Docker 主机步骤相同。

**每一步都带「看到什么算成」，看不到就别往下走** —— 后面的步骤看不出前面漏了什么。
NAS 那侧（1～6）约半小时，手机那侧（7）十分钟。

| 想找 | 去 |
|---|---|
| 怎么用管理台、宾客怎么用 | [usage.md](usage.md) |
| 出问题了 | [faq.md](faq.md) |
| 某个数字/限制怎么来的 | [deploy-details.md](deploy-details.md) |
| 例行维护命令 | [../deploy/README.md](../deploy/README.md) |

---

## 0. 准备

**只需要一样**：照片和视频在 NAS 上的路径，例 `/share/Photo`、`/share/Video`、CloudDrive2 的 `/share/CloudDrive`。

用 `/share/Photo` 这一层，**不要用 `ls -l` 出来的 `/share/CACHEDEV1_DATA/Photo`** —— 后者是符号链接的真身，两者混用会 403。

词表、token、`config.json` 都是可选的（词表见第 5 步，其余见 [.env.example](../.env.example)）。

## 1. SSH，找到 docker

```bash
ssh admin@<NAS 内网 IP>
export PATH=$(dirname $(ls /share/*/.qpkg/container-station/bin/docker)):$PATH
docker compose version
```

**算成**：打出 `Docker Compose version v2.x`。

那行 `export` 每次 SSH 都要重来，写进 `~/.profile` 省事（QTS 升级会重置）。QTS 控制台没开 SSH 的话：网络与文件服务 → Telnet / SSH → 勾「允许 SSH 连接」。

## 2. 放文件

```bash
mkdir -p /share/Container/photo-ar/{data,tools} /share/Photo/_arphoto_inbox
cd /share/Container/photo-ar

R=https://raw.githubusercontent.com/shimmerjordan/photo_ar/main
curl -fsSLO $R/docker-compose.yml
curl -fsSL $R/.env.example -o .env
curl -fsSL $R/tools/batch_ingest.py -o tools/batch_ingest.py    # 第 5 步用
```

改两处：

**`.env`** —— 只有 `PHOTOAR_ROOTS` 必须看一眼，写的是**容器内**路径：

```
PHOTOAR_ROOTS=照片=/share/Photo,视频=/share/Video,网盘=/share/CloudDrive
```

要批量入库再加 `PHOTOAR_TOKEN=$(openssl rand -hex 24)`。

**`docker-compose.yml`** —— 把 `volumes` 改成自己的共享文件夹，**冒号两边写成一样**：

```yaml
- /share/Photo:/share/Photo:ro        # 左宿主机，右容器内
```

一样是故意的：入库时填的路径在三个地方是同一个字符串，不用换算。改完让 `PHOTOAR_ROOTS` 与它们对得上。

> 三条**不报错**的失败方式，见 [docker-compose.yml](../docker-compose.yml) 顶部：`/data` 别落在 `PHOTOAR_ROOTS` 之内；上传目录必须在 `PHOTOAR_ROOTS` 之内且挂载可写；宿主机目录要先 `mkdir -p`（不建的话 dockerd 会以 root 建个空目录，然后服务真去索引它）。

**算成**：`ls .env docker-compose.yml` 都在。

## 3. 起服务

```bash
docker compose pull        # 从 GHCR 拉现成镜像，不在 NAS 上构建
docker compose up -d
docker compose logs -f photo-ar-server
```

**算成**：日志里 `[photoar] 监听 0.0.0.0:8964｜照片 0 张｜后端 orb`，约 20 秒后 `docker compose ps` 的 health 变 `healthy`。

日志里有一行**只出现一次**，现在就抄走：

```
[photoar] 已创建引导管理员 'admin'，随机口令：xxxxxxxxxxxx
```

登录 `http://<NAS>:8964/admin`，**进去第一件事就是改掉**。（想要自己记得住的口令，先在 `.env` 里填 `PHOTOAR_ADMIN_PASSWORD`。）

日志里那条 `⚠️ 没有词表` 是**正常**的，第 5 步末尾会训。

然后确认鉴权真的在：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8964/v1/ping   # 要 401
```

**没设 `PHOTOAR_TOKEN` 时也必须是 401** —— 空 token 是让运维凭证那条路整体禁用，不是"谁都能进"。设了的话带上它再来一次该回 `{"ok": true, ...}`，响应里几个字段值得看一眼：`backendDegraded`（true = XFeat 没取到、回退了 ORB）、`vocabTrained`、`photos`。

> `pull` 报 `denied`：GHCR 上的包默认 private，见 [faq.md](faq.md#pull-报-denied--unauthorized)。

## 4. 确认核显硬编真的生效

`video_encoder` 默认 `auto`，探测不到核显会**静默回退软编** —— 而软编在这台机器上慢一个量级（30 秒视频约 56 秒 vs 几秒），慢到会撞上隧道的 125 秒超时。静默是故意的，所以必须显式验一次：

```bash
docker compose exec photo-ar-server python -c \
  "from photoar import transcode as T; print(T.resolve_encoder('auto'))"
```

**算成**：打出 `h264_vaapi`。它是真编一帧，不是查 `ffmpeg -encoders`（列得出 ≠ 跑得动）。

确认可用后把 `.env` 的 `PHOTOAR_VIDEO_ENCODER` 改成 `h264_vaapi` 再 `up -d` —— 这样哪天核显不可用会**直接报错**而不是悄悄软编。

打出 `libx264` 的排查步骤见 [faq.md](faq.md#硬编回退成了-libx264)。

## 5. 入库

**先手工入一张**，挑纹理丰富的（人多、建筑、树叶、图案衣服）：

```bash
T=$(grep PHOTOAR_TOKEN .env | cut -d= -f2)
curl -sS -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"refPath":"/share/Photo/2019/IMG_0421.jpg",
       "videoPath":"/share/Video/2019/IMG_0421.mov",
       "title":"外婆家院子"}' \
  http://127.0.0.1:8964/v1/photo
```

**算成**：`201` 和一个 `photoId`。被拒的话返回里会写明原因，逐条对照见 [faq.md](faq.md#入库被拒了)。

**再批量入**：

```bash
# 先看配对对不对：主文件名相同的照片和视频算一对
python3 tools/batch_ingest.py --base http://127.0.0.1:8964 \
    --photos /share/Photo/那批 --videos /share/Video/那批 \
    --recursive --title-from-name --limit 5 --dry-run

# 对了就去掉 --limit --dry-run 正式跑
```

**必须在 LAN 上跑，别走隧道**（单张约 5 秒，带视频再加几十秒，隧道 125 秒就断）。一万张约 14.5 小时，挂 `screen` 过夜或分批。断了直接再跑：进度记在 `batch-ingest-state.json`，已入库的会跳过。

照片视频不同名就给一份 TSV：`--manifest pairs.tsv`（`照片 <TAB> 视频 <TAB> 宽度mm <TAB> 标题`）。

**确认浏览器拿得到识别库** —— 识别是在用户浏览器里做的，前提是它能下到那份包：

```bash
C=$(curl -sS -i -X POST -H 'Content-Type: application/json' \
      -d '{"name":"admin","password":"<你的口令>"}' \
      http://127.0.0.1:8964/v1/auth/login \
    | grep -i '^set-cookie:' | sed 's/^[Ss]et-[Cc]ookie: *//' | cut -d';' -f1)

curl -sS -H "Cookie: $C" -o /dev/null -w '%{http_code}  %{size_download} bytes\n' \
  http://127.0.0.1:8964/api/lib
```

**算成**：`200` 加几十 KB（45 张约 50KB）。这个包是**按调用者的授权集**算的，`nPhotos: 0` 说明授权没做。

**最后训词表**（不训也能用，识别结果一样正确，只是每次全量扫描：45 张的库实测 124ms → 64ms，差距随库大小线性拉开）：

```bash
docker compose exec photo-ar-server photoar-server build-vocab
docker compose restart photo-ar-server        # 词表是启动时加载的
```

**算成**：打出 `词表训好了：/data/models/vocab.npz`，重启后 `/v1/ping` 的 `vocabTrained` 变 `true`。

> **先入库，再决定打印哪几张。** 别先印好送出去了才发现认不出来。

## 6. 外网通道

分工是硬性的：**视频走 Tailscale，Cloudflare 只跑 API 小包**。其中一条是账号级风险，见 [deploy-details.md 的三条硬限制](deploy-details.md#隧道的三条硬限制)。

### 6a. Tailscale

NAS：App Center 装 Tailscale（没有就去 [pkgs.tailscale.com](https://pkgs.tailscale.com/stable/#qnap) 下 x86-64 的 `.qpkg`），登录你的 tailnet。手机装 App 登同一个。

```bash
tailscale ip -4        # 100.x.y.z
```

**算成**：手机**关 WiFi 走 4G**、开着 Tailscale，打开 `http://<100.x.y.z>:8964/v1/ping` 看到 **401**（不是连不上）。

不用开子网路由，也**不要开 Funnel**。

### 6b. Cloudflare Tunnel

已经有 tunnel 和通配符 DNS 的话，**不新建 tunnel、不改 DNS**，只加一条 ingress，插在 404 兜底那条**之前**：

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

**算成**：外网 `curl -sS -H "Authorization: Bearer $T" https://arphoto.<你的域名>/v1/ping` 回 `{"ok": true...}`，且 `https://arphoto.<你的域名>/` 打开就是网页版。

顺手看隧道健康：`docker exec cloudflared cloudflared tunnel info <tunnel 名>` 要有 4 条连接、落在**两个不同 region**（只落一个会 Degraded，表现是偶发 502）。

> 用完把这条 ingress 摘掉 —— 理由见 details 的三条硬限制。

### 6c. 让引擎停在 Cloudflare 边缘

网页版有 2.6MB 静态资源是所有宾客共享、内容永不变的。不配的话每个宾客都从 NAS 拉一遍，而且最大那块拉不进边缘缓存 —— Cloudflare 的默认缓存按扩展名，名单里**没有 `.wasm`**。差别在宾客第一屏 10 秒以上。

Caching → Cache Rules 加一条，**表达式必须按路径限定**：

```
表达式：(http.host eq "arphoto.你的域名") and
        (starts_with(http.request.uri.path, "/vendor/") or
         starts_with(http.request.uri.path, "/art/"))
设置：Cache eligibility → Eligible for cache
     Edge TTL         → Use cache-control header if present
     Cache key → Query string → Include all
```

> ⛔ **绝对不要用 "Cache Everything" 或不限路径的规则。** `/v1/*` 与 `/api/*` 是**按人授权**的。缓存到边缘就是把一个人的视频发给另一个人 —— 而且没有任何症状，你看到的是"能播"。

**算成**：连打两次，第二次 `cf-cache-status` 是 `HIT`：

```bash
V=$(curl -s https://arphoto.<你的域名>/vendor/opencv.js | grep -o 'opencv\.wasm?v=[0-9a-f]*')
for i in 1 2; do
  curl -sI -H 'Accept-Encoding: br' "https://arphoto.<你的域名>/vendor/$V" \
    | grep -iE 'cf-cache-status|content-length'
done
```

`Content-Length` 该是 250 万左右而不是 1195 万（说明预压的 brotli 透传了）。拿到 `DYNAMIC` / `BYPASS` 见 [faq.md](faq.md#边缘缓存没命中)。

## 7. 发给宾客

没有 App 要装，把地址发出去就行。一条硬性前提：

> ⚠️ **地址必须是 `https://`。** 相机（`getUserMedia`）只在安全上下文里存在：`https://` 任意域名都算，`http://localhost` 算（只有本机），**`http://192.168.x.x` 和 `http://100.x.x.x` 都不算**。

所以对外地址就是第 6b 那条隧道的地址。**宾客看视频的正路是 Tailscale**（Cloudflare 那条只跑 API）：让他们登进你的 tailnet，打开 `https://<机器>.<tailnet>.ts.net:8964/`。要拿真证书先去 Tailscale 后台 DNS 页打开 **HTTPS Certificates**（默认关），然后：

```bash
tailscale cert <机器>.<tailnet>.ts.net
```

把 `.crt` / `.key` 放一个目录，`.env` 里指过去（左宿主目录，右两个是**容器内**路径）：

```bash
WEBFRONT_CERT_DIR=/share/Container/photoar-certs
WEBFRONT_TLS_CERT=/certs/<机器名>.<tailnet>.ts.net.crt
WEBFRONT_TLS_KEY=/certs/<机器名>.<tailnet>.ts.net.key
```

`up -d` 之后启动日志第一行变成 `https://0.0.0.0:8964`。

> 公共 CA 不会给 `100.64.0.0/10` 的 IP 签证书，所以必须用 MagicDNS 主机名，不能用 `https://100.x.y.z`。
>
> 婚礼那种"几十个人一次性"的场合，更省事的是现场 Wi-Fi + 自签证书，见 [faq.md](faq.md#局域网里自测没有隧道也没有真证书)。

**算成**：手机 4G 打开那个地址 → 登录蒙版 → 输名字（宾客口令留空）→ 一整页一颗「扫一扫」→ 给相机权限 → 举起**打印出来的**照片，离半米左右 → 视频贴在照片上播起来。

怎么建账号、怎么授权、宾客和管理员看到什么不一样，见 [usage.md](usage.md)。

---

## 跑通清单

| # | 做什么 | 看到什么算成 | 步骤 |
|---|---|---|---|
| 1 | SSH，找到 docker | `docker compose version` 打出 v2.x | 1 |
| 2 | 建 `_arphoto_inbox` | 目录在 | 2 |
| 3 | 拉 compose 与 `.env`，填 `PHOTOAR_ROOTS` | 文件都在，冒号两边一样 | 2 |
| 4 | `docker compose pull && up -d` | 日志 `监听 0.0.0.0:8964`，20s 后 `healthy` | 3 |
| 5 | **抄走日志里那行随机管理员口令** | 登进 `/admin` 并立刻改掉 | 3 |
| 6 | 不带凭证 ping | `401` | 3 |
| 7 | 问服务它选了哪个编码器 | `h264_vaapi` | 4 |
| 8 | 手工入一张（纹理丰富的） | `201` + `photoId` | 5 |
| 9 | 批量入库（先 `--limit 5 --dry-run`） | 配对没错，再放量 | 5 |
| 10 | 训词表并重启 | `vocabTrained` 变 `true` | 5 |
| 11 | 带 cookie 拉一次 `/api/lib` | `200` + 几十 KB | 5 |
| 12 | 配上 https | 启动日志第一行 `https://0.0.0.0:8964` | 7 |
| 13 | 手机打开那个地址 | 登录蒙版出来 | 7 |
| 14 | 管理员登进去看「照片」 | 第 8 步那张的缩略图 | 7 |
| 15 | 「扫一扫」举起照片 | **视频贴在照片上播起来** | 7 |
| 16 | 装 Tailscale（NAS + 手机） | 4G 下 ping 回 401 | 6a |
| 17 | 加一条 cloudflared ingress | 外网 curl 到 `{"ok": true}` | 6b |
| 18 | 建宾客账号、授权几张 | 用它登进去只看到一颗「扫一扫」 | 7 |
| 19 | 关 WiFi 走 4G 再扫一次 | 还能认出来、还能播 | 7 |
| 20 | 备份 `data/` | 有一份压缩包 | 下面 |

**第 15 步是整条链路第一次真正闭合的地方** —— 在它之前的绿灯都只说明「零件没坏」。

## 之后

**升级**（先 `cd` 到 compose 所在目录，忘了就 `docker inspect photo-ar-server --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'`）：

```bash
docker compose pull && docker compose up -d
docker image prune -f --filter label=org.opencontainers.image.source=https://github.com/shimmerjordan/photo_ar
```

第二行不能省，那个 `--filter` 也不能省 —— 理由见 [deploy-details.md](deploy-details.md#升级备份恢复)。

**备份**：值钱的只有 `data/`，停机再拷：

```bash
docker compose stop && sudo tar czf /share/Backup/photo-ar-$(date +%F).tar.gz data/ && docker compose start
```

**在开发机上跑**（给改代码的人）：见 [deploy-details.md](deploy-details.md#在开发机上跑)。
