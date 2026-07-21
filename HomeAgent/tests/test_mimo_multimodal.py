import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from home_modules.mimo_multimodal import MiMoMultimodalClient
from agent import HomeAgent


class Response:
    status = 200
    async def text(self): return json.dumps({"choices": [{"message": {"content": "识别结果"}}]})
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class Session:
    def __init__(self): self.payload = None; self.headers = None
    def post(self, url, json, headers): self.payload = json; self.headers = headers; return Response()


class MiMoMultimodalTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_screen_care_deletes_temporary_screenshot(self):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"screen_care": {"enabled": True, "speak": False}, "home": {"auto_speak": True}}
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


if __name__ == "__main__": unittest.main()
