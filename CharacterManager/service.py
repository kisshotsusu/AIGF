"""UI-independent data service for the character manager.

Both the Qt and legacy Tk frontends can use this module.  All writes are atomic,
and mapping updates preserve settings unknown to the UI.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
CONFIG = ROOT / "config.yaml"
HOME_CONFIG = ROOT / "HomeAgent" / "config.yaml"
IDENTITY = WORKSPACE / "IDENTITY.yaml"
PROFILE = WORKSPACE / "CHARACTER.md"
MEMORY = WORKSPACE / "memory"
IMAGES = WORKSPACE / "character_images"
MANIFEST = IMAGES / "manifest.json"
ENV_FILE = ROOT / ".env"
MCP_CONFIG = WORKSPACE / "MCP_SERVERS.yaml"
TOOL_CONFIG_DIR = ROOT / "HomeAgent" / "config.d"
TOOL_DOCUMENTS = {
    ("computer_control", True): TOOL_CONFIG_DIR / "computer_control.yaml",
    ("vision_mcp", True): TOOL_CONFIG_DIR / "vision_mcp.yaml",
    ("context_maintenance", True): TOOL_CONFIG_DIR / "context_maintenance.yaml",
    ("context_cleanup", False): TOOL_CONFIG_DIR / "live_context_cleanup.yaml",
}

DOCUMENTS = {
    "灵魂与人格": WORKSPACE / "SOUL.md",
    "通用安全规则": WORKSPACE / "RULES.md",
    "直播模式规则": WORKSPACE / "LIVE_RULES.md",
    "家庭模式规则": WORKSPACE / "HOME_RULES.md",
    "能力文档": WORKSPACE / "ABILITIES.md",
    "家庭场景": WORKSPACE / "HOME.md",
}


class CharacterServiceError(RuntimeError):
    pass


class CharacterService:
    """Stable interface between persistent character data and any GUI."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.lock = threading.RLock()
        for path in (WORKSPACE, MEMORY, IMAGES, TOOL_CONFIG_DIR):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _read_yaml(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return deepcopy(default)
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            return value if value is not None else deepcopy(default)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + f".broken-{stamp}.bak"))
            except OSError:
                pass
            raise CharacterServiceError(f"无法读取 {path.name}，已保留损坏文件备份：{exc}") from exc

    def _write_yaml(self, path: Path, value: Any) -> None:
        self._atomic_text(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return deepcopy(default)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CharacterServiceError(f"无法读取 {path.name}：{exc}") from exc

    def _write_json(self, path: Path, value: Any) -> None:
        self._atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def load_identity(self) -> dict[str, Any]:
        with self.lock:
            data = self._read_yaml(IDENTITY, {"character": {}, "user": {}, "notes": ""})
            data["profile"] = PROFILE.read_text(encoding="utf-8") if PROFILE.exists() else "# 角色形象说明\n"
            return data

    def save_identity(self, data: dict[str, Any]) -> None:
        with self.lock:
            stored = {k: deepcopy(v) for k, v in data.items() if k != "profile"}
            self._write_yaml(IDENTITY, stored)
            if "profile" in data:
                self._atomic_text(PROFILE, str(data.get("profile", "")).rstrip() + "\n")

    def load_appearance_profile(self) -> str:
        return PROFILE.read_text(encoding="utf-8") if PROFILE.exists() else "# 角色形象说明\n"

    def save_appearance_profile(self, text: str) -> None:
        with self.lock:
            self._atomic_text(PROFILE, text.rstrip() + "\n")

    def list_documents(self) -> list[str]:
        return list(DOCUMENTS)

    def load_document(self, name: str) -> str:
        path = DOCUMENTS.get(name)
        if path is None:
            raise CharacterServiceError("未知文档")
        return path.read_text(encoding="utf-8") if path.exists() else f"# {name}\n"

    def save_document(self, name: str, text: str) -> None:
        path = DOCUMENTS.get(name)
        if path is None:
            raise CharacterServiceError("未知文档")
        with self.lock:
            self._atomic_text(path, text.rstrip() + "\n")

    def list_memories(self, query: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(MEMORY.glob("*.jsonl"), reverse=True):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for index, line in enumerate(lines):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    item = deepcopy(item)
                    item["_file"], item["_index"] = str(path), index
                    item["_key"] = str(item.get("id") or f"{path.name}:{index}")
                    rows.append(item)
        rows.sort(key=lambda x: str(x.get("updated_at") or x.get("time") or ""), reverse=True)
        needle = query.strip().lower()
        return [x for x in rows if not needle or needle in json.dumps(x, ensure_ascii=False, default=str).lower()]

    def save_memory(self, record: dict[str, Any], original: dict[str, Any] | None = None) -> str:
        with self.lock:
            clean = {k: deepcopy(v) for k, v in record.items() if not k.startswith("_")}
            now = datetime.now()
            if original:
                clean.setdefault("id", original.get("id"))
                clean.setdefault("time", original.get("time"))
                clean["updated_at"] = now.isoformat(timespec="seconds")
                self._replace_memory(original, clean)
            else:
                clean.setdefault("id", uuid.uuid4().hex)
                clean.setdefault("time", now.isoformat(timespec="seconds"))
                clean.setdefault("source", "character-manager-qt")
                path = MEMORY / f"{now:%Y-%m-%d}.jsonl"
                previous = path.read_text(encoding="utf-8") if path.exists() else ""
                self._atomic_text(path, previous + json.dumps(clean, ensure_ascii=False) + "\n")
            return str(clean["id"])

    def delete_memory(self, item: dict[str, Any]) -> None:
        with self.lock:
            self._replace_memory(item, None)

    def _replace_memory(self, item: dict[str, Any], replacement: dict[str, Any] | None) -> None:
        path = Path(str(item.get("_file", "")))
        index = int(item.get("_index", -1))
        if not path.is_file():
            raise CharacterServiceError("记忆文件已不存在")
        lines = path.read_text(encoding="utf-8").splitlines()
        if index < 0 or index >= len(lines):
            raise CharacterServiceError("记忆位置已变化，请刷新后重试")
        expected_id = item.get("id")
        if expected_id:
            try:
                current_id = json.loads(lines[index]).get("id")
            except (json.JSONDecodeError, AttributeError):
                current_id = None
            if current_id != expected_id:
                matches = []
                for candidate, line in enumerate(lines):
                    try:
                        if json.loads(line).get("id") == expected_id:
                            matches.append(candidate)
                    except (json.JSONDecodeError, AttributeError):
                        pass
                if len(matches) != 1:
                    raise CharacterServiceError("记忆文件已被其它进程修改，请刷新后重试")
                index = matches[0]
        if replacement is None:
            lines.pop(index)
        else:
            lines[index] = json.dumps(replacement, ensure_ascii=False)
        self._atomic_text(path, "\n".join(lines) + ("\n" if lines else ""))

    def get_config_section(self, section: str, home: bool = False) -> dict[str, Any]:
        if section == "__mcp__":
            return self.load_mcp_servers()
        path = HOME_CONFIG if home else CONFIG
        data = self._read_yaml(path, {})
        value = data.get(section, {})
        document = TOOL_DOCUMENTS.get((section, home))
        if document:
            if document.exists():
                if path.exists() and path.stat().st_mtime_ns > document.stat().st_mtime_ns:
                    value = value if isinstance(value, dict) else {}
                    self._write_yaml(document, value)
                else:
                    value = self._read_yaml(document, value if isinstance(value, dict) else {})
                    if data.get(section) != value:
                        data[section] = deepcopy(value)
                        self._write_yaml(path, data)
            else:
                self._write_yaml(document, value if isinstance(value, dict) else {})
        return deepcopy(value if isinstance(value, dict) else {})

    def save_config_section(self, section: str, value: dict[str, Any], home: bool = False) -> None:
        if section == "__mcp__":
            self.save_mcp_servers(value)
            return
        path = HOME_CONFIG if home else CONFIG
        with self.lock:
            data = self._read_yaml(path, {})
            data[section] = deepcopy(value)
            self._write_yaml(path, data)
            document = TOOL_DOCUMENTS.get((section, home))
            if document:
                self._write_yaml(document, value)

    def read_env(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    result[key.strip()] = value.strip()
        return result

    def save_secret(self, key: str, value: str) -> None:
        if not key or not value:
            return
        with self.lock:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
            for i, line in enumerate(lines):
                if line.startswith(key + "="):
                    lines[i] = f"{key}={value}"
                    break
            else:
                lines.append(f"{key}={value}")
            self._atomic_text(ENV_FILE, "\n".join(lines) + "\n")

    def list_images(self) -> tuple[str | None, list[dict[str, Any]]]:
        data = self._read_json(MANIFEST, {"primary": None, "images": []})
        return data.get("primary"), deepcopy(data.get("images", []))

    def image_path(self, item: dict[str, Any]) -> Path:
        path = Path(str(item.get("filename") or item.get("path") or ""))
        return path if path.is_absolute() else IMAGES / path

    def add_image(self, source: Path) -> str:
        source = Path(source)
        if not source.is_file():
            raise CharacterServiceError("图片文件不存在")
        with self.lock:
            primary, images = self.list_images()
            image_id = uuid.uuid4().hex
            filename = image_id + source.suffix.lower()
            shutil.copy2(source, IMAGES / filename)
            images.append({"id": image_id, "filename": filename, "original_name": source.name,
                           "label": source.stem, "tags": [], "created_at": datetime.now().isoformat(timespec="seconds")})
            self._write_json(MANIFEST, {"primary": primary or image_id, "images": images})
            return image_id

    def set_primary_image(self, image_id: str) -> None:
        with self.lock:
            _, images = self.list_images()
            if not any(x.get("id") == image_id for x in images):
                raise CharacterServiceError("图片不存在")
            self._write_json(MANIFEST, {"primary": image_id, "images": images})

    def delete_image(self, image_id: str) -> None:
        with self.lock:
            primary, images = self.list_images()
            target = next((x for x in images if x.get("id") == image_id), None)
            if not target:
                return
            path = self.image_path(target)
            if path.is_file() and path.parent.resolve() == IMAGES.resolve():
                path.unlink()
            images = [x for x in images if x.get("id") != image_id]
            self._write_json(MANIFEST, {"primary": (images[0].get("id") if images else None) if primary == image_id else primary, "images": images})

    def load_mcp_servers(self) -> dict[str, Any]:
        return self._read_yaml(MCP_CONFIG, {}).get("mcpServers", {}) if MCP_CONFIG.exists() else {}

    def save_mcp_servers(self, servers: dict[str, Any]) -> None:
        self._write_yaml(MCP_CONFIG, {"mcpServers": servers})
