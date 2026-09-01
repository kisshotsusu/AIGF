@echo off
setlocal
title AI Character Manager (Qt)
cd /d "%~dp0CharacterManager"
if exist "..\.venv\Scripts\pythonw.exe" (
  set "PYTHONW=..\.venv\Scripts\pythonw.exe"
  set "PYTHON=..\.venv\Scripts\python.exe"
) else (
  set "PYTHONW=pyw.exe"
  set "PYTHON=py"
)
%PYTHON% -c "import yaml,PySide6;from PIL import Image" >nul 2>nul
if errorlevel 1 %PYTHON% -m pip install -r requirements.txt
if not exist logs mkdir logs
start "" /b %PYTHONW% qt_app.py >>logs\character-manager-qt-windowless.log 2>&1
exit /b
