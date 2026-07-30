# 在 QNAP 上把 photo-ar 跑起来，并从外网访问

目标机器：QNAP TS-464C2（Celeron N5095，4C/4T，8GB），QTS + Container Station，
已经在跑 cloudflared（tunnel 名 `nas-adan`）与 CloudDrive2。

这份文档是**按顺序做完就能用**的流程；每一步都带验证命令，验证不过就不要往下走。
只讲「怎么做」和「怎么确认做成了」，设计上的理由在
[spec](superpowers/specs/2026-07-28-ar-photo-video-design.md)，日常维护命令在
[deploy/README.md](../deploy/README.md)。

**按这个顺序读**：§1→§7 是服务端和三条通道，做完 NAS 那边就齐了；然后 §11 出 APK
并在手机上配通道，§12 是一张从零到「举起手机认出一张照片」的清单 —— 想快点看到
效果的话，先扫一眼 §12 再回头做 §1。§8～§10 和 §13 是参考章节，需要的时候再翻：
§8 加速、§9 排障、§10 性能基线、§13 升级与备份。

> 章节号被 `deploy/README.md`、`docker-compose.yml` 的注释和 spec 引用着（§4.3、
> §7.1、§8.2 这些），所以新增内容都挂在子小节或末尾，1～10 的编号不动。

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

- [ ] **SSH 能进 NAS**：QTS 控制台 →「网络与文件服务」→「Telnet / SSH」→ 勾
      「允许 SSH 连接」（默认 22 端口，只有 administrators 组的账号能登）。然后
      `ssh admin@<NAS 内网 IP>`
- [ ] Container Station 已装，且 SSH 里能执行 `docker`。**不在 PATH 里是常态**，
      这样找：

      ```bash
      ls /share/*/.qpkg/container-station/bin/docker        # 卷名 QTS 是
      # CACHEDEV1_DATA、QuTS hero 是 ZFS530_DATA 之类，各机不同
      export PATH=$(dirname $(ls /share/*/.qpkg/container-station/bin/docker)):$PATH
      docker compose version    # v2 是插件形式，要能打出 v2.x
      ```

      这一行 `export` 每次 SSH 都要重来，写进 `~/.profile` 省事（QTS 升级会重置）
- [ ] 照片、视频所在的共享文件夹路径记下来（例：`/share/Photo`、`/share/Video`、
      CloudDrive2 的挂载点 `/share/CloudDrive`）。**用 `/share/xxx` 这一层，不要用
      `ls -l` 出来的 `/share/CACHEDEV1_DATA/xxx`** —— 前者是符号链接，容器里挂进去
      的、`config.json` 白名单里写的都是它；混用会 403（§2.3）
- [ ] `arcoreimg`：ARCore SDK for Android 里的 `tools/arcoreimg/linux/arcoreimg`，
      闭源二进制不在仓库里，要自己拷到 `tools/arcoreimg` 并 `chmod +x`
- [ ] 词汇树 `vocab.npz`：**服务端不训练**，必须先在有 Python 环境的机器上
      `photoar build --photos <一批风格接近的照片> --out /tmp/corpus`，把
      `/tmp/corpus/vocab.npz` 拷到 NAS 的 `data/vocab.npz`
- [ ] 一个随机 token：`openssl rand -hex 24`
- [ ] 只有要自己出 APK 才需要：一台装了 **JDK 17 + Android SDK（platform 35）**
      的机器（§11）。已经有别人给的 apk 就跳过

> 换过 `vocab.npz` 就必须 `reindex --rebuild-words`。不重建的表现是**识别率突然
> 掉到底而日志一切正常** —— 库里存的词序列是旧树量化出来的，倒排索引指向错误的桶。

## 2. 把代码放到 NAS 上

### 2.1 传代码：这个仓库没有远端

`git remote -v` 是空的 —— 代码只在开发机上，NAS 上 `git clone` 无从下手。两条路，
选一条：

**A. 传源码，在 NAS 上构建**（推荐，之后改代码也走同一条命令）

