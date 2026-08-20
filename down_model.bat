@echo off
setlocal

echo ============================================================
echo   Download Model Weights  (GUI-Owl-1.5-2B + SenseVoiceSmall)
echo ============================================================

REM Resolve the folder this bat lives in (handles CJK paths via %~dp0)
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY=%ROOT%\.venv\Scripts\python.exe"

REM Make sure the env exists
if not exist "%PY%" (
    echo [ERROR] .venv not found. Run set_env.bat first.
    pause
    exit /b 1
)

echo [STEP] Downloading GUI-Owl-1.5-2B (Vision, 默认识别后端) ...  (~5 GB, resumable)
"%PY%" "%ROOT%\Vision\download_model.py" --model gui-owl
REM 旧版 GUI-Actor 只在需要时下载:
REM "%PY%" "%ROOT%\Vision\download_model.py" --model gui-actor

echo.
echo [STEP] Downloading SenseVoiceSmall (Sound) ...  (~1 GB, via ModelScope)
"%PY%" "%ROOT%\Sound\download_model.py"

echo ============================================================
echo   Model download complete!
echo   Now trust/enable vision-gui and sound-asr in WorkBuddy.
echo ============================================================
pause
