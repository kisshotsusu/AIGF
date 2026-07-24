from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class Workspace:
    def __init__(self, root: Path, cfg: dict[str, Any]):
        self.project_root = root
        self.root = root / cfg.get("path", "workspace")
        self.cfg = cfg
        (self.root / cfg.get("memory_dir", "memory")).mkdir(parents=True, exist_ok=True)

    def identity_data(self) -> dict[str, Any]:
        path = self.root / self.cfg.get("identity_file", "IDENTITY.yaml")
        if not path.exists(): return {}
        try:
            import yaml
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError): return {}

    def resolve_user(self, value: str) -> dict[str, Any]:
        """把家庭称呼和直播用户名解析为同一个稳定身份。"""
        source = str(value or "").strip(); user = self.identity_data().get("user", {})
        canonical_name = str(user.get("name", "主人")).strip() or "主人"
        aliases = {canonical_name.casefold()}
        aliases.update(str(x).strip().casefold() for x in user.get("aliases", []) if str(x).strip())
        live_names = {str(x).strip().casefold() for x in user.get("live_usernames", []) if str(x).strip()}
        matched = source.casefold() in aliases | live_names
        return {
            "id": str(user.get("id", "owner")) if matched else f"viewer:{source.casefold()}",
            "name": canonical_name if matched else source,
            "source_username": source,
            "is_owner": matched,
            "matched_as": "live_username" if source.casefold() in live_names else "home_alias" if matched else "viewer",
        }

    def canonical_user(self, value: str) -> str:
        return str(self.resolve_user(value)["name"])

    def normalize_memory_identity(self, item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item); source = str(result.get("user", ""))
        resolved = self.resolve_user(source)
        if resolved["is_owner"]:
            if source and source != resolved["name"]: result.setdefault("source_username", source)
            result["user"] = resolved["name"]; result["user_id"] = resolved["id"]
        return result

    def prompt_documents(self, mode: str | None = None) -> str:
        sections = []
        for key in ("identity_file", "soul_file", "rules_file", "abilities_file", "character_profile_file"):
            filename = self.cfg.get(key)
            if not filename: continue
            path = self.root / filename
            if path.exists():
                sections.append(path.read_text(encoding="utf-8"))
        scene_key = "live_rules_file" if mode == "live" else "home_rules_file" if mode == "home" else None
        if scene_key and self.cfg.get(scene_key):
            scene_path = self.root / self.cfg[scene_key]
            if scene_path.exists(): sections.append(scene_path.read_text(encoding="utf-8"))
        image_dir = self.root / self.cfg.get("character_image_dir", "character_images")
        manifest = image_dir / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                primary = data.get("primary") or "未设置"
                assets = [f"{item.get('filename')}: {item.get('label', '')}" for item in data.get("images", [])]
                sections.append("# 角色形象库索引\n主形象：" + primary + "\n可用形象：\n" + "\n".join(assets))
            except (OSError, json.JSONDecodeError):
                pass
        return "\n\n".join(sections)

    def remember(self, event: dict[str, Any]) -> None:
        if not self.cfg.get("daily_memory", True):
            return
        # 所有入口统一按 IDENTITY.yaml 归一化，避免家庭称呼和直播账号被写成两个人。
        event = self.normalize_memory_identity(event)
        event = {"id": uuid.uuid4().hex, "time": datetime.now().isoformat(timespec="seconds"), **event}
        path = self.root / self.cfg.get("memory_dir", "memory") / f"{datetime.now():%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def recent_memories(self, limit: int, include_private: bool = True) -> list[dict[str, Any]]:
        folder = self.root / self.cfg.get("memory_dir", "memory")
        rows: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.jsonl"), reverse=True)[:7]:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    item = json.loads(line)
                    if include_private or str(item.get("privacy", "shared")) != "private": rows.append(self.normalize_memory_identity(item))
            except (OSError, json.JSONDecodeError):
                continue
        rows.sort(key=lambda item: str(item.get("updated_at") or item.get("time") or ""))
        return rows[-limit:]

    def cleanup_home_chatter(self) -> dict[str, int]:
        """只移除家庭模式全量写入的普通对话，保留重要、手动和隐私记忆。"""
        folder = self.root / self.cfg.get("memory_dir", "memory")
        scanned = removed = 0
        for path in folder.glob("*.jsonl"):
            kept: list[str] = []
            changed = False
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip(): continue
                try: item = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line); continue
                scanned += 1
                source = str(item.get("source", ""))
                disposable = source == "home-auto-all" or (
                    source.startswith("home-") and str(item.get("type", "")) in {"conversation", "reply"}
                    and int(item.get("importance", 0) or 0) < 70
                )
                if disposable:
                    removed += 1; changed = True
                else:
                    kept.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            if changed:
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
                temporary.replace(path)
        return {"scanned": scanned, "removed": removed}