```bash
# 在开发机上。只传 git 跟踪的文件（约 1.9MB）加上那个不进版本库的闭源二进制。
# 用 tar 而不是 rsync：QTS 默认没装 rsync 服务端。
git -C ~/Projects/priv/photo-ar archive --format=tar HEAD \
  | ssh admin@<NAS> 'mkdir -p /share/Container/photo-ar && tar -x -C /share/Container/photo-ar'
scp ~/Projects/priv/photo-ar/tools/arcoreimg admin@<NAS>:/share/Container/photo-ar/tools/
scp /tmp/corpus/vocab.npz admin@<NAS>:/share/Container/photo-ar/data/vocab.npz
```

`git archive HEAD` 只打包**已提交**的内容 —— 手上有没提交的改动就先提交，或者换成
`tar -c $(git ls-files) tools/arcoreimg`。

**B. 在开发机上构建镜像，把镜像搬过去**（NAS 上不装构建链，也不占 CPU）

```bash
docker build -t photo-ar-server:phase1 .
docker save photo-ar-server:phase1 | gzip -1 | ssh admin@<NAS> 'gunzip | docker load'
# 实测传输量约 330MB（镜像 815MB，gzip -1 之后）
```

镜像里没有 `docker-compose.yml` 和 `deploy/config.json`，这两份还是得传上去（用 A
里的 `tar` 那条），并且**把 compose 里的 `build: .` 注掉**，不然 `up` 会重新构建。

### 2.2 目录布局

```
/share/Container/photo-ar/          ← 代码放这里（/share/Container 是 Container
                                       Station 自己建的共享文件夹）
├── data/                           ← 持久卷：catalog.db / library / imgdb / thumb / playable
│   └── vocab.npz                   ← 第 1 步拷进来的
├── deploy/config.json              ← 从 config.example.json 复制后改
├── tools/arcoreimg                 ← 第 1 步拷进来的
└── docker-compose.yml
```

### 2.3 config.json

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

同时改 `docker-compose.yml` 里 `volumes` 那几条 `:ro` 挂载，让**冒号两边一样**：

```yaml
- /share/Photo:/share/Photo:ro        # 左边宿主机，右边容器内
```

写成一样是故意的：这样入库时填的路径在宿主机、容器里、白名单里都是同一个字符串，
不用在脑子里做换算。**别顺手把左边换成 `ls -l` 显示的真实路径**
（`/share/CACHEDEV1_DATA/Photo`）—— 那样容器里就只有 `/share/Photo` 这个名字了，
而你在 NAS 上 `find` 出来的路径是 CACHEDEV 那条，提交进去必然 403 `path_denied`。
批量入库脚本的 `--map` 就是为这种不一致准备的（§5.2）。

## 3. 起服务

```bash
cd /share/Container/photo-ar
export PHOTOAR_TOKEN=$(openssl rand -hex 24); echo "$PHOTOAR_TOKEN"   # 记下来，客户端要填
docker compose build          # 走 2.1B 搬过镜像的话跳过这条
docker compose up -d
docker compose logs -f photo-ar-server      # 看到 listening on 0.0.0.0:8964 就行
```

`build` 在 N5095 上要几分钟（光装 opencv 那一层就约 1 分钟），中途别 Ctrl-C。
只改 `src/` 下的代码时重建很快 —— Dockerfile 把依赖单独放了一层，缓存还在。

> 也可以不用 SSH：Container Station →「应用程序」→「创建」，把
> `docker-compose.yml` 整份贴进去。代价是 `PHOTOAR_TOKEN` 得直接写在 YAML 的
> `environment:` 里（界面上没有「环境变量另填」的地方），以及 `build: .` 在界面里
> 没有构建上下文，必须先按 2.1B 把镜像 `docker load` 进去、再把 `build:` 那行删掉。
> **后面每一步的验证命令都是 SSH 里跑的**，所以 SSH 早晚要开，建议直接走命令行。

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

### 5.1 先手工入一张

入库的唯一入口是 HTTP（客户端的「新建」页也是调它）。一张照片 + 一条视频：

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

### 5.2 批量入库

上万张不可能一条条 curl。`tools/batch_ingest.py` 只用标准库（QTS 自带的 python3
就能跑），串行、可续跑：

