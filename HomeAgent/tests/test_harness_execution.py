import asyncio
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from home_modules.deepseek_harness import HarnessExecutor
from home_modules.harness_tools import HarnessToolBridge
from agent import HomeAgent


class HarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_uses_explicit_home_and_accepts_completed_only(self):
        runtime = Mock()
        runtime.run.return_value = SimpleNamespace(finish_reason="completed", final_response="done")
        factory = Mock(return_value=runtime)
        executor = HarnessExecutor(Path.cwd(), {"dsh_home": "state/test-dsh"}, factory=factory)
        answer = await executor.run("hello", env={}, cancel_event=threading.Event())
        self.assertEqual(answer, "done")
        self.assertTrue(Path(factory.call_args.kwargs["dsh_home"]).is_absolute())
        runtime.close.assert_called_once()

    async def test_failed_harness_does_not_report_success(self):
        for reason in ("error", "max-tokens", None):
            runtime = Mock()
            runtime.run.return_value = SimpleNamespace(finish_reason=reason, final_response="claimed success")
            with self.assertRaises(RuntimeError):
                await HarnessExecutor(".", {}, factory=lambda **_: runtime).run(
                    "hello", env={}, cancel_event=threading.Event())

    async def test_cancel_closes_runtime(self):
        stopped = threading.Event()
        cancel = threading.Event()
        runtime = Mock()
        runtime.run.side_effect = lambda *a, **k: stopped.wait(3)
        runtime.close.side_effect = stopped.set
        task = asyncio.create_task(HarnessExecutor(".", {}, factory=lambda **_: runtime).run(
            "hello", env={}, cancel_event=cancel))
        await asyncio.sleep(.05)
        cancel.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_bridge_auth_schema_and_dispatch(self):
        tool = {"type": "function", "function": {"name": "echo", "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}}
        invoke = AsyncMock(return_value={"ok": True, "result": "hello"})
        with HarnessToolBridge([tool], invoke) as bridge:
            def request(path, data=None, authorized=True):
                req = urllib.request.Request(bridge.env["AIGF_TOOL_BRIDGE_URL"] + path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Authorization": "Bearer " + bridge.token if authorized else ""})
                with urllib.request.urlopen(req) as response:
                    return json.load(response)
            self.assertEqual(await asyncio.to_thread(request, "/tools"), [tool])
            with self.assertRaises(urllib.error.HTTPError):
                await asyncio.to_thread(request, "/tools", None, False)
            with self.assertRaises(urllib.error.HTTPError):
                await asyncio.to_thread(request, "/call", {"name": "echo", "arguments": {}})
            invoke.assert_not_called()
            result = await asyncio.to_thread(request, "/call", {"name": "echo", "arguments": {"text": "hello"}})
            self.assertTrue(result["ok"])
            invoke.assert_awaited_once_with("echo", {"text": "hello"})

    async def test_chat_bypasses_local_planner_and_never_falls_back(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"home": {"max_context_messages": 30}}
        agent.history = []
        agent.is_restart_request = lambda _: False
        agent.detect_wake_word = lambda text: (False, text)
        agent.log_event = Mock()
        agent._compact_context_if_needed = AsyncMock()
        agent._chat_with_harness = AsyncMock(side_effect=RuntimeError("offline"))
        agent._plan_task = AsyncMock()
        agent._run_tool = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "offline"):
            await agent.chat("execute task")
        agent._plan_task.assert_not_called()
        agent._run_tool.assert_not_called()

    async def test_bridge_excludes_alternative_agent_delegation(self):
        tools = [{"function": {"name": name}} for name in ("codex_cli_task", "mcp_task", "list_mcp_servers")]
        bridge = HarnessToolBridge(tools, AsyncMock())
        self.assertEqual(bridge.tools, [])
