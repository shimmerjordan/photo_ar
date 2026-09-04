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

改两处：`.env` 里的 `PHOTOAR_ROOTS`，和 `docker-compose.yml` 里的 `volumes`。要批量入库再给 `.env` 加一行 `PHOTOAR_TOKEN=$(openssl rand -hex 24)`。

### PHOTOAR_ROOTS 怎么填

白名单根目录 —— 服务只允许访问这些目录以下的文件。要动的是**成对的三处**，而它们必须互相对得上：

```
volumes 冒号左边    宿主机上的真实路径
volumes 冒号右边    容器内的路径   ┐ 这两个写成**一模一样**
PHOTOAR_ROOTS       容器内的路径   ┘
```

一样是刻意的：入库时填的那个路径，在宿主机、在容器里、在白名单里是同一个字符串，不用在脑子里换算。素材全在 `/share/Study/media_bed/photo-ar` 下面时（推荐分四个子目录）：

```yaml
environment:
  PHOTOAR_ROOTS: photos=/share/Study/media_bed/photo-ar/photos,videos=/share/Study/media_bed/photo-ar/videos
  PHOTOAR_UPLOAD_DIR: /share/Study/media_bed/photo-ar/inbox
volumes:
  - /share/Study/media_bed/photo-ar/data:/data                                        # 库，可写
  - /share/Study/media_bed/photo-ar/photos:/share/Study/media_bed/photo-ar/photos:ro
  - /share/Study/media_bed/photo-ar/videos:/share/Study/media_bed/photo-ar/videos:ro
  - /share/Study/media_bed/photo-ar/inbox:/share/Study/media_bed/photo-ar/inbox        # 上传落地，可写
```

`/data` 是唯一一条左右不一样的：镜像里写死了 `PHOTOAR_DATA=/data`，让它保持默认最省事。

`照片=` 那半截是**显示标签**，不是变量名 —— 变量名是 `PHOTOAR_ROOTS` 本身。标签只用来在管理台的目录浏览器里区分哪个根是哪个，不参与任何路径解析。三种写法都行：`照片=/share/Photo,视频=/share/Video`（界面上显示中文）、`photos=/share/Photo,videos=/share/Video`（ASCII 标签）、`/share/Photo,/share/Video`（不给标签，自动取目录名）。中文标签能用（compose 里 `LANG: C.UTF-8` 那行就是为它设的），换来的只是界面上几个字，纯偏好。两个根撞名（`/a/Photo` 与 `/b/Photo` 都取 `Photo`）会**直接报错**而不是后者覆盖 —— 覆盖的后果是其中一个目录整体访问不到，而界面上只是少了一项。

**三条约束，每条都有一个不响的失败方式**：

1. **`/data` 别落在 `PHOTOAR_ROOTS` 之内。** 落进去的话，服务自己的 SQLite 和索引会出现在管理台的目录浏览器里。不致命（列目录是只读的），但没有理由把它们摆在「选一张照片」的界面上 —— 所以上面那个例子把 ROOTS 指到 `photos/` 和 `videos/` 两个子目录，而不是整个 `photo-ar/`。
2. **`PHOTOAR_UPLOAD_DIR` 必须在 `PHOTOAR_ROOTS` 之内，且那条挂载不能是 `:ro`。** 三种失败长得完全不一样，而只有第一种说了实话：不设 → 503 `upload_disabled`，明说「上传功能关闭」；设了但落在 ROOTS 之外 → **启动时不报错**，每次上传 403 `path_denied`（落地路径要再过一遍白名单，不信任配置里的前缀）；挂载写成 `:ro` → 前两关都过，写的时候才失败。不用上传功能就干脆留空，那是唯一会明说的那一种。
3. **宿主机上的目录要先 `mkdir -p` 出来。** bind mount 的源不存在时，dockerd 会**以 root 身份建一个空目录**，一声不响 —— 然后服务真的去索引那个空目录，表现是「入库一张都找不到」。

**算成**：`ls .env docker-compose.yml` 都在，且 `PHOTOAR_ROOTS` 与 `volumes` 冒号右边逐字相同。

## 3. 起服务

```bash
docker compose pull        # 从 GHCR 拉现成镜像，不在 NAS 上构建
docker compose up -d
docker compose logs -f photo-ar-server
```

**算成**：日志里 `[photoar] 监听 0.0.0.0:8964｜照片 0 张｜后端 orb`，约 20 秒后 `docker compose ps` 的 health 变 `healthy`。

登录 `http://<NAS>:8964/admin`，账号 `admin`、初始口令 `admin`，**第一次登录会强制改口令**。
（想跳过强制改密，先在 `.env` 里填 `PHOTOAR_ADMIN_PASSWORD`。）

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

宾客走 **Cloudflare Tunnel，全部流量含视频**（他们不装任何东西）。Tailscale 只给自己：管理台、入库大文件、在外面看自己的照片。风险与取舍见 [deploy-details.md](deploy-details.md#隧道的三条硬限制)。

### 6a. Tailscale（可选，给自己用）

NAS：App Center 装 Tailscale（没有就去 [pkgs.tailscale.com](https://pkgs.tailscale.com/stable/#qnap) 下 x86-64 的 `.qpkg`），登录你的 tailnet。手机装 App 登同一个。

```bash
tailscale ip -4        # 100.x.y.z
```

**算成**：手机**关 WiFi 走 4G**、开着 Tailscale，打开 `http://<100.x.y.z>:8964/v1/ping` 看到 **401**（不是连不上）。

不用开子网路由，也**不要开 Funnel**。

宾客用不到这一步。

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

所以发给宾客的就是第 6b 那条隧道的地址 `https://arphoto.<你的域名>/` —— Cloudflare 的证书是现成的，不用自己配。

> 想让自己在外面不经隧道也能开相机（Tailscale 直连），要给 MagicDNS 主机名签一张证书：
> Tailscale 后台 DNS 页打开 **HTTPS Certificates**，`tailscale cert <机器>.<tailnet>.ts.net`，
> 把 `.crt/.key` 放一个目录并在 `.env` 里填 `WEBFRONT_CERT_DIR` / `WEBFRONT_TLS_CERT` / `WEBFRONT_TLS_KEY`（后两个是容器内路径），
> `up -d` 后启动日志第一行变成 `https://0.0.0.0:8964`。公共 CA 不给 `100.x` 的 IP 签，所以必须用主机名。
>
> 婚礼那种「几十个人一次性」的场合，现场 Wi-Fi + 自签证书更省事，见 [faq.md](faq.md#局域网里自测没有隧道也没有真证书)。

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
| 5 | 用 admin / admin 登 `/admin` | 被要求改口令，改完进入管理台 | 3 |
| 6 | 不带凭证 ping | `401` | 3 |
| 7 | 问服务它选了哪个编码器 | `h264_vaapi` | 4 |
| 8 | 手工入一张（纹理丰富的） | `201` + `photoId` | 5 |
| 9 | 批量入库（先 `--limit 5 --dry-run`） | 配对没错，再放量 | 5 |
| 10 | 训词表并重启 | `vocabTrained` 变 `true` | 5 |
| 11 | 带 cookie 拉一次 `/api/lib` | `200` + 几十 KB | 5 |
| 12 | 隧道 ingress 生效 | 外网 `https://arphoto.<域名>/v1/ping` 回 401 | 6b |
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