```bash
cd /share/Container/photo-ar

# 目录配对：主文件名相同的照片和视频算一对（IMG_0421.jpg ↔ IMG_0421.MOV，
# 大小写不敏感）。先加 --limit 5 --dry-run 看配对对不对，再放量。
python3 tools/batch_ingest.py --base http://127.0.0.1:8964 \
    --photos /share/Photo/送出去的那批 --videos /share/Video/送出去的那批 \
    --recursive --width-mm 152 --title-from-name --limit 5 --dry-run

# 去掉 --dry-run --limit 正式跑。token 从环境变量取。
python3 tools/batch_ingest.py --base http://127.0.0.1:8964 \
    --photos /share/Photo/送出去的那批 --videos /share/Video/送出去的那批 \
    --recursive --width-mm 152 --title-from-name
```

照片和视频不同名、或者每张的打印尺寸不一样，就给一份清单（TSV，`照片 <TAB> 视频
<TAB> 打印宽度mm <TAB> 标题`，后两列可省，`#` 开头是注释）：

```bash
python3 tools/batch_ingest.py --base http://127.0.0.1:8964 --manifest pairs.tsv
```

要点：

- **进度写在 `batch-ingest-state.json`**（`--state` 可改）。断了、断电了、Ctrl-C 了，
  再跑一次会跳过已入库的和被确定性拒绝的（质量分不够 / 近重复 / 不在白名单 / 格式不
  支持）；网络错、超时、5xx 不记账，下次自动重试。换过照片想重试被拒的那批加
  `--retry-rejected`
- **只在 LAN 上跑**，别走隧道：单张约 5.2 秒（N5095 折算，§10），带视频的还要加转码
  的几十秒 —— 隧道 125 秒就断了（§7.1）。**一万张约 14.5 小时**，挂 `screen` / `tmux`
  里过夜，或者分几批
- **它故意不并发**。近重复闸门是拿新照片跟库里已有的比，而服务端是多线程的：并发提交
  两张互为近重复的照片，两边都看到对方还不在库里 → 两张都进去 → **两张都永久识别不
  出来**。这是正确性，不是快慢
- 宿主机路径和容器内路径不一致时（§2.3），用 `--map /share/CACHEDEV1_DATA/Photo=/share/Photo`
  改写，可以给多条
- `--skip-videos` 先只入照片（快得多），之后再逐条 `POST /v1/photo/<id>/video` 补视频。
  先看到「能认出来」再补「能播」，比一次全上更容易定位问题

跑完再 `check` 一次，然后看一眼库里到底有多少条：

```bash
docker compose exec photo-ar-server photoar-server -c /config/config.json check
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" http://127.0.0.1:8964/v1/photos \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
print(d["total"], "条，其中带视频", sum(1 for p in d["photos"] if p["hasVideo"]))'
```

### 5.3 打印那一侧：两件影响成败的事

**一、`printWidthMm` 要量，不要算。** 它是照片**画面**在现实里的实际宽度，AR 里视频
贴不贴得住全靠它。冲印店的「6 寸」不等于 152.0mm，同一批不同店也能差出两三毫米，而
且很多店会留白边 —— **拿尺量画面本身，白边不算**。横竖也要对：填的是照片摆在你面前
时的**水平方向**那条边，6 寸竖着放就是 102mm 而不是 152mm。差 5% 的表现是视频比照片
大一圈或小一圈、边缘对不齐，不是「认不出来」，所以容易被当成别的问题查半天。

**二、能不能过质量闸门，是照片本身决定的。** 本地拿 869 张真实照片实测，**569 张
（65%）被 422 拒掉**。这不是故障，是 ARCore 对增强图像的硬要求：它要的是密集且分布
均匀的高对比纹理。大片天空、纯色墙面、逆光剪影、糊掉的老照片基本都过不去；人多、
建筑、树叶、图案衣服、有细节背景的合影通过率高。所以：

- **先入库，再决定送哪几张**。不要先把照片印好送出去了才发现认不出来
- 挑不过的那批，`batch-ingest-state.json` 的 `rejected` 里有每张的质量分（满分 100，
  门槛 75）。分数 60 出头的换个裁切或许能过，个位数的别折腾了
- 一张照片印两份分别送两个人是可以的（同一份文件 → 同一个 photoId → 认出来播同一条
  视频）；但**同一场景连拍的两张**很可能互相判近重复，那时两张都认不出来 ——
  409 `near_duplicate` 就是在拦这个，别绕过它

