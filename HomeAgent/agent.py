from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import quote
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from dotenv import dotenv_values

ROOT = Path(r"E:\Doc\AI直播")
HOME_AGENT = ROOT / "HomeAgent"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Skill" / "schedule-home-task" / "scripts"))

from src.ai_live_assistant.tts import TTSClient, cleanup_audio_files
from src.ai_live_assistant.workspace import Workspace
from src.ai_live_assistant.long_term_memory import LongTermMemoryStore
from task_manager import TaskStore


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
        threading.Thread(target=self.ensure_vision_service, daemon=True, name="vision-mcp-autostart").start()
        threading.Thread(target=self.ensure_sound_service, daemon=True, name="sound-mcp-autostart").start()
        self.character_name = "小助手"
        self.refresh_identity()
        self.log_event("long_term_memory_migration", result=migration, total=self.long_term_memory.count())

    def begin_task(self) -> None:
        self.cancel_event.clear()

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

    def _codex_environment(self, command: list[str]) -> dict[str, str]:
        env = os.environ.copy()
        node_dir = str(Path(command[0]).parent) if Path(command[0]).name.lower() == "node.exe" else ""
        if node_dir:
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        return env

    def _should_route_to_codex(self, text: str) -> bool:
        cfg = self._codex_config()
        if not cfg.get("enabled", False):
            return False
        mode = str(cfg.get("trigger_mode", "auto")).lower()
        if mode == "always":
            return True
        if mode != "auto":
            return False
        lowered = text.lower()
        return any(str(word).strip().lower() in lowered for word in cfg.get("trigger_keywords", []) if str(word).strip())

    def _should_route_to_vision(self, text: str) -> bool:
        cfg = self.config.get("vision_mcp", {})
        if not cfg.get("enabled", True) or not cfg.get("gui_enabled", True) or not self._codex_config().get("enabled", False): return False
        lowered = str(text).lower()
        return any(str(word).strip().lower() in lowered for word in cfg.get("trigger_keywords", []) if str(word).strip())

    @staticmethod
    def _is_multistep_web_request(text: str) -> bool:
        """Recognize outcome-oriented web requests that must not degrade to open_url."""
        value = str(text).lower()
        site_hint = any(word in value for word in (
            "网页", "网站", "浏览器", "http://", "https://", "b站", "bilibili", "哔哩哔哩",
            "百度", "淘宝", "天猫", "京东", "知乎", "小红书", "微博", "抖音", "github",
        ))
        action_hint = any(word in value for word in (
            "搜索", "搜一下", "搜一搜", "搜", "查找", "找一下", "找找", "找", "选择",
            "点开", "点击", "播放", "购买", "加入购物车", "填写", "提交", "登录",
        ))
        return site_hint and action_hint

    def _has_recent_web_context(self, text: str) -> bool:
        """Carry the active website into short follow-up commands such as '搜这个并播放'."""
        current = str(text).lower()
        if not any(word in current for word in (
            "搜索", "搜", "查找", "找", "选择", "点开", "点击", "播放", "购买", "加入购物车", "填写", "提交",
        )):
            return False
        recent = "\n".join(str(item.get("content", "")) for item in self.history[-5:])
        return self._is_multistep_web_request(f"{recent}\n{text}")

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
        log_dir = ROOT / "Vision" / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "vision-mcp.log").open("a", encoding="utf-8")
        env = os.environ.copy(); env.update({
            "VISION_MCP_TRANSPORT": "http", "VISION_MCP_HOST": host, "VISION_MCP_PORT": str(port),
            "VISION_PRELOAD_MODEL": "1" if cfg.get("preload_model", True) else "0",
            "GUI_ACTOR_MODEL": str(ROOT / "Vision" / "models" / "GUI-Actor-2B-Qwen2-VL"),
            "GUI_ACTOR_REPO": str(ROOT / "Vision" / "GUI-Actor"),
        })
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
        self.vision_service_process = subprocess.Popen([str(python), str(server)], cwd=str(server.parent), env=env, stdout=log, stderr=log, creationflags=flags)
        self.log_event("vision_service_started", pid=self.vision_service_process.pid, host=host, port=port, preload=cfg.get("preload_model", True))
        if not wait_until_ready: return True
        deadline = time.monotonic() + float(cfg.get("startup_timeout_seconds", 120))
        while time.monotonic() < deadline:
            if ready(): return True
            if self.vision_service_process.poll() is not None: return False
            time.sleep(1)
        return False

    async def codex_status(self) -> dict[str, Any]:
        """检查 CLI 版本和已配置 MCP 服务，不执行 Agent 任务。"""
        codex_command = self._codex_command()
        env = self._codex_environment(codex_command)
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
        }

    async def _vision_mcp_call(self, tool_name: str, arguments: dict[str, Any] | None = None):
        """Call persistent vision MCP through the project venv dependency bridge."""
        url = str(self.config.get("vision_mcp", {}).get("url", "http://127.0.0.1:8765/mcp"))
        python = ROOT / ".venv" / "Scripts" / "python.exe"
        helper = ROOT / "Vision" / "mcp_call.py"
        proc = await asyncio.create_subprocess_exec(str(python), str(helper), url, tool_name,
            json.dumps(arguments or {}, ensure_ascii=False), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
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
    def _visual_search_query(text: str) -> str:
        patterns = (
            r"(?:找找|找到|找一下|找|搜索|搜一下|搜|播放|听)\s*[《\"“]?([^《》\"”]+?)[》\"”]?(?:然后|并且|并|播放|的视频|$)",
            r"[《\"“]([^《》\"”]+)[》\"”]",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                query = match.group(1).strip(" ，,。.!！?？")
                query = re.sub(r"\s*(?:唱的|演唱的|的歌)\s*", " ", query).strip()
                query = re.sub(r"\s+的\s*", " ", query).strip()
                if query: return query[:80]
        return ""

    @staticmethod
    def _is_direct_visual_media_request(text: str) -> bool:
        lowered = text.lower()
        target = any(x in lowered for x in ("bilibili", "哔哩哔哩", "b站", "网易云", "cloudmusic"))
        return target and any(x in text for x in ("找", "搜", "播放", "视频", "听"))

    async def _run_direct_visual_media(self, text: str, status=None) -> dict[str, Any] | None:
        """Fast deterministic path for common Bilibili/CloudMusic search-and-play tasks."""
        lowered = text.lower(); is_bili = any(x in lowered for x in ("bilibili", "哔哩哔哩", "b站"))
        is_cloud = any(x in lowered for x in ("网易云", "cloudmusic"))
        if not self._is_direct_visual_media_request(text):
            return None
        if not await asyncio.to_thread(self.ensure_vision_service, True):
            return {"error": "视觉服务未就绪"}
        query = self._visual_search_query(text)
        if not query: return None
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
            preferred = ("哔哩", "bilibili", "chrome", "edge") if is_bili else ("网易云", "cloudmusic")
            window = next((item for key in preferred for item in windows if key.lower() in str(item.get("title", "")).lower()), None)
            if not window: return {"error": "没有检测到目标软件窗口"}
            title = str(window["title"])
            if is_cloud:
                if status: status("正在识别搜索框并输入…")
                await asyncio.wait_for(self._vision_mcp_call("window_type_text", {"title_contains": title, "instruction": "网易云音乐顶部的搜索框", "text": query}), timeout=90)
                await self._vision_mcp_call("desktop_hotkey", {"keys": ["enter"]}); await asyncio.sleep(3)
            if status: status("正在识别并播放第一项…")
            instruction = "Bilibili搜索结果中的第一个视频封面" if is_bili else "搜索结果中第一首歌曲对应的播放按钮"
            await asyncio.wait_for(self._vision_mcp_call("window_click", {"title_contains": title, "instruction": instruction}), timeout=90)
            self.log_event("direct_vision_completed", target="bilibili" if is_bili else "cloudmusic", query=query, window=title)
            return {"ok": True, "answer": f"找到啦，我已经在{'Bilibili' if is_bili else '网易云音乐'}里帮你点开 {query} 了。"}
        except asyncio.TimeoutError:
            self.log_event("direct_vision_timeout", query=query)
            return {"error": "视觉识别步骤超时"}
        except Exception as exc:
            self.log_event("direct_vision_failed", query=query, error=str(exc))
            return {"error": str(exc)}

    async def _run_direct_web_media(self, text: str, status=None) -> dict[str, Any] | None:
        """Operate Bilibili through DOM/text only; never loads the GUI vision model."""
        lowered = text.lower()
        if not any(x in lowered for x in ("bilibili", "哔哩哔哩", "b站")): return None
        query = self._visual_search_query(text)
        if not query: return {"error": "没有识别出要搜索的内容"}
        if not await asyncio.to_thread(self.ensure_vision_service, True): return {"error": "网页工具服务未就绪"}
        self.log_event("direct_web_started", target="bilibili", query=query)
        try:
            if status: status("网页 Agent 正在分步搜索、选择并验证…")
            python = ROOT / ".venv" / "Scripts" / "python.exe"
            script = ROOT / "Skill" / "web-agent-operator" / "scripts" / "web_agent.py"
            timeout = max(5, int(self.config.get("vision_mcp", {}).get("direct_operation_timeout_seconds", 20)))
            action = "play" if any(word in text for word in ("播放", "听", "播一下")) else "open"
            proc = await asyncio.create_subprocess_exec(str(python), str(script), "--site", "bilibili", "--query", query,
                "--action", action, "--timeout", str(timeout), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
            for event in events: self.log_event("web_agent_step", **event)
            completed = next((event for event in reversed(events) if event.get("event") == "completed"), None)
            failed = next((event for event in reversed(events) if event.get("event") == "failed"), None)
            if proc.returncode or not completed: return {"error": (failed or {}).get("error") or err.decode("utf-8", "replace")[-500:] or "网页 Agent 未验证完成状态"}
            self.log_event("direct_web_completed", query=query, title=completed.get("title"), url=completed.get("url"))
            verb = "打开并播放" if action == "play" else "找到并打开"
            return {"ok": True, "answer": f"找到啦，我已经在B站{verb} {completed.get('title') or query} 了。"}
        except Exception as exc:
            self.log_event("direct_web_failed", query=query, error=str(exc))
            return {"error": str(exc)}

    async def _natural_visual_failure(self, request: str, reason: str) -> str:
        """Generate only failure wording through the character LLM; keep it short for TTS."""
        fallback = "这次没能顺利操作成功，我已经停下来了。你可以稍后再让我试一次。"
        try:
            provider, key = self._provider(); llm_cfg = self.project["llm"]
            url = provider["base_url"].rstrip("/") + "/chat/completions"
            payload = {"model": provider["model"], "temperature": 0.55, "max_tokens": 100, "messages": [
                {"role": "system", "content": self._system_prompt() + "\n请用符合角色性格的自然口语说明一次电脑操作失败。只说一到两句，不要责怪用户，不要虚构成功。"},
                {"role": "user", "content": f"用户请求：{request}\n失败原因：{reason}"},
            ]}
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}) as response:
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

    async def _run_codex_task(self, task: str, require_mcp: bool = False, status=None, preferred_mcp: str = "") -> dict[str, Any]:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError
        cfg = self._codex_config()
        if not cfg.get("enabled", False):
            return {"error": "Codex CLI 功能未启用"}
        if preferred_mcp:
            if not await asyncio.to_thread(self.ensure_vision_service, True):
                return {"error": "视觉 MCP 常驻服务启动超时，请检查 Vision/logs/vision-mcp.log"}
        codex_command = self._codex_command()
        working_directory = Path(str(cfg.get("working_directory", ROOT))).expanduser().resolve()
        if not working_directory.is_dir():
            return {"error": f"Codex 工作目录不存在：{working_directory}"}
        gui_enabled = bool(self.config.get("vision_mcp", {}).get("gui_enabled", False))
        prompt = (
            "你是家庭 AI 助手的执行代理。请使用可用的 CLI、文件和 MCP 工具完成任务，"
            "最后用简洁中文给出适合语音朗读的结果。不要输出密钥。\n\n"
            + ("本任务必须优先使用合适的 MCP 工具；若没有可用 MCP，请明确说明。\n\n" if require_mcp else "")
            + (f"本任务必须首先使用 `{preferred_mcp}` MCP。"
               + ("这是网页任务且图像 GUI 已关闭：必须遵循 E:\\Doc\\AI直播\\Skill\\web-agent-operator\\SKILL.md，"
                  "只用 navigate/get_url/web_read/web_fill/web_click_text/web_press/web_play_media。"
                  "打开首页只是阶段一，绝不是完成；必须继续搜索、读取结果、选择匹配项、执行目标动作，并读取最终页面验证。"
                  "只有终态证据满足用户目标才能报告成功。\n\n" if not gui_enabled else
                  "先用 list_windows 检测目标应用，再用目标窗口工具操作；每次关键操作后重新读取状态验证。\n\n")
               if preferred_mcp else "")
            + f"角色与规则：\n{self.workspace.prompt_documents('home')}\n\n用户任务：{task}"
        )
        command = [*codex_command, "exec", "--json"]
        if cfg.get("skip_git_repo_check", True):
            command.append("--skip-git-repo-check")
        sandbox = str(cfg.get("sandbox", "danger-full-access")).strip()
        if sandbox:
            command += ["--sandbox", sandbox]
        command.append(prompt)
        if status:
            status("Codex CLI 正在执行…")
        self.log_event("codex_task_started", task=task, require_mcp=require_mcp, working_directory=working_directory)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = self._codex_environment(codex_command)
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=str(working_directory), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=creationflags, env=env,
        )
        with self.active_process_lock:
            self.active_process = proc
        if self.cancel_event.is_set():
            self.stop_current_task()
            raise asyncio.CancelledError
        try:
            task_timeout = int(self.config.get("vision_mcp", {}).get("task_timeout_seconds", 150)) if preferred_mcp else int(cfg.get("timeout_seconds", 600))
            out, err = await asyncio.wait_for(proc.communicate(), timeout=task_timeout)
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()
            with self.active_process_lock:
                if self.active_process is proc:
                    self.active_process = None
            self.log_event("codex_task_cancelled")
            raise
        except asyncio.TimeoutError:
            proc.kill(); out, err = await proc.communicate()
            detail = err.decode("utf-8", "replace").strip()[-1200:]
            self.log_event("codex_task_timeout", detail=detail)
            return {"error": "Codex CLI 执行超时。请检查网络、登录状态和 MCP 服务。" + (f"\n{detail}" if detail else "")}
        with self.active_process_lock:
            if self.active_process is proc:
                self.active_process = None
        stdout = out.decode("utf-8", "replace")
        stderr = err.decode("utf-8", "replace")
        log_dir = HOME_AGENT / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "codex-last.jsonl").write_text(stdout, encoding="utf-8")
        (log_dir / "codex-last.stderr.log").write_text(stderr, encoding="utf-8")
        answer = ""; events: list[dict[str, Any]] = []; mcp_calls: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            if event.get("type") == "thread.started":
                self.codex_thread_id = event.get("thread_id")
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                answer = str(item.get("text", "")).strip()
            if "mcp" in str(item.get("type", "")).lower():
                mcp_calls.append(str(item.get("name") or item.get("tool") or item.get("type")))
        if proc.returncode != 0:
            self.log_event("codex_task_failed", exit_code=proc.returncode, error=(stderr or stdout)[-2000:])
            return {"error": (stderr or stdout)[-2000:], "exit_code": proc.returncode}
        result = {"ok": True, "answer": answer or "Codex CLI 已完成任务。", "thread_id": self.codex_thread_id, "mcp_calls": mcp_calls, "event_count": len(events)}
        self.log_event("codex_task_completed", result=result)
        return result

    def list_skills(self) -> list[dict[str, str]]:
        root = Path(self.config["agent"].get("skill_root", ROOT / "Skill")); result = []
        if not root.exists(): return result
        for skill_md in root.glob("*/SKILL.md"):
            text = skill_md.read_text(encoding="utf-8")
            name = re.search(r"^name:\s*(.+)$", text, re.M)
            desc = re.search(r"^description:\s*(.+)$", text, re.M)
            if name: result.append({"name": name.group(1).strip(), "description": desc.group(1).strip() if desc else "", "path": str(skill_md)})
        return result

    def _tools(self) -> list[dict[str, Any]]:
        tools = [
            {"type": "function", "function": {"name": "search_memories", "description": "搜索旧式共享记忆索引。用户询问个人过去经历或‘你记得吗’时不得使用本工具，必须调用 long_term_memory 的 retrieve。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "list_skills", "description": "列出本地可用技能", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "list_character_images", "description": "列出角色形象库和主形象", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "generate_character_image", "description": "调用 ai-live-character-image 技能生成或编辑角色形象", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "operation": {"type": "string", "enum": ["generate", "edit"]}, "reference": {"type": "string", "description": "编辑时使用 primary 或图片路径"}, "label": {"type": "string"}, "tags": {"type": "string"}, "set_primary": {"type": "boolean"}}, "required": ["prompt"]}}},
            {"type": "function", "function": {"name": "sing_song", "description": "当用户要求唱歌、唱一首、哼唱或朗读歌词时调用。默认使用角色当前本地 TTS/SVC 音色朗读最多十行歌词；MiMo 唱歌仅作为已关闭的备用分支。", "parameters": {"type": "object", "properties": {"song": {"type": "string", "description": "歌曲名称或演唱主题"}, "lyrics": {"type": "string", "description": "最多十行需要朗读的歌词或测试文本"}, "style": {"type": "string", "description": "演唱或朗读情绪"}, "voice": {"type": "string", "description": "仅备用 MiMo 分支使用"}}, "required": ["song", "lyrics"]}}},
            {"type": "function", "function": {"name": "create_scheduled_task", "description": "创建TTS语音提醒或闹钟。一次性任务成功执行后自动删除；重复任务会保留并等待下一次。必须根据当前本地时间解析用户的自然语言时间。", "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "message": {"type": "string", "description": "触发时由TTS播放的文本"}, "recurrence": {"type": "string", "enum": ["once", "daily", "weekdays", "weekly"]}, "scheduled_at": {"type": "string", "description": "仅once使用，本地ISO时间，如2026-07-17T15:00"}, "time": {"type": "string", "description": "重复任务使用的24小时HH:MM"}, "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}, "description": "仅weekly使用，周一为1、周日为7"}, "action": {"type": "string", "enum": ["tts"]}}, "required": ["title", "message", "recurrence"]}}},
            {"type": "function", "function": {"name": "list_scheduled_tasks", "description": "列出当前所有提醒、闹钟和重复任务", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "delete_scheduled_task", "description": "取消并删除一个定时任务", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
            {"type": "function", "function": {"name": "acknowledge_scheduled_task", "description": "当用户明确回应刚才的提醒、表示知道了或已经完成时，确认最近一个待回应任务并停止本轮重复提醒。无关回复不要调用。", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "可选；不填时确认最近的待回应任务"}, "response": {"type": "string", "description": "用户的原始确认回复"}}}}},
            {"type": "function", "function": {"name": "long_term_memory", "description": "结构化长期记忆指令。高价值信息用store；用户询问过去经历或‘你记得吗’时必须先用retrieve。普通闲聊禁止store。", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["store", "retrieve"]}, "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 5}, "summary": {"type": "string", "maxLength": 20}, "detail": {"type": "string"}, "category": {"type": "string", "enum": ["health", "emotion", "major_event", "preference", "habit", "relationship", "agreement"]}, "importance": {"type": "integer", "minimum": 70, "maximum": 100}, "query_tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}}, "required": ["action"]}}},
        ]
        if self._codex_config().get("enabled", False):
            tools += [
                {"type": "function", "function": {"name": "codex_cli_task", "description": "调用 Codex CLI 完成复杂的本机命令、文件、编程或多步骤任务", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
                {"type": "function", "function": {"name": "mcp_task", "description": "通过 Codex CLI 调用已配置的 MCP 服务完成任务", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
                {"type": "function", "function": {"name": "web_agent_task", "description": "完成多步骤网页任务。只要请求包含搜索、查找、选择、点击、播放、填写或提交，就必须使用本工具，不能仅调用 open_url。", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}},
                {"type": "function", "function": {"name": "list_mcp_servers", "description": "检查 Codex CLI 和已配置的 MCP 服务", "parameters": {"type": "object", "properties": {}}}},
            ]
            if self.config.get("vision_mcp", {}).get("gui_enabled", False):
                tools.append({"type": "function", "function": {"name": "vision_gui_task", "description": "调用 GUI 图像识别 MCP 操作桌面窗口", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}}})
        if self.config.get("computer_control", {}).get("enabled", False):
            tools += [
                {"type": "function", "function": {"name": "list_directory", "description": "列出允许目录中的文件和子目录", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "read_text_file", "description": "读取允许目录中的文本文件，不能读取密钥文件", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "open_path", "description": "经过用户确认后，用系统默认程序打开允许目录中的文件或文件夹", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
                {"type": "function", "function": {"name": "open_url", "description": "仅用于目标就是打开某个网页的单步请求。若还要搜索、查找、点击、选择、播放、填写或提交，禁止使用本工具，必须调用 web_agent_task。", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
                {"type": "function", "function": {"name": "launch_app", "description": "按软件目录映射启动应用或打开软件目录。完整权限模式下也可使用可执行文件绝对路径", "parameters": {"type": "object", "properties": {"name": {"type": "string", "description": "已配置的软件名称、程序路径或目录路径"}, "arguments": {"type": "array", "items": {"type": "string"}}}, "required": ["name"]}}},
            ]
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

    async def _speak_home(self, session: aiohttp.ClientSession, text: str, status=None) -> list[str]:
        await asyncio.to_thread(self.tts_execution_lock.acquire)
        try:
            return await self._speak_home_unlocked(session, text, status)
        finally:
            self.tts_execution_lock.release()

    async def _speak_home_unlocked(self, session: aiohttp.ClientSession, text: str, status=None) -> list[str]:
        chunks = self._home_tts_chunks(text)
        client = TTSClient(session, self.project["tts"], ROOT / "audio"); paths: list[str] = []
        self.log_event("home_tts_split", chunks=len(chunks), chunk_chars=self.config.get("home", {}).get("tts_chunk_chars", 90))
        if not chunks: return paths
        queue: asyncio.Queue[tuple[int, Path] | None] = asyncio.Queue()

        async def generate() -> None:
            try:
                for index, chunk in enumerate(chunks, start=1):
                    if self.cancel_event.is_set():
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
                if self.cancel_event.is_set():
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

    async def chat(self, text: str, status=None, confirm=None) -> str:
        self._acknowledge_common_response(text)
        max_context = int(self.config["home"].get("max_context_messages", 30))
        self.history.append({"role": "user", "content": text}); self.history = self.history[-max_context:]
        self.log_event("user_message", message=text, history_messages=len(self.history))
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
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True): await self._speak_home(session, answer, status)
            return answer
        normalized = str(text).lower().replace(" ", "")
        simple_bilibili_open = (
            ("打开" in normalized or "访问" in normalized) and
            ("bilibili" in normalized or "哔哩哔哩" in normalized or "b站" in normalized) and
            not any(word in normalized for word in ("找", "搜", "播放", "点击", "登录", "发消息", "直播间", "视频", "听"))
        )
        if simple_bilibili_open:
            explicit_url = next(iter(re.findall(r"https?://[^\s，。]+", str(text), re.I)), "https://www.bilibili.com/")
            if status: status("正在打开 Bilibili…")
            await asyncio.to_thread(webbrowser.open, explicit_url)
            answer = "已经为你打开指定的 Bilibili 页面。" if explicit_url != "https://www.bilibili.com/" else "已经为你打开 Bilibili。"
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            self.log_event("direct_browser_open", url=explicit_url)
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True):
                    await self._speak_home(session, answer, status)
            return answer
        if self._is_direct_visual_media_request(text):
            visual_query = self._visual_search_query(text)
            visual_target = "B站" if any(x in text.lower() for x in ("bilibili", "哔哩哔哩", "b站")) else "网易云"
            gui_enabled = bool(self.config.get("vision_mcp", {}).get("gui_enabled", True))
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True):
                    await self._speak_home(session, f"好呀，我去{visual_target}帮你找找{visual_query or '想要的内容'}。", status)
            operation_timeout = max(5, int(self.config.get("vision_mcp", {}).get("direct_operation_timeout_seconds", 20)))
            try:
                operation = self._run_direct_visual_media(text, status) if gui_enabled else self._run_direct_web_media(text, status)
                direct_visual = await asyncio.wait_for(operation, timeout=operation_timeout)
            except asyncio.TimeoutError:
                self.stop_current_task()
                direct_visual = {"error": f"超过{operation_timeout}秒仍未执行成功，操作已超时停止"}
                self.log_event("direct_vision_total_timeout", limit_seconds=operation_timeout)
            if direct_visual is None:
                direct_visual = {"error": "图像 GUI 识别已禁用，而且该目标不是可直接读取的网页"}
            answer = (await self._natural_visual_failure(text, str(direct_visual["error"])) if direct_visual.get("error")
                      else str(direct_visual.get("answer", "好啦，已经帮你操作完成了。")))
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            async with aiohttp.ClientSession() as session:
                if self.config["home"].get("auto_speak", True): await self._speak_home(session, answer, status)
            return answer
        web_route = self._is_multistep_web_request(text) or self._has_recent_web_context(text)
        vision_route = self._should_route_to_vision(text)
        if web_route or vision_route or self._should_route_to_codex(text):
            route = "web_agent" if web_route else ("vision_mcp" if vision_route else "codex_cli")
            reason = "multi_step_web" if web_route else ("vision_priority" if vision_route else "trigger_mode_or_keyword")
            self.log_event("route_selected", route=route, reason=reason)
            preferred = str(self.config.get("vision_mcp", {}).get("server_name", "vision-gui")) if (web_route or vision_route) else ""
            if web_route or vision_route:
                async with aiohttp.ClientSession() as session:
                    if self.config["home"].get("auto_speak", True): await self._speak_home(session, "好呀，我来看一下屏幕，很快就好。", status)
                operation_timeout = max(90 if web_route else 5, int(self.config.get("vision_mcp", {}).get("direct_operation_timeout_seconds", 20)))
                try:
                    result = await asyncio.wait_for(self._run_codex_task(text, require_mcp=True, status=status, preferred_mcp=preferred), timeout=operation_timeout)
                except asyncio.TimeoutError:
                    self.stop_current_task(); result = {"error": f"超过{operation_timeout}秒仍未执行成功，操作已超时停止"}
            else:
                result = await self._run_codex_task(text, require_mcp=False, status=status, preferred_mcp="")
            if result.get("error"):
                answer = (await self._natural_visual_failure(text, str(result["error"])) if vision_route
                          else f"Codex CLI 执行失败：{result['error']}")
            else:
                answer = str(result.get("answer", "")).strip()
            self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
            async with aiohttp.ClientSession() as session:
                try:
                    provider, key = self._provider()
                    await self._maybe_remember_home(text, answer, session, provider, key)
                except Exception:
                    pass
                if self.config["home"].get("auto_speak", True) and answer:
                    await self._speak_home(session, answer, status)
            return answer
        self.log_event("route_selected", route="llm_tool_loop")
        provider, key = self._provider(); llm_cfg = self.project["llm"]
        memory_context = ""
        if recalled_memories:
            memory_context = "\n\n【已从SQLite长期记忆检索到的事实】\n" + json.dumps(recalled_memories, ensure_ascii=False) + "\n回答相关问题时只依据这些事实，不要虚构。"
        elif self._is_memory_recall_request(text):
            memory_context = "\n\n【SQLite长期记忆检索结果为空】不要声称记得具体事实；如实说明没有找到。"
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt() + memory_context}, *self.history]
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        timeout = aiohttp.ClientTimeout(total=llm_cfg.get("timeout_seconds", 45))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            singing_performed = False
            created_task_result = None
            long_term_stored = False
            for _ in range(int(self.config["agent"].get("max_tool_rounds", 5))):
                if status: status("正在思考…")
                tuning = llm_cfg.get("home", {})
                payload = {"model": provider["model"], "messages": messages, "tools": self._tools(), "tool_choice": "auto", "temperature": tuning.get("temperature", llm_cfg.get("temperature", .7)), "max_tokens": int(tuning.get("max_tokens", llm_cfg.get("max_tokens", 600)))}
                async with session.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}) as response:
                    raw = await response.text()
                    if response.status >= 400: raise RuntimeError(f"LLM HTTP {response.status}: {raw[:600]}")
                    choice = json.loads(raw)["choices"][0]["message"]
                messages.append(choice)
                calls = choice.get("tool_calls") or []
                if not calls:
                    answer = (choice.get("content") or "").strip()
                    if created_task_result:
                        task = created_task_result["task"]
                        # 精确时间、任务 ID 和队列长度仅供内部状态同步，不能进入普通回复或 TTS。
                        answer = f"好啦主人，{task['title']}已经设置好了。"
                    self.log_event("assistant_answer", answer=answer, tool_round_complete=True)
                    self.history.append({"role": "assistant", "content": answer}); self.history = self.history[-max_context:]
                    if not long_term_stored:
                        await self._maybe_remember_home(text, answer, session, provider, key)
                    if self.config["home"].get("auto_speak", True) and answer and not singing_performed:
                        await self._speak_home(session, answer, status)
                    return answer
                for call in calls:
                    name = call["function"]["name"]
                    try: args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError: args = {}
                    if status: status(f"正在调用工具：{name}")
                    self.log_event("tool_started", tool=name, arguments=args)
                    result = await self._run_tool(name, args, confirm)
                    if name == "create_scheduled_task" and isinstance(result, dict) and result.get("ok"):
                        created_task_result = result
                    if name == "sing_song" and isinstance(result, dict) and result.get("ok"):
                        singing_performed = True
                    if name == "long_term_memory" and str(args.get("action", "")) == "store" and isinstance(result, dict) and result.get("ok"):
                        long_term_stored = True
                    self.log_event("tool_completed", tool=name, result=result)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, ensure_ascii=False)})
        raise RuntimeError("工具调用轮次过多，已停止")

    async def _run_tool(self, name: str, args: dict[str, Any], confirm=None) -> Any:
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
            path = ROOT / "workspace" / "character_images" / "manifest.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"primary": None, "images": []}
        if name == "generate_character_image":
            if not self.config["agent"].get("allow_character_image_skill", True): return {"error": "角色图像技能已禁用"}
            script = Path(self.config["agent"]["skill_root"]) / "ai-live-character-image" / "scripts" / "character_image_api.py"
            cmd = [sys.executable, str(script), "--prompt", str(args.get("prompt", "")), "--operation", str(args.get("operation", "generate")), "--label", str(args.get("label", "家庭Agent生成")), "--tags", str(args.get("tags", "AI生成"))]
            if args.get("reference"): cmd += ["--reference", str(args["reference"])]
            if args.get("set_primary"): cmd.append("--set-primary")
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            if proc.returncode: return {"error": err.decode("utf-8", "replace")[-800:] or out.decode("utf-8", "replace")[-800:]}
            try: return json.loads(out.decode("utf-8").splitlines()[-1])
            except Exception: return {"output": out.decode("utf-8", "replace")[-1000:]}
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
            try: return {"path": str(path), "content": path.read_text(encoding="utf-8")[:30000]}
            except UnicodeDecodeError: return {"error": "不是 UTF-8 文本文件"}
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

    async def run_due_tasks(self) -> list[dict[str, Any]]:
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
                payload = {"model": provider["model"], "messages": [{"role": "user", "content": prompt}], "temperature": tuning.get("temperature", 0.2), "max_tokens": int(tuning.get("max_tokens", 180))}
                timeout = aiohttp.ClientTimeout(total=llm_cfg.get("timeout_seconds", 45))
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(provider["base_url"].rstrip("/") + "/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}) as response:
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
            result = {"ok": True, "compressed_messages": len(snapshot), "remaining_messages": len(self.history), "memory_cleanup": cleanup, "next_run_at": state["next_run_at"]}
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
                payload = {"model": provider["model"], "messages": [{"role": "system", "content": "只输出合法JSON，不要Markdown。"}, {"role": "user", "content": prompt}], "temperature": tuning.get("temperature", 0.2), "max_tokens": int(tuning.get("max_tokens", 180))}
                async with session.post(provider["base_url"].rstrip("/") + "/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"}) as response:
                    raw = await response.text()
                    if response.status < 400:
                        content = json.loads(raw)["choices"][0]["message"].get("content", ""); match = re.search(r"\{.*\}", content, re.S)
                        if match: result.update(json.loads(match.group(0)))
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
