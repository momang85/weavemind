@echo off
REM ============================================================
REM  WeaveMind (ZhiGuang) - one-click stop for Windows
REM  NOTE: keep this file ASCII-only (see start.bat for why).
REM  Non-interactive use: set WM_ASSUME_YES=1 to skip the prompt.
REM ============================================================
title WeaveMind - Shutdown
setlocal
cd /d "%~dp0"

chcp 65001 >nul 2>&1
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo.
echo   ============================================
echo     Stopping WeaveMind...
echo   ============================================
echo.

echo   [1/3] Python
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo   ERROR: Python not found in PATH; cannot stop managed services.
    exit /b 1
)

echo   [2/3] Services ^(PID-managed; portable Redis started by us is stopped too^)
%PY% launcher.py stop
if errorlevel 1 echo   WARNING: stop reported an issue (see messages above)

echo   [3/3] Redis ^(optional^)
if "%WM_ASSUME_YES%"=="1" (
    echo        WM_ASSUME_YES=1 : leaving Redis running ^(safe default^)
    goto done
)
set "STOPREDIS="
set /p "STOPREDIS=   Stop the Docker Redis container (zhiguan-redis) too? (y/n): "
if /i "%STOPREDIS%"=="y" (
    docker ps --filter name=zhiguan --format "{{.Names}}" 2>nul | findstr zhiguan >nul && (
        docker stop zhiguan-redis >nul 2>&1
        echo        Docker Redis container stopped
    ) || (
        echo        No Docker container named zhiguan-redis
        echo        ^(a system-installed Redis is not managed by this script^)
    )
) else (
    echo        Redis left running
)

:done
echo.
echo   ============================================
echo     All services stopped.
echo   ============================================
echo.
exit /b 0
