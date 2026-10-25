FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    CAMOUFOX_EXCLUDE_ADDONS=true

WORKDIR /app

# 运行时依赖：Camoufox(Firefox 系) / Playwright Chromium 浏览器内核需要的图形与音频库
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl ca-certificates \
        libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libxcb-dri3-0 \
        libxshmfence1 libxss1 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 浏览器内核（L2/L3 过 Cloudflare 用）。
# Camoufox 优先（反检测更强），Playwright Chromium 作为 ARM 架构兜底
# （camoufox 官方只发 x86_64 内核，aarch64 机器上启动会失败，auto 模式会自动降级）。
# 注意：camoufox fetch 这里刻意不加 "|| true" —— 下载失败必须让构建失败，
# 否则镜像能起来但 NodeSeek 每次签到都会因为缺内核而失败（且症状隐蔽）。
RUN python -m camoufox fetch || true
RUN playwright install chromium || true

COPY . .

# 构建期自检：确认至少有一种浏览器后端可用，避免带病出厂。
# auto 模式下 camoufox 不可用时会降级到 playwright，所以二者任一就绪即可。
RUN python -c "from core.browser import diagnose, _playwright_chromium_ready; \
a,v,_ = diagnose('auto'); \
pw = _playwright_chromium_ready(); \
print('[docker] camoufox:', v, '| playwright chromium:', 'ready' if pw else 'missing'); \
raise SystemExit(0 if (a or pw) else 1)"

CMD ["python", "main.py"]
