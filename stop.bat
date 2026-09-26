@echo off
REM ============================================================
REM  WeaveMind (ZhiGuang) - one-click stop for Windows
REM  NOTE: keep this file ASCII-only (see start.bat for why).
REM  Runs unattended: it never asks questions and never stops a Redis that this
REM  project did not start itself.
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
REM Prefer the interpreter bundled with a run package (a clean machine may have no
REM system Python); the candidate search below stays for source checkouts.
if exist "%~dp0runtime\python.exe" call :try_bundled_runtime
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

REM No question asked here on purpose: a Redis that this project did not start
REM (Docker container, Windows service, another install) is never touched, so
REM there is nothing for the user to decide. `docker stop` is left to Docker users.
echo   [3/3] Other Redis instances left running ^(not started by this project^)

:done
echo.
echo   ============================================
echo     All services stopped.
echo   ============================================
echo.
exit /b 0


:try_bundled_runtime
REM Packaged interpreter first on PATH, so "where python" resolves to it. Accepted
REM only after the same marker check as any other candidate.
"%~dp0runtime\python.exe" -c "import sys;assert sys.version_info>=(3,10);print('WMPYOK')" 2>nul | findstr /c:"WMPYOK" >nul
if not errorlevel 1 set "PATH=%~dp0runtime;%PATH%"
exit /b 0
