@echo off
setlocal
title AI Character Manager
cd /d "%~dp0"
if not exist logs mkdir logs
if exist "..\.venv\Scripts\pythonw.exe" (
  set "PYTHONW=..\.venv\Scripts\pythonw.exe"
  set "PYTHON=..\.venv\Scripts\python.exe"
) else (
  set "PYTHONW=pyw.exe"
  set "PYTHON=py"
)
%PYTHON% -c "import yaml, PySide6" >nul 2>nul
if errorlevel 1 %PYTHON% -m pip install -r requirements.txt
start "" /b %PYTHONW% app.py >>logs\character-manager-windowless.log 2>&1
exit /b
