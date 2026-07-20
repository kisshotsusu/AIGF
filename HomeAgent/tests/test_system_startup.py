import tempfile
import unittest
from pathlib import Path

from home_modules.system_startup import run_network_guard, set_windows_autostart


class SystemStartupTests(unittest.TestCase):
    def test_startup_entry_uses_explicit_autostart_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "启动.bat"
            launcher.write_text("@echo off", encoding="utf-8")
            target = root / "Startup" / "HomeAgent.cmd"
            set_windows_autostart(True, launcher, target)
            self.assertIn("--system-autostart", target.read_text(encoding="utf-8"))
            set_windows_autostart(False, launcher, target)
            self.assertFalse(target.exists())

    def test_guard_never_restarts_on_manual_launch(self):
        restarts = []
        result = run_network_guard(
            {"enabled": True, "restart_on_network_failure": True},
            Path("unused.json"),
            is_autostart=False,
            probe=lambda *_: False,
            restart=restarts.append,
            sleeper=lambda _: None,
        )
        self.assertEqual(result, "inactive")
        self.assertEqual(restarts, [])

    def test_guard_caps_failed_network_restarts_at_five(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "network.json"
            restarts = []
            config = {
                "enabled": True,
                "restart_on_network_failure": True,
                "max_restart_attempts": 5,
                "startup_grace_seconds": 0,
                "check_rounds": 1,
            }
            for _ in range(7):
                run_network_guard(
                    config,
                    state,
                    is_autostart=True,
                    probe=lambda *_: False,
                    restart=restarts.append,
                    sleeper=lambda _: None,
                )
            self.assertEqual(len(restarts), 5)

    def test_online_result_resets_restart_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "network.json"
            config = {"enabled": True, "restart_on_network_failure": True, "startup_grace_seconds": 0}
            result = run_network_guard(
                config,
                state,
                is_autostart=True,
                probe=lambda *_: True,
                restart=lambda _: self.fail("online guard must not reboot"),
                sleeper=lambda _: None,
            )
            self.assertEqual(result, "online")
            self.assertIn('"restart_attempts": 0', state.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
