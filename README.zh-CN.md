# photo-ar

[English](README.md) · **简体中文**

举起手机对着一张**打印出来的**照片，一段视频就贴在照片上播起来，跟着你的视角走。
**打开一个网页就行，不用装任何东西。**

照片是**触发条件 + 画布**：它不必是视频里的某一帧，任何一张打印出来的照片都能挂上
任何一段视频。

全自托管：照片、视频、识别索引都在自己的 NAS 上，没有云服务、不调第三方识别 API。
谁能看哪些照片在自带的网页管理台里配。

**识别与贴合全部在浏览器里跑，服务端不在热路径上。** 手机打开页面时下一次识别库
（几十 KB），之后每一帧的特征提取、匹配、单应矩阵、贴合渲染都在本地 —— 服务端只做
三件事：资源索引、传输（识别库与视频）、管理（用户 / 权限 / 配置）。

```
┌── 手机浏览器 ───────────────┐    ┌── NAS：一个容器，一个端口 ─────┐
│ opencv.js(wasm) 提 ORB 特征 │    │  /          网页版（这一半）    │
│ + RANSAC 单应矩阵           │←库─┤  /api/lib   识别库包（ETag）    │
│ + WebGL 贴视频面片          │    │  /admin     网页管理台          │
│   识别一帧都不走网络        │←mp4┤  /v1/*      API、视频吐流       │
└─────────────────────────────┘    └────────────────────────────────┘
```

## 识别是怎么做的

1. **入库** —— 每张照片提局部描述子，量化成词序列存进倒排索引。特征后端可切换：
   **ORB**（默认，已通过出口条件的基线，也是浏览器侧唯一实现的那个）或
   **XFeat**（CVPR 2024 预训练模型，Apache-2.0）。两者的取舍与实测数字见
   [docs/decisions.md](docs/decisions.md)。
2. **查询** —— 相机帧走同一条管线，倒排索引给出候选，然后每个候选用 RANSAC 单应
   矩阵做几何校验。判定命中需要**内点数 ≥ 40**。
3. **为什么是 40** —— 量出来的，不是猜的。29740 次查询里，真实误识别的内点数最大
   只到 39，而库内真阳性的 5 分位是 69。两个分布几乎不重叠，40 卡在中间，把真实
   误识别率压到 0。复现脚本在 `bench/`。（XFeat 后端是另一套分布，阈值 60，
   依据见决策记录。）

识别侧有两种情况是**故意拒绝**的：纹理太稀疏、跟不住的照片（实测**真实家庭照片约
65% 属于这类**），以及与库中已有照片构成近重复的 —— 两张都留下的后果是**两张都
永远认不出来**。

## 部署

**一个容器，一个端口。** 网页版、管理台、API 在同一个端口上按 URI 分：

| 地址 | 是什么 |
|---|---|
| `/` | 宾客扫照片的页面 |
| `/admin` | 网页管理台（用户、授权、参数、照片↔视频映射） |
| `/v1/*` | 后端 API（批量入库脚本打这里） |

NAS 上不用 clone 源码、不用构建：

```bash
cp .env.example .env      # 只有 PHOTOAR_ROOTS 必须看一眼
docker compose up -d
```

不需要手写配置文件、不需要预先训练词表、不需要预置模型 —— 缺的都会在启动日志里
说清楚，并且服务照样起得来。引导管理员的口令会打印在日志里一次。

⚠️ **对宾客开放时前面必须有一层 https**：相机（`getUserMedia`）只在安全上下文里
存在，`http://<局域网IP>` 不算。现有的 Cloudflare Tunnel 加一条 ingress 指到这个
端口就够。只用管理台和 API 的话 http 直连没问题。

每一步都带「看到什么算成」的完整流程：**[docs/deploy.md](docs/deploy.md)**。

