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
REM A run package carries its own interpreter in runtime\. Put it first: a clean
REM machine may have no Python at all, and a broken or incompatible system Python
REM must not run our services. The marker check rejects a damaged bundle, so the
REM candidate search below still works for source checkouts.
if exist "%~dp0runtime\python.exe" call :try_bundled_runtime
REM Try candidates in order and only accept one that actually RUNS a script.
REM Why verification matters: on Windows "where python" also matches the
REM Microsoft Store placeholder alias, which executes nothing and prints
REM nothing - the novice then only sees a confusing error further down.
REM Order matters: prefer "python" (that is where the user's dependencies were
REM installed), fall back to the official launcher "py -3" when "python" is a
REM placeholder, then "python3". A working candidate is required in every case.
where python >nul 2>&1 && call :try_python "python"
if defined PY goto :python_ready
where py >nul 2>&1 && call :try_python "py -3"
if defined PY goto :python_ready
where python3 >nul 2>&1 && call :try_python "python3"
:python_ready
if not defined PY goto :python_missing
echo        Using: %PY%
if defined WM_BUNDLED_RUNTIME echo        Source: packaged runtime (%~dp0runtime\python.exe)
goto :python_ok

:python_missing
echo   ERROR: no usable Python 3.10+ found.
echo          "python" may be the Microsoft Store placeholder alias: it is
echo          found by "where" but runs nothing.
echo          Fix it with either:
echo            1^) install from https://www.python.org/downloads/ and check
echo               "Add python.exe to PATH" during setup; or
echo            2^) winget install -e --id Python.Python.3.12
echo          Also turn OFF App execution aliases for python.exe in
echo          Settings ^> Apps ^> Advanced app settings ^> App execution aliases.
echo          Then open a NEW terminal and run start.bat again.
call :pause_if_interactive
exit /b 1
:python_ok

REM ---- Config, dependencies, Redis, services and readiness ----
REM One controller owns the whole sequence (launcher.py up): effective config first,
REM then dependency check, then services, then a three-layer readiness verdict.
REM No Docker Desktop auto-start here: a normal launch must not start other software
REM (Redis is prepared by the dependency check, which can fetch a portable build).
%PY% launcher.py up
set "WM_UP_RC=%errorlevel%"
if not %WM_UP_RC%==0 (
    echo.
    echo   NOT READY - see the lines above for the one next action.
    echo   Diagnostics ^(redacted, stays local^): %PY% launcher.py diagnostics diag.txt
    call :pause_if_interactive
    exit /b 1
)

REM Ask the launcher for the real URL instead of hardcoding 8080: WEB_PORT (or the
REM config file) may point elsewhere, and opening the wrong port looks like a dead app.
set "WM_URL="
for /f "usebackq delims=" %%u in (`%PY% launcher.py url 2^>nul`) do set "WM_URL=%%u"
if not defined WM_URL set "WM_URL=http://localhost:8080"

echo.
echo   ============================================
echo     Ready.  %WM_URL%
echo     Stop:   stop.bat
echo     Status: %PY% launcher.py status
echo     Health: %PY% launcher.py readiness
echo   ============================================
echo.
start "" "%WM_URL%"
call :pause_if_interactive
exit /b 0


:try_python
REM %~1 = candidate command (e.g. "py -3"). Accept it only when it really runs
REM Python 3.10+ and actually prints our marker.
REM Checking the marker (not just the exit code) matters: a placeholder alias
REM may exit 0 while running nothing at all.
%~1 -c "import sys;assert sys.version_info>=(3,10);print('WMPYOK')" 2>nul | findstr /c:"WMPYOK" >nul
if not errorlevel 1 set "PY=%~1"
exit /b 0

:try_bundled_runtime
REM Packaged interpreter first on PATH, so "where python" resolves to it. Accepted
REM only after the same marker check as any other candidate.
"%~dp0runtime\python.exe" -c "import sys;assert sys.version_info>=(3,10);print('WMPYOK')" 2>nul | findstr /c:"WMPYOK" >nul
if not errorlevel 1 (
    set "PATH=%~dp0runtime;%PATH%"
    set "WM_BUNDLED_RUNTIME=1"
)
exit /b 0

:check_config
REM Exit code 0 when config.json has a usable llm.api_key / base_url / model.
REM Keep this a single line: the guard test requires balanced quotes per line.
%PY% -c "import json;c=json.load(open('config.json',encoding='utf-8'));l=c['llm'];assert l.get('api_key') and l.get('base_url') and l.get('model'),'missing llm config';print('Config OK:',l['model'])" 2>nul
exit /b %errorlevel%

:pause_if_interactive
if defined WM_NONINTERACTIVE exit /b 0
REM Keep the window open when launched by double-click. findstr (no Unix
REM namesake) is used instead of find, because Git for Windows ships GNU
REM find.exe which shadows the Windows tool in a Git shell. Automation should
REM set WM_NONINTERACTIVE=1 to skip this entirely.
echo %cmdcmdline% | findstr /i /c:"%~nx0" >nul && pause
exit /b 0
