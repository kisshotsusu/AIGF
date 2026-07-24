from __future__ import annotations

import asyncio
import ast
import base64
import csv
import difflib
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
HOME_AGENT = ROOT / "HomeAgent"
_SCREEN_GRAB_LOCK = threading.Lock()


def _iso_now() -> str:
    """Return an ordered, timezone-aware timestamp for tool evidence."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _grab_screen_with_retry(*, all_screens: bool = False, attempts: int = 3):
    """Capture a detached RGB image, retrying transient Windows GDI failures."""
    from PIL import ImageGrab

    errors: list[str] = []
    with _SCREEN_GRAB_LOCK:
        for attempt in range(max(1, attempts)):
            strategies = [("desktop", {"all_screens": all_screens})]
            if os.name == "nt":
                try:
                    import ctypes
                    hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                    if hwnd:
                        strategies.append(("foreground-window", {"window": hwnd, "include_layered_windows": True}))
                except OSError:
                    pass
            for label, kwargs in strategies:
                source = None
                try:
                    source = ImageGrab.grab(**kwargs)
                    converted = source.convert("RGB")
                    if converted is source:
                        if hasattr(converted, "copy"):
                            return converted.copy()
                        source = None
                    return converted
                except OSError as exc:
                    errors.append(f"{label}: {exc}")
                finally:
                    if source is not None and hasattr(source, "close"):
                        source.close()
            if attempt + 1 < attempts:
                time.sleep(0.15 * (attempt + 1))
    detail = errors[-1] if errors else "unknown capture error"
    raise RuntimeError(f"screen grab failed after {max(1, attempts)} attempts: {detail}")


def _read_compatible_text(path: Path) -> tuple[str, str]:
    """Read source files and common Windows GB18030/UTF-16 logs."""
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if b"\x00" in data[:4096]:
        raise UnicodeDecodeError("binary", data, 0, 1, "NUL byte detected")
    text_suffixes = {
        ".txt", ".log", ".json", ".jsonl", ".md", ".csv", ".yaml", ".yml",
        ".ini", ".cfg", ".conf", ".py", ".ps1", ".bat", ".cmd", ".c", ".cc",
        ".cpp", ".h", ".hpp", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    }
    if path.suffix.lower() not in text_suffixes:
        raise UnicodeDecodeError("binary", data, 0, 1, "unsupported non-UTF-8 file")
    return data.decode("gb18030"), "gb18030"


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _tts_safe_text_for_fallback(text: str) -> str:
    """Keep emergency Windows speech short and compatible with local voices."""
    value = re.sub(r"[`*_#>|]", "", str(text or ""))
    return value.encode("gbk", errors="ignore").decode("gbk").strip()


sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Skill" / "schedule-home-task" / "scripts"))

from src.ai_live_assistant.tts import TTSClient, cleanup_audio_files
from src.ai_live_assistant.workspace import Workspace
from src.ai_live_assistant.long_term_memory import LongTermMemoryStore
from task_manager import TaskStore
from self_upgrade import SelfUpgradeManager
from home_modules.command_executor import CommandExecutor
from home_modules.mimo_multimodal import MiMoMultimodalClient


class HomeAgent:
    def __init__(self):
        self.project = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        cleanup_audio_files(ROOT / "audio", 20)
        self.config = yaml.safe_load((HOME_AGENT / "config.yaml").read_text(encoding="utf-8")) or {}
        for key, value in dotenv_values(ROOT / ".env").items():
            if value: os.environ[key] = value
        self.workspace = Workspace(ROOT, self.project["workspace"])
        self.history: list[dict[str, Any]] = []
        self.codex_thread_id: str | None = None
        self.task_store = TaskStore(ROOT / "Task")
        self.long_term_memory = LongTermMemoryStore(ROOT / "LongTermMemory")
        migration = self.long_term_memory.migrate_legacy(self.workspace.root / self.workspace.cfg.get("memory_dir", "memory"))
        self.tts_execution_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.active_process = None
        self.active_process_lock = threading.Lock()
        self.vision_service_process: subprocess.Popen | None = None
        self.vision_service_lock = threading.Lock()
        self.sound_service_process: subprocess.Popen | None = None
        self.sound_service_lock = threading.Lock()
        self.self_upgrade = SelfUpgradeManager(ROOT, HOME_AGENT, self.config)
        self.command_executor = CommandExecutor(ROOT)
        self.mimo_multimodal = MiMoMultimodalClient(self.project.get("mimo_multimodal", {}))
        self.restart_requested = False
        threading.Thread(target=self.ensure_vision_service, daemon=True, name="vision-mcp-autostart").start()
        threading.Thread(target=self.ensure_sound_service, daemon=True, name="sound-mcp-autostart").start()
        self.character_name = "小助手"
        self.refresh_identity()
        self.log_event("long_term_memory_migration", result=migration, total=self.long_term_memory.count())


    def is_prompt_wake_enabled(self) -> bool:
        """Check if prompt wake feature is enabled."""
        return bool(self.config.get('prompt_wake', {}).get('enabled', False))

    def get_wake_words(self) -> list:
        """Get list of wake words from config."""
        return self.config.get('prompt_wake', {}).get('wake_words', ['苏苏', '小助手'])

    def detect_wake_word(self, text: str) -> tuple:
        """
        Detect if text starts with a wake word and extract the command.
        Returns (is_wake, command_text).
        """
        if not self.is_prompt_wake_enabled():
            return False, text
        
        wake_words = self.get_wake_words()
        text_stripped = text.strip()
        
        for wake_word in wake_words:
            if text_stripped.startswith(wake_word):
                command = text_stripped[len(wake_word):].strip()
                # Remove common connectors
                command = re.sub(r'^[，,。.！!？?\s]+', '', command)
                if command:
                    self.log_event('wake_word_detected', wake_word=wake_word, command=command)
                    return True, command
        
        return False, text

    def should_auto_send_after_wake(self) -> bool:
        """Check if should auto send after wake word detection."""
        return bool(self.config.get('prompt_wake', {}).get('auto_send_after_wake', True))

    def begin_task(self, prompt: str = "", resumed: bool = False) -> None:
        self.cancel_event.clear()
        if prompt:
            if self.is_restart_request(prompt):
                self.self_upgrade.clear()
                return
            self.self_upgrade.begin(prompt, resumed=resumed)

    def update_task_recovery(self, current: str, completed: list[str]) -> None:
        self.self_upgrade.progress(current, completed)

    def finalize_task_recovery(self, answer: str) -> bool:
        direct_restart = self.restart_requested
        if direct_restart:
            # A restart command is local process control, not a recoverable task.
            self.self_upgrade.clear()
            upgrade_restart = False
        else:
            state = self.self_upgrade.read()
            if state.get("is_self_upgrade") and not bool(getattr(self, "current_code_verified", False)):
                self.self_upgrade.fail("自升级执行未取得写入并通过测试的证据")
                self.log_event("task_recovery_failed", reason="self_upgrade_not_verified")
                return False
            upgrade_restart = self.self_upgrade.finalize(answer)
        self.restart_requested = direct_restart or upgrade_restart
        self.log_event("task_recovery_finalized", restart_requested=self.restart_requested)
        return self.restart_requested

    def recover_interrupted_task(self) -> str:
        return self.self_upgrade.resume_prompt()

    def stop_current_task(self) -> bool:
        """Cancel current tool work without stopping persistent services."""
        self.cancel_event.set()
        with self.active_process_lock:
            proc = self.active_process
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=8)
                else:
                    proc.kill()
            except Exception as exc:
                self.log_event("task_stop_process_error", error=str(exc), pid=pid)
        self.log_event("current_task_stop_requested", pid=pid)
        self.self_upgrade.cancel()
        return bool(pid)

    def log_event(self, event: str, **data: Any) -> None:
        """记录 Agent 决策与工具轨迹；密钥会脱敏，单条记录限制长度。"""
        log_dir = HOME_AGENT / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        safe: dict[str, Any] = {}
        for key, value in data.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            text = re.sub(r"(?i)(bearer\s+|sk-[a-z0-9_-]{6})[a-z0-9_.-]+", r"\1***", str(text))
            safe[key] = text[:4000]
        record = {"time": datetime.now().isoformat(timespec="milliseconds"), "event": event, **safe}
        try:
            with (log_dir / "agent-events.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def context_snapshot(self) -> str:
        """供本地调试页面读取当前上下文，不包含 API Key。"""
        tools = [{"name": item["function"]["name"], "description": item["function"].get("description", "")} for item in self._tools()]
        summary = {
            "character_name": self.character_name,
            "history_messages": len(self.history),
            "max_context_messages": self.config.get("home", {}).get("max_context_messages", 30),
            "max_tool_rounds": self.config.get("agent", {}).get("max_tool_rounds", 5),
            "codex_thread_id": self.codex_thread_id,
            "codex_cli": {key: value for key, value in self._codex_config().items() if "key" not in key.lower()},
            "tools": tools,
        }
        return (
            "【Agent 状态】\n" + json.dumps(summary, ensure_ascii=False, indent=2, default=str) +
            "\n\n【当前系统提示词】\n" + self._system_prompt() +
            "\n\n【短期对话上下文】\n" + json.dumps(self.history, ensure_ascii=False, indent=2, default=str)
        )

    @staticmethod
    def _is_memory_recall_request(text: str) -> bool:
        value = str(text).replace(" ", "")
        return any(word in value for word in ("记得吗", "还记得", "记不记得", "我之前", "以前的我", "过去的我", "我的生日", "我喜欢什么", "我讨厌什么"))

    @staticmethod
    def _memory_query_tags(text: str) -> list[str]:
        value = str(text).replace(" ", "")
        known = ("生日", "喜欢", "讨厌", "睡眠", "身体", "健康", "心情", "情绪", "习惯", "约定", "名字", "称呼", "家人", "工作", "学校")
        tags = [word for word in known if word in value]
        if not tags:
            cleaned = re.sub(r"(你|还|是否|吗|呢|我|的|之前|以前|记得|记不记得|哪一天|什么)", "", value)
            if cleaned: tags.append(cleaned[:12])
        return tags or ["长期记忆"]

    def refresh_identity(self) -> str:
        path = self.workspace.root / self.project.get("workspace", {}).get("identity_file", "IDENTITY.yaml")
        try:
            identity = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            name = str(identity.get("character", {}).get("name", "")).strip()
            if name: self.character_name = name
        except (OSError, yaml.YAMLError):
            self.character_name = str(self.config.get("home", {}).get("assistant_name", "小助手"))
        self.config.setdefault("home", {})["assistant_name"] = self.character_name
        return self.character_name

    def _provider(self) -> tuple[dict[str, Any], str]:
        llm = self.project["llm"]; provider = llm["providers"][llm["provider"]]
        key = os.getenv(provider.get("api_key_env", ""), "").strip()
        if not key: raise RuntimeError(f"缺少 {provider.get('api_key_env')}，请在主项目 .env 中设置")
        return provider, key

    @staticmethod
    def _provider_headers(provider: dict[str, Any], key: str) -> dict[str, str]:
        is_mimo = "xiaomimimo" in str(provider.get("base_url", "")).lower() or str(provider.get("model", "")).lower().startswith("mimo-")
        header = str(provider.get("auth_header") or ("api-key" if is_mimo else "Authorization")).strip()
        value = f"Bearer {key}" if header.lower() == "authorization" else key
        return {header: value}

    @staticmethod
    def _set_token_limit(payload: dict[str, Any], provider: dict[str, Any], value: int) -> None:
        is_mimo = "xiaomimimo" in str(provider.get("base_url", "")).lower() or str(provider.get("model", "")).lower().startswith("mimo-")
        field = str(provider.get("max_tokens_field") or ("max_completion_tokens" if is_mimo else "max_tokens")).strip()
        payload[field] = int(value)
        extra = provider.get("extra_body", {})
        if isinstance(extra, dict):
            for key, option in extra.items():
                payload.setdefault(str(key), option)
        if is_mimo:
            payload.setdefault("thinking", {"type": "disabled"})

    def _system_prompt(self) -> str:
        home = self.config["home"]
        scene = ROOT / home.get("scene_file", "workspace/HOME.md")
        scene_text = scene.read_text(encoding="utf-8") if scene.exists() else "当前在家中进行私人对话。"
        memories = [item for item in self.workspace.recent_memories(200) if item.get("type") != "reply"][-20:]
        # 直播流水与长期记忆分层读取：保留跨场景连续性，但不把直播话术当作永久事实。
        live_conversations = self.workspace.recent_live_conversations(20)
        skills = self.list_skills()
        active_tasks = self.task_store.list()
        awaiting_tasks = self.task_store.awaiting_acknowledgements()
        summary_path = ROOT / self.config.get("context_maintenance", {}).get("summary_file", "workspace/HOME_CONTEXT_SUMMARY.md")
        context_summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else "暂无压缩摘要。"
        return (
            self.workspace.prompt_documents("home") + "\n\n" + scene_text +
            "\n\n家庭长期上下文摘要：\n" + context_summary +
            f"\n\n当前本地时间：{datetime.now():%Y-%m-%d %H:%M:%S}。当前用户称呼：{home.get('user_name', '用户')}。当前不是直播场景。" +
            "\n\n可用长期记忆：\n" + "\n".join(json.dumps(x, ensure_ascii=False) for x in memories) +
            "\n\n近期直播对话（仅用于识别共同经历和用户身份，当前回答仍须使用家庭场景话术）：\n" +
            "\n".join(json.dumps(x, ensure_ascii=False) for x in live_conversations) +
            "\n\n可用Skills：\n" + "\n".join(f"- {x['name']}: {x['description']}" for x in skills) +
            "\n\n当前任务文件快照（这是任务数量和状态的唯一可信来源，不得用历史对话推算）：\n" + json.dumps({"active_count": len(active_tasks), "tasks": active_tasks}, ensure_ascii=False) +
            "\n\n正在等待用户确认的提醒：\n" + "\n".join(json.dumps(x, ensure_ascii=False) for x in awaiting_tasks) +
            "\n长期数据库采用按需检索，不能把整个数据库放进上下文。当当前对话出现值得长期记住的身体状况、明显情绪波动、重大事件、稳定偏好或习惯、重要关系或明确约定时，必须调用 long_term_memory，输出 action=store、3-5个tags、20字以内summary和保留原文关键句的detail；天气、寒暄、玩笑和临时闲聊禁止存储。当用户询问“我之前……”“你记得……吗”或明显追问过去经历时，必须先调用 long_term_memory，输出 action=retrieve 和 query_tags，得到结果后才能回答。工具结构不要直接朗读给用户。"
            "\n长期记忆由家庭和直播共用的规则自动判断；需要搜索记忆或生成角色图片时调用工具。用户提出提醒、闹钟、定时、每天、工作日或每周重复事项时，必须调用 create_scheduled_task，不要只在文字中承诺。创建成功后只需简短确认，禁止主动朗读或复述 ISO 时间、任务 ID、文件路径和队列长度；只有用户主动查询详情时才展示。报告任务数量时只能使用当前任务快照或工具返回的 active_count，历史中“设好了几个任务”等话术一律视为过期。若用户对正在等待确认的提醒作出语义合适的回应（如知道了、完成了、喝了、起来了），必须调用 acknowledge_scheduled_task；无关回复不要确认。用户要求唱歌、唱一首或哼唱时必须调用 sing_song，使用最多十行合规或原创演唱文本，不要只用普通文字回答。不要声称已调用而不实际调用。"
        )

    def _codex_config(self) -> dict[str, Any]:
        return self.config.get("codex_cli", {})

    def _codex_command(self) -> list[str]:
        configured = str(self._codex_config().get("executable", "codex")).strip() or "codex"
        local_script = HOME_AGENT / "cli" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if configured.lower() == "codex" and local_script.is_file():
            node = shutil.which("node")
            if not node:
                candidates = sorted((Path.home() / ".cache" / "codex-runtimes").glob("**/dependencies/node/bin/node.exe"), reverse=True)
                node = str(candidates[0]) if candidates else None
            if not node:
                raise RuntimeError("已安装本地 Codex CLI，但找不到 Node.js 运行时")
            return [node, str(local_script)]
        candidate = Path(configured).expanduser()
        executable = str(candidate) if candidate.is_file() else shutil.which(configured)
        if not executable:
            raise RuntimeError("找不到 Codex CLI，请在设置中填写 codex.exe 路径")
        return [executable]

    def _codex_home_path(self) -> Path:
        if self._codex_config().get("isolated_home", True):
            return HOME_AGENT / "state" / "codex-home"
        return Path.home() / ".codex"

    @staticmethod
    def _codex_exec_command(codex_command: list[str], cfg: dict[str, Any]) -> list[str]:
        """Build a bounded Windows command line; the full prompt is sent on stdin."""
        command = [*codex_command, "exec", "--json"]
        if cfg.get("skip_git_repo_check", True):
            command.append("--skip-git-repo-check")
        sandbox = str(cfg.get("sandbox", "danger-full-access")).strip()
        if sandbox:
            command += ["--sandbox", sandbox]
        command.append("-")
        return command

    def _prepare_codex_home(self) -> Path:
        """Isolate CLI cache/plugins from the newer desktop build while sharing login."""
        target_home = self._codex_home_path()
        if target_home == Path.home() / ".codex":
            return target_home
        target_home.mkdir(parents=True, exist_ok=True)
        source_home = Path.home() / ".codex"
        source_auth = source_home / "auth.json"
        target_auth = target_home / "auth.json"
        if source_auth.is_file():
            try:
                same = target_auth.exists() and os.path.samefile(source_auth, target_auth)
            except OSError:
                same = False
            if not same:
                try:
                    if target_auth.exists(): target_auth.unlink()
                    os.link(source_auth, target_auth)
                    auth_mode = "hardlink"
                except OSError:
                    shutil.copy2(source_auth, target_auth)
                    try: os.chmod(target_auth, 0o600)
                    except OSError: pass
                    auth_mode = "copy"
                self.log_event("codex_isolated_auth_synced", mode=auth_mode, target=target_auth)
        source_config = source_home / "config.toml"
        source_text = source_config.read_text(encoding="utf-8") if source_config.is_file() else ""
        def top_value(name: str, default: str) -> str:
            match = re.search(rf'(?m)^{re.escape(name)}\s*=\s*"([^"]+)"', source_text)
            return match.group(1) if match else default
        model = top_value("model", "gpt-5.6-sol")
        effort = top_value("model_reasoning_effort", "low")
        tier = top_value("service_tier", "default")
        vision_url = str(self.config.get("vision_mcp", {}).get("url", "http://127.0.0.1:8765/mcp"))
        minimal_config = (
            f'model = {json.dumps(model)}\n'
            f'model_reasoning_effort = {json.dumps(effort)}\n'
            f'service_tier = {json.dumps(tier)}\n\n'
            '[features]\n'
            'apps = false\n'
            'plugins = false\n'
            'remote_plugin = false\n'
            'shell_snapshot = false\n\n'
            '[mcp_servers.vision-gui]\n'
            f'url = {json.dumps(vision_url)}\n'
        )
        config_path = target_home / "config.toml"
        if not config_path.exists() or config_path.read_text(encoding="utf-8") != minimal_config:
            temporary = config_path.with_suffix(".tmp")
            temporary.write_text(minimal_config, encoding="utf-8")
            os.replace(temporary, config_path)
        return target_home

    def _codex_environment(self, command: list[str]) -> dict[str, str]:
        codex_home = self._prepare_codex_home()
        self._repair_codex_models_cache(codex_home)
        env = os.environ.copy()
        node_dir = str(Path(command[0]).parent) if Path(command[0]).name.lower() == "node.exe" else ""
        if node_dir:
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "CODEX_HOME": str(codex_home)})
        return env

    def _codex_package_version(self) -> str:
        package = HOME_AGENT / "cli" / "node_modules" / "@openai" / "codex" / "package.json"
        try:
            return str(json.loads(package.read_text(encoding="utf-8")).get("version", "")).strip()
        except (OSError, json.JSONDecodeError):
            return ""

    def _repair_codex_models_cache(self, codex_home: Path | None = None) -> dict[str, Any]:
        """Backfill fields required by the bundled CLI without discarding desktop cache data."""
        path = (codex_home or self._codex_home_path()) / "models_cache.json"
        result = {"path": str(path), "changed": False, "cache_client_version": "", "cli_package_version": self._codex_package_version()}
        try:
            if not path.is_file():
                return result
            payload = json.loads(path.read_text(encoding="utf-8"))
            result["cache_client_version"] = str(payload.get("client_version", ""))
            models = payload.get("models") or []
            changed = False
            responses_lite_disabled = 0
            cli_version = str(result.get("cli_package_version") or "").strip()
            if cli_version and str(payload.get("client_version", "")).strip() != cli_version:
                payload["client_version"] = cli_version
                changed = True
            for model in models:
                if not isinstance(model, dict):
                    continue
                if "supports_reasoning_summaries" not in model:
                    summary = str(model.get("default_reasoning_summary", "")).strip().lower()
                    model["supports_reasoning_summaries"] = summary not in {"", "none", "disabled", "off"}
                    changed = True
                # Desktop 0.145 cache enables Responses Lite, but standalone
                # CLI 0.144.x does not always emit reasoning.context=all_turns
                # for tool/MCP requests. Use the standard Responses transport.
                if cli_version.startswith("0.144.") and model.get("use_responses_lite") is True:
                    model["use_responses_lite"] = False
                    responses_lite_disabled += 1
                    changed = True
            if changed:
                temporary = path.with_name(f"{path.name}.home-agent-{os.getpid()}.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(temporary, path)
                result["changed"] = True
                result["effective_client_version"] = str(payload.get("client_version", ""))
                result["responses_lite_disabled"] = responses_lite_disabled
                self.log_event("codex_models_cache_compatibility_repaired", result=result)
            return result
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            result["error"] = str(exc)
            self.log_event("codex_models_cache_compatibility_failed", error=str(exc), path=path)
            return result

    def _codex_task_timeout(self, preferred_mcp: str = "") -> int:
        if preferred_mcp:
            return max(30, int(self.config.get("vision_mcp", {}).get("task_timeout_seconds", 180)))
        return max(30, int(self._codex_config().get("timeout_seconds", 600)))

    @staticmethod
    def _codex_progress_text(event: dict[str, Any]) -> str:
        event_type = str(event.get("type", ""))
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type", "")).lower()
        name = str(item.get("name") or item.get("tool") or item.get("command") or "").strip()
        if event_type == "thread.started": return "Codex 已建立任务，正在分析…"
        if event_type == "turn.started": return "Codex 正在制定执行步骤…"
        if event_type == "error": return "Codex 网络响应异常，正在重试…"
        if "mcp" in item_type:
            return f"Codex 正在调用 MCP：{name or item_type}"
        if any(token in item_type for token in ("command", "shell", "exec")):
            return f"Codex 正在执行命令：{name[:60]}" if name else "Codex 正在执行本地命令…"
        if item_type == "agent_message": return "Codex 已完成执行，正在整理结果…"
        if event_type == "turn.completed": return "Codex 执行完成，正在验证结果…"
        return ""

    def _should_route_to_codex(self, text: str) -> bool:
        cfg = self._codex_config()
        if not cfg.get("enabled", False):
            return False
        if self.self_upgrade.code_editor.is_code_task(text) and self.config.get("agent", {}).get("prefer_local_code_tools", True):
            lowered = str(text).lower()
            return any(word in lowered for word in ("codex", "调用cli", "调用 cli", "使用cli", "使用 cli"))
        mode = str(cfg.get("trigger_mode", "auto")).lower()
        if mode == "always":
            return True
        if mode != "auto":
            return False
        lowered = text.lower()
        return any(str(word).strip().lower() in lowered for word in cfg.get("trigger_keywords", []) if str(word).strip())

    def _should_route_to_vision(self, task_plan: dict[str, Any]) -> bool:
        cfg = self.config.get("vision_mcp", {})
        if not cfg.get("enabled", True) or not cfg.get("gui_enabled", True):
            return False
        return bool(task_plan.get("visual_required"))

    @staticmethod
    def _should_route_to_web(task_plan: dict[str, Any]) -> bool:
        """Route from the validated model plan, never from words in the request."""
        return bool(task_plan.get("is_task") and task_plan.get("actionable") and task_plan.get("domain") == "web")

    @staticmethod
    def _planner_context(history: list[dict[str, Any]], limit: int = 8) -> str:
        """Preserve both sides of the recent conversation for semantic planning."""
        rows = []
        for item in history[-max(1, int(limit)):]:
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            rows.append({
                "role": role,
                "content": str(item.get("content") or "")[:800],
                "source": str(item.get("source") or "chat")[:80],
            })
        return json.dumps(rows, ensure_ascii=False)

    @staticmethod
    def _image_message_content(text: str, image_paths) -> list[dict[str, Any]]:
        """Build one ephemeral MiMo message containing all pasted images."""
        values = [image_paths] if isinstance(image_paths, (str, Path)) else list(image_paths or [])
        if not values:
            raise ValueError("没有可提交的图片")
        content: list[dict[str, Any]] = []
        total_encoded = 0
        for value in values:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"粘贴的图片不存在：{path}")
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            if not mime.startswith("image/"):
                raise ValueError(f"不支持的图片类型：{path.name}")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            if len(encoded) > 10 * 1024 * 1024:
                raise ValueError(f"图片 {path.name} 编码后超过 10 MB，请先缩小图片")
            total_encoded += len(encoded)
            if total_encoded > 30 * 1024 * 1024:
                raise ValueError("全部图片编码后超过 30 MB，请减少图片数量或缩小图片")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        prompt = str(text or "").strip() or ("请分析这些图片。" if len(values) > 1 else "请分析这张图片。")
        content.append({"type": "text", "text": prompt})
        return content

    @staticmethod
    def _is_file_authoring_request(text: str) -> bool:
        """Recognize direct file/script creation without pretending it is a GUI task."""
        value = str(text or "").lower().replace(" ", "")
        action = any(word in value for word in ("写个", "写一个", "编写", "创建", "新建", "生成", "修改", "修复"))
        target = any(ext in value for ext in (".bat", ".cmd", ".ps1", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".ini", ".md", ".txt")) or any(
            word in value for word in ("bat", "批处理", "一键启动", "启动脚本", "脚本文件", "配置文件")
        )
        return action and target

    @staticmethod
    def _favorite_folder_key(value: str) -> str:
        """Convert a conversational folder mention or API title to a comparable key."""
        name = str(value or "").strip()
        name = re.sub(r"[\s,，。.!！?？:：、;；'\"“”‘’]+", "", name)
        for _ in range(4):
            previous = name
            name = re.sub(r"^(?:(?:请|麻烦)?(?:帮我)?(?:打开|进入|查看))+", "", name)
            name = re.sub(r"^(?:(?:bilibili|哔哩哔哩|B站)(?:里|中|的)*)+", "", name, flags=re.I)
            name = re.sub(r"^(?:(?:我|本人)的)+", "", name)
            if name == previous:
                break
        name = re.sub(r"(?:收藏夹|收藏)$", "", name)
        return name.casefold()

    @staticmethod
    def _normalize_favorite_folder_name(value: str) -> str:
        """Produce a display/API candidate without enumerating whole user phrases."""
        key = HomeAgent._favorite_folder_key(value)
        return "默认收藏夹" if not key or key == "默认" else f"{key}收藏夹"

    @staticmethod
    def _resolve_favorite_folder(requested: str, folders: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Resolve against the account's live folder titles using exact and scored evidence."""
        requested_key = HomeAgent._favorite_folder_key(requested)
        candidates = [(item, str(item.get("title", "")).strip(), HomeAgent._favorite_folder_key(item.get("title", ""))) for item in folders]
        exact = [item for item, _title, key in candidates if key == requested_key]
        if len(exact) == 1:
            return exact[0], []
        scored: list[dict[str, Any]] = []
        for item, title, key in candidates:
            if not key:
                continue
            containment = bool(requested_key and (requested_key in key or key in requested_key))
            ratio = difflib.SequenceMatcher(None, requested_key, key).ratio()
            score = max(ratio, 0.92 if containment else 0.0)
            scored.append({"item": item, "title": title, "key": key, "score": score})
        scored.sort(key=lambda row: row["score"], reverse=True)
        if scored and scored[0]["score"] >= 0.82 and (len(scored) == 1 or scored[0]["score"] - scored[1]["score"] >= 0.08):
            return scored[0]["item"], scored
        return None, scored

    @staticmethod
    def _analyze_task(text: str, context: str = "") -> dict[str, Any]:
        """Return a conservative contract only when the semantic planner is unavailable."""
        value = str(text).strip()
        return {
            "goal": value, "is_task": False, "response_mode": "answer", "execution_strategy": "direct_answer",
            "domain": "conversation", "actionable": False, "multi_step": False, "requires_mcp": False,
            "site": "", "operation": "conversation", "handler": None, "query": "", "query_is_explicit": False, "index": None,
            "favorite_folder": "", "steps": [], "preferred_tools": [], "required_capabilities": [],
            "browser_policy": "not_applicable", "visual_required": False, "interaction_mode": "none",
            "implementation_change": False,
            "requires_clarification": False, "clarification_question": "", "risk_level": "low",
            "final_action_requires_verification": False, "success_criteria": "给出准确、直接的回答",
        }

    @staticmethod
    def _apply_cloudmusic_handler(plan: dict[str, Any]) -> dict[str, Any]:
        """Keep only the planner's target semantics; execution stays model-driven."""
        operation = str(plan.get("operation") or "")
        explicit_target = bool(plan.get("query_is_explicit"))
        search_requested = operation == "search" and bool(plan.get("query"))
        if not (explicit_target or search_requested):
            plan["query"] = ""
        plan["handler"] = "model_ui"
        return plan

    @staticmethod
    def _apply_implementation_change_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """Enforce the model's semantic decision that the requested result is code."""
        if not plan.get("implementation_change"):
            return plan
        plan.update({
            "domain": "code", "operation": "code", "actionable": True,
            "requires_mcp": False, "visual_required": False, "interaction_mode": "none",
            "execution_strategy": "code_loop", "response_mode": "execute",
            "browser_policy": "not_applicable", "handler": None, "site": "",
        })
        return plan

    async def _plan_task(self, text: str, context: str = "") -> dict[str, Any]:
        """Use the LLM for semantic planning, then enforce deterministic safety invariants."""
        fallback = self._analyze_task(text, context)
        cfg = self.config.get("semantic_planner", {})
        if not cfg.get("enabled", True):
            fallback["planner"] = "disabled"
            if fallback.get("requires_mcp"):
                fallback["handler"] = None
                fallback["requires_clarification"] = True
                fallback["success_criteria"] = "先确认目标对象，禁止猜测后执行"
            return fallback
        try:
            provider, key = self._provider()
            prompt = (
                "你是 Home Agent 的总任务判定器和执行规划器。结合当前请求与最近上下文，输出一个JSON对象，不要输出解释或Markdown。"
                "最重要的语义边界：先找用户最终要求取得的结果，不要把故障描述当成待执行动作。"
                "“执行某请求后读取了屏幕/调用了某工具/输出了某JSON”中的“读取、调用、输出”是在报告已经发生的错误，绝不能规划为下一步。"
                "只要最终诉求是检查并修复、修改程序、优化页面行为、减少程序输出或防止错误再次发生，implementation_change必须为true，"
                "即使消息多次出现屏幕、页面、窗口、观察结果、工具名，或粘贴完整窗口JSON，也禁止visual_required和任何UI观察工具。"
                "故障证据中的工具名不得复制到preferred_tools或steps；需要改的是产生错误的代码。"
                "完整示例：“执行命令后优先读取了屏幕，这是硬编码错误，检查并修复；Home Agent程序页面展示任务过程细节过多，减少细节；"
                "后附窗口observation JSON”必须判定implementation_change=true、domain=code、visual_required=false、execution_strategy=code_loop。"
                "先判断当前消息是否要求助手取得结果、检查状态、查找信息、修改内容或执行动作：是则is_task=true；闲聊、致谢、确认、简单回应为false。"
                "知识问答、解释、总结、翻译、建议、计算等即使不需要工具，也属于任务：例如‘解释递归’必须is_task=true、actionable=false、direct_answer；"
                "‘好的/谢谢/你好吗’属于非任务对话。domain=code只用于检查或修改代码，domain=file用于普通文件读写。"
                "编辑角色设定、固定外观说明、提示词、记忆、普通Markdown文档、数据或图片素材属于内容/资产任务："
                "必须判定implementation_change=false、domain=file、execution_strategy=tool_loop；持久写入文件本身不等于修改程序实现。"
                "例如“读取你自己的角色三视图，并完善固定外观文档”应先列出已登记角色图片，使用清单返回的绝对路径分析三视图，"
                "再读取、写入并重新读取CHARACTER.md；不得进入code_loop或调用code_validate_project。"
                "若当前请求标记已附带剪贴板截图，图片会直接随执行消息提供：仅分析附件时actionable=false、visual_required=false、direct_answer；"
                "visual_required只表示还必须读取当前实时屏幕，不要把已有图片附件误判为实时读屏。"
                "implementation_change表示用户要持久修改程序实现、程序界面、页面行为、任务路由或输出方式，而不是临时操作正在运行的界面。"
                "implementation_change=true时必须使用domain=code、operation=code、visual_required=false、interaction_mode=none、execution_strategy=code_loop；"
                "页面、窗口、屏幕等词只是被修改的软件对象，不能据此优先读屏。用户粘贴的日志、窗口列表或工具JSON只是故障证据，不是要求执行这些工具。"
                "例如“检查你自己的Home Agent程序页面，减少任务过程展示的细节”是implementation_change=true的代码修改任务；"
                "“看看当前Home Agent窗口显示了什么”才是implementation_change=false、visual_required=true的实时观察任务。"
                "is_task不等于actionable：可以仅靠模型知识回答的任务actionable=false；必须读取外部状态或调用工具才为true。"
                "response_mode只能是answer/execute/clarify；execution_strategy只能是direct_answer/tool_loop/vision_loop/web_loop/code_loop。"
                "只有缺少的信息会实质改变目标、阻止执行或带来风险时才clarify，并给出具体clarification_question；不要为可从屏幕或工具观察的信息追问。"
                "当前消息明确指定的平台、软件和对象必须覆盖历史上下文；不要把泛称当作具体搜索词。"
                "用户说停止、暂停或关掉音乐时，operation=stop_media、required_capabilities包含media_control、preferred_tools包含media_stop；"
                "这表示停止声音而不是退出应用，禁止规划Space切换、Alt+F4、Stop-Process或taskkill。"
                "用户明确要求关闭/退出应用时用operation=close_app；明确要求结束/强制终止进程，或常规停止失败且目标必须退出时，"
                "用operation=terminate_process并加入process_termination能力，此类计划允许Stop-Process/taskkill。"
                "query必须是用户真正指定的内容对象原文（例如歌曲、视频标题、人名或搜索关键词），不能包含操作动词、连接词、平台名或软件名；"
                "site已经表示平台，绝对不能再把网易云音乐、B站、浏览器等平台/软件名称填进query。"
                "query_is_explicit表示用户是否明确给出了要搜索/选择的具体内容；只要求打开软件、播放当前/任意内容时为false且query必须为空，明确给出歌名、歌手、标题或明确要求搜索某关键词时才为true。"
                "例1：‘打开网易云音乐播放音乐’应为site=cloudmusic、operation=play、query=''、query_is_explicit=false；"
                "例2：‘打开网易云音乐播放稻香’应为site=cloudmusic、operation=play、query='稻香'、query_is_explicit=true；"
                "桌面任务的steps必须由你按目标规划成可执行流程，并使用条件步骤：先检查程序是否已打开，再读取当前窗口；"
                "只有query_is_explicit=true或operation=search时才规划点击搜索框和输入query；结果出现后先识别匹配项，再点击播放并重新识别终态。"
                "query为空的通用播放任务禁止规划搜索，应打开/激活程序、识别当前播放区，并按当前状态决定是否点击播放。"
                "一句话含多个动作时要理解动作之间的关系，不要把动作之间的文字误当成目标。"
                "只负责理解和规划，不得声称任务已经完成。必须输出全部字段："
                "is_task, response_mode, execution_strategy, domain(conversation/web/desktop/file/code/memory), site(bilibili/cloudmusic/空), "
                "operation(open/search/play/control/stop_media/close_app/terminate_process/favorites/form/file/code/conversation/observe_screen/solve_screen/play_game), handler, query, query_is_explicit, "
                "favorite_folder, index(整数或null), actionable, multi_step, requires_mcp, browser_policy, risk_level(low/medium/high), "
                "visual_required(是否必须读取当前屏幕), interaction_mode(none/observe/solve/game), implementation_change(是否要求持久修改程序实现或程序UI), "
                "只描述画面使用observe；要求读取题目后计算、回答、选择答案或解谜必须使用solve；要求持续操控游戏使用game。"
                "preferred_tools(工具名字符串数组), required_capabilities(能力字符串数组), steps(字符串数组；执行动作后必须包含重新观察并验证终态), "
                "needs_clarification, clarification_question, final_action_requires_verification(是否要求播放/提交等终态), "
                "success_criteria, confidence(0到1), reasoning_short。工具可从ui_analyze_screen/ui_inspect_target/web_read/web_fill/web_click_text/"
                "ui_list_windows/ui_activate_window/ui_analyze_window/ui_click_window/ui_double_click_window/ui_hotkey/ui_type_active_text/"
                "launch_app/media_stop/list_character_images/analyze_image/read_text_file/write_text_file/code_tools中选择，不要发明工具。\n"
                f"最近上下文：{context[-1200:]}\n当前请求：{text}"
            )
            timeout_seconds = max(3, min(20, int(cfg.get("timeout_seconds", 10))))
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {"model": provider["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
                self._set_token_limit(payload, provider, int(cfg.get("max_tokens", 900)))
                async with session.post(provider["base_url"].rstrip("/") + "/chat/completions", json=payload, headers=self._provider_headers(provider, key)) as response:
                    raw = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"planner HTTP {response.status}: {raw[:300]}")
            planner_choice = json.loads(raw)["choices"][0]
            if self._is_incomplete_model_response(planner_choice.get("finish_reason")):
                raise ValueError(f"planner response is incomplete: {planner_choice.get('finish_reason')}")
            content = str(planner_choice["message"].get("content", "")).strip()
            match = re.search(r"\{[\s\S]*\}", content)
            if not match:
                raise ValueError("planner did not return JSON")
            proposed = json.loads(match.group(0))
            if not isinstance(proposed, dict) or float(proposed.get("confidence", 0)) < float(cfg.get("minimum_confidence", 0.55)):
                raise ValueError("planner confidence is too low")

            required_fields = {"is_task", "response_mode", "execution_strategy", "actionable"}
            if not required_fields.issubset(proposed):
                raise ValueError("planner response is missing task-decision fields")
            for boolean_field in ("is_task", "actionable", "multi_step", "requires_mcp", "final_action_requires_verification", "visual_required", "needs_clarification", "query_is_explicit", "implementation_change"):
                if boolean_field in proposed and not isinstance(proposed[boolean_field], bool):
                    raise ValueError(f"planner field {boolean_field} must be boolean")
            plan = dict(fallback)
            allowed_handlers = {None, "", "bilibili_favorites", "bilibili_search", "cloudmusic_search", "cloudmusic_control", "model_ui"}
            for key_name in ("domain", "site", "operation", "query", "favorite_folder", "browser_policy", "success_criteria", "reasoning_short", "clarification_question"):
                if key_name in proposed:
                    plan[key_name] = str(proposed.get(key_name) or "").strip()
            if plan.get("domain") not in {"conversation", "web", "desktop", "file", "code", "memory"}:
                plan["domain"] = "conversation"
            if plan.get("site") not in {"", "bilibili", "cloudmusic"}:
                plan["site"] = ""
            for key_name in ("is_task", "actionable", "multi_step", "requires_mcp", "final_action_requires_verification", "visual_required", "query_is_explicit", "implementation_change"):
                if key_name in proposed:
                    plan[key_name] = bool(proposed[key_name])
            for list_name in ("steps", "preferred_tools", "required_capabilities"):
                if isinstance(proposed.get(list_name), list):
                    plan[list_name] = [str(item).strip() for item in proposed[list_name] if str(item).strip()][:10]
            try:
                plan["index"] = int(proposed["index"]) if proposed.get("index") is not None else None
            except (TypeError, ValueError):
                plan["index"] = fallback.get("index")
            proposed_handler = proposed.get("handler")
            plan["handler"] = proposed_handler if proposed_handler in allowed_handlers else None
            response_mode = str(proposed.get("response_mode") or "answer").strip().lower()
            plan["response_mode"] = response_mode if response_mode in {"answer", "execute", "clarify"} else "answer"
            strategy = str(proposed.get("execution_strategy") or "direct_answer").strip().lower()
            plan["execution_strategy"] = strategy if strategy in {"direct_answer", "tool_loop", "vision_loop", "web_loop", "code_loop"} else "direct_answer"
            risk_level = str(proposed.get("risk_level") or "low").strip().lower()
            plan["risk_level"] = risk_level if risk_level in {"low", "medium", "high"} else "low"
            plan["requires_clarification"] = bool(proposed.get("needs_clarification")) or plan["response_mode"] == "clarify"
            interaction_mode = str(proposed.get("interaction_mode") or "none").strip().lower()
            plan["interaction_mode"] = interaction_mode if interaction_mode in {"none", "observe", "solve", "game"} else "none"
            if plan.get("implementation_change"):
                # This is enforced from the planner's semantic decision, not from
                # request keywords. Live pixels are not a prerequisite for a
                # persistent implementation change, even when the evidence names
                # windows, pages, screenshots, or UI tools.
                self._apply_implementation_change_plan(plan)
            if plan.get("visual_required"):
                plan["domain"] = "desktop"
                plan["actionable"] = True
                plan["requires_mcp"] = True
                plan["execution_strategy"] = "vision_loop"
                plan["response_mode"] = "execute"
                plan["browser_policy"] = "prefer_existing"
                if not plan.get("steps"):
                    plan["steps"] = ["读取当前屏幕", "按任务目标分析画面", "执行必要操作并重新读取画面验证"]
                plan["success_criteria"] = str(plan.get("success_criteria") or "以最新屏幕观察证明任务完成")

            # Specialized handlers are selected only from the model's structured
            # site/operation decision. Raw request keywords never override it.
            if plan.get("site") == "cloudmusic":
                self._apply_cloudmusic_handler(plan)
            elif plan.get("site") == "bilibili":
                if plan.get("operation") == "favorites":
                    plan["handler"] = "bilibili_favorites"
                    plan["browser_policy"] = "existing_profile_only"
                elif plan.get("query"):
                    plan["handler"] = "bilibili_search"
            if plan.get("actionable") and plan.get("domain") == "web":
                plan["execution_strategy"] = "web_loop"
                plan["response_mode"] = "execute"
            elif plan.get("actionable") and plan.get("domain") == "code":
                plan["execution_strategy"] = "code_loop"
                plan["response_mode"] = "execute"
            elif plan.get("actionable") and plan.get("execution_strategy") == "direct_answer":
                plan["execution_strategy"] = "tool_loop"
                plan["response_mode"] = "execute"
            if not plan.get("is_task"):
                plan.update({
                    "domain": "conversation", "operation": "conversation", "actionable": False, "requires_mcp": False,
                    "response_mode": "answer", "execution_strategy": "direct_answer", "requires_clarification": False,
                    "clarification_question": "", "handler": None, "visual_required": False, "interaction_mode": "none",
                })
            plan["planner"] = "llm_validated"
            plan["planner_confidence"] = round(float(proposed.get("confidence", 0)), 3)
            return plan
        except Exception as exc:
            fallback["planner"] = "deterministic_fallback"
            fallback["planner_error"] = str(exc)[:500]
            if fallback.get("requires_mcp"):
                fallback["handler"] = None
                fallback["requires_clarification"] = True
                fallback["success_criteria"] = "先确认目标对象，禁止猜测后执行"
            self.log_event("semantic_planner_fallback", error=str(exc), fallback=fallback)
            return fallback

    @staticmethod
    def _normalize_tool_result(name: str, result: Any) -> Any:
        """Give the model explicit success/failure semantics instead of ambiguous raw output."""
        if isinstance(result, dict):
            normalized = dict(result)
            if normalized.get("error"):
                normalized.setdefault("status", "failed")
                normalized.setdefault("next_action", "检查错误原因，换一种可用工具或路径继续；未经终态验证不得报告成功")
            elif normalized.get("cancelled"):
                normalized.setdefault("status", "cancelled")
                normalized.setdefault("next_action", "尊重用户取消，不再执行该操作")
            else:
                normalized.setdefault("status", "success")
                normalized.setdefault("evidence", {key: normalized[key] for key in ("url", "path", "title", "opened", "played") if key in normalized})
            normalized.setdefault("tool", name)
            return normalized
        if isinstance(result, list): return {"status": "success", "tool": name, "count": len(result), "items": result}
        return {"status": "success", "tool": name, "result": result}

    @staticmethod
    def _emit_activity(status, payload: dict[str, Any], fallback: str) -> None:
        """Expose concise decision/action summaries without revealing hidden reasoning."""
        if not status:
            return
        status(payload if getattr(status, "supports_structured_status", False) else fallback)

    @staticmethod
    def _activity_text(value: Any, limit: int = 96) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @classmethod
    def _tool_activity_arguments(cls, name: str, args: dict[str, Any]) -> str:
        """Describe intent without echoing payloads, coordinates, or private UI state."""
        if name == "ui_list_windows":
            return "检查当前可用的应用窗口"
        if name in {"ui_analyze_screen", "ui_analyze_window"}:
            return "读取画面并判断下一步"
        if name in {"ui_click_window", "ui_double_click_window"}:
            target = args.get("target") or args.get("description") or args.get("element")
            return f"目标：{cls._activity_text(target, 54)}" if target else "按识别结果操作目标控件"
        if name == "ui_type_active_text":
            return f"向当前输入位置写入 {len(str(args.get('text') or ''))} 个字符"
        if name == "ui_hotkey":
            return "发送所需快捷键"
        if name == "launch_app":
            return f"应用：{cls._activity_text(args.get('app') or args.get('name'), 54)}"
        if name in {"read_text_file", "write_text_file", "code_read_file", "code_write_file", "code_replace_text"}:
            path = args.get("path") or args.get("file_path")
            return f"文件：{cls._activity_text(path, 76)}" if path else "处理目标文件"
        if name == "code_search_text":
            query = args.get("query") or args.get("text") or args.get("pattern")
            return f"查找：{cls._activity_text(query, 64)}" if query else "定位相关实现"
        if name == "code_validate_project":
            return "运行项目检查和相关测试"
        return ""

    @classmethod
    def _tool_activity_result(cls, name: str, result: Any) -> str:
        """Summarize evidence; never render raw tool JSON in the task card."""
        if not isinstance(result, dict):
            return "工具已返回结果"
        state = str(result.get("status") or "success")
        if state in {"failed", "uncertain"}:
            reason = result.get("error") or result.get("warning") or result.get("reason") or result.get("next_action")
            return cls._activity_text(reason or ("执行失败" if state == "failed" else "结果仍需确认"), 110)
        if name == "ui_list_windows":
            observation = result.get("observation")
            count = len(observation) if isinstance(observation, list) else int(result.get("count") or 0)
            return f"找到 {count} 个可用窗口"
        if name in {"ui_analyze_screen", "ui_analyze_window"}:
            return "画面识别完成，已获得状态摘要"
        if name in {"ui_click_window", "ui_double_click_window", "ui_type_active_text", "ui_hotkey"}:
            observation = result.get("observation")
            changed = isinstance(observation, dict) and (
                observation.get("changed") or observation.get("title_changed")
            )
            return "操作完成，界面状态已变化" if changed else "操作已发送，等待后续验证"
        if name in {"code_search_text", "code_list_files"}:
            matches = result.get("matches") or result.get("items")
            count = len(matches) if isinstance(matches, list) else result.get("count")
            return f"定位到 {count} 项相关内容" if count is not None else "已定位相关内容"
        if name == "code_validate_project":
            return "项目检查和相关测试已通过" if result.get("ok", True) else "验证完成"
        if name.startswith("code_") or name in {"read_text_file", "write_text_file"}:
            return "文件处理完成"
        return cls._activity_text(result.get("next_action") or result.get("message") or "执行完成", 96)

    @staticmethod
    def _tool_display_name(name: str) -> str:
        labels = {
            "ui_list_windows": "读取可见窗口", "ui_activate_window": "激活目标窗口",
            "ui_analyze_window": "识别窗口画面", "ui_analyze_screen": "识别当前屏幕",
            "ui_click_window": "点击界面目标", "ui_double_click_window": "双击界面目标",
            "ui_type_active_text": "输入文字", "ui_hotkey": "执行快捷键",
            "launch_app": "启动应用", "read_text_file": "读取文件", "write_text_file": "写入文件",
            "code_read_file": "读取代码", "code_search_text": "搜索代码",
            "code_replace_text": "修改代码", "code_write_file": "写入代码",
            "code_validate_project": "验证代码和测试", "media_stop": "停止媒体",
        }
        return labels.get(name, name)

    @staticmethod
    def _is_media_stop_plan(plan: dict[str, Any]) -> bool:
        """Recognize the validated plan shape used for idempotent media stopping."""
        if HomeAgent._allows_application_termination(plan):
            return False
        capabilities = {str(value).strip().lower() for value in plan.get("required_capabilities", [])}
        combined = " ".join(str(plan.get(key) or "") for key in ("goal", "success_criteria", "query"))
        return bool(
            plan.get("operation") == "stop_media"
            or "media_control" in capabilities
            or ("音乐" in combined and any(word in combined for word in ("停止", "关掉", "暂停")))
        )

    @staticmethod
    def _allows_application_termination(plan: dict[str, Any]) -> bool:
        capabilities = {str(value).strip().lower() for value in plan.get("required_capabilities", [])}
        return (
            plan.get("operation") in {"close_app", "terminate_process"}
            or bool({"application_close", "process_termination"} & capabilities)
        )

    @staticmethod
    def _parse_tool_arguments(value: Any) -> dict[str, Any]:
        """Parse function arguments without silently turning malformed JSON into an action."""
        if isinstance(value, dict):
            return dict(value)
        parsed = json.loads(str(value or "{}"))
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return parsed

    @staticmethod
    def _is_incomplete_model_response(finish_reason: Any) -> bool:
        """Reject responses that the MiMo API marks as truncated or filtered."""
        return str(finish_reason or "").strip().lower() in {"length", "content_filter", "repetition_truncation"}

    def ensure_vision_service(self, wait_until_ready: bool = False) -> bool:
        with self.vision_service_lock:
            return self._ensure_vision_service_unlocked(wait_until_ready)

    def ensure_sound_service(self, wait_until_ready: bool = False) -> bool:
        """Keep the local SenseVoice HTTP MCP alive; model loading remains lazy."""
        cfg = self.config.get("stt", {})
        if cfg.get("mode") != "sound_mcp" or not cfg.get("auto_start", True): return False
        host = str(cfg.get("mcp_host", "127.0.0.1")); port = int(cfg.get("mcp_port", 8766))

        def ready() -> bool:
            try:
                with socket.create_connection((host, port), timeout=0.5): return True
            except OSError: return False

        with self.sound_service_lock:
            if ready(): return True
            if not self.sound_service_process or self.sound_service_process.poll() is not None:
                python = ROOT / ".venv" / "Scripts" / "python.exe"
                server = ROOT / "Sound" / "mcp_server.py"
                if not python.exists() or not server.exists(): return False
                log_dir = ROOT / "Sound" / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
                log = (log_dir / "sound-mcp.log").open("a", encoding="utf-8")
                env = os.environ.copy(); env.update({
                    "SOUND_MCP_TRANSPORT": "http", "SOUND_MCP_HOST": host, "SOUND_MCP_PORT": str(port),
                    "SENSEVOICE_MODEL": str(ROOT / "Sound" / "models" / "SenseVoiceSmall"),
                    "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                })
                flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
                self.sound_service_process = subprocess.Popen(
                    [str(python), str(server)], cwd=str(server.parent), env=env,
                    stdout=log, stderr=log, creationflags=flags,
                )
                self.log_event("sound_service_started", pid=self.sound_service_process.pid, port=port)
            if not wait_until_ready: return True
            deadline = time.monotonic() + float(cfg.get("startup_timeout_seconds", 30))
            while time.monotonic() < deadline:
                if ready(): return True
                if self.sound_service_process.poll() is not None: return False
                time.sleep(0.5)
            return False

    async def _sound_mcp_transcribe(self, wav_path: Path) -> str:
        if not await asyncio.to_thread(self.ensure_sound_service, True):
            raise RuntimeError("SenseVoice 语音识别服务启动失败，请检查 Sound/logs/sound-mcp.log")
        cfg = self.config.get("stt", {})
        helper = ROOT / "Vision" / "mcp_call.py"
        url = str(cfg.get("mcp_url") or "http://127.0.0.1:8766/mcp")
        arguments = json.dumps({"path": str(wav_path), "language": str(cfg.get("language", "auto"))}, ensure_ascii=False)
        client_env = os.environ.copy()
        client_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        proc = await asyncio.create_subprocess_exec(
            str(ROOT / ".venv" / "Scripts" / "python.exe"), str(helper), url, "transcribe_file", arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, env=client_env,
        )
        out, err = await proc.communicate()
        lines = out.decode("utf-8", "replace").strip().splitlines()
        try: payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError: payload = {}
        if proc.returncode or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or err.decode("utf-8", "replace")[-800:] or "SenseVoice MCP 调用失败")
        result = str(payload.get("text", ""))
        match = re.search(r"(?:^|\n)文本:\s*(.*?)(?:\n原始|$)", result, re.S)
        text = (match.group(1) if match else result).strip()
        if not text: raise RuntimeError("SenseVoice 没有识别出文本")
        return text

    def _ensure_vision_service_unlocked(self, wait_until_ready: bool = False) -> bool:
        cfg = self.config.get("vision_mcp", {})
        if not cfg.get("enabled", True) or not cfg.get("auto_start", True): return False
        host = str(cfg.get("host", "127.0.0.1")); port = int(cfg.get("port", 8765))
        def ready() -> bool:
            try:
                with socket.create_connection((host, port), timeout=0.5): return True
            except OSError: return False
        if ready(): return True
        if self.vision_service_process and self.vision_service_process.poll() is None:
            if not wait_until_ready: return True
            deadline = time.monotonic() + float(cfg.get("startup_timeout_seconds", 120))
            while time.monotonic() < deadline:
                if ready(): return True
                time.sleep(1)
            return False
        python = ROOT / ".venv" / "Scripts" / "python.exe"; server = ROOT / "Vision" / "mcp_server.py"
        if not python.exists() or not server.exists():
            self.log_event("vision_service_missing", python=python, server=server); return False
        # The HTTP port is not opened until model preload finishes.  Coordinate
        # across HomeAgent processes so that only one process loads the model.
        lock_path = ROOT / "Vision" / "state" / "startup.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = float(cfg.get("startup_timeout_seconds", 120))
        token = uuid.uuid4().hex
        payload = {"owner_pid": os.getpid(), "token": token, "created_at": time.time()}
        owns_lock = False
        while not owns_lock:
            # Another process may have finished preload between lock checks.
            if ready(): return True
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                owns_lock = True
            except FileExistsError:
                try:
                    existing = json.loads(lock_path.read_text(encoding="utf-8"))
                    owner_pid = int(existing.get("owner_pid", 0))
                    created_at = float(existing.get("created_at", 0))
                    os.kill(owner_pid, 0)
                    stale = time.time() - created_at > timeout + 30
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    stale = True
                if stale:
                    try: lock_path.unlink()
                    except FileNotFoundError: pass
                    continue
                if not wait_until_ready: return True
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if ready(): return True
                    if not lock_path.exists(): break
                    time.sleep(1)
                if ready(): return True
                continue
        log_dir = ROOT / "Vision" / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "vision-mcp.log").open("a", encoding="utf-8")
        env = os.environ.copy(); env.update({
            "VISION_MCP_TRANSPORT": "http", "VISION_MCP_HOST": host, "VISION_MCP_PORT": str(port),
            "VISION_PRELOAD_MODEL": "1" if cfg.get("preload_model", True) else "0",
            "GUI_ACTOR_MODEL": str(ROOT / "Vision" / "models" / "GUI-Actor-2B-Qwen2-VL"),
            "GUI_ACTOR_REPO": str(ROOT / "Vision" / "GUI-Actor"),
            "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
            "BROWSER_CDP_ENDPOINTS": ",".join(str(item) for item in cfg.get(
                "existing_browser_cdp_endpoints",
                ["http://127.0.0.1:9222", "http://127.0.0.1:9223", "http://127.0.0.1:9333"],
            )),
        })
        try:
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
            self.vision_service_process = subprocess.Popen([str(python), str(server)], cwd=str(server.parent), env=env, stdout=log, stderr=log, creationflags=flags)
            payload["server_pid"] = self.vision_service_process.pid
            lock_path.write_text(json.dumps(payload), encoding="utf-8")
            self.log_event("vision_service_started", pid=self.vision_service_process.pid, host=host, port=port, preload=cfg.get("preload_model", True))
            if not wait_until_ready:
                # Keep the cross-process lock for the whole preload window even
                # though the caller requested a non-blocking autostart.
                def release_when_started() -> None:
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline and not ready():
                        if self.vision_service_process.poll() is not None: break
                        time.sleep(1)
                    try:
                        current = json.loads(lock_path.read_text(encoding="utf-8"))
                        if current.get("token") == token: lock_path.unlink()
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        pass
                threading.Thread(target=release_when_started, daemon=True, name="vision-startup-lock").start()
                owns_lock = False
                return True
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if ready(): return True
                if self.vision_service_process.poll() is not None: return False
                time.sleep(1)
            return False
        finally:
            if owns_lock:
                try:
                    current = json.loads(lock_path.read_text(encoding="utf-8"))
                    if current.get("token") == token: lock_path.unlink()
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass

    async def codex_status(self) -> dict[str, Any]:
        """检查 CLI 版本和已配置 MCP 服务，不执行 Agent 任务。"""
        codex_command = self._codex_command()
        env = self._codex_environment(codex_command)
        compatibility = self._repair_codex_models_cache(Path(env["CODEX_HOME"]))
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        version_proc = await asyncio.create_subprocess_exec(
            *codex_command, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags, env=env,
        )
        version_out, version_err = await version_proc.communicate()
        mcp_proc = await asyncio.create_subprocess_exec(
            *codex_command, "mcp", "list", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags, env=env,
        )
        mcp_out, mcp_err = await mcp_proc.communicate()
        return {
            "ok": version_proc.returncode == 0 and mcp_proc.returncode == 0,
            "executable": " ".join(codex_command),
            "version": (version_out or version_err).decode("utf-8", "replace").strip(),
            "mcp_servers": (mcp_out or mcp_err).decode("utf-8", "replace").strip(),
            "models_cache_compatibility": compatibility,
            "local_tools_preferred": bool(self.config.get("agent", {}).get("prefer_local_tools", True)),
        }

    async def _vision_mcp_call(self, tool_name: str, arguments: dict[str, Any] | None = None):
        """Call persistent vision MCP through the project venv dependency bridge."""
        url = str(self.config.get("vision_mcp", {}).get("url", "http://127.0.0.1:8765/mcp"))
        python = ROOT / ".venv" / "Scripts" / "python.exe"
        helper = ROOT / "Vision" / "mcp_call.py"
        client_env = os.environ.copy()
        client_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        proc = await asyncio.create_subprocess_exec(str(python), str(helper), url, tool_name,
            json.dumps(arguments or {}, ensure_ascii=False), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env=client_env)
        with self.active_process_lock: self.active_process = proc
        try:
            out, err = await proc.communicate()
        except asyncio.CancelledError:
            try: proc.kill()
            except ProcessLookupError: pass
            await proc.communicate()
            raise
        finally:
            with self.active_process_lock:
                if self.active_process is proc: self.active_process = None
        lines = out.decode("utf-8", "replace").strip().splitlines()
        try: payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError: payload = {}
        if proc.returncode or not payload.get("ok"):
            raise RuntimeError(payload.get("error") or err.decode("utf-8", "replace")[-600:] or f"视觉工具 {tool_name} 执行失败")
        return str(payload.get("text", ""))

    @staticmethod
    def _is_direct_visual_media_request(text: str) -> bool:
        lowered = text.lower()
        target = any(x in lowered for x in ("bilibili", "哔哩哔哩", "b站", "网易云", "cloudmusic"))
        return target and any(x in text for x in ("找", "搜", "播放", "视频", "听"))

    async def _run_direct_visual_media(self, text: str, status=None, task_plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Fast deterministic path for common Bilibili/CloudMusic search-and-play tasks."""
        lowered = text.lower(); is_bili = any(x in lowered for x in ("bilibili", "哔哩哔哩", "b站"))
        is_cloud = any(x in lowered for x in ("网易云", "cloudmusic"))
        if not self._is_direct_visual_media_request(text):
            return None
        if not await asyncio.to_thread(self.ensure_vision_service, True):
            return {"error": "视觉服务未就绪"}
        query = str((task_plan or {}).get("query") or "").strip()
        if not query and is_cloud:
            operation = str((task_plan or {}).get("operation") or "")
            if operation not in {"open", "play", "control"}:
                return {"error": "任务规划器没有给出明确的搜索对象，已停止执行以避免误操作"}
        if not query and not is_cloud: return None
        self.log_event("direct_vision_started", target="bilibili" if is_bili else "cloudmusic", query=query)
        try:
            if is_bili:
                if status: status("正在打开 Bilibili 搜索结果…")
                await asyncio.to_thread(webbrowser.open, f"https://search.bilibili.com/all?keyword={quote(query)}")
                await asyncio.sleep(3)
            if status: status("正在检测目标窗口…")
            raw = await asyncio.wait_for(self._vision_mcp_call("list_windows", {}), timeout=15)
            try: windows = ast.literal_eval(raw)
            except (ValueError, SyntaxError): windows = []
            if is_cloud:
                window = next((item for item in windows if str(item.get("process_name", "")).lower() == "cloudmusic.exe"), None)
                if window is None:
                    window = next((item for item in windows if any(key in str(item.get("title", "")).lower() for key in ("网易云", "cloudmusic"))), None)
            else:
                preferred = ("哔哩", "bilibili", "chrome", "edge")
                window = next((item for key in preferred for item in windows if key.lower() in str(item.get("title", "")).lower()), None)
            if not window: return {"error": "没有检测到目标软件窗口"}
            title = str(window["title"])
            if is_cloud and query:
                if status: status("正在识别搜索框并输入…")
                await asyncio.wait_for(self._vision_mcp_call("window_type_text", {"title_contains": title, "instruction": "网易云音乐顶部的搜索框", "text": query}), timeout=90)
                await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]}); await asyncio.sleep(3)
            if status: status("正在识别并播放第一项…" if query else "正在操作网易云音乐播放控制…")
            instruction = "Bilibili搜索结果中的第一个视频封面" if is_bili else ("搜索结果中第一首歌曲对应的播放按钮" if query else "网易云音乐窗口底部播放控制栏中央的播放按钮")
            await asyncio.wait_for(self._vision_mcp_call("window_click", {"title_contains": title, "instruction": instruction}), timeout=90)
            self.log_event("direct_vision_completed", target="bilibili" if is_bili else "cloudmusic", query=query, window=title)
            target_text = query if query else "当前音乐"
            return {"ok": True, "answer": f"找到啦，我已经在{'Bilibili' if is_bili else '网易云音乐'}里帮你播放 {target_text} 了。"}
        except asyncio.TimeoutError:
            self.log_event("direct_vision_timeout", query=query)
            return {"error": "视觉识别步骤超时"}
        except Exception as exc:
            self.log_event("direct_vision_failed", query=query, error=str(exc))
            return {"error": str(exc)}

    async def _run_cloudmusic_search_and_play(self, query: str, status=None) -> dict[str, Any]:
        """Search and select a CloudMusic result with bounded, evidence-driven retries."""
        query = str(query or "").strip()
        if not query: return {"error": "没有明确的歌曲名", "failure_stage": "task_plan"}
        if not await asyncio.to_thread(self.ensure_vision_service, True):
            return {"error": "视觉服务未就绪", "failure_stage": "vision_start"}
        retry_rounds = max(2, min(8, int(self.config.get("agent", {}).get("operation_retry_rounds", 4))))
        failures: list[dict[str, Any]] = []

        def parse(raw: str, default: Any):
            try: return ast.literal_eval(raw)
            except (ValueError, SyntaxError): return default

        async def cloud_window() -> dict[str, Any] | None:
            rows = parse(await self._vision_mcp_call("list_windows", {}), [])
            return next((item for item in rows if str(item.get("process_name", "")).lower() == "cloudmusic.exe"), None)

        window = await cloud_window()
        if not window:
            executable = Path(r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe")
            configured = self.config.get("computer_control", {}).get("applications", {})
            configured_path = configured.get("网易云音乐") or configured.get("cloudmusic")
            if configured_path: executable = Path(str(configured_path))
            if not executable.is_file():
                return {"error": f"没有检测到网易云音乐窗口，且程序路径不存在：{executable}", "failure_stage": "launch"}
            if status: status("没有检测到网易云窗口，正在启动本地网易云音乐…")
            subprocess.Popen([str(executable)], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            for _ in range(15):
                await asyncio.sleep(1); window = await cloud_window()
                if window: break
        if not window:
            return {"error": "网易云音乐已启动，但15秒内没有出现可操作窗口", "failure_stage": "window_wait"}

        title = str(window.get("title", ""))
        if query.casefold() in title.casefold():
            return {"ok": True, "answer": f"网易云音乐当前已经选中并播放《{query}》。", "query": query, "title": title, "attempts": 0, "used_local_tools": True, "already_playing": True}
        if status: status(f"正在网易云音乐中搜索《{query}》…")
        try:
            await self._vision_mcp_call("activate_window", {"title_contains": title})
            await self._vision_mcp_call("window_type_text", {"title_contains": title, "instruction": "网易云音乐窗口顶部的搜索框", "text": query})
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]})
            await asyncio.sleep(3)
        except Exception as exc:
            return {"error": f"搜索提交失败：{exc}", "failure_stage": "search_submit"}

        instructions = [
            f'搜索结果“单曲”列表中歌曲名为“{query}”的第一条结果的歌曲名称文字；不要点歌手名、页面标题或底部播放栏',
            f'搜索结果列表中“{query}”这一行左侧的歌曲标题文字；不要点右侧歌手名',
            f'单曲结果中同时匹配歌曲“{query}”和其歌手的那一整行中央空白区域；不要点底部全局播放按钮',
            f'搜索结果中的第一首“{query}”歌曲标题；不是下方相似歌曲或歌单',
        ]
        for attempt in range(1, retry_rounds + 1):
            if self.cancel_event.is_set(): raise asyncio.CancelledError
            current = await cloud_window()
            if not current:
                failures.append({"attempt": attempt, "stage": "window", "reason": "网易云窗口消失"}); break
            title = str(current.get("title", title))
            instruction = instructions[(attempt - 1) % len(instructions)]
            candidate_index = (attempt - 1) % 3
            if status: status(f"正在尝试播放《{query}》（第 {attempt}/{retry_rounds} 次：精确选择结果）…")
            try:
                clicked = parse(await self._vision_mcp_call("window_double_click", {
                    "title_contains": title, "instruction": instruction, "topk": 3, "idx": candidate_index,
                }), {})
                await asyncio.sleep(2)
                current = await cloud_window(); after_title = str((current or {}).get("title", ""))
                if query.casefold() in after_title.casefold():
                    result = {"ok": True, "answer": f"已经在网易云音乐中搜索并播放《{query}》。", "query": query, "title": after_title, "attempts": attempt, "strategy": "double_click_result", "candidate_index": candidate_index, "used_local_tools": True, "failures": failures}
                    self.log_event("cloudmusic_search_play_completed", result=result); return result
                reason = f"双击后窗口标题仍为“{after_title or title}”"
                failures.append({"attempt": attempt, "stage": "double_click", "reason": reason, "pixel": clicked.get("pixel"), "candidate_index": candidate_index})
                if status: status(f"第 {attempt} 次未播放成功：{reason}；正在尝试对已选中结果按 Enter…")
                await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]}); await asyncio.sleep(2)
                current = await cloud_window(); after_enter = str((current or {}).get("title", ""))
                if query.casefold() in after_enter.casefold():
                    result = {"ok": True, "answer": f"已经在网易云音乐中搜索并播放《{query}》。", "query": query, "title": after_enter, "attempts": attempt, "strategy": "select_then_enter", "candidate_index": candidate_index, "used_local_tools": True, "failures": failures}
                    self.log_event("cloudmusic_search_play_completed", result=result); return result
                failures.append({"attempt": attempt, "stage": "enter_selected", "reason": f"按 Enter 后仍为“{after_enter or title}”"})
            except Exception as exc:
                failures.append({"attempt": attempt, "stage": "result_select", "reason": str(exc)})
            if attempt < retry_rounds:
                reason = failures[-1]["reason"]
                if status: status(f"第 {attempt} 次失败：{reason}；正在重新搜索并更换识别锚点…")
                self.log_event("cloudmusic_search_play_retry", query=query, attempt=attempt, failure=failures[-1])
                current = await cloud_window(); title = str((current or {}).get("title", title))
                try:
                    await self._vision_mcp_call("window_type_text", {"title_contains": title, "instruction": "网易云音乐窗口顶部的搜索框", "text": query})
                    await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]}); await asyncio.sleep(3)
                except Exception as exc:
                    failures.append({"attempt": attempt, "stage": "research", "reason": str(exc)})
        reason_summary = "；".join(f"第{x['attempt']}次/{x['stage']}：{x['reason']}" for x in failures[-6:])
        self.log_event("cloudmusic_search_play_failed", query=query, attempts=retry_rounds, failures=failures)
        return {"error": f"尝试 {retry_rounds} 轮后仍未能确认《{query}》开始播放。{reason_summary}", "failure_stage": "play_verification", "query": query, "attempts": retry_rounds, "failures": failures, "next_action": "保留当前搜索结果，不点击底部全局播放按钮；可根据失败记录继续更换结果锚点"}

    async def _run_direct_web_media(self, text: str, status=None, task_plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Operate Bilibili through DOM/text only; never loads the GUI vision model."""
        lowered = text.lower(); plan = task_plan or self._analyze_task(text)
        implicit_favorite = plan.get("handler") == "bilibili_favorites"
        if not any(x in lowered for x in ("bilibili", "哔哩哔哩", "b站")) and not implicit_favorite: return None
        favorite_mode = plan.get("handler") == "bilibili_favorites"
        query = str(plan.get("query") or "").strip()
        if not query and not favorite_mode: return {"error": "没有识别出要搜索的内容"}
        if favorite_mode:
            ordinal = re.search(r"第\s*(\d+|[一二两三四五六七八九十])\s*个", text)
            number_map = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            index = int(ordinal.group(1)) if ordinal and ordinal.group(1).isdigit() else number_map.get(ordinal.group(1), 1) if ordinal else 1
            return await self._run_existing_browser_favorites(index, status, str(plan.get("favorite_folder") or "默认收藏夹"))
        if not await asyncio.to_thread(self.ensure_vision_service, True): return {"error": "网页工具服务未就绪"}
        self.log_event("direct_web_started", target="bilibili", query=query)
        try:
            if status: status("网页 Agent 正在分步搜索、选择并验证…")
            python = ROOT / ".venv" / "Scripts" / "python.exe"
            script = ROOT / "Skill" / "web-agent-operator" / "scripts" / "web_agent.py"
            timeout = max(45 if favorite_mode else 5, int(self.config.get("vision_mcp", {}).get("direct_operation_timeout_seconds", 20)))
            action = "play" if any(word in text for word in ("播放", "听", "播一下")) else "open"
            number_map = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            ordinal = re.search(r"第\s*(\d+|[一二两三四五六七八九十])\s*个", text)
            index = int(ordinal.group(1)) if ordinal and ordinal.group(1).isdigit() else number_map.get(ordinal.group(1), 1) if ordinal else 1
            proc = await asyncio.create_subprocess_exec(str(python), str(script), "--site", "bilibili", "--query", query,
                "--mode", "favorites" if favorite_mode else "search", "--index", str(index), "--action", action, "--timeout", str(timeout), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            with self.active_process_lock: self.active_process = proc
            try: out, err = await proc.communicate()
            finally:
                with self.active_process_lock:
                    if self.active_process is proc: self.active_process = None
            events = []
            for line in out.decode("utf-8", "replace").splitlines():
                try: events.append(json.loads(line))
                except json.JSONDecodeError: continue
            for event in events:
                self.log_event("web_agent_step", step_event=event.get("event"), **{key: value for key, value in event.items() if key != "event"})
            completed = next((event for event in reversed(events) if event.get("event") == "completed"), None)
            failed = next((event for event in reversed(events) if event.get("event") == "failed"), None)
            if proc.returncode or not completed: return {"error": (failed or {}).get("error") or err.decode("utf-8", "replace")[-500:] or "网页 Agent 未验证完成状态"}
            self.log_event("direct_web_completed", query=query, title=completed.get("title"), url=completed.get("url"))
            verb = "打开并播放" if action == "play" else "找到并打开"
            return {"ok": True, "answer": f"找到啦，我已经在B站{verb} {completed.get('title') or query} 了。"}
        except Exception as exc:
            self.log_event("direct_web_failed", query=query, error=str(exc))
            return {"error": str(exc)}

    async def _run_existing_browser_favorites(self, index: int, status=None, folder_name: str = "默认收藏夹") -> dict[str, Any]:
        """Use an already-open normal browser profile; never launch Playwright Chromium."""
        if not await asyncio.to_thread(self.ensure_vision_service, True): return {"error": "视觉服务未就绪"}
        allowed = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe"}

        async def windows():
            raw = await self._vision_mcp_call("list_windows", {})
            try: values = ast.literal_eval(raw)
            except (ValueError, SyntaxError): values = []
            return [item for item in values if str(item.get("process_name", "")).lower() in allowed and "ms-playwright" not in str(item.get("process_path", "")).lower()]

        candidates = await windows()
        if not candidates:
            return {"error": "没有检测到现有浏览器窗口；为保护登录会话，未启动任何新浏览器"}
        browser = next((item for item in candidates if any(word in str(item.get("title", "")).lower() for word in ("bilibili", "哔哩哔哩"))), candidates[0])
        pid = int(browser.get("pid", 0)); title = str(browser.get("title", ""))

        async def current_title():
            current = await windows()
            match = next((item for item in current if int(item.get("pid", 0)) == pid), None)
            return str((match or browser).get("title", title))

        async def current_address():
            active_title = await current_title()
            await self._vision_mcp_call("activate_window", {"title_contains": active_title})
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["ctrl", "l"]})
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["ctrl", "c"]})
            copied = ast.literal_eval(await self._vision_mcp_call("desktop_read_clipboard", {}))
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["esc"]})
            return str(copied.get("text", "")).strip()

        if status: status("正在读取现有浏览器中的 Bilibili 登录会话…")
        await self._vision_mcp_call("activate_window", {"title_contains": title})
        profile_path = HOME_AGENT / "state" / "browser-profiles.json"
        try: profile = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
        except (OSError, json.JSONDecodeError): profile = {}
        stored_mid = str(profile.get("bilibili_mid", "")).strip()
        address = await current_address(); uid_match = re.search(r"space\.bilibili\.com/(\d+)", address)
        if not uid_match and stored_mid.isdigit(): uid_match = re.match(r"(\d+)", stored_mid)
        if not uid_match:
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["ctrl", "l"]})
            await self._vision_mcp_call("desktop_type_active_text", {"text": "https://www.bilibili.com/", "clear": True})
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]}); await asyncio.sleep(4)
            title = await current_title()
            if status: status("正在从已登录首页进入个人空间…")
            avatar = ast.literal_eval(await asyncio.wait_for(self._vision_mcp_call("window_click", {"title_contains": title, "instruction": "Bilibili页面顶部已登录用户的头像或个人中心入口"}), timeout=90))
            if not avatar.get("clicked"): return {"error": "无法从现有浏览器读取当前 Bilibili 用户空间"}
            await asyncio.sleep(3); address = await current_address(); uid_match = re.search(r"space\.bilibili\.com/(\d+)", address)
        if not uid_match: return {"error": "现有浏览器虽然打开了 Bilibili，但无法确认当前账户的用户空间地址"}
        mid = uid_match.group(1)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile.update({"bilibili_mid": int(mid), "updated_at": datetime.now().isoformat(timespec="seconds")})
        temporary = profile_path.with_suffix(".json.tmp"); temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"); temporary.replace(profile_path)
        if status: status(f"正在读取“{folder_name}”的准确数据顺序…")
        headers = {"User-Agent": "Mozilla/5.0", "Referer": f"https://space.bilibili.com/{mid}/favlist"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), headers=headers) as session:
                async with session.get(f"https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid={mid}") as response:
                    folders = await response.json(content_type=None)
                folder_list = ((folders.get("data") or {}).get("list") or []) if folders.get("code") == 0 else []
                requested = self._normalize_favorite_folder_name(folder_name)
                default_folder, ranked_folders = self._resolve_favorite_folder(requested, folder_list)
                if not default_folder:
                    available = "、".join(str(item.get("title", "")).strip() for item in folder_list[:12] if str(item.get("title", "")).strip()) or "无"
                    ranked = "、".join(f"{row['title']}({row['score']:.2f})" for row in ranked_folders[:3])
                    raise RuntimeError(f"无法唯一匹配收藏夹“{requested}”；当前可用收藏夹：{available}；最接近：{ranked or '无'}")
                folder_name = str(default_folder.get("title") or requested).strip()
                page_number = (index - 1) // 20 + 1; page_index = (index - 1) % 20
                media_id = int(default_folder["id"])
                resource_url = f"https://api.bilibili.com/x/v3/fav/resource/list?media_id={media_id}&pn={page_number}&ps=20&keyword=&order=mtime&type=0&tid=0&platform=web"
                async with session.get(resource_url) as response:
                    resources = await response.json(content_type=None)
                medias = ((resources.get("data") or {}).get("medias") or []) if resources.get("code") == 0 else []
                if page_index >= len(medias): raise RuntimeError(f"“{folder_name}”没有第 {index} 个视频")
                media = medias[page_index]; bvid = str(media.get("bvid", "")).strip(); video_title = str(media.get("title", "")).strip()
                if not re.fullmatch(r"BV[0-9A-Za-z]+", bvid): raise RuntimeError("收藏条目缺少有效 BV 号")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError) as exc:
            return {"error": f"读取默认收藏夹数据失败：{exc}"}
        video_url = f"https://www.bilibili.com/video/{bvid}/?spm_id_from=333.1387.favlist.content.click"
        existing_title = await current_title(); existing_address = await current_address()
        if bvid.lower() in existing_address.lower() and video_title[:10].lower() in existing_title.lower():
            result = {"ok": True, "answer": f"“{folder_name}”第 {index} 个视频已经在当前浏览器中打开：{video_title}。", "browser_pid": pid, "browser_process": browser.get("process_name"), "title": existing_title, "url": existing_address, "bvid": bvid, "favorite_folder": folder_name, "favorite_index": index, "order": "mtime", "used_existing_browser": True, "already_open": True}
            self.log_event("existing_browser_favorite_completed", result=result); return result
        if status: status(f"正在现有浏览器中打开“{folder_name}”第 {index} 个视频…")
        previous_title = await current_title()
        await self._vision_mcp_call("desktop_hotkey", {"keys": ["ctrl", "l"]})
        await self._vision_mcp_call("desktop_type_active_text", {"text": video_url, "clear": True})
        await asyncio.sleep(0.4); await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]})

        async def verify_loaded(seconds: int):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                active_title = await current_title()
                title_changed = active_title != previous_title
                title_matches = video_title[:10].lower() in active_title.lower() if len(video_title) >= 4 else title_changed
                if title_changed and title_matches:
                    active_address = await current_address()
                    if bvid.lower() in active_address.lower(): return active_title, active_address
                await asyncio.sleep(1)
            return "", ""

        final_title, final_address = await verify_loaded(12)
        if not final_address:
            # Some browsers leave a pasted URL pending in the address bar. Re-focus
            # and commit once more before using a new tab in the same normal browser.
            self.log_event("existing_browser_navigation_retry", bvid=bvid, strategy="commit_address_again")
            await self._vision_mcp_call("activate_window", {"title_contains": await current_title()})
            await self._vision_mcp_call("desktop_hotkey", {"keys": ["ctrl", "l"]})
            await self._vision_mcp_call("desktop_type_active_text", {"text": video_url, "clear": True})
            await asyncio.sleep(0.5); await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]})
            final_title, final_address = await verify_loaded(12)
        if not final_address:
            process_path = str(browser.get("process_path", ""))
            if not process_path or not Path(process_path).is_file(): return {"error": f"地址栏导航未生效，且无法找到现有浏览器程序：{process_path}"}
            self.log_event("existing_browser_navigation_retry", bvid=bvid, strategy="existing_browser_new_tab", process=process_path)
            switch = "-new-tab" if str(browser.get("process_name")) == "firefox.exe" else "--new-tab"
            subprocess.Popen([process_path, switch, video_url], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            final_title, final_address = await verify_loaded(15)
        if not final_address:
            observed_title = await current_title(); observed_address = await current_address()
            return {"error": f"浏览器没有真正切换到“{folder_name}”第 {index} 个视频。目标 {bvid}；当前标题：{observed_title}；当前地址：{observed_address}"}
        result = {"ok": True, "answer": f"已经用你现有的浏览器账户打开“{folder_name}”第 {index} 个视频：{video_title}。", "browser_pid": pid, "browser_process": browser.get("process_name"), "title": final_title, "url": final_address, "bvid": bvid, "favorite_folder": folder_name, "favorite_index": index, "order": "mtime", "used_existing_browser": True}
        self.log_event("existing_browser_favorite_completed", result=result)
        return result

    async def _natural_visual_failure(self, request: str, reason: str) -> str:
        """Generate only failure wording through the character LLM; keep it short for TTS."""
        fallback = "这次没能顺利操作成功，我已经停下来了。你可以稍后再让我试一次。"
        try:
            provider, key = self._provider(); llm_cfg = self.project["llm"]
            url = provider["base_url"].rstrip("/") + "/chat/completions"
            payload = {"model": provider["model"], "temperature": 0.55, "messages": [
                {"role": "system", "content": self._system_prompt() + "\n请用符合角色性格的自然口语说明一次电脑操作失败。只说一到两句，不要责怪用户，不要虚构成功。"},
                {"role": "user", "content": f"用户请求：{request}\n失败原因：{reason}"},
            ]}
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self._set_token_limit(payload, provider, 100)
                async with session.post(url, json=payload, headers=self._provider_headers(provider, key)) as response:
                    raw = await response.text()
                    if response.status >= 400: return fallback
                    data = json.loads(raw); answer = str(data["choices"][0]["message"]["content"]).strip()
                    return answer or fallback
        except Exception as exc:
            self.log_event("visual_failure_wording_failed", error=str(exc))
            return fallback

    def _request_live_context_clear(self) -> dict[str, Any]:
        state_dir = ROOT / "state"; state_dir.mkdir(parents=True, exist_ok=True)
        context_path = state_dir / "live-context.json"
        removed = 0
        if context_path.exists():
            try:
                rows = json.loads(context_path.read_text(encoding="utf-8")); removed = len(rows) if isinstance(rows, list) else 0
            except (OSError, json.JSONDecodeError): pass
        temporary_context = context_path.with_suffix(".tmp")
        temporary_context.write_text("[]\n", encoding="utf-8"); temporary_context.replace(context_path)
        path = state_dir / "live-context-control.json"
        token = f"{time.time_ns()}"
        payload = {"action": "clear", "token": token, "requested_at": datetime.now().isoformat(timespec="seconds"), "source": "home-agent", "status": "storage_cleared", "removed_messages": removed}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"); temporary.replace(path)
        self.log_event("live_context_clear_requested", token=token)
        return payload

    async def _run_codex_task(self, task: str, require_mcp: bool = False, status=None, preferred_mcp: str = "", task_plan: dict[str, Any] | None = None, previous_failure: str = "", code_retry_round: int = 0) -> dict[str, Any]:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError
        cfg = self._codex_config()
        if not cfg.get("enabled", False):
            return {"error": "Codex CLI 功能未启用"}
        if preferred_mcp:
            if not await asyncio.to_thread(self.ensure_vision_service, True):
                return {"error": "视觉 MCP 常驻服务启动超时，请检查 Vision/logs/vision-mcp.log"}
        codex_command = self._codex_command()
        working_directory = project_path(str(cfg.get("working_directory", ".")))
        if not working_directory.is_dir():
            return {"error": f"Codex 工作目录不存在：{working_directory}"}
        gui_enabled = bool(self.config.get("vision_mcp", {}).get("gui_enabled", False))
        plan = task_plan or self._analyze_task(task)
        self_code_task = self.self_upgrade.is_upgrade_request(task)
        code_task = self.self_upgrade.code_editor.is_code_task(task)
        self_code_contract = ""
        if code_task:
            if status: status("正在准备代码工程和自动测试约束…")
            self_code_contract, loaded_documents = self.self_upgrade.code_editor.build_execution_contract(
                self_edit=self_code_task, include_document_contents=False,
            )
            self.log_event("code_task_prepared", self_edit=self_code_task, documents=loaded_documents)
        prompt = (
            "你是家庭 AI 助手的执行代理。请使用可用的 CLI、文件和 MCP 工具完成任务，"
            "最后用简洁中文给出适合语音朗读的结果。不要输出密钥。\n"
            "执行规则：先观察再操作；逐项完成清单；每次操作后读取新状态；失败时诊断原因并切换 DOM、视觉、直接网址或其他可用路径；"
            "只有成功条件有可观察证据时才能报告成功。不得把‘已打开首页’当作多步骤任务完成。\n\n"
            f"结构化任务合同：\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            + ("浏览器策略是 existing_profile_only：必须先用 list_windows 查找普通 Edge/Chrome/Firefox/Brave，"
               "只通过 activate_window、window_screenshot、window_click、desktop_hotkey、desktop_type_active_text 操作现有登录会话。"
               "禁止调用 navigate 或任何会启动 Playwright/内部 Chromium 的网页工具。若没有现有浏览器，只能使用系统默认浏览器，不得强制 Chromium。\n\n"
               if plan.get("browser_policy") == "existing_profile_only" else "")
            + (f"上一条确定性路径失败：{previous_failure}\n必须避免重复同一失败动作，改用不同策略继续。\n\n" if previous_failure else "")
            + ("本任务必须优先使用合适的 MCP 工具；若没有可用 MCP，请明确说明。\n\n" if require_mcp else "")
            + (f"本任务必须首先使用 `{preferred_mcp}` MCP。"
               + ("这是网页任务且图像 GUI 已关闭：必须遵循项目内 Skill/web-agent-operator/SKILL.md，"
                  "网页操作前先调用 inspect_active_target。若 mode=browser_dom，优先读取当前网页的 web_read DOM/HTML，"
                  "并只用 get_url/web_read/web_fill/web_click_text/web_press/web_play_media 操作现有页面；"
                  "若 mode 不是 browser_dom，因为图像 GUI 已关闭，应明确返回当前页面 DOM 不可用，不能调用视觉工具。"
                  "不要为了读取 DOM 强制新开浏览器。"
                  "打开首页只是阶段一，绝不是完成；必须继续搜索、读取结果、选择匹配项、执行目标动作，并读取最终页面验证。"
                  "只有终态证据满足用户目标才能报告成功。\n\n" if not gui_enabled else
                  "先拆解用户请求中的全部动作并建立完成清单。先调用 inspect_active_target 判定目标；"
                  "browser_dom 优先使用当前页 HTML/DOM，browser_visual 使用浏览器窗口视觉，desktop_visual 使用桌面视觉；"
                  "需要图像定位时，视觉点击输入框后用 type_active_text 输入，禁止为同一输入框重复定位。"
                  "原生应用先用 list_windows 检测目标窗口，再用 window_screenshot/window_click/window_type_text。"
                  "每次点击、输入、搜索、选择后必须重新读取或截图验证；清单中任何动作未完成时不得结束或报告成功。"
                  "连续两次状态不变才报告具体失败。\n\n")
               if preferred_mcp else "")
            + self_code_contract
            + f"角色与规则：\n{self.workspace.prompt_documents('home')}\n\n用户任务：{task}"
        )
        # Large self-upgrade contracts exceed Windows' command-line limit when
        # passed as an argument. A lone '-' makes Codex read the prompt on stdin.
        command = self._codex_exec_command(codex_command, cfg)
        if status:
            status("Codex CLI 正在执行…")
        self.log_event("codex_task_started", task=task, require_mcp=require_mcp, working_directory=working_directory, task_plan=plan, previous_failure=previous_failure)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = self._codex_environment(codex_command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, cwd=str(working_directory), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE,
                creationflags=creationflags, env=env,
            )
        except OSError as exc:
            self.log_event("codex_process_start_failed", error=str(exc), prompt_chars=len(prompt))
            return {"error": f"Codex CLI 启动失败：{exc}", "prompt_chars": len(prompt)}
        with self.active_process_lock:
            self.active_process = proc
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            try: proc.kill()
            except ProcessLookupError: pass
            await proc.wait()
            with self.active_process_lock:
                if self.active_process is proc: self.active_process = None
            self.log_event("codex_prompt_delivery_failed", error=str(exc), prompt_chars=len(prompt))
            return {"error": f"Codex CLI 未能接收任务内容：{exc}", "prompt_chars": len(prompt)}
        if self.cancel_event.is_set():
            self.stop_current_task()
            raise asyncio.CancelledError

        log_dir = HOME_AGENT / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "codex-last.jsonl"
        stderr_path = log_dir / "codex-last.stderr.log"
        events: list[dict[str, Any]] = []
        stderr_lines: list[str] = []
        answer = ""
        mcp_calls: list[str] = []
        codex_errors: list[str] = []
        turn_completed = False
        last_progress = ""

        async def read_stdout() -> None:
            nonlocal answer, turn_completed, last_progress
            assert proc.stdout is not None
            with stdout_path.open("w", encoding="utf-8", newline="\n") as output:
                while True:
                    raw_line = await proc.stdout.readline()
                    if not raw_line: break
                    line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                    output.write(line + "\n"); output.flush()
                    try: event = json.loads(line)
                    except json.JSONDecodeError: continue
                    events.append(event)
                    if event.get("type") == "thread.started":
                        self.codex_thread_id = event.get("thread_id")
                    if event.get("type") == "turn.completed":
                        turn_completed = True
                    if event.get("type") == "error":
                        codex_errors.append(str(event.get("message", "Codex error")))
                    if event.get("type") == "turn.failed":
                        failure = event.get("error") if isinstance(event.get("error"), dict) else {}
                        codex_errors.append(str(failure.get("message") or event.get("error") or "Codex turn failed"))
                    item = event.get("item") if isinstance(event.get("item"), dict) else {}
                    item_type = str(item.get("type", "")).lower()
                    if event.get("type") == "item.completed" and item_type == "agent_message":
                        answer = str(item.get("text", "")).strip()
                    if event.get("type") == "item.completed" and item_type == "error":
                        codex_errors.append(str(item.get("message", "Codex item error")))
                    if "mcp" in item_type:
                        server = str(item.get("server") or item.get("server_name") or "").strip()
                        tool = str(item.get("name") or item.get("tool") or item.get("tool_name") or item_type).strip()
                        call_name = ".".join(value for value in (server, tool) if value)
                        if call_name and call_name not in mcp_calls: mcp_calls.append(call_name)
                    progress = self._codex_progress_text(event)
                    if progress and progress != last_progress:
                        last_progress = progress
                        if status: status(progress)
                        self.log_event("codex_task_progress", progress=progress, event_type=event.get("type"), item_type=item_type)

        async def read_stderr() -> None:
            assert proc.stderr is not None
            with stderr_path.open("w", encoding="utf-8", newline="\n") as output:
                while True:
                    raw_line = await proc.stderr.readline()
                    if not raw_line: break
                    line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
                    stderr_lines.append(line)
                    output.write(line + "\n"); output.flush()

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        try:
            task_timeout = self._codex_task_timeout(preferred_mcp)
            await asyncio.wait_for(asyncio.gather(proc.wait(), stdout_task, stderr_task), timeout=task_timeout)
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            for reader in (stdout_task, stderr_task):
                if not reader.done(): reader.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            with self.active_process_lock:
                if self.active_process is proc:
                    self.active_process = None
            self.log_event("codex_task_cancelled")
            raise
        except asyncio.TimeoutError:
            proc.kill(); await proc.wait()
            for reader in (stdout_task, stderr_task):
                if not reader.done(): reader.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            detail = "\n".join(stderr_lines).strip()[-1200:]
            self.log_event("codex_task_timeout", detail=detail)
            return {"error": "Codex CLI 执行超时。请检查网络、登录状态和 MCP 服务。" + (f"\n{detail}" if detail else "")}
        finally:
            with self.active_process_lock:
                if self.active_process is proc:
                    self.active_process = None
            # The desktop app may refresh the shared cache with a newer schema
            # while this older standalone CLI is running. Restore compatibility
            # for the next invocation without deleting any model entries.
            self._repair_codex_models_cache()
        stderr = "\n".join(stderr_lines)
        if proc.returncode != 0:
            event_detail = "\n".join(codex_errors)
            error = (event_detail or stderr or "Codex CLI 未返回错误详情")[-2000:]
            self.log_event("codex_task_failed", exit_code=proc.returncode, error=error)
            return {"error": error, "exit_code": proc.returncode, "mcp_calls": mcp_calls}
        if not turn_completed or not answer:
            detail = "；".join(codex_errors[-3:]) or "没有收到完整的 turn.completed 与 agent_message"
            self.log_event("codex_task_incomplete", detail=detail, event_count=len(events), mcp_calls=mcp_calls)
            return {"error": f"Codex CLI 未返回完整结果：{detail}", "mcp_calls": mcp_calls, "event_count": len(events)}
        if require_mcp and not mcp_calls:
            self.log_event("codex_required_mcp_missing", preferred_mcp=preferred_mcp, answer=answer, codex_errors=codex_errors)
            return {"error": f"Codex 没有实际调用所要求的 MCP{f'（{preferred_mcp}）' if preferred_mcp else ''}，已拒绝把文字回答判定为成功。", "answer": answer, "mcp_calls": [], "event_count": len(events)}
        if preferred_mcp and not any(preferred_mcp.lower() in call.lower() for call in mcp_calls):
            self.log_event("codex_preferred_mcp_missing", preferred_mcp=preferred_mcp, mcp_calls=mcp_calls)
            return {"error": f"Codex 调用了 MCP，但没有调用指定的 {preferred_mcp}。", "answer": answer, "mcp_calls": mcp_calls, "event_count": len(events)}
        code_validation = None
        autonomous_tests = None
        if code_task:
            code_validation = self.self_upgrade.validate_current_changes(require_changes=True)
            self.log_event("code_validation", self_edit=self_code_task, result=code_validation)
            if not code_validation.get("ok"):
                max_repairs = max(0, min(4, int(self.config.get("agent", {}).get("code_test_retry_rounds", 2))))
                if code_retry_round < max_repairs:
                    failure = f"本地文件校验失败：{code_validation.get('error', 'unknown error')}"
                    if status: status(f"代码校验失败，正在自主修复（第 {code_retry_round + 1}/{max_repairs} 轮）…")
                    self.log_event("code_repair_retry", stage="validation", round=code_retry_round + 1, failure=failure)
                    return await self._run_codex_task(task, require_mcp, status, preferred_mcp, plan, failure, code_retry_round + 1)
                return {"error": f"代码任务未通过本地校验：{code_validation.get('error', 'unknown error')}", "answer": answer, "validation": code_validation, "event_count": len(events)}
            if status: status("代码已写入，正在由本地模块独立运行测试…")
            autonomous_tests = await asyncio.to_thread(self.self_upgrade.code_editor.run_autonomous_tests, code_validation.get("changed", []))
            self.log_event("code_autonomous_tests", result=autonomous_tests)
            if not autonomous_tests.get("ok"):
                max_repairs = max(0, min(4, int(self.config.get("agent", {}).get("code_test_retry_rounds", 2))))
                if code_retry_round < max_repairs:
                    failed_output = "\n".join(str(row.get("output") or row.get("error") or "") for row in autonomous_tests.get("failed", []))[-5000:]
                    failure = f"本地自主测试失败：\n{failed_output}"
                    if status: status(f"自动测试失败，正在自主修复（第 {code_retry_round + 1}/{max_repairs} 轮）…")
                    self.log_event("code_repair_retry", stage="tests", round=code_retry_round + 1, failure=failure)
                    return await self._run_codex_task(task, require_mcp, status, preferred_mcp, plan, failure, code_retry_round + 1)
                return {"error": f"代码已生成，但自主测试未通过：{autonomous_tests.get('error', 'unknown error')}", "answer": answer, "validation": code_validation, "tests": autonomous_tests, "event_count": len(events)}
        result = {"ok": True, "answer": answer, "thread_id": self.codex_thread_id, "mcp_calls": mcp_calls, "event_count": len(events), "degraded_network": bool(codex_errors), "network_events": codex_errors[-5:], "validation": code_validation, "tests": autonomous_tests}
        self.log_event("codex_task_completed", result=result)
        return result

    def list_skills(self) -> list[dict[str, str]]:
        root = project_path(self.config["agent"].get("skill_root", "Skill")); result = []
        if not root.exists(): return result
        for skill_md in root.glob("*/SKILL.md"):
            text = skill_md.read_text(encoding="utf-8")
            name = re.search(r"^name:\s*(.+)$", text, re.M)
            desc = re.search(r"^description:\s*(.+)$", text, re.M)
            if name: result.append({"name": name.group(1).strip(), "description": desc.group(1).strip() if desc else "", "path": str(skill_md)})
        return result

    def _tools(self, scoped: bool = False) -> list[dict[str, Any]]:
        tools = [
            {"type": "function", "function": {"name": "search_memories", "description": "搜索旧式共享记忆索引。用户询问个人过去经历或‘你记得吗’时不得使用本工具，必须调用 long_term_memory 的 retrieve。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "list_skills", "description": "列出本地可用技能", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "list_character_images", "description": "列出角色形象库、主形象及每张图片可直接传给 analyze_image 的绝对路径。查找自己的立绘、三视图或角色参考图时先调用本工具，不要猜测相对路径。", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "generate_character_image", "description": "调用 ai-live-character-image 技能生成或编辑角色形象", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "operation": {"type": "string", "enum": ["generate", "edit"]}, "reference": {"type": "string", "description": "编辑时使用 primary 或图片路径"}, "label": {"type": "string"}, "tags": {"type": "string"}, "set_primary": {"type": "boolean"}}, "required": ["prompt"]}}},
            {"type": "function", "function": {"name": "analyze_image", "description": "使用 MiMo 多模态 API 理解图片内容、识别画面细节或文字；不用于生成图片。已登记角色图可传 primary、图片ID、文件名、标签或 list_character_images 返回的绝对路径。", "parameters": {"type": "object", "properties": {"image": {"type": "string", "description": "图片绝对路径；已登记角色图也可用 primary、ID、文件名或标签"}, "prompt": {"type": "string", "description": "希望从图片中分析的问题"}}, "required": ["image", "prompt"]}}},
            {"type": "function", "function": {"name": "mimo_transcribe_audio", "description": "使用 MiMo API 识别项目内 WAV 或 MP3 语音文件", "parameters": {"type": "object", "properties": {"audio": {"type": "string", "description": "项目内音频路径"}, "language": {"type": "string", "enum": ["auto", "zh", "en"]}}, "required": ["audio"]}}},
            {"type": "function", "function": {"name": "sing_song", "description": "当用户要求唱歌、唱一首、哼唱或朗读歌词时调用。默认使用角色当前本地 TTS/SVC 音色朗读最多十行歌词；MiMo 唱歌仅作为已关闭的备用分支。", "parameters": {"type": "object", "properties": {"song": {"type": "string", "description": "歌曲名称或演唱主题"}, "lyrics": {"type": "string", "description": "最多十行需要朗读的歌词或测试文本"}, "style": {"type": "string", "description": "演唱或朗读情绪"}, "voice": {"type": "string", "description": "仅备用 MiMo 分支使用"}}, "required": ["song", "lyrics"]}}},
            {"type": "function", "function": {"name": "create_scheduled_task", "description": "创建TTS语音提醒或闹钟。一次性任务成功执行后自动删除；重复任务会保留并等待下一次。必须根据当前本地时间解析用户的自然语言时间。", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "message": {"type": "string", "description": "触发时由TTS播放的文本"}, "recurrence": {"type": "string", "enum": ["once", "daily", "weekdays", "weekly"]}, "scheduled_at": {"type": "string", "description": "仅once使用，本地ISO时间，如2026-07-17T15:00"}, "time": {"type": "string", "description": "重复任务使用的24小时HH:MM"}, "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}, "description": "仅weekly使用，周一为1、周日为7"}, "action": {"type": "string", "enum": ["tts"]}}, "required": ["title", "message", "recurrence"]}}},
            {"type": "function", "function": {"name": "list_scheduled_tasks", "description": "列出当前所有提醒、闹钟和重复任务", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "delete_scheduled_task", "description": "取消并删除一个定时任务", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
            {"type": "function", "function": {"name": "acknowledge_scheduled_task", "description": "当用户明确回应刚才的提醒、表示知道了或已经完成时，确认最近一个待回应任务并停止本轮重复提醒。无关回复不要调用。", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "可选；不填时确认最近的待回应任务"}, "response": {"type": "string", "description": "用户的原始确认回复"}}}}},
            {"type": "function", "function": {"name": "long_term_memory", "description": "结构化长期记忆指令。高价值信息用store；用户询问过去经历或‘你记得吗’时必须先用retrieve。普通闲聊禁止store。", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["store", "retrieve"]}, "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5}, "summary": {"type": "string", "maxLength": 20}, "detail": {"type": "string"}, "category": {"type": "string", "enum": ["health", "emotion", "major_event", "preference", "habit", "relationship", "agreement"]}, "importance": {"type": "integer", "minimum": 70, "maximum": 100}, "query_tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}}, "required": ["action"]}}},
        ]
        if getattr(self, "current_code_task", False) and self.config.get("agent", {}).get("prefer_local_code_tools", True):
            tools += [
                {"type": "function", "function": {"name": "code_list_files", "description": "列出当前代码任务允许目录中的文件。独立项目只能访问 Projects；自修改任务可访问工程源码区。", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "工程根目录相对路径"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}}}},
                {"type": "function", "function": {"name": "code_read_file", "description": "读取代码文本。code_search_text 返回行号后必须用 start_line 从该行附近读取，不能反复读取文件开头；禁止密钥、日志、模型、缓存和运行状态。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "max_lines": {"type": "integer", "minimum": 1, "maximum": 2000}, "max_chars": {"type": "integer", "minimum": 1000, "maximum": 100000}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "code_search_text", "description": "在允许的代码目录中搜索文本并返回文件、行号和内容，用于先定位再修改。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": ["query"]}}},
                {"type": "function", "function": {"name": "code_write_file", "description": "原子创建或完整写入一个代码/测试/README 文件。独立项目写到 Projects/<name>/。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
                {"type": "function", "function": {"name": "code_replace_text", "description": "在已读取文件中精确替换原文，适合小范围修改，找不到原文会失败。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 100}}, "required": ["path", "old", "new"]}}},
                {"type": "function", "function": {"name": "code_validate_project", "description": "扫描本任务真实变更并由 HomeAgent 本地执行语法检查和自动测试。代码任务完成前必须调用且必须返回ok=true。", "parameters": {"type": "object", "properties": {}}}},
            ]
        if self.config.get("vision_mcp", {}).get("enabled", False) and self.config.get("agent", {}).get("model_driven_computer_actions", True):
            tools += [
                {"type": "function", "function": {"name": "bilibili_open_favorite_video", "description": "在用户已经打开且已登录的普通浏览器中，按B站收藏夹真实数据顺序打开指定收藏夹第N个视频。B站收藏夹任务应优先调用；不会创建Chrome、Chromium或临时浏览器。", "parameters": {"type": "object", "properties": {"favorite_folder": {"type": "string", "description": "用户说出的收藏夹名称，例如二次元好看或默认收藏夹"}, "index": {"type": "integer", "minimum": 1, "description": "从1开始的视频序号"}}, "required": ["favorite_folder", "index"]}}},
                {"type": "function", "function": {"name": "media_stop", "description": "幂等地向当前媒体会话发送系统“停止播放”命令。用户要求停止、暂停或关掉音乐时必须调用；禁止用 Space 切换播放状态，也禁止退出或终止音乐应用进程。", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "process_status", "description": "只读查询指定 Windows 进程是否正在运行。进程不存在也返回 ok=true、running=false，避免把查询命令的退出码误当成工具失败。", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "进程映像名，例如 cloudmusic.exe"}}, "required": ["name"]}}},
                {"type": "function", "function": {"name": "ui_analyze_screen", "description": "截取当前桌面并使用 MiMo 回答任务模型提出的视觉问题。用于判断用户在做什么、读取屏幕题目、识别游戏状态和每轮操作后的画面验证；question 必须说明本轮要识别的目标和证据。", "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "针对当前屏幕的具体分析问题，例如识别题干选项并求解，或判断游戏状态和下一步动作"}}, "required": ["question"]}}},
                {"type": "function", "function": {"name": "ui_inspect_target", "description": "观察当前活动目标，判断是可读DOM网页、视觉网页还是原生程序。任何网页/界面任务第一步调用。", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "ui_list_windows", "description": "读取当前可见窗口标题和进程，用于找到并验证目标程序以及媒体播放标题。", "parameters": {"type": "object", "properties": {"title_contains": {"type": "string"}}}}},
                {"type": "function", "function": {"name": "ui_activate_window", "description": "激活一个已经打开的窗口，不启动新浏览器。优先传 ui_list_windows 返回的 title；工具也兼容 hwnd、process_name 和 process_path。", "parameters": {"type": "object", "properties": {"title_contains": {"type": "string", "description": "优先使用窗口真实 title，也可使用 hwnd、进程名或完整进程路径"}}, "required": ["title_contains"]}}},
                {"type": "function", "function": {"name": "ui_type_window", "description": "在指定原生窗口中按语义定位输入框并输入文字。", "parameters": {"type": "object", "properties": {"title_contains": {"type": "string"}, "target": {"type": "string"}, "text": {"type": "string"}}, "required": ["title_contains", "target", "text"]}}},
                {"type": "function", "function": {"name": "ui_click_window", "description": "在指定原生窗口中定位并单击目标。目标描述必须包含当前任务对象，不能用它代替播放指定搜索结果。失败重试时可用candidate_index切换视觉候选点。", "parameters": {"type": "object", "properties": {"title_contains": {"type": "string"}, "target": {"type": "string"}, "candidate_index": {"type": "integer", "minimum": 0, "maximum": 2}}, "required": ["title_contains", "target"]}}},
                {"type": "function", "function": {"name": "ui_double_click_window", "description": "在指定原生窗口中定位并双击目标，适合选择并播放搜索结果行。必须明确描述目标标题，禁止描述底部全局播放按钮。若标题未变化，用candidate_index 1或2重试其他视觉候选点。", "parameters": {"type": "object", "properties": {"title_contains": {"type": "string"}, "target": {"type": "string"}, "candidate_index": {"type": "integer", "minimum": 0, "maximum": 2}}, "required": ["title_contains", "target"]}}},
                {"type": "function", "function": {"name": "ui_analyze_window", "description": "使用 MiMo 图像理解读取指定窗口当前画面并返回文字观察。搜索提交后、选择结果前以及最终验证时调用。", "parameters": {"type": "object", "properties": {"title_contains": {"type": "string"}, "question": {"type": "string"}}, "required": ["title_contains", "question"]}}},
                {"type": "function", "function": {"name": "ui_hotkey", "description": "向当前活动窗口发送按键组合。媒体停止任务禁止使用 Space 或 Alt+F4，必须使用 media_stop。", "parameters": {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, "required": ["keys"]}}},
                {"type": "function", "function": {"name": "ui_type_active_text", "description": "向现有活动窗口已聚焦的输入框输入文字；浏览器无DOM时可在Ctrl+L后填写网址，绝不启动新浏览器。", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "clear": {"type": "boolean"}}, "required": ["text"]}}},
                {"type": "function", "function": {"name": "web_read", "description": "读取当前网页DOM/HTML的正文、链接、按钮和输入框。网页操作优先调用。", "parameters": {"type": "object", "properties": {"max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000}}}}},
                {"type": "function", "function": {"name": "web_navigate", "description": "仅在现有浏览器已开放CDP DOM时导航；无DOM时返回失败，绝不新建浏览器，改用现有窗口Ctrl+L。", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
                {"type": "function", "function": {"name": "web_fill", "description": "按DOM字段名称、label或placeholder填写网页输入框，可选择提交。", "parameters": {"type": "object", "properties": {"field": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, "required": ["field", "text"]}}},
                {"type": "function", "function": {"name": "web_click_text", "description": "按网页DOM中的可见文字点击结果或按钮。", "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "exact": {"type": "boolean"}}, "required": ["text"]}}},
                {"type": "function", "function": {"name": "web_play_media", "description": "播放当前已选定详情页中的媒体。搜索任务必须先选择正确结果，禁止在搜索结果选择前调用。", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "web_get_url", "description": "读取当前网页地址，用于验证导航和结果选择。", "parameters": {"type": "object", "properties": {}}}},
            ]
        if self._codex_config().get("enabled", False):
            tools += [
                {"type": "function", "function": {"name": "codex_cli_task", "description": "仅在本地白名单工具无法完成的复杂编程、终端或自升级任务中调用 Codex CLI。普通网页、视觉和电脑控制必须优先使用本地工具。", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
                {"type": "function", "function": {"name": "mcp_task", "description": "仅当本地工具没有所需能力时，才通过 Codex CLI 调用其他 MCP；网络不稳定，禁止把它作为网页和视觉任务首选。", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
                {"type": "function", "function": {"name": "web_agent_task", "description": "完成多步骤网页任务。只要请求包含搜索、查找、选择、点击、播放、填写或提交，就必须使用本工具，不能仅调用 open_url。", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
                {"type": "function", "function": {"name": "list_mcp_servers", "description": "检查 Codex CLI 和已配置的 MCP 服务", "parameters": {"type": "object", "properties": {}}}},
            ]
            if self.config.get("vision_mcp", {}).get("gui_enabled", False):
                tools.append({"type": "function", "function": {"name": "vision_gui_task", "description": "调用 GUI 图像识别 MCP 完成多步骤桌面或网页操作。必须持续执行用户要求的全部点击、输入、搜索、选择和播放动作，并在最终状态验证后才返回成功。", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}})
        if self.config.get("computer_control", {}).get("enabled", False):
            tools += [
                {"type": "function", "function": {"name": "list_directory", "description": "列出允许目录中的文件和子目录", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "read_text_file", "description": "读取允许目录中的文本文件，不能读取密钥文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "write_text_file", "description": "原子创建或完整写入文本、脚本和配置文件。写入现有文件前先读取，写入后必须重新读取验证。", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
                {"type": "function", "function": {"name": "open_path", "description": "经过用户确认后，用系统默认程序打开允许目录中的文件或文件夹", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "open_url", "description": "仅用于目标就是打开某个网页的单步请求。若还要搜索、查找、点击、选择、播放、填写或提交，禁止使用本工具，必须调用 web_agent_task。", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
                {"type": "function", "function": {"name": "launch_app", "description": "按软件目录映射启动应用或打开软件目录。完整权限模式下也可使用可执行文件绝对路径", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "已配置的软件名称、程序路径或目录路径"}, "arguments": {"type": "array", "items": {"type": "string"}}}, "required": ["name"]}}},
            ]
            shell_cfg = self.config.get("shell_execution", {})
            if shell_cfg.get("shell_enabled", True):
                tools.append({"type": "function", "function": {"name": "run_shell", "description": "在本机非交互 PowerShell 中执行命令。由你根据任务和已观察信息决定命令、工作目录与超时；适合 PowerShell cmdlet、系统查询和文件操作。停止音乐不等于退出应用，禁止为媒体停止任务执行 Stop-Process、taskkill 或其他进程终止命令。执行后必须检查 exit_code、stdout、stderr。", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "完整 PowerShell 命令"}, "cwd": {"type": "string", "description": "可选工作目录"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300}}, "required": ["command"]}}})
            if shell_cfg.get("cmd_enabled", True):
                tools.append({"type": "function", "function": {"name": "run_cmd", "description": "在本机非交互 CMD 中执行命令。由你在 BAT/CMD 语法、传统 Windows 命令或需要 cmd.exe 时自主选择；执行后必须检查 exit_code、stdout、stderr。", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "完整 CMD 命令"}, "cwd": {"type": "string", "description": "可选工作目录"}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300}}, "required": ["command"]}}})
        if self.config.get("agent", {}).get("model_driven_computer_actions", True):
            delegated = {"web_agent_task", "vision_gui_task"}
            tools = [item for item in tools if item.get("function", {}).get("name") not in delegated]
        if getattr(self, "current_code_task", False) and self.config.get("agent", {}).get("prefer_local_code_tools", True):
            tools = [item for item in tools if item.get("function", {}).get("name") != "codex_cli_task"]
        if getattr(self, "current_file_authoring_task", False):
            blocked = {"codex_cli_task", "mcp_task", "launch_app", "open_path"}
            tools = [item for item in tools if item.get("function", {}).get("name") not in blocked
                     and not item.get("function", {}).get("name", "").startswith(("ui_", "web_", "vision_"))]
        if scoped:
            plan = getattr(self, "current_task_plan", {})
            blocked = {"cloudmusic_search_and_play"}
            if not self._is_media_stop_plan(plan):
                blocked.add("media_stop")
            if plan.get("handler") != "bilibili_favorites":
                blocked.add("bilibili_open_favorite_video")
            tools = [item for item in tools if item.get("function", {}).get("name") not in blocked]
        return tools

    def _home_tts_chunks(self, text: str) -> list[str]:
        limit = max(30, int(self.config.get("home", {}).get("tts_chunk_chars", 90)))
        normalized = re.sub(r"\n{3,}", "\n\n", str(text)).strip()
        sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;：:\n])", normalized) if part.strip()]
        pieces: list[str] = []
        for sentence in sentences:
            while len(sentence) > limit:
                cut = max(sentence.rfind(mark, 0, limit + 1) for mark in "，、, ")
                if cut < limit // 2: cut = limit
                pieces.append(sentence[:cut + (1 if cut < len(sentence) and sentence[cut] in "，、, " else 0)].strip())
                sentence = sentence[cut + (1 if cut < len(sentence) and sentence[cut] in "，、, " else 0):].strip()
            if sentence: pieces.append(sentence)
        chunks: list[str] = []
        for piece in pieces:
            if chunks and len(chunks[-1]) + len(piece) + 1 <= limit: chunks[-1] += "\n" + piece
            else: chunks.append(piece)
        return chunks or ([normalized] if normalized else [])

    @staticmethod
    def _contains_unexecuted_tool_markup(text: str) -> bool:
        value = str(text or "")
        return bool(re.search(r"<\s*(?:tool_call|function\s*=|parameter\s*=|/\s*tool_call)", value, re.I))

    @classmethod
    def _answer_is_speakable(cls, text: str) -> bool:
        value = str(text or "").strip()
        if not value or cls._contains_unexecuted_tool_markup(value):
            return False
        if "```" in value or (len(value) > 500 and re.search(r"(?m)^\s*(?:from|import|class|def)\s+", value)):
            return False
        return True

    async def _speak_home(self, session: aiohttp.ClientSession, text: str, status=None, ignore_cancel: bool = False) -> list[str]:
        if not self._answer_is_speakable(text):
            self.log_event("home_tts_skipped_unsafe_content", reason="tool_markup_or_code", chars=len(str(text or "")))
            return []
        await asyncio.to_thread(self.tts_execution_lock.acquire)
        try:
            try:
                return await self._speak_home_unlocked(session, text, status, ignore_cancel=ignore_cancel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log_event("home_tts_failed", error=str(exc), fallback="windows_sapi")
                if status: status("主语音服务暂时不可用，正在使用系统语音播报…")
                spoken = await asyncio.to_thread(self._windows_sapi_speak, text)
                self.log_event("home_tts_fallback", ok=spoken)
                return []
        finally:
            self.tts_execution_lock.release()

    async def _speak_with_fresh_session(self, text: str, status=None, ignore_cancel: bool = False) -> list[str]:
        """Speak after a previous tool-loop HTTP session has already left its context manager."""
        timeout = aiohttp.ClientTimeout(total=int(self.project.get("tts", {}).get("timeout_seconds", 60)) + 30)
        async with aiohttp.ClientSession(timeout=timeout) as speech_session:
            return await self._speak_home(speech_session, text, status, ignore_cancel=ignore_cancel)

    @staticmethod
    def _windows_sapi_speak(text: str) -> bool:
        if os.name != "nt": return False
        safe = _tts_safe_text_for_fallback(text)[:1200]
        if not safe: return False
        env = os.environ.copy(); env["HOME_AGENT_TTS_TEXT"] = safe
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$s.Speak($env:HOME_AGENT_TTS_TEXT)"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=90,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    async def _speak_home_unlocked(self, session: aiohttp.ClientSession, text: str, status=None, ignore_cancel: bool = False) -> list[str]:
        chunks = self._home_tts_chunks(text)
        client = TTSClient(session, self.project["tts"], ROOT / "audio"); paths: list[str] = []
        self.log_event("home_tts_split", chunks=len(chunks), chunk_chars=self.config.get("home", {}).get("tts_chunk_chars", 90))
        if not chunks: return paths
        queue: asyncio.Queue[tuple[int, Path] | None] = asyncio.Queue()

        async def generate() -> None:
            try:
                for index, chunk in enumerate(chunks, start=1):
                    if self.cancel_event.is_set() and not ignore_cancel:
                        self.log_event("home_tts_cancelled", stage="generate", index=index)
                        break
                    if status: status(f"正在生成语音 {index}/{len(chunks)}…")
                    path = await client.synthesize(chunk)
                    if path:
                        paths.append(str(path)); await queue.put((index, path))
                        self.log_event("home_tts_queued", index=index, path=path)
            finally:
                await queue.put(None)

        async def play_in_order() -> None:
            while True:
                item = await queue.get()
                if item is None: break
                if self.cancel_event.is_set() and not ignore_cancel:
                    self.log_event("home_tts_cancelled", stage="play")
                    break
                index, path = item
                if self.project["tts"].get("play_audio", True):
                    if status: status(f"正在播放语音 {index}/{len(chunks)}…")
                    self.log_event("home_tts_play_started", index=index, path=path)
                    await client.play(path)
                    self.log_event("home_tts_play_completed", index=index, path=path)

        await asyncio.gather(generate(), play_in_order())
        return paths

    async def speak_progress_report(self, task: str, completed: list[str], current: str, elapsed_seconds: int) -> None:
        """Summarize a long-running task and speak one short, non-blocking update."""
        try:
            provider, key = self._provider(); llm_cfg = self.project["llm"]
            prompt = (
                "把下面的任务进度改写成一句自然、简短的中文口语，最多45个汉字。"
                "说明已经完成什么、现在正在做什么；不要声称任务已经全部完成，不要读秒数、路径或技术日志。\n"
                f"用户任务：{task[:300]}\n已完成：{json.dumps(completed[-5:], ensure_ascii=False)}\n"
                f"当前：{current}\n已运行：{elapsed_seconds}秒"
            )
            timeout = aiohttp.ClientTimeout(total=min(20, int(llm_cfg.get("timeout_seconds", 45))))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {"model": provider["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
                self._set_token_limit(payload, provider, 100)
                async with session.post(provider["base_url"].rstrip("/") + "/chat/completions", json=payload, headers=self._provider_headers(provider, key)) as response:
                    raw = await response.text()
                    if response.status >= 400: raise RuntimeError(f"progress LLM HTTP {response.status}")
                    spoken = str(json.loads(raw)["choices"][0]["message"].get("content", "")).strip()
                if spoken:
                    await self._speak_home(session, spoken, None)
                    self.log_event("task_progress_spoken", text=spoken, elapsed_seconds=elapsed_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log_event("task_progress_speech_failed", error=str(exc))

    async def proactive_screen_care(self, notification_ready=None) -> str:
        """Capture the primary screen once, let MiMo compose a privacy-safe caring line, then discard it."""
        cfg = self.config.get("screen_care", {})
        if not cfg.get("enabled", True):
            return ""
        screenshot: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="home-agent-screen-", suffix=".png", delete=False) as handle:
                screenshot = Path(handle.name)
            image = await asyncio.to_thread(_grab_screen_with_retry, all_screens=bool(cfg.get("all_screens", False)))
            try:
                await asyncio.to_thread(image.save, screenshot, "PNG")
            finally:
                if hasattr(image, "close"):
                    image.close()
            prompt = (
                "你是家庭桌宠，请观察当前屏幕，用一句自然、温柔、不过度打扰的中文向主人问候或关心。"
                "可以结合正在进行的活动（如工作、学习、娱乐）提醒休息、喝水或鼓励，但不要复述屏幕上的姓名、"
                "账号、聊天、文件内容、密码、验证码、金额等隐私信息，也不要假装知道画面之外的情况。"
                f"只输出一句话，不要解释，不要Markdown，最多{max(12, int(cfg.get('max_chars', 42)))}个汉字。"
            )
            timeout = aiohttp.ClientTimeout(total=int(self.mimo_multimodal.config.get("timeout_seconds", 60)))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                result = await self.mimo_multimodal.analyze_image(session, screenshot, prompt)
                message = re.sub(r"\s+", " ", str(result.get("text") or "")).strip().strip('"“”')
                if not message:
                    return ""
                message = message[:max(12, int(cfg.get("max_chars", 42)))]
                max_context = int(self.config.get("home", {}).get("max_context_messages", 30))
                self.history.append({"role": "assistant", "content": message, "source": "proactive_screen_care"})
                self.history = self.history[-max_context:]
                if notification_ready:
                    notification_ready(message)
                if cfg.get("speak", True) and self.config.get("home", {}).get("auto_speak", True):
                    await self._speak_home(session, message, None, ignore_cancel=True)
                self.log_event("proactive_screen_care", model=result.get("model"), message=message)
                return message
        except Exception as exc:
            self.log_event("proactive_screen_care_failed", error=str(exc))
            return ""
        finally:
            if screenshot:
                try:
                    screenshot.unlink(missing_ok=True)
                except OSError as exc:
                    self.log_event("proactive_screen_cleanup_failed", error=str(exc))

    @staticmethod
    def _publish_answer(answer: str, answer_ready=None) -> None:
        if answer_ready and str(answer or "").strip():
            answer_ready(str(answer).strip())

    async def analyze_current_screen(self, question: str, status=None) -> dict[str, Any]:
        """Capture the desktop and answer a visual question chosen by the task model."""
        screenshot = None
        request_submitted_at = _iso_now()
        try:
            if status: status("正在截取屏幕…")
            
            with tempfile.NamedTemporaryFile(prefix="home-agent-screen-read-", suffix=".png", delete=False) as handle:
                screenshot = Path(handle.name)
            image = await asyncio.to_thread(_grab_screen_with_retry, all_screens=False)
            screenshot_captured_at = _iso_now()
            try:
                await asyncio.to_thread(image.save, screenshot, "PNG")
            finally:
                if hasattr(image, "close"):
                    image.close()
            
            if status: status("正在分析屏幕内容…")
            
            prompt = str(question or "请根据当前任务准确分析屏幕，并指出支持结论的可见证据。").strip()
            
            timeout = aiohttp.ClientTimeout(total=int(self.mimo_multimodal.config.get("timeout_seconds", 60)))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                result = await self.mimo_multimodal.analyze_image(session, screenshot, prompt)
                answer = re.sub(r"\s+", " ", str(result.get("text") or "")).strip().strip('\'"\'"')
                
                if not answer:
                    return {"ok": False, "error": "屏幕分析没有返回内容"}
                response = {
                    "ok": True, "observation": answer, "model": result.get("model"), "question": prompt,
                    "request_submitted_at": request_submitted_at,
                    "screenshot_captured_at": screenshot_captured_at,
                    "analysis_completed_at": _iso_now(),
                }
                self.log_event("screen_analysis_completed", question=prompt[:200], answer=answer[:300])
                return response
                
        except Exception as exc:
            self.log_event("screen_analysis_failed", question=str(question)[:200], error=str(exc))
            return {"ok": False, "error": f"读取屏幕失败：{str(exc)[:200]}"}
        finally:
            if screenshot:
                try:
                    Path(screenshot).unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def is_restart_request(text: str) -> bool:
        """Recognize direct restart commands without routing capability questions to the LLM."""
        value = re.sub(r"[\s，。！？、,.!?;；:：]+", "", str(text or "")).lower()
        if not value or any(word in value for word in ("不要重启", "别重启", "停止重启", "取消重启")):
            return False
        if any(word in value for word in ("如何", "怎么", "为什么", "能不能", "能否", "是否", "可以吗", "会不会")):
            return False
        if any(word in value for word in ("功能", "支持", "实现", "增加", "添加", "修改", "完善", "代码", "消息", "识别", "处理")):
            return False
        exact = {
            "重启", "重新启动", "重启自己", "自己重启", "重启你自己", "你重启自己",
            "重启homeagent", "重新启动homeagent", "重启桌宠", "重新启动桌宠",
        }
        if value in exact:
            return True
        has_target = any(word in value for word in ("重启自己", "重启你自己", "重启homeagent", "重启桌宠", "重新启动自己", "重新启动homeagent", "重新启动桌宠"))
        imperative = value.startswith(("请", "麻烦", "现在", "立即", "马上", "帮我")) or value.endswith(("吧", "一下"))
        return has_target and imperative

    async def chat(self, text: str, status=None, confirm=None, answer_ready=None, image_path=None) -> str:
        self.current_task_submitted_at = _iso_now()
        if self.is_restart_request(text):
            answer = "好的主人，Home Agent 正在重启。"
            self.restart_requested = True
            self.log_event("direct_restart_requested", message=text)
            self._publish_answer(answer, answer_ready)
            return answer
        
        # Prompt wake detection
        is_wake, wake_command = self.detect_wake_word(text)
        if is_wake:
            self.log_event("prompt_wake_activated", original=text, command=wake_command)
            text = wake_command
        
        image_paths = [image_path] if isinstance(image_path, (str, Path)) else list(image_path or [])
        text = str(text or "").strip() or (("请分析这些图片。" if len(image_paths) > 1 else "请分析这张图片。") if image_paths else "")
        self._acknowledge_common_response(text)
        max_context = int(self.config["home"].get("max_context_messages", 30))
        user_history = {"role": "user", "content": text}
        if image_paths: user_history.update({"source": "clipboard_image", "has_image": True, "image_count": len(image_paths)})
        self.history.append(user_history); self.history = self.history[-max_context:]
        self.log_event("user_message", message=text, history_messages=len(self.history), has_image=bool(image_paths), image_count=len(image_paths))
        planner_context_limit = int(self.config.get("semantic_planner", {}).get("context_messages", 8))
        recent_task_context = self._planner_context(self.history[:-1], planner_context_limit)
        if status: status("正在理解任务并制定执行计划…")
        planning_text = text
        if image_paths:
            planning_text += f"\n[本消息已附带 {len(image_paths)} 张图片；执行模型可直接读取全部附件，不要仅因图片内容未知而追问。]"
        task_plan = await self._plan_task(planning_text, recent_task_context)
        if image_paths:
            task_plan["has_image_attachment"] = True
            task_plan["image_attachment_count"] = len(image_paths)
        self.current_task_plan = task_plan
        code_task = bool(task_plan.get("is_task") and task_plan.get("actionable") and task_plan.get("domain") == "code")
        self.current_code_task = code_task
        self.current_file_authoring_task = bool(task_plan.get("is_task") and task_plan.get("actionable") and task_plan.get("domain") == "file")
        self.current_code_self_edit = bool(code_task and self.self_upgrade.is_upgrade_request(text))
        self.current_code_verified = not self.current_code_self_edit
        self.log_event("task_plan_created", task_plan=task_plan)
        self._emit_activity(status, {
            "type": "plan",
            "title": "任务计划已生成",
            "detail": f"{task_plan.get('domain', '任务')} · {task_plan.get('operation', 'execute')}",
            "reasoning_summary": str(task_plan.get("reasoning_short") or "根据当前请求和最近上下文确定执行方式。"),
            "steps": list(task_plan.get("steps") or []),
            "success_criteria": str(task_plan.get("success_criteria") or ""),
            "state": "completed",
        }, "任务计划已生成")
        if task_plan.get("requires_clarification"):
            answer = str(task_plan.get("clarification_question") or "我还缺少完成这项任务所需的关键信息，请补充一下具体目标。").strip()
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            self._publish_answer(answer, answer_ready)
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True):
                    await self._speak_home(session, answer, status)
            return answer
        recalled_memories = []
        if self._is_memory_recall_request(text):
            query_tags = self._memory_query_tags(text)
            recalled_memories = self.long_term_memory.retrieve(query_tags, limit=8, user_id="owner")
            self.log_event("long_term_memory_retrieved", query_tags=query_tags, matches=len(recalled_memories), route="deterministic")
        normalized_clear = str(text).replace(" ", "")
        if (any(word in normalized_clear for word in ("清理", "清空", "删除")) and "直播" in normalized_clear
                and any(word in normalized_clear for word in ("上下文", "聊天记录", "对话记录", "近期对话"))):
            request = self._request_live_context_clear()
            removed = request.get("removed_messages", 0)
            answer = f"好，直播场景的短期聊天上下文已经独立清空了，共移除{removed}条，长期记忆不会受影响。"
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            self._publish_answer(answer, answer_ready)
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True): await self._speak_home(session, answer, status)
            return answer
        legacy_direct_site = task_plan.get("site") in {"cloudmusic", "bilibili"}
        if legacy_direct_site and not self.config.get("agent", {}).get("model_driven_computer_actions", True):
            visual_query = str(task_plan.get("query") or "")
            visual_target = "B站" if task_plan.get("site") == "bilibili" else "网易云"
            gui_enabled = bool(self.config.get("vision_mcp", {}).get("gui_enabled", True))
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True):
                    await self._speak_home(session, f"好呀，我去{visual_target}帮你找找{visual_query or '想要的内容'}。", status)
            is_favorite_request = "收藏夹" in text or ("收藏" in text and any(word in text for word in ("第", "默认", "我的")))
            operation_timeout = max(120 if is_favorite_request else 5, int(self.config.get("vision_mcp", {}).get("direct_operation_timeout_seconds", 20)))
            try:
                is_bilibili = task_plan.get("site") == "bilibili"
                # Bilibili has deterministic DOM verification and must not be
                # downgraded to a single visual click merely because GUI is on.
                operation = self._run_direct_web_media(text, status, task_plan) if is_bilibili or not gui_enabled else self._run_direct_visual_media(text, status, task_plan)
                direct_visual = await asyncio.wait_for(operation, timeout=operation_timeout)
            except asyncio.TimeoutError:
                self.stop_current_task()
                direct_visual = {"error": f"超过{operation_timeout}秒仍未执行成功，操作已超时停止"}
                self.log_event("direct_vision_total_timeout", limit_seconds=operation_timeout)
            if direct_visual is None:
                direct_visual = {"error": "图像 GUI 识别已禁用，而且该目标不是可直接读取的网页"}
            deterministic_parameter_error = bool(
                task_plan.get("handler") == "bilibili_favorites"
                and direct_visual.get("error")
                and any(marker in str(direct_visual.get("error")) for marker in ("找不到收藏夹", "没有第", "缺少有效 BV"))
            )
            if deterministic_parameter_error:
                self.log_event("deterministic_failure_preserved", handler=task_plan.get("handler"), error=direct_visual.get("error"))
            if direct_visual.get("error") and not deterministic_parameter_error and self._codex_config().get("enabled", False):
                previous_failure = str(direct_visual["error"])
                self.log_event("deterministic_route_fallback", handler=task_plan.get("handler"), error=previous_failure)
                if status: status("固定流程未完成，正在切换通用 Agent 继续处理…")
                preferred = str(self.config.get("vision_mcp", {}).get("server_name", "vision-gui"))
                try:
                    fallback_timeout = self._codex_task_timeout(preferred) + 10
                    direct_visual = await asyncio.wait_for(
                        self._run_codex_task(text, require_mcp=True, status=status, preferred_mcp=preferred, task_plan=task_plan, previous_failure=previous_failure),
                        timeout=fallback_timeout,
                    )
                except asyncio.TimeoutError:
                    self.stop_current_task(); direct_visual = {"error": f"固定流程失败后，通用 Agent 在 {fallback_timeout} 秒内也未完成"}
            if direct_visual.get("error") and deterministic_parameter_error:
                answer = f"这次没有执行完成：{direct_visual['error']}。我保留了实际错误和可用选项，没有再盲目切换其他流程。"
            elif direct_visual.get("error"):
                answer = await self._natural_visual_failure(text, str(direct_visual["error"]))
            else:
                answer = str(direct_visual.get("answer", "好啦，已经帮你操作完成了。"))
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            self._publish_answer(answer, answer_ready)
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True): await self._speak_home(session, answer, status, ignore_cancel=True)
            return answer
        web_route = self._should_route_to_web(task_plan)
        vision_route = self._should_route_to_vision(task_plan)
        prefer_local_tools = bool(self.config.get("agent", {}).get("prefer_local_tools", True))
        codex_requested = self._should_route_to_codex(text)
        delegate_ui_to_codex = (web_route or vision_route) and not prefer_local_tools
        if codex_requested or delegate_ui_to_codex:
            route = "web_agent" if web_route and delegate_ui_to_codex else ("vision_mcp" if vision_route and delegate_ui_to_codex else "codex_cli")
            reason = "multi_step_web" if web_route else ("vision_priority" if vision_route else "trigger_mode_or_keyword")
            self.log_event("route_selected", route=route, reason=reason)
            preferred = str(self.config.get("vision_mcp", {}).get("server_name", "vision-gui")) if (web_route or vision_route) else ""
            if web_route or vision_route:
                async with aiohttp.ClientSession() as session:
                    if self.config["home"].get("auto_speak", True): await self._speak_home(session, "好呀，我来看一下屏幕，很快就好。", status)
                result = await self._run_codex_task(text, require_mcp=True, status=status, preferred_mcp=preferred, task_plan=task_plan)
            else:
                result = await self._run_codex_task(text, require_mcp=False, status=status, preferred_mcp="", task_plan=task_plan)
            if result.get("error"):
                answer = (await self._natural_visual_failure(text, str(result["error"])) if vision_route
                          else f"Codex CLI 执行失败：{result['error']}")
            else:
                answer = str(result.get("answer", "")).strip()
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            self._publish_answer(answer, answer_ready)
            async with aiohttp.ClientSession() as session:
                try:
                    provider, key = self._provider()
                    await self._maybe_remember_home(text, answer, session, provider, key)
                except Exception:
                    pass
                if self.config["home"].get("auto_speak", True) and answer:
                    await self._speak_home(session, answer, status)
            return answer
        self.log_event("route_selected", route="llm_tool_loop", local_tools_preferred=prefer_local_tools, web_route=web_route, vision_route=vision_route)
        provider, key = self._provider(); llm_cfg = self.project["llm"]
        memory_context = ""
        if recalled_memories:
            memory_context = "\n\n【已从SQLite长期记忆检索到的事实】\n" + json.dumps(recalled_memories, ensure_ascii=False) + "\n回答相关问题时只依据这些事实，不要虚构。"
        elif self._is_memory_recall_request(text):
            memory_context = "\n\n【SQLite长期记忆检索结果为空】不要声称记得具体事实；如实说明没有找到。"
        operation_contract = ""
        if task_plan.get("actionable"):
            operation_contract = (
                "\n\n【当前操作任务计划】\n" + json.dumps(task_plan, ensure_ascii=False) +
                "\n该计划由语义规划模型判定。严格遵循 execution_strategy、preferred_tools、required_capabilities、steps 和 success_criteria，"
                "你负责根据观察结果逐步决定并调用白名单工具；本地代码只执行工具调用、实施权限边界并验证结果。"
                "网页、视觉、窗口、文件和已提供的专用站点工具必须优先在本地执行；只有本地工具明确缺少能力时才可调用 Codex。"
                "每次工具返回后重新判断下一步，不得跳过选择目标和终态验证。"
                "点击、输入、快捷键和滚动工具会自动等待并重新截图；必须读取 state_changed、post_action_verified 和 next_action。"
                "若返回 status=uncertain 或 state_changed=false，不得继续假设操作成功，必须重新识别、切换候选点或使用另一种操作方式。"
                "所有工具证据都带 task_submitted_at、tool_submitted_at、tool_completed_at 和 tool_sequence；判断状态时必须让较新的同对象证据覆盖旧证据。"
                "Vision 的 vision_request_submitted_at/screenshot_captured_at 表示画面所属时刻，分析完成较晚时不得把旧画面当成当前状态。"
                + ("当前是B站收藏夹任务：直接调用 bilibili_open_favorite_video，并把任务计划中的 favorite_folder 和 index 原样传入。"
                   "该工具会读取真实收藏夹顺序、复用现有登录浏览器并验证最终BV地址；不要用截图猜收藏夹入口。"
                   if task_plan.get("handler") == "bilibili_favorites" else "") +
                ("当前是模型驱动的网易云界面任务。严格执行计划中的steps，并在每一步后读取最新窗口。"
                 "先用ui_list_windows判断程序是否存在；不存在才用launch_app，存在则激活。"
                 "用ui_analyze_window识别当前界面和可操作控件，再根据观察选择ui_click_window、ui_type_active_text或ui_hotkey。"
                 "query为空时禁止进入搜索框；query非空时只能输入计划中的原始query，搜索后必须重新识别结果并选择匹配项。"
                 "点击后再次调用ui_analyze_window验证实际播放对象和状态；不得用固定坐标、固定候选或预设页面结构。"
                if task_plan.get("site") == "cloudmusic" and not self._is_media_stop_plan(task_plan) else "") +
                ("当前是停止媒体任务：只调用幂等 media_stop。停止音乐不等于退出应用；禁止 Space、播放按钮、Alt+F4、Stop-Process 和 taskkill。"
                 if self._is_media_stop_plan(task_plan) else "") +
                ("当前计划明确要求关闭应用或终止进程，可以在常规关闭无效后使用 Stop-Process/taskkill；执行后用 process_status 验证进程已退出。"
                 if self._allows_application_termination(task_plan) else "") +
                "网页先调用 ui_inspect_target；若是 browser_dom，优先 web_read/web_fill/web_click_text。"
                "若是 browser_visual，必须保持现有浏览器，用 ui_hotkey Ctrl+L、ui_type_active_text、Enter 和窗口视觉工具操作。"
                "禁止调用 launch_app 启动 Chrome/Edge/Firefox，也禁止为了DOM能力创建新浏览器。"
                "原生程序第一次调用 ui_list_windows 时不要传标题过滤；应用窗口标题可能是当前文档或歌曲名，"
                "应从全部窗口的 process_name 找到目标进程，再把同一条记录返回的真实 title（不是 process_path）用于激活、输入和点击。"
                "确认程序确实未运行后才允许启动；随后输入、提交并选择目标。"
                "原生窗口搜索提交后必须调用 ui_analyze_window 观察结果列表，再根据观察双击准确结果。"
                "选择点击锚点时使用分析结果中最有区分度的精确可见文字；存在明确歌手时优先用歌手名或‘歌名+歌手’，"
                "不要只描述‘结果区域’或模糊整行。双击工具返回 before_title/after_title；"
                "只有该自动化工具自身造成 after_title 切换到目标，才可作为自动播放成功证据。"
                "用户或其他程序在工具调用之外改变的状态不能算自动化成功。"
                "当 query 非空且目标是播放搜索结果时，必须先明确选择与 query 匹配的结果；"
                "原生音乐列表优先双击准确的歌曲行。禁止直接点击底部或全局播放按钮来代替选择目标。"
                "操作后必须再次读取网页、URL或窗口标题验证实际目标已打开/播放；没有证据不得报告成功。"
            )
            if task_plan.get("visual_required"):
                mode = str(task_plan.get("interaction_mode") or "observe")
                operation_contract += (
                    "\n\n【模型驱动屏幕任务】本任务的视觉需求来自语义规划器，不得再用固定短语或固定截图问题替代。"
                    "第一步调用 ui_analyze_screen，并根据任务目标自行编写具体 question。"
                    "若 interaction_mode=observe，只读取和回答，不点击；若为 solve，先完整识别题干、选项、图形和约束，推理后回答，只有用户明确要求代为作答时才操作界面；"
                    "若为 game，先识别游戏、当前局面、可用操作和目标，再调用 ui_list_windows/ui_analyze_window 与点击或按键工具执行一步，随后重新观察。"
                    "游戏不得盲目连续输入；每一步必须基于最新画面，状态不明时停止并说明。"
                    f"当前 interaction_mode={mode}。"
                )
        if self.current_file_authoring_task:
            operation_contract += (
                "\n\n【本地文件创建任务】这是文件系统任务，不是界面自动化任务。"
                "禁止启动终端窗口、禁止调用视觉/网页/Codex。先用 list_directory 和 read_text_file 检查真实目录与入口，"
                "再用 write_text_file 原子写入目标文件，最后重新 read_text_file 验证内容；本地工具足够时不得网络回退。"
            )
        if code_task and self.config.get("agent", {}).get("prefer_local_code_tools", True):
            local_code_contract, loaded_documents = self.self_upgrade.code_editor.build_execution_contract(self_edit=self.current_code_self_edit)
            self.log_event("local_code_task_prepared", self_edit=self.current_code_self_edit, documents=loaded_documents)
            operation_contract += (
                "\n\n【本地代码工具主路径】\n" + local_code_contract +
                "必须优先使用 code_list_files、code_read_file、code_search_text、code_write_file、code_replace_text 完成编辑。"
                "完成后必须调用 code_validate_project；只有它返回 ok=true 才能结束。"
                "不要主动调用 Codex，网络执行器仅由主程序在本地工具彻底失败后低优先级回退。"
            )
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt() + memory_context + operation_contract}, *self.history]
        if image_paths:
            for message_index in range(len(messages) - 1, 0, -1):
                if messages[message_index].get("role") == "user":
                    messages[message_index] = {**messages[message_index], "content": self._image_message_content(text, image_paths)}
                    break
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        timeout = aiohttp.ClientTimeout(total=llm_cfg.get("timeout_seconds", 45))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            singing_performed = False
            created_task_result = None
            long_term_stored = False
            automated_media_target_verified = False
            cloudmusic_verified_result = None
            bilibili_favorite_verified = False
            bilibili_favorite_result = None
            initial_media_state_checked = False
            media_target_preexisting = False
            tool_failures: list[dict[str, str]] = []
            completion_evidence: list[dict[str, Any]] = []
            tool_sequence = 0
            code_inspection_iterations = 0
            completion_check_failures = 0
            local_code_verified = False
            max_failed_rounds = max(1, int(self.config["agent"].get("max_tool_rounds", 8)))
            max_tool_iterations = max(max_failed_rounds, int(self.config["agent"].get("max_tool_iterations", max_failed_rounds * 4)))
            failed_rounds = 0
            for round_index in range(max_tool_iterations):
                if failed_rounds >= max_failed_rounds:
                    break
                round_failed = False
                if status: status("正在思考…")
                tuning = llm_cfg.get("home", {})
                payload = {"model": provider["model"], "messages": messages, "tools": self._tools(scoped=True), "tool_choice": "auto", "temperature": tuning.get("temperature", llm_cfg.get("temperature", .7))}
                self._set_token_limit(payload, provider, int(tuning.get("max_tokens", llm_cfg.get("max_tokens", 600))))
                async with session.post(url, json=payload, headers=self._provider_headers(provider, key)) as response:
                    raw = await response.text()
                    if response.status >= 400: raise RuntimeError(f"LLM HTTP {response.status}: {raw[:600]}")
                    response_data = json.loads(raw)
                    response_choice = response_data["choices"][0]
                    finish_reason = response_choice.get("finish_reason")
                    choice = response_choice["message"]
                if self._is_incomplete_model_response(finish_reason):
                    failed_rounds += 1
                    self.log_event("incomplete_model_response_rejected", finish_reason=finish_reason, round=round_index)
                    messages.append({
                        "role": "system",
                        "content": (
                            f"上一次响应因 finish_reason={finish_reason} 未完整生成，未执行其中任何工具。"
                            "请缩短说明；若需要工具，只生成参数完整的结构化 tool_calls。"
                        ),
                    })
                    continue
                messages.append(choice)
                calls = choice.get("tool_calls") or []
                if not calls:
                    answer = (choice.get("content") or "").strip()
                    if self._contains_unexecuted_tool_markup(answer):
                        failed_rounds += 1
                        self.log_event("unexecuted_tool_markup_rejected", answer=answer, round=round_index)
                        messages.append({
                            "role": "system",
                            "content": (
                                "你刚才把工具调用写进了普通文本，因此它没有执行。禁止输出 <tool_call>、<function=> 或 "
                                "<parameter=> 标签；请通过 API 的结构化 tool_calls 字段调用当前可用工具。"
                            ),
                        })
                        continue
                    if code_task and self.config.get("agent", {}).get("prefer_local_code_tools", True) and not local_code_verified:
                        failed_rounds += 1
                        self.log_event("unverified_local_code_answer_rejected", answer=answer, round=round_index)
                        messages.append({"role": "system", "content": "代码任务还没有通过本地验证。继续调用本地 code_* 工具实际写入代码，最后调用 code_validate_project；没有 ok=true 的测试证据不得回答完成。"})
                        continue
                    requires_automated_media_proof = bool(
                        task_plan.get("site") == "cloudmusic"
                        and bool(task_plan.get("query_is_explicit"))
                        and str(task_plan.get("query") or "").strip()
                        and bool(task_plan.get("final_action_requires_verification") or task_plan.get("operation") == "play")
                    )
                    if requires_automated_media_proof and not (automated_media_target_verified or media_target_preexisting):
                        failed_rounds += 1
                        self.log_event("unproven_media_success_rejected", answer=answer, query=task_plan.get("query"))
                        messages.append({"role": "system", "content": "当前目标播放尚无自动化归因证据。不要依据用户手动操作后的画面报告成功；继续执行精确自动双击。只有 ui_double_click_window 自身返回的 after_title 包含目标 query，才可完成。"})
                        continue
                    requires_bilibili_favorite_proof = task_plan.get("handler") == "bilibili_favorites"
                    if requires_bilibili_favorite_proof and not bilibili_favorite_verified:
                        failed_rounds += 1
                        self.log_event("unproven_bilibili_favorite_success_rejected", answer=answer, task_plan=task_plan)
                        messages.append({"role": "system", "content": "B站收藏夹任务尚未取得受验证结果。立即调用 bilibili_open_favorite_video；只有返回 ok=true、used_existing_browser=true、BV号以及匹配的收藏夹序号后才能完成。"})
                        continue
                    if task_plan.get("actionable") and round_index == 0:
                        failed_rounds += 1
                        self.log_event("premature_answer_rejected", answer=answer, task_plan=task_plan)
                        messages.append({"role": "system", "content": "这是需要实际执行的操作任务，但你尚未调用任何工具。不要只描述步骤或声称完成；立即选择合适工具执行，并在获得终态证据后回答。"})
                        continue
                    if created_task_result:
                        task = created_task_result["task"]
                        # 精确时间、任务 ID 和队列长度仅供内部状态同步，不能进入普通回复或 TTS。
                        answer = f"好啦主人，{task['title']}已经设置好了。"
                    if bilibili_favorite_result:
                        # 收藏夹执行器已经验证浏览器、序号和最终 BV；不要让模型
                        # 在完成措辞中额外声称“正在播放”等未经验证的状态。
                        answer = str(bilibili_favorite_result.get("answer") or answer)
                    if cloudmusic_verified_result:
                        answer = str(cloudmusic_verified_result.get("answer") or answer)
                    if task_plan.get("actionable"):
                        try:
                            verification = await self.mimo_multimodal.verify_completion(session, text, task_plan, answer, completion_evidence)
                        except Exception as exc:
                            verification = {"passed": not bool(self.mimo_multimodal.config.get("fail_closed", True)), "reason": f"MiMo 完成检查不可用：{exc}", "next_action": "检查 MiMo 配置和网络后，继续收集本地终态证据"}
                        self.log_event("mimo_completion_check", result=verification, evidence_count=len(completion_evidence), round=round_index)
                        self._emit_activity(status, {
                            "type": "verification",
                            "title": "完成验证" + (" · 通过" if verification.get("passed") else " · 未通过"),
                            "detail": self._activity_text(
                                verification.get("reason")
                                or ("已取得足够的完成证据" if verification.get("passed") else verification.get("next_action")),
                                110,
                            ),
                            "state": "success" if verification.get("passed") else "failed",
                        }, "完成检查已通过" if verification.get("passed") else "完成检查未通过")
                        if not verification.get("passed"):
                            completion_check_failures += 1
                            failed_rounds += 1
                            reason = str(verification.get("reason") or "完成证据不足")
                            if status: status(f"完成检查未通过：{reason[:100]}；正在继续修正…")
                            if completion_check_failures <= int(self.mimo_multimodal.config.get("completion_max_retries", 2)):
                                messages.append({"role": "user", "content": f"独立完成检查未通过：{reason}\n建议下一步：{verification.get('next_action') or '重新观察并取得可验证的终态证据'}。请继续调用本地工具修正，不要重复口头声明。"})
                                continue
                            answer = f"任务执行后的独立检查仍未通过：{reason[:220]}。我没有把未验证的结果报告为完成。"
                    self.log_event("assistant_answer", answer=answer, tool_round_complete=True)
                    self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
                    self._publish_answer(answer, answer_ready)
                    if not long_term_stored:
                        await self._maybe_remember_home(text, answer, session, provider, key)
                    if self.config["home"].get("auto_speak", True) and answer and not singing_performed:
                        await self._speak_home(session, answer, status)
                    return answer
                for call in calls:
                    tool_sequence += 1
                    name = call["function"]["name"]
                    post_tool_instruction = ""
                    try:
                        args = self._parse_tool_arguments(call["function"].get("arguments"))
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        round_failed = True
                        failure_reason = f"工具参数不是合法 JSON 对象：{exc}"
                        result = {"status": "failed", "tool": name, "error": failure_reason, "executed": False}
                        completion_evidence.append({"tool": name, "result": result})
                        tool_failures.append({"tool": name, "reason": failure_reason[:500]})
                        self.log_event("tool_arguments_rejected", tool=name, error=failure_reason, round=round_index)
                        if status: status(f"没有执行工具 {name}：参数不完整；正在要求模型重新生成…")
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
                        continue
                    display_name = self._tool_display_name(name)
                    self._emit_activity(status, {
                        "type": "tool_start", "title": display_name,
                        "detail": self._tool_activity_arguments(name, args), "tool": name, "state": "running",
                    }, f"正在执行：{display_name}")
                    tool_submitted_at = _iso_now()
                    tool_started = time.monotonic()
                    self.log_event("tool_started", tool=name, arguments=args, sequence=tool_sequence, task_submitted_at=self.current_task_submitted_at, tool_submitted_at=tool_submitted_at)
                    result = self._normalize_tool_result(name, await self._run_tool(name, args, confirm, status))
                    if isinstance(result, dict):
                        result.setdefault("task_submitted_at", self.current_task_submitted_at)
                        result.setdefault("tool_submitted_at", tool_submitted_at)
                        result.setdefault("tool_completed_at", _iso_now())
                        result.setdefault("tool_elapsed_ms", int((time.monotonic() - tool_started) * 1000))
                        result.setdefault("tool_sequence", tool_sequence)
                    completion_evidence.append({"tool": name, "result": result})
                    if isinstance(result, dict) and result.get("status") == "failed":
                        round_failed = True
                        failure_reason = str(result.get("error") or result.get("reason") or "未知原因")
                        tool_failures.append({"tool": name, "reason": failure_reason[:500]})
                        if status: status(f"步骤失败：{name}：{failure_reason[:120]}；正在判断重试方案…")
                    elif isinstance(result, dict) and result.get("status") == "uncertain":
                        round_failed = True
                        uncertain_reason = str(result.get("warning") or result.get("next_action") or "操作后画面没有明显变化")
                        tool_failures.append({"tool": name, "reason": uncertain_reason[:500]})
                        if status: status(f"操作尚未确认：{uncertain_reason[:120]}；正在重新识别或更换操作方式…")
                    if name == "ui_list_windows" and not initial_media_state_checked and isinstance(result, dict):
                        initial_media_state_checked = True
                        observation = result.get("observation")
                        query = str(task_plan.get("query") or "").strip().lower()
                        if isinstance(observation, list) and query:
                            media_target_preexisting = any(
                                str(item.get("process_name", "")).lower() == "cloudmusic.exe"
                                and query in str(item.get("title", "")).lower()
                                for item in observation if isinstance(item, dict)
                            )
                            if media_target_preexisting:
                                self.log_event("media_target_preexisting", query=query)
                    if name in {"ui_click_window", "ui_double_click_window"} and isinstance(result, dict):
                        observation = result.get("observation")
                        after_title = str(observation.get("after_title", "")) if isinstance(observation, dict) else ""
                        title_changed = bool(observation.get("title_changed")) if isinstance(observation, dict) else False
                        query = str(task_plan.get("query") or "").strip()
                        if query and title_changed and query.lower() in after_title.lower():
                            automated_media_target_verified = True
                            self.log_event("automated_media_target_verified", query=query, after_title=after_title, tool=name)
                    if name == "cloudmusic_search_and_play" and isinstance(result, dict):
                        query = str(task_plan.get("query") or "").strip()
                        title = str(result.get("title") or "")
                        if result.get("ok") and query and query.casefold() in title.casefold() and result.get("used_local_tools") is True:
                            automated_media_target_verified = True
                            cloudmusic_verified_result = result
                            self.log_event("automated_media_target_verified", query=query, after_title=title, tool=name, attempts=result.get("attempts"), strategy=result.get("strategy"))
                    if name == "bilibili_open_favorite_video" and isinstance(result, dict):
                        expected_index = int(task_plan.get("index") or 1)
                        actual_index = int(result.get("favorite_index") or 0)
                        bilibili_favorite_verified = bool(
                            result.get("ok")
                            and result.get("used_existing_browser") is True
                            and actual_index == expected_index
                            and re.fullmatch(r"BV[0-9A-Za-z]+", str(result.get("bvid", "")))
                            and str(result.get("url", "")).lower().find(str(result.get("bvid", "")).lower()) >= 0
                        )
                        if bilibili_favorite_verified:
                            bilibili_favorite_result = result
                            self.log_event("bilibili_favorite_verified", folder=result.get("favorite_folder"), index=actual_index, bvid=result.get("bvid"), browser_pid=result.get("browser_pid"))
                    if name == "create_scheduled_task" and isinstance(result, dict) and result.get("ok"):
                        created_task_result = result
                    if name == "sing_song" and isinstance(result, dict) and result.get("ok"):
                        singing_performed = True
                    if name == "long_term_memory" and str(args.get("action", "")) == "store" and isinstance(result, dict) and result.get("ok"):
                        long_term_stored = True
                    if name == "code_validate_project" and isinstance(result, dict) and result.get("ok") is True:
                        local_code_verified = True
                        if self.current_code_self_edit:
                            self.current_code_verified = True
                    if code_task:
                        if name in {"code_write_file", "code_replace_text"} and isinstance(result, dict) and result.get("ok"):
                            code_inspection_iterations = 0
                        elif name in {"code_list_files", "code_read_file", "code_search_text"}:
                            code_inspection_iterations += 1
                            if code_inspection_iterations == 8:
                                post_tool_instruction = "已经连续8次只读检查而没有编辑。请使用搜索结果的行号调用 code_read_file(start_line=行号附近)，随后立即 code_replace_text/code_write_file；禁止再次读取文件开头。"
                            elif code_inspection_iterations >= 12:
                                round_failed = True
                                failure_reason = "本地代码工具连续12次只读检查且没有产生编辑，已停止无进展循环"
                                tool_failures.append({"tool": name, "reason": failure_reason})
                                post_tool_instruction = failure_reason + "。必须立即编辑并验证，或结束本地路径交给后备执行器。"
                    self.log_event("tool_completed", tool=name, result=result)
                    result_state = str(result.get("status") or "success") if isinstance(result, dict) else "success"
                    self._emit_activity(status, {
                        "type": "tool_failed" if result_state in {"failed", "uncertain"} else "tool_complete",
                        "title": f"{display_name} · {'失败' if result_state == 'failed' else '待确认' if result_state == 'uncertain' else '完成'}",
                        "detail": self._tool_activity_result(name, result), "tool": name, "state": result_state,
                    }, f"已完成：{display_name}")
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
                    if post_tool_instruction:
                        messages.append({"role": "system", "content": post_tool_instruction})
                if round_failed:
                    failed_rounds += 1
        failure_summary = "；".join(f"{row['tool']}：{row['reason']}" for row in tool_failures[-6:])
        rounds = failed_rounds
        iteration_limit_reached = round_index + 1 >= max_tool_iterations and failed_rounds < max_failed_rounds
        last_reason = tool_failures[-1]["reason"] if tool_failures else "执行过程中始终没有取得可验证的完成证据"
        if code_task and self.config.get("agent", {}).get("prefer_local_code_tools", True) and self.config.get("agent", {}).get("codex_code_fallback", True) and self._codex_config().get("enabled", False):
            if status: status("本地代码工具未能完成验证，正在低优先级尝试 Codex 后备…")
            self.log_event("local_code_fallback_to_codex", reason=last_reason, failed_rounds=rounds, iterations=round_index + 1)
            result = await self._run_codex_task(text, require_mcp=False, status=status, task_plan=task_plan, previous_failure=f"HomeAgent 本地代码工具累计失败 {rounds} 轮后仍未完成：{failure_summary or last_reason}")
            if result.get("error"):
                answer = f"本地代码工具未能完成验证，Codex 后备也失败了：{result['error']}"
            else:
                if self.current_code_self_edit:
                    self.current_code_verified = True
                answer = str(result.get("answer", "代码任务已通过后备执行器完成。"))
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            self._publish_answer(answer, answer_ready)
            if self.config["home"].get("auto_speak", True):
                await self._speak_with_fresh_session(answer, status, ignore_cancel=True)
            return answer
        limit_reason = f"达到总迭代安全上限 {max_tool_iterations} 次" if iteration_limit_reached else f"累计失败 {rounds} 轮"
        answer = f"这次任务在{limit_reason}后仍未完成，我已经停下来了。最后失败原因是：{last_reason[:180]}。没有把未验证的操作当作成功。"
        self.log_event("tool_round_limit_reached", failed_rounds=rounds, iterations=round_index + 1, max_failed_rounds=max_failed_rounds, max_tool_iterations=max_tool_iterations, failures=tool_failures, failure_summary=failure_summary, answer=answer)
        if status: status(f"任务失败：{last_reason[:120]}")
        self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
        self._publish_answer(answer, answer_ready)
        if self.config["home"].get("auto_speak", True):
            await self._speak_with_fresh_session(answer, status, ignore_cancel=True)
        return answer

    async def _run_tool(self, name: str, args: dict[str, Any], confirm=None, status=None) -> Any:
        if name == "media_stop" and not self._is_media_stop_plan(getattr(self, "current_task_plan", {})):
            self.log_event("tool_scope_rejected", tool=name, task_plan=getattr(self, "current_task_plan", {}), arguments=args)
            return {
                "error": "当前任务计划未授权停止媒体；未执行任何媒体操作",
                "executed": False,
                "blocked_by": "task_plan_scope",
            }
        if name.startswith("code_"):
            editor = self.self_upgrade.code_editor
            self_edit = bool(getattr(self, "current_code_self_edit", False))
            try:
                if name == "code_list_files":
                    default_path = "HomeAgent" if self_edit else "Projects"
                    return await asyncio.to_thread(editor.list_files, str(args.get("path") or default_path), self_edit, int(args.get("limit", 300)))
                if name == "code_read_file":
                    return await asyncio.to_thread(
                        editor.read_file, str(args.get("path", "")), self_edit,
                        int(args.get("max_chars", 30000)), int(args.get("start_line", 1)),
                        int(args.get("max_lines", 500)),
                    )
                if name == "code_search_text":
                    default_path = "HomeAgent" if self_edit else "Projects"
                    return await asyncio.to_thread(editor.search_text, str(args.get("query", "")), str(args.get("path") or default_path), self_edit, int(args.get("limit", 100)))
                if name == "code_write_file":
                    return await asyncio.to_thread(editor.write_file, str(args.get("path", "")), str(args.get("content", "")), self_edit)
                if name == "code_replace_text":
                    return await asyncio.to_thread(editor.replace_text, str(args.get("path", "")), str(args.get("old", "")), str(args.get("new", "")), self_edit, int(args.get("count", 1)))
                if name == "code_validate_project":
                    validation = await asyncio.to_thread(editor.validate_current_changes, True)
                    if not validation.get("ok"):
                        return {"error": validation.get("error", "代码文件校验失败"), "validation": validation}
                    tests = await asyncio.to_thread(editor.run_autonomous_tests, validation.get("changed", []))
                    if not tests.get("ok"):
                        return {"error": tests.get("error", "自动测试失败"), "validation": validation, "tests": tests}
                    return {"ok": True, "validation": validation, "tests": tests, "changed": validation.get("changed", [])}
            except (OSError, ValueError, UnicodeError) as exc:
                return {"error": str(exc)}
        if name == "cloudmusic_search_and_play":
            plan = getattr(self, "current_task_plan", {})
            planned_query = str(plan.get("query") or "").strip()
            requested_query = str(args.get("query") or "").strip()
            authorized = bool(
                plan.get("is_task")
                and plan.get("actionable")
                and plan.get("site") == "cloudmusic"
                and plan.get("handler") == "cloudmusic_search"
                and planned_query
            )
            if not authorized:
                self.log_event("tool_scope_rejected", tool=name, task_plan=plan, arguments=args)
                return {
                    "error": "当前任务计划未授权网易云搜索；未执行任何搜索或播放操作",
                    "executed": False,
                    "blocked_by": "task_plan_scope",
                }
            if requested_query and requested_query != planned_query:
                self.log_event("tool_argument_scope_rejected", tool=name, planned_query=planned_query, requested_query=requested_query)
                return {
                    "error": "工具参数与任务计划中的明确搜索对象不一致；未执行",
                    "executed": False,
                    "blocked_by": "task_plan_query",
                }
            query = planned_query
            return await self._run_cloudmusic_search_and_play(query, status)
        if name == "process_status":
            process_name = str(args.get("name") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_. -]{1,120}(?:\.exe)?", process_name):
                return {"error": "进程名格式无效"}
            if not process_name.lower().endswith(".exe"):
                process_name += ".exe"
            completed = await asyncio.to_thread(
                subprocess.run,
                ["tasklist", "/fo", "csv", "/nh", "/fi", f"imagename eq {process_name}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                check=False,
            )
            stdout = CommandExecutor._decode(completed.stdout)
            rows = []
            for row in csv.reader(stdout.splitlines()):
                if row and row[0].casefold() == process_name.casefold():
                    rows.append({"name": row[0], "pid": int(row[1]) if len(row) > 1 and row[1].isdigit() else None})
            return {"ok": True, "process_name": process_name, "running": bool(rows), "processes": rows, "observed_at": _iso_now()}
        if name == "bilibili_open_favorite_video":
            plan = getattr(self, "current_task_plan", {})
            folder_name = str(args.get("favorite_folder") or plan.get("favorite_folder") or "默认收藏夹").strip()
            try:
                index = int(args.get("index") or plan.get("index") or 1)
            except (TypeError, ValueError):
                return {"error": "收藏夹视频序号必须是正整数"}
            if index < 1:
                return {"error": "收藏夹视频序号必须从1开始"}
            return await self._run_existing_browser_favorites(index, None, folder_name)
        if name == "long_term_memory":
            action = str(args.get("action", "")).strip().lower()
            try:
                if action == "store":
                    record = self.long_term_memory.store(
                        tags=args.get("tags") or [], summary=str(args.get("summary", "")), detail=str(args.get("detail", "")),
                        category=str(args.get("category", "")), importance=int(args.get("importance", 80)),
                        user_id="owner", scene="home", privacy="private", source="home-agent",
                    )
                    if not record.get("duplicate"):
                        self.workspace.remember({
                            "type": "long_term_index", "user": "主人", "user_id": "owner", "privacy": "private",
                            "tags": record["tags"], "content": record["summary"], "db_id": record["id"],
                            "category": record["category"], "importance": record["importance"], "source": "long-term-index",
                        })
                    self.log_event("long_term_memory_stored", db_id=record.get("id"), tags=record.get("tags"), summary=record.get("summary"), duplicate=record.get("duplicate", False))
                    return {"ok": True, "action": "store", "record_id": record["id"], "tags": record["tags"], "summary": record["summary"], "duplicate": record.get("duplicate", False)}
                if action == "retrieve":
                    matches = self.long_term_memory.retrieve(args.get("query_tags") or [], limit=8, user_id="owner")
                    self.log_event("long_term_memory_retrieved", query_tags=args.get("query_tags") or [], matches=len(matches))
                    return {"ok": True, "action": "retrieve", "query_tags": args.get("query_tags") or [], "matches": matches}
                return {"error": "action 必须是 store 或 retrieve"}
            except (TypeError, ValueError, OSError) as exc:
                self.log_event("long_term_memory_rejected", action=action, error=str(exc))
                return {"error": str(exc), "action": action}
        if name == "create_scheduled_task":
            try:
                task = self.task_store.create(
                    title=str(args.get("title", "")), message=str(args.get("message", "")),
                    recurrence=str(args.get("recurrence", "once")), scheduled_at=str(args.get("scheduled_at", "")),
                    at_time=str(args.get("time", "")), weekdays=args.get("weekdays"), action=str(args.get("action", "tts")),
                )
                self.log_event("scheduled_task_created", task=task)
                active_count = len(self.task_store.list())
                return {"ok": True, "task": task, "active_count": active_count, "state_source": str(ROOT / "Task")}
            except (TypeError, ValueError, OSError) as exc:
                return {"error": str(exc)}
        if name == "list_scheduled_tasks":
            tasks = self.task_store.list()
            return {"ok": True, "active_count": len(tasks), "tasks": tasks, "state_source": str(ROOT / "Task")}
        if name == "delete_scheduled_task":
            try: deleted = self.task_store.delete(str(args.get("task_id", "")))
            except (ValueError, OSError) as exc: return {"error": str(exc)}
            return {"ok": deleted, "deleted": str(args.get("task_id", "")) if deleted else None, "active_count": len(self.task_store.list())}
        if name == "acknowledge_scheduled_task":
            outcome = self.task_store.acknowledge(str(args.get("task_id", "")), str(args.get("response", "")))
            if outcome: self.log_event("scheduled_task_acknowledged", outcome=outcome)
            return {"ok": bool(outcome), "outcome": outcome, "active_count": len(self.task_store.list()), "message": "已停止本轮重复提醒" if outcome else "当前没有等待确认的提醒"}
        if name == "ui_analyze_screen":
            question = str(args.get("question") or "").strip()
            if not question:
                return {"error": "屏幕分析问题不能为空"}
            return await self.analyze_current_screen(question)
        if name == "sing_song":
            singing = self.project.get("singing", {})
            if not singing.get("enabled", True): return {"error": "歌词朗读技能已禁用"}
            lines = [line.strip() for line in str(args.get("lyrics", "")).splitlines() if line.strip()][:10]
            if not lines: return {"error": "歌词内容不能为空"}
            if str(singing.get("mode", "local_tts")).lower() == "local_tts":
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=int(self.project.get("tts", {}).get("timeout_seconds", 60)) + 10)) as session:
                    path = await TTSClient(session, self.project["tts"], ROOT / "audio").speak("\n".join(lines))
                return {"ok": True, "backend": "local_tts", "path": str(path) if path else None, "lines": len(lines), "speaker": self.project.get("tts", {}).get("speaker", "default"), "played": bool(path and self.project.get("tts", {}).get("play_audio", True))}
            if not singing.get("mimo_fallback_enabled", False):
                return {"error": "MiMo 备用唱歌分支当前已停用，请将 singing.mode 改回 local_tts"}
            script = ROOT / "Skill" / "sing-with-mimo" / "scripts" / "sing_mimo.py"
            command = [sys.executable, str(script), "--song", str(args.get("song", "")), "--lyrics", str(args.get("lyrics", "")), "--style", str(args.get("style", ""))]
            if str(args.get("voice", "")).strip(): command += ["--voice", str(args["voice"])]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=creationflags)
            out, err = await proc.communicate()
            output = out.decode("utf-8", "replace").strip(); error = err.decode("utf-8", "replace").strip()
            try: result = json.loads(output.splitlines()[-1])
            except (json.JSONDecodeError, IndexError): result = {"ok": False, "error": error or output[-1000:] or "唱歌脚本没有返回结果"}
            return result
        if name == "ui_analyze_window":
            if not self.config.get("vision_mcp", {}).get("enabled", False):
                return {"error": "窗口分析服务未启用"}
            title = str(args.get("title_contains", "")).strip()
            question = str(args.get("question", "")).strip()
            if not title or not question:
                return {"error": "窗口标题和观察问题不能为空"}
            script = ROOT / "Vision" / "analyze_window.py"
            vision_python = ROOT / ".venv" / "Scripts" / "python.exe"
            python_executable = str(vision_python if vision_python.exists() else Path(sys.executable))
            analysis_env = os.environ.copy(); analysis_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
            request_submitted_at = _iso_now()
            proc = await asyncio.create_subprocess_exec(
                python_executable, str(script), "--title", title, "--prompt", question,
                "--request-submitted-at", request_submitted_at,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, env=analysis_env,
            )
            out, err = await proc.communicate()
            try: result = json.loads(out.decode("utf-8", "replace").splitlines()[-1])
            except (json.JSONDecodeError, IndexError): result = {"ok": False, "error": err.decode("utf-8", "replace")[-800:] or out.decode("utf-8", "replace")[-800:]}
            return result
        vision_tool_map = {
            "ui_inspect_target": ("inspect_active_target", lambda a: {}),
            "ui_list_windows": ("list_windows", lambda a: {"title_contains": str(a.get("title_contains", ""))}),
            "ui_activate_window": ("activate_window", lambda a: {"title_contains": str(a.get("title_contains", ""))}),
            "ui_type_window": ("window_type_text", lambda a: {"title_contains": str(a.get("title_contains", "")), "instruction": str(a.get("target", "")), "text": str(a.get("text", ""))}),
            "ui_click_window": ("window_click", lambda a: {"title_contains": str(a.get("title_contains", "")), "instruction": str(a.get("target", "")), "topk": 3, "idx": max(0, min(2, int(a.get("candidate_index", 0))))}),
            "ui_double_click_window": ("window_double_click", lambda a: {"title_contains": str(a.get("title_contains", "")), "instruction": str(a.get("target", "")), "topk": 3, "idx": max(0, min(2, int(a.get("candidate_index", 0))))}),
            "ui_hotkey": ("desktop_hotkey", lambda a: {"keys": [str(key) for key in a.get("keys", [])]}),
            "media_stop": ("desktop_media_stop", lambda a: {}),
            "ui_type_active_text": ("desktop_type_active_text", lambda a: {"text": str(a.get("text", "")), "clear": bool(a.get("clear", True))}),
            "web_read": ("web_read", lambda a: {"max_chars": max(1000, min(30000, int(a.get("max_chars", 12000))))}),
            "web_navigate": ("navigate", lambda a: {"url": str(a.get("url", ""))}),
            "web_fill": ("web_fill", lambda a: {"field": str(a.get("field", "")), "text": str(a.get("text", "")), "submit": bool(a.get("submit", False))}),
            "web_click_text": ("web_click_text", lambda a: {"text": str(a.get("text", "")), "exact": bool(a.get("exact", False))}),
            "web_play_media": ("web_play_media", lambda a: {}),
            "web_get_url": ("get_url", lambda a: {}),
        }
        if name in vision_tool_map:
            tool_name, build_arguments = vision_tool_map[name]
            arguments = build_arguments(args)
            plan = getattr(self, "current_task_plan", {})
            if name == "ui_hotkey" and self._is_media_stop_plan(plan):
                keys = {str(key).strip().lower() for key in arguments.get("keys", [])}
                if "space" in keys or ({"alt", "f4"} <= keys):
                    return {
                        "error": "媒体停止任务禁止使用可反转的 Space 或关闭应用的 Alt+F4；请调用 media_stop",
                        "executed": False, "executed_tool": tool_name,
                    }
            if not self.config.get("vision_mcp", {}).get("enabled", False):
                return {"error": "网页/界面执行服务未启用"}
            if not await asyncio.to_thread(self.ensure_vision_service, True):
                return {"error": "网页/界面执行服务未就绪"}
            if name == "web_navigate" and not str(arguments.get("url", "")).lower().startswith(("http://", "https://")):
                return {"error": "只允许导航到 HTTP/HTTPS 地址"}
            try:
                vision_submitted_at = _iso_now()
                vision_started = time.monotonic()
                observation_raw = await self._vision_mcp_call(tool_name, arguments)
                vision_completed_at = _iso_now()
                try:
                    observation = ast.literal_eval(observation_raw)
                except (ValueError, SyntaxError):
                    observation = observation_raw
                result = {
                    "ok": True, "observation": observation, "executed_tool": tool_name,
                    "vision_request_submitted_at": vision_submitted_at,
                    "vision_response_completed_at": vision_completed_at,
                    "vision_elapsed_ms": int((time.monotonic() - vision_started) * 1000),
                }
                if isinstance(observation, dict) and "state_changed" in observation:
                    changed = bool(observation.get("state_changed"))
                    result.update({"post_action_verified": changed, "status": "success" if changed else "uncertain", "next_action": observation.get("next_action")})
                    if not changed:
                        result["warning"] = "操作已发送，但等待后重新截图未观察到明显状态变化"
                return result
            except Exception as exc:
                return {"error": str(exc), "executed_tool": tool_name}
        if name == "codex_cli_task":
            return await self._run_codex_task(str(args.get("task", "")))
        if name == "mcp_task":
            return await self._run_codex_task(str(args.get("task", "")), require_mcp=True)
        if name == "vision_gui_task":
            preferred = str(self.config.get("vision_mcp", {}).get("server_name", "vision-gui"))
            return await self._run_codex_task(str(args.get("task", "")), require_mcp=True, preferred_mcp=preferred)
        if name == "list_mcp_servers":
            return await self.codex_status()
        if name == "search_memories":
            q = str(args.get("query", "")).lower()
            shared = self.workspace.recent_memories(500) + self.workspace.recent_live_conversations(200)
            return [x for x in shared if q in json.dumps(x, ensure_ascii=False).lower()][-20:]
        if name == "list_skills": return self.list_skills()
        if name == "list_character_images":
            return self._character_image_catalog()
        if name == "generate_character_image":
            if not self.config["agent"].get("allow_character_image_skill", True): return {"error": "角色图像技能已禁用"}
            script = project_path(self.config["agent"].get("skill_root", "Skill")) / "ai-live-character-image" / "scripts" / "character_image_api.py"
            cmd = [sys.executable, str(script), "--prompt", str(args.get("prompt", "")), "--operation", str(args.get("operation", "generate")), "--label", str(args.get("label", "家庭Agent生成")), "--tags", str(args.get("tags", "AI生成"))]
            if args.get("reference"): cmd += ["--reference", str(args["reference"])]
            if args.get("set_primary"): cmd.append("--set-primary")
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            if proc.returncode: return {"error": err.decode("utf-8", "replace")[-800:] or out.decode("utf-8", "replace")[-800:]}
            try: return json.loads(out.decode("utf-8").splitlines()[-1])
            except Exception: return {"output": out.decode("utf-8", "replace")[-1000:]}
        if name == "analyze_image":
            image_value = str(args.get("image") or "").strip()
            registered_path = self._resolve_character_image(image_value)
            path = registered_path if registered_path is not None else self._allowed_path(image_value)
            if not path.is_file():
                return {"status": "failed", "error": f"图片不存在：{path}"}
            try:
                timeout = aiohttp.ClientTimeout(total=int(self.mimo_multimodal.config.get("timeout_seconds", 60)))
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    return await self.mimo_multimodal.analyze_image(session, path, str(args.get("prompt") or "请描述图片内容"))
            except Exception as exc:
                return {"status": "failed", "error": str(exc)}
        if name == "mimo_transcribe_audio":
            path = self._allowed_path(str(args.get("audio") or ""))
            try:
                timeout = aiohttp.ClientTimeout(total=int(self.mimo_multimodal.config.get("timeout_seconds", 60)))
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    return await self.mimo_multimodal.transcribe_audio(session, path, str(args.get("language") or self.mimo_multimodal.config.get("speech_language", "auto")))
            except Exception as exc:
                return {"status": "failed", "error": str(exc)}
        if name in {"web_agent_task", "web_agent_operator", "web-agent-operator"}:
            task = str(args.get("task", "")).strip()
            if not task: return {"error": "网页任务内容为空"}
            preferred = str(self.config.get("vision_mcp", {}).get("server_name", "vision-gui"))
            return await self._run_codex_task(task, require_mcp=True, preferred_mcp=preferred)
        if name == "list_directory":
            path = self._allowed_path(args.get("path"));
            if not path.is_dir(): return {"error": "目录不存在"}
            return [{"name": p.name, "type": "directory" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else None} for p in sorted(path.iterdir())[:200]]
        if name == "read_text_file":
            path = self._allowed_path(args.get("path")); blocked = {".env", ".pem", ".key", ".pfx"}
            if path.name.lower() == ".env" or path.suffix.lower() in blocked: return {"error": "该文件可能包含密钥，禁止读取"}
            if not path.is_file(): return {"error": "文件不存在"}
            if path.stat().st_size > 1024 * 1024: return {"error": "文本文件超过 1MB"}
            try:
                content, encoding = _read_compatible_text(path)
                return {"path": str(path), "content": content[:30000], "encoding": encoding}
            except (UnicodeDecodeError, LookupError):
                return {"error": "不是支持的文本文件（支持 UTF-8、UTF-16 和 GB18030）"}
        if name == "write_text_file":
            path = self._allowed_path(args.get("path")); content = str(args.get("content", ""))
            blocked = {".env", ".pem", ".key", ".pfx"}
            if path.name.lower() == ".env" or path.suffix.lower() in blocked: return {"error": "禁止写入密钥文件"}
            if len(content.encode("utf-8")) > 1024 * 1024: return {"error": "写入内容超过 1MB"}
            if not path.parent.is_dir(): return {"error": "目标目录不存在"}
            if self.config.get("computer_control", {}).get("confirm_before_action", True) and not await self._confirm_control(f"写入文件：{path}", confirm): return {"cancelled": True}
            temporary = path.with_name(f".{path.name}.home-agent.tmp")
            temporary.write_text(content, encoding="utf-8", newline="\r\n" if path.suffix.lower() in {".bat", ".cmd"} else "\n")
            temporary.replace(path)
            return {"ok": True, "path": str(path), "bytes": path.stat().st_size, "status": "success", "evidence": {"path": str(path)}}
        if name in {"run_shell", "run_cmd"}:
            cfg = self.config.get("shell_execution", {})
            kind = "shell" if name == "run_shell" else "cmd"
            if not cfg.get(f"{kind}_enabled", True): return {"error": f"{kind} 执行功能未启用"}
            cwd_value = str(args.get("cwd") or ROOT)
            cwd = self._allowed_path(cwd_value)
            if not cwd.is_dir(): return {"error": f"工作目录不存在：{cwd}"}
            command = str(args.get("command") or "").strip()
            plan = getattr(self, "current_task_plan", {})
            termination = bool(re.search(r"(?i)\b(stop-process|taskkill|kill|terminate-process)\b", command))
            media_process = bool(re.search(r"(?i)\b(cloudmusic|netease|spotify|music)\b", command))
            if self._is_media_stop_plan(plan) and termination and media_process and not self._allows_application_termination(plan):
                return {
                    "error": "用户要求停止媒体播放，不是退出应用；已阻止终止音乐进程，请调用 media_stop",
                    "executed": False, "blocked_by": "media_process_termination_guard",
                }
            if cfg.get("confirm_before_execute", False) and not await self._confirm_control(f"使用 {kind} 执行命令：{command[:200]}", confirm): return {"cancelled": True}
            executor = getattr(self, "command_executor", None) or CommandExecutor(ROOT)
            return await asyncio.to_thread(
                executor.execute, kind, command, cwd=cwd,
                timeout_seconds=int(args.get("timeout_seconds") or cfg.get("timeout_seconds", 60)),
                max_output_chars=int(cfg.get("max_output_chars", 20000)),
            )
        if name == "open_path":
            path = self._allowed_path(args.get("path"))
            if not path.exists(): return {"error": "路径不存在"}
            if not self.config.get("computer_control", {}).get("full_access", False) and path.is_file() and path.suffix.lower() in {".exe", ".bat", ".cmd", ".ps1", ".com", ".msi"}: return {"error": "可执行文件只能通过应用白名单启动"}
            if not await self._confirm_control(f"打开路径：{path}", confirm): return {"cancelled": True}
            os.startfile(str(path)); return {"ok": True, "opened": str(path)}
        if name == "open_url":
            url = str(args.get("url", "")).strip()
            if not url.lower().startswith(("http://", "https://")): return {"error": "只允许 HTTP/HTTPS 地址"}
            if not await self._confirm_control(f"打开网页：{url}", confirm): return {"cancelled": True}
            webbrowser.open(url); return {"ok": True, "opened": url}
        if name == "launch_app":
            app_name = str(args.get("name", "")); apps = self.config.get("computer_control", {}).get("applications", {})
            task_policy = str(getattr(self, "current_task_plan", {}).get("browser_policy", ""))
            browser_names = ("chrome", "chromium", "msedge", "microsoft edge", "edge", "firefox", "浏览器")
            if task_policy == "existing_profile_only" and any(token in app_name.lower() for token in browser_names):
                return {"error": "当前任务只允许接管现有浏览器登录会话，禁止启动新的浏览器实例"}
            command = apps.get(app_name)
            full = self.config.get("computer_control", {}).get("full_access", False)
            if not command and full:
                candidate = Path(app_name).expanduser()
                command = str(candidate.resolve()) if candidate.is_file() else __import__("shutil").which(app_name)
            target = Path(command) if command else None
            if not target or not target.exists(): return {"error": f"找不到应用或目录，可用软件映射：{list(apps)}，或提供可执行文件绝对路径"}
            if self.config.get("computer_control", {}).get("confirm_launch_app", False) and not await self._confirm_control(f"启动应用：{app_name}", confirm): return {"cancelled": True}
            arguments = [str(x) for x in args.get("arguments", [])][:30]
            if target.is_dir():
                await asyncio.to_thread(os.startfile, str(target)); return {"ok": True, "application": app_name, "opened_directory": str(target)}
            await asyncio.create_subprocess_exec(str(command), *arguments); return {"ok": True, "application": app_name, "arguments": arguments}
        return {"error": f"未知工具: {name}"}

    async def run_due_tasks(self, notification_ready=None) -> list[dict[str, Any]]:
        """由桌宠常驻轮询器调用；任务只有在 TTS 成功后才算执行成功。"""
        results = []
        maintenance = await self.run_context_maintenance()
        if maintenance is not None: results.append({"context_maintenance": maintenance})
        for task in self.task_store.claim_due():
            success = False; error = ""
            try:
                if task.get("action", "tts") != "tts": raise ValueError(f"不支持的任务动作：{task.get('action')}")
                attempt = int(task.get("reminder_attempts", 0)) + 1
                base = str(task.get("message", "提醒时间到了")).strip()
                if attempt == 1: spoken = f"{base}。主人，听到后记得回应我一声哦。"
                elif attempt == 2: spoken = f"主人，我还没有收到你的回应。再提醒一次：{base}。"
                else: spoken = f"主人，这是最后一次提醒：{base}。"
                # Publish the reminder before synthesis/playback. UI delivery must
                # not be delayed or lost when TTS is slow, busy, or unavailable.
                if notification_ready:
                    notification_ready(spoken)
                async with aiohttp.ClientSession() as session:
                    paths = await self._speak_home(session, spoken)
                if not paths: raise RuntimeError("TTS 没有生成可播放音频")
                success = True
            except Exception as exc:
                error = str(exc); self.log_event("scheduled_task_failed", task_id=task.get("id"), error=error)
            outcome = self.task_store.finish(str(task["id"]), success, error)
            self.log_event("scheduled_task_finished", task_id=task.get("id"), success=success, outcome=outcome)
            results.append({"task_id": task.get("id"), "success": success, "error": error, "outcome": outcome})
        return results

    def _maintenance_state_path(self) -> Path:
        path = HOME_AGENT / "state" / "context-maintenance.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _write_maintenance_state(self, state: dict[str, Any]) -> None:
        path = self._maintenance_state_path(); temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _next_maintenance_time(self, now: datetime, following_day: bool = False) -> datetime:
        value = str(self.config.get("context_maintenance", {}).get("time", "03:00"))
        clock = datetime.strptime(value, "%H:%M").time()
        target = datetime.combine(now.date(), clock)
        if following_day or target <= now: target += timedelta(days=1)
        return target

    @staticmethod
    def _cleanup_work_directory(cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Delete only expired files inside the explicitly configured temporary work root."""
        if not cfg.get("cleanup_work_directory", True):
            return {"enabled": False, "scanned": 0, "removed": 0, "freed_bytes": 0}
        configured = Path(str(cfg.get("work_directory", "work")))
        work_root = configured if configured.is_absolute() else ROOT / configured
        work_root = work_root.resolve()
        allowed_root = (ROOT / "work").resolve()
        # Never let a configuration typo broaden deletion beyond the dedicated work tree.
        if work_root != allowed_root and allowed_root not in work_root.parents:
            raise ValueError(f"拒绝清理非 work 工作区：{work_root}")
        keep_days = max(1, int(cfg.get("work_retention_days", 3)))
        cutoff = now.timestamp() - keep_days * 86400
        scanned = removed = freed = skipped = 0
        if not work_root.exists():
            return {"enabled": True, "path": str(work_root), "retention_days": keep_days, "scanned": 0, "removed": 0, "freed_bytes": 0}
        directories: list[Path] = []
        for base, names, files in os.walk(work_root, topdown=True, followlinks=False):
            base_path = Path(base)
            names[:] = [name for name in names if not (base_path / name).is_symlink()]
            directories.extend(base_path / name for name in names)
            for name in files:
                path = base_path / name; scanned += 1
                try:
                    if path.is_symlink() or path.name == ".gitkeep" or path.stat().st_mtime >= cutoff:
                        skipped += 1; continue
                    size = path.stat().st_size
                    path.unlink(); removed += 1; freed += size
                except (FileNotFoundError, PermissionError, OSError):
                    skipped += 1
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            try: directory.rmdir()
            except OSError: pass
        return {"enabled": True, "path": str(work_root), "retention_days": keep_days, "scanned": scanned, "removed": removed, "skipped": skipped, "freed_bytes": freed}

    async def run_context_maintenance(self) -> dict[str, Any] | None:
        """每日压缩家庭上下文；错过或失败时保持到期状态并每分钟重试。"""
        cfg = self.config.get("context_maintenance", {})
        if not cfg.get("enabled", True): return None
        now = datetime.now().replace(microsecond=0)
        path = self._maintenance_state_path()
        try: state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError): state = {}
        if not state.get("next_run_at"):
            clock = datetime.strptime(str(cfg.get("time", "03:00")), "%H:%M").time()
            state["next_run_at"] = datetime.combine(now.date(), clock).isoformat(timespec="seconds")
        try: due = datetime.fromisoformat(str(state["next_run_at"])) <= now
        except ValueError: due = True
        if not due: return None
        state.update({"status": "running", "last_attempt_at": now.isoformat(timespec="seconds")})
        self._write_maintenance_state(state)
        snapshot = list(self.history)
        try:
            work_cleanup = await asyncio.to_thread(self._cleanup_work_directory, cfg, now)
            self.log_event("work_directory_cleanup_completed", result=work_cleanup)
        except Exception as exc:
            work_cleanup = {"ok": False, "error": str(exc)}
            self.log_event("work_directory_cleanup_failed", result=work_cleanup)
        try:
            summary_path = ROOT / cfg.get("summary_file", "workspace/HOME_CONTEXT_SUMMARY.md")
            previous = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
            summary = previous
            if snapshot:
                provider, key = self._provider(); llm_cfg = self.project["llm"]; tuning = llm_cfg.get("memory", {})
                prompt = (
                    "整理家庭Agent上下文。合并旧摘要和本次短期对话，只保留对未来有用的身份、关系、稳定偏好、承诺、未完成事项和重要事件；"
                    "彻底丢弃寒暄、玩笑、重复表达、临时闲聊和已经结束的小事。输出简洁中文Markdown摘要，不要解释过程。\n\n"
                    f"旧摘要：\n{previous or '无'}\n\n短期对话：\n{json.dumps(snapshot, ensure_ascii=False)}"
                )
                payload = {"model": provider["model"], "messages": [{"role": "user", "content": prompt}], "temperature": tuning.get("temperature", 0.2)}
                self._set_token_limit(payload, provider, int(tuning.get("max_tokens", 180)))
                timeout = aiohttp.ClientTimeout(total=llm_cfg.get("timeout_seconds", 45))
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(provider["base_url"].rstrip("/") + "/chat/completions", json=payload, headers=self._provider_headers(provider, key)) as response:
                        raw = await response.text()
                        if response.status >= 400: raise RuntimeError(f"上下文压缩 LLM HTTP {response.status}: {raw[:500]}")
                        summary = str(json.loads(raw)["choices"][0]["message"].get("content", "")).strip()
                if summary:
                    summary_path.parent.mkdir(parents=True, exist_ok=True)
                    summary_path.write_text(f"# 家庭上下文摘要\n\n更新时间：{now:%Y-%m-%d %H:%M:%S}\n\n{summary}\n", encoding="utf-8")
            cleanup = self.workspace.cleanup_home_chatter() if cfg.get("cleanup_home_chatter", True) else {"scanned": 0, "removed": 0}
            if self.history[:len(snapshot)] == snapshot: self.history = self.history[len(snapshot):]
            state.update({"status": "pending", "last_success_at": now.isoformat(timespec="seconds"), "last_error": None, "next_run_at": self._next_maintenance_time(now, following_day=True).isoformat(timespec="seconds")})
            self._write_maintenance_state(state)
            result = {"ok": True, "compressed_messages": len(snapshot), "remaining_messages": len(self.history), "memory_cleanup": cleanup, "work_cleanup": work_cleanup, "next_run_at": state["next_run_at"]}
            self.log_event("context_maintenance_completed", result=result)
            return result
        except Exception as exc:
            retry = now + timedelta(minutes=max(1, int(cfg.get("retry_minutes", 1))))
            state.update({"status": "retry", "last_error": str(exc)[:1000], "next_run_at": retry.isoformat(timespec="seconds")})
            self._write_maintenance_state(state)
            result = {"ok": False, "error": str(exc), "next_retry_at": state["next_run_at"]}
            self.log_event("context_maintenance_failed", result=result)
            return result

    def _acknowledge_common_response(self, text: str) -> dict[str, Any] | None:
        """常见短确认直接处理；复杂语义仍交给模型和确认工具判断。"""
        if not self.task_store.awaiting_acknowledgements(): return None
        value = re.sub(r"[\s，。！？!?、]", "", str(text)).lower()
        keywords = ("知道了", "好的", "收到", "明白了", "喝了", "吃了", "完成了", "做完了", "起来了", "起床了", "醒了", "马上", "这就去", "不用提醒", "别提醒了")
        if not any(word in value for word in keywords): return None
        outcome = self.task_store.acknowledge(response=text)
        if outcome: self.log_event("scheduled_task_auto_acknowledged", response=text, outcome=outcome)
        return outcome

    def _allowed_path(self, value) -> Path:
        path = Path(str(value or "")).expanduser().resolve()
        if self.config.get("computer_control", {}).get("full_access", False): return path
        roots = [Path(x).expanduser().resolve() for x in self.config.get("computer_control", {}).get("allowed_roots", [])]
        if not any(path == root or root in path.parents for root in roots): raise PermissionError(f"路径不在允许范围：{path}")
        return path

    @staticmethod
    def _character_image_catalog() -> dict[str, Any]:
        """Return registered character images with canonical paths for tool chaining."""
        image_root = (ROOT / "workspace" / "character_images").resolve()
        manifest = image_root / "manifest.json"
        if not manifest.exists():
            return {"primary": None, "primary_path": None, "images": []}
        data = json.loads(manifest.read_text(encoding="utf-8"))
        images: list[dict[str, Any]] = []
        primary_path: str | None = None
        primary_id = str(data.get("primary") or "")
        for raw_item in data.get("images", []):
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            path = (image_root / str(item.get("filename") or "")).resolve()
            item["path"] = str(path)
            item["exists"] = path.is_file()
            images.append(item)
            if str(item.get("id") or "") == primary_id:
                primary_path = str(path)
        return {"primary": data.get("primary"), "primary_path": primary_path, "images": images}

    @classmethod
    def _resolve_character_image(cls, value: str) -> Path | None:
        """Resolve a registered image by primary alias, id, filename or human label."""
        query = str(value or "").strip()
        if not query:
            return None
        catalog = cls._character_image_catalog()
        if query.casefold() == "primary":
            primary_path = catalog.get("primary_path")
            return Path(primary_path) if primary_path else None
        query_key = query.casefold()
        query_name = Path(query).name.casefold()
        for item in catalog.get("images", []):
            candidates = {
                str(item.get("id") or "").casefold(),
                str(item.get("filename") or "").casefold(),
                str(item.get("original_name") or "").casefold(),
                str(item.get("label") or "").casefold(),
                Path(str(item.get("filename") or "")).stem.casefold(),
            }
            if query_key in candidates or query_name in candidates:
                return Path(str(item["path"]))
        return None

    async def _confirm_control(self, description: str, confirm) -> bool:
        if not self.config.get("computer_control", {}).get("confirm_before_action", True): return True
        if confirm is None: return False
        return bool(await asyncio.to_thread(confirm, description))

    async def _maybe_remember_home(self, message: str, reply: str, session: aiohttp.ClientSession, provider: dict[str, Any], key: str) -> None:
        """主动工具未存储时，以严格分类器兜底写入 SQLite 长期记忆。"""
        cfg = self.project.get("memory_write", {}); mode = cfg.get("mode", "important")
        if mode == "off": return
        today = self.workspace.root / self.workspace.cfg.get("memory_dir", "memory") / f"{datetime.now():%Y-%m-%d}.jsonl"
        daily_count = 0
        if today.exists():
            try:
                for line in today.read_text(encoding="utf-8").splitlines():
                    item = json.loads(line); source = str(item.get("source", ""))
                    if source.startswith("auto") or source.startswith("home-auto") or source == "long-term-index": daily_count += 1
            except (OSError, json.JSONDecodeError): pass
        if daily_count >= int(cfg.get("max_daily_writes", 20)): return
        raw_user = self.config["home"].get("user_name", "主人")
        identity = self.workspace.resolve_user(raw_user)
        user = identity["name"]
        identity_fields = {"user_id": identity["id"], "source_username": raw_user}
        always = any(word and word in message for word in cfg.get("always_keywords", []))
        ignored = any(word and word in message for word in cfg.get("ignore_keywords", []))
        if ignored and not always: return
        if len(message.strip()) < int(cfg.get("min_message_length", 4)) and not always: return
        threshold = int(cfg.get("importance_threshold", 70))
        result = {"importance": 90 if always else 50, "should_remember": always, "category": "", "summary": "", "tags": [], "detail": message}
        if cfg.get("analyze_with_llm", True):
            prompt = (
                "你是严格的私人长期记忆筛选器。只有身体状况、明显情绪波动、重大事件、稳定偏好习惯、重要关系或明确约定值得存储。"
                "寒暄、天气、玩笑、测试、重复表达和临时闲聊必须拒绝，即使用户说了‘记住’也不能降低标准。"
                "只输出JSON对象：importance(0-100)、should_remember(布尔)、category(health/emotion/major_event/preference/habit/relationship/agreement)、"
                "tags(3-5个关键词)、summary(20字以内核心事实)、detail(保留用户原文关键句)。\n"
                f"用户：{user}\n稳定身份ID：{identity['id']}\n用户消息：{message}\nAI回复：{reply}"
            )
            try:
                tuning = self.project.get("llm", {}).get("memory", {})
                payload = {"model": provider["model"], "messages": [{"role": "system", "content": "只输出合法JSON，不要Markdown。"}, {"role": "user", "content": prompt}], "temperature": tuning.get("temperature", 0.2)}
                self._set_token_limit(payload, provider, int(tuning.get("max_tokens", 180)))
                async with session.post(provider["base_url"].rstrip("/") + "/chat/completions", json=payload, headers=self._provider_headers(provider, key)) as response:
                    raw = await response.text()
                    if response.status < 400:
                        content = json.loads(raw)["choices"][0]["message"].get("content", ""); match = re.search(r"\{.*\}", content, re.S)
                        if match:
                            parsed = json.loads(match.group(0))
                            if not isinstance(parsed, dict) or not isinstance(parsed.get("should_remember"), bool):
                                raise ValueError("长期记忆判断的 should_remember 必须是 JSON boolean")
                            if not isinstance(parsed.get("importance"), int) or isinstance(parsed.get("importance"), bool):
                                raise ValueError("长期记忆判断的 importance 必须是整数")
                            result.update(parsed)
            except Exception as exc:
                self.log_event("long_term_memory_classifier_error", error=str(exc), message=message)
        if always and not result.get("category"):
            category = "major_event" if "生日" in message else ("preference" if any(x in message for x in ("喜欢", "讨厌")) else "agreement")
            subject = "生日" if "生日" in message else ("偏好" if category == "preference" else "约定")
            result.update({"importance": 90, "should_remember": True, "category": category,
                           "tags": ["主人", subject, "明确记忆"], "summary": message.strip()[:20], "detail": message})
        result["category"] = {"identity": "relationship", "event": "major_event"}.get(str(result.get("category", "")), result.get("category", ""))
        score = max(0, min(100, int(result.get("importance", 0))))
        should = bool(result.get("should_remember")) and score >= threshold
        self.log_event("long_term_memory_classifier_decision", should_remember=should, importance=score, category=result.get("category"), tags=result.get("tags") or [])
        if not should: return
        try:
            record = self.long_term_memory.store(
                tags=result.get("tags") or [], summary=str(result.get("summary", "")).strip(), detail=str(result.get("detail") or message).strip(),
                category=str(result.get("category", "")), importance=score, user_id=identity["id"], scene="home", privacy="private", source="home-auto-classifier",
            )
        except (TypeError, ValueError, OSError) as exc:
            self.log_event("long_term_memory_classifier_rejected", error=str(exc), message=message)
            return
        if not record.get("duplicate"):
            self.workspace.remember({"type": "long_term_index", "user": user, **identity_fields, "privacy": "private", "tags": record["tags"], "content": record["summary"], "db_id": record["id"], "category": record["category"], "importance": record["importance"], "source": "long-term-index"})
        self.log_event("long_term_memory_classifier_stored", db_id=record.get("id"), tags=record.get("tags"), duplicate=record.get("duplicate", False))

    async def transcribe(self, wav_path: Path) -> str:
        cfg = self.config["stt"]; mode = cfg.get("mode", "api")
        if mode == "sound_mcp":
            return await self._sound_mcp_transcribe(wav_path)
        if mode == "mimo":
            timeout = aiohttp.ClientTimeout(total=int(self.mimo_multimodal.config.get("timeout_seconds", 60)))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                result = await self.mimo_multimodal.transcribe_audio(session, wav_path, str(cfg.get("language") or self.mimo_multimodal.config.get("speech_language", "auto")))
            return str(result.get("text") or "").strip()
        if mode == "faster_whisper":
            python = Path(cfg.get("local_python", "")); model = str(cfg.get("local_model", ""))
            if not python.exists() or not model: raise RuntimeError("请在 HomeAgent/config.yaml 设置 stt.local_python 和本地模型目录")
            helper = HOME_AGENT / "transcribe_local.py"
            proc = await asyncio.create_subprocess_exec(str(python), str(helper), model, str(wav_path), str(cfg.get("language", "zh")), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            if proc.returncode: raise RuntimeError(err.decode("utf-8", "replace")[-800:])
            return json.loads(out.decode("utf-8"))["text"].strip()
        url = str(cfg.get("api_url", "")).strip()
        if not url: raise RuntimeError("语音录制成功，但尚未配置 STT。请在 HomeAgent/config.yaml 填写 stt.api_url")
        key = os.getenv(cfg.get("api_key_env", "STT_API_KEY"), "").strip()
        form = aiohttp.FormData(); form.add_field("file", wav_path.read_bytes(), filename=wav_path.name, content_type="audio/wav")
        if cfg.get("model"): form.add_field("model", str(cfg["model"]))
        if cfg.get("language"): form.add_field("language", str(cfg["language"]))
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=cfg.get("timeout_seconds", 180))) as session:
            async with session.post(url, data=form, headers=headers) as response:
                raw = await response.text()
                if response.status >= 400: raise RuntimeError(f"STT HTTP {response.status}: {raw[:500]}")
                data = json.loads(raw)
        return str(data.get("text", "")).strip()
