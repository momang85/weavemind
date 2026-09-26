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

# ---- 统一启动控制器（与 Windows 侧同一套：launcher.py up） ----
# 检查运行包 → 依赖 → 配置 → 服务 → 三层就绪；失败只给一个可执行的下一步。
# 这里不再各自维护 config 闸门 / Redis 探测 / 依赖安装三套逻辑。
"$PY" launcher.py up || {
    echo ""
    echo "  未就绪——请按上面给出的下一步处理。"
    echo "  脱敏诊断（仅本地）：$PY launcher.py diagnostics diag.txt"
    exit 1
}

URL="$("$PY" launcher.py url 2>/dev/null || echo http://localhost:8080)"
echo ""
echo "============================================"
echo "  织光系统已启动"
echo "  Web 前端: ${URL}"
echo "  停止: $PY launcher.py stop   （或 bash stop.sh）"
echo "  状态: $PY launcher.py status     健康: $PY launcher.py readiness"
echo "============================================"
echo ""

# 可选：自动打开浏览器（macOS: open / Linux: xdg-open / WSL: cmd.exe start）
if [ "${WM_NONINTERACTIVE:-0}" != "1" ]; then
    if command -v open >/dev/null 2>&1; then
        open "${URL}" 2>/dev/null || true
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "${URL}" 2>/dev/null || true
    elif command -v cmd.exe >/dev/null 2>&1; then
        cmd.exe /c start "" "${URL}" 2>/dev/null || true
    else
        echo "  （未找到浏览器打开命令，请手动访问 ${URL}）"
    fi
fi
