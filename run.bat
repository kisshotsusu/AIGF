@echo off
setlocal
cd /d "%~dp0"
if not exist config.yaml copy /y config.example.yaml config.yaml >nul
if not exist .env copy /y .env.example .env >nul
py -c "import aiohttp, yaml, dotenv, brotli" >nul 2>nul
if errorlevel 1 goto missing_deps
if not exist logs mkdir logs
start "" /b pyw.exe main.py --config config.yaml >>logs\assistant-windowless.log 2>&1
exit /b

:missing_deps
start "" mshta.exe "javascript:alert('Missing dependencies. Run: py -m pip install -r requirements.txt');close()"
exit /b
