# 部署：照着做就能跑起来

目标机器是 QNAP TS-464C2（Celeron N5095，x86-64）+ Container Station，其它 x86-64
的 Docker 主机步骤一样。

**每一步都有「看到什么算成」，看不到就别往下走** —— 后面的步骤看不出前面漏了什么。
NAS 那侧（第 1～6 步）大约半小时；手机那侧（第 7 步）十分钟。批量入库和打印照片是
另外的时间，见第 5 步。

想知道**为什么**这么做（实测数据、限制的出处、排障），都在
[deploy-details.md](deploy-details.md)。这份只讲怎么做。

---

## 先准备四样东西

| 东西 | 怎么来 |
|---|---|
| 一个随机 token | `openssl rand -hex 24`，记下来，手机上要填同一个 |
| `arcoreimg` | ARCore SDK for Android 里的 `tools/arcoreimg/linux/arcoreimg`。闭源二进制，不可再分发，所以镜像里没有，要自己拷 |
| `vocab.npz`（词汇树） | 服务端不训练，必须先在一台有 Python 的机器上训好（下面一条命令） |
| 照片、视频在 NAS 上的路径 | 例：`/share/Photo`、`/share/Video`、CloudDrive2 的 `/share/CloudDrive` |

训词汇树（在开发机上，几千张风格接近的照片就够，不必是要入库的那批）：

```bash
git clone https://github.com/shimmerjordan/photo_ar && cd photo_ar
pip install -e .
photoar build --photos <一批照片的目录> --out /tmp/corpus     # 产出 /tmp/corpus/vocab.npz
```

> 照片路径请用 `/share/Photo` 这一层，**不要用 `ls -l` 出来的
> `/share/CACHEDEV1_DATA/Photo`** —— 前者是符号链接，两者混用会 403。

---

## 1. 开 SSH，找到 docker

QTS 控制台 →「网络与文件服务」→「Telnet / SSH」→ 勾「允许 SSH 连接」，然后
`ssh admin@<NAS 内网 IP>`。

`docker` **不在 PATH 里是常态**：

```bash
export PATH=$(dirname $(ls /share/*/.qpkg/container-station/bin/docker)):$PATH
docker compose version
```

**看到什么算成**：打出 `Docker Compose version v2.x`。

这行 `export` 每次 SSH 都要重来，写进 `~/.profile` 省事（QTS 升级会重置）。

---

## 2. 放文件

不需要把源码传上去 —— 镜像在 GHCR 上，只要四个文件加两个二进制：

```bash
mkdir -p /share/Container/photo-ar/{data,deploy,tools}
cd /share/Container/photo-ar

R=https://raw.githubusercontent.com/shimmerjordan/photo_ar/main
curl -fsSLO $R/docker-compose.yml
curl -fsSL $R/.env.example -o .env
curl -fsSL $R/deploy/config.example.json -o deploy/config.json
curl -fsSL $R/tools/batch_ingest.py -o tools/batch_ingest.py    # 第 5 步用
```

然后从开发机把那两个不在仓库里的文件传上来：

```bash
scp tools/arcoreimg  admin@<NAS>:/share/Container/photo-ar/tools/
scp /tmp/corpus/vocab.npz admin@<NAS>:/share/Container/photo-ar/data/
ssh admin@<NAS> chmod +x /share/Container/photo-ar/tools/arcoreimg
```

改两处配置：

**`.env`** —— 填 token（这个文件不要提交、不要外传）：

```
PHOTOAR_TOKEN=刚才 openssl 生成的那串
```

**`deploy/config.json`** —— 只有 `roots` 必须改，写的是**容器内**路径：

```json
"roots": { "照片": "/share/Photo", "视频": "/share/Video", "网盘": "/share/CloudDrive" }
```

**`docker-compose.yml`** —— 把 `volumes` 里那几条挂载改成你自己的共享文件夹，
**冒号两边写成一样**：

```yaml
- /share/Photo:/share/Photo:ro        # 左边宿主机，右边容器内
```

一样是故意的：入库时填的路径在宿主机、容器里、白名单里是同一个字符串，不用换算。

**看到什么算成**：`ls data/vocab.npz tools/arcoreimg .env deploy/config.json` 四个都在。

