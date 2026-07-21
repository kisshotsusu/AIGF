import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from home_modules.mimo_multimodal import MiMoMultimodalClient


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


if __name__ == "__main__": unittest.main()
