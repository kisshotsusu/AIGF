@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Home Agent launcher (robust)
rem  1) Try to self-elevate so the GPU model services it spawns share the
rem     same token and "Release GPU for games" can kill them.
rem  2) If elevation is declined/unavailable, fall back to a normal launch
rem     so the GUI still opens; never let an admin check block startup.
rem  3) Detect an existing single-instance lock before starting.
rem ============================================================

net session >nul 2>&1
if %errorlevel% equ 0 goto :run

rem ---- not admin: request elevation, fall back on failure ----
echo Requesting administrator privileges to run Home Agent with highest rights...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs -ErrorAction Stop } catch { exit 5 }"
if %errorlevel% equ 5 (
    echo.
    echo [WARN] Administrator elevation was declined or unavailable.
    echo        Falling back to a normal, non-admin launch so the UI still opens.
    echo        If model services were started as admin earlier, the release
    echo        GPU action may not be able to stop them.
    echo        Use the admin-mode launcher later to run with full rights.
    echo.
    timeout /t 2 /nobreak >nul
    goto :run
)
rem The elevated re-launch re-enters this script with net session==0 and goes to :run.
exit /b

:run
if exist "..\.venv\Scripts\pythonw.exe" (
  set "PYTHONW=..\.venv\Scripts\pythonw.exe"
  set "PYTHON=..\.venv\Scripts\python.exe"
) else (
  set "PYTHONW=pyw.exe"
  set "PYTHON=py"
)
%PYTHON% -c "import aiohttp,yaml,dotenv,sounddevice,numpy,PySide6" >nul 2>nul
if errorlevel 1 goto missing_deps
if not exist logs mkdir logs

rem ---- detect an already-running instance via the single-instance lock ----
set "LOCKED=0"
%PYTHON% -c "import sys;sys.path.insert(0,r'E:\Doc\AIAgent');from agent import HOME_AGENT;from modules.live.ai_live_assistant.instance_lock import InstanceLock;sys.exit(0 if InstanceLock(HOME_AGENT/'state'/'ai-home-agent.lock').acquire() else 1)" >nul 2>nul
if %errorlevel% neq 0 set "LOCKED=1"
if "!LOCKED!"=="1" (
    echo [WARN] Home Agent is already running, single-instance lock is held.
    echo        If the desktop pet or tray icon is visible, just use it.
    echo        If no UI is visible, end leftover python and pythonw processes,
    echo        then run this launcher again.
    start "" mshta.exe "javascript:alert('Home Agent is already running.\nIf you cannot see its pet/tray icon, end leftover python/pythonw processes first, then retry.');close()"
    exit /b
)

start "" /b %PYTHONW% app.py %* >>logs\home-agent-windowless.log 2>&1
exit /b

:missing_deps
start "" mshta.exe "javascript:alert('Missing dependencies in the project environment. Install HomeAgent requirements first.');close()"
exit /b
