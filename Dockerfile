# photo-ar —— 跑在 QNAP TS-464C2（N5095，x86-64）的 Container Station 上。
#
# **一个镜像装两半**：Python 的后端 + Node 的网页版。2026-08-05 合的，之前是两个镜像
# 两个容器两个端口。合并的理由与 URI 怎么分见 docker/entrypoint.py 的模块 docstring；
# 这里只说镜像层面的三件事：
#
# * Node 是**从官方镜像里抠出来的那一个二进制**，不是 apt 装的。Debian trixie 的
#   `nodejs` 包是 20.x，而 web-front 声明 `engines: node >=22`。抠二进制的前提是
#   两个基底同一个发行版（都是 trixie）—— glibc/libstdc++ 版本因此天然对齐，
#   不需要再装任何运行时依赖。换 Python 基底的大版本时要连这一行的 tag 一起换。
# * 网页版**没有构建步骤**：整个前端是原生 ES modules，服务端只用 Node 标准库。
#   所以这里就是拷源码，没有 npm install、没有多阶段产物。
# * `web-front/test/` 与 `tools/` 不进镜像（.dockerignore 里挡着）。它们要在有
#   photo-ar 源码和 Chrome 的地方跑，容器里跑不了也不该跑。
#
# 后端侧的取舍说明：
# * 基础镜像用 python:3.11-slim 而不是 alpine —— opencv 与 numpy 在 alpine 上
#   没有 manylinux wheel，得从源码编译，N5095 上编一次要几十分钟。
# * ⚠️ 基底**不能换成按 `x86-64-v3` 编译的发行版**（比如某些"优化版"镜像，或者自己
#   加 `-march=x86-64-v3` 重编 numpy/onnxruntime）。N5095 是 Jasper Lake：
#   **没有 AVX/AVX2，只到 SSE4.2**。x86-64-v3 的前提就是 AVX2，装上去的表现是
#   进程直接 SIGILL（Illegal instruction）——没有任何一行日志，看起来像"容器起不来"。
#   官方 `python:*-slim` 与 PyPI 的 manylinux wheel 都是 x86-64-v1/v2 基线，安全。
#   onnxruntime 的 CPU EP 会在运行时检测指令集，所以它在 SSE4.2 上能跑，只是比有
#   AVX2 的机器慢 —— 那是慢，不是不能用。
# * opencv 装 headless 版：容器里没有 X11，普通 opencv-python 会拖进一整套
#   GUI 依赖（约 200MB）却一个像素也不显示。
# * ffmpeg 从 apt 装。用它 remux（`-c copy -movflags +faststart`）和转码，
#   发行版版本足够，不值得自己编。
# * intel-media-va-driver（iHD）是 N5095 走硬件编码的运行时。**不要换成
#   QSV 那条路**：trixie 的 ffmpeg 链的是 oneVPL，而它的 GPU runtime
#   （libmfx-gen1.2）只覆盖 Gen12+；N5095 是 Jasper Lake（Gen11），要靠已
#   弃用的 Media SDK，而 trixie 里 intel-media-sdk/libmfx1/libmfxhw64 三个
#   包都不存在（实测 h264_qsv 报 "MFX session: -9"）。iHD 覆盖 Gen8+，
#   h264_vaapi 是这台机器上唯一走得通的硬编。没挂 /dev/dri 时这两个包只是
#   多占约 40MB，服务自动回退 libx264（见 transcode.resolve_encoder）。
#   vainfo 留着是为了部署时能一条命令确认核显透进来了。
# * xfeat.onnx 随镜像分发（models/，4.3MB）：曾经是启动时下载，死于"release 没发 +
#   NAS 连不上 github.com"的叠加。entrypoint 装它时仍过 sha256 校验（_model_source）。

# Node 运行时的来源。**tag 里的 trixie 必须与下面 python 那行的基底一致**（见文件头）。
FROM node:22-trixie-slim AS nodert

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      intel-media-va-driver \
      libva-drm2 \
      vainfo \
      libstdc++6 \
      libgl1 \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/photoar

# 依赖单独一层：改代码不必重装 opencv（在 N5095 上装一次约 1 分钟）。
#
# onnxruntime 的版本上界与 pyproject.toml 里那条约束**必须是同一个**（理由写在那边：
# 让容器和开发机跑同一个 ORT 版本，"本地对、容器里不对"就不可能是 ORT 版本差异导致
# 的）。这里显式写出来而不是靠下面 `pip install --no-deps .` 去解 —— 那条命令带了
# --no-deps，根本不会装依赖。
RUN pip install --no-cache-dir \
      "numpy>=1.26,<3" \
      "opencv-python-headless>=4.9" \
      "onnxruntime>=1.17,<=1.23.2"

COPY pyproject.toml README.md* ./
COPY src/ ./src/
# 装包但不再解依赖：opencv 已经装的是 headless 版，让 pip 按 pyproject 里的
# opencv-python 再装一遍普通版会白占 200MB 并覆盖掉 headless。
RUN pip install --no-cache-dir --no-deps .

# tools/ 整个目录拷进来（batch_ingest.py / fetch_models.py 在里面）。开发机上这个
# 目录里可能还躺着一个 gitignore 的 arcoreimg 二进制（已删的安卓客户端时代遗产，
# decisions §46）—— 被顺带拷进来也没关系，没有代码再引用它。
COPY tools/ ./tools/
COPY docker/ ./docker/
# XFeat 模型（4.3MB，sha256 由 tools/fetch_models.py 钉住）。**打进镜像是刻意的**：
# 曾经的默认是启动时从 GitHub release 下载，它在真实部署里死于两件事的叠加 ——
# 那个 release 一直没发布，而且用户的 NAS 根本连不上 github.com。模型跟着镜像走，
# 启动就不再需要任何外网。entrypoint 仍会过一遍 sha256 校验（见 _model_source）。
COPY models/ ./models/

