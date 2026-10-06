# 织光 (ZhiGuang) - 应用镜像（前端构建 + 后端服务）
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt requirements-runtime.lock ./
RUN pip install --no-cache-dir -r requirements-runtime.lock

COPY --from=frontend-build /build/dist frontend/dist
COPY *.py ./
COPY workers/ ./workers/
# 运行期导入的本地包：编排器（launcher 拉起的子进程）顶层 import 这两个 pipeline，
# 缺失会导入即崩；其余按能力/校验路径懒加载，跑起来才炸。
COPY charts_pipeline/ ./charts_pipeline/
COPY structured_pipeline/ ./structured_pipeline/
COPY adapters/ ./adapters/
# 金融分析包（Q1）：data_analyzer 的金融路径与会话都 import 它（缺了金融任务必失败）
COPY financial_analysis/ ./financial_analysis/
# 量化域（Y2）：`delivery_pipeline` 会把量化读数收进交付快照（`quant/*`），
# 缺这个包容器里就永远导不出量化附件——`test_deploy_manifest` 的"运行入口会导入但镜像没拷"守卫
# 正是抓到这一点（2026-10-05，提交 29689ef 的 CI 失败）。
COPY quant_research/ ./quant_research/
COPY validators/ ./validators/
COPY skills/ ./skills/
COPY evals/ ./evals/
# 不拷 prompts/：它只装自迭代产出的 overrides.json（已 gitignore），干净检出里目录不存在，
# COPY 会直接失败；override 属于运行时可写状态，prompt_registry 写入时自建目录，缺失即空覆盖。
COPY templates.json config.example.json ./

# 数据目录（通过卷挂载持久化）
RUN mkdir -p /data /app/logs

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    WEAVEMIND_DATA_DIR=/data \
    WEAVEMIND_DB=/data/agents.db \
    REGISTRY_DB=/data/agents.db \
    MEMORY_DIR=/data/chroma_memory \
    METRICS_FILE=/data/metrics.csv \
    METRICS_SUMMARY=/data/metrics_summary.json \
    WEB_PORT=8080 \
    SKIP_DEP_CHECK=1

EXPOSE 8080
CMD ["python", "launcher.py"]
