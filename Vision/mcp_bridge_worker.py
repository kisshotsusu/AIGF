#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""常驻 Vision MCP 桥 worker。

由 HomeAgent 以子进程方式启动（使用项目 venv python，内含 mcp 客户端库），
在进程内保持一条到 Vision(8765) 的 streamable-http MCP 长连接，通过 stdio
JSON-lines 与父进程(HomeAgent)通信。父进程(HomeAgent 运行在系统 py312、无 mcp 包)
因此无需每次视觉工具调用都冷启动一个 mcp_call.py 子进程 + 完整 MCP 握手，
从而把单次工具调用的进程开销从 ~1.6 秒降到几毫秒。

协议(逐行 JSON, 严格一次一请求一应答)：
  父 -> worker stdin : {"id": <int>, "tool": "<name>", "args": {..}}
  worker -> stdout   : {"id": <int>, "ok": true, "text": "..."}
                       或 {"id": <int>, "ok": false, "error": "..."}
stdin 到达 EOF 时 worker 优雅退出（父进程退出/被 kill 时自动触发，绝不残留）。

说明：工具调用一律在事件循环内串行执行（8765 侧视觉推理本身也有锁），
因此连接是线程安全的，不需要再套额外锁。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# 输出可能含任意 Unicode/替换字符，禁止继承 Windows 的 GBK writer。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def _log(message: str) -> None:
    try:
        sys.stderr.write(f"[mcp-bridge-worker] {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


async def _run_session(url: str) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (mcp_read, mcp_write, _):
        async with ClientSession(mcp_read, mcp_write) as session:
            await session.initialize()
            _log(f"connected to {url}")
            # 就绪信号：父进程靠读到这一行判断 worker 已连上、可接收请求。
            _write_line({"id": 0, "ok": True, "ready": True})
            while True:
                raw = await asyncio.to_thread(_read_line)
                if raw is None:
                    break  # stdin EOF -> parent gone, exit cleanly
                try:
                    request = json.loads(raw)
                    request_id = int(request.get("id"))
                    tool = str(request.get("tool") or "")
                    args = request.get("args") or {}
                except (ValueError, TypeError, json.JSONDecodeError):
                    _log(f"bad request line: {raw[:200]}")
                    continue
                response: dict = {"id": request_id}
                try:
                    result = await asyncio.wait_for(
                        session.call_tool(tool, args),
                        timeout=float(os.environ.get("VISION_CALL_TIMEOUT", "300")),
                    )
                    texts = [getattr(item, "text", "") for item in result.content if getattr(item, "text", "")]
                    if getattr(result, "isError", False):
                        response.update({"ok": False, "error": "\n".join(texts) or f"tool failed: {tool}"})
                    else:
                        response.update({"ok": True, "text": "\n".join(texts)})
                except asyncio.TimeoutError:
                    _log(f"tool {tool} timed out")
                    response.update({"ok": False, "error": f"视觉工具 {tool} 执行超时"})
                except Exception as exc:  # noqa: BLE001 保持 worker 存活, 把错误回传
                    _log(f"tool {tool} error: {exc}")
                    response.update({"ok": False, "error": str(exc)})
                _write_line(response)
    return 0


def _read_line() -> str | None:
    """阻塞读 stdin 一整行(UTF-8)。EOF 返回 None。"""
    try:
        line = sys.stdin.buffer.readline()
    except (ValueError, OSError):
        return None
    if not line:
        return None
    return line.decode("utf-8", "replace").strip()


def _write_line(payload: dict) -> None:
    """向 stdout 写一行 JSON(UTF-8) 并 flush。"""
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (ValueError, OSError):
        pass


async def main() -> int:
    if len(sys.argv) < 2:
        _log("usage: mcp_bridge_worker.py URL")
        return 2
    url = sys.argv[1]
    _log(f"worker pid={os.getpid()} target={url}")
    try:
        return await _run_session(url)
    except Exception as exc:  # noqa: BLE001
        _log(f"fatal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
