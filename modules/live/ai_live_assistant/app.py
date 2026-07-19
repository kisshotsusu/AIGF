from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from .bilibili import BilibiliLive
from .config import secret_from_env
from .llm import LLMClient
from .tts import TTSClient
from .workspace import Workspace
from .long_term_memory import LongTermMemoryStore


class LiveAssistant:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg, self.root = cfg, Path(cfg["_root"])
        self.log = logging.getLogger("ai_live")
        self.context: deque[dict[str, Any]] = deque(maxlen=cfg["reply"].get("max_context_messages", 12))
        self.last_context_cleanup = 0.0
        self.context_state_file = self.root / "state" / "live-context.json"
        self.context_control_file = self.root / "state" / "live-context-control.json"
        self.last_context_control_token = ""
        self._load_live_context_state()
        self.welcomed: dict[str, float] = {}
        self._welcoming: set[str] = set()
        self.last_reply: dict[str, float] = {}
        self.send_lock = asyncio.Lock()
        self.message_log = self.root / "logs" / "messages.jsonl"
        self.message_log.parent.mkdir(parents=True, exist_ok=True)
        self.recent_danmaku: dict[tuple[str, str], float] = {}
        self.last_gift_reply: dict[str, float] = {}
        self._speech_queue: asyncio.PriorityQueue[tuple[int, int, str, asyncio.Future[bool]]] | None = None
        self._speech_sequence = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def _start_background_task(self, coroutine: Any) -> None:
        """Keep fire-and-forget handlers alive and make their failures visible."""
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    def _shorten_live_reply(reply: str, limit: int) -> str:
        reply = re.sub(r"[\r\n]+", " ", reply).strip().strip('"“”')
        if limit <= 0 or len(reply) <= limit: return reply
        shortened = reply[:limit]
        punctuation = max(shortened.rfind(mark) for mark in "。！？!?，,")
        return shortened[:punctuation + 1] if punctuation >= max(10, limit // 2) else reply[:max(1, limit - 1)].rstrip("，,") + "…"

    def _record_message(self, event: str, user: str, message: str, **extra: Any) -> None:
        row = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, "user": user, "message": message, **extra}
        with self.message_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def run(self) -> None:
        headers = {"User-Agent": "Mozilla/5.0 AI-Live-Assistant/1.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            bili = BilibiliLive(session, int(self.cfg["app"]["room_id"]), secret_from_env(self.cfg["bilibili"].get("cookie_env")))
            self.llm = LLMClient(session, self.cfg["llm"])
            self.tts = TTSClient(session, self.cfg["tts"], self.root / "audio")
            self.workspace = Workspace(self.root, self.cfg["workspace"])
            self.long_term_memory = LongTermMemoryStore(self.root / "LongTermMemory")
            self.long_term_memory.migrate_legacy(self.workspace.root / self.workspace.cfg.get("memory_dir", "memory"))
            self.bili = bili
            self._speech_queue = asyncio.PriorityQueue()
            self.log.info(
                "正在连接 B站直播间 %s（dry_run=%s, send_danmaku=%s）",
                bili.room_id,
                self.cfg["app"].get("dry_run", True),
                self.cfg["app"].get("send_danmaku", False),
            )
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            async def produce(stream) -> None:
                async for item in stream: await queue.put(item)
            producers = [asyncio.create_task(produce(bili.events())), asyncio.create_task(produce(bili.history_events())), asyncio.create_task(self._context_cleanup_loop()), asyncio.create_task(self._speech_worker())]
            try:
                while True: await self.handle_event(await queue.get())
            finally:
                for task in producers: task.cancel()

    async def handle_event(self, event: dict[str, Any]) -> None:
        self._cleanup_live_context()
        cmd = str(event.get("cmd", "")).split(":", 1)[0]
        if cmd == "DANMU_MSG":
            info = event.get("info", [])
            if len(info) > 2:
                text = str(info[1])
                user_info = info[2] if isinstance(info[2], (list, tuple)) else []
                user = str(user_info[1]).strip() if len(user_info) > 1 else ""
                uid = str(user_info[0]) if user_info else ""
                if not user:
                    self._record_message("received", "", text, status="skipped", reason="missing_username", uid=uid)
                    return
                if self._is_masked_username(user):
                    self._record_message("received", user, text, status="skipped", reason="masked_username", uid=uid)
                    self.log.info("跳过脱敏用户名弹幕 <%s>", user)
                    return
                duplicate_key = (user, text)
                now = time.monotonic()
                if now - self.recent_danmaku.get(duplicate_key, 0) < 3:
                    return
                self.recent_danmaku[duplicate_key] = now
                self.log.info("弹幕 <%s> %s", user, text)
                identity = self.workspace.resolve_user(user)
                identity_label = f"同一用户身份: {identity['name']} ({identity['id']})" if identity["is_owner"] else f"观众身份: {identity['id']}"
                self._append_live_context("user", f"[观众用户名: {user}]\n[{identity_label}]\n弹幕内容: {text}")
                should_reply, reason = self._should_reply(user, text)
                self._record_message("received", user, text, status="triggered" if should_reply else "skipped", reason=reason)
                if should_reply: self._start_background_task(self._smart_reply(user, text))
        elif cmd == "SEND_GIFT":
            await self._handle_gift(event.get("data", {}))
        elif cmd in {"INTERACT_WORD", "ENTRY_EFFECT"}:
            data = event.get("data", {})
            user = str(data.get("uname") or data.get("copy_writing") or "新朋友")
            uid = str(data.get("uid") or user)
            if self.cfg["bilibili"].get("welcome_enabled", True):
                self._record_message("welcome", user, "", status="received", uid=uid, command=cmd)
                self._start_background_task(self._welcome(uid, user))

    def _is_masked_username(self, user: str) -> bool:
        return bool(self.cfg.get("reply", {}).get("ignore_masked_usernames", True) and "***" in str(user))

    async def _handle_gift(self, data: dict[str, Any]) -> None:
        cfg = self.cfg.get("gift_reply", {})
        if not cfg.get("enabled", True): return
        user = str(data.get("uname") or data.get("username") or "").strip()
        uid = str(data.get("uid") or user)
        if not user or self._is_masked_username(user):
            self._record_message("gift", user, "", status="skipped", reason="masked_or_missing_username", uid=uid)
            return
        gift_name = str(data.get("giftName") or data.get("gift_name") or "礼物").strip()
        try: amount = max(1, int(data.get("num", 1)))
        except (TypeError, ValueError): amount = 1
        try: total_coin = int(data.get("total_coin") or data.get("price") or 0)
        except (TypeError, ValueError): total_coin = 0
        if total_coin < max(0, int(cfg.get("min_total_coin", 0))):
            self._record_message("gift", user, gift_name, status="skipped", reason="below_min_total_coin", amount=amount, total_coin=total_coin)
            return
        now = time.monotonic(); cooldown = max(0.0, float(cfg.get("cooldown_seconds", 8)))
        if now - self.last_gift_reply.get(uid, 0) < cooldown:
            self._record_message("gift", user, gift_name, status="skipped", reason="cooldown", amount=amount, total_coin=total_coin)
            return
        self.last_gift_reply[uid] = now
        safe_user = re.sub(r"[\r\n]", "", user)[:20]
        safe_gift = re.sub(r"[\r\n]", "", gift_name)[:20]
        template = str(cfg.get("template", "谢谢{username}送出的{gift_name}×{num}！"))
        try: reply = template.format(username=safe_user, gift_name=safe_gift, num=amount, total_coin=total_coin)
        except (KeyError, ValueError): reply = f"谢谢{safe_user}送出的{safe_gift}×{amount}！"
        reply = self._shorten_live_reply(reply, int(self.cfg.get("reply", {}).get("max_reply_chars", 60)))
        self._record_message("gift", user, gift_name, status="triggered", amount=amount, total_coin=total_coin, reply=reply)
        await self._emit(reply)

    async def _welcome(self, uid: str, user: str) -> None:
        if self._is_masked_username(user):
            self._record_message("welcome", user, "", status="skipped", reason="masked_username", uid=uid)
            return
        now = time.monotonic(); cooldown = self.cfg["bilibili"].get("welcome_cooldown_seconds", 1800)
        if now - self.welcomed.get(uid, 0) < cooldown:
            self._record_message("welcome", user, "", status="skipped", reason="cooldown", uid=uid)
            return
        if uid in self._welcoming:
            self._record_message("welcome", user, "", status="skipped", reason="already_queued", uid=uid)
            return
        self._welcoming.add(uid)
        safe_user = re.sub(r"[\r\n]", "", user)[:12]
        text = self.cfg["bilibili"]["welcome_template"].format(username=safe_user)
        try:
            success = await self._emit(text, speech_priority=0)
            if success:
                # A failed/busy TTS attempt must not consume the user's welcome cooldown.
                self.welcomed[uid] = time.monotonic()
                self._record_message("welcome", user, "", status="success", uid=uid, reply=text)
            else:
                self._record_message("welcome", user, "", status="error", reason="tts_failed", uid=uid, reply=text)
        finally:
            self._welcoming.discard(uid)

    def _should_reply(self, user: str, text: str) -> tuple[bool, str]:
        rc = self.cfg["reply"]
        if not rc.get("enabled", True): return False, "reply_disabled"
        if user in rc.get("ignore_users", []): return False, "ignored_user"
        remaining = float(rc.get("cooldown_seconds", 2)) - (time.monotonic() - self.last_reply.get(user, 0))
        if remaining > 0:
            self.log.info("跳过弹幕 <%s>：冷却中，剩余 %.1f 秒", user, remaining)
            return False, f"cooldown:{remaining:.1f}s"
        mode = rc.get("trigger_mode", "mention")
        hit = mode == "all" or (mode == "mention" and any(n in text for n in rc.get("bot_names", []))) or (mode == "prefix" and any(text.startswith(p) for p in rc.get("prefixes", [])))
        if hit: self.last_reply[user] = time.monotonic()
        return (True, f"mode:{mode}") if hit else (False, f"not_matched:{mode}")

    async def _smart_reply(self, user: str, text: str) -> None:
        try:
            # 私密记忆和私人照片只允许家庭模式读取，绝不注入直播模型上下文。
            memories = self.workspace.recent_memories(self.cfg["reply"].get("max_memory_items", 8), include_private=False)
            system = self.workspace.prompt_documents("live") + "\n\n身份识别规则：每条弹幕都带有[观众用户名]标签。用户名代表不同观众，回复时必须识别当前发言者，不要把不同用户名的经历或身份混淆。\n\n近期记忆：\n" + "\n".join(str(x) for x in memories)
            identity = self.workspace.resolve_user(user)
            relation = f"该账号已关联为家庭模式用户“{identity['name']}”，规范身份ID为 {identity['id']}。" if identity["is_owner"] else "该账号是独立直播观众，不要套用主人的记忆。"
            current = f"当前需要回复的观众用户名：{user}\n{relation}\n该观众本次弹幕：{text}\n请直接生成对这位观众的简短口语回复。"
            self._cleanup_live_context()
            context = [{"role": item["role"], "content": item["content"]} for item in self.context]
            messages = [{"role": "system", "content": system}, *context, {"role": "user", "content": current}]
            reply = self._shorten_live_reply(await self.llm.reply(messages), int(self.cfg["reply"].get("max_reply_chars", 60)))
            await self._emit(reply)
            self._append_live_context("assistant", reply)
            self._record_message("reply", user, text, status="success", reply=reply)
            await self._maybe_remember(user, text, reply)
        except Exception as exc:
            self._record_message("reply", user, text, status="error", error=str(exc))
            self.log.exception("智能回复失败")

    def _append_live_context(self, role: str, content: str) -> None:
        self.context.append({"role": role, "content": content, "_created_at": time.time()})
        self._save_live_context_state()

    def _load_live_context_state(self) -> None:
        if not self.context_state_file.exists(): return
        try: rows = json.loads(self.context_state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return
        if isinstance(rows, list):
            self.context.clear(); self.context.extend(item for item in rows if isinstance(item, dict))

    def _save_live_context_state(self) -> None:
        try:
            self.context_state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.context_state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(list(self.context), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.context_state_file)
        except OSError: pass

    def _cleanup_live_context(self, force: bool = False) -> int:
        cfg = self.cfg.get("context_cleanup", {})
        if not cfg.get("live_enabled", True): return 0
        now = time.time(); interval = max(10, int(cfg.get("check_interval_seconds", 60)))
        if not force and now - self.last_context_cleanup < interval: return 0
        self.last_context_cleanup = now
        cutoff = now - max(10, int(cfg.get("live_max_age_minutes", 120))) * 60
        before = len(self.context)
        kept = [item for item in self.context if float(item.get("_created_at", now)) >= cutoff]
        self.context.clear(); self.context.extend(kept)
        removed = before - len(self.context)
        if removed: self._save_live_context_state()
        if removed: self.log.info("已清理 %s 条超过保留时长的直播模型上下文", removed)
        return removed

    async def _context_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            self._apply_context_control()
            self._cleanup_live_context(force=False)

    def _apply_context_control(self) -> int:
        if not self.context_control_file.exists(): return 0
        try: request = json.loads(self.context_control_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return 0
        token = str(request.get("token", ""))
        if not token or token == self.last_context_control_token or request.get("action") != "clear": return 0
        removed = len(self.context); self.context.clear(); self._save_live_context_state(); self.last_context_control_token = token
        request.update({"status": "completed", "completed_at": datetime.now().isoformat(timespec="seconds"), "removed_messages": max(removed, int(request.get("removed_messages", 0) or 0))})
        try: self.context_control_file.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError: pass
        self.log.info("收到外部请求：已清空直播模型上下文，共 %s 条", removed)
        return removed

    async def _maybe_remember(self, user: str, message: str, reply: str) -> None:
        cfg = self.cfg.get("memory_write", {})
        identity = self.workspace.resolve_user(user)
        memory_user = identity["name"] if identity["is_owner"] else user
        mode = cfg.get("mode", "important")
        if mode == "off":
            return
        today = self.workspace.root / self.workspace.cfg.get("memory_dir", "memory") / f"{datetime.now():%Y-%m-%d}.jsonl"
        daily_count = 0
        if today.exists():
            try:
                for line in today.read_text(encoding="utf-8").splitlines():
                    item = json.loads(line); source = str(item.get("source", ""))
                    if source.startswith("auto") or source.startswith("home-auto"): daily_count += 1
            except (OSError, json.JSONDecodeError):
                pass
        if daily_count >= int(cfg.get("max_daily_writes", 20)):
            self._record_message("memory", user, message, status="skipped", reason="daily_limit")
            return
        base = {"user": memory_user, "user_id": identity["id"], "source_username": user, "message": message, "reply": reply, "source": "auto-all"}
        if mode == "all":
            self.workspace.remember({"type": "conversation", **base})
            self._record_message("memory", user, message, status="success", reason="mode:all")
            return
        always = any(word and word in message for word in cfg.get("always_keywords", []))
        ignored = any(word and word in message for word in cfg.get("ignore_keywords", []))
        if ignored and not always:
            self._record_message("memory", user, message, status="skipped", reason="ignored_keyword")
            return
        if len(message.strip()) < int(cfg.get("min_message_length", 4)) and not always:
            self._record_message("memory", user, message, status="skipped", reason="too_short")
            return
        threshold = int(cfg.get("importance_threshold", 70))
        result = {"importance": 90 if always else 50, "should_remember": always, "category": "identity" if always else "event", "summary": f"{memory_user}说：{message}"}
        if cfg.get("analyze_with_llm", True):
            prompt = (
                "你是直播间长期记忆筛选器。判断这段互动是否值得跨场次长期记住。"
                "身份、称呼、稳定喜好、重要关系、明确约定和重大事件分数应高；寒暄、玩笑、测试、重复弹幕和临时话题分数应低。"
                "只输出JSON对象，字段为 importance(0到100整数)、should_remember(布尔)、category(identity/preference/relationship/agreement/event)、summary(一句不超过60字的第三人称事实)。\n"
                f"观众用户名：{user}\n规范用户身份：{memory_user} ({identity['id']})\n观众消息：{message}\nAI回复：{reply}"
            )
            try:
                raw = await self.llm.reply([{"role": "system", "content": "只输出合法JSON，不要Markdown。"}, {"role": "user", "content": prompt}], profile="memory")
                match = re.search(r"\{.*\}", raw, re.S)
                if match: result.update(json.loads(match.group(0)))
            except Exception:
                self.log.exception("记忆重要度分析失败")
        score = max(0, min(100, int(result.get("importance", 0))))
        should = bool(result.get("should_remember")) and score >= threshold
        if always: should, score = True, max(score, threshold)
        if not should:
            self._record_message("memory", user, message, status="skipped", reason=f"importance:{score}")
            return
        category = str(result.get("category", "event"))
        summary = str(result.get("summary") or f"{memory_user}说：{message}").strip()[:120]
        self.workspace.remember({
            "type": category, "user": memory_user, "user_id": identity["id"],
            "source_username": user, "content": summary, "importance": score,
            "source": "auto-important", "original_message": message,
        })
        allowed = {"preference", "relationship", "agreement"}
        sqlite_category = category if category in allowed else "major_event"
        tags = [memory_user, sqlite_category, "直播记忆"]
        try:
            self.long_term_memory.store(
                tags=tags, summary=summary[:20], detail=message, category=sqlite_category,
                importance=score, user_id=identity["id"], scene="live",
                privacy="private" if identity["is_owner"] else "public", source="live-auto-classifier",
            )
        except (TypeError, ValueError, OSError) as exc:
            self.log.warning("SQLite长期记忆写入失败: %s", exc)
        self._record_message("memory", user, message, status="success", reason=f"importance:{score}", summary=summary)

    async def _speech_worker(self) -> None:
        """Serialize GPU synthesis/playback without blocking event ingestion."""
        assert self._speech_queue is not None
        while True:
            _, _, text, completed = await self._speech_queue.get()
            try:
                await self.tts.speak(text)
            except asyncio.CancelledError:
                if not completed.done(): completed.cancel()
                raise
            except Exception as exc:
                self.log.exception("语音生成/播放最终失败（已完成内部重试）")
                if not completed.done(): completed.set_result(False)
                self._record_message("speech", "", text, status="error", error=str(exc))
            else:
                if not completed.done(): completed.set_result(True)
                self._record_message("speech", "", text, status="success")
            finally:
                self._speech_queue.task_done()

    async def _emit(self, text: str, speech_priority: int = 10) -> bool:
        self.log.info("回复: %s", text)
        async with self.send_lock:
            if self.cfg["app"].get("send_danmaku", False) and not self.cfg["app"].get("dry_run", True):
                limit = int(self.cfg["bilibili"].get("max_message_length", 20))
                await self.bili.send_danmaku(text[:limit])
                await asyncio.sleep(self.cfg["bilibili"].get("send_interval_seconds", 6))
        if not self.cfg.get("tts", {}).get("enabled", True): return True
        if self._speech_queue is None:
            try: await self.tts.speak(text)
            except Exception:
                self.log.exception("语音生成/播放失败")
                return False
            return True
        completed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._speech_sequence += 1
        await self._speech_queue.put((speech_priority, self._speech_sequence, text, completed))
        self.log.info("语音已排队: priority=%s queue=%s", speech_priority, self._speech_queue.qsize())
        return await completed
