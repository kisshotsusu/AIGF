@echo off
REM ---------------------------------------------------------------------------
REM 用 GUI-Owl-1.5-2B 后端启动 Vision MCP 服务 (streamable-http, 端口 8765)
REM
REM GUI-Owl 与 GUI-Actor 现已统一运行于项目共享 .venv（已升级
REM transformers>=4.57 + qwen-vl-utils>=0.0.14），直接使用 .venv 即可。
REM
REM 用法: 直接双击，或命令行执行此 bat。
REM   启动后 HomeAgent 的 vision_mcp.url = http://127.0.0.1:8765/mcp 即可对接。
REM   若要让 HomeAgent 自动拉起本后端，只需在 HomeAgent/config.yaml 的
REM   vision_mcp 段设 backend: gui_owl 即可（agent.py 统一用 .venv 拉起服务）。
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "VENV=%HERE%\.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [错误] 未找到 %VENV%
    echo 请先按 README 准备 .venv 环境: .venv\Scripts\python.exe -m pip install -r Vision\requirements.txt
    pause
    exit /b 1
)

set "VISION_BACKEND=gui_owl"
set "GUI_OWL_MODEL=%HERE%\models\GUI-Owl-1.5-2B-Instruct"
REM 输入降采样(最长边1280≈720p): 实测端到端 1.70s->1.30s, 归一化坐标无损; 设 0 关闭
set "VISION_MAX_SIDE=1280"
REM 输出格式: compact(默认,单行JSON,省~490ms) | tool_call(官方<tool_call>格式)
set "VISION_OWL_OUTPUT_FORMAT=compact"
set "VISION_MCP_TRANSPORT=http"
set "VISION_MCP_HOST=127.0.0.1"
set "VISION_MCP_PORT=8765"
set "VISION_PRELOAD_MODEL=1"
set "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"

echo [Vision] 以 GUI-Owl 后端启动 MCP (http://%VISION_MCP_HOST%:%VISION_MCP_PORT%/mcp)
"%VENV%" "%HERE%\mcp_server.py"
endlocal
