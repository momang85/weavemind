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
    echo   ERROR: Docker not found. Please install Docker Desktop, then re-run start.bat.
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

:: ---- [3/7] Dependencies ----
echo   [3/7] Dependencies
pip install -r requirements.txt -q
if errorlevel 1 echo   WARNING: some dependencies failed, services may be limited
echo        OK

:: ---- [4/7] Frontend (首次运行自动构建) ----
echo   [4/7] Frontend
if exist frontend\dist\index.html (
    echo        dist exists, skip build
) else (
    where node >nul 2>nul && (
        echo        Building frontend (first run)...
        pushd frontend
        call npm install --no-audit --no-fund
        call npm run build
        popd
    ) || (
        echo   WARNING: Node.js not found; frontend will use built-in fallback page
    )
)

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
