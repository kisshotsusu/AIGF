from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any


HIGH_VALUE_CATEGORIES = {"health", "emotion", "major_event", "preference", "habit", "relationship", "agreement"}
TRIVIAL_PHRASES = {"今天天气不错", "天气很好", "哈哈", "呵呵", "早上好", "晚上好", "你好", "在吗"}


class LongTermMemoryStore:
    """SQLite-backed private memory loaded only through tag retrieval."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "memory.db"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    importance INTEGER NOT NULL,
                    privacy TEXT NOT NULL,
                    source TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)")
            db.commit()

    @staticmethod
    def _tags(values: list[Any]) -> list[str]:
        result = []
        for value in values:
            tag = str(value).strip().lstrip("#")[:24]
            if tag and tag not in result: result.append(tag)
        return result

    def store(self, *, tags: list[Any], summary: str, detail: str, category: str, importance: int = 80, user_id: str = "owner", scene: str = "home", privacy: str = "private", source: str = "home-agent") -> dict[str, Any]:
        tags = self._tags(tags); summary = str(summary).strip(); detail = str(detail).strip(); category = str(category).strip()
        if category not in HIGH_VALUE_CATEGORIES: raise ValueError("只允许存储身体、情绪、重大事件、偏好习惯、关系或约定等高价值记忆")
        if not 3 <= len(tags) <= 5: raise ValueError("长期记忆必须包含 3-5 个有效标签")
        if not summary or len(summary) > 20: raise ValueError("summary 必须为 1-20 个字符")
        if not detail: raise ValueError("detail 必须保留原文关键句")
        if int(importance) < 70: raise ValueError("低价值内容不进入长期数据库")
        normalized = detail.replace(" ", "")
        if any(phrase in normalized for phrase in TRIVIAL_PHRASES) or (len(normalized) <= 4 and category not in {"health", "emotion"}):
            raise ValueError("普通寒暄或闲聊不进入长期数据库")
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, closing(self._connect()) as db:
            duplicate = db.execute("SELECT * FROM memories WHERE user_id=? AND summary=? AND detail=? ORDER BY created_at DESC LIMIT 1", (user_id, summary, detail)).fetchone()
            if duplicate: return {**dict(duplicate), "tags": json.loads(duplicate["tags"]), "duplicate": True}
            record = {
                "id": uuid.uuid4().hex, "created_at": now, "user_id": user_id, "scene": scene,
                "category": category, "tags": tags, "summary": summary, "detail": detail[:2000],
                "importance": max(70, min(100, int(importance))), "privacy": privacy, "source": source,
            }
            db.execute(
                "INSERT INTO memories(id,created_at,user_id,scene,category,tags,summary,detail,importance,privacy,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], record["created_at"], user_id, scene, category, json.dumps(tags, ensure_ascii=False), summary, record["detail"], record["importance"], privacy, source),
            )
            db.commit()
        return record

    def retrieve(self, query_tags: list[Any], limit: int = 8, user_id: str = "owner") -> list[dict[str, Any]]:
        query = self._tags(query_tags)
        if not query: raise ValueError("query_tags 不能为空")
        with self._lock, closing(self._connect()) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM memories WHERE user_id=? ORDER BY created_at DESC LIMIT 1000", (user_id,))]
        ranked = []
        for row in rows:
            tags = json.loads(row["tags"]); haystack = (row["summary"] + "\n" + row["detail"]).casefold()
            overlap = sum(1 for q in query if any(q.casefold() in tag.casefold() or tag.casefold() in q.casefold() for tag in tags))
            text_hits = sum(1 for q in query if q.casefold() in haystack)
            score = overlap * 10 + text_hits * 3
            if score: ranked.append((score, row["created_at"], {**row, "tags": tags, "match_score": score}))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in ranked[:max(1, min(20, int(limit)))]]

    def count(self) -> int:
        with closing(self._connect()) as db: return int(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def migrate_legacy(self, memory_dir: str | Path) -> dict[str, int]:
        """Idempotently copy high-value JSONL memories into SQLite."""
        folder = Path(memory_dir)
        result = {"scanned": 0, "stored": 0, "duplicates": 0, "skipped": 0}
        if not folder.is_dir(): return result
        category_map = {
            "identity": "relationship", "event": "major_event", "conversation": "major_event",
            "preference": "preference", "habit": "habit", "health": "health",
            "emotion": "emotion", "relationship": "relationship", "agreement": "agreement",
            "major_event": "major_event",
        }
        for path in sorted(folder.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try: item = json.loads(line)
                except json.JSONDecodeError:
                    result["skipped"] += 1; continue
                result["scanned"] += 1
                importance = int(item.get("importance", 0) or 0)
                category = category_map.get(str(item.get("category") or item.get("type") or ""))
                detail = str(item.get("original_message") or item.get("message") or item.get("content") or "").strip()
                if importance < 70 or not category or not detail:
                    result["skipped"] += 1; continue
                summary = str(item.get("content") or detail).strip()[:20]
                user_id = str(item.get("user_id") or item.get("user") or "owner")
                user = str(item.get("user") or user_id)
                tags = self._tags(item.get("tags") or [user, category, "长期记忆"])
                while len(tags) < 3: tags.append(("重要信息", "历史记录", "用户记忆")[len(tags)])
                try:
                    record = self.store(
                        tags=tags[:5], summary=summary, detail=detail, category=category,
                        importance=importance, user_id=user_id, scene="home" if user_id == "owner" else "live",
                        privacy=str(item.get("privacy") or ("private" if user_id == "owner" else "public")),
                        source=f"legacy-migration:{item.get('source', 'workspace-memory')}",
                    )
                    key = "duplicates" if record.get("duplicate") else "stored"
                    result[key] += 1
                except (TypeError, ValueError, OSError):
                    result["skipped"] += 1
        return result
