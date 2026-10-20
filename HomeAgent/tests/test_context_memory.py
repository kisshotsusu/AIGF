import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from home_modules.context_memory import compact_prefix, select_prefix


class ContextMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "summary.md"
        self.path.write_text("previous", encoding="utf-8")
        self.history = [{"role": role, "content": str(i)} for i in range(4)
                        for role in ("user", "assistant")]

    async def test_commits_before_removing_and_keeps_recent_turn(self):
        count = await compact_prefix(self.history, self.path, AsyncMock(return_value="merged"), keep_recent=2)
        self.assertEqual(count, 6)
        self.assertEqual(len(self.history), 2)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "merged\n")

    async def test_empty_summary_keeps_original(self):
        before = list(self.history)
        with self.assertRaises(ValueError):
            await compact_prefix(self.history, self.path, AsyncMock(return_value=""), keep_recent=2)
        self.assertEqual(self.history, before)
        self.assertEqual(self.path.read_text(), "previous")

    async def test_write_failure_keeps_history(self):
        before = list(self.history)
        with patch("home_modules.context_memory.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await compact_prefix(self.history, self.path, AsyncMock(return_value="merged"), keep_recent=2)
        self.assertEqual(self.history, before)
        self.assertEqual(self.path.read_text(), "previous")

    async def test_concurrent_append_survives(self):
        async def summarize(*_):
            self.history.append({"role": "user", "content": "new"})
            return "merged"
        await compact_prefix(self.history, self.path, summarize, keep_recent=2)
        self.assertEqual(len(self.history), 3)
        self.assertEqual(self.history[-1]["content"], "new")

    async def test_concurrent_prefix_change_does_not_commit(self):
        async def summarize(*_):
            self.history[0]["content"] = "changed"
            return "merged"
        with self.assertRaises(RuntimeError):
            await compact_prefix(self.history, self.path, summarize, keep_recent=2)
        self.assertEqual(len(self.history), 8)
        self.assertEqual(self.path.read_text(), "previous")

    def test_never_splits_turn_or_silently_truncates_large_message(self):
        self.assertEqual(select_prefix(self.history, 3, 24000), self.history[:4])
        self.assertEqual(select_prefix(self.history, 2, 1), [])

    async def test_long_summary_rejected(self):
        with self.assertRaises(ValueError):
            await compact_prefix(self.history, self.path, AsyncMock(return_value="x" * 100),
                                 keep_recent=2, max_summary_chars=20)
        self.assertEqual(len(self.history), 8)
