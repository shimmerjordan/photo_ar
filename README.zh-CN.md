# photo-ar

[English](README.md) · **简体中文**

举起手机对着一张**打印出来的**照片，当时那段视频就贴在照片上播起来。

全自托管：识别索引跑在自己的 NAS 上，Android App 负责相机和 AR 跟踪。照片和视频
都不出家门 —— 没有云服务、不用注册、不调第三方识别 API。

```
┌── Android App ──────────┐        ┌── NAS ─────────────────────────┐
│ 相机帧 ───────────────────POST──→ /v1/recognize                   │
│                         │        │   ORB → 词汇树 → 倒排索引      │
│                         │        │   → RANSAC 几何校验            │
│ ARCore 增强图像         │←──id───┤                                │
│ + GLES 视频面片         │        │ /v1/photo/<id>/media           │
│                         │←─mp4───┤   （支持 Range 的吐流）        │
└─────────────────────────┘        └────────────────────────────────┘
```

## 识别是怎么做的

1. **入库** —— 每张照片提 ORB 描述子，拿预训练好的词汇树量化成词序列存进倒排
   索引；同时让 ARCore 的 `arcoreimg` 给一个质量分，并生成手机跟踪要用的 `.imgdb`。
2. **查询** —— 相机帧走同一条管线，倒排索引给出候选，然后每个候选用 RANSAC 单应
   矩阵做几何校验。判定命中需要**内点数 ≥ 40**。
3. **为什么是 40** —— 量出来的，不是猜的。29740 次查询里，真实误识别的内点数最大
   只到 39，而库内真阳性的 5 分位是 69。两个分布几乎不重叠，40 卡在中间，把真实
   误识别率压到 0。复现脚本在 `bench/`。

识别侧有两种情况是**故意拒绝**的：纹理太稀疏、ARCore 跟不住的照片（实测**真实家庭
照片约 65% 属于这类**），以及与库中已有照片构成近重复的 —— 两张都留下的后果是
**两张都永远认不出来**。

## 部署

服务端以容器镜像发布在 GHCR 上。NAS 上只需要四个配置文件加两个你自己的文件，
**不用 clone 源码、不用构建**：

```bash
docker compose pull && docker compose up -d
```

每一步都带「看到什么算成」的完整流程：**[docs/deploy.md](docs/deploy.md)**。

镜像里**故意不含** `arcoreimg`（ARCore 的闭源二进制，不可再分发）和 `vocab.npz`
（用你自己的照片训出来的词汇树），两个都在运行时用 bind mount 送进容器。

## 文档

| 文档 | 里面有什么 |
|---|---|
| [docs/deploy.md](docs/deploy.md) | 部署步骤：开 SSH → compose 起服务 → 确认核显硬编 → 入库 → Tailscale → Cloudflare → 出 APK → 手机配通道。每步都写了看到什么算成 |
| [docs/deploy-details.md](docs/deploy-details.md) | 取舍与数字：为什么媒体绝不走 Cloudflare、为什么用 VAAPI 而不是 QuickSync、批量入库为什么串行、实测基线、排障对照表 |
| [deploy/README.md](deploy/README.md) | 命令速查、例行维护命令、`data/` 下每个文件丢了会怎样 |
| [bench/README.md](bench/README.md) | 上面每个数字背后的测量脚本 |

## 目录结构

```
src/photoar/          识别、入库、转码和 HTTP 服务端（Python）
  server/             /v1/* 接口、路径白名单、媒体 URL 解析
android/
  arview/             ARCore + GLES 扫描视图、通道选择、离线缓存
  app/                Compose 外壳：照片库、详情、浏览、设置
tools/                batch_ingest.py（只用标准库）、cf_edge_probe.py
bench/                Phase 0 的测量脚本
deploy/               config.example.json 与命令速查
docs/                 部署文档
```

## 开发

```bash
pip install -e ".[dev]" && pytest        # 服务端与识别
cd android && ./gradlew test             # arview 与外壳的单测
```

发一版新的服务端镜像是个明确动作，不是推代码的副作用：

```bash
git tag v0.2.0 && git push origin v0.2.0    # 触发构建并推到 GHCR
```

读源码的两点提醒：

- 注释里的 `§N` 指的是一份没有随仓库发布的内部设计文档。每处注释都把真正的理由
  写在了旁边，所以不看那份文档也不缺信息。
- Android 的 release 是**故意用 debug key 签的** —— 这个包只装自己那几台手机，
  不上应用市场。代价是**换机器出的包签名不同，不能覆盖安装**。

## 现状

识别可行性验证、NAS 服务端、ARCore 扫描视图、App 外壳、端侧缓存与离线识别都已经
做完并在跑。一个只读的网页版（让亲友不装 App 也能看到效果）设计好了但还没做。
