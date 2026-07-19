from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from modules.live.ai_live_assistant.app import LiveAssistant
from modules.live.ai_live_assistant.tts import TTSClient


class ReliableSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_retries_transient_gpu_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            client = TTSClient(object(), {"retry_attempts": 4, "retry_delay_seconds": 0.001}, Path(folder))
            calls = 0

            async def synthesize_once(text: str) -> Path:
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise TimeoutError("GPU busy")
                return Path(folder) / "ok.wav"

            client._synthesize_once = synthesize_once  # type: ignore[method-assign]
            result = await client.synthesize("欢迎")
            self.assertEqual(Path(folder) / "ok.wav", result)
            self.assertEqual(3, calls)

    async def test_welcome_cooldown_is_committed_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cfg = {
                "_root": folder,
                "reply": {"max_context_messages": 4, "ignore_masked_usernames": True},
                "bilibili": {"welcome_cooldown_seconds": 18, "welcome_template": "欢迎 {username}！"},
            }
            assistant = LiveAssistant(cfg)
            results = iter((False, True))

            async def emit(text: str, speech_priority: int = 10) -> bool:
                return next(results)

            assistant._emit = emit  # type: ignore[method-assign]
            await assistant._welcome("42", "测试用户")
            self.assertNotIn("42", assistant.welcomed)
            await assistant._welcome("42", "测试用户")
            self.assertIn("42", assistant.welcomed)

    async def test_duplicate_entry_events_share_one_pending_welcome(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cfg = {
                "_root": folder,
                "reply": {"max_context_messages": 4, "ignore_masked_usernames": True},
                "bilibili": {"welcome_cooldown_seconds": 18, "welcome_template": "欢迎 {username}！"},
            }
            assistant = LiveAssistant(cfg)
            release = asyncio.Event()
            calls = 0

            async def emit(text: str, speech_priority: int = 10) -> bool:
                nonlocal calls
                calls += 1
                await release.wait()
                return True

            assistant._emit = emit  # type: ignore[method-assign]
            first = asyncio.create_task(assistant._welcome("42", "测试用户"))
            await asyncio.sleep(0)
            second = asyncio.create_task(assistant._welcome("42", "测试用户"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
