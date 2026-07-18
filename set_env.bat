@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   AI-Live Environment Setup  (Python 3.12 recommended)
echo   Creates .venv and installs all deps + Playwright browser
echo ============================================================

REM Resolve the folder this bat lives in (handles CJK paths via %~dp0)
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM 1) Check Python on PATH
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found. Install Python 3.12 and tick "Add to PATH".
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [INFO] Detected Python %PYVER%

REM 2) Create venv (skip if already present)
if exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [INFO] .venv already exists, skip creation
) else (
    echo [STEP] Creating virtualenv .venv ...
    python -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv. Run as admin or check Python install.
        pause
        exit /b 1
    )
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
set "PIP=%ROOT%\.venv\Scripts\pip.exe"

REM 3) Warn if GUI-Actor source is missing (needed at runtime by Vision)
if not exist "%ROOT%\Vision\GUI-Actor\src\gui_actor" (
    echo [WARN] Vision\GUI-Actor source not found; the Vision web-control will not work.
    echo         If you unzipped a package, keep that folder; or run:
    echo         git clone https://github.com/microsoft/GUI-Actor.git "%ROOT%\Vision\GUI-Actor"
)

REM 4) Upgrade pip
echo [STEP] Upgrading pip ...
"%PY%" -m pip install --upgrade pip

REM 5) Install PyTorch cu128 (Blackwell / RTX 50 support; MUST use official cu source)
echo [STEP] Installing PyTorch (cu128) ...  (~2-3 GB, please wait)
"%PIP%" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128

REM 6) Install remaining dependencies
echo [STEP] Installing requirements.txt ...
"%PIP%" install -r "%ROOT%\requirements.txt"

REM 7) Install Playwright browser
echo [STEP] Installing Playwright Chromium ...
"%PY%" -m playwright install chromium

echo ============================================================
echo   Environment setup complete!
echo   Next: run down_model.bat to download model weights
echo ============================================================
pause