镜像里**故意不含** `vocab.npz`（用你自己的照片训出来的词汇树）与 `xfeat.onnx`，
两个都在运行时送进容器 —— 理由与做法见 Dockerfile 里那段注释。

## 文档

**从 [docs/README.md](docs/README.md) 进** —— 那是一页索引，按你要干的事挑一份。

| 文档 | 里面有什么 |
|---|---|
| [docs/deploy.md](docs/deploy.md) | 部署步骤：开 SSH → compose 起服务 → 确认核显硬编 → 入库 → Tailscale → Cloudflare → 手机打开网页。每步都写了看到什么算成 |
| [docs/decisions.md](docs/decisions.md) | **决策记录**：识别特征为什么选 XFeat、为什么放弃全局描述子、阈值怎么量出来的、用户体系与权限为什么这样设计、实测延迟、以及**已知风险与下一步必须做的测量** |
| [docs/deploy-details.md](docs/deploy-details.md) | 取舍与数字：证书为什么同时管着相机和缓存、CDN 该缓存什么、为什么用 VAAPI 而不是 QuickSync、实测基线、排障对照表 |
| [web-front/README.md](web-front/README.md) | 网页版自己那一半：浏览器里怎么跑 ORB、跟踪与贴合、为什么没有 ARCore 的等价物 |
| [deploy/README.md](deploy/README.md) | 命令速查、例行维护命令、`data/` 下每个文件丢了会怎样 |
| [bench/README.md](bench/README.md) | 上面每个数字背后的测量脚本 |

## 目录结构

```
src/photoar/          识别、入库、转码和 HTTP 服务端（Python）
  server/             /v1/* 接口、路径白名单、媒体 URL 解析
  server/webui/       零构建的网页管理台（用户、授权、配置、照片）
web-front/            网页版（原生 ES modules + 零依赖 Node，没有构建步骤）
  public/             页面、识别管线（opencv.js）、WebGL 渲染
  server/             静态资源、/v1 与 /admin 反代、识别库打包、媒体票据
docker/               容器入口（双进程管理器）与健康检查
tools/                batch_ingest.py（只用标准库）、export_models.py、fetch_models.py
bench/                Phase 0 的测量脚本
deploy/               config.example.json、开发机的 compose 覆盖层、运维速查
docs/                 README.md 是索引；部署、取舍与决策记录
```

## 开发

```bash
pip install -e ".[dev]" && pytest        # 服务端与识别
cd web-front && npm test                 # 网页版（零依赖，只用 node --test）
```

网页版还有几套要真浏览器的：`npm run test:browser`（识别管线的黄金用例）、
`npm run test:smoke` 与 `npm run test:pages`（对着一个跑着的容器点一遍每个页面）。

发一版新的服务端镜像是个明确动作，不是推代码的副作用：

```bash
git tag v0.2.0 && git push origin v0.2.0    # 触发构建并推到 GHCR
```

读源码的一点提醒：注释里的 `§N` 指的是一份没有随仓库发布的内部设计文档。每处注释
都把真正的理由写在了旁边，所以不看那份文档也不缺信息。

## 现状

识别可行性验证、NAS 服务端、网页版（识别 + 跟踪 + 贴合 + 像素风界面）、用户与权限、
网页管理台都已经做完并在跑，真机（安卓 / Chromium）上验证过完整链路。一个容器一个
端口的部署形态在开发机上按 NAS 的资源预算（3 核 / 3 GiB）验证过。

**安卓原生客户端 2026-08-05 下线**，精力集中在网页版：不用装、iOS 与鸿蒙同样能用，
而它的识别与贴合质量已经够。那一套代码在 git 历史里（`android/`），决策记录在
[docs/decisions.md](docs/decisions.md)。

还没在目标硬件上验证的：XFeat 后端在 N5095 上的延迟（在更快的机器上按 3 核预算实测
p50 800ms，那台上大概太慢）。它默认关着。见 [docs/decisions.md](docs/decisions.md) §11。