最后长这样：

```
/share/Container/photo-ar/
├── docker-compose.yml
├── .env                  ← token
├── data/vocab.npz        ← 持久卷，之后索引、缩略图、转码产物都落这里
├── deploy/config.json
└── tools/arcoreimg
```

---

## 3. 起服务

```bash
docker compose pull        # 从 GHCR 拉现成镜像，不在 NAS 上构建
docker compose up -d
docker compose logs -f photo-ar-server
```

**看到什么算成**：日志里 `listening on 0.0.0.0:8964`。然后

```bash
curl -sS -H "Authorization: Bearer $(grep PHOTOAR_TOKEN .env | cut -d= -f2)" \
  http://127.0.0.1:8964/v1/ping
# {"ok": true, "version": "phase1", "serverTime": 1753...}

curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8964/v1/ping   # 401
```

不带 token 必须是 401 —— 这是唯一一层鉴权，顺手确认它真的在。

> `pull` 报 `denied` / `unauthorized` 的话，是 **GHCR 上的包默认是 private**。
> 到 GitHub 的仓库 → 右侧 Packages → `photo-ar-server` → Package settings →
> Change visibility 改成 public（只是镜像公开，仓库不受影响）。不想公开就在 NAS 上
> `docker login ghcr.io -u <你的用户名>`，密码用一个有 `read:packages` 的 PAT。

