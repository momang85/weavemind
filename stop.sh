#!/bin/bash
# 织光 (ZhiGuang) - 一键停止 (Git Bash / Linux)
set -e

echo "============================================"
echo "  织光 (ZhiGuang) - 停止服务"
echo "============================================"

cd "$(dirname "$0")"

# 按 PID 文件精确停止全部 Python/Node 服务（含守护复活进程）
python launcher.py stop

# 可选：停止 Redis 容器（仅当确实由本项目 Docker 容器提供；原生 Redis
# （Memurai/WSL 等系统服务）不由本脚本管理，避免误停或误报）
read -p "同时停止 Redis 容器吗? (y/n): " ans
if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    if command -v docker >/dev/null 2>&1 \
       && docker ps --format '{{.Names}}' 2>/dev/null | grep -q zhiguan-redis; then
        docker stop zhiguan-redis 2>/dev/null || true
        echo "Redis 容器已停止"
    else
        echo "未发现 Docker 启动的 zhiguan-redis；原生 Redis 服务请自行管理（如 systemctl stop redis）"
    fi
else
    echo "Redis 保持运行"
fi

echo "全部服务已停止。"
