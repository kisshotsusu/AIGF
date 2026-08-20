import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent import HomeAgent
from home_modules.video_understanding import (
    QwenVideoClient,
    normalize_event,
    parse_events,
    parse_time,
)


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def text(self):
        return json.dumps(self.payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, payload=None):
        self.payload = None
        self.headers = None
        self.response_payload = payload or {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "events": [
                            {"start_time": "00:00:00", "end_time": "00:00:02", "event": "开头"},
                            {"start_time": "00:00:02", "end_time": "00:00:05", "event": "中间"},
                        ]
                    })
                }
            }]
        }

    def post(self, url, json, headers):
        self.payload = json
        self.headers = headers
        return Response(self.response_payload)


class VideoTimeTests(unittest.TestCase):
    def test_parse_time_accepts_hhmmss_minutes_and_seconds(self):
        self.assertEqual(parse_time("00:00:03"), 3.0)
        self.assertEqual(parse_time("01:02:03"), 3723.0)
        self.assertEqual(parse_time("01:02"), 62.0)
        self.assertEqual(parse_time("12.5"), 12.5)
        self.assertEqual(parse_time(7), 7.0)
        self.assertEqual(parse_time("00:00:02.500"), 2.5)

    def test_parse_time_rejects_invalid_values(self):
        self.assertIsNone(parse_time("abc"))
        self.assertIsNone(parse_time("25:99"))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))
        self.assertIsNone(parse_time(True))

    def test_normalize_event_fills_text_and_default_duration(self):
        event = normalize_event({"start_time": "00:00:01"}, 0)
        self.assertEqual(event["start_seconds"], 1.0)
        self.assertEqual(event["end_seconds"], 6.0)
        self.assertEqual(event["event"], "片段 1")
        self.assertEqual(event["start_time"], "00:00:01")


class ParseEventsTests(unittest.TestCase):
    def test_parse_events_accepts_array_shape(self):
        events = parse_events(
            '[{"start_time":"00:00:00","end_time":"00:00:05","event":"A"},'
            '{"start_time":"00:00:05","end_time":"00:00:10","event":"B"}]'
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["start_seconds"], 0.0)
        self.assertEqual(events[0]["event"], "A")
        self.assertEqual(events[1]["end_time"], "00:00:10")

    def test_parse_events_accepts_dict_with_events_key(self):
        events = parse_events('{"events":[{"start":"0","end":"2","text":"你好"}]}')
        self.assertEqual(events[0]["event"], "你好")
        self.assertEqual(events[0]["end_seconds"], 2.0)

    def test_parse_events_accepts_markdown_fenced_json(self):
        events = parse_events(
            '```json\n{"events":[{"start_time":"00:00:00","end_time":"00:00:01","event":"x"}]}\n```'
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "x")

    def test_parse_events_missing_end_uses_default_duration(self):
        events = parse_events('{"events":[{"start_time":"00:00:01","event":"A"}]}', 4)
        self.assertEqual(events[0]["end_seconds"], 4.0)

    def test_parse_events_non_json_returns_empty(self):
        self.assertEqual(parse_events("视频里是猫在玩球。"), [])

    def test_parse_events_falls_back_to_timeline_lines(self):
        events = parse_events(
            "00:00:00 - 00:00:05 猫在玩球\n"
            "00:00:05 - 00:00:08 猫喝水"
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "猫在玩球")
        self.assertEqual(events[1]["start_seconds"], 5.0)


class QwenVideoClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_builds_official_payload_and_parses_events(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"DASHSCOPE_API_KEY": "secret"}):
            video = Path(directory) / "demo.mp4"
            video.write_bytes(b"fake-video")
            session = Session()
            result = await QwenVideoClient({"provider": "qwen"}).analyze(session, video, "按事件分析")
            content = session.payload["messages"][0]["content"]
            self.assertEqual(content[0]["type"], "video_url")
            self.assertTrue(content[0]["video_url"]["url"].startswith("data:video/mp4;base64,"))
            self.assertEqual(content[0]["fps"], 2.0)
            self.assertEqual(content[1]["text"], "按事件分析")
            self.assertEqual(session.headers["Authorization"], "Bearer secret")
            self.assertEqual(result["provider"], "qwen")
            self.assertEqual(result["event_count"], 2)
            self.assertEqual(result["events"][0]["event"], "开头")

    async def test_analyze_mimo_uses_api_key_header_and_thinking_disabled(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MIMO_API_KEY": "secret"}):
            video = Path(directory) / "demo.mp4"
            video.write_bytes(b"fake-video")
            session = Session()
            result = await QwenVideoClient({"provider": "mimo"}).analyze(session, video, "按事件分析")
            content = session.payload["messages"][0]["content"]
            self.assertEqual(content[0]["type"], "video_url")
            self.assertEqual(content[0]["fps"], 2.0)
            self.assertEqual(session.headers["api-key"], "secret")
            self.assertNotIn("Authorization", session.headers)
            self.assertIn("max_completion_tokens", session.payload)
            self.assertEqual(session.payload["thinking"], {"type": "disabled"})
            self.assertEqual(session.payload["model"], "mimo-v2.5")
            self.assertEqual(result["provider"], "mimo")

    async def test_analyze_falls_back_from_mimo_to_qwen_when_first_provider_fails(self):
        class FlakySession(Session):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def post(self, url, json, headers):
                self.calls += 1
                if self.calls == 1:
                    return Response({"error": "boom"}, status=400)
                self.payload = json
                self.headers = headers
                return Response(self.response_payload)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"MIMO_API_KEY": "bad", "DASHSCOPE_API_KEY": "good"},
        ):
            video = Path(directory) / "demo.mp4"
            video.write_bytes(b"fake-video")
            session = FlakySession()
            result = await QwenVideoClient(
                {"provider": "mimo", "fallback_provider": "qwen"}
            ).analyze(session, video, "按事件分析")
            self.assertEqual(result["provider"], "qwen")
            self.assertEqual(session.headers["Authorization"], "Bearer good")

    async def test_analyze_rejects_missing_api_key(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
                await QwenVideoClient({"provider": "qwen"}).analyze(Session(), "https://example.com/video.mp4")

    def test_local_video_size_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "big.mp4"
            video.write_bytes(b"x" * 2048)
            client = QwenVideoClient({"max_video_mb": 0.001})
            with self.assertRaisesRegex(ValueError, "超过限制"):
                client._resolve_video(video)

    def test_remote_url_is_passed_through_without_encoding(self):
        url, local = QwenVideoClient()._resolve_video("https://example.com/clip.mp4")
        self.assertEqual(url, "https://example.com/clip.mp4")
        self.assertIsNone(local)


class ToolSurfaceTests(unittest.TestCase):
    def test_qwen_video_tools_exposed_when_enabled(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"agent": {}, "vision_mcp": {"enabled": False}}
        agent.comfyui = None
        agent.cosyvoice = None
        agent.qwen_video = SimpleNamespace(config={"enabled": True})
        names = [tool["function"]["name"] for tool in agent._tools(scoped=True)]
        for expected in (
            "qwen_analyze_video",
            "video_cut_segments",
            "video_concat_segments",
            "video_add_subtitles",
            "video_generate_voiceover",
        ):
            self.assertIn(expected, names)

    def test_qwen_video_tools_hidden_when_disabled(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"agent": {}, "vision_mcp": {"enabled": False}}
        agent.comfyui = None
        agent.cosyvoice = None
        agent.qwen_video = SimpleNamespace(config={"enabled": False})
        names = [tool["function"]["name"] for tool in agent._tools(scoped=True)]
        self.assertNotIn("qwen_analyze_video", names)
        self.assertNotIn("video_cut_segments", names)
if __name__ == "__main__":
    unittest.main()