> 想自己改代码再部署，就 clone 仓库后 `docker compose build && docker compose up -d`
> （N5095 上首次构建几分钟）。也可以不用 SSH，在 Container Station「应用程序」里贴
> compose —— 代价见 [deploy-details.md](deploy-details.md#不用-ssh-的那条路)。

---

## 4. 确认核显硬编真的生效

配置里 `video_encoder` 默认 `auto`：探测不到核显会**静默回退软编**，而软编在这台
机器上慢一个量级（一条 30 秒视频约 56 秒 vs 几秒），慢到会撞上隧道的 125 秒超时。
静默是故意的，所以必须显式验一次：

```bash
docker compose exec photo-ar-server python -c \
  "from photoar import transcode as T; print(T.resolve_encoder('auto'))"
```

**看到什么算成**：打出 `h264_vaapi`。它不是查 `ffmpeg -encoders`（列得出 ≠ 跑得动），
是真编一帧，所以这一行就是结论。

打出 `libx264` 说明回退了，按这个顺序查：

```bash
ls -l /dev/dri/                                    # 要有 renderD128；没有就是 BIOS 关了核显
docker compose exec photo-ar-server vainfo | grep EncSlice   # 要有 VAEntrypointEncSlice
stat -c '%g' /dev/dri/renderD128                   # Permission denied 时把这个 GID 填进
                                                   # compose 里已备好的 group_add
```

确认可用之后，把 `deploy/config.json` 的 `video_encoder` 改成 `"h264_vaapi"` 再重启。
这样哪天核显不可用（QTS 升级、设备号变了）会**直接报错**，而不是悄悄软编 —— 后者
唯一的发现方式是掐表。

---

## 5. 入库

### 先手工入一张

挑一张**纹理丰富**的（人多、建筑、树叶、图案衣服）。大片天空、纯色墙、逆光剪影
过不了质量闸门。

```bash
T=$(grep PHOTOAR_TOKEN .env | cut -d= -f2)
curl -sS -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"refPath":"/share/Photo/2019/IMG_0421.jpg",
       "videoPath":"/share/Video/2019/IMG_0421.mov",
       "printWidthMm":152,
       "title":"外婆家院子"}' \
  http://127.0.0.1:8964/v1/photo
```

**看到什么算成**：`201` 和一个 `photoId`。

`printWidthMm` 是照片**打印出来、画面本身**的实际宽度（白边不算），**要拿尺量**，
AR 里视频贴不贴得住全靠它。6 寸≈152 / 5 寸≈127 / 4 寸≈102，但各店有差；竖着放的
6 寸填的是 102。

被拒的话看返回的原因，四种都是明确的：`quality_too_low`（分数不够，**真实照片约
65% 会被拒，这是正常的**）、`already_ingested`、`near_duplicate`、`path_denied`。
详见 [deploy-details.md](deploy-details.md#入库为什么会被拒)。

### 再批量入

```bash
# 先看配对对不对：主文件名相同的照片和视频算一对
python3 tools/batch_ingest.py --base http://127.0.0.1:8964 \
    --photos /share/Photo/送出去的那批 --videos /share/Video/送出去的那批 \
    --recursive --width-mm 152 --title-from-name --limit 5 --dry-run

# 对了就去掉 --limit --dry-run 正式跑
```

**必须在 LAN 上跑，别走隧道**（单张约 5 秒，带视频再加几十秒，隧道 125 秒就断）。
**一万张约 14.5 小时**，挂 `screen` 过夜或者分批。断了直接再跑一次：进度记在
`batch-ingest-state.json` 里，已入库的和被确定性拒绝的会跳过。

它**故意不并发** —— 原因见 details，那是正确性问题，不是快慢。

照片视频不同名、或每张打印尺寸不同，就给一份 TSV 清单：`--manifest pairs.tsv`
（`照片 <TAB> 视频 <TAB> 宽度mm <TAB> 标题`）。

跑完对一下账：

```bash
docker compose exec photo-ar-server photoar-server -c /config/config.json check
curl -sS -H "Authorization: Bearer $T" http://127.0.0.1:8964/v1/photos \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["total"], "条")'
```

> **先入库，再决定打印哪几张。** 别先印好送出去了才发现认不出来。

---

## 6. 两条外网通道

在外面要能用，需要两条路，分工是硬性的：**视频只走 Tailscale，Cloudflare 只跑
API 小包**（原因见 details 的「隧道的三条硬限制」，其中一条是账号级风险，请务必看
一眼）。

### 6a. Tailscale（外网看视频靠它）

NAS：App Center 搜 Tailscale 装上（商店没有就去
[pkgs.tailscale.com](https://pkgs.tailscale.com/stable/#qnap) 下 x86-64 的 `.qpkg`
手动安装），打开、登录到你的 tailnet。手机：装 Tailscale App，登同一个 tailnet。

```bash
tailscale ip -4        # 100.x.y.z，这个地址等下要填进手机
```

**看到什么算成**：手机**关 WiFi 走 4G**、开着 Tailscale，浏览器打开
`http://<100.x.y.z>:8964/v1/ping` 看到 **401**（而不是连不上）。

不用开子网路由，也**不要开 Funnel**。

### 6b. Cloudflare Tunnel（只跑 API）

已经有 tunnel 和通配符 DNS 的话，**不新建 tunnel、不改 DNS**，只加一条 ingress。
编辑 NAS 上 cloudflared 的 `config.yml`（通常在
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

**看到什么算成**：从任意外网环境（手机 4G）

```bash
curl -sS -H "Authorization: Bearer $T" https://arphoto.<你的域名>/v1/ping
```

回 `{"ok": true...}`。顺手看一眼隧道健康：
`docker exec cloudflared cloudflared tunnel info <tunnel 名>` 要有 4 条连接、落在
**两个不同 region**（只落一个会 `Degraded`，表现是偶发 502）。

---

## 7. 手机 App

### 出 APK

需要 JDK 17 + Android SDK（platform 35）：

```bash
cd android && ./gradlew :app:assembleRelease
# → app/build/outputs/apk/release/app-release.apk（约 9.1MB）
```

`adb install -r app-release.apk`，或者把 apk 拷到 NAS 共享文件夹、手机下载后安装
（要允许「未知来源」）。手机需要 Android 7.0+；ARCore（Google Play Services for
AR）建议装上，**不装也能用**，只是退化成认出后全屏播、视频不贴在照片上。

> **release 是用 debug key 签的**，所以**换机器出的包不能覆盖安装**，只能先卸载，
> 而卸载会清掉设置里的通道、令牌和离线缓存。要长期给几台手机出包，就备份好
> `~/.android/debug.keystore` 始终用同一份。

### 填通道

App → 底栏「设置」：

- **访问令牌**：`.env` 里那串，四条通道共用一个
- **通道**：默认已经有四张卡，**开关都已经设对了，你只要填地址**（要带端口 8964）

| 卡片 | 填什么 |
|---|---|
| LAN | `http://<NAS 内网 IP>:8964` |
| Tailscale | `http://<100.x.y.z>:8964` |
| Tunnel | `https://arphoto.<你的域名>`（「是隧道」已勾好，**别取消**；「适合 media」**别勾**） |
| DDNS | 没有就不管，默认停用 |

地址留空的卡不参与探活，所以**只填 LAN 就能先用起来**，剩下的等第 6 步做完再回来填。

点「保存并探活」。**看到什么算成**：每张卡下面显示 `通 · 23ms`。失败会显示原因原文：
`401` 是令牌错了、`404` 是这个地址上没有服务、`不通` 是网络到不了（LAN 卡在外网就
是这个，正常）。

再看「现在走的是」：在家 api 和 media 都该是 LAN；4G 下 api 落 Tunnel、media 落
Tailscale。

### 第一次扫

底栏「照片」→ 右下角「扫一扫」→ 给相机权限 → 举起**打印出来的**那张照片，离半米
左右，光别太暗。

**看到什么算成**：视频贴在照片上播起来。

手机上也能入库（「照片库」右上角「＋」），挑的是 NAS 上的路径、请求体只有几百字节，
适合零散补几张。出门前建议进「设置 → 管理离线缓存」同步一次，之后没网也能扫。

---

## 跑通清单

| # | 做什么 | 看到什么算成 | 步骤 |
|---|---|---|---|
| 1 | 开 SSH，找到 docker | `docker compose version` 打出 v2.x | 1 |
| 2 | 训好 `vocab.npz`、拷来 `arcoreimg` | 两个文件都在 | 准备 |
| 3 | 拉配置、填 `.env` 和 `roots` | 四个文件都在，冒号两边一样 | 2 |
| 4 | `docker compose pull && up -d` | 日志 `listening on 0.0.0.0:8964` | 3 |
| 5 | 带 / 不带 token 各 ping 一次 | `{"ok": true...}` 和 `401` | 3 |
| 6 | 问服务它选了哪个编码器 | `h264_vaapi` | 4 |
| 7 | **手工入一张**（挑纹理丰富的） | `201` + `photoId` | 5 |
| 8 | 出 APK 装到手机 | 能打开，底栏三个 tab | 7 |
| 9 | 填令牌 + LAN 地址，保存并探活 | LAN 显示 `通 · xx ms` | 7 |
| 10 | 「照片库」里能看到第 7 步那张 | 缩略图和标题 | 7 |
| 11 | **打印那张照片，量画面宽度** | 与入库时填的一致 | 5 |
| 12 | 「扫一扫」举起照片 | **视频贴在照片上播起来** | 7 |
| 13 | 批量入库（先 `--limit 5 --dry-run`） | 配对没错，再放量 | 5 |
| 14 | 装 Tailscale（NAS + 手机） | 4G 下 ping 回 401 | 6a |
| 15 | 加一条 cloudflared ingress | 外网 curl 到 `{"ok": true}` | 6b |
| 16 | 手机上补填 Tailscale / Tunnel 两张卡 | 4G 下 api 落 Tunnel、media 落 Tailscale | 7 |
| 17 | 关 WiFi 走 4G 再扫一次 | 还能认出来、还能播 | 7 |
| 18 | 备份 `data/` | 有一份压缩包 | 下面 |

**第 12 步是整条链路第一次真正闭合的地方** —— 在它之前的绿灯都只说明「零件没坏」。

---

## 之后

- **升级**：`docker compose pull && docker compose up -d`。什么时候需要动库（换
  `vocab.npz` 必须 `reindex --rebuild-words`，否则**识别率突然掉到底而日志一切正常**）
  见 [deploy-details.md](deploy-details.md#升级备份恢复)
- **备份**：值钱的只有 `data/`，停机再拷：`docker compose stop && sudo tar czf
  /share/Backup/photo-ar-$(date +%F).tar.gz data/ && docker compose start`
- **日常命令速查**（verify / check / reindex、`data/` 里每个文件的作用）：
  [deploy/README.md](../deploy/README.md)
- **出问题**：[deploy-details.md](deploy-details.md#排障) 的症状对照表