## 6. Tailscale：在外面也能看视频的那条路

Cloudflare 那条通道**不传视频**（§7.1），所以「在外面想看」靠 Tailscale。

**NAS 上装**：App Center 里搜 Tailscale（官方 QPKG）装上；商店里没有就去
[pkgs.tailscale.com](https://pkgs.tailscale.com/stable/#qnap) 下对应架构的 `.qpkg`
（TS-464C2 是 x86-64），App Center →「手动安装」传上去。装完从 QTS 主菜单打开它，
点登录，浏览器里授权到你的 tailnet。然后 SSH 里确认拿到地址：

```bash
tailscale ip -4        # 100.x.y.z，这就是要填进客户端的那个
tailscale status       # 手机登进同一个 tailnet 后应该在这份列表里
```

> 已经在 NAS 上跑着 Tailscale **容器**的话别再装 QPKG，两个都抢 `tailscale0` 会互相
> 打。容器方案要 `--net=host` 或者把 8964 转进去，否则容器拿到的 100.x 地址上没有
> photo-ar 在听。

**手机上装**：应用商店里的 Tailscale，登同一个 tailnet。**不要开 Funnel** —— Funnel
把流量重新推到公网，等于又回到带宽和条款那两个问题上。

**验证**：手机关 WiFi 走 4G、开着 Tailscale，浏览器打开
`http://<tailscale IP>:8964/v1/ping` 要看到 **401**（没带 token）而不是连不上 ——
401 就说明这条通道通了。

**要不要开子网路由**：不用。只需要访问 NAS 这一台，装了 Tailscale 的两端直连就够；
`tailscale up --advertise-routes=...` 是为了让手机访问家里**别的**设备，与本项目无关，
而且开了要在后台批准路由，多一处会忘的配置。

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

## 11. 客户端：出 APK，在手机上配通道

### 11.1 出包

需要 JDK 17 和 Android SDK（platform 35 + build-tools）。SDK 位置用环境变量
`ANDROID_HOME`，或者在 `android/local.properties` 里写 `sdk.dir=/path/to/Android`。

```bash
cd android
./gradlew :app:assembleRelease
# → app/build/outputs/apk/release/app-release.apk（实测 9.1MB）
```

装到手机：`adb install -r app-release.apk`，或者把 apk 拷进 NAS 的共享文件夹、
手机浏览器下载后点安装（要先允许「未知来源」）。

> **release 是用 debug key 签的**（`app/build.gradle.kts` 里写着：这个包只装自己的
> 机器，不上应用市场）。带来一个真会咬人的后果：debug keystore 在
> `~/.android/debug.keystore`，**换机器出包就换了签名，签名不同的包不能覆盖安装**，
> 只能先卸载 —— 而卸载会把设置里填的通道、令牌和离线缓存一起清掉。要长期给几台手机
> 出包，就把这个 keystore 备份好，始终用同一份。

### 11.2 手机那边的前提

| 项 | 要求 | 不满足会怎样 |
|---|---|---|
| 系统版本 | Android 7.0+（minSdk 24） | 装不上 |
| ARCore（Google Play Services for AR） | 商店里装一下 | **功能不丢**：识别照旧，只是退化成「认出后全屏播放」，视频不再贴在照片上（清单里 ARCore 写的是 optional） |
| 相机权限 | 首次进扫描界面时给 | 扫不了 |
| Tailscale | 想在外面看视频才需要（§6） | 在外面只能用 API，视频播不了 |

### 11.3 填通道和令牌

打开 App → 底栏「设置」：

- **访问令牌**：就是 `PHOTOAR_TOKEN`，和 compose 里那串一模一样。四条通道共用一个
- **通道**：默认已经有四张卡，**「适合」和「是隧道」这些开关已经按下表设好了，你要做的
  只是填地址**。地址**要带端口 8964**，末尾斜杠去不去都行；`http://` 明文是允许的
  （清单里开了 `usesCleartextTraffic`）

| 卡片 | 填什么地址 | 适合 api | 适合 media | 是隧道 | 默认状态 |
|---|---|---|---|---|---|
| LAN | `http://<NAS 内网 IP>:8964` | ✅ | ✅ | ✗ | 空地址，开 |
| Tailscale | `http://<100.x.y.z>:8964` | 默认没勾（勾上也行） | ✅ | ✗ | 空地址，开 |
| Tunnel | `https://arphoto.<你的域名>` | ✅ | **✗ 别勾** | **✅ 已勾，别取消** | 空地址，开 |
| DDNS | 没有就不管 | — | — | — | **默认停用** |

地址留空的卡不参与探活，所以只填 LAN 就能先用起来 —— 剩下三张等 §6 / §7 做完再回来填。

**Tunnel 那张卡的「是隧道」一定要勾上。** 不勾的后果不是「少个标记」：客户端据此
判断能不能上传，一旦 media 落到隧道上又没标记，它会放行上传，然后被 Cloudflare 413
（服务端见到 `CF-Ray` 也会挡一道），白等一次。而「适合 media」勾在隧道上更糟 ——
那是把视频往 CDN 上推，§7.1 第三条。

填完点「保存并探活」。每张卡下面会显示 `通 · 23ms`，或者**失败原因的原文**：

- `401` → 令牌错了（不是网络问题，改令牌）
- `404` → 这个地址上没有 photo-ar-server（端口或域名指错了）
- `不通` → 网络到不了（LAN 卡在外网就是这个，正常）

再看「现在走的是」那一段：`api` 和 `media` 分别落在哪条、以及「上传」是否允许。
在家应该两条都是 LAN；4G 下 api 落 Tunnel、media 落 Tailscale。

> 一个例外要知道：**LAN 和 Tailscale 都不通时，media 会兜底落到隧道上**——
> 没有这条兜底，Tailscale 一掉线在外面就完全播不了视频，所以是故意留的
> （`EndpointChoice.select`：没有存活候选声明 media 时取第一条通的）。介意 §7.1
> 第三条的话，出门前用离线缓存，或者干脆把 Tunnel 那张卡的开关关掉 —— 关掉的卡不
> 参与探活，也就不会被选中。

### 11.4 第一次扫

底栏「照片」→ 右下角「扫一扫」→ 给相机权限 → 举起**打印出来的**那张照片，离半米
左右、光别太暗。认出来会在照片上贴视频；ARCore 不可用时是全屏播。

单张也能直接从「照片库」点进详情 →「去扫这张」，省得在库里翻。

**手机上也能入库**，不必回到 SSH：「照片库」右上角的「＋」（关联新照片）→ 浏览 NAS 白
名单目录挑参考图 → 填打印宽度和标题 → 再挑视频。挑的是 **NAS 上的路径**，请求体只有几百
字节（App 里没有任何「上传文件」的动作，设置页「上传」那一行只是个状态显示），所以走哪条
通道都能入。适合零散补几张，上万张还是用 §5.2 的脚本。

唯一要留神的是**在外面隔着隧道入带视频的照片**：入库是同步请求，要等转码转完才回，
而隧道有 125 秒上限（§7.1），大视频会 524。这时**照片其实已经入进去了**（写库是最后一步，
转码完成后才写），你看到的只是超时；再提交一次会得到 409 `already_ingested` 并带上
photoId —— 不会重复入库。带视频的那些还是留到回家走 LAN 更省事。

出门前建议进「设置 → 管理离线缓存」同步一次：把最近扫过的照片和视频存到手机上，
之后没网也能扫。

## 12. 第一次跑通：一张从零开始的清单

每一步做完都有一个能看的结果，看不到就别往下走 —— 后面的步骤看不出前面漏了什么。

| # | 做什么 | 看到什么算成 | 章节 |
|---|---|---|---|
| 1 | 开 SSH，找到 `docker` | `docker compose version` 打出 v2.x | §1 |
| 2 | 拷 `arcoreimg`、训好 `vocab.npz` | 两个文件都在 | §1 |
| 3 | 传代码到 `/share/Container/photo-ar` | `ls` 看到 `docker-compose.yml` | §2.1 |
| 4 | 改 `deploy/config.json` 的 `roots` 和 compose 的挂载 | 冒号两边一样 | §2.3 |
| 5 | `docker compose up -d` | 日志里 `listening on 0.0.0.0:8964` | §3 |
| 6 | 带 token / 不带 token 各 ping 一次 | `{"ok": true...}` / `401` | §3 |
| 7 | 问服务它选了哪个编码器 | `h264_vaapi`（回退了先查 4.1/4.2） | §4 |
| 8 | **手工入一张**（挑一张纹理丰富的） | `201` + `photoId` | §5.1 |
| 9 | 出 APK 装到手机 | 能打开，底栏三个 tab | §11.1 |
| 10 | 设置里填令牌 + LAN 地址，保存并探活 | LAN 显示 `通 · xx ms` | §11.3 |
| 11 | 「照片库」能看到第 8 步那张 | 缩略图和标题 | §11.4 |
| 12 | **打印那张照片，量出画面宽度** | 与入库时填的 `printWidthMm` 一致 | §5.3 |
| 13 | 「扫一扫」举起照片 | 视频贴在照片上播起来 | §11.4 |
| 14 | 批量入库（先 `--limit 5 --dry-run`） | 配对没错，再放量 | §5.2 |
| 15 | 装 Tailscale（NAS + 手机） | 4G 下 `/v1/ping` 回 401 | §6 |
| 16 | 加一条 cloudflared ingress | 外网 curl 到 `{"ok": true}` | §7 |
| 17 | 手机上把 Tailscale / Tunnel 两张卡也填上 | 4G 下 api 落 Tunnel、media 落 Tailscale | §11.3 |
| 18 | 关 WiFi 走 4G 再扫一次 | 还能认出来、还能播 | §11.4 |
| 19 | 备份 `data/` | 有一份压缩包 | §13 |

第 13 步是整条链路第一次真正闭合的地方 —— 在它之前的所有绿灯都只是「零件没坏」。

## 13. 升级、备份、恢复

**改了服务端代码**：按 §2.1A 重传，然后

```bash
docker compose build && docker compose up -d      # 依赖层有缓存，通常几十秒
```

什么时候需要动库（其余情况都不用）：

| 改了什么 | 要做什么 | 不做的表现 |
|---|---|---|
| `vocab.npz` | `reindex --rebuild-words` | **识别率突然掉到底，日志一切正常** |
| 特征提取参数（ORB/描述子） | 全库重新入库 | 同上，且 `check` 也看不出来 |
| 只改了服务端逻辑 / 接口 | 什么都不用 | — |
| 照片原文件被移动或改名 | `verify` 看报告，重新关联 | 详情页 `refStale`，识别仍在（用的是入库时存的特征） |

**改了 Android**：重新 `assembleRelease`，用**同一份 debug keystore** 覆盖安装
（§11.1 那段警告）。

**备份**：值钱的只有 `data/`（每个文件的作用见
[deploy/README.md](../deploy/README.md) 的表）。`imgdb/`、`thumb/`、`playable/` 丢了
只能靠重新入库再生成，所以别只备份 `catalog.db`。SQLite 正在被写的时候拷出来的文件
可能是坏的，停一下再拷最省心：

```bash
cd /share/Container/photo-ar
docker compose stop
sudo tar czf /share/Backup/photo-ar-data-$(date +%F).tar.gz data/    # 属主是 root，要 sudo
docker compose start
```

一万张的量级下 `data/` 的大头是 `playable/`（每条视频最大 16.2MiB）。带宽或空间紧
的话可以只备份 `catalog.db` + `library/` + `imgdb/` + `thumb/`，把 `playable/` 排除
—— 它能从原视频重新转码出来（代价是每条几十秒）。

**恢复到一台新 NAS**：`data/` 拷回去、`vocab.npz` 用**同一份**、照片和视频原文件放回
**同样的路径**（`roots` 是按路径存的，路径变了就要 `verify` 后重新关联）。三份记录
（`slots.json` / `desc.bin` / `words.bin`）条数不齐时服务会**拒绝启动**并让你跑
`reindex` —— 这是故意的，错位一位的后果是「识别命中后播的是别人的视频」。

**换 token**：改 compose 的 `PHOTOAR_TOKEN` → `docker compose up -d` → 手机「设置」里
改成新的。旧 token 立刻失效，客户端表现是所有通道 401（原因会写在卡片下面）。
