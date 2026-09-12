#!/bin/bash
# 织光 (ZhiGuang) - 一键启动脚本 (Git Bash / Linux / macOS)
# 用法: bash start.sh
# 非交互（CI/管道）：WM_NONINTERACTIVE=1 bash start.sh

set -e

cd "$(dirname "$0")"

# 控制台编码：UTF-8 是 *nix 默认；仅在未设置 locale 时兜底，避免覆盖用户环境
if [ -z "${LC_ALL:-}" ] && [ -z "${LANG:-}" ]; then
    export LC_ALL=C.UTF-8 2>/dev/null || true
fi
export PYTHONIOENCODING=utf-8     # 与 Windows 侧保持一致

echo "============================================"
echo "  织光 (ZhiGuang) - 一键启动"
echo "============================================"
echo ""

# ---- 解释器探测：python3 优先，回退 python ----
PY=""
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "  ERROR: 未找到 python3 / python，请先安装 Python 3.10–3.14。"
    exit 1
fi

# [1/6] Redis
echo "[1/6] Redis..."
# 三级探测（与 start.bat 对齐）：本机 6379 已有 Redis → 跳过 Docker；
# 否则走 Docker 容器；两者都不可用时给出原生安装指引
if "$PY" -c "import socket,sys
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
    echo "  提示：未找到 Docker 也未检测到本机 Redis，交由第 2 步依赖自检"
    echo "        尝试自动获取（便携版 / 系统 redis-server）。"
fi

# [2/6] Dependencies（自检 + 自动补齐：缺包自动装、Redis 缺失自动获取）
echo "[2/6] Dependencies..."
if ! "$PY" dep_check.py --fix; then
    echo "  ERROR: 必需依赖未就绪（见上方报告）。"
    echo "  可手动处理：$PY -m pip install -r requirements.txt"
    echo "  Redis 安装见 docs/ 部署指南（5.1 节）"
    exit 1
fi

# [3/6] Frontend (首次运行自动构建；仓库已带构建产物时可跳过)
echo "[3/6] Frontend..."
if [ -f frontend/dist/index.html ]; then
    echo "  dist exists, skip build"
else
    if command -v node >/dev/null 2>&1; then
        echo "  Building frontend (first run)..."
        (cd frontend && npm install --no-audit --no-fund && npm run build) || echo "  WARNING: frontend build failed, will use fallback page"
    else
        echo "  WARNING: Node.js not found; web UI will show a built-in status page"
    fi
fi

# [4/6] Start all services (PID-managed, 会先清理旧进程)
echo "[4/6] Starting services..."
"$PY" launcher.py

# [5/6] Frontend URL
if [ -f frontend/dist/index.html ]; then
    FRONT_URL="http://localhost:8080"
else
    FRONT_URL="http://localhost:8080"
fi
echo ""
echo "============================================"
echo "  织光系统已启动！"
echo "  Web 前端: ${FRONT_URL}"
echo "  停止: $PY launcher.py stop   （或 bash stop.sh）"
echo "  状态: $PY launcher.py status"
echo "============================================"
echo ""

# [6/6] 可选：自动打开浏览器（macOS: open / Linux: xdg-open / WSL: cmd.exe start）
if [ "${WM_NONINTERACTIVE:-0}" != "1" ]; then
    if command -v open >/dev/null 2>&1; then
        open "${FRONT_URL}" 2>/dev/null || true
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "${FRONT_URL}" 2>/dev/null || true
    elif command -v cmd.exe >/dev/null 2>&1; then
        cmd.exe /c start "" "${FRONT_URL}" 2>/dev/null || true
    else
        echo "  （未找到浏览器打开命令，请手动访问 ${FRONT_URL}）"
    fi
fi
