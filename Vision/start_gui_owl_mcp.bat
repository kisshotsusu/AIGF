@echo off
REM ---------------------------------------------------------------------------
REM 用 GUI-Owl-1.5-2B 后端启动 Vision MCP 服务 (streamable-http, 端口 8765)
REM
REM 与默认 GUI-Actor 不同，GUI-Owl 需要 transformers>=4.57，无法用共享 .venv
REM 运行，因此这里固定使用专门为其创建的 .venv-owl 环境。
REM
REM 用法: 直接双击，或命令行执行此 bat。
REM   启动后 HomeAgent 的 vision_mcp.url = http://127.0.0.1:8765/mcp 即可对接。
REM   若要让 HomeAgent 自动拉起本后端，需把 agent.py 中 Vision 服务的
REM   python 路径从 .venv 改为 .venv-owl (见 README "GUI-Owl 专用环境" 一节)。
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "VENV=%HERE%\.venv-owl\Scripts\python.exe"
if not exist "%VENV%" (
    echo [错误] 未找到 %VENV%
    echo 请先按 README 创建 .venv-owl: .venv\Scripts\python.exe -m venv .venv-owl 并安装依赖
    pause
    exit /b 1
)

set "VISION_BACKEND=gui_owl"
set "GUI_OWL_MODEL=%HERE%\models\GUI-Owl-1.5-2B-Instruct"
set "VISION_MCP_TRANSPORT=http"
set "VISION_MCP_HOST=127.0.0.1"
set "VISION_MCP_PORT=8765"
set "VISION_PRELOAD_MODEL=1"
set "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"

echo [Vision] 以 GUI-Owl 后端启动 MCP (http://%VISION_MCP_HOST%:%VISION_MCP_PORT%/mcp)
"%VENV%" "%HERE%\mcp_server.py"
endlocal
