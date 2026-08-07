"""Code validation and autonomous test execution for HomeAgent.

独立代码验证模块：只负责文件级语法检查、仓库级静态检查和项目自动测试，
不包含编辑、变更追踪、任务恢复、UI、TTS 或 Codex 进程管理。这些编排职责
由 CodeEditorModule / SelfUpgradeManager / HomeAgent 持有，本模块只暴露
可重复运行的验证结果。
"""
from __future__ import annotations

import configparser
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


class CodeValidator:
    """按 AI Read 规范执行代码验证：语法检查、git 静态检查与项目自动测试。"""

    EXCLUDED_PARTS = {".git", ".venv", "node_modules", "logs", "__pycache__", "models"}
    PY_SUFFIXES = {".py"}
    YAML_SUFFIXES = {".yaml", ".yml"}
    JS_SUFFIXES = {".js", ".mjs", ".cjs"}

    def __init__(self, root: Path, home_agent: Path):
        self.root = root.resolve()
        self.home_agent = home_agent.resolve()
        self.python_exe = self._find_python()

    def _find_python(self) -> str:
        python = self.root / ".venv" / "Scripts" / "python.exe"
        return str(python if python.is_file() else Path(sys.executable))

    def validate_files(self, changed: list[str]) -> dict[str, Any]:
        """对变更文件执行适用的语法/结构检查。

        支持 Python（py_compile）、JSON、YAML、TOML、INI、JS（node --check）、
        HTML 根标签和 CSS 大括号平衡；不认识的扩展名跳过。返回 checked 列表，
        任一失败时返回 ok=false 与首个错误摘要。
        """
        checked: list[str] = []
        problems: list[str] = []
        js_paths: list[Path] = []
        for relative in changed:
            candidate = Path(relative)
            path = candidate if candidate.is_absolute() else self.root / candidate
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            try:
                if suffix in self.PY_SUFFIXES:
                    py_compile.compile(str(path), doraise=True)
                    checked.append(relative)
                elif suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
                    checked.append(relative)
                elif suffix in self.YAML_SUFFIXES:
                    import yaml
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                    checked.append(relative)
                elif suffix == ".toml":
                    tomllib.loads(path.read_text(encoding="utf-8"))
                    checked.append(relative)
                elif suffix == ".ini":
                    parser = configparser.ConfigParser()
                    parser.read_string(path.read_text(encoding="utf-8"))
                    checked.append(relative)
                elif suffix in self.JS_SUFFIXES:
                    js_paths.append(path)
                elif suffix == ".html":
                    self._validate_html(path)
                    checked.append(relative)
                elif suffix == ".css":
                    self._validate_css(path)
                    checked.append(relative)
            except Exception as exc:
                problems.append(f"{relative}: {exc}")
        if js_paths:
            js_errors = self._check_js(js_paths)
            if js_errors:
                problems.extend(js_errors)
            else:
                checked.extend(self._display_path(path) for path in js_paths)
        if problems:
            return {"ok": False, "checked": checked, "error": "；".join(problems[:10])}
        return {"ok": True, "checked": checked}

    def validate_repo(self, timeout: int = 60) -> dict[str, Any]:
        """运行 git diff --check，捕获尾随空格等补丁级静态问题；非 git 仓库时跳过。"""
        if not (self.root / ".git").exists():
            return {"ok": True, "skipped": True, "output": "不是 git 仓库，跳过 git diff --check"}
        result = self._run_command(["git", "diff", "--check"], self.root, timeout)
        result["skipped"] = False
        return result

    def run_autonomous_tests(self, changed: list[str], timeout: int = 180) -> dict[str, Any]:
        """按变更文件检测项目类型，独立运行适用的编译、语法检查和测试。"""
        results: list[dict[str, Any]] = []
        changed_paths = [self.root / relative for relative in changed]
        non_project_python = [
            path for path in changed_paths
            if path.is_file() and path.suffix.lower() in self.PY_SUFFIXES
            and not (len(path.relative_to(self.root).parts) >= 2 and path.relative_to(self.root).parts[0].casefold() == "projects")
        ]
        if non_project_python:
            results.append(self._run_command(
                [self.python_exe, "-m", "py_compile", *map(str, non_project_python)],
                self.root, timeout,
            ))

        for project in self._project_roots(changed):
            if not project.is_dir():
                continue
            project_python = [
                path for path in changed_paths
                if path.is_file() and path.suffix.lower() in self.PY_SUFFIXES and project in path.parents
            ]
            if project_python:
                results.append(self._run_command([self.python_exe, "-m", "compileall", "-q", "."], project, timeout))
                tests = project / "tests"
                if tests.is_dir():
                    if importlib.util.find_spec("pytest") is not None:
                        results.append(self._run_command([self.python_exe, "-m", "pytest", "-q"], project, timeout))
                    else:
                        results.append(self._run_command([self.python_exe, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], project, timeout))

            node = shutil.which("node")
            for path in changed_paths:
                if node and path.is_file() and path.suffix.lower() in self.JS_SUFFIXES and project in path.parents:
                    results.append(self._run_command([node, "--check", str(path)], project, timeout))
            static_files = [
                path for path in changed_paths
                if path.is_file() and path.suffix.lower() in {".html", ".css"} and project in path.parents
            ]
            if static_files:
                static_errors: list[str] = []
                for path in static_files:
                    try:
                        if path.suffix.lower() == ".html":
                            self._validate_html(path)
                        elif path.suffix.lower() == ".css":
                            self._validate_css(path)
                    except ValueError as exc:
                        static_errors.append(f"{path.name}: {exc}")
                results.append({"command": ["static-asset-check"], "cwd": str(project), "ok": not static_errors, "output": "\n".join(static_errors)})
            package = project / "package.json"
            npm = shutil.which("npm")
            if package.is_file() and npm:
                try:
                    script = str(json.loads(package.read_text(encoding="utf-8")).get("scripts", {}).get("test", "")).strip()
                except (OSError, json.JSONDecodeError, TypeError):
                    script = ""
                if script and "no test specified" not in script.lower():
                    results.append(self._run_command([npm, "test"], project, timeout))
            tsconfig = project / "tsconfig.json"
            npx = shutil.which("npx")
            if tsconfig.is_file() and npx:
                results.append(self._run_command([npx, "--no-install", "tsc", "--noEmit"], project, timeout))

        if any(path.startswith("HomeAgent/") for path in changed):
            results.append(self._run_command(
                [self.python_exe, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                self.home_agent, timeout,
            ))
        if any(path.startswith("modules/live/") for path in changed):
            results.append(self._run_command(
                [self.python_exe, "-m", "unittest", "discover", "-s", "modules/live/tests", "-p", "test_*.py", "-v"],
                self.root, timeout,
            ))

        failed = [row for row in results if not row.get("ok")]
        return {
            "ok": bool(results) and not failed,
            "commands": results,
            "failed": failed,
            "changed": changed,
            "error": "没有检测到可运行的代码检查" if not results else ("自动测试失败" if failed else ""),
        }

    @staticmethod
    def _validate_html(path: Path) -> None:
        content = path.read_text(encoding="utf-8", errors="replace")
        if "<html" not in content.lower() or "</html>" not in content.lower():
            raise ValueError("缺少完整 html 根标签")

    @staticmethod
    def _validate_css(path: Path) -> None:
        content = path.read_text(encoding="utf-8", errors="replace")
        if content.count("{") != content.count("}"):
            raise ValueError("CSS 大括号不平衡")

    def _check_js(self, paths: list[Path]) -> list[str]:
        node = shutil.which("node")
        if not node:
            return []
        errors: list[str] = []
        for path in paths:
            result = self._run_command([node, "--check", str(path)], self.root, 30)
            if not result.get("ok"):
                errors.append(f"{self._display_path(path)}: {result.get('output') or result.get('error')}")
        return errors

    def _project_roots(self, changed: list[str]) -> list[Path]:
        projects: set[Path] = set()
        for relative in changed:
            parts = Path(relative).parts
            if len(parts) >= 2 and parts[0].casefold() == "projects":
                projects.add((self.root / parts[0] / parts[1]).resolve())
        return sorted(projects, key=str)

    def _display_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    @staticmethod
    def _run_command(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
        try:
            result = subprocess.run(
                command, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())[-6000:]
            return {"command": command, "cwd": str(cwd), "ok": result.returncode == 0, "exit_code": result.returncode, "output": output}
        except subprocess.TimeoutExpired as exc:
            return {"command": command, "cwd": str(cwd), "ok": False, "error": f"检查超过 {timeout} 秒", "output": str(exc)[-1000:]}
        except OSError as exc:
            return {"command": command, "cwd": str(cwd), "ok": False, "error": str(exc)}
