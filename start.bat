@echo off
title WeaveMind - ZhiGuang AI System
setlocal
cd /d "%~dp0"
echo.
echo   ============================================
echo     WeaveMind -- ZhiGuang AI System
echo   ============================================
echo.

:: ---- [1/7] Load config.json ----
echo   [1/7] Config
python -c "import json;c=json.load(open('config.json',encoding='utf-8'));l=c['llm'];assert l.get('api_key') and l.get('base_url') and l.get('model'),'missing llm config';print('Config OK:',l['model'])" 2>nul || (
    echo   ERROR: config.json missing or invalid
    echo   First run: copy config.example.json config.json, then fill in your LLM API key.
    echo   Expected: {"llm":{"api_key":"...","base_url":"...","model":"..."}}
    pause & exit /b 1
)

set "PYTHONIOENCODING=utf-8"

:: ---- [2/7] Redis ----
echo   [2/7] Redis
:: 三级探测：本地 6379 已有 Redis（Memurai/redis-windows/WSL）→ 跳过 Docker；
:: 否则走 Docker 容器；两者都不可用时给出下载指引
python -c "import socket;s=socket.create_connection(('127.0.0.1',6379),2);s.sendall(b'PING\r\n');raise SystemExit(0 if s.recv(64).startswith(b'+PONG') else 1)" >nul 2>&1
if not errorlevel 1 (
    echo        Local Redis detected at 127.0.0.1:6379, skip Docker
    goto redis_ok
)
docker info >nul 2>&1
if errorlevel 1 goto redis_start_docker
goto redis_check_container

:redis_start_docker
echo        Docker engine not running, starting Docker Desktop...
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
) else if exist "%LOCALAPPDATA%\Docker\Docker Desktop.exe" (
    start "" "%LOCALAPPDATA%\Docker\Docker Desktop.exe"
) else (
    echo   ERROR: 未找到 Docker，也未检测到本机 Redis 服务。
    echo   无需 Docker 的三种方案（任选其一，装好保持 6379 端口即可重跑 start.bat^）：
    echo     1^) Memurai（Redis 兼容的 Windows 服务，开发者版免费^）：https://www.memurai.com
    echo     2^) tporadowski/redis（Redis 5.x Windows 移植版^）：GitHub 搜 tporadowski/redis 下载解压，
    echo        双击 redis-server.exe 后保持窗口；或 redis-server.exe --service-install 注册服务
    echo     3^) WSL2：wsl --install 后执行 sudo apt install redis-server ^&^& sudo service redis-server start
    echo   详见 docs/部署指南.md「无 Docker 的 Redis 方案」。
    pause & exit /b 1
)
echo        Waiting for Docker engine (up to 90s)...
set /a _redis_wait=0
:redis_wait_loop
ping -n 6 127.0.0.1 >nul
docker info >nul 2>&1
if not errorlevel 1 goto redis_check_container
set /a _redis_wait+=5
if %_redis_wait% LSS 90 goto redis_wait_loop
echo   ERROR: Docker Desktop did not become ready in 90 seconds.
echo   Please start Docker Desktop manually, then re-run start.bat.
pause & exit /b 1

:redis_check_container
docker ps --filter name=zhiguan --format "{{.Names}}" 2>nul | findstr zhiguan >nul && (
    echo        Already running
) || (
    docker start zhiguan-redis >nul 2>&1 || docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine >nul 2>&1
    echo        Started
)

:redis_ok

:: ---- [3/7] Dependencies (自检 + 自动补齐：缺包自动装、Redis 缺失自动获取) ----
echo   [3/7] Dependencies
python dep_check.py --fix
if errorlevel 1 (
    echo   ERROR: 必需依赖未就绪（见上方报告）。
    echo   可手动处理：pip install -r requirements.txt；Redis 安装见 docs/部署指南.md
    pause & exit /b 1
)

:: ---- [4/7] Frontend (首次运行自动构建) ----
echo   [4/7] Frontend
if exist frontend\dist\index.html (
    echo        dist exists, skip build
    goto frontend_done
)
where node >nul 2>nul
if errorlevel 1 (
    echo   WARNING: Node.js not found; frontend will use built-in fallback page
    goto frontend_done
)
echo        Building frontend (first run)...
pushd frontend
call npm install --no-audit --no-fund
call npm run build
popd
:frontend_done

:: ---- [5/7] Start services (PID-managed) ----
echo   [5/7] Starting services...
python launcher.py

:: ---- [6/7] Open browser ----
echo.
if exist frontend\dist\index.html (
    set "FRONT_URL=http://localhost:8080"
) else (
    set "FRONT_URL=http://localhost:5173"
)
echo   ============================================
echo     Ready. %FRONT_URL%
echo     Double-click stop.bat to shutdown
echo   ============================================
echo.
start "" "%FRONT_URL%"
pause
