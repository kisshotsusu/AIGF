from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate_tree(pid: int) -> bool:
    """Actively kill the process tree. On Windows use taskkill /T /F so children
    are reaped too. Inherits the caller's token, so an elevated Home Agent can
    kill its own previously-spawned (possibly elevated) old instance."""
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return r.returncode == 0
        except Exception:
            return False
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    old_pid, executable, app = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    graceful_deadline = time.monotonic() + 12
    # Phase 1: give the old instance a short window to exit cleanly on its own.
    while process_exists(old_pid) and time.monotonic() < graceful_deadline:
        time.sleep(0.4)
    # Phase 2: if it is still alive (Qt teardown hung / a non-daemon thread
    # blocked / crashed), force-terminate it so the lock file is released and a
    # brand-new single instance can acquire it. Never launch a second instance
    # while the old one is still holding the lock.
    if process_exists(old_pid):
        _terminate_tree(old_pid)
        hard_deadline = time.monotonic() + 10
        while process_exists(old_pid) and time.monotonic() < hard_deadline:
            time.sleep(0.3)
    # Phase 3: old process gone (or gave up), lock released -> start fresh.
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    subprocess.Popen(
        [str(executable), str(app)],
        cwd=str(app.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
