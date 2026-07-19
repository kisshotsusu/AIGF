from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
from pathlib import Path

import aiohttp
import yaml
from dotenv import dotenv_values

import agent as vision_agent


ROOT = Path(__file__).resolve().parents[1]


async def analyze(title: str, prompt: str) -> dict:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    settings = config.get("image_understanding", {})
    key_name = str(settings.get("api_key_env", "MIMO_API_KEY"))
    env = dotenv_values(ROOT / ".env")
    key = str(env.get(key_name) or os.getenv(key_name) or "").strip()
    if not key:
        raise RuntimeError(f"Missing {key_name}")
    image = vision_agent.window_screenshot_pil(title)
    buffer = io.BytesIO(); image.save(buffer, "PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    header = str(settings.get("auth_header", "api-key"))
    headers = {header: f"Bearer {key}" if header.lower() == "authorization" else key}
    payload = {
        "model": settings.get("model", "mimo-v2.5"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "max_completion_tokens": int(settings.get("max_completion_tokens", 1024)),
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    timeout = aiohttp.ClientTimeout(total=int(settings.get("timeout_seconds", 60)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(str(settings.get("base_url", "https://api.xiaomimimo.com/v1")).rstrip("/") + "/chat/completions", headers=headers, json=payload) as response:
            body = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"MiMo window analysis HTTP {response.status}: {body[:500]}")
    data = json.loads(body)
    text = str(data["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("MiMo window analysis returned no text")
    return {"ok": True, "analysis": text, "window": title}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(analyze(args.title, args.prompt))
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
