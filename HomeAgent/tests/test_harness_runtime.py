"""Opt-in real Windows runtime test, using an offline model and a harmless tool."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

from home_modules.deepseek_harness import HarnessExecutor
from home_modules.harness_tools import HarnessToolBridge


@unittest.skipUnless(os.environ.get("AIGF_TEST_HARNESS_RUNTIME") == "1",
                     "Set AIGF_TEST_HARNESS_RUNTIME=1 to launch the installed SDK runtime")
class HarnessRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_runtime_executes_bridge_tool_and_returns_observation(self):
        root = Path(__file__).resolve().parents[2]
        # Fixed harmless command; no untrusted interpolation into PowerShell.
        quote = lambda text: "'" + str(text).replace("'", "''") + "'"
        command = (
            quote('{"name":"echo","arguments":{"text":"bridge-smoke"}}')
            + " | & " + quote(sys.executable) + " "
            + quote(root / "HomeAgent" / "harness_tool_client.py")
        )
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(body)
                first = len(received) == 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                block = ({"type": "tool_use", "id": "call_echo", "name": "pwsh", "input": {}}
                         if first else {"type": "text", "text": ""})
                delta = ({"type": "input_json_delta", "partial_json": json.dumps({"command": command})}
                         if first else {"type": "text_delta", "text": "bridge-smoke-completed"})
                events = [
                    ("message_start", {"message": {"id": "msg_smoke", "type": "message",
                        "role": "assistant", "model": body["model"], "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 0}}}),
                    ("content_block_start", {"index": 0, "content_block": block}),
                    ("content_block_delta", {"index": 0, "delta": delta}),
                    ("content_block_stop", {"index": 0}),
                    ("message_delta", {"delta": {"stop_reason": "tool_use" if first else "end_turn",
                        "stop_sequence": None}, "usage": {"output_tokens": 1}}),
                    ("message_stop", {}),
                ]
                for kind, value in events:
                    self.wfile.write(("event: " + kind + "\ndata: "
                        + json.dumps({"type": kind, **value}) + "\n\n").encode())
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tool = {"function": {"name": "echo", "parameters": {"type": "object",
                "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
        invoke = AsyncMock(return_value={"ok": True, "observation": "verified-bridge-observation"})
        try:
            with tempfile.TemporaryDirectory() as folder:
                with HarnessToolBridge([tool], invoke) as bridge:
                    executor = HarnessExecutor(root, {
                        "dsh_home": folder, "profile": "sdk-minimal", "timeout_seconds": 30,
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                    })
                    answer = await executor.run("Run the harmless echo tool.",
                        env={**bridge.env, "DEEPSEEK_API_KEY": "offline-test"},
                        cancel_event=threading.Event())
                self.assertEqual(answer, "bridge-smoke-completed")
                self.assertIn("verified-bridge-observation", json.dumps(received[-1]["messages"]))
                invoke.assert_awaited_once_with("echo", {"text": "bridge-smoke"})
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
