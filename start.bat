@echo off
REM ============================================================
REM  WeaveMind (ZhiGuang) - one-click start for Windows
REM  NOTE: keep this file ASCII-only. Non-ASCII text in a .bat is
REM  decoded with the active console code page (GBK by default)
REM  and shows as mojibake. Chinese messages are printed by the
REM  Python side (launcher.py / dep_check.py), which adapts its
REM  own encoding. Set WM_PLAIN_TEXT=1 for English-only output.
REM ============================================================
title WeaveMind - ZhiGuang AI System
setlocal
cd /d "%~dp0"

REM Switch console to UTF-8 so Python's Chinese output renders correctly
chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo.
echo   ============================================
echo     WeaveMind -- ZhiGuang AI System
echo   ============================================
echo.

REM ---- [1/6] Locate a Python interpreter ----
echo   [1/6] Python
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo   ERROR: Python not found in PATH.
    echo   Install Python 3.10-3.14 from https://www.python.org/downloads/
    echo   and enable "Add python.exe to PATH" during setup.
    call :pause_if_interactive
    exit /b 1
)
echo        Using: %PY%

REM ---- [2/6] Config ----
echo   [2/6] Config
%PY% -c "import json;c=json.load(open('config.json',encoding='utf-8'));l=c['llm'];assert l.get('api_key') and l.get('base_url') and l.get('model'),'missing llm config';print('Config OK:',l['model'])" 2>nul
if errorlevel 1 (
    if not exist config.json (
        echo   ERROR: config.json not found. First run:
        echo          copy config.example.json config.json
        echo   then fill in llm.api_key / base_url / model.
    ) else (
        echo   ERROR: config.json exists but llm.api_key / base_url / model is incomplete.
    )
    call :pause_if_interactive
    exit /b 1
)

REM ---- [3/6] Redis ----
echo   [3/6] Redis
REM Three-stage probe: local 6379 first (skip Docker), then Docker, else guide.
REM NOTE: keep the probe on ONE line. cmd.exe cannot pass a multi-line quoted
REM argument to "python -c"; the following lines would be executed as commands
REM ("'try:' is not recognized ...") and the script would abort here.
%PY% -c "import socket,sys; s=socket.create_connection(('127.0.0.1',6379),2); s.sendall(b'PING\r\n'); sys.exit(0 if s.recv(64).startswith(b'+PONG') else 1)" >nul 2>&1
if not errorlevel 1 (
    echo        Local Redis detected at 127.0.0.1:6379, skip Docker
    goto redis_ok
)
docker info >nul 2>&1
if errorlevel 1 goto redis_no_docker
docker ps --filter name=zhiguan --format "{{.Names}}" 2>nul | findstr zhiguan >nul && (
    echo        Docker Redis already running
) || (
    docker start zhiguan-redis >nul 2>&1 || docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine >nul 2>&1
    echo        Docker Redis started
)
goto redis_ok

:redis_no_docker
echo        No local Redis and Docker engine is not running.
echo        Starting Docker Desktop ^(waiting up to 90s^)...
if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
) else if exist "%LOCALAPPDATA%\Docker\Docker Desktop.exe" (
    start "" "%LOCALAPPDATA%\Docker\Docker Desktop.exe"
) else (
    echo   NOTE: Docker Desktop not found. Redis is the only hard dependency;
    echo         Python-side dependency check will try to fetch a portable
    echo         Redis automatically ^(see step 4^).
    goto redis_ok
)
set /a _redis_wait=0
:redis_wait_loop
ping -n 6 127.0.0.1 >nul
docker info >nul 2>&1
if not errorlevel 1 goto redis_ok
set /a _redis_wait+=5
if %_redis_wait% LSS 90 goto redis_wait_loop
echo   WARNING: Docker Desktop not ready in 90s; continuing ^(step 4 will try
echo            the local/portable Redis path^).
goto redis_ok

:redis_ok

REM ---- [4/6] Dependencies (self-check + auto-fetch missing pieces) ----
echo   [4/6] Dependencies
%PY% dep_check.py --fix
if errorlevel 1 (
    echo   ERROR: required dependencies are still missing ^(see report above^).
    echo   Manual fallback: %PY% -m pip install -r requirements.txt
    echo   Redis install guide: docs\  ^(deployment guide, section 5.1^)
    call :pause_if_interactive
    exit /b 1
)

REM ---- [5/6] Frontend ----
echo   [5/6] Frontend
if exist frontend\dist\index.html (
    echo        dist exists, skip build
    goto frontend_done
)
where node >nul 2>nul
if errorlevel 1 (
    echo        Node.js not found; web UI will show a built-in status page
    goto frontend_done
)
echo        Building frontend ^(first run^)...
pushd frontend
call npm install --no-audit --no-fund
call npm run build
popd
:frontend_done

REM ---- [6/6] Start services (PID-managed) ----
echo   [6/6] Starting services...
%PY% launcher.py
if errorlevel 1 (
    echo   ERROR: services failed to start ^(see log above^).
    call :pause_if_interactive
    exit /b 1
)

echo.
echo   ============================================
echo     Ready.  http://localhost:8080
echo     Stop:   stop.bat
echo     Status: %PY% launcher.py status
echo   ============================================
echo.
start "" "http://localhost:8080"
call :pause_if_interactive
exit /b 0

:pause_if_interactive
if defined WM_NONINTERACTIVE exit /b 0
REM Keep the window open when launched by double-click. findstr (no Unix
REM namesake) is used instead of find, because Git for Windows ships GNU
REM find.exe which shadows the Windows tool in a Git shell. Automation should
REM set WM_NONINTERACTIVE=1 to skip this entirely.
echo %cmdcmdline% | findstr /i /c:"%~nx0" >nul && pause
exit /b 0
