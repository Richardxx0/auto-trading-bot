FROM python:3.11-slim

WORKDIR /app

# 运行时系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    screen \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN useradd -m -u 1000 bot

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 项目源码（排除项由 .dockerignore 控制）
COPY . .

# 数据目录
RUN mkdir -p logs && chown -R bot:bot /app

USER bot

# 默认启动交易机器人；可覆盖为 dashboard 或 proxy
CMD ["python", "main.py"]
