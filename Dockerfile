FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 浏览器内核（L2/L3 过 Cloudflare 用）；体积较大，确定不用浏览器可注释掉
RUN python -m camoufox fetch || true
RUN python -m playwright install chromium || true

COPY . .

CMD ["python", "main.py"]
