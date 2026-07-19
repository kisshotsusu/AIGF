from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


class SelfUpgradeManager:
    """Persist task recovery and coordinate safe, restartable self-upgrades."""

    TRACKED_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".txt", ".bat", ".cmd", ".ps1"}
    TRACKED_AREAS = ("HomeAgent", "Vision", "Skill", "CharacterManager", "modules", "src")
    RESTART_AREAS = ("HomeAgent/", "Vision/")

    def __init__(self, root: Path, home_agent: Path, config: dict[str, Any]):
        self.root = root.resolve()
        self.home_agent = home_agent.resolve()
        self.config = config.get("self_upgrade", {})
        self.state_dir = self.home_agent / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "task-recovery.json"
        self._baseline: dict[str, str] = {}

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _fingerprint(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for area in self.TRACKED_AREAS:
            folder = self.root / area
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in self.TRACKED_SUFFIXES:
                    continue
                if any(part in {".git", ".venv", "node_modules", "logs", "__pycache__", "models"} for part in path.parts):
                    continue
                if self.home_agent / "state" in path.parents:
                    continue
                try:
                    stat = path.stat()
                    result[path.relative_to(self.root).as_posix()] = hashlib.sha1(
                        f"{stat.st_size}:{stat.st_mtime_ns}".encode()
                    ).hexdigest()
                except OSError:
                    continue
        return result

    @staticmethod
    def is_upgrade_request(prompt: str) -> bool:
        compact = str(prompt).replace(" ", "").lower()
        subjects = ("homeagent", "你自己", "自身", "自我", "程序", "系统")
        actions = ("升级", "更新", "修改", "优化", "修复", "编辑", "增加功能", "重构")
        return any(word in compact for word in subjects) and any(word in compact for word in actions)

    def begin(self, prompt: str, resumed: bool = False) -> None:
        self._baseline = self._fingerprint()
        now = datetime.now().isoformat(timespec="seconds")
        previous = self.read() if resumed else {}
        self._write({
            "version": 1, "status": "running", "prompt": previous.get("prompt") or prompt,
            "is_self_upgrade": self.is_upgrade_request(prompt) or bool(previous.get("is_self_upgrade")),
            "started_at": previous.get("started_at") or now, "updated_at": now,
            "current_step": previous.get("current_step", "正在恢复任务" if resumed else "正在分析任务"),
            "completed_steps": previous.get("completed_steps", []),
            "changed_files": previous.get("changed_files", []),
            "restart_count": int(previous.get("restart_count", 0)),
        })

    def progress(self, current: str, completed: list[str]) -> None:
        state = self.read()
        if state.get("status") != "running":
            return
        state.update({"current_step": str(current), "completed_steps": [str(x) for x in completed[-12:]], "updated_at": datetime.now().isoformat(timespec="seconds")})
        self._write(state)

    def changed_files(self) -> list[str]:
        after = self._fingerprint()
        keys = set(self._baseline) | set(after)
        return sorted(key for key in keys if self._baseline.get(key) != after.get(key))

    def _validate(self, changed: list[str]) -> dict[str, Any]:
        if not self.config.get("require_validation", True):
            return {"ok": True, "skipped": True}
        checked: list[str] = []
        try:
            for relative in changed:
                path = self.root / relative
                if not path.is_file():
                    continue
                if path.suffix.lower() == ".py":
                    py_compile.compile(str(path), doraise=True)
                    checked.append(relative)
                elif path.suffix.lower() == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
                    checked.append(relative)
                elif path.suffix.lower() in {".yaml", ".yml"}:
                    import yaml
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                    checked.append(relative)
            return {"ok": True, "checked": checked}
        except Exception as exc:
            return {"ok": False, "checked": checked, "error": str(exc)}

    def finalize(self, answer: str) -> bool:
        state = self.read()
        changed = self.changed_files()
        state["changed_files"] = sorted(set(state.get("changed_files", [])) | set(changed))
        state["last_answer"] = str(answer)[:4000]
        validation = self._validate(changed)
        state["validation"] = validation
        if not validation.get("ok"):
            state.update({"status": "validation_failed", "updated_at": datetime.now().isoformat(timespec="seconds")})
            self._write(state)
            raise RuntimeError(f"自升级校验失败，已阻止重启：{validation.get('error', 'unknown error')}")
        restart_sensitive = any(path.startswith(self.RESTART_AREAS) and path.endswith(".py") for path in changed)
        should_restart = bool(self.config.get("enabled", True) and self.config.get("auto_restart", True) and state.get("is_self_upgrade") and restart_sensitive and int(state.get("restart_count", 0)) < int(self.config.get("max_restart_attempts", 2)))
        if should_restart:
            state.update({"status": "restart_pending", "current_step": "重启后验证升级并继续任务", "restart_count": int(state.get("restart_count", 0)) + 1, "updated_at": datetime.now().isoformat(timespec="seconds")})
            self._write(state)
            return True
        state.update({"status": "completed", "updated_at": datetime.now().isoformat(timespec="seconds")})
        self._write(state)
        return False

    def cancel(self) -> None:
        state = self.read()
        state.update({"status": "cancelled", "updated_at": datetime.now().isoformat(timespec="seconds")})
        self._write(state)

    def resume_prompt(self) -> str:
        state = self.read()
        if state.get("status") not in {"running", "restart_pending"} or not str(state.get("prompt", "")).strip():
            return ""
        original = str(state["prompt"]).strip()
        completed = "；".join(state.get("completed_steps") or []) or "暂无可靠完成记录"
        changed = "、".join(state.get("changed_files") or []) or "暂无"
        state.update({"status": "running", "updated_at": datetime.now().isoformat(timespec="seconds")})
        self._write(state)
        return f"这是重启或异常退出后自动恢复的未完成任务。继续执行原任务，不要重复已经完成的步骤。\n原任务：{original}\n已完成阶段：{completed}\n已变更文件：{changed}\n先验证当前程序和变更是否正常，再完成剩余工作；只有全部验证通过后才能结束。"

    def launch_restart_watchdog(self, current_pid: int) -> None:
        script = self.home_agent / "restart_watchdog.py"
        pythonw = self.root / ".venv" / "Scripts" / "pythonw.exe"
        python = self.root / ".venv" / "Scripts" / "python.exe"
        executable = python if python.is_file() else Path(sys.executable)
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
        subprocess.Popen([str(executable), str(script), str(current_pid), str(pythonw if pythonw.is_file() else sys.executable), str(self.home_agent / "app.py")], cwd=str(self.home_agent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
