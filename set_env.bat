@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   AI-Live 环境统一安装  (单一共享 .venv, Python 3.12 推荐)
echo   覆盖: Vision / Sound / HomeAgent / CharacterManager
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
    echo [WARN] Vision\GUI-Actor source not found; the GUI-Actor backend will not work.
    echo         If you unzipped a package, keep that folder; or run:
    echo         git clone https://github.com/microsoft/GUI-Actor.git "%ROOT%\Vision\GUI-Actor"
)

REM 4) Upgrade pip
echo [STEP] Upgrading pip ...
"%PY%" -m pip install --upgrade pip

REM 5) Install PyTorch cu130 (CUDA 13.0; matches current env, supports Blackwell / RTX 50)
echo [STEP] Installing PyTorch (cu130) ...  (~2-3 GB, please wait)
"%PIP%" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130

REM 6) Install shared deps (Vision + Sound; transformers 4.57.1 serves both GUI-Actor & GUI-Owl)
echo [STEP] Installing requirements.txt (shared: Vision + Sound) ...
"%PIP%" install -r "%ROOT%\requirements.txt"

REM 6.5) Patch funasr for numpy 2.x compatibility (np.float / np.int 等已在 numpy2 删除)
echo [STEP] Patching funasr for numpy 2.x ...
"%PY%" "%ROOT%\patch_funasr_numpy2.py"

REM 7) Install HomeAgent deps (aiohttp / dotenv / PySide6 ...)
if exist "%ROOT%\HomeAgent\requirements.txt" (
    echo [STEP] Installing HomeAgent\requirements.txt ...
    "%PIP%" install -r "%ROOT%\HomeAgent\requirements.txt"
)

REM 8) Install CharacterManager deps (PySide6 / Pillow ...)
if exist "%ROOT%\CharacterManager\requirements.txt" (
    echo [STEP] Installing CharacterManager\requirements.txt ...
    "%PIP%" install -r "%ROOT%\CharacterManager\requirements.txt"
)

REM 9) Install Playwright browser
echo [STEP] Installing Playwright Chromium ...
"%PY%" -m playwright install chromium

REM 10) Self-verification: key imports + version gates
echo [STEP] Verifying environment ...
"%PY%" -c "import yaml,PySide6,aiohttp,dotenv,sounddevice,PIL;import transformers,qwen_vl_utils;v=transformers.__version__;assert tuple(int(x) for x in v.split('.')[:2])>=(4,57), f'transformers {v} < 4.57 required by GUI-Owl';print('[OK] transformers', v);print('[OK] PySide6 / aiohttp / dotenv / sounddevice / PIL / qwen-vl-utils all importable')" 2>&1
if errorlevel 1 (
    echo [ERROR] Environment verification FAILED. Check pip output above.
    pause
    exit /b 1
)

REM 10b) Sound chain: funasr 必须能导入 (依赖 pytorch_wpe + torchaudio), 且 numpy2 补丁已生效
"%PY%" -c "from funasr import AutoModel; from funasr.frontends.default import DefaultFrontend; import pytorch_wpe; print('[OK] funasr ASR chain + pytorch_wpe available')" 2>&1
if errorlevel 1 (
    echo [ERROR] funasr/Sound verification FAILED. Check output above.
    pause
    exit /b 1
)

"%PY%" -m pip check
if errorlevel 1 (
    echo [WARN] pip check reported dependency conflicts (see above); usually safe to ignore if imports passed.
)

echo ============================================================
echo   环境安装完成并通过自检 (单一 .venv 服务全部子项目)
echo   下一步:
echo     1. down_model.bat   下载模型权重 (GUI-Owl / SenseVoice)
echo     2. 启动家庭Agent.bat / 启动角色管理器.bat
echo   备注: 若 pip 仍报 invalid-distribution 或 site-packages 有 ~orch 残留,
echo        先彻底退出 agent 后运行 cleanup_orphans.bat 清理孤立目录。
echo ============================================================
pause
