# 常见问题与排障

先按症状查下面这张表，展开的条目在后面。装机步骤见 [deploy.md](deploy.md)，日常使用见 [usage.md](usage.md)，某个数字为什么是这个值见 [deploy-details.md](deploy-details.md)。

| 症状 | 多半是 | 去哪 |
|---|---|---|
| `pull` 报 `denied` / `unauthorized` | GHCR 包默认 private | [↓](#pull-报-denied--unauthorized) |
| 硬编回退成了 `libx264` | 核显没透进容器 | [↓](#硬编回退成了-libx264) |
| 入库被拒（4xx） | 照片本身或路径 | [↓](#入库被拒了) |
| 手机上扫不出来 / 贴不上 | 多半是库里有重复 | [↓](#扫不出来--贴不上) |
| 隧道 502，但容器完全健康 | ingress 的 http/https 与容器对不上 | [↓](#隧道-502但容器是绿的) |
| 发了新版，用户拿到的还是旧的 | Cloudflare 覆盖了源站的 `no-cache` | [↓](#新版发了用户拿到的还是旧的) |
| 边缘缓存没命中（`DYNAMIC` / `BYPASS`） | Cache Rule 没配对 | [↓](#边缘缓存没命中) |
| `docker images` 里一堆 `<none>` | **正常**，`pull` 挪走 tag 留下的 | [↓](#docker-images-里一堆-none) |
| 局域网自测没有证书，相机开不了 | 需要 https 或 `adb reverse` | [↓](#局域网里自测没有隧道也没有真证书) |
| `/v1/ping` 通、识别一直未命中 | 词汇树和库对不上 | `photoar-server check`，必要时 `reindex --rebuild-words` |
| 转码特别慢 | 回退软编了 | 见 [硬编](#硬编回退成了-libx264)；`docker stats` 看 CPU 是否打满 |
| 隧道 524 | 请求超过 125 秒 | 带视频的入库走 LAN。注意照片其实已经入进去了 |
| 隧道 413 `upload_via_tunnel` | 单文件超 95MiB | 那个文件走 LAN 或 Tailscale。**小文件也报 = 镜像太老**，升级 |
| 上传/入库 403 `path_denied`，什么都传不上 | `PHOTOAR_UPLOAD_DIR` 不在任何 `PHOTOAR_ROOTS` 之内 | 新镜像会**自动收编**并打警告（看启动日志有没有「自动收编」）；老镜像要显式加进 `PHOTOAR_ROOTS` |
| 隧道偶发 502 / 失败 | Tunnel Degraded | `cloudflared tunnel info <名>`，连接要落在两个 region |
| 扫描时整台 NAS 发木 | CPU 配额 | compose 里 `cpus: "3.0"` 是故意留一核给 cloudflared / QTS 的，别调到 4 |
| 容器被 OOM kill | 内存 | 实测峰值 1061MB / 3g，还有两倍余量；真 OOM 说明库远超一万张，调 `mem_limit` |
| 服务拒绝启动，说三份记录条数不齐 | 入库中途断电 | 跑 `reindex`。**这个拒绝是故意的** —— 错位一位的后果是「命中后播的是别人的视频」 |
| 手机上所有通道都 401 | token 不一致 | 改了 `.env` 并重启了容器，但手机里还是旧的 |

---

## `pull` 报 `denied` / `unauthorized`

GHCR 上的包默认是 **private**。两条路：

- GitHub 仓库 → 右侧 Packages → `photo-ar-server` → Package settings → Change visibility 改 public（只是镜像公开，仓库不受影响）
- 不想公开就在 NAS 上 `docker login ghcr.io -u <用户名>`，密码用一个有 `read:packages` 的 PAT

## 硬编回退成了 `libx264`

按这个顺序查：

```bash
ls -l /dev/dri/                                    # 要有 renderD128；没有就是 BIOS 关了核显
docker compose exec photo-ar-server vainfo | grep EncSlice   # 要有 VAEntrypointEncSlice
stat -c '%g' /dev/dri/renderD128                   # Permission denied 时把这个 GID
                                                   # 填进 compose 里已备好的 group_add
```

## 入库被拒了

| 状态码 | 原因 | 怎么办 |
|---|---|---|
| 422 `no_features` | 提不出任何特征点 | 换纹理丰富的；大片天空、纯色墙面基本提不出来 |
| 409 `already_ingested` | 同内容已入库 | photoId 是内容哈希，同内容必然同 id |
| 409 `near_duplicate` | 与库中某张过于相似 | 会列出冲突对象。**两张都留着的后果是两张都永远认不出来** |
| 403 `path_denied` | 路径在 `roots` 白名单外 | 响应体不回显被拒路径（免得变成探测工具），看 NAS 日志 |
| 422 `bad_print_width` | `printWidthMm` 不合法 | 这个字段是可选的，网页版根本不看它，可以不填 |

`path_denied` 最常见的成因是 `/share/Photo` 和 `/share/CACHEDEV1_DATA/Photo` 混用（前者是符号链接）。挂载时冒号两边写成一样、白名单写 `/share/Photo`，**别用 `find` 出来的 CACHEDEV 路径提交**。批量脚本的 `--map` 就是给已经不一致的情况准备的。

## 扫不出来 / 贴不上

**先看这一条：库里有没有同一张照片的两份。**

同一内容入库两次的话，识别时两份会互相触发比值检验判 `ambiguous` —— **两份都永久扫不出来**，而现象和「识别器坏了」一模一样。

判断：管理台「媒体」页看有没有两条缩略图长得一样；或者「管理 → 识别历史」里未命中那几条有没有标红字「库里有近重复，两张互相挤掉了」。

处理：在「媒体」页那一行点**删除**，留一张（参考图和视频文件都不动，NAS 上什么都不会少）。删完立刻能扫出来。

> 入库闸门现在会拦住新的重复（409 并列出是哪一张），所以这件事只会发生在 2026-08-03 之前入的库上。

排除重复之后，界面上那几句提示都对应真实的干扰，照做就行：

| 提示 | 为什么 |
|---|---|
| 手指别压住边缘 | 一只手拿照片时手指常压在边上，特征匹配掉一大截 |
| 避开反光 | 覆膜或玻璃相框的高光会把那一块特征全盖掉 |
| 拿稳一点 | 运动模糊直接毁掉 ORB 的角点 |
| 贴合可能偏了，正对照片重新识别 | 大角度下跟丢了原图，正过来会自动重新锁定 |

**要查为什么贴不上**：设置 → 关于 → **连按版本号 7 下**打开调试模式，再扫一次 —— 屏幕上会出现一块滚动日志，从抽帧、识别耗时、内点数、四角坐标一直到视频的 `readyState`。截个图就够排查。

## 隧道 502，但容器是绿的

2026-08-06 真踩到的。**所有常规检查都告诉你一切正常**：`docker ps` 是 `Up (healthy)`、日志干干净净、healthcheck 也过（它在容器**内部**探，走回环，根本不经过隧道）。

根因：容器配了 `WEBFRONT_TLS_CERT` / `WEBFRONT_TLS_KEY`，于是网页版在 8964 上说的是 **TLS**；而 ingress 写的是 `service: http://localhost:8964`。cloudflared 拿明文去打 TLS 端口，握手阶段被丢掉 → 502。

**一句话确诊**（在 NAS 上）：

```bash
curl -s  -o /dev/null -w 'http  %{http_code}\n'  http://127.0.0.1:8964/healthz   # 挂掉 / 000
curl -sk -o /dev/null -w 'https %{http_code}\n' https://127.0.0.1:8964/healthz   # 200
```

`http` 那条报 `curl: (52) Empty reply from server` 而 `https` 那条 200，就是它。两条修法**二选一并保持两侧一致**：

| | 容器侧 | ingress |
|---|---|---|
| **走隧道（推荐）** | 不设 `WEBFRONT_TLS_*` | `service: http://localhost:8964` |
| 要保住局域网直连开相机 | 设 `WEBFRONT_TLS_*` | `service: https://localhost:8964` + `originRequest: {noTLSVerify: true}` |

推荐第一条：走隧道时证书由 Cloudflare 提供，容器再包一层对公网访问**零收益**（浏览器看到的是 Cloudflare 的证书）。代价是局域网直连 `http://192.168.x.x:8964` 不是安全上下文、**相机用不了**，只影响「断网退回局域网给宾客扫」这个备份方案。

⚠️ 改 `WEBFRONT_TLS_*` 之后要 **`docker compose up -d`**，不能 `restart` —— 后者不重新读 `.env`。

## 新版发了，用户拿到的还是旧的

**症状**：容器日志里版本号是新的，`curl` 拿到的是新的，边缘节点上也是新的 —— 只有浏览器里是旧的。表现是接口报 400、改的样式没生效。**排查会全部指向别处**，因为三个能查的地方都正常。

**原因**：Cloudflare 的 **Browser Cache TTL** 会**覆盖源站的 `Cache-Control`**。

```
$ curl -sI https://<域名>/api.js | grep -iE 'cache-control|cf-cache-status'
cache-control: max-age=14400      ← 源站发的是 no-cache，被换掉了
```

`max-age=14400` = 四小时内浏览器**连问都不会问**。普通刷新（F5）也不会，只有硬刷新才绕得过去 —— 而那件事没人知道要做。HTML 不受影响（Cloudflare 默认不缓存 HTML），所以页面框架是新的、里面的 JS 是旧的，这个组合让症状更难认。

**修**：Cloudflare 控制台 → Caching → Configuration → Browser Cache TTL → **`Respect Existing Headers`**。

源站发的已经是正确的头（`no-cache` + ETag：每次问一句，没变就 304，没有下载成本）。改完 `curl -I` 确认那行变成 `no-cache`。`.wasm` / `.woff2` 的 URL 带内容哈希、源站给的是 `immutable`，Respect 之后仍然长缓存，不受影响。

> 应用自己也会说：`web-front/public/staleguard.js` 比对「跟 JS 包一起被缓存的版本号」与「`/api/config` 里服务端当下的版本号」，不等就在第一屏停住并给一个能自救的按钮。但**它不能替代把 CDN 配对** —— 加这个探测的那一版自己救不了自己，从下一次部署开始才生效。

## 边缘缓存没命中

`cf-cache-status` 拿到 `DYNAMIC` 说明 Cache Rule 没命中（检查表达式里的 host 和路径前缀），`BYPASS` 说明被别的规则或 cookie 挡掉了。规则怎么写见 [deploy.md 第 6c 步](deploy.md#6c-让引擎停在-cloudflare-边缘)，为什么只缓存那两个路径见 [deploy-details.md](deploy-details.md#cdn该缓存什么绝对不该缓存什么)。

## `docker images` 里一堆 `<none>`

**正常，不是故障。** `pull` 拿到新的 `latest` 时 docker 把 tag 挪到新镜像上，上一份就丢掉全部 tag 变成 `<none>`（1.1GB 一个）。升级完跟一句：

```bash
docker image prune -f --filter label=org.opencontainers.image.source=https://github.com/shimmerjordan/photo_ar
```

**别用不带 `--filter` 的版本** —— 那会连这台机器上别的服务的无 tag 镜像一起清。完整说明见 [deploy-details.md](deploy-details.md#升级备份恢复)。

## 局域网里自测（没有隧道也没有真证书）

两条路。**最省事的是完全不要证书**，调试时首选：

```bash
adb reverse tcp:8964 tcp:8964      # 手机上的 8964 转到这台机器
# 手机浏览器打开 http://localhost:8964
```

`localhost` 按规范就是安全上下文，相机能开，也没有 TLS 那层要绕。

要给别人用就得自签证书（`gen-dev-cert.sh` 会把本机所有 IPv4 和 tailnet 域名写进 SAN —— 少了 SAN 现代浏览器连「继续访问」都不给）：

```bash
cd web-front && ./tools/gen-dev-cert.sh
```

然后照 [deploy.md 第 7 步](deploy.md#7-发给宾客)填三个 `WEBFRONT_*`。手机第一次打开有证书警告，点「高级 → 继续」；嫌烦就让手机打开 `https://<地址>:8964/ca.crt` 装一次本地 CA。

> 自签证书下**视频照样能播**：平台媒体组件有自己的 TLS 栈、不认自签证书，但网页版走的是 MediaSource（页面自己 fetch 再喂解码器），完全不经过那个组件。这是默认路径，不需要配置。见 [mp4stream.js](../web-front/public/mp4stream.js) 顶部那张表。
