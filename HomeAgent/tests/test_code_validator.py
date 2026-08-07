import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from home_modules.code_validator import CodeValidator
from home_modules.code_editor import CodeEditorModule


class CodeValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _validator(self):
        return CodeValidator(self.root, self.root / "HomeAgent")

    def _write(self, relative: str, content: str) -> str:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return relative.replace("\\", "/")

    def test_validate_files_accepts_python_yaml_json_toml_ini(self):
        validator = self._validator()
        changed = [
            self._write("a.py", "value = 1\n"),
            self._write("b.yaml", "key: value\n"),
            self._write("c.json", '{"ok": true}\n'),
            self._write("d.toml", 'key = "value"\n'),
            self._write("e.ini", "[section]\nkey = value\n"),
        ]
        result = validator.validate_files(changed)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["checked"]), 5)

    def test_validate_files_reports_python_syntax_error(self):
        validator = self._validator()
        bad = self._write("bad.py", "def broken(:\n")
        result = validator.validate_files([bad])
        self.assertFalse(result["ok"])
        self.assertIn("bad.py", result["error"])

    def test_validate_files_reports_invalid_yaml_and_json(self):
        validator = self._validator()
        bad_yaml = self._write("bad.yaml", "key: [unclosed\n")
        bad_json = self._write("bad.json", "{not json}\n")
        result = validator.validate_files([bad_yaml, bad_json])
        self.assertFalse(result["ok"])
        self.assertIn("bad.yaml", result["error"])
        self.assertIn("bad.json", result["error"])

    def test_validate_files_checks_html_and_css(self):
        validator = self._validator()
        good_html = self._write("page.html", "<html><body>hi</body></html>\n")
        good_css = self._write("style.css", "body { color: red; }\n")
        result = validator.validate_files([good_html, good_css])
        self.assertTrue(result["ok"], result.get("error"))
        bad_css = self._write("broken.css", "body { color: red;\n")
        result = validator.validate_files([bad_css])
        self.assertFalse(result["ok"])
        self.assertIn("broken.css", result["error"])

    def test_validate_files_uses_node_check_when_available(self):
        validator = self._validator()
        js = self._write("app.js", "const value = 1;\nconsole.log(value);\n")
        result = validator.validate_files([js])
        if shutil.which("node"):
            self.assertTrue(result["ok"], result.get("error"))
            self.assertIn("app.js", result["checked"])
        else:
            self.assertIn("app.js", result["checked"])

    def test_validate_files_skips_unknown_suffixes(self):
        validator = self._validator()
        text = self._write("note.txt", "hello\n")
        result = validator.validate_files([text])
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], [])

    def test_git_diff_check_skips_non_git_directory(self):
        result = self._validator().validate_repo()
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])

    def test_git_diff_check_reports_trailing_whitespace(self):
        if not shutil.which("git"):
            self.skipTest("git 不可用")
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
        clean = repo / "clean.py"
        clean.write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
        clean.write_text("value = 1   \n", encoding="utf-8")
        result = CodeValidator(repo, repo / "HomeAgent").validate_repo()
        self.assertFalse(result["ok"])
        self.assertIn("trailing whitespace", result.get("output", ""))

    def test_run_autonomous_tests_compiles_changed_python(self):
        validator = self._validator()
        good = self._write("sample.py", "value = 1\n")
        result = validator.run_autonomous_tests([good], timeout=30)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(any("py_compile" in " ".join(row.get("command", [])) for row in result["commands"]))

    def test_run_autonomous_tests_reports_failed_python(self):
        validator = self._validator()
        bad = self._write("broken.py", "def broken(:\n")
        result = validator.run_autonomous_tests([bad], timeout=30)
        self.assertFalse(result["ok"])
        self.assertTrue(result["failed"])

    def test_code_editor_delegates_to_independent_validator(self):
        editor = CodeEditorModule(self.root, self.root / "HomeAgent")
        self.assertIsInstance(editor.validator, CodeValidator)
        good = self._write("sample.py", "value = 1\n")
        validation = editor.validate_files([good])
        self.assertTrue(validation["ok"], validation.get("error"))
        self.assertEqual(editor.git_diff_check()["skipped"], True)


if __name__ == "__main__":
    unittest.main()
