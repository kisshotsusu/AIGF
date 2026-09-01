#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读验证：Vision 服务能否对具体窗口截图（不点击）。"""
import asyncio, base64, os
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

MCP_URL = "http://127.0.0.1:8765/mcp"
ART = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_artifacts", "cap")
os.makedirs(ART, exist_ok=True)

async def main():
    async with streamablehttp_client(MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for title in ["任务管理器", "WorkBuddy"]:
                print(f"\n--- window_screenshot({title!r}) ---")
                try:
                    res = await s.call_tool("window_screenshot", {"title_contains": title})
                    saved = []
                    for i, c in enumerate(res.content):
                        if getattr(c, "type", None) == "image":
                            raw = getattr(c, "data", None) or getattr(c, "blob", None)
                            if raw:
                                if isinstance(raw, str): raw = base64.b64decode(raw)
                                p = os.path.join(ART, f"{title}_{i}.png")
                                open(p, "wb").write(raw); saved.append(p)
                    print("  saved:", saved)
                except Exception as e:
                    print("  ERR:", e)
            # 也试一下 list_windows 看全部标题
            print("\n--- list_windows('') ---")
            try:
                res = await s.call_tool("list_windows", {"title_contains": ""})
                print(" ", "".join(getattr(c,'text','') for c in res.content if getattr(c,'type',None)=='text')[:1500])
            except Exception as e:
                print("  ERR:", e)

asyncio.run(main())
