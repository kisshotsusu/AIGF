"""Bounded context compaction, independent of the UI and model provider."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def select_prefix(history: list[dict], keep_recent: int, max_chars: int) -> list[dict]:
    """Select a bounded old prefix without splitting a user/assistant turn."""
    end = max(0, len(history) - max(0, keep_recent))
    while end and end < len(history) and history[end].get("role") != "user":
        end -= 1
    selected = []
    size = 0
    for item in history[:end]:
        length = len(json.dumps(item, ensure_ascii=False))
        if size + length > max_chars:
            break
        selected.append(dict(item))
        size += length
    while selected and len(selected) < len(history) and history[len(selected)].get("role") != "user":
        selected.pop()
    return selected


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def compact_prefix(history, summary_path, summarize, *, keep_recent=8,
                         max_input_chars=24000, max_summary_chars=6000):
    """Commit a valid summary before removing its unchanged source prefix.

    Model errors, empty/truncated summaries and concurrent prefix changes leave
    both the prior summary and source messages intact. New appended turns survive.
    """
    snapshot = select_prefix(history, keep_recent, max_input_chars)
    if not snapshot:
        return 0
    previous = summary_path.read_text(encoding="utf-8").strip() if summary_path.exists() else ""
    if len(previous) > max_summary_chars:
        raise ValueError("Existing context summary exceeds max_summary_chars; increase the budget")
    summary = await summarize(previous, snapshot)
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Context summarizer returned an empty summary")
    summary = summary.strip()
    if len(summary) > max_summary_chars:
        raise ValueError("Context summary exceeds max_summary_chars")
    if history[:len(snapshot)] != snapshot:
        raise RuntimeError("Context changed during compaction; retry without deleting messages")
    atomic_write(summary_path, summary + "\n")
    del history[:len(snapshot)]
    return len(snapshot)
