import tempfile
import unittest
from pathlib import Path

from home_modules.command_executor import CommandExecutor


class CommandExecutorTests(unittest.TestCase):
    def test_powershell_command_returns_structured_output(self):
        with tempfile.TemporaryDirectory() as folder:
            result = CommandExecutor(Path(folder)).execute("shell", "Write-Output 'shell-ok'")
        self.assertTrue(result["ok"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("shell-ok", result["stdout"])

    def test_cmd_command_uses_requested_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            result = CommandExecutor(Path(folder)).execute("cmd", "echo cmd-ok", cwd=Path(folder))
        self.assertTrue(result["ok"])
        self.assertIn("cmd-ok", result["stdout"])
        self.assertEqual(Path(result["cwd"]), Path(folder).resolve())

    def test_failed_command_is_not_reported_as_success(self):
        result = CommandExecutor(Path.cwd()).execute("cmd", "exit /b 7")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
