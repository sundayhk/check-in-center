FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    CAMOUFOX_EXCLUDE_ADDONS=true

WORKDIR /app

# 运行时依赖：Camoufox/Playwright 浏览器内核需要的图形与音频库
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl ca-certificates \
        libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libxcb-dri3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 浏览器内核（L2/L3 过 Cloudflare 用）。
# 注意：这里刻意不加 "|| true" —— 下载失败必须让构建失败，
# 否则镜像能起来但 NodeSeek 每次签到都会因为缺内核而失败（且症状隐蔽）。
RUN python -m camoufox fetch

COPY . .

# 构建期自检：确认浏览器确实可用，避免带病出厂（镜像能起但 NodeSeek 永远失败）
RUN python -c "from core.browser import diagnose; \
actual, verdict, _ = diagnose('auto'); print('[docker] ' + verdict); \
raise SystemExit(0 if actual else 1)"

CMD ["python", "main.py"]
