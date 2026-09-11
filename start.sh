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
# 三级探测（与 start.bat 对齐）：本机 6379 已有 Redis → 跳过 Docker；
# 否则走 Docker 容器；两者都不可用时给出原生安装指引
if python -c "import socket,sys
try:
    s=socket.create_connection(('127.0.0.1',6379),2); s.sendall(b'PING\r\n')
    sys.exit(0 if s.recv(64).startswith(b'+PONG') else 1)
except Exception:
    sys.exit(1)" >/dev/null 2>&1; then
    echo "  Local Redis detected at 127.0.0.1:6379, skip Docker"
elif command -v docker >/dev/null 2>&1; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q zhiguan-redis; then
        echo "  Redis already running (docker)"
    else
        docker rm -f zhiguan-redis 2>/dev/null || true
        docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine
        echo "  Redis started (docker)"
    fi
    sleep 2
else
    echo "  ERROR: 未找到 Docker，也未检测到本机 Redis（127.0.0.1:6379）。"
    echo "  无需 Docker 的三种方案（任选其一，装好保持 6379 端口后重跑）："
    echo "    1) Memurai（Redis 兼容，Windows 原生服务）：https://www.memurai.com"
    echo "    2) tporadowski/redis（Redis 5.x Windows 移植版）：GitHub 搜 tporadowski/redis"
    echo "    3) WSL2 / Linux：sudo apt install redis-server && sudo service redis-server start"
    echo "  详见 docs/部署指南.md「无 Docker 的 Redis 方案」"
    exit 1
fi

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
