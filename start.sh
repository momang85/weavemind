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

# [1/5] Redis
echo "[1/5] Redis..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q zhiguan-redis; then
    echo "  Redis already running"
else
    docker rm -f zhiguan-redis 2>/dev/null || true
    docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine
    echo "  Redis started"
fi
sleep 2

# [2/5] Dependencies
echo "[2/5] Dependencies..."
pip install -q aiosqlite httpx ddgs scikit-learn || true

# [3/5] Start all services (PID-managed, 会先清理旧进程)
echo "[3/5] Starting services..."
python launcher.py

# [4/5] Open browser
echo ""
echo "============================================"
echo "  织光系统已启动！"
echo "  Web 前端: http://localhost:5173"
echo "  停止: python launcher.py stop"
echo "============================================"
echo ""

# [5/5] 可选：自动打开浏览器
if command -v start &>/dev/null; then
    start "http://localhost:5173" 2>/dev/null || true
elif command -v open &>/dev/null; then
    open "http://localhost:5173" 2>/dev/null || true
fi
