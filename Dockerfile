# photo-ar-server —— 跑在 QNAP TS-464C2（N5095，x86-64）的 Container Station 上。
#
# 取舍说明：
# * 基础镜像用 python:3.11-slim 而不是 alpine —— opencv 与 numpy 在 alpine 上
#   没有 manylinux wheel，得从源码编译，N5095 上编一次要几十分钟。
# * opencv 装 headless 版：容器里没有 X11，普通 opencv-python 会拖进一整套
#   GUI 依赖（约 200MB）却一个像素也不显示。
# * ffmpeg 从 apt 装。只用它 remux（`-c copy -movflags +faststart`）和偶尔
#   转码，发行版版本足够，不值得自己编。
# * arcoreimg 是 ARCore SDK 里的闭源二进制，不在仓库里（.gitignore 排除）。
#   构建前自己放到 tools/arcoreimg。它只依赖 libstdc++/libc，能直接跑。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libstdc++6 \
      libgl1 \
      libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/photoar

# 依赖单独一层：改代码不必重装 opencv（在 N5095 上装一次约 1 分钟）
RUN pip install --no-cache-dir "numpy>=1.26,<3" "opencv-python-headless>=4.9"

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
RUN chmod +x ./tools/arcoreimg 2>/dev/null || true

# 8964：spec §9.1 的 LAN endpoint 端口
EXPOSE 8964
VOLUME ["/data", "/config"]

# token 不写进镜像。Container Station 直接注入环境变量即可（config.py 里
# PHOTOAR_TOKEN 的优先级高于配置文件）。
ENV PHOTOAR_DATA=/data \
    PATH="/opt/photoar/tools:${PATH}"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; \
t=os.environ.get('PHOTOAR_TOKEN',''); \
r=u.Request('http://127.0.0.1:8964/v1/ping',headers={'Authorization':'Bearer '+t}); \
u.urlopen(r,timeout=4).read()"

ENTRYPOINT ["python", "-m", "photoar.server.httpd", "-c", "/config/config.json"]
CMD ["serve"]
