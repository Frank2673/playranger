# ============================================================
#  PlayRanger — 魔搭社区（ModelScope）Docker 部署
# ============================================================
#
#  魔搭 Docker 形态硬性要求：
#    - 必须监听 0.0.0.0:7860（不是 8080）
#    - 不要依赖 Authorization 请求头（会被平台拦截）
#    - 镜像尽量小，首次启动要能自检
#
#  构建：
#    docker build -t playranger:0.3.0 .
#  本地运行：
#    docker run -p 7860:7860 -v $(pwd)/output:/app/output playranger:0.3.0
#  魔搭：把本仓库关联为 Docker 空间，端口填 7860
# ============================================================

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOST=0.0.0.0 \
    PORT=7860

WORKDIR /app

# 系统依赖（截图/中文字体）
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-cjk \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用镜像层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir pillow

# 再复制源码
COPY pyproject.toml README.md LICENSE /app/
COPY playranger/ /app/playranger/
COPY examples/ /app/examples/

RUN pip install --no-cache-dir -e /app

# 输出目录（任务轨迹 / 截图 / 报告）
RUN mkdir -p /app/output
VOLUME ["/app/output"]

# 健康检查：确认 API 起来
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/health || curl -fsS http://127.0.0.1:7860/ || exit 1

EXPOSE 7860

# 启动 Web 控制台 + API（端口固定 7860）
CMD ["playranger", "server", "--host", "0.0.0.0", "--port", "7860"]
