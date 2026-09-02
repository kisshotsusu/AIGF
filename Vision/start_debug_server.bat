@echo off
REM ---------------------------------------------------------------------------
REM 启动 Vision 外部调试 HTTP 服务 (独立端口 8790, 不占用 MCP 8765)。
REM
REM 用途: 用 curl / Python / 浏览器直接调用, 排查视觉 grounding、窗口结构、
REM       截图与点击, 不必经过 MCP 或 HomeAgent。
REM
REM 端点见 debug_server.py 头部注释 (GET /tools 可实时查看)。
REM 常用:
REM   curl http://127.0.0.1:8790/health
REM   curl "http://127.0.0.1:8790/windows?contains=哔哩"
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "ROOT=%HERE%\.."
set "VENV=%ROOT%\.venv\Scripts\python.exe"
if not exist "%VENV%" (
    echo [错误] 未找到 %VENV%
    pause
    exit /b 1
)

set "VISION_DEBUG_PORT=8790"
REM 1=启动即预加载模型(占用~4GB显存); 0=首次调用时才懒加载(省显存)
set "VISION_PRELOAD_MODEL=0"
set "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"

echo [Debug] 启动 Vision 调试服务 http://127.0.0.1:8790  ^(GET /tools 看端点^)
echo [Debug] 关闭窗口或 Ctrl+C 即停止; 服务默认懒加载模型以省显存。
"%VENV%" "%HERE%\debug_server.py"
endlocal
