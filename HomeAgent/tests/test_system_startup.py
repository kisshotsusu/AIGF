import tempfile
import unittest
from pathlib import Path

from home_modules.system_startup import (
    REGISTRY_RUN_KEY,
    REGISTRY_VALUE_NAME,
    SCHEDULED_TASK_NAME,
    AUTOSTART_ARGUMENT,
    configure_system_autostart,
    greeting_enabled,
    greeting_text,
    registry_autostart_command,
    run_network_guard,
    scheduled_task_command,
    set_windows_autostart,
)


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

    def test_greeting_enabled_by_default(self):
        self.assertTrue(greeting_enabled({}))
        self.assertTrue(greeting_enabled({"greeting_on_startup": True}))
        self.assertFalse(greeting_enabled({"greeting_on_startup": False}))

    def test_greeting_text_uses_config_or_default(self):
        self.assertEqual(greeting_text({"greeting_text": "你好呀"}), "你好呀")
        self.assertEqual(greeting_text({}), "主人，早上好呀，苏苏已经准备好陪你了。")
        self.assertEqual(greeting_text({"greeting_text": "   "}), "主人，早上好呀，苏苏已经准备好陪你了。")

    def test_registry_command_installs_run_value(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "启动.bat"
            command = registry_autostart_command(True, launcher)
            self.assertIn(REGISTRY_RUN_KEY, command)
            self.assertIn(REGISTRY_VALUE_NAME, command)
            self.assertIn(AUTOSTART_ARGUMENT, command)
            self.assertIn(str(launcher.resolve()), command)

    def test_registry_command_removes_run_value(self):
        command = registry_autostart_command(False, Path("x.bat"))
        self.assertTrue(command.startswith("reg delete"))
        self.assertIn(REGISTRY_VALUE_NAME, command)

    def test_scheduled_task_command_creates_on_login(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "启动.bat"
            command = scheduled_task_command(True, launcher)
            self.assertIn(SCHEDULED_TASK_NAME, command)
            self.assertIn("/sc onlogon", command)
            self.assertIn(AUTOSTART_ARGUMENT, command)
            self.assertIn(str(launcher.resolve()), command)

    def test_scheduled_task_command_deletes(self):
        command = scheduled_task_command(False, Path("x.bat"))
        self.assertTrue(command.startswith("schtasks /delete"))
        self.assertIn(SCHEDULED_TASK_NAME, command)

    def test_configure_issues_registry_and_task_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "启动.bat"
            launcher.write_text("@echo off", encoding="utf-8")
            issued = []
            commands = configure_system_autostart(True, launcher, startup_target=root / "Startup" / "HomeAgent.cmd", runner=issued.append)
            self.assertEqual(len(commands), 2)
            self.assertEqual(issued, commands)
            self.assertTrue(all("reg " in c or "schtasks" in c for c in commands))
            self.assertTrue((root / "Startup" / "HomeAgent.cmd").exists())
            configure_system_autostart(False, launcher, startup_target=root / "Startup" / "HomeAgent.cmd", runner=issued.append)
            self.assertFalse((root / "Startup" / "HomeAgent.cmd").exists())


if __name__ == "__main__":
    unittest.main()
