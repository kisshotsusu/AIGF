from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class InstanceLock:
    """A process-lifetime, cross-platform non-blocking file lock."""

    def __init__(self, path: Path):
        self.path = path
        self.handle: IO[bytes] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                if handle.read(1) == b"":
                    handle.seek(0); handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle = handle
            return True
        except (OSError, IOError):
            handle.close()
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise RuntimeError("直播助手已在运行，拒绝启动重复实例")
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
