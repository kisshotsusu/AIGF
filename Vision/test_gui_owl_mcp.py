#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
GUI-Owl MCP 服务 运行时冒烟测试 (不走 __main__, 不抢实例锁)
============================================================
在独立端口 8799 拉起 mcp_server 的 FastMCP 应用, 用真实 MCP 客户端
调用 vision_backend() 等工具, 验证:
  - GUI-Owl 后端经 MCP 传输可达
  - 工具确实被注册并可调度 (transport + dispatch 正常)

不触碰 Vision/state/vision-mcp.lock (用户实跑会话持有), 不加载模型
(grounding 需要浏览器/桌面, 当前沙箱无显示)。模型加载/定位已由
test_gui_owl.py 直接验证过。
"""
import os
import sys
import time
import threading

os.environ["VISION_BACKEND"] = "gui_owl"
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
sys.path.insert(0, "E:/Doc/AIAgent")
sys.path.insert(0, "E:/Doc/AIAgent/Vision")

import mcp_server as s  # 注册 32 个工具, 不进 __main__, 不抢锁

PORT = 8799
s.mcp.settings.host = "127.0.0.1"
s.mcp.settings.port = PORT


def _run_server():
    # 在后台线程运行 FastMCP (自带事件循环), 不调用 __main__ 的实例锁
    s.mcp.run(transport="streamable-http")


def _client_call():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = f"http://127.0.0.1:{PORT}/mcp"
    async def _go():
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                res = await session.call_tool("vision_backend", {})
                text = "".join(getattr(c, "text", "") for c in res.content)
                return names, text
    return _go()


if __name__ == "__main__":
    print("=" * 70)
    print("GUI-Owl MCP 运行时冒烟测试 (端口 %d, 后端=%s)" % (PORT, os.environ["VISION_BACKEND"]))
    print("=" * 70)

    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    time.sleep(4)  # 等 uvicorn 起来

    try:
        import asyncio
        names, text = asyncio.run(_client_call())
    except Exception as e:
        print("客户端调用失败:", repr(e))
        sys.exit(1)

    print(f"已注册工具数: {len(names)}")
    for must in ("vision_backend", "click", "desktop_click", "window_click", "ground_page"):
        print(f"  - {must}:", "OK" if must in names else "缺失")
    print("vision_backend() 返回:", text)

    # 证明服务进程本身能加载 GUI-Owl (lazy → 显式加载后再次查询)
    print("\n[强制在服务器进程中加载 GUI-Owl 模型]")
    s.agent.load_model()
    backend2 = asyncio.run(_client_call())[1]
    print("加载后 vision_backend() 返回:", backend2)
    loaded_ok = '"loaded": true' in backend2.lower() or "'loaded': True" in backend2

    ok = (len(names) == 32 and "vision_backend" in names and "gui_owl" in text and loaded_ok)
    print("\n结果:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
