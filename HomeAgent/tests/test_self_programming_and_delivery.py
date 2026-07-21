from __future__ import annotations

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

from agent import HomeAgent
from home_modules.code_editor import CodeEditorModule
from self_upgrade import SelfUpgradeManager


class AnswerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_is_published_before_tts_finishes(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"home": {"max_context_messages": 4, "auto_speak": True}}
        agent.history = []
        root = Path(__file__).resolve().parents[2]
        agent.self_upgrade = SimpleNamespace(code_editor=CodeEditorModule(root, root / "HomeAgent"), is_upgrade_request=lambda text: False)
        agent.log_event = lambda *args, **kwargs: None
        agent._acknowledge_common_response = lambda text: None

        async def plan(text: str, context: str = "") -> dict:
            return {"requires_clarification": True}

        events: list[str] = []

        async def speak(session, text: str, status=None, ignore_cancel: bool = False):
            events.append("tts_started")
            events.append("tts_finished")

        agent._plan_task = plan
        agent._speak_home = speak
        answer = await agent.chat("不明确的任务", answer_ready=lambda text: events.append("message_shown"))

        self.assertTrue(answer)
        self.assertEqual(["message_shown", "tts_started", "tts_finished"], events)

    async def test_agent_executes_local_code_tools_without_codex(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            editor = CodeEditorModule(root, home)
            editor.begin_tracking()
            agent = HomeAgent.__new__(HomeAgent)
            agent.self_upgrade = SimpleNamespace(code_editor=editor)
            agent.current_code_self_edit = False
            written = await agent._run_tool("code_write_file", {"path": "Projects/demo/main.py", "content": "VALUE = 1\n"})
            read = await agent._run_tool("code_read_file", {"path": "Projects/demo/main.py"})
            self.assertTrue(written["ok"])
            self.assertEqual("VALUE = 1\n", read["content"])

    async def test_tts_failure_uses_windows_fallback_without_failing_chat(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.tts_execution_lock = __import__("threading").Lock()
        events = []
        agent.log_event = lambda name, **data: events.append((name, data))
        agent._speak_home_unlocked = AsyncMock(side_effect=asyncio.LimitOverrunError("separator missing", 100))
        with patch.object(agent, "_windows_sapi_speak", return_value=True):
            result = await agent._speak_home(None, "任务没有完成。")
        self.assertEqual(result, [])
        self.assertTrue(any(name == "home_tts_fallback" and data["ok"] for name, data in events))

    async def test_general_text_writer_atomically_creates_external_script(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            agent = HomeAgent.__new__(HomeAgent)
            agent.config = {"computer_control": {"full_access": True, "confirm_before_action": False}}
            target = Path(folder) / "start_demo.bat"
            result = await agent._run_tool("write_text_file", {"path": str(target), "content": "@echo off\necho ready\n"})
            self.assertTrue(result["ok"])
            self.assertIn("echo ready", target.read_text(encoding="utf-8"))


class SelfProgrammingTests(unittest.TestCase):
    def test_external_bat_request_uses_file_tools_not_ui_or_codex(self) -> None:
        prompt = r"检查 D:\Program\hermes-agent 给hermes 写个一键启动的bat"
        self.assertTrue(HomeAgent._is_file_authoring_request(prompt))
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = False
        agent.current_file_authoring_task = True
        agent.config = {
            "agent": {"prefer_local_code_tools": True, "model_driven_computer_actions": True},
            "codex_cli": {"enabled": True}, "vision_mcp": {"enabled": True},
            "computer_control": {"enabled": True},
        }
        names = {item["function"]["name"] for item in agent._tools()}
        self.assertIn("write_text_file", names)
        self.assertNotIn("ui_list_windows", names)
        self.assertNotIn("launch_app", names)
        self.assertNotIn("codex_cli_task", names)

    def test_model_receives_shell_and_cmd_tools_when_enabled(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = False
        agent.current_file_authoring_task = False
        agent.config = {
            "agent": {"prefer_local_code_tools": True, "model_driven_computer_actions": True},
            "codex_cli": {"enabled": False}, "vision_mcp": {"enabled": False},
            "computer_control": {"enabled": True},
            "shell_execution": {"shell_enabled": True, "cmd_enabled": True},
        }
        tools = {item["function"]["name"]: item["function"] for item in agent._tools()}
        self.assertIn("run_shell", tools)
        self.assertIn("run_cmd", tools)
        self.assertIn("由你", tools["run_shell"]["description"])
    def test_independent_project_request_prefers_local_tools(self) -> None:
        root = Path(__file__).resolve().parents[2]
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"agent": {"prefer_local_code_tools": True}, "codex_cli": {"enabled": True, "trigger_mode": "auto", "trigger_keywords": ["写程序"]}}
        agent.self_upgrade = SimpleNamespace(code_editor=CodeEditorModule(root, root / "HomeAgent"))
        self.assertFalse(agent._should_route_to_codex("创建一个独立的 Python 记账项目并测试"))
        self.assertTrue(agent._should_route_to_codex("明确调用 Codex 创建一个独立 Python 项目"))

    def test_code_tool_surface_hides_codex_during_local_code_task(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.current_code_task = True
        agent.config = {
            "agent": {"prefer_local_code_tools": True, "model_driven_computer_actions": True},
            "codex_cli": {"enabled": True}, "vision_mcp": {"enabled": False}, "computer_control": {"enabled": False},
        }
        names = {item["function"]["name"] for item in agent._tools()}
        self.assertIn("code_write_file", names)
        self.assertIn("code_validate_project", names)
        self.assertNotIn("codex_cli_task", names)

    def test_home_agent_code_request_is_detected(self) -> None:
        prompts = (
            r"你的本体在 E:\Doc\AIAgent\HomeAgent，修改你自身的代码",
            "给 HomeAgent 添加自己写代码的功能",
            "修复你的代码并完成测试",
            "在播放语音的时候就显示消息，不要等语音播完再显示消息",
        )
        for prompt in prompts:
            self.assertTrue(SelfUpgradeManager.is_upgrade_request(prompt), prompt)

    def test_self_programming_requires_real_validated_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            source = home / "sample.py"
            source.write_text("value = 1\n", encoding="utf-8")
            manager = SelfUpgradeManager(root, home, {"self_upgrade": {"require_validation": True}})
            manager.begin("修改 HomeAgent 程序")
            self.assertFalse(manager.validate_current_changes(require_changes=True)["ok"])
            source.write_text("value = 2\n", encoding="utf-8")
            result = manager.validate_current_changes(require_changes=True)
            self.assertTrue(result["ok"])
            self.assertEqual(["HomeAgent/sample.py"], result["changed"])

    def test_self_programming_reads_engineering_documents(self) -> None:
        root = Path(__file__).resolve().parents[2]
        module = CodeEditorModule(root, root / "HomeAgent")
        content, loaded = module.load_engineering_documents()
        self.assertIn("README.md", loaded)
        self.assertIn("AI Read/01_ARCHITECTURE.md", loaded)
        self.assertIn("# AI 直播工具箱", content)
        self.assertIn("# 设计架构", content)

    def test_execution_contract_is_owned_by_isolated_module(self) -> None:
        root = Path(__file__).resolve().parents[2]
        module = CodeEditorModule(root, root / "HomeAgent")
        contract, loaded = module.build_execution_contract()
        self.assertTrue(loaded)
        self.assertIn("必须在本机实际完成代码写入", contract)
        self.assertIn("没有写入文件", contract)

    def test_independent_project_request_and_contract(self) -> None:
        prompt = "创建一个独立的 Python 待办事项项目并编写测试"
        self.assertTrue(CodeEditorModule.is_independent_project_request(prompt))
        self.assertTrue(CodeEditorModule.is_code_task(prompt))
        root = Path(__file__).resolve().parents[2]
        contract, loaded = CodeEditorModule(root, root / "HomeAgent").build_execution_contract(self_edit=False)
        self.assertFalse(loaded)
        self.assertIn("Projects/<简短英文项目名>", contract)
        self.assertIn("自动测试", contract)

    def test_independent_python_project_is_tracked_and_tested(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            module = CodeEditorModule(root, home)
            module.begin_tracking()
            project = root / "Projects" / "demo"
            tests = project / "tests"
            tests.mkdir(parents=True)
            (project / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (tests / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(3, add(1, 2))\n",
                encoding="utf-8",
            )
            changed = module.changed_files()
            self.assertIn("Projects/demo/calculator.py", changed)
            validation = module.validate_current_changes(require_changes=True)
            self.assertTrue(validation["ok"])
            tests_result = module.run_autonomous_tests(changed, timeout=30)
            self.assertTrue(tests_result["ok"], tests_result)
            self.assertGreaterEqual(len(tests_result["commands"]), 2)

    def test_local_code_file_tools_are_atomic_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            module = CodeEditorModule(root, home)
            written = module.write_file("Projects/demo/main.py", "VALUE = 1\n")
            self.assertTrue(written["created"])
            self.assertEqual("VALUE = 1\n", module.read_file("Projects/demo/main.py")["content"])
            replaced = module.replace_text("Projects/demo/main.py", "1", "2")
            self.assertEqual(1, replaced["replaced"])
            self.assertEqual("VALUE = 2\n", module.read_file("Projects/demo/main.py")["content"])
            with self.assertRaises(ValueError):
                module.write_file("HomeAgent/agent.py", "blocked")
            with self.assertRaises(ValueError):
                module.read_file(".env")

    def test_failing_project_tests_block_success(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / "HomeAgent"
            home.mkdir()
            module = CodeEditorModule(root, home)
            module.begin_tracking()
            tests = root / "Projects" / "broken" / "tests"
            tests.mkdir(parents=True)
            (tests.parent / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
            (tests / "test_value.py").write_text(
                "import unittest\nfrom value import VALUE\n\n"
                "class ValueTests(unittest.TestCase):\n"
                "    def test_expected_value(self):\n        self.assertEqual(2, VALUE)\n",
                encoding="utf-8",
            )
            result = module.run_autonomous_tests(module.changed_files(), timeout=30)
            self.assertFalse(result["ok"])
            self.assertTrue(result["failed"])


if __name__ == "__main__":
    unittest.main()
