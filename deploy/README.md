# 部署 photo-ar-server（Phase 1）

目标环境：QNAP TS-464C2（Celeron N5095，4C/4T，8GB），Container Station。
复用已有的 cloudflared `nas-adan` 与 CloudDrive2 挂载，**不新建任何服务**。

> 第一次部署请按
> [docs/nas-deploy-and-cloudflare.md](../docs/nas-deploy-and-cloudflare.md) 走 ——
> 那份是带验证步骤的完整流程（含**核显硬编怎么确认真的生效**、三条通道的分工、
> Cloudflare 加速哪几招值得做）。这份是命令速查。

## 1. 准备

```bash
# 1) arcoreimg（ARCore SDK for Android 的 tools/arcoreimg/linux/arcoreimg）
#    闭源二进制，不在仓库里。放到 tools/ 下，构建时会一起进镜像。
cp ~/Downloads/arcoreimg tools/arcoreimg && chmod +x tools/arcoreimg

# 2) 词汇树。服务端不训练，必须预先训好拷进 data/。
#    用一批与家里照片风格接近的照片训（几千张即可，与要入库的照片不必相同）。
photoar build --photos /path/to/一批照片 --out /tmp/corpus
cp /tmp/corpus/vocab.npz data/vocab.npz

# 3) 配置
cp deploy/config.example.json deploy/config.json
$EDITOR deploy/config.json          # 改 roots 为容器内路径
export PHOTOAR_TOKEN=$(openssl rand -hex 24)   # 别写进配置文件
```

`vocab.npz` 换了就必须 `photoar-server reindex --rebuild-words`，否则库里存的
词序列还是旧树量化出来的，倒排索引会指向错误的桶——表现是识别率突然掉到底，
而日志里一切正常。

## 2. 起服务

```bash
docker compose build
PHOTOAR_TOKEN=$PHOTOAR_TOKEN docker compose up -d
docker compose logs -f photo-ar-server
```

自检：

```bash
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" http://<NAS 的 LAN IP>:8964/v1/ping
# {"ok": true, "version": "phase1", "serverTime": 1753...}

# 硬编到底有没有生效（软编回退是静默的，只能这样问）
docker compose exec photo-ar-server vainfo 2>&1 | grep -c VAEntrypointEncSlice
docker compose exec photo-ar-server python -c \
  "from photoar import transcode as T; print(T.resolve_encoder('auto'))"
# h264_vaapi = 硬编可用；libx264 = 回退了，查 /dev/dri 有没有透进来
```

## 3. 加一条 cloudflared ingress

**不新建 tunnel，也不改 DNS。** `*.<你的域名>` 的通配符 CNAME 已经指向
现有的 `nas-adan` tunnel，加一条 ingress 规则即可。

编辑 NAS 上 cloudflared 的配置（QNAP 上通常是
`/share/Container/cloudflared/config.yml`），在 `ingress:` 列表里、**404 兜底那条
之前**插入：

```yaml
ingress:
  # ...已有的规则...

  - hostname: arphoto.<你的域名>
    service: http://127.0.0.1:8964
    originRequest:
      # 识别请求要跑 ORB+RANSAC，N5095 上 P95 约 100ms，但入库一张要 1~3s
      # （arcoreimg + 20 次自匹配 + 可能的转码）。默认 30s 够，但改大更省心。
      connectTimeout: 30s
      # 视频走 LAN/Tailscale，隧道上只跑 API 小包，不需要大 buffer
      noHappyEyeballs: true

  # 兜底 404 必须留在最后
  - service: http_status:404
```

然后重启 cloudflared：

```bash
docker restart cloudflared     # 或 Container Station 里重启
```

验证（从任何外网环境）：

```bash
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" https://arphoto.<你的域名>/v1/ping
```

### 隧道上的两条硬限制

