#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SenseVoiceSmall 语音识别 - MCP Server
把本地语音识别封装成 MCP 工具, 可被 WorkBuddy / 任意 MCP 客户端调用。

工具:
  transcribe_file(path, language)       转写本地音频文件(wav/mp3/flac/m4a...)
  record_and_transcribe(duration, lang) 录音并转写(需麦克风)

运行: python mcp_server.py   (由 MCP 宿主以 stdio 方式拉起)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from mcp.server.fastmcp import FastMCP
import asr

mcp = FastMCP(
    "sound-asr",
    host=os.getenv("SOUND_MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("SOUND_MCP_PORT", "8766")),
    log_level="INFO",
)


@mcp.tool()
def transcribe_file(path: str, language: str = "auto") -> str:
    """转写本地音频文件(wav/mp3/flac/m4a/ogg 等)。
    path: 音频文件绝对路径; language: auto/zh/en/ja/ko/yue。
    只返回清洗后的纯文本，避免调用方把协议标签或调试前缀当作用户输入。"""
    r = asr.transcribe_file(path, language=language)
    return r["text"]


@mcp.tool()
def record_and_transcribe(duration: float = 5.0, language: str = "auto") -> str:
    """录制麦克风 duration 秒并实时转写(需本机有麦克风)。
    duration: 录音秒数; language: auto/zh/en/ja/ko/yue。
    返回识别文本。"""
    r = asr.record_and_transcribe(duration=duration, language=language)
    return r["text"]


if __name__ == "__main__":
    from pathlib import Path
    from modules.live.ai_live_assistant.instance_lock import InstanceLock
    transport = os.getenv("SOUND_MCP_TRANSPORT", "stdio").strip().lower()
    lock = InstanceLock(Path(HERE) / "state" / "sound-mcp.lock")
    if not lock.acquire():
        raise SystemExit("Sound MCP 已在运行，拒绝启动重复实例")
    try:
        mcp.run(transport="streamable-http" if transport in {"http", "streamable-http"} else "stdio")
    finally:
        lock.release()
