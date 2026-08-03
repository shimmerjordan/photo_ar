# photo-ar-server —— 跑在 QNAP TS-464C2（N5095，x86-64）的 Container Station 上。
#
# 取舍说明：
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
# * arcoreimg 是 ARCore SDK 里的闭源二进制，不在仓库里（.gitignore 排除）。
#   构建前自己放到 tools/arcoreimg。它只依赖 libstdc++/libc，能直接跑。
# * xfeat.onnx **不进镜像**：它是几 MB 的运行时资产，进镜像等于每次发版都重传一遍，
#   也让"换模型"必须重建镜像。由 docker/entrypoint.py 在启动时取到数据卷里
#   （tools/fetch_models.py，幂等 + sha256 校验）。取不到不影响启动，会回退 ORB。
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

# tools/ 整个目录拷进来（而不是单独拷 tools/arcoreimg）：仓库里那个文件是
# gitignore 的，单文件 COPY 在没放二进制的构建上下文里会直接失败，而失败信息
# 指向 Docker 而不是"你忘了放 arcoreimg"。缺了它服务照样起，只有入库会报
# ArcoreimgMissing，信息里写着去哪儿取。
COPY tools/ ./tools/
COPY docker/ ./docker/
RUN chmod +x ./tools/arcoreimg 2>/dev/null || true

# 8964：spec §9.1 的 LAN endpoint 端口
EXPOSE 8964
VOLUME ["/data", "/config"]

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
