@echo off
setlocal
title Home AI Agent
cd /d "%~dp0"
py -c "import aiohttp,yaml,dotenv,sounddevice,numpy" >nul 2>nul
if errorlevel 1 goto missing_deps
if not exist logs mkdir logs
start "" /b pyw.exe app.py >>logs\home-agent-windowless.log 2>&1
exit /b

:missing_deps
start "" mshta.exe "javascript:alert('Missing dependencies. Run: py -m pip install -r requirements.txt');close()"
exit /b

