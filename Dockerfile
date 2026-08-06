# 织光 (ZhiGuang) - 应用镜像（前端构建 + 后端服务）
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=frontend-build /build/dist frontend/dist
COPY *.py ./
COPY workers/ ./workers/

# 数据目录（通过卷挂载持久化）
RUN mkdir -p /data /app/logs

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    REGISTRY_DB=/data/agents.db \
    MEMORY_DIR=/data/chroma_memory \
    METRICS_FILE=/data/metrics.csv \
    METRICS_SUMMARY=/data/metrics_summary.json \
    WEB_PORT=8080

EXPOSE 8080
CMD ["python", "launcher.py"]
