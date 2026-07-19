from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    old_pid, executable, app = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    deadline = time.monotonic() + 30
    while process_exists(old_pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    subprocess.Popen([str(executable), str(app)], cwd=str(app.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
