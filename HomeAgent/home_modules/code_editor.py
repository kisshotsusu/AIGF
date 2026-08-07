"""Code-edit preparation, change tracking, and validation for HomeAgent.

This module deliberately contains no UI, TTS, task recovery, or Codex process
management.  The orchestration layer delegates those concerns and only consumes
the execution contract and validation result exposed here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .code_validator import CodeValidator


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
        self.validator = CodeValidator(self.root, self.home_agent)

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
        return self.validator.validate_files(changed)

    def git_diff_check(self, timeout: int = 60) -> dict[str, Any]:
        """运行仓库级 git diff --check 静态检查（委托独立校验模块）。"""
        return self.validator.validate_repo(timeout=timeout)

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

    def run_autonomous_tests(self, changed: list[str], timeout: int = 180) -> dict[str, Any]:
        """Detect project types and independently rerun their local checks."""
        return self.validator.run_autonomous_tests(changed, timeout=timeout)
