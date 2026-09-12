#!/bin/bash
# 织光 (ZhiGuang) - 一键停止 (Git Bash / Linux / macOS)
# 用法: bash stop.sh
# 非交互（CI/管道）：WM_ASSUME_YES=1 bash stop.sh   （跳过 Redis 询问，Redis 保持运行）

cd "$(dirname "$0")"

if [ -z "${LC_ALL:-}" ] && [ -z "${LANG:-}" ]; then
    export LC_ALL=C.UTF-8 2>/dev/null || true
fi
export PYTHONIOENCODING=utf-8

echo "============================================"
echo "  织光 (ZhiGuang) - 停止服务"
echo "============================================"

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "  ERROR: 未找到 python3 / python，无法停止受管服务。"
    exit 1
fi

# 按 PID 文件精确停止全部 Python/Node 服务（含守护复活进程与本项目启动的便携 Redis）
"$PY" launcher.py stop
stop_rc=$?
if [ "$stop_rc" -ne 0 ]; then
    echo "  WARNING: 停止过程有异常（见上方消息）"
fi

# 可选：停止 Redis 容器（仅当确实由本项目 Docker 容器提供；
# 原生 Redis / 便携版 Redis 中，便携版已由 launcher.py stop 处理，
# 系统服务（Memurai/WSL）交由用户自行管理）
if [ "${WM_ASSUME_YES:-0}" = "1" ]; then
    echo "  WM_ASSUME_YES=1：Redis 保持运行"
    echo "全部服务已停止。"
    exit 0
fi

ans=""
if [ -t 0 ]; then
    read -r -p "同时停止 Docker Redis 容器（zhiguan-redis）吗? (y/n): " ans || ans=""
else
    echo "  （非交互环境：跳过询问，Redis 保持运行；如需全停请设 WM_ASSUME_YES=1 ... 见脚本头注释）"
fi

if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    if command -v docker >/dev/null 2>&1 \
       && docker ps --format '{{.Names}}' 2>/dev/null | grep -q zhiguan-redis; then
        docker stop zhiguan-redis 2>/dev/null || true
        echo "  Docker Redis 容器已停止"
    else
        echo "  未发现 Docker 启动的 zhiguan-redis；系统级 Redis 请自行管理（如 systemctl stop redis）"
    fi
else
    echo "  Redis 保持运行"
fi

echo "全部服务已停止。"
