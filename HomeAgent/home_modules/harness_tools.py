"""Authenticated, per-turn loopback bridge for Harness-owned tool execution."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HarnessToolBridge:
    def __init__(self, tools, invoke, *, timeout=300):
        self.tools = [tool for tool in tools if tool["function"]["name"] not in {"codex_cli_task", "mcp_task", "list_mcp_servers"}]
        self.names = {tool["function"]["name"] for tool in self.tools}
        self.invoke = invoke
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.loop = asyncio.get_running_loop()
        self.pending = set()
        self.lock = threading.Lock()
        self.closed = False

    def __enter__(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, value):
                data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                return hmac.compare_digest(self.headers.get("Authorization", ""),
                                           "Bearer " + bridge.token)

            def do_GET(self):
                if not self.authorized():
                    return self.reply(403, {"ok": False})
                if self.path != "/tools":
                    return self.reply(404, {"ok": False})
                self.reply(200, bridge.tools)

            def do_POST(self):
                if not self.authorized():
                    return self.reply(403, {"ok": False})
                if self.path != "/call":
                    return self.reply(404, {"ok": False})
                future = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2_000_000:
                        raise ValueError("Invalid request size")
                    request = json.loads(self.rfile.read(length))
                    name, args = request["name"], request["arguments"]
                    if name not in bridge.names or not isinstance(args, dict):
                        raise ValueError("Unknown tool or non-object arguments")
                    from jsonschema import validate
                    schema = next(tool["function"]["parameters"] for tool in bridge.tools
                                  if tool["function"]["name"] == name)
                    validate(args, schema)
                    with bridge.lock:
                        if bridge.closed:
                            raise RuntimeError("Tool bridge is closed")
                        future = asyncio.run_coroutine_threadsafe(bridge.invoke(name, args), bridge.loop)
                        bridge.pending.add(future)
                    self.reply(200, future.result(timeout=bridge.timeout))
                except Exception as exc:
                    if future is not None:
                        future.cancel()
                    self.reply(400, {"ok": False, "error": str(exc)})
                finally:
                    with bridge.lock:
                        bridge.pending.discard(future)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {"AIGF_TOOL_BRIDGE_URL": f"http://127.0.0.1:{self.server.server_port}",
                    "AIGF_TOOL_BRIDGE_TOKEN": self.token}
        return self

    def __exit__(self, *_):
        with self.lock:
            self.closed = True
            for future in self.pending:
                future.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
