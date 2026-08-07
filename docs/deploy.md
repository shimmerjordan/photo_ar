# 部署：照着做就能跑起来

目标机器是 QNAP TS-464C2（Celeron N5095，x86-64）+ Container Station，其它 x86-64
的 Docker 主机步骤一样。

**每一步都有「看到什么算成」，看不到就别往下走** —— 后面的步骤看不出前面漏了什么。
NAS 那侧（第 1～6 步）大约半小时；手机那侧（第 7 步）十分钟。批量入库和打印照片是
另外的时间，见第 5 步。

想知道**为什么**这么做（实测数据、限制的出处、排障），都在
[deploy-details.md](deploy-details.md)。这份只讲怎么做。

> 只想把**某一次 CI 构建出来的那个镜像**拉起来看看，不用读这份：Actions → server →
> 那次 run 的页面顶部就有那一版的 tag、版本号和一条最小命令（生成它的是
> `.github/scripts/summary.py`）。这份是完整流程 —— 核显硬编、白名单、隧道、入库、
> 训词表，每步都带「看到什么算成」。

---

## 先准备一样东西

| 东西 | 怎么来 |
|---|---|
| 照片、视频在 NAS 上的路径 | 例：`/share/Photo`、`/share/Video`、CloudDrive2 的 `/share/CloudDrive` |

**就这一样。**（`arcoreimg` 曾经在这张表里 —— 那是已下线安卓客户端的依赖，
整条链删掉了，见 decisions §46。）以下三样以前是必须的，现在都不是了：

- **`vocab.npz`（词汇树）** —— 服务现在**没有词表也能起**，用一个空词表跑，识别结果
  完全正确，只是每次识别会全量扫描整个库（库大了变慢）。入库几十张之后在 NAS 上
  一条命令就能训出来（第 5 步末尾），而且用你自己这批照片训的词表比拿别的照片训的
  更合身。以前那条"先在开发机上 `photoar build` 再 scp 上去"的路仍然可用，只是不再
  必须 —— 它本来是个死锁：训词表要先有描述子，有描述子要先能入库，能入库要先起服务。
- **一个随机 token** —— 现在是可选的。人走 `/admin` 各自一个账号登录，鉴权不依赖它；
  它只是给**机器**用的（`tools/batch_ingest.py`）。要批量入库就设一个
  （`openssl rand -hex 24`），只手工入几张可以不设。
- **`config.json`** —— 全部配置都能从环境变量来（`.env`），不需要这个文件。想用它
  也行（更细的参数），把它放在 `deploy/config.json` 就会被优先采用。

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

不需要把源码传上去 —— 镜像在 GHCR 上，只要两个文件加一个二进制：

```bash
mkdir -p /share/Container/photo-ar/{data,tools} /share/Photo/_arphoto_inbox
cd /share/Container/photo-ar

R=https://raw.githubusercontent.com/shimmerjordan/photo_ar/main
curl -fsSLO $R/docker-compose.yml
curl -fsSL $R/.env.example -o .env
curl -fsSL $R/tools/batch_ingest.py -o tools/batch_ingest.py    # 第 5 步用
```

> `_arphoto_inbox` 那个目录要**先建出来**：compose 里它是一条可写挂载，落在只读挂载
> 的 `/share/Photo` 里面 —— 宿主机上不存在的话容器起不来（报
> `read-only file system`）。不需要上传功能就把 `.env` 里的 `PHOTOAR_UPLOAD_DIR`
> 留空、并删掉 compose 里那条挂载。

改一处配置：

**`.env`** —— **只有 `PHOTOAR_ROOTS` 必须看一眼**，写的是**容器内**路径：

```
PHOTOAR_ROOTS=照片=/share/Photo,视频=/share/Video,网盘=/share/CloudDrive
```

要批量入库的话再填 `PHOTOAR_TOKEN=$(openssl rand -hex 24)`（这个文件不要提交、
不要外传）。其余变量都有能用的默认值，`.env.example` 里逐条写了改它会发生什么。

**`docker-compose.yml`** —— 把 `volumes` 里那几条挂载改成你自己的共享文件夹，
**冒号两边写成一样**：

```yaml
- /share/Photo:/share/Photo:ro        # 左边宿主机，右边容器内
```

