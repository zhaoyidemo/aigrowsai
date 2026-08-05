FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    chromium \
    ffmpeg \
    fonts-noto-cjk \
    python3 \
    python3-venv \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 统一北京时间；Remotion 使用镜像内固定 Chromium，不在运行时下载浏览器。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    REMOTION_BROWSER_EXECUTABLE=/usr/bin/chromium

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && pip install --no-cache-dir -r requirements.txt

# 依赖层只随 package-lock 变化；真实渲染不依赖工作区 node_modules。
COPY video_renderer/package.json video_renderer/package-lock.json ./video_renderer/
RUN npm ci --prefix ./video_renderer --ignore-scripts

COPY . .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
