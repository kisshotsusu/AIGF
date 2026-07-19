from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def load_config(path: Path) -> dict[str, Any]:
    load_dotenv(path.parent / ".env")
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_root"] = str(path.parent.resolve())
    room_id = int(cfg.get("app", {}).get("room_id", 0))
    if room_id <= 0:
        raise ValueError("请先在 config.yaml 中设置 app.room_id")
    return cfg


def secret_from_env(name: str | None) -> str:
    return os.getenv(name or "", "").strip()

