@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  Stop lingering model services, then start Home Agent as
rem  Administrator so one token is used for both Home Agent and
rem  its GPU services, letting the release-GPU action kill them.
rem ============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/2] Force-stopping model services on ports 8765 8766 9879 9880 ...
set KILLED=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr LISTENING ^| findstr ":8765 :8766 :9879 :9880"') do (
    echo    killing PID %%p
    taskkill /PID %%p /T /F >nul 2>&1
    set KILLED=1
)
if "%KILLED%"=="0" echo    none found on those ports
timeout /t 2 /nobreak >nul

echo [2/2] Verifying ports are now free ...
netstat -ano | findstr LISTENING | findstr ":8765 :8766 :9879 :9880" || echo    all model ports are free.

echo Starting Home Agent as Administrator ...
call "%~dp0Æô¶¯¼ÒÍ¥Agent.bat"
echo Done. Home Agent is running with highest privileges.
timeout /t 4 /nobreak >nul
exit /b
