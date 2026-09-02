"""经验知识库（完成后沉淀，后续可检索复用）。

对应架构需求：「可以根据已完成的东西，整理成为知识库」。用户提到 Weaviate
（weknora）不错，因此本模块提供两类后端：

- `LocalKnowledgeBase`（默认）：SQLite + JSONL 镜像，纯标准库，离线可用，
  BM25 风格关键词 + 时间衰减检索。保证功能在任何环境都能跑起来。
- `WeaviateKnowledgeBase`（可选）：连接 Weaviate 实例，使用 `bm25` 检索
  （无需向量模型即可工作；如需语义检索可在服务端配置 text2vec 模块）。
  weaviate-client 未安装或实例不可达时自动降级到本地。

对外统一接口由 `KnowledgeBase` 抽象类定义，`create_knowledge_base(cfg)`
根据配置实例化：backend=weaviate 且可达时用 Weaviate，否则用 local。

agent 在每次任务成功后调用 `record_completed_task(...)` 沉淀经验，并在规划前
可选地 `search(...)` 复用历史经验。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "KnowledgeEntry",
    "KnowledgeBase",
    "LocalKnowledgeBase",
    "WeaviateKnowledgeBase",
    "create_knowledge_base",
]


@dataclass
class KnowledgeEntry:
    """一条经验知识。"""

    id: str
    goal: str
    approach: str = ""
    outcome: str = ""
    domain: str = ""
    tags: list[str] = field(default_factory=list)
    source_task_id: str = ""
    success: bool = True
    created_at: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at
        return d


_TOKEN_RE = re.compile(r"[一-鿿A-Za-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """中文按字、英文/数字按词切分，便于轻量检索。"""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    tokens: list[str] = []
    buf = []
    for ch in text:
        if ch.isascii() and (ch.isalnum()):
            buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf = []
            if "一" <= ch <= "鿿":
                tokens.append(ch)
    if buf:
        tokens.append("".join(buf))
    return [t for t in tokens if t]


class KnowledgeBase(ABC):
    """知识库统一接口。"""

    @abstractmethod
    def add(self, entry: KnowledgeEntry) -> str:
        ...

    @abstractmethod
    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def count(self) -> int:
        ...

    # ------------------------------------------------------------------ #
    # 便捷封装
    # ------------------------------------------------------------------ #
    def record_completed_task(
        self,
        *,
        goal: str,
        plan: Optional[dict[str, Any]] = None,
        answer: str = "",
        evidence: Optional[list[dict[str, Any]]] = None,
        source_task_id: str = "",
        success: bool = True,
    ) -> str:
        """从一次完成的任务沉淀经验条目。"""
        plan = plan or {}
        domain = str(plan.get("domain") or "").strip()
        steps = [str(s) for s in (plan.get("steps") or []) if str(s).strip()]
        tags = [domain] if domain else []
        tags += steps[:8]
        approach = "；".join(steps)
        # 规划器回退(rule)时 plan.steps 可能为空 -> approach 会是空串, 沉淀出"没有做法"的废条目,
        # 检索命中后既浪费上下文又无法指导复用。这里用真实执行证据兜底生成工具链做法。
        if not approach.strip() and evidence:
            used_tools = [
                str(item.get("tool") or "")
                for item in evidence
                if isinstance(item, dict) and str(item.get("tool") or "").strip()
                and str((item.get("result") or {}).get("status") or "success") not in {"failed", "cancelled"}
            ]
            # 去掉相邻重复, 保留"何时用了什么工具"的节奏
            chain: list[str] = []
            for tool in used_tools:
                if not chain or chain[-1] != tool:
                    chain.append(tool)
            if chain:
                approach = f"执行工具链: {' → '.join(chain)}"
                tags += chain[:6]
        # 失败证据里挑一个原因作为 outcome 补充
        if not success and evidence:
            for item in reversed(evidence):
                err = (item.get("result") or {}).get("error") or (item.get("result") or {}).get("reason")
                if err:
                    answer = f"{answer}\n失败原因：{err}"[:400]
                    break
        entry = KnowledgeEntry(
            id="kb_" + _ts_id(),
            goal=_clip(goal, 600),
            approach=_clip(approach, 1200),
            outcome=_clip(answer, 1200),
            domain=domain,
            tags=[_clip(t, 60) for t in tags if t][:12],
            source_task_id=source_task_id,
            success=success,
        )
        return self.add(entry)


class LocalKnowledgeBase(KnowledgeBase):
    """本地后端：SQLite（检索）+ JSONL（人类可读镜像）。"""

    def __init__(self, db_path: str | Path, jsonl_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else self.db_path.with_suffix(".jsonl")
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge (
                    id TEXT PRIMARY KEY,
                    goal TEXT,
                    approach TEXT,
                    outcome TEXT,
                    domain TEXT,
                    tags TEXT,
                    success INTEGER,
                    source_task_id TEXT,
                    created_at REAL,
                    payload TEXT
                )
                """
            )
            conn.commit()

    def add(self, entry: KnowledgeEntry) -> str:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO knowledge "
                "(id, goal, approach, outcome, domain, tags, success, source_task_id, created_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.id,
                    entry.goal,
                    entry.approach,
                    entry.outcome,
                    entry.domain,
                    json.dumps(entry.tags, ensure_ascii=False),
                    int(bool(entry.success)),
                    entry.source_task_id,
                    entry.created_at,
                    json.dumps(entry.to_payload(), ensure_ascii=False),
                ),
            )
            conn.commit()
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_payload(), ensure_ascii=False) + "\n")
        return entry.id

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        rows: list[tuple] = []
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT * FROM knowledge ORDER BY created_at DESC LIMIT 500"):
                rows.append(row)
        scored: list[tuple[float, dict[str, Any]]] = []
        now = time.time()
        for row in rows:
            text = " ".join(
                str(row[c] or "") for c in ("goal", "approach", "outcome", "domain", "tags")
            )
            doc_tokens = _tokenize(text)
            score = self._overlap(q_tokens, doc_tokens)
            if score <= 0:
                continue
            # 时间衰减：越近权重略高
            age_days = max(0.0, (now - float(row["created_at"])) / 86400.0)
            recency = 1.0 / (1.0 + 0.05 * age_days)
            final = score * recency
            payload = json.loads(row["payload"]) if row["payload"] else {}
            payload["score"] = round(final, 4)
            scored.append((final, payload))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored[:k]]

    @staticmethod
    def _overlap(q_tokens: list[str], doc_tokens: list[str]) -> float:
        if not doc_tokens:
            return 0.0
        doc_set = set(doc_tokens)
        hit = sum(1 for t in set(q_tokens) if t in doc_set)
        return float(hit) / max(1, len(set(q_tokens)))

    def count(self) -> int:
        with sqlite3.connect(str(self.db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]


class WeaviateKnowledgeBase(KnowledgeBase):
    """Weaviate 后端（可选）。使用 bm25 检索，无需向量模型。

    需安装 `weaviate-client>=4` 且实例可达；否则构造函数抛异常，
    由工厂回退到本地后端。
    """

    def __init__(self, url: str, api_key: str = "", class_name: str = "AgentKnowledge", timeout_seconds: int = 20) -> None:
        import weaviate  # 懒加载：未安装则抛 ImportError
        from weaviate.classes.init import Auth
        from weaviate.classes.config import Configure, Property, DataType

        self.class_name = class_name
        host = "127.0.0.1"
        port = 8080
        m = re.match(r"https?://([^:/]+)(?::(\d+))?", str(url or ""))
        if m:
            host = m.group(1)
            if m.group(2):
                port = int(m.group(2))
        auth = Auth.api_key(api_key) if api_key else None
        self.client = weaviate.connect_to_local(
            host=host,
            port=port,
            auth_credentials=auth,
            timeout=timeout_seconds,
        )
        if not self.client.is_ready():
            raise RuntimeError(f"Weaviate 实例 {host}:{port} 未就绪")
        if not self.client.collections.exists(class_name):
            self.client.collections.create(
                name=class_name,
                vectorizer_config=Configure.Vectorizer.none(),
                properties=[
                    Property(name="goal", data_type=DataType.TEXT),
                    Property(name="approach", data_type=DataType.TEXT),
                    Property(name="outcome", data_type=DataType.TEXT),
                    Property(name="domain", data_type=DataType.TEXT),
                    Property(name="tags", data_type=DataType.TEXT_ARRAY),
                    Property(name="source_task_id", data_type=DataType.TEXT),
                    Property(name="success", data_type=DataType.BOOL),
                    Property(name="created_at", data_type=DataType.NUMBER),
                    Property(name="payload", data_type=DataType.TEXT),
                ],
            )
        self._collection = self.client.collections.get(class_name)

    def add(self, entry: KnowledgeEntry) -> str:
        props = {
            "goal": entry.goal,
            "approach": entry.approach,
            "outcome": entry.outcome,
            "domain": entry.domain,
            "tags": entry.tags,
            "source_task_id": entry.source_task_id,
            "success": bool(entry.success),
            "created_at": entry.created_at,
            "payload": json.dumps(entry.to_payload(), ensure_ascii=False),
        }
        self._collection.data.insert(properties=props)
        return entry.id

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        try:
            resp = self._collection.query.bm25(query=query, limit=k)
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for obj in getattr(resp, "objects", []) or []:
            props = obj.properties or {}
            payload = props.get("payload")
            try:
                data = json.loads(payload) if isinstance(payload, str) and payload else {}
            except (TypeError, json.JSONDecodeError):
                data = {}
            if not data:
                data = {k: v for k, v in props.items() if k != "payload"}
            data["score"] = 1.0
            out.append(data)
        return out

    def count(self) -> int:
        try:
            return self._collection.aggregate.over_all(total_count=True).total_count
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


def create_knowledge_base(cfg: dict[str, Any]) -> KnowledgeBase:
    """根据配置构建知识库。backend=weaviate 且可达时用 Weaviate，否则本地。"""
    cfg = cfg or {}
    backend = str(cfg.get("backend", "local")).lower()
    if backend == "weaviate":
        try:
            wcfg = cfg.get("weaviate", {}) or {}
            return WeaviateKnowledgeBase(
                url=wcfg.get("url", "http://127.0.0.1:8080"),
                api_key=wcfg.get("api_key", "") or "",
                class_name=wcfg.get("class_name", "AgentKnowledge"),
                timeout_seconds=int(wcfg.get("timeout_seconds", 20)),
            )
        except Exception as exc:  # 不可达/未安装 -> 降级
            import logging
            logging.getLogger("home_agent.knowledge_base").warning(
                "Weaviate 不可用，降级到本地知识库: %s", exc
            )
    local = cfg.get("local", {}) or {}
    root = Path(__file__).resolve().parents[1]
    db_path = local.get("db_path") or "knowledge_base/knowledge.db"
    jsonl_path = local.get("jsonl_path")
    db_path = db_path if Path(db_path).is_absolute() else root / db_path
    if jsonl_path:
        jsonl_path = jsonl_path if Path(jsonl_path).is_absolute() else root / jsonl_path
    return LocalKnowledgeBase(db_path, jsonl_path)


def _ts_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
