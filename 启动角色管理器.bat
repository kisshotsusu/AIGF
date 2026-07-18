@echo off
setlocal
title AI Character Manager
cd /d "%~dp0CharacterManager"
py -c "import yaml; from PIL import Image, ImageTk" >nul 2>nul
if errorlevel 1 py -m pip install -r requirements.txt
if not exist logs mkdir logs
start "" /b pyw.exe app.py >>logs\character-manager-windowless.log 2>&1
exit /b