一样是故意的：入库时填的路径在宿主机、容器里、白名单里是同一个字符串，不用换算。
改完记得 `PHOTOAR_ROOTS` 与它们对得上。

> **你的目录完全不是 `/share/Photo` 那一套**（比如素材全在
> `/share/Study/media_bed/photo-ar` 下面）——`docker-compose.yml` 顶部的
> 「改成你自己的路径」那一节有完整的对照例子，以及三条各有一个**不响的失败方式**的
> 约束：`/data` 别落在 `PHOTOAR_ROOTS` 之内、上传目录必须在 `PHOTOAR_ROOTS` 之内且
> 挂载可写、宿主机上的目录要先 `mkdir -p`（不建的话 dockerd 会以 root 建一个空目录，
> 不报错，然后服务真的去索引它）。

**看到什么算成**：`ls .env docker-compose.yml` 都在。

最后长这样：

```
/share/Container/photo-ar/
├── docker-compose.yml
├── .env                  ← PHOTOAR_ROOTS（token 可选）
├── data/                 ← 持久卷，索引、SQLite、词表、模型、缩略图、转码产物都落这里
```

---

## 3. 起服务

```bash
docker compose pull        # 从 GHCR 拉现成镜像，不在 NAS 上构建
docker compose up -d
docker compose logs -f photo-ar-server
```

**看到什么算成**：日志里 `[photoar] 监听 0.0.0.0:8964｜照片 0 张｜后端 orb`。
日志里还有两条要**现在就读掉**的：

```
[photoar] 已创建引导管理员 'admin'，随机口令：xxxxxxxxxxxx
[photoar] ↑ 这行只出现这一次。
```
抄走它，等下登录 `http://<NAS>:8964/admin`，**进去第一件事就是改掉**。
（想要一个自己记得住的口令就先在 `.env` 里填 `PHOTOAR_ADMIN_PASSWORD`。）

管理台一共五个页签，**每个都有自己的地址**（`/admin/users`、`/admin/config`…），
可以直接收藏或者发给另一个管理员，刷新和浏览器的前进后退都成立：

| 页签 | 地址 | 干什么 |
|---|---|---|
| 用户 | `/admin/users` | 建账号。**服务端不会自动建号**，宾客的名字只能在这里出现 |
| 授权 | `/admin/grants` | 选一个人，勾出他能看到的照片。整体替换，不是追加 |
| 配置 | `/admin/config` | 识别阈值、入库闸门、会话时长，以及**素材挂载点**（见下） |
| 媒体 | `/admin/photos` | 库里有什么、各自配了哪段视频（见下）。**显示名是「媒体」，地址仍是 `photos`** —— 那个地址写在文档和收藏夹里 |
| 批量 | `/admin/batch` | Excel 模板下载 / 导入 / 导出（见下） |

### 素材挂载点（配置页底部）

「照片和视频从哪儿找」。除了 compose 里的 `PHOTOAR_ROOTS`（那几个会列出来，只读），
还能在这儿加，**改完立刻生效，不用重启**：

| 类型 | 填什么 | 行为 |
|---|---|---|
| 本机绝对路径 | 容器内的目录，如 `/media/photos` | 直接读，**不拷贝** |
| WebDAV | 完整地址 + 可选的用户名/口令 | 添加时先下载到落地目录 |

> **SMB / NFS 的 NAS 怎么办**：在宿主机 `mount` 好，再在 compose 里挂进容器 —— 那样它
> 在容器里就是一个普通路径，用「本机绝对路径」就行。没有为 SMB/SFTP 单独做客户端
> （那需要第三方依赖，理由见 decisions.md 第 26 节）。

WebDAV 地址的常见写法：群晖是 `https://<host>:5006/`，Nextcloud 是
`https://<host>/remote.php/dav/files/<用户名>/`。填错时会明确告诉你「这不是一个 WebDAV
端点」还是「凭证不对」还是「连不上」——三者的下一步动作不同。

> ⚠️ 挂载点的口令**明文存在库里**（`data/catalog.db`）。没有 KMS 可用的情况下加密只是
> 把明文换成「明文 + 一层仪式」，真正的边界是 `data/` 的文件权限。给 WebDAV 用一个
> 只读、只能看照片目录的账号，别用管理员账号。
>
> ⚠️ 加「本机绝对路径」挂载点会**扩大服务端愿意读的范围**。这是管理员权限之内的事
> （他本来就能改配置、建管理员），但每次变动都会在日志里记一行，方便回查。

