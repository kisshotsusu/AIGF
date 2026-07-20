from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent import HomeAgent
from home_modules.code_editor import CodeEditorModule
from self_upgrade import SelfUpgradeManager


class AnswerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_is_published_before_tts_finishes(self) -> None:
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"home": {"max_context_messages": 4, "auto_speak": True}}
        agent.history = []
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


class SelfProgrammingTests(unittest.TestCase):
    def test_home_agent_code_request_is_detected(self) -> None:
        prompts = (
            r"你的本体在 E:\Doc\AI直播\HomeAgent，修改你自身的代码",
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
        self.assertIn("必须在本机工程中实际完成修改", contract)
        self.assertIn("没有写入文件", contract)


if __name__ == "__main__":
    unittest.main()
