from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from home_modules.code_editor import CodeEditorModule
except ModuleNotFoundError:  # Package-style imports used by tests and tooling.
    from HomeAgent.home_modules.code_editor import CodeEditorModule


class SelfUpgradeManager:
    """Persist task recovery and coordinate safe, restartable self-upgrades."""

    RESTART_AREAS = ("HomeAgent/", "Vision/")

    def __init__(self, root: Path, home_agent: Path, config: dict[str, Any]):
        self.root = root.resolve()
        self.home_agent = home_agent.resolve()
        self.config = config.get("self_upgrade", {})
        self.state_dir = self.home_agent / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "task-recovery.json"
        control = config.get("computer_control", {})
        self.code_editor = CodeEditorModule(
            self.root, self.home_agent, self.config.get("require_validation", True),
            allow_external_read=bool(control.get("full_access", False)),
            external_read_roots=control.get("allowed_roots", []),
            allow_external_write=bool(control.get("full_access", False)),
        )

    def _write(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def clear(self) -> None:
        """Remove recovery state once a task no longer needs recovery."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _is_restart_only_prompt(prompt: str) -> bool:
        value = re.sub(r"[\s，。！？、,.!?;；:：]+", "", str(prompt or "")).lower()
        return value in {
            "重启", "重新启动", "重启自己", "自己重启", "重启你自己", "你重启自己",
            "重启homeagent", "重新启动homeagent", "重启桌宠", "重新启动桌宠",
            "请重启自己", "请重启你自己", "请重启homeagent", "请重启桌宠",
        } or (any(target in value for target in ("重启自己", "重启你自己", "重启homeagent", "重启桌宠"))
               and value.startswith(("请", "现在", "立即", "马上", "麻烦", "帮我")))

    def validate_current_changes(self, require_changes: bool = False) -> dict[str, Any]:
        """Validate edits before an execution agent is allowed to claim success."""
        return self.code_editor.validate_current_changes(require_changes)

    def begin(self, prompt: str, resumed: bool = False, track_changes: bool = True) -> None:
        if track_changes:
            self.code_editor.begin_tracking()
        now = datetime.now().isoformat(timespec="seconds")
        previous = self.read() if resumed else {}
        self._write({
            "version": 1, "status": "running", "prompt": previous.get("prompt") or prompt,
            "is_self_upgrade": bool(previous.get("is_self_upgrade")) if resumed else False,
            "started_at": previous.get("started_at") or now, "updated_at": now,
            "current_step": previous.get("current_step", "正在恢复任务" if resumed else "正在分析任务"),
            "completed_steps": previous.get("completed_steps", []),
            "changed_files": previous.get("changed_files", []),
            "restart_count": int(previous.get("restart_count", 0)),
        })

    def begin_tracking(self) -> None:
        """Start code-change tracking without creating a recovery task."""
        self.code_editor.begin_tracking()

    def set_self_upgrade(self, enabled: bool) -> None:
        """Persist the semantic planner's code-scope decision for recovery."""
        state = self.read()
        if state.get("status") != "running":
            return
        state.update({"is_self_upgrade": bool(enabled), "updated_at": datetime.now().isoformat(timespec="seconds")})
        self._write(state)

    def progress(self, current: str, completed: list[str]) -> None:
        state = self.read()
        if state.get("status") != "running":
            return
        state.update({"current_step": str(current), "completed_steps": [str(x) for x in completed[-12:]], "updated_at": datetime.now().isoformat(timespec="seconds")})
        self._write(state)

    def changed_files(self) -> list[str]:
        return self.code_editor.changed_files()

    def _validate(self, changed: list[str]) -> dict[str, Any]:
        return self.code_editor.validate_files(changed)

    def finalize(self, answer: str) -> bool:
        state = self.read()
        changed = self.changed_files()
        state["changed_files"] = sorted(set(state.get("changed_files", [])) | set(changed))
        state["last_answer"] = str(answer)[:4000]
        validation = (
            self.code_editor.validate_current_changes(require_changes=True)
            if state.get("is_self_upgrade")
            else self._validate(changed)
        )
        state["validation"] = validation
        if not validation.get("ok"):
            state.update({"status": "validation_failed", "updated_at": datetime.now().isoformat(timespec="seconds")})
            self._write(state)
            raise RuntimeError(f"自升级校验失败，已阻止重启：{validation.get('error', 'unknown error')}")
        restart_sensitive = any(path.startswith(self.RESTART_AREAS) and path.endswith(".py") for path in changed)
        should_restart = bool(self.config.get("enabled", True) and self.config.get("auto_restart", True) and state.get("is_self_upgrade") and restart_sensitive and int(state.get("restart_count", 0)) < int(self.config.get("max_restart_attempts", 2)))
        if should_restart:
            state.update({
                "status": "restart_pending", "task_completed": True,
                "current_step": "升级已完成，等待重启加载新代码",
                "restart_count": int(state.get("restart_count", 0)) + 1,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._write(state)
            return True
        self.clear()
        return False

    def cancel(self) -> None:
        self.clear()

    def fail(self, error: str) -> None:
        """Keep diagnostics for a handled failure without auto-resuming it."""
        state = self.read()
        if not state:
            return
        state.update({
            "status": "failed", "current_step": "任务执行失败",
            "last_error": str(error)[:2000],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        self._write(state)

    def resume_prompt(self) -> str:
        state = self.read()
        status = state.get("status")
        if self._is_restart_only_prompt(str(state.get("prompt", ""))):
            self.clear()
            return ""
        if status == "restart_pending":
            # finalize() already validated and completed this task.  Restart only
            # loads the new code; it must never submit the original prompt again.
            self.clear()
            return ""
        if status != "running" or not str(state.get("prompt", "")).strip():
            if status in {"completed", "cancelled"}:
                self.clear()
            return ""
        if state.get("is_self_upgrade") is not True:
            # Legacy versions persisted every conversation as a recoverable
            # current task. Only an unfinished self-upgrade belongs here.
            self.clear()
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