| 限制 | 后果 | 应对 |
|---|---|---|
| Cloudflare 免费版请求体 **100MB** 上限 | 上传原片会被 413 掉 | 客户端在识别到走隧道时隐藏上传入口；服务端见到 `CF-Ray` 头的上传请求直接 413 并说明原因 |
| Proxy Read Timeout **125 秒**，非 Enterprise 不可调 | 带视频入库是同步请求，软编慢 preset 必然 524 | 批量入库走 LAN；软编默认 preset 已经是 `veryfast`（见 spec §12.3） |
| 代理视频流**明文违反** CDN 服务条款（不是灰区：非 Enterprise 套餐要通过 CDN 提供视频/大文件必须另买 Stream/Images，Cloudflare 保留停用权且通知不保证提前） | 赔的是整个账号，`nas-adan` 上其它服务一起没 | 媒体只走 LAN/Tailscale。隧道只跑 API 小包（上行约 50KB / 下行 <2KB） |

## 4. 入库

Phase 1 只有 HTTP 入口（Phase 3 的 Flutter 外壳会给它做界面）：

```bash
curl -sS -H "Authorization: Bearer $PHOTOAR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"refPath":"/share/Photo/2019/IMG_0421.jpg",
       "videoPath":"/share/Video/2019/IMG_0421.mov",
       "printWidthMm":152,
       "title":"外婆家院子"}' \
  http://<NAS>:8964/v1/photo
```

`printWidthMm` 是**打印出来的照片实际宽度**，不是像素宽度。跟踪精度直接依赖
它，所以没有默认值。常见规格：6 寸 152mm、5 寸 127mm、4 寸 102mm。

会被拒的几种情况，都带明确原因：

| 状态码 | 原因 | 说明 |
|---|---|---|
| 422 `quality_too_low` | arcoreimg 质量分 < 75 | 大片天空、纯色背景、过曝、严重模糊。换图或给照片加一圈细纹理边框 |
| 409 `already_ingested` | 同一张照片已入库 | photoId 是内容哈希，同内容必然同 id |
| 409 `near_duplicate` | 与库中某张过于相似 | 会列出冲突对象。两张都留着的后果是两张都永远认不出来（0d 实测） |
| 403 `path_denied` | 路径在白名单外 | 响应体不回显被拒的路径 |

## 5. 例行维护

```bash
# 素材完整性（mtime + bytes，只在不一致时才哈希）。不自动改绑，只报告。
docker compose exec photo-ar-server python -m photoar.server.httpd -c /config/config.json verify

# catalog 与识别库是否一致（有不一致时退出码 1）
docker compose exec photo-ar-server python -m photoar.server.httpd -c /config/config.json check

# 重建倒排索引（换了 vocab 加 --rebuild-words）
docker compose exec photo-ar-server python -m photoar.server.httpd -c /config/config.json reindex
```

`/data` 里各文件的作用：

| 文件 | 作用 | 丢了会怎样 |
|---|---|---|
| `catalog.db` | 照片、素材、识别历史 | 全部元数据丢失，要重新入库 |
| `library/desc.bin` | 每张照片的 ORB 描述子 | 同上 |
| `library/words.bin` | 每张照片的词序列 | 可用 `reindex --rebuild-words` 从 desc.bin 重算 |
| `library/index.npz` | 倒排索引 | 可用 `reindex` 重建（秒级） |
| `library/slots.json` | slot ↔ photoId 对照 | **最要紧的一个**。丢了 desc.bin 里的特征就对不上 id 了 |
| `imgdb/`, `thumb/` | ARCore 增强图像库、缩略图 | 要重新入库才能再生成 |
| `playable/` | 转码后的 faststart mp4 | 会重新转码 |

容器以 root 跑（QTS 上的容器惯例如此），`/data` 里的产物属主是 root。想在宿主
机上直接删 `data/` 会遇到 Permission denied，用容器自己删：

```bash
docker run --rm --entrypoint sh -v "$PWD/data:/x" photo-ar-server:phase1 -c 'rm -rf /x/*'
```

`library/` 里三份记录（`slots.json` / `desc.bin` / `words.bin`）的条数必须相等。
入库中途断电会留下条数不齐的目录，服务启动时会直接拒绝并让你跑 `reindex`
——这是故意的：错位一位的后果是「识别命中后播的是别人的视频」，宁可不启动。
