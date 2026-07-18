@echo off
setlocal
title AI Live Console
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  set "PYTHONW=.venv\Scripts\pythonw.exe"
  set "PYTHON=.venv\Scripts\python.exe"
) else (
  set "PYTHONW=pyw.exe"
  set "PYTHON=py"
)
%PYTHON% -c "import aiohttp,yaml,dotenv" >nul 2>nul
if errorlevel 1 goto missing_deps
if not exist logs mkdir logs
start "" /b %PYTHONW% manager.py >>logs\manager-windowless.log 2>&1
exit /b

:missing_deps
start "" mshta.exe "javascript:alert('Missing dependencies. Run: py -m pip install -r requirements.txt');close()"
exit /b
