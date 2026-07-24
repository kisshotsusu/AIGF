"""Code-edit preparation, change tracking, and validation for HomeAgent.

This module deliberately contains no UI, TTS, task recovery, or Codex process
management.  The orchestration layer delegates those concerns and only consumes
the execution contract and validation result exposed here.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class CodeEditorModule:
    TRACKED_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".txt", ".bat", ".cmd", ".ps1", ".toml", ".ini", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css"}
    TRACKED_AREAS = ("HomeAgent", "Vision", "Skill", "CharacterManager", "modules", "src", "Projects", "AI Read")
    EXCLUDED_PARTS = {".git", ".venv", "node_modules", "logs", "__pycache__", "models"}
    ROOT_TRACKED_FILES = ("config.yaml", "config.example.yaml", "README.md")
    DEVELOPMENT_DOCUMENTS = (
        "README.md",
        "AI Read/00_START_HERE.md",
        "AI Read/01_ARCHITECTURE.md",
        "AI Read/02_COMPONENTS.md",
        "AI Read/05_OPERATIONS_AND_RULES.md",
        "AI Read/06_CURRENT_STATE.md",
        "AI Read/07_DEVELOPER_REFERENCE.md",
        "AI Read/08_TESTING.md",
    )

    def __init__(self, root: Path, home_agent: Path, require_validation: bool = True,
                 allow_external_read: bool = False, external_read_roots: list[str] | None = None,
                 allow_external_write: bool = False):
        self.root = root.resolve()
        self.home_agent = home_agent.resolve()
        self.require_validation = bool(require_validation)
        self.allow_external_read = bool(allow_external_read)
        self.allow_external_write = bool(allow_external_write)
        self.external_read_roots = [Path(value).expanduser().resolve() for value in (external_read_roots or []) if str(value).strip()]
        self._baseline: dict[str, str] = {}
        self._external_changed: set[str] = set()

    def _resolve_edit_path(self, value: str, self_edit: bool = False) -> Path:
        raw = str(value or "").strip()
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            path = candidate.resolve()
            if not self.allow_external_write and not any(path == root or root in path.parents for root in self.external_read_roots):
                raise ValueError("绝对路径不在代码写入权限范围；请开启完整磁盘访问或配置 allowed_roots")
            if path.name.lower().startswith(".env") or path.suffix.lower() in {".pem", ".key", ".pfx"}:
                raise ValueError("禁止读取或编辑密钥文件")
            return path
        raw = raw.replace("\\", "/")
        if not raw:
            raise ValueError("代码工具只接受工程根目录内的相对路径")
        path = (self.root / raw).resolve()
        allowed = [self.root] if self_edit else [self.root / "Projects"]
        if not any(path == folder.resolve() or folder.resolve() in path.parents for folder in allowed):
            raise ValueError("路径不在当前代码任务允许的目录中")
        if any(part in {".git", ".venv", "node_modules", "__pycache__"} for part in path.parts):
            raise ValueError("依赖、缓存和 Git 元数据目录不作为源码编辑目标")
        if path.name.lower().startswith(".env"):
            raise ValueError("禁止读取或编辑密钥文件")
        return path

    def _resolve_read_path(self, value: str, self_edit: bool = False) -> Path:
        raw = str(value or "").strip()
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return self._resolve_edit_path(raw, self_edit)
        path = candidate.resolve()
        if not self.allow_external_read and not any(path == root or root in path.parents for root in self.external_read_roots):
            raise ValueError("绝对路径不在代码读取权限范围；请开启完整磁盘访问或配置 allowed_roots")
        if path.name.lower().startswith(".env") or path.suffix.lower() in {".pem", ".key", ".pfx"}:
            raise ValueError("禁止读取或编辑密钥文件")
        return path

    def list_files(self, path: str = "Projects", self_edit: bool = False, limit: int = 300) -> dict[str, Any]:
        target = self._resolve_read_path(path, self_edit)
        if not target.exists():
            try: display = target.relative_to(self.root).as_posix()
            except ValueError: display = str(target)
            return {"ok": True, "path": display, "files": []}
        if not target.is_dir():
            raise ValueError("列出路径必须是目录")
        files: list[str] = []
        for item in target.rglob("*"):
            if item.is_file() and not any(part in self.EXCLUDED_PARTS for part in item.parts):
                try: display = item.relative_to(self.root).as_posix()
                except ValueError: display = str(item)
                files.append(display)
                if len(files) >= max(1, min(1000, int(limit))):
                    break
        try: display_target = target.relative_to(self.root).as_posix()
        except ValueError: display_target = str(target)
        return {"ok": True, "path": display_target, "files": files, "count": len(files)}

    def read_file(self, path: str, self_edit: bool = False, max_chars: int = 30000,
                  start_line: int = 1, max_lines: int = 500) -> dict[str, Any]:
        target = self._resolve_read_path(path, self_edit)
        if not target.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        content = target.read_text(encoding="utf-8")
        limit = max(1000, min(100000, int(max_chars)))
        lines = content.splitlines(keepends=True)
        first = max(1, int(start_line))
        line_limit = max(1, min(2000, int(max_lines)))
        selected = "".join(lines[first - 1:first - 1 + line_limit])
        clipped = selected[:limit]
        try: display = target.relative_to(self.root).as_posix()
        except ValueError: display = str(target)
        return {
            "ok": True, "path": display, "content": clipped,
            "start_line": first, "end_line": min(len(lines), first + line_limit - 1),
            "total_lines": len(lines), "truncated": first > 1 or len(selected) > limit or first - 1 + line_limit < len(lines),
            "chars": len(content),
        }

    def search_text(self, query: str, path: str = "Projects", self_edit: bool = False, limit: int = 100) -> dict[str, Any]:
        target = self._resolve_read_path(path, self_edit)
        needle = str(query or "")
        if not needle:
            raise ValueError("搜索内容不能为空")
        files = [target] if target.is_file() else target.rglob("*") if target.exists() else []
        matches: list[dict[str, Any]] = []
        for file in files:
            if not file.is_file() or file.suffix.lower() not in self.TRACKED_SUFFIXES or any(part in self.EXCLUDED_PARTS for part in file.parts):
                continue
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(lines, 1):
                if needle.casefold() in line.casefold():
                    try: display = file.relative_to(self.root).as_posix()
                    except ValueError: display = str(file)
                    matches.append({"path": display, "line": number, "text": line[:500]})
                    if len(matches) >= max(1, min(500, int(limit))):
                        return {"ok": True, "query": needle, "matches": matches, "truncated": True}
        return {"ok": True, "query": needle, "matches": matches, "truncated": False}

    def write_file(self, path: str, content: str, self_edit: bool = False) -> dict[str, Any]:
        target = self._resolve_edit_path(path, self_edit)
        existed = target.exists()
        text = str(content)
        if len(text) > 500_000:
            raise ValueError("单次写入超过 500000 字符，请拆分文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.home-agent.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(target)
        try: display = target.relative_to(self.root).as_posix()
        except ValueError:
            display = str(target); self._external_changed.add(display)
        return {"ok": True, "path": display, "chars": len(text), "created": not existed}

    def replace_text(self, path: str, old: str, new: str, self_edit: bool = False, count: int = 1) -> dict[str, Any]:
        target = self._resolve_edit_path(path, self_edit)
        if not target.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        source = target.read_text(encoding="utf-8")
        occurrences = source.count(str(old))
        if not old or occurrences == 0:
            raise ValueError("没有找到需要替换的原文")
        requested = max(1, min(occurrences, int(count)))
        updated = source.replace(str(old), str(new), requested)
        temporary = target.with_name(f".{target.name}.home-agent.tmp")
        temporary.write_text(updated, encoding="utf-8", newline="\n")
        temporary.replace(target)
        try: display = target.relative_to(self.root).as_posix()
        except ValueError:
            display = str(target); self._external_changed.add(display)
        return {"ok": True, "path": display, "replaced": requested, "remaining_matches": occurrences - requested}

    def _fingerprint(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in self.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in self.TRACKED_SUFFIXES:
                continue
            if any(part in self.EXCLUDED_PARTS for part in path.parts):
                continue
            if self.home_agent / "state" in path.parents:
                continue
            try:
                stat = path.stat()
                result[path.relative_to(self.root).as_posix()] = hashlib.sha1(
                    f"{stat.st_size}:{stat.st_mtime_ns}".encode()
                ).hexdigest()
            except OSError:
                continue
        return result

    def begin_tracking(self) -> None:
        self._baseline = self._fingerprint()
        self._external_changed.clear()

    def changed_files(self) -> list[str]:
        after = self._fingerprint()
        keys = set(self._baseline) | set(after)
        return sorted({key for key in keys if self._baseline.get(key) != after.get(key)} | self._external_changed)

    def validate_files(self, changed: list[str]) -> dict[str, Any]:
        if not self.require_validation:
            return {"ok": True, "skipped": True, "checked": []}
        checked: list[str] = []
        try:
            for relative in changed:
                candidate = Path(relative)
                path = candidate if candidate.is_absolute() else self.root / candidate
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix == ".py":
                    py_compile.compile(str(path), doraise=True)
                    checked.append(relative)
                elif suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
                    checked.append(relative)
                elif suffix in {".yaml", ".yml"}:
                    import yaml
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                    checked.append(relative)
            return {"ok": True, "checked": checked}
        except Exception as exc:
            return {"ok": False, "checked": checked, "error": str(exc)}

    def validate_current_changes(self, require_changes: bool = False) -> dict[str, Any]:
        changed = self.changed_files()
        if require_changes and not changed:
            return {"ok": False, "changed": [], "error": "自编程任务没有产生任何代码或配置变更"}
        result = self.validate_files(changed)
        result["changed"] = changed
        return result

    def load_engineering_documents(self) -> tuple[str, list[str]]:
        sections: list[str] = []
        loaded: list[str] = []
        for relative in self.DEVELOPMENT_DOCUMENTS:
            path = self.root / relative
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                sections.append(f"===== {relative} =====\n[读取失败: {exc}]")
                continue
            loaded.append(relative)
            sections.append(f"===== {relative} =====\n{content}")
        return "\n\n".join(sections), loaded

    def build_execution_contract(self, self_edit: bool = True, include_document_contents: bool = True) -> tuple[str, list[str]]:
        documents, loaded = self.load_engineering_documents() if self_edit else ("", [])
        scope = (
            "主要写入范围是 HomeAgent，只有共享接口确实需要时才修改相关模块。"
            if self_edit else
            "这是独立项目任务。默认在工程根目录 Projects/<简短英文项目名>/ 中创建完整项目，禁止把项目源码塞进 HomeAgent、work 或临时目录；若用户明确给出工程根目录内的其他路径则使用该路径。"
        )
        contract = (
            f"这是 {'HomeAgent 自编程' if self_edit else '独立项目开发'}任务，不能只给建议或代码片段，必须在本机实际完成代码写入。\n"
            "强制流程：\n"
            + ("1. 先阅读已注入的 README 和 AI Read 工程文档。\n" if self_edit else "1. 明确需求、技术栈、入口、目录结构和可自动验证的完成条件。\n") +
            "2. 用 git status 和代码搜索确认现有用户改动；不得覆盖或回退无关变更。\n"
            f"3. 检查入口、业务层和测试后实际编辑文件；{scope}\n"
            "4. 同时编写可重复运行的自动测试；每次修改代码或配置，必须重写 AI Read 中受影响部分，使架构、组件、接口、规则和当前状态与磁盘实现一致，不能只追加含糊的更新日志。\n"
            "5. 独立项目同步更新项目 README；AIAgent 自身的重要入口或使用方式变化也同步根 README。\n"
            "6. 使用适合技术栈的编译、语法检查和测试命令自行测试并修复失败。\n"
            "7. 最终报告必须列出真实变更文件、文档同步范围、启动方式和验证结果；没有写入文件、AI Read 未同步或测试未通过时明确返回失败。\n"
            "禁止读取或输出 .env 密钥。\n"
            + (f"工程文档清单：{', '.join(loaded)}。" +
               (f"以下是当前磁盘内容：\n\n{documents}\n\n" if include_document_contents else "执行前必须从磁盘重新读取这些文件的相关章节。\n\n") if self_edit else
               "完成后 HomeAgent 的独立校验模块会再次运行语法检查和项目测试；不得伪造测试结果。\n\n")
        )
        return contract, loaded

    def _project_roots(self, changed: list[str]) -> list[Path]:
        projects: set[Path] = set()
        for relative in changed:
            parts = Path(relative).parts
            if len(parts) >= 2 and parts[0].casefold() == "projects":
                projects.add((self.root / parts[0] / parts[1]).resolve())
        return sorted(projects, key=str)

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
            return {"command": command, "cwd": str(cwd), "ok": False, "error": f"测试超过 {timeout} 秒", "output": str(exc)[-1000:]}
        except OSError as exc:
            return {"command": command, "cwd": str(cwd), "ok": False, "error": str(exc)}

    def run_autonomous_tests(self, changed: list[str], timeout: int = 180) -> dict[str, Any]:
        """Detect project types and independently rerun their local checks."""
        results: list[dict[str, Any]] = []
        python = self.root / ".venv" / "Scripts" / "python.exe"
        python_exe = str(python if python.is_file() else Path(sys.executable))
        changed_paths = [self.root / relative for relative in changed]
        non_project_python = [
            path for path in changed_paths
            if path.is_file() and path.suffix.lower() == ".py"
            and not (len(path.relative_to(self.root).parts) >= 2 and path.relative_to(self.root).parts[0].casefold() == "projects")
        ]
        if non_project_python:
            results.append(self._run_command([python_exe, "-m", "py_compile", *map(str, non_project_python)], self.root, timeout))

        for project in self._project_roots(changed):
            if not project.is_dir():
                continue
            project_python = [path for path in changed_paths if path.is_file() and path.suffix.lower() == ".py" and project in path.parents]
            if project_python:
                results.append(self._run_command([python_exe, "-m", "compileall", "-q", "."], project, timeout))
                tests = project / "tests"
                if tests.is_dir():
                    if importlib.util.find_spec("pytest") is not None:
                        results.append(self._run_command([python_exe, "-m", "pytest", "-q"], project, timeout))
                    else:
                        results.append(self._run_command([python_exe, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], project, timeout))

            node = shutil.which("node")
            for path in changed_paths:
                if node and path.is_file() and path.suffix.lower() in {".js", ".mjs", ".cjs"} and project in path.parents:
                    results.append(self._run_command([node, "--check", str(path)], project, timeout))
            static_files = [path for path in changed_paths if path.is_file() and path.suffix.lower() in {".html", ".css"} and project in path.parents]
            if static_files:
                static_errors: list[str] = []
                for path in static_files:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    if path.suffix.lower() == ".html" and not ("<html" in content.lower() and "</html>" in content.lower()):
                        static_errors.append(f"{path.name}: 缺少完整 html 根标签")
                    if path.suffix.lower() == ".css" and content.count("{") != content.count("}"):
                        static_errors.append(f"{path.name}: CSS 大括号不平衡")
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
            results.append(self._run_command([python_exe, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"], self.home_agent, timeout))
        if any(path.startswith("modules/live/") for path in changed):
            results.append(self._run_command([python_exe, "-m", "unittest", "discover", "-s", "modules/live/tests", "-p", "test_*.py", "-v"], self.root, timeout))

        failed = [row for row in results if not row.get("ok")]
        return {"ok": bool(results) and not failed, "commands": results, "failed": failed, "changed": changed, "error": "没有检测到可运行的代码检查" if not results else ("自动测试失败" if failed else "")}
