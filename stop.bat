@echo off
title WeaveMind - Shutdown
echo.
echo   ============================================
echo     Stopping WeaveMind...
echo   ============================================

echo   [1/3] Python services + frontend...
python launcher.py stop
echo        Stopped

echo   [2/3] Cleanup pid file...
echo        Done

echo   [3/3] Redis (optional)...
set /p STOPREDIS="   Stop Redis container too? (y/n): "
if /i "%STOPREDIS%"=="y" (
    docker stop zhiguan-redis >nul 2>&1
    echo        Redis stopped
) else (
    echo        Redis left running
)

echo.
echo   ============================================
echo     All services stopped.
echo   ============================================
echo.
timeout /t 2 >nul
