@echo off
title WeaveMind - ZhiGuang AI System
cd /d "%~dp0"
echo.
echo   ============================================
echo     WeaveMind -- ZhiGuang AI System
echo   ============================================
echo.

:: ---- [1/6] Load config.json ----
echo   [1/6] Config
python -c "import json;c=json.load(open('config.json',encoding='utf-8'));l=c['llm'];assert l.get('api_key') and l.get('base_url') and l.get('model'),'missing llm config';print('Config OK:',l['model'])" 2>nul || (
    echo   ERROR: config.json missing or invalid
    echo   Expected: {"llm":{"api_key":"...","base_url":"...","model":"..."}}
    pause & exit /b 1
)

set "PYTHONIOENCODING=utf-8"

:: ---- [2/6] Redis ----
echo   [2/6] Redis
docker info >nul 2>&1 || (echo   ERROR: Docker required & pause & exit /b 1)
docker ps --filter name=zhiguan --format "{{.Names}}" 2>nul | findstr zhiguan >nul && (
    echo        Already running
) || (
    docker start zhiguan-redis >nul 2>&1 || docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine >nul 2>&1
    echo        Started
)

:: ---- [3/6] Dependencies ----
echo   [3/6] Dependencies
pip install aiosqlite httpx ddgs scikit-learn -q 2>nul
echo        OK

:: ---- [4/6] Start services (PID-managed) ----
echo   [4/6] Starting services...
python launcher.py

:: ---- [5/6] Open browser ----
echo.
echo   ============================================
echo     Ready. http://localhost:5173
echo     Double-click stop.bat to shutdown
echo   ============================================
echo.
start http://localhost:5173
pause