**媒体页**顶部有「添加照片」：挑一张图 → 挑配它的视频（可跳过）→ 入库并建立映射。素材从
上面配的那些位置来。如果那张照片已经入库了，它不会只报一句「已存在」——会告诉你那是哪一张、
现在配的是哪段视频，并问你要不要换成刚挑的这段。

照片列表**下面**还有一段「传上来但还没入库的文件」。手机传上来的东西先落到落地目录再入库，
中间断了（超时、被判近重复、或者人退出了）就会躺在那儿 —— 原来管理台上哪儿都
看不到它。图片可以在那儿直接入库，视频可以挑几张照片配上去。

> 媒体页在你**切回这个标签页**时会自动重取。所以在手机上加完照片，转回电脑看一眼就是新的，
> 不用点刷新。其它几页刻意不自动刷 —— 那上面有你改了没保存的东西。

**媒体页**有「按照片」和「按视频」两个方向。它们不是详略之分，是两个不同的问题：按照片是
「这张配了吗」，按视频是「这段视频影响谁」—— 一段迎宾视频往往配给很多张照片，改它之前要
知道会牵动哪几张。解除关联只清 photo 表上那两列，**视频文件本身不删**（同一段可能还配给
别的照片）。

**批量页**的流程是「下模板 → 填 → 导入（只预览）→ 执行」。一行 = 一个用户 + 一张照片 +
一段视频 + 一次授权，填几列就做几件事。要点：

- 照片和视频路径填的是**容器内**的路径（和 `PHOTOAR_ROOTS` 一致），不是你电脑上的路径。
  这是导入失败最常见的原因，所以预览阶段就会把不在白名单里、不存在、类型填反的路径逐行指出来。
- 选完文件**只是预览**，一行都还没写库。确认之后点执行，逐行做，每行成败单独显示。
- 逐行执行是**浏览器**在做的，所以执行期间别关页面。关了就停在半路 —— 而改完出错的几行
  再导一遍是安全的（重复的用户和照片不会建两遍）。
- `.xlsx` 和 `.csv` 都收。Windows 上 Excel「另存为 CSV」默认写的是 GBK，也认。
- 「用户」那份导出的表头与模板一致，可以导出 → 在 Excel 里改 → 导回来。

```
[photoar] ⚠️ 没有词表（找不到 /data/models/vocab.npz），正在用空词表运行。
```
这条是**正常**的（见"先准备"那节）。第 5 步末尾会训一份。

然后确认鉴权真的在：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8964/v1/ping   # 401
```

不带凭证必须是 401。**没设 `PHOTOAR_TOKEN` 时也必须是 401** —— 空 token 让运维凭证
那条路整体禁用，不是"谁都能进"。设了 token 的话再带上它试一次，该回
`{"ok": true, ...}`：

```bash
curl -sS -H "Authorization: Bearer $(grep '^PHOTOAR_TOKEN=' .env | cut -d= -f2)" \
  http://127.0.0.1:8964/v1/ping
```

响应里还有几个值得看一眼的状态字段：

| 字段 | 意思 |
|---|---|
| `backend` / `backendRequested` | 实际在跑的后端 / 配置要的那个 |
| `backendDegraded` | true = 两者不一样（XFeat 模型没取到，回退了 ORB）。**别忽略它** —— 否则你会以为换了特征却毫无变化 |
| `vocabTrained` | false = 正在用空词表，每次识别全量扫描 |
| `photos` | 识别库里几张 |

> `docker compose ps` 里的 `health` 要在约 20 秒后变成 `healthy`。它探的就是
> `/v1/ping`；没设 token 时 401 也算健康（那已经是没有凭证时能证明的全部）。

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

确认可用之后，把 `.env` 里的 `PHOTOAR_VIDEO_ENCODER` 改成 `h264_vaapi` 再
`docker compose up -d`。这样哪天核显不可用（QTS 升级、设备号变了）会**直接报错**，
而不是悄悄软编 —— 后者唯一的发现方式是掐表。

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
docker compose exec photo-ar-server photoar-server check
curl -sS -H "Authorization: Bearer $T" http://127.0.0.1:8964/v1/photos \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["total"], "条")'
```

