from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from dotenv import dotenv_values

ROOT = Path(os.getenv("AI_LIVE_ROOT", r"E:\Doc\AI直播"))
CONFIG = ROOT / "config.yaml"


def load_settings() -> tuple[dict[str, Any], str]:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    settings = cfg.get("image_generation", {})
    env = dotenv_values(ROOT / ".env")
    key_name = settings.get("api_key_env", "IMAGE_API_KEY")
    key = str(env.get(key_name) or os.getenv(key_name) or "").strip()
    if not key:
        raise SystemExit(f"Missing {key_name}; save it in {ROOT / '.env'}")
    if not settings.get("base_url") or not settings.get("model"):
        raise SystemExit("Configure image_generation.base_url and image_generation.model in config.yaml")
    return settings, key


def library() -> tuple[Path, Path, dict[str, Any]]:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    wc = cfg.get("workspace", {})
    workspace = ROOT / wc.get("path", "workspace")
    folder = workspace / wc.get("character_image_dir", "character_images")
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    try: manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): manifest = {"primary": None, "images": []}
    manifest.setdefault("primary", None); manifest.setdefault("images", [])
    return folder, manifest_path, manifest


def reference_path(value: str | None, folder: Path, manifest: dict[str, Any]) -> Path | None:
    if not value: return None
    if value == "primary":
        item = next((x for x in manifest["images"] if x.get("id") == manifest.get("primary")), None)
        if not item: raise SystemExit("The character library has no primary image")
        return folder / item["filename"]
    direct = Path(value)
    if direct.is_file(): return direct
    item = next((x for x in manifest["images"] if value in {str(x.get("id")), str(x.get("filename"))}), None)
    if not item: raise SystemExit(f"Reference image not found: {value}")
    return folder / item["filename"]


def profile_text() -> str:
    path = ROOT / "workspace" / "CHARACTER.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def extract_image(data: dict[str, Any]) -> tuple[bytes | None, str | None]:
    items = data.get("data") or []
    if items:
        if items[0].get("b64_json"): return base64.b64decode(items[0]["b64_json"]), None
        if items[0].get("url"): return None, items[0]["url"]
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    for image in message.get("images") or []:
        value = image.get("image_url", {}).get("url") or image.get("url")
        if isinstance(value, str):
            if value.startswith("data:image/"): return base64.b64decode(value.split(",", 1)[1]), None
            if value.startswith("http"): return None, value
    content = message.get("content", "")
    if isinstance(content, list): content = "\n".join(str(x.get("image_url", {}).get("url") or x.get("text") or "") for x in content if isinstance(x, dict))
    match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", str(content))
    if match: return base64.b64decode(match.group(1)), None
    url = re.search(r"https?://\S+", str(content))
    return (None, url.group(0).rstrip(")]")) if url else (None, None)


async def call_api(settings: dict[str, Any], key: str, prompt: str, operation: str, reference: Path | None) -> bytes:
    base = str(settings["base_url"]).rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    timeout = aiohttp.ClientTimeout(total=float(settings.get("timeout_seconds", 180)))
    mode = settings.get("mode", "images")
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        if mode == "images" and operation == "edit":
            if not reference: raise SystemExit("Edit operation requires --reference")
            form = aiohttp.FormData()
            form.add_field("model", str(settings["model"])); form.add_field("prompt", prompt); form.add_field("size", str(settings.get("size", "1024x1024")))
            form.add_field("image", reference.read_bytes(), filename=reference.name, content_type=mimetypes.guess_type(reference.name)[0] or "image/png")
            response = await session.post(base + "/images/edits", data=form)
        elif mode == "images":
            body = {"model": settings["model"], "prompt": prompt, "size": settings.get("size", "1024x1024"), "n": 1, **settings.get("extra_body", {})}
            response = await session.post(base + "/images/generations", json=body)
        else:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            if reference:
                mime = mimetypes.guess_type(reference.name)[0] or "image/png"
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(reference.read_bytes()).decode()}"}})
            body = {"model": settings["model"], "messages": [{"role": "user", "content": content}], **settings.get("extra_body", {})}
            response = await session.post(base + "/chat/completions", json=body)
        raw = await response.text()
        if response.status >= 400: raise SystemExit(f"Image API HTTP {response.status}: {raw[:800]}")
        data = json.loads(raw); binary, url = extract_image(data)
        if binary: return binary
        if url:
            async with session.get(url) as image_response:
                if image_response.status >= 400: raise SystemExit(f"Image download HTTP {image_response.status}")
                return await image_response.read()
        raise SystemExit("The API response did not contain an image")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True); parser.add_argument("--operation", choices=["generate", "edit"], default="generate")
    parser.add_argument("--reference"); parser.add_argument("--label", default="AI generated character image"); parser.add_argument("--tags", default="AI生成")
    parser.add_argument("--set-primary", action="store_true")
    args = parser.parse_args()
    settings, key = load_settings(); folder, manifest_path, manifest = library(); reference = reference_path(args.reference, folder, manifest)
    full_prompt = profile_text().strip() + "\n\nIMAGE REQUEST:\n" + args.prompt.strip()
    binary = await call_api(settings, key, full_prompt, args.operation, reference)
    image_id = uuid.uuid4().hex; filename = f"generated_{datetime.now():%Y%m%d_%H%M%S}_{image_id[:8]}.png"; path = folder / filename
    path.write_bytes(binary)
    item = {"id": image_id, "filename": filename, "original_name": filename, "label": args.label, "tags": [x.strip() for x in args.tags.replace("，", ",").split(",") if x.strip()], "created_at": datetime.now().isoformat(timespec="seconds"), "source": "multimodal-api", "prompt": args.prompt}
    manifest["images"].append(item)
    if args.set_primary or not manifest.get("primary"): manifest["primary"] = image_id
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path.resolve()), "id": image_id, "primary": manifest["primary"] == image_id}, ensure_ascii=False))


if __name__ == "__main__": asyncio.run(main())
