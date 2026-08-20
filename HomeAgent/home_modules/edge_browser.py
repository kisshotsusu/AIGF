"""Edge 浏览器控制模块（独立能力，不绑定任何固定流程）。

通过 Chrome DevTools Protocol (CDP) 直连 Edge：
- 已有 CDP 端点（如 Vision MCP 的 9222/9223/9333）时直接复用；
- 没有时自动拉起 Edge（独立用户目录，避免影响日常浏览）；
- 可列出/打开标签页、执行 JS、导航、截图；
- 可发现 chrome-extension:// 目标，用于与 ChatGPT 等 Edge 插件页交互。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp


EDGE_EXECUTABLE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _default_user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "HomeAgent" / "EdgeProfile"


def find_edge_executable() -> str:
    """查找 msedge.exe；找不到返回空字符串。"""
    found = shutil.which("msedge")
    if found:
        return found
    for candidate in EDGE_EXECUTABLE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return ""


def is_extension_target(target: dict[str, Any]) -> bool:
    """chrome-extension:// 目标（ChatGPT 等插件页面）。"""
    return str(target.get("url") or "").startswith("chrome-extension://")


def target_label(target: dict[str, Any]) -> str:
    return str(target.get("title") or target.get("url") or "（未命名标签）")


class EdgeBrowserClient:
    """CDP 客户端：状态、打开标签、执行 JS、导航、截图、扩展页发现。"""

    DEFAULTS: dict[str, Any] = {
        "enabled": True,
        "executable": "",
        "user_data_dir": "",
        "port": 9223,
        "chatgpt_url": "https://chatgpt.com/",
        "chatgpt_extension_url": "",
        "timeout_seconds": 30,
        "startup_wait_seconds": 60,
        "output_dir": "outputs/edge",
        "project_root": "",
    }

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**self.DEFAULTS, **(config or {})}

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{int(self.config.get('port', 9223))}"

    def _output_dir(self) -> Path:
        value = str(self.config.get("output_dir") or "outputs/edge")
        path = Path(value)
        if not path.is_absolute():
            root = str(self.config.get("project_root") or "").strip() or str(Path.cwd())
            path = Path(root) / path
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    async def _http_json(self, session: aiohttp.ClientSession, method: str, path: str, timeout: float | None = None) -> Any:
        url = self.endpoint.rstrip("/") + path
        timeout = aiohttp.ClientTimeout(total=timeout or float(self.config.get("timeout_seconds", 30)))
        async with session.request(method, url, timeout=timeout) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"CDP HTTP {response.status}: {raw[:600]}")
            if not raw:
                return None
            return json.loads(raw)

    async def _ws_rpc(self, session: aiohttp.ClientSession, ws_url: str, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        """在指定 target 的 WebSocket 上执行一条 CDP 命令。"""
        timeout = aiohttp.ClientTimeout(total=timeout or float(self.config.get("timeout_seconds", 30)))
        async with session.ws_connect(ws_url, timeout=timeout) as ws:
            request_id = int(time.time_ns() % 10**9)
            await ws.send_json({"id": request_id, "method": method, "params": params or {}})
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if data.get("id") != request_id:
                    continue
                if "error" in data:
                    raise RuntimeError(f"CDP {method} 失败：{data['error']}")
                return data.get("result", {})
        raise RuntimeError(f"CDP {method} 未返回结果")

    async def status(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """返回浏览器版本、标签页与扩展页列表。"""
        version = await self._http_json(session, "GET", "/json/version")
        targets = await self._http_json(session, "GET", "/json/list") or []
        pages = [t for t in targets if t.get("type") == "page"]
        extensions = [t for t in targets if is_extension_target(t)]
        return {
            "ok": True,
            "endpoint": self.endpoint,
            "browser": str(version.get("Browser") or "") if isinstance(version, dict) else "",
            "web_socket_debugger_url": str(version.get("webSocketDebuggerUrl") or "") if isinstance(version, dict) else "",
            "page_count": len(pages),
            "pages": [{"id": t.get("id"), "title": target_label(t), "url": t.get("url")} for t in pages],
            "extension_count": len(extensions),
            "extensions": [{"id": t.get("id"), "title": target_label(t), "url": t.get("url")} for t in extensions],
        }

    async def ensure_running(self, session: aiohttp.ClientSession) -> bool:
        """优先复用现有 CDP 端点；否则拉起独立 Edge 并等待就绪。"""
        try:
            await self._http_json(session, "GET", "/json/version", timeout=3)
            return True
        except Exception:
            pass
        executable = str(self.config.get("executable") or "").strip() or find_edge_executable()
        if not executable:
            raise RuntimeError("未找到 msedge.exe，请在 edge_browser.executable 配置完整路径")
        user_data = Path(str(self.config.get("user_data_dir") or "") or _default_user_data_dir())
        user_data.mkdir(parents=True, exist_ok=True)
        args = [
            executable,
            "--remote-debugging-port", str(int(self.config.get("port", 9223))),
            "--user-data-dir", str(user_data),
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msUndersideButton",
            "about:blank",
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, creationflags=creationflags)
        deadline = time.monotonic() + float(self.config.get("startup_wait_seconds", 60))
        while time.monotonic() < deadline:
            try:
                await self._http_json(session, "GET", "/json/version", timeout=3)
                return True
            except Exception:
                await asyncio.sleep(1)
        raise RuntimeError(f"Edge 在 {self.config.get('startup_wait_seconds')} 秒内未开放 CDP 端口 {self.config.get('port')}")

    async def open_url(self, session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
        await self.ensure_running(session)
        target = await self._http_json(session, "PUT", f"/json/new?{url}")
        return {
            "ok": True,
            "target_id": str(target.get("id") or ""),
            "url": str(target.get("url") or url),
            "title": target_label(target),
        }

    async def open_chatgpt(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        await self.ensure_running(session)
        url = str(self.config.get("chatgpt_extension_url") or "").strip() or str(self.config.get("chatgpt_url") or "https://chatgpt.com/")
        return await self.open_url(session, url)

    async def _resolve_target(self, session: aiohttp.ClientSession, target_id: str = "") -> dict[str, Any]:
        targets = await self._http_json(session, "GET", "/json/list") or []
        if target_id:
            match = next((t for t in targets if t.get("id") == target_id), None)
            if not match:
                raise RuntimeError(f"目标不存在：{target_id}")
            return match
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise RuntimeError("Edge 中没有可用的标签页，请先 open_url 或 open_chatgpt")
        return page

    async def eval_js(self, session: aiohttp.ClientSession, expression: str, target_id: str = "") -> dict[str, Any]:
        await self.ensure_running(session)
        target = await self._resolve_target(session, target_id)
        result = await self._ws_rpc(
            session, str(target["webSocketDebuggerUrl"]),
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
        )
        value = result.get("result", {})
        if result.get("exceptionDetails"):
            return {"ok": False, "error": str(result["exceptionDetails"])[:400]}
        return {"ok": True, "target_id": str(target.get("id") or ""), "value": value.get("value")}

    async def navigate(self, session: aiohttp.ClientSession, url: str, target_id: str = "") -> dict[str, Any]:
        await self.ensure_running(session)
        target = await self._resolve_target(session, target_id)
        await self._ws_rpc(session, str(target["webSocketDebuggerUrl"]), "Page.navigate", {"url": url})
        return {"ok": True, "target_id": str(target.get("id") or ""), "url": url}

    async def screenshot(self, session: aiohttp.ClientSession, target_id: str = "") -> dict[str, Any]:
        await self.ensure_running(session)
        target = await self._resolve_target(session, target_id)
        result = await self._ws_rpc(session, str(target["webSocketDebuggerUrl"]), "Page.captureScreenshot", {"format": "png"})
        encoded = str(result.get("data") or "")
        if not encoded:
            raise RuntimeError("CDP 未返回截图数据")
        output = self._output_dir() / f"edge_{datetime.now():%Y%m%d_%H%M%S}.png"
        output.write_bytes(base64.b64decode(encoded))
        return {"ok": True, "path": str(output), "target_id": str(target.get("id") or ""), "title": target_label(target)}