### 顺手确认「浏览器里的识别库」准备好了

识别是在**用户的浏览器**里做的，前提是它能拿到那份识别库包（ORB 描述子 + 元数据）。
一条 curl 就够 —— 但它要**会话 cookie**（这个接口是网页版自己的，不吃 Bearer 令牌）：

```bash
# 先登录拿一次 cookie（口令就是启动日志里那个，或者你自己设的）
C=$(curl -sS -i -X POST -H 'Content-Type: application/json' \
      -d '{"name":"admin","password":"<你的口令>"}' \
      http://127.0.0.1:8964/v1/auth/login \
    | grep -i '^set-cookie:' | sed 's/^[Ss]et-[Cc]ookie: *//' | cut -d';' -f1)

curl -sS -H "Cookie: $C" -o /dev/null -w '%{http_code}  %{size_download} bytes\n' \
  http://127.0.0.1:8964/api/lib
```

**看到什么算成**：`200` 加一个几十 KB 的数字。45 张照片的库约 50KB —— 手机上零点几秒
就下完，而且带 ETag，第二次进页面是 304。

- 库是**按调用者的授权集**算的：用某个家人的账号调，拿到的就是他能扫的那些照片。
- 一张都没被授权时它照样回 200，包里 `nPhotos: 0` —— 页面会显示"还没有照片可扫"
  而不是报错。看到这个就是授权没做，跟浏览器无关。
- ETag 是**包的内容哈希**：授权集变了、或者换了一张参考图，它就变，手机下次进页面
  自动重下。不需要任何一处"记得去清缓存"。
### 最后：训词表

入完库再训，用的就是你这批照片自己的描述子。**不训也能用**（识别结果一样正确），
但每次识别会全量扫描整个库 —— 实测 45 张的库上服务端耗时 124ms，训完降到 64ms，
而且这个差距随库大小线性拉开。

```bash
docker compose exec photo-ar-server photoar-server build-vocab
docker compose restart photo-ar-server        # 词表是启动时加载的
```

**看到什么算成**：`build-vocab` 打出 `词表训好了：/data/models/vocab.npz` 和
`N 张照片｜M 条描述子｜K 个词`；重启后 `/v1/ping` 的 `vocabTrained` 变成 `true`。

> 服务**跑着**的时候也能训：`curl -X POST .../v1/admin/rebuild-vocab`（要 admin
> 凭证）。那条路在同一个进程里，不用重启就生效。CLI 那条要与服务分开跑
> （`exec` 时服务照常在跑，但**别同时**在入库 —— 两个进程各有一把进程内写锁，
> 管不住对方）。
>
> 之后又入了很多张，可以再训一次；不训也不会坏，只是词表对新照片的区分度略低。

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

（宾客要用相机的话这个地址还不够 —— 它是 http。见第 7 步「视频走哪条路」。）

不用开子网路由，也**不要开 Funnel**。

### 6b. Cloudflare Tunnel（宾客走这条，视频也走）

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

回 `{"ok": true...}`，而且 `https://arphoto.<你的域名>/` 打开就是网页版。顺手看一眼隧道
健康：`docker exec cloudflared cloudflared tunnel info <tunnel 名>` 要有 4 条连接、落在
**两个不同 region**（只落一个会 `Degraded`，表现是偶发 502）。

