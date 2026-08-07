#!/bin/bash
# 织光 (ZhiGuang) - 一键启动脚本 (Git Bash / Linux)
# 用法: bash start.sh

set -e

echo "============================================"
echo "  织光 (ZhiGuang) - 一键启动"
echo "============================================"
echo ""

cd "$(dirname "$0")"

# 配置统一来自 config.json（与 start.bat 一致）
export PYTHONIOENCODING=utf-8

# [1/6] Redis
echo "[1/6] Redis..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q zhiguan-redis; then
    echo "  Redis already running"
else
    docker rm -f zhiguan-redis 2>/dev/null || true
    docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine
    echo "  Redis started"
fi
sleep 2

# [2/6] Dependencies
echo "[2/6] Dependencies..."
pip install -q -r requirements.txt || echo "  WARNING: some dependencies failed, services may be limited"

# [3/6] Frontend (首次运行自动构建)
echo "[3/6] Frontend..."
if [ -f frontend/dist/index.html ]; then
    echo "  dist exists, skip build"
else
    if command -v node >/dev/null 2>&1; then
        echo "  Building frontend (first run)..."
        (cd frontend && npm install --no-audit --no-fund && npm run build) || echo "  WARNING: frontend build failed, will use fallback page"
    else
        echo "  WARNING: Node.js not found; frontend will use built-in fallback page"
    fi
fi

# [4/6] Start all services (PID-managed, 会先清理旧进程)
echo "[4/6] Starting services..."
python launcher.py

# [5/6] Open browser
if [ -f frontend/dist/index.html ]; then
    FRONT_URL="http://localhost:8080"
else
    FRONT_URL="http://localhost:5173"
fi
echo ""
echo "============================================"
echo "  织光系统已启动！"
echo "  Web 前端: ${FRONT_URL}"
echo "  停止: python launcher.py stop"
echo "============================================"
echo ""

# [6/6] 可选：自动打开浏览器
if command -v start &>/dev/null; then
    start "${FRONT_URL}" 2>/dev/null || true
elif command -v open &>/dev/null; then
    open "${FRONT_URL}" 2>/dev/null || true
fi
