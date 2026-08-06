#!/bin/bash
# 织光 (ZhiGuang) - 一键停止 (Git Bash / Linux)
set -e

echo "============================================"
echo "  织光 (ZhiGuang) - 停止服务"
echo "============================================"

cd "$(dirname "$0")"

# 按 PID 文件精确停止全部 Python/Node 服务（含守护复活进程）
python launcher.py stop

# 可选：停止 Redis 容器
read -p "同时停止 Redis 容器吗? (y/n): " ans
if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    docker stop zhiguan-redis 2>/dev/null || true
    echo "Redis 已停止"
else
    echo "Redis 保持运行"
fi

echo "全部服务已停止。"
