from __future__ import annotations

from typing import Any

import aiohttp

from .config import secret_from_env


class LLMClient:
    def __init__(self, session: aiohttp.ClientSession, cfg: dict[str, Any]):
        self.session = session
        self.cfg = cfg

    async def reply(self, messages: list[dict[str, str]], profile: str = "live") -> str:
        name = self.cfg.get("provider", "deepseek")
        provider = self.cfg["providers"][name]
        key = secret_from_env(provider.get("api_key_env"))
        if not key:
            raise RuntimeError(f"缺少环境变量 {provider.get('api_key_env')}")
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        tuning = self.cfg.get(profile, {})
        payload = {
            "model": provider["model"], "messages": messages,
            "temperature": tuning.get("temperature", self.cfg.get("temperature", 0.55)),
            "max_tokens": tuning.get("max_tokens", self.cfg.get("max_tokens", 160)), "stream": False,
        }
        timeout = aiohttp.ClientTimeout(total=self.cfg.get("timeout_seconds", 45))
        async with self.session.post(url, json=payload, headers={"Authorization": f"Bearer {key}"}, timeout=timeout) as r:
            body = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"LLM HTTP {r.status}: {body[:500]}")
            data = await r.json()
        return data["choices"][0]["message"]["content"].strip()
