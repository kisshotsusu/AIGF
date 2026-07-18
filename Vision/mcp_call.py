#!/usr/bin/env python
"""Small subprocess bridge so HomeAgent itself does not need the mcp package."""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# This bridge is often launched without a console by HomeAgent, but it is also
# useful as a standalone diagnostic.  Never inherit Windows' locale-dependent
# GBK writer because MCP errors may contain arbitrary Unicode/replacement chars.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


async def main() -> int:
    if len(sys.argv) < 4:
        print(json.dumps({"ok": False, "error": "usage: mcp_call.py URL TOOL JSON"}, ensure_ascii=False), flush=True)
        return 2
    url, tool, raw = sys.argv[1], sys.argv[2], sys.argv[3]
    arguments = json.loads(raw)
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
    texts = [getattr(item, "text", "") for item in result.content if getattr(item, "text", "")]
    if getattr(result, "isError", False):
        print(json.dumps({"ok": False, "error": "\n".join(texts) or f"tool failed: {tool}"}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({"ok": True, "text": "\n".join(texts)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
