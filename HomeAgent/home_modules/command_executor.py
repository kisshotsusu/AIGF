from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any


class CommandExecutor:
    """Bounded, non-interactive Windows command execution for model tool calls."""

    def __init__(self, default_cwd: Path):
        self.default_cwd = default_cwd.resolve()

    @staticmethod
    def _decode(value: bytes | str | None) -> str:
        if value is None: return ""
        if isinstance(value, str): return value
        if b"\x00" in value[:200]:
            try: return value.decode("utf-16le", "replace")
            except UnicodeError: pass
        for encoding in ("utf-8", "gb18030"):
            try: return value.decode(encoding)
            except UnicodeError: continue
        return value.decode("utf-8", "replace")

    def execute(
        self,
        kind: str,
        command: str,
        *,
        cwd: Path | None = None,
        timeout_seconds: int = 60,
        max_output_chars: int = 20000,
    ) -> dict[str, Any]:
        value = str(command or "").strip()
        if not value: return {"status": "failed", "error": "命令为空"}
        shell_kind = str(kind or "").strip().lower()
        if shell_kind == "shell":
            args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", value]
        elif shell_kind == "cmd":
            # A list makes subprocess escape embedded quotes as \". cmd.exe does
            # not understand that escaping, so use Python's Windows CMD wrapper.
            args = value
        else:
            return {"status": "failed", "error": f"不支持的命令类型：{kind}"}

        workdir = (cwd or self.default_cwd).resolve()
        timeout = max(1, min(300, int(timeout_seconds)))
        limit = max(1000, min(100000, int(max_output_chars)))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = time.monotonic()
        env = os.environ.copy(); env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        try:
            result = subprocess.run(
                args, cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, creationflags=flags, check=False,
                shell=shell_kind == "cmd",
            )
            stdout = self._decode(result.stdout)
            stderr = self._decode(result.stderr)
            truncated = len(stdout) > limit or len(stderr) > limit
            return {
                "ok": result.returncode == 0,
                "status": "success" if result.returncode == 0 else "failed",
                "shell": shell_kind,
                "exit_code": result.returncode,
                "stdout": stdout[-limit:],
                "stderr": stderr[-limit:],
                "output_truncated": truncated,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "cwd": str(workdir),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False, "status": "failed", "shell": shell_kind,
                "error": f"命令执行超过 {timeout} 秒，已终止",
                "stdout": self._decode(exc.stdout)[-limit:],
                "stderr": self._decode(exc.stderr)[-limit:],
                "timed_out": True, "cwd": str(workdir),
            }
        except OSError as exc:
            return {"ok": False, "status": "failed", "shell": shell_kind, "error": str(exc), "cwd": str(workdir)}
