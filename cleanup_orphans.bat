@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  清理 venv 中的损坏/孤立安装残留 (~-prefixed 目录, 如 ~orch)
REM
REM  重要: 仅在所有使用该 venv 的 Python / AI 直播 agent 进程
REM        都已完全停止后运行! 否则 DLL 被占用会报 "拒绝访问"。
REM
REM  这些 ~ 目录是 pip/torch 升级中断留下的半截编译残留, 不被
REM  Python 导入、pip 也不追踪, 但会触发 pip 的 invalid-distribution
REM  警告, 且旧 torch 的 DLL 可能长期被运行中的进程锁住。
REM ============================================================

set "SP=%~dp0.venv\Lib\site-packages"
if not exist "%SP%" (
    echo [ERROR] site-packages not found: %SP%
    pause
    exit /b 1
)

echo ============================================================
echo   清理 venv 孤立残留 (~-prefixed 目录)
echo   目标: %SP%
echo   警告: 请先停止所有 Python / AI 直播 agent 进程!
echo ============================================================

powershell -NoProfile -Command "Get-ChildItem '%~dp0.venv\Lib\site-packages' -Directory -Filter '~*' | ForEach-Object { attrib -R -S -H $_.FullName /S /D; Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop; Write-Host ('[OK] removed ' + $_.Name) }; Write-Host '[DONE] cleanup complete'"

echo.
echo 若仍报 "拒绝访问", 说明有进程仍占用其中的 DLL。
echo 请彻底退出 agent (或重启机器) 后再次运行本脚本。
pause