# ---- 网页版 ----
#
# Node 就一个二进制（约 120MB）。没有 npm、没有 corepack、没有 node_modules ——
# web-front 是零依赖的，那些一个都用不上，少拷一样就少一样要跟着升级的东西。
COPY --from=nodert /usr/local/bin/node /usr/local/bin/node
# 只拷真正要跑的。`server/` 与 `public/` 之外的（test/ tools/ local/）由
# .dockerignore 挡在构建上下文外，这里的路径写死是第二道 —— 两道都要，
# 因为 .dockerignore 改一行就能悄悄把 3.7MB 的测试素材放进来。
COPY web-front/server/ ./web-front/server/
COPY web-front/public/ ./web-front/public/
COPY web-front/package.json ./web-front/

# 版本号注入。CI 填 tag 名或 `sha-<短sha>`（`--build-arg PHOTOAR_VERSION=...`）；不填的话
# 网页版退回 package.json 里那个 + `-dev`，而那正好区分"从镜像跑的"和"本地跑的"。
# 它显示在设置页「关于」那一节，也是那个"连按 7 下进调试模式"的行，而且
# `GET /api/config` 会回它 —— 所以"线上跑的是哪一版"是一句 curl 能问出来的。
#
# ⚠️ **位置有意义：必须在所有 COPY 之后。** 它的值每次提交都变（短 sha），而 `ENV`
# 会让后面所有层失效 —— 放在原来那个位置（`COPY --from=nodert` 之前）等于每次构建都
# 重新拷一遍 120MB 的 node 二进制并重新导出层缓存。放在这里，失效的只有下面几行元数据。
ARG PHOTOAR_VERSION=
ENV PHOTOAR_VERSION=$PHOTOAR_VERSION

# OCI 标准标签。**主要目的不是好看，是让升级后的清理能"只清自己的"。**
#
# 每次 `docker compose pull` 拿到新的 `latest`，上一份镜像会**丢掉 tag 变成
# `<none>`**（1.1GB 一个，随升级次数线性堆积）—— 这是 docker 移动 tag 的固有行为，
# 不是配置错误。清理办法是 `docker image prune`，但**不带过滤的 prune 会清掉这台机器
# 上所有服务的无 tag 镜像**（NAS 上还跑着 CloudDrive2 / Calibre / cloudflared /
# explore_journal 那几个），而它是要写进文档、每次升级都跑的命令 —— 一条会伸手到项目
# 外面的常规命令，出事时最难追。
#
# 有了 source 标签，清理就能精确到这个项目：
#
#   docker image prune -f --filter label=org.opencontainers.image.source=https://github.com/shimmerjordan/photo_ar
#
# 顺带的好处：GHCR 靠这个标签把镜像包关联回仓库（包页面才会出现 Source repository
# 与 README）。仓库名是 `photo_ar`（下划线），与镜像名 `photo-ar-server` 不同 ——
# 写错的话标签在，但两头都对不上，而且不会有任何报错。
#
# 位置与上面的 ARG 同理：在所有 COPY 之后。version 那条每次构建都变，放这里只让
# 这几行元数据失效（LABEL 不产生文件系统层）。
LABEL org.opencontainers.image.source="https://github.com/shimmerjordan/photo_ar" \
      org.opencontainers.image.title="photo-ar-server" \
      org.opencontainers.image.description="扫一张打印出来的照片，视频就贴在照片上播" \
      org.opencontainers.image.version="$PHOTOAR_VERSION"

# 8964：spec §9.1 的 LAN endpoint 端口。合并之后**它是唯一对外的端口** ——
# 网页版、管理台、API 全在这一个上，按 URI 分（见 docker/entrypoint.py）。
# 后端自己退到容器内的 127.0.0.1:8965，那个端口不 EXPOSE、也打不到。
EXPOSE 8964
# ⚠️ 这里**刻意没有** `VOLUME ["/data", "/config"]`（曾经有，被删掉了）。
#
# VOLUME 的语义是：容器创建时，声明过的路径若没被显式挂载，就为它生成一个**匿名卷**。
# 后果在真实 NAS 上量过：用户用环境变量配置（不挂 /config），于是**每次**
# `docker compose up -d` 换镜像重建容器都凭空多一个匿名卷，`docker rm` 还不带走 ——
# 卷列表随升级次数线性变长，全是 64 位哈希名的垃圾。
#
# 它想换来的东西（"数据别丢在容器层里"）本来就是 compose 文件的职责：/data 在
# docker-compose.yml 里是显式 bind mount。忘了挂的人得到的也不是保护，是一个
# 藏在匿名卷里、下次重建就"丢失"的数据目录 —— 比直接写进容器层更难排查。

# token 不写进镜像。Container Station 直接注入环境变量即可（config.py 里
# PHOTOAR_TOKEN 的优先级高于配置文件）。
#
# PHOTOAR_DATA 给默认值，是为了「只设 PHOTOAR_ROOTS 就能起」成立：其余路径全部从它
# 派生（/data/models、/data/library、/data/catalog.db …）。
ENV PHOTOAR_DATA=/data \
    PATH="/opt/photoar/tools:${PATH}"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "/opt/photoar/docker/healthcheck.py"]

# ⚠️ 这里**不再**写 `-c /config/config.json`。以前那样写的后果是：没挂 /config 的
# 部署（一键部署的正常形态）一启动就 FileNotFoundError。现在由 entrypoint 决定 ——
# 文件在就用文件，不在就走环境变量（见 docker/entrypoint.py 与 httpd._load）。
ENTRYPOINT ["python", "/opt/photoar/docker/entrypoint.py"]
CMD ["serve"]