**宾客的视频也从这条路出去**（2026-08-05 定的，之前那版文档写着"隧道只跑 API"）。
理由是网页版的宾客不装任何东西，没有第二条路。代价与能降低暴露面的两件事写在
[deploy-details 的「隧道的三条硬限制」](deploy-details.md#隧道的三条硬限制)，
**其中一件要现在就做**：用完把这条 ingress 摘掉。

### 6c. 让引擎停在 Cloudflare 边缘（宾客的第一屏差 10 秒以上）

网页版有 2.6MB 的静态资源是所有宾客共享、内容永不变的（识别引擎 2.43MB、字体 175KB、
素材 37KB）。不做任何配置的话，**每个宾客都从 NAS 拉一遍**，而且最大的那一块拉不到
边缘缓存里 —— Cloudflare 的默认缓存是按扩展名的，那份名单里**没有 `.wasm`**。

Caching → Cache Rules 加一条，**表达式必须按路径限定**：

```
表达式：(http.host eq "arphoto.你的域名") and
        (starts_with(http.request.uri.path, "/vendor/") or
         starts_with(http.request.uri.path, "/art/"))
设置：Cache eligibility → Eligible for cache
     Edge TTL         → Use cache-control header if present
     Cache key → Query string → Include all
```

> ⛔ **绝对不要用 "Cache Everything" 或不限路径的规则。** `/v1/*` 与 `/api/*` 是**按人
> 授权**的（`/api/lib` 是这个用户能扫的照片、`/api/stream/<票>` 是一次性票据换来的
> 视频）。把它们缓存到边缘就是把一个人的视频发给另一个人 —— 而且没有任何症状，
> 你看到的是"能播"。

**看到什么算成**：连打两次，第二次是 `HIT`。

```bash
V=$(curl -s https://arphoto.<你的域名>/vendor/opencv.js | grep -o 'opencv\.wasm?v=[0-9a-f]*')
for i in 1 2; do
  curl -sI -H 'Accept-Encoding: br' "https://arphoto.<你的域名>/vendor/$V" \
    | grep -iE 'cf-cache-status|content-length'
done
```

`Content-Length` 该是 250 万左右而不是 1195 万（说明预压的 brotli 一路透传了）。
拿到 `DYNAMIC` 说明规则没命中，`BYPASS` 说明被别的规则或 cookie 挡掉了 ——
两种都在 [deploy-details 的「CDN」那节](deploy-details.md)里有对照。

---

## 7. 让宾客用起来（打开一个网页）

没有 App 要装。**把地址发出去就行** —— 但有一条硬性前提：

> ⚠️ **地址必须是 `https://`。** 相机（`getUserMedia`）只在**安全上下文**里存在：
> `https://` 任意域名都算，`http://localhost` 算（只有本机），
> **`http://192.168.x.x` 和 `http://100.x.x.x` 都不算**。
> http 地址打开的页面会明确告诉用户"这个页面不是安全上下文"并给出三条出路，
> 但那是排查用的，不该发给宾客。

所以对外的地址就是第 6 步那条隧道的地址：`https://arphoto.<你的域名>/`。

**看到什么算成**：手机 4G 打开那个地址 → 登录蒙版 → 输名字（宾客口令留空）→
进去是一整页一颗「扫一扫」→ 给相机权限 → 举起**打印出来的**那张照片，离半米左右 →
视频贴在照片上播起来。

### 视频走哪条路

⚠️ 这里有一个**账号级风险**要留意：Cloudflare 的服务条款不允许用 CDN 大量分发视频
（details 里「隧道的三条硬限制」写了全文）。上面那条 ingress 只该用来跑 API 小包。

所以宾客看视频的正路是 **Tailscale**：让他们装 Tailscale 登进你的 tailnet，
打开 `https://<你的机器>.<tailnet>.ts.net:8964/`（MagicDNS 名字 + `tailscale cert`
签的真证书，无警告）。婚礼那种"几十个人一次性"的场合，更省事的做法是现场 Wi-Fi +
下面那条"局域网里自测"的证书路子。

要在 Tailscale 上拿真证书，先去 Tailscale 后台 DNS 页面打开 **HTTPS Certificates**
（默认是关的），然后：

```bash
tailscale cert <你的机器>.<tailnet>.ts.net
```

把生成的 `.crt` / `.key` 放进一个目录，在 `.env` 里指过去：

```bash
WEBFRONT_CERT_DIR=/share/Container/photoar-certs
WEBFRONT_TLS_CERT=/certs/<机器名>.<tailnet>.ts.net.crt
WEBFRONT_TLS_KEY=/certs/<机器名>.<tailnet>.ts.net.key
```

（左边是宿主目录，右边两个是**容器内**的路径 —— 那个目录挂到 `/certs`。）
`docker compose up -d` 之后启动日志第一行会变成 `https://0.0.0.0:8964`。

**公共 CA 不会给 `100.64.0.0/10` 这样的 IP 签证书**，所以这条路必须用 MagicDNS 的
主机名，不能用 `https://100.x.y.z`。

### 局域网里自测（没有隧道也没有真证书时）

自签证书，`web-front/tools/gen-dev-cert.sh` 会把本机所有 IPv4 和 tailnet 域名都写进
SAN（少了 SAN 现代浏览器连"继续访问"都不给）：

```bash
cd web-front && ./tools/gen-dev-cert.sh
```

然后同上填三个 `WEBFRONT_*`。手机第一次打开会有证书警告，点「高级 → 继续」；
嫌烦就让手机打开 `https://<地址>:8964/ca.crt` 装一次本地 CA，之后这台机器签的证书
全都被信任。

> 自签证书下**视频照样能播**：安卓的平台媒体组件有自己的 TLS 栈、不认自签证书，
> 但网页版走的是 MediaSource（页面自己 fetch 再喂解码器），完全不经过那个组件。
> 这是默认路径，不需要配置。见 `web-front/public/mp4stream.js` 顶部那张表。

还有一条**完全不要证书**的路，调试时最顺手：

```bash
adb reverse tcp:8964 tcp:8964      # 手机上的 8964 转到这台机器
# 手机浏览器打开 http://localhost:8964
```

`localhost` 按规范就是安全上下文，相机能开、也没有 TLS 那一层要绕。

### 登录之后，宾客和管理员看到的不一样

| | 宾客（viewer） | 管理员（admin） |
|---|---|---|
| 底栏 | 扫一扫 / 设置 | 照片 / 素材 / 管理 / 设置 |
| 首页 | 整页一颗「扫一扫」，下面一行「你有 N 张照片可扫」 | 照片库，扫一扫是底栏中间那颗圆 |

名字必须是管理台里建过的那个（服务端不自动建号）。**宾客留空口令**，管理员必填。

给宾客的时候只要说一句「打开链接，输你的名字，口令不用填」就够了 —— 他那一屏上只有
一个按钮。如果他说扫了没反应，先看首页那行提示：显示「管理员还没有把照片授权给你」
就是授权没做，跟他的手机无关。

管理员的「管理」页里有管理台入口、识别历史、本机缓存。管理台就在**同一个地址**的
`/admin` 上，同源、共用同一条会话 —— 点开不用再登一次。

> ⚠️ 在管理台里点「登出」会把扫描页那边的登录一起作废（是同一条会话）。想换账号才用它。

### 「素材」页：手机相册 → NAS

婚礼当天刚拍的东西在手机里，而管理台跑在 NAS 上看不到它们。挑一张照片 + 配它的那段
视频，点一次「传上去并建立映射」就完事 —— 不用再去别处入库、也不用去管理台配视频。

上传时会问一句**照片印出来有多宽**（七个预设）。**留「不知道」完全没问题** ——
网页版按照片四个角贴，不看物理尺寸，这个数只是记进库里。

下面的**上传历史**列出这台手机传过的每一组，每条都能：

- **换照片** —— photoId 不变，所以授权和配的视频都留着（换成一张更清楚的扫描件时用）
- **换视频** / **配视频** —— 当时忘了配就在这儿补
- **试播** —— 全屏放一遍确认配对没错，不开相机

> 换照片要重算特征，几十秒是正常的。如果被拒，服务端会说清原因（提不出特征 /
> 和库里另一张近重复），并且明确告诉你**原来那张没有被换掉**。

历史只记这台手机传的（换个浏览器就看不到了）。全库的映射在管理台的「媒体」页。

**传到重复的怎么办**：第二次挑同一张照片时服务端会拦下来（这是对的），但**不是死胡同**
—— 页面会把那张已有照片查出来，告诉你它是哪一张、现在配的是哪段视频，然后按情况给出
唯一可做的那件事：

| 库里那张的情况 | 你这次挑了视频 | 页面给你什么 |
|---|---|---|
| 已有别的视频 | 是 | 「换成新的」（会说清旧的那段不再关联） |
| 还没配视频 | 是 | 「配上」（不覆盖任何东西） |
| 配的就是这段 | 是 | 「已经是你要的样子了」，没有动作 |
| 任意 | 否 | 说清现状；没配视频时会提醒你去配 |

> 一张照片只能配**一段**视频，但一段视频可以配给**多张**照片。所以「视频重复」从来不是
> 问题（直接配给新照片就行），而「照片重复」只能去改那一张已有的。

**上传是先查后传的**：页面先在本地算一遍哈希问服务端一次（几百字节、几十毫秒）。如果那份
内容服务端已经有了，就**整个跳过上传** —— 不会让你等着传完 20MB 才被告知「已存在」。

**入库要多久**：一张 10MP 手机照片跑完整条流水线约 3.5 秒（在开发机上；N5095 上大概
3–4 倍），视频转码另算（硬编几秒，软编可能几十秒）。觉得慢就去管理台「配置」页调
**入库时自匹配分的计算分辨率**：默认 1280，调到 960 会快 4 倍，代价是去重判定略微保守。

### 扫不出来 / 贴不上怎么办

> **非调试模式下取景器底下那一行基本是空的**，这是有意的：宾客要的答案只有一句
> 「我该怎么做才能看到视频」，而库里几张、多少 fps、四角过期了多少毫秒一个都不回答
> 这个问题，挤在一起还会把真正有用的那半句（「这张照片还没配视频」）盖掉。
> 打开调试模式之后那一行才变成完整的技术读数。

界面上那三句提示都对应真实的干扰，照做就行：

| 提示 | 为什么 |
|---|---|
| 手指别压住边缘 | 一只手拿着照片时手指常压在边上，特征匹配会掉一大截 |
| 避开反光 | 覆膜或玻璃相框的高光会把那一块的特征全盖掉 |
| 拿稳一点 | 运动模糊直接毁掉 ORB 的角点 |

**「一直没反应」先看这一条：库里有没有同一张照片的两份。**

同一内容入库两次的话，识别时两份会互相触发比值检验判 `ambiguous` —— **两份都永久扫不
出来**，而现象和「识别器坏了」一模一样。判断方法：管理台「媒体」页看有没有两条缩略图
长得一样；或者看「管理 → 识别历史」，未命中那几条如果标着红字
「库里有近重复，两张互相挤掉了」，就是这个。

处理：在管理台「媒体」页那一行点**删除**，留一张就好（参考图和视频文件都不动，
NAS 上什么都不会少）。删完立刻就能扫出来。

入库闸门现在会拦住新的重复（会回 409 并列出是哪一张），所以这件事只会发生在
2026-08-03 之前入的库上。

**要查为什么贴不上**：设置 → 关于 → **连按版本号 7 下**打开调试模式，再扫一次 ——
屏幕左上角会出现一块滚动日志，从抽帧、识别耗时、内点数、四角坐标一直到视频的
`readyState`，全在里面。截个图就够排查。关掉调试模式就没有它。

零散补几组走「素材」页（媒体库右上角的「＋」就是切到那里）。批量则用管理台的「批量」页 ——
两条路的区别只是素材在哪：素材页从**手机相册**取，批量页处理**已经在 NAS 上**的文件。

## 附：在开发机上跑（给改代码的人）

不碰 NAS，在自己机器上起同一套服务端，手机走 Tailscale 连过来。用的是覆盖层
`deploy/compose.local.yml`，和 NAS 那份的差别只有三样：照片/视频目录、镜像本地构建、
不去下 xfeat 模型。

```bash
cd photo-ar
mkdir -p local/photos/_inbox local/videos          # 素材放这儿，local/ 在 .gitignore 里
cp .env.example .env                                # 至少填 PHOTOAR_ADMIN_PASSWORD（见下）

# 把文件列表写进 .env，之后这个目录里直接用 docker compose，覆盖层自动带上
echo 'COMPOSE_FILE=docker-compose.yml:deploy/compose.local.yml' >> .env

docker compose up -d --build
docker compose logs -f
```

**别漏掉那个覆盖层**，而且漏掉的后果是不可逆的：主文件挂的是 NAS 的 `/share/Photo`，
开发机上没有那个路径，dockerd 会**以 root 身份在你的根目录下建出来**（空目录，不报错，
服务还真去索引它）。写进 `.env` 就没机会忘；理由和合并规则在
[../deploy/compose.local.yml](../deploy/compose.local.yml) 顶部。

起来之后本机 `http://127.0.0.1:8964/` 是网页版、`/admin` 是管理台，同一个端口。

手机上要开相机就得 https（`http://100.x.x.x` 不算安全上下文），两条路：给容器配自签
证书（`web-front/tools/gen-dev-cert.sh`，见第 7 步），或者 `adb reverse tcp:8964
tcp:8964` 之后在手机上打开 `http://localhost:8964` —— 后者不用证书，调试时最顺手。

三个容易踩的点：

- **管理员口令写在 `.env` 里，不在 compose 里。** 这个仓库是公开的，固定口令写进
  compose 就等于发布出去了（理由见 [decisions.md §16](decisions.md#16-开发机上的固定口令为什么不写在-compose-里)）。
  `.env` 里填 `PHOTOAR_ADMIN_PASSWORD=admin` 就能省掉每次重建容器去日志里翻随机口令。
  留空也行，那就是每次去翻。
- **cpus / mem 刻意不放宽。** 验收条件之一是「在 NAS 的资源预算内跑得动」（N5095 四核）。
  开发机放开了怎么测都快，然后到 NAS 上才发现撞超时 —— 那就白测了。只有专门压测上限时
  才临时放宽，改完记得改回来。


---

## 跑通清单

| # | 做什么 | 看到什么算成 | 步骤 |
|---|---|---|---|
| 1 | 开 SSH，找到 docker | `docker compose version` 打出 v2.x | 1 |
| 2 | 建 `_arphoto_inbox` | 目录在 | 准备 |
| 3 | 拉 compose 与 `.env`，填 `PHOTOAR_ROOTS` | 三个文件都在，冒号两边一样 | 2 |
| 4 | `docker compose pull && up -d` | 日志 `监听 0.0.0.0:8964`，约 20s 后 `healthy` | 3 |
| 5 | **抄走日志里那行随机管理员口令** | 登进 `/admin` 并立刻改掉 | 3 |
| 6 | 不带凭证 ping 一次 | `401`（设了 token 的话带上它再来一次要 `200`） | 3 |
| 7 | 问服务它选了哪个编码器 | `h264_vaapi` | 4 |
| 8 | **手工入一张**（挑纹理丰富的） | `201` + `photoId` | 5 |
| 9 | 批量入库（先 `--limit 5 --dry-run`） | 配对没错，再放量 | 5 |
| 10 | **训词表**并重启 | `/v1/ping` 的 `vocabTrained` 变 `true` | 5 |
| 11 | 带 cookie 拉一次 `/api/lib` | `200` + 几十 KB | 5 |
| 12 | 配上 https（隧道的真证书，或自签） | 启动日志第一行是 `https://0.0.0.0:8964` | 7 |
| 13 | 手机打开 `https://<地址>:8964/` | 登录蒙版出来 | 7 |
| 14 | 用管理员登进去，「照片」里能看到第 8 步那张 | 缩略图和标题 | 7 |
| 15 | 「扫一扫」举起照片 | **视频贴在照片上播起来** | 7 |
| 16 | 装 Tailscale（NAS + 手机） | 4G 下 ping 回 401 | 6a |
| 17 | 加一条 cloudflared ingress | 外网 curl 到 `{"ok": true}` | 6b |
| 18 | 建一个宾客账号、授权几张照片 | 用它登进去只看到一颗「扫一扫」 | 7 |
| 19 | 关 WiFi 走 4G 再扫一次 | 还能认出来、还能播 | 7 |
| 20 | 备份 `data/` | 有一份压缩包 | 下面 |

**第 15 步是整条链路第一次真正闭合的地方** —— 在它之前的绿灯都只说明「零件没坏」。

---

## 之后

- **升级**：`docker compose pull && docker compose up -d`，然后**清掉被顶掉 tag 的
  旧镜像** —— `pull` 会把上一份 1.1GB 的镜像变成没名字的 `<none>` 留在盘上，不清就随
  升级次数堆积：

  ```bash
  docker image prune -f --filter label=org.opencontainers.image.source=https://github.com/shimmerjordan/photo_ar
  ```

  那个 `--filter` 不能省：不带它就会连这台机器上**别的服务**的无 tag 镜像一起清。
  什么时候需要动库（换 `vocab.npz` 必须 `reindex --rebuild-words`，否则**识别率突然
  掉到底而日志一切正常**）见 [deploy-details.md](deploy-details.md#升级备份恢复)
- **备份**：值钱的只有 `data/`，停机再拷：`docker compose stop && sudo tar czf
  /share/Backup/photo-ar-$(date +%F).tar.gz data/ && docker compose start`
- **日常命令速查**（verify / check / reindex、`data/` 里每个文件的作用）：
  [deploy/README.md](../deploy/README.md)
- **出问题**：[deploy-details.md](deploy-details.md#排障) 的症状对照表
