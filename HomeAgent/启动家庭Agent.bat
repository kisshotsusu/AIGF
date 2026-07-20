@echo off
setlocal
title Home AI Agent
cd /d "%~dp0"
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
start "" /b %PYTHONW% app.py %* >>logs\home-agent-windowless.log 2>&1
exit /b

:missing_deps
start "" mshta.exe "javascript:alert('Missing dependencies in the project environment. Install HomeAgent requirements first.');close()"
exit /b

