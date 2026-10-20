"""Own the external Harness runtime; never implement a local model/tool loop."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import threading
import uuid


class HarnessExecutor:
    def __init__(self, root, config, *, factory=None):
        self.root = Path(root).resolve()
        self.config = config
        self.factory = factory

    def path(self, value):
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else self.root / path).resolve())

    async def run(self, prompt, *, env, cancel_event, status=None):
        factory = self.factory
        if factory is None:
            try:
                from deepseek_harness import DeepSeekHarness
            except ImportError as exc:
                raise RuntimeError("请在项目环境安装 deepseek-harness-sdk；不会回退本地工具循环") from exc
            factory = DeepSeekHarness
        cfg = self.config
        options = {
            "dsh_home": self.path(cfg.get("dsh_home", "HomeAgent/state/dsh-home")),
            "cwd": self.path(cfg.get("cwd", ".")),
            "provider": cfg.get("provider", "deepseek-official"),
            "model": cfg.get("model", "deepseek-v4-flash"),
            "profile": cfg.get("profile", "sdk-minimal"),
            "request_timeout_seconds": float(cfg.get("timeout_seconds", 300)),
            "env": env,
            "patches": tuple(self.path(p) for p in cfg.get("patches", [])),
        }
        for key in ("reasoning_effort", "max_tokens", "base_url", "dsh_bin"):
            if cfg.get(key) is not None:
                options[key] = cfg[key]
        if cfg.get("dsh_bin"):
            options["dsh_bin"] = self.path(cfg["dsh_bin"])
        harness = factory(**options)
        loop = asyncio.get_running_loop()
        session_id = "aigf-" + uuid.uuid4().hex
        close_lock = threading.Lock()
        stopping = threading.Event()
        deadline = loop.time() + options["request_timeout_seconds"]

        def close():
            with close_lock:
                harness.close()

        def notify(notification):
            if status and notification.method == "session.event":
                event = notification.payload.get("event", {})
                # Do not log tokens, complete prompts or private tool arguments.
                if event.get("type"):
                    loop.call_soon_threadsafe(status, "Harness: " + str(event["type"]))

        def execute():
            try:
                if stopping.is_set() or cancel_event.is_set():
                    raise asyncio.CancelledError()
                harness.start()
                if stopping.is_set() or cancel_event.is_set():
                    raise asyncio.CancelledError()
                return harness.run(prompt, session_id=session_id, on_notification=notify)
            finally:
                close()

        if cancel_event.is_set():
            raise asyncio.CancelledError()
        worker = asyncio.create_task(asyncio.to_thread(execute))
        try:
            while not worker.done():
                if cancel_event.is_set():
                    raise asyncio.CancelledError()
                if loop.time() >= deadline:
                    raise TimeoutError("Harness turn exceeded timeout_seconds")
                await asyncio.wait({worker}, timeout=0.1)
            result = await worker
            if result.finish_reason != "completed":
                raise RuntimeError(f"Harness execution did not complete: {result.finish_reason}")
            if not isinstance(result.final_response, str) or not result.final_response.strip():
                raise RuntimeError("Harness returned no final response")
            return result.final_response.strip()
        except BaseException:
            stopping.set()
            await asyncio.to_thread(close)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(worker), timeout=5)
            raise
