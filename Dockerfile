FROM python:3.12-slim

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    curl \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# === SPEC-3 §3: 镜像版本指纹注入 ===
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"
LABEL org.opencontainers.image.created="${BUILD_TIME}"
ENV AUTOTEAM_GIT_SHA="${GIT_SHA}"
ENV AUTOTEAM_BUILD_TIME="${BUILD_TIME}"
# === SPEC-3 §3 end ===

# 复制项目文件
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

# 安装 Playwright 浏览器
RUN uv run playwright install chromium && uv run playwright install-deps chromium

# 复制源码
COPY src/ src/
COPY web/ web/

# 数据卷（.env、accounts.json、auths/、state.json、screenshots/）
VOLUME ["/app/data"]

# 启动时将数据目录软链到工作目录
RUN mkdir -p /app/data
ENV DISPLAY=:99

EXPOSE 8787

# 启动脚本
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api"]
