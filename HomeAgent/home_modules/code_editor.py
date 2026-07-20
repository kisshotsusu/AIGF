"""Code-edit preparation, change tracking, and validation for HomeAgent.

This module deliberately contains no UI, TTS, task recovery, or Codex process
management.  The orchestration layer delegates those concerns and only consumes
the execution contract and validation result exposed here.
"""
from __future__ import annotations

import hashlib
import json
import py_compile
from pathlib import Path
from typing import Any


class CodeEditorModule:
    TRACKED_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".txt", ".bat", ".cmd", ".ps1"}
    TRACKED_AREAS = ("HomeAgent", "Vision", "Skill", "CharacterManager", "modules", "src")
    EXCLUDED_PARTS = {".git", ".venv", "node_modules", "logs", "__pycache__", "models"}
    DEVELOPMENT_DOCUMENTS = (
        "README.md",
        "AI Read/00_START_HERE.md",
        "AI Read/01_ARCHITECTURE.md",
        "AI Read/02_COMPONENTS.md",
        "AI Read/05_OPERATIONS_AND_RULES.md",
        "AI Read/06_CURRENT_STATE.md",
    )

    def __init__(self, root: Path, home_agent: Path, require_validation: bool = True):
        self.root = root.resolve()
        self.home_agent = home_agent.resolve()
        self.require_validation = bool(require_validation)
        self._baseline: dict[str, str] = {}

    @staticmethod
    def is_code_edit_request(prompt: str) -> bool:
        compact = str(prompt).replace(" ", "").lower()
        subjects = ("homeagent", "你自己", "自身", "自我", "本体", "你的代码", "程序", "系统")
        actions = ("升级", "更新", "修改", "优化", "修复", "编辑", "增加功能", "添加功能", "写代码", "改代码", "重构", "实现")
        explicit_path = "ai直播/homeagent" in compact.replace("\\", "/")
        delivery_change = "播放语音" in compact and "显示消息" in compact and any(word in compact for word in ("不要等", "立刻", "立即", "同时", "的时候"))
        return ((any(word in compact for word in subjects) or explicit_path) and any(word in compact for word in actions)) or delivery_change

    def _fingerprint(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for area in self.TRACKED_AREAS:
            folder = self.root / area
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
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

    def changed_files(self) -> list[str]:
        after = self._fingerprint()
        keys = set(self._baseline) | set(after)
        return sorted(key for key in keys if self._baseline.get(key) != after.get(key))

    def validate_files(self, changed: list[str]) -> dict[str, Any]:
        if not self.require_validation:
            return {"ok": True, "skipped": True, "checked": []}
        checked: list[str] = []
        try:
            for relative in changed:
                path = self.root / relative
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

    def build_execution_contract(self) -> tuple[str, list[str]]:
        documents, loaded = self.load_engineering_documents()
        contract = (
            "这是 HomeAgent 自编程任务，不能只给建议或代码片段，必须在本机工程中实际完成修改。\n"
            "强制流程：\n"
            "1. 先阅读已注入的 README 和 AI Read 工程文档。\n"
            "2. 用 git status 和代码搜索确认现有用户改动；不得覆盖或回退无关变更。\n"
            "3. 检查真实默认入口、业务层和相关测试，再实际编辑文件；主要写入范围是 HomeAgent，"
            "只有共享接口确实需要时才修改相关模块。\n"
            "4. 使用项目 .venv 运行 py_compile、YAML 读取和相关回归测试。\n"
            "5. 最终报告必须列出真实变更文件和验证结果；没有写入文件或测试未通过时明确返回失败。\n"
            "禁止读取或输出 .env 密钥。\n"
            f"委派前已经实际读取：{', '.join(loaded)}。以下是当前磁盘内容：\n\n{documents}\n\n"
        )
        return contract, loaded
