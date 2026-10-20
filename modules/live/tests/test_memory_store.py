import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from modules.live.ai_live_assistant.long_term_memory import LongTermMemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LongTermMemoryStore(self.temp.name)
        self.record = dict(tags=["owner", "preference", "饮食"], summary="饮食偏好",
                           detail="喜欢不加糖的咖啡", category="preference", importance=80)

    def test_privacy_and_user_filters(self):
        self.store.store(**self.record)
        self.assertEqual(self.store.retrieve(["咖啡"], scene="live"), [])
        self.assertEqual(self.store.retrieve(["咖啡"], user_id="other"), [])
        self.assertEqual(len(self.store.retrieve(["咖啡"])), 1)
        self.store.store(**self.record, privacy="public")
        self.assertEqual(len(self.store.retrieve(["咖啡"], scene="live")), 1)

    def test_truncated_detail_is_deduplicated(self):
        value = {**self.record, "detail": "很长的事实" * 500}
        self.store.store(**value)
        self.assertTrue(self.store.store(**value)["duplicate"])
        self.assertEqual(self.store.count(), 1)

    def test_model_decision_not_overridden_by_greeting_substring(self):
        self.store.store(**{**self.record, "detail": "你好，我对花生过敏，这是长期健康信息。", "category": "health"})
        self.assertEqual(self.store.count(), 1)

    def test_strict_importance(self):
        for score in (True, "80", 69, 101):
            with self.assertRaises(ValueError):
                self.store.store(**{**self.record, "importance": score})

    def test_old_memory_remains_searchable_beyond_1000_rows(self):
        self.store.store(**self.record)
        with closing(self.store._connect()) as db:
            for i in range(1001):
                db.execute("INSERT INTO memories SELECT ?, '2099-01-01', user_id, scene, category, tags, 'other', 'other', importance, privacy, source FROM memories LIMIT 1", (str(i),))
            db.commit()
        self.assertEqual(len(self.store.retrieve(["咖啡"])), 1)

    def test_migration_skips_malformed_record_and_continues(self):
        folder = Path(self.temp.name) / "legacy"
        folder.mkdir()
        (folder / "day.jsonl").write_text('[]\n{"importance":"oops"}\n', encoding="utf-8")
        result = self.store.migrate_legacy(folder)
        self.assertEqual(result["skipped"], 2)
