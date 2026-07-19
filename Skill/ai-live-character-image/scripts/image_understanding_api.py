from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from dotenv import dotenv_values


ROOT = Path(os.getenv("AI_LIVE_ROOT", str(Path(__file__).resolve().parents[3]))).resolve()
CONFIG = ROOT / "config.yaml"


def load_settings() -> tuple[dict[str, Any], str]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    settings = config.get("image_understanding", {})
    key_name = str(settings.get("api_key_env", "MIMO_API_KEY"))
    env = dotenv_values(ROOT / ".env")
    key = str(env.get(key_name) or os.getenv(key_name) or "").strip()
    if not key:
        raise SystemExit(f"Missing {key_name}; save it in {ROOT / '.env'}")
    if not settings.get("base_url") or not settings.get("model"):
        raise SystemExit("Configure image_understanding in config.yaml")
    return settings, key


def resolve_image(value: str) -> Path:
    if value != "primary":
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise SystemExit(f"Image not found: {path}")
        return path.resolve()

    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    workspace = config.get("workspace", {})
    folder = ROOT / workspace.get("path", "workspace") / workspace.get("character_image_dir", "character_images")
    manifest_path = folder / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("The character image library has no valid manifest") from exc
    item = next((entry for entry in manifest.get("images", []) if entry.get("id") == manifest.get("primary")), None)
    if not item:
        raise SystemExit("The character image library has no primary image")
    path = folder / str(item["filename"])
    if not path.is_file():
        raise SystemExit(f"Primary image not found: {path}")
    return path.resolve()


def image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def response_text(data: dict[str, Any]) -> str:
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content", ""))
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict)).strip()
    return str(content).strip()


async def analyze(path: Path, prompt: str) -> dict[str, Any]:
    settings, key = load_settings()
    header = str(settings.get("auth_header", "api-key"))
    headers = {header: f"Bearer {key}" if header.lower() == "authorization" else key}
    payload: dict[str, Any] = {
        "model": settings["model"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url(path)}},
        ]}],
        "stream": False,
    }
    token_field = str(settings.get("max_tokens_field", "max_completion_tokens"))
    payload[token_field] = int(settings.get("max_completion_tokens", 1024))
    extra = settings.get("extra_body", {})
    if isinstance(extra, dict):
        payload.update(extra)
    url = str(settings["base_url"]).rstrip("/") + "/chat/completions"
    timeout = aiohttp.ClientTimeout(total=int(settings.get("timeout_seconds", 60)))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(3):
            async with session.post(url, headers=headers, json=payload) as response:
                body = await response.text()
                if response.status == 429 and attempt < 2:
                    await asyncio.sleep(0.75 * (2 ** attempt))
                    continue
                if response.status >= 400:
                    raise RuntimeError(f"MiMo image understanding HTTP {response.status}: {body[:800]}")
                data = json.loads(body)
                text = response_text(data)
                if not text:
                    raise RuntimeError("MiMo response did not contain analysis text")
                return {"ok": True, "analysis": text, "model": settings["model"], "image": str(path)}
    raise RuntimeError("MiMo image understanding request failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze an image with the configured MiMo multimodal model")
    parser.add_argument("--image", default="primary", help="Image path or 'primary'")
    parser.add_argument("--prompt", default="请详细描述这张图片中的主体、场景、文字和重要细节。")
    args = parser.parse_args()
    try:
        result = asyncio.run(analyze(resolve_image(args.image), args.prompt))
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
