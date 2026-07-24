import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from home_modules.mimo_multimodal import MiMoMultimodalClient
from agent import HomeAgent, _grab_screen_with_retry, _read_compatible_text


class Response:
    status = 200
    async def text(self): return json.dumps({"choices": [{"message": {"content": "识别结果"}}]})
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class Session:
    def __init__(self): self.payload = None; self.headers = None
    def post(self, url, json, headers): self.payload = json; self.headers = headers; return Response()


class MiMoMultimodalTests(unittest.IsolatedAsyncioTestCase):
    def test_screen_capture_retries_transient_gdi_failure(self):
        from PIL import Image

        captured = Image.new("RGB", (4, 4), "white")
        with patch("PIL.ImageGrab.grab", side_effect=[OSError("screen grab failed"), captured]) as grab:
            result = _grab_screen_with_retry(attempts=2)
        self.assertEqual(result.size, (4, 4))
        self.assertEqual(grab.call_count, 2)
        result.close()

    def test_read_compatible_text_accepts_gb18030_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build.log"
            path.write_bytes("编译进度 43%".encode("gb18030"))
            content, encoding = _read_compatible_text(path)
        self.assertEqual(content, "编译进度 43%")
        self.assertEqual(encoding, "gb18030")

    def test_pasted_image_builds_ephemeral_multimodal_message(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "clipboard.png"; image.write_bytes(b"png-data")
            content = HomeAgent._image_message_content("这道题怎么做？", image)
            self.assertEqual(content[0]["type"], "image_url")
            self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(content[1], {"type": "text", "text": "这道题怎么做？"})

    def test_multiple_pasted_images_are_in_one_multimodal_message(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.png"; first.write_bytes(b"first")
            second = Path(directory) / "second.jpg"; second.write_bytes(b"second")
            content = HomeAgent._image_message_content("比较两张图", [first, second])
            self.assertEqual([item["type"] for item in content], ["image_url", "image_url", "text"])
            self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(content[2]["text"], "比较两张图")

    async def test_image_uses_official_content_shape(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MIMO_API_KEY": "hidden"}):
            image = Path(directory) / "screen.png"; image.write_bytes(b"png")
            session = Session(); result = await MiMoMultimodalClient().analyze_image(session, image, "看什么")
            content = session.payload["messages"][0]["content"]
            self.assertEqual(result["text"], "识别结果")
            self.assertEqual(content[0]["type"], "image_url")
            self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(session.headers["api-key"], "hidden")

    async def test_audio_uses_asr_model_and_language(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MIMO_API_KEY": "hidden"}):
            audio = Path(directory) / "voice.wav"; audio.write_bytes(b"wav")
            session = Session(); await MiMoMultimodalClient().transcribe_audio(session, audio, "zh")
            self.assertEqual(session.payload["model"], "mimo-v2.5-asr")
            self.assertEqual(session.payload["asr_options"], {"language": "zh"})
            self.assertEqual(session.payload["messages"][0]["content"][0]["type"], "input_audio")

    async def test_audio_rejects_unsupported_format(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MIMO_API_KEY": "hidden"}):
            bad = Path(directory) / "voice.flac"; bad.write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "WAV 或 MP3"):
                await MiMoMultimodalClient().transcribe_audio(Session(), bad)

    async def test_completion_check_parses_json(self):
        with patch.dict(os.environ, {"MIMO_API_KEY": "hidden"}):
            client = MiMoMultimodalClient()
            async def fake_post(session, payload): return '{"passed":false,"reason":"没有终态证据","next_action":"重新读取页面"}'
            client._post = fake_post
            result = await client.verify_completion(Session(), "打开页面", {"actionable": True}, "完成", [])
            self.assertEqual(result, {"passed": False, "reason": "没有终态证据", "next_action": "重新读取页面"})

    async def test_completion_check_disables_thinking_without_response_format(self):
        client = MiMoMultimodalClient()
        captured = {}

        async def fake_post(session, payload):
            captured.update(payload)
            return '{"passed":true,"reason":"证据充分","next_action":""}'

        client._post = fake_post
        result = await client.verify_completion(Session(), "打开页面", {"actionable": True}, "完成", [])
        self.assertTrue(result["passed"])
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertFalse(captured["stream"])
        self.assertNotIn("response_format", captured)
        self.assertIn("只读任务", captured["messages"][1]["content"])
        self.assertIn("不得额外要求被观察对象达到终态", captured["messages"][1]["content"])

    async def test_completion_check_orders_evidence_by_capture_time(self):
        client = MiMoMultimodalClient()
        captured = {}

        async def fake_post(session, payload):
            captured.update(payload)
            return '{"passed":true,"reason":"最新证据充分","next_action":""}'

        client._post = fake_post
        await client.verify_completion(Session(), "停止音乐", {"actionable": True}, "已停止", [
            {"tool_sequence": 1, "tool_completed_at": "2026-01-01T10:00:00+08:00", "state": "playing"},
            {"tool_sequence": 2, "tool_completed_at": "2026-01-01T10:00:01+08:00", "state": "stopped"},
        ])
        prompt = captured["messages"][1]["content"]
        self.assertIn("tool_sequence", prompt)
        self.assertIn("较新状态覆盖较早状态", prompt)
        self.assertIn("截图采集时刻", prompt)

    async def test_music_completion_uses_fresh_visual_state_not_title_change(self):
        client = MiMoMultimodalClient()
        captured = {}

        async def fake_post(session, payload):
            captured.update(payload)
            return '{"passed":true,"reason":"最新视觉证据确认目标歌曲正在播放","next_action":""}'

        client._post = fake_post
        result = await client.verify_completion(
            Session(),
            "打开网易云播放 All by My Design",
            {"site": "cloudmusic", "operation": "play", "query": "All by My Design"},
            "已经播放",
            [{"tool": "ui_analyze_window", "result": {
                "tool_sequence": 6,
                "screenshot_captured_at": "2026-07-24T22:42:54+08:00",
                "analysis": "暂停图标可见，All by My Design 正在播放",
            }}],
        )
        prompt = captured["messages"][1]["content"]
        self.assertTrue(result["passed"])
        self.assertIn("窗口标题是否变化不是成功条件", prompt)
        self.assertIn("目标在任务开始时已经播放也视为幂等完成", prompt)
        self.assertIn("不得为了制造标题变化而停止、重播或重复双击", prompt)

    def test_completion_evidence_compaction_always_preserves_newest_state(self):
        old = [
            {"tool": "ui_analyze_window", "result": {
                "tool_sequence": index,
                "analysis": f"OLD-{index}-" + ("旧画面" * 2500),
            }}
            for index in range(1, 20)
        ]
        newest = {"tool": "ui_analyze_window", "result": {
            "tool_sequence": 20,
            "analysis": "LATEST-PLAYING-All by My Design",
            "screenshot_captured_at": "2026-07-24T22:55:35+08:00",
        }}
        compact = MiMoMultimodalClient._compact_completion_evidence(
            [*old, newest], max_chars=5000,
        )
        self.assertIn("LATEST-PLAYING-All by My Design", compact)
        self.assertLessEqual(len(compact), 5000)

    async def test_hotkey_tool_does_not_apply_business_task_restrictions(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"vision_mcp": {"enabled": True}}
        agent.current_task_plan = {"operation": "stop_media"}
        agent.ensure_vision_service = Mock(return_value=True)
        agent._vision_mcp_call = AsyncMock(return_value="{'pressed': ['space'], 'state_changed': True}")
        result = await agent._run_tool("ui_hotkey", {"keys": ["space"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["executed_tool"], "desktop_hotkey")
        agent.ensure_vision_service.assert_called_once()

    async def test_cloudmusic_allows_active_input_and_enter(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"vision_mcp": {"enabled": True}}
        agent.current_task_plan = {"site": "cloudmusic", "operation": "play"}
        agent.ensure_vision_service = Mock(return_value=True)
        agent._vision_mcp_call = AsyncMock(side_effect=[
            "{'typed': True, 'state_changed': True}",
            "{'pressed': ['enter'], 'state_changed': True}",
        ])
        typed = await agent._run_tool("ui_type_active_text", {"text": "All by My Design"})
        submitted = await agent._run_tool("ui_hotkey", {"keys": ["enter"]})
        self.assertTrue(typed["ok"])
        self.assertTrue(typed["observation"]["typed"])
        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["observation"]["pressed"], ["enter"])
        self.assertEqual(agent._vision_mcp_call.await_count, 2)

    async def test_vision_tool_result_contains_submission_and_completion_times(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"vision_mcp": {"enabled": True}}
        agent.current_task_plan = {"operation": "stop_media"}
        agent.ensure_vision_service = Mock(return_value=True)
        agent._vision_mcp_call = AsyncMock(return_value="{'ok': True, 'requested_state': 'stopped', 'idempotent': True}")
        result = await agent._run_tool("media_stop", {})
        self.assertTrue(result["ok"])
        self.assertIn("vision_request_submitted_at", result)
        self.assertIn("vision_response_completed_at", result)
        self.assertGreaterEqual(result["vision_elapsed_ms"], 0)

    async def test_completion_check_rejects_string_false(self):
        with patch.dict(os.environ, {"MIMO_API_KEY": "hidden"}):
            client = MiMoMultimodalClient()
            async def fake_post(session, payload): return '{"passed":"false","reason":"没有终态证据","next_action":"重新读取页面"}'
            client._post = fake_post
            with self.assertRaisesRegex(RuntimeError, "JSON boolean"):
                await client.verify_completion(Session(), "打开页面", {"actionable": True}, "完成", [])

    async def test_screen_care_deletes_temporary_screenshot(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"screen_care": {"enabled": True, "speak": False}, "home": {"auto_speak": True}}
        agent.history = []
        agent.mimo_multimodal = MiMoMultimodalClient()
        events = []
        captured = []
        agent.log_event = lambda event, **data: events.append((event, data))

        class Image:
            def convert(self, mode): return self
            def save(self, path, kind): Path(path).write_bytes(b"png")

        async def analyze(session, path, prompt):
            self.assertTrue(path.exists())
            captured.append(path)
            self.assertIn("不要复述屏幕", prompt)
            return {"text": "主人，忙一会儿也记得喝口水呀。", "model": "mimo-v2.5"}

        agent.mimo_multimodal.analyze_image = analyze
        with patch("PIL.ImageGrab.grab", return_value=Image()):
            result = await agent.proactive_screen_care()
        self.assertEqual(result, "主人，忙一会儿也记得喝口水呀。")
        self.assertFalse(captured[0].exists())
        self.assertEqual(events[-1][0], "proactive_screen_care")
        self.assertEqual(result, agent.history[-1]["content"])
        self.assertEqual("proactive_screen_care", agent.history[-1]["source"])

    async def test_screen_care_publishes_context_before_tts(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"screen_care": {"enabled": True, "speak": True, "max_chars": 42}, "home": {"auto_speak": True, "max_context_messages": 8}}
        agent.history = [{"role": "assistant", "content": "旧代码任务"}]
        agent.mimo_multimodal = SimpleNamespace(
            config={"timeout_seconds": 5},
            analyze_image=AsyncMock(return_value={"text": "主人，记得喝口水哦。", "model": "mimo-v2.5"}),
        )
        agent.log_event = Mock()
        order = []

        async def speak(*_args, **_kwargs):
            order.append("tts")
            self.assertEqual("主人，记得喝口水哦。", agent.history[-1]["content"])
            return ["care.wav"]

        agent._speak_home = speak

        class Image:
            def convert(self, mode): return self
            def save(self, path, kind): Path(path).write_bytes(b"png")

        with patch("PIL.ImageGrab.grab", return_value=Image()):
            result = await agent.proactive_screen_care(lambda _message: order.append("message"))
        self.assertEqual("主人，记得喝口水哦。", result)
        self.assertEqual(["message", "tts"], order)

    async def test_model_screen_tool_forwards_arbitrary_visual_question(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.analyze_current_screen = AsyncMock(return_value={"ok": True, "observation": "答案是 B"})
        result = await agent._run_tool("ui_analyze_screen", {"question": "读取题干和四个选项，计算后给出答案及依据"})
        self.assertTrue(result["ok"])
        agent.analyze_current_screen.assert_awaited_once_with("读取题干和四个选项，计算后给出答案及依据")


if __name__ == "__main__": unittest.main()
