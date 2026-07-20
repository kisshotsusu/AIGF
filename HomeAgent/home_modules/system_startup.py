from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable


AUTOSTART_ARGUMENT = "--system-autostart"
DEFAULT_TEST_URLS = (
    "https://www.bilibili.com/",
    "https://www.baidu.com/",
    "https://www.qq.com/",
)


def startup_script_path(appdata: str | None = None) -> Path:
    root = Path(appdata or os.environ.get("APPDATA", ""))
    if not str(root):
        raise RuntimeError("无法确定 Windows 启动目录：APPDATA 未设置")
    return root / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "HomeAgent.cmd"


def set_windows_autostart(enabled: bool, launcher: Path, target: Path | None = None) -> Path:
    """Install a per-user Startup entry without requiring administrator rights."""
    target = target or startup_script_path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "@echo off\r\n"
            f'call "{launcher.resolve()}" {AUTOSTART_ARGUMENT}\r\n'
        )
        temporary = target.with_suffix(".cmd.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
    else:
        target.unlink(missing_ok=True)
    return target


def probe_network(urls=DEFAULT_TEST_URLS, timeout_seconds: float = 6.0) -> bool:
    headers = {"User-Agent": "HomeAgent-Network-Guard/1.0"}
    for url in urls:
        try:
            request = urllib.request.Request(str(url), headers=headers, method="HEAD")
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if 200 <= int(response.status) < 500:
                    return True
        except Exception:
            continue
    return False


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def request_windows_restart(delay_seconds: int = 15) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["shutdown.exe", "/r", "/t", str(max(0, int(delay_seconds))), "/c", "Home Agent 检测到网络持续不可用，正在重启电脑。"],
        creationflags=flags,
        close_fds=True,
    )


def run_network_guard(
    config: dict,
    state_path: Path,
    *,
    is_autostart: bool,
    probe: Callable[..., bool] = probe_network,
    restart: Callable[[int], None] = request_windows_restart,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Check connectivity only for a real system-autostart launch.

    Returns a small status string to make the safety policy independently testable.
    """
    if not is_autostart or not bool(config.get("enabled", False)):
        return "inactive"
    if not bool(config.get("restart_on_network_failure", False)):
        return "restart_disabled"

    grace = max(0, int(config.get("startup_grace_seconds", 45)))
    rounds = max(1, min(6, int(config.get("check_rounds", 3))))
    interval = max(0, int(config.get("check_interval_seconds", 8)))
    timeout = max(1.0, min(15.0, float(config.get("request_timeout_seconds", 6))))
    urls = tuple(config.get("test_urls") or DEFAULT_TEST_URLS)
    if grace:
        sleeper(grace)
    for index in range(rounds):
        if probe(urls, timeout):
            _write_state(state_path, {"restart_attempts": 0, "last_result": "online", "updated_at": int(time.time())})
            return "online"
        if index + 1 < rounds and interval:
            sleeper(interval)

    state = _read_state(state_path)
    attempts = max(0, int(state.get("restart_attempts", 0)))
    maximum = max(1, min(5, int(config.get("max_restart_attempts", 5))))
    if attempts >= maximum:
        _write_state(state_path, {"restart_attempts": attempts, "last_result": "limit_reached", "updated_at": int(time.time())})
        return "limit_reached"

    attempts += 1
    _write_state(state_path, {"restart_attempts": attempts, "last_result": "restart_requested", "updated_at": int(time.time())})
    restart(max(0, int(config.get("restart_delay_seconds", 15))))
    return "restart_requested"
