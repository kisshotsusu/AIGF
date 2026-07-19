from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from aiohttp import web
from .ai_live_assistant.tts import cleanup_audio_files

MODULE_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
WEB = MODULE_DIR / "web"
CONFIG = ROOT / "config.yaml"
EXAMPLE = ROOT / "config.example.yaml"
ENV_FILE = ROOT / ".env"
DOCS = {"soul": "SOUL.md", "rules": "RULES.md", "abilities": "ABILITIES.md"}
assistant_process: subprocess.Popen | None = None
assistant_log = None


def ensure_files() -> None:
    cleanup_audio_files(ROOT / "audio", 20)
    if not CONFIG.exists(): shutil.copyfile(EXAMPLE, CONFIG)
    if not ENV_FILE.exists() and (ROOT / ".env.example").exists():
        shutil.copyfile(ROOT / ".env.example", ENV_FILE)


def read_config() -> dict[str, Any]:
    ensure_files()
    with CONFIG.open("r", encoding="utf-8") as f: return yaml.safe_load(f) or {}


def write_config(data: dict[str, Any]) -> None:
    with CONFIG.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=120)


def read_env() -> dict[str, str]:
    ensure_files(); result = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1); result[k.strip()] = v.strip()
    return result


def write_env(updates: dict[str, str]) -> None:
    current = read_env()
    for key, value in updates.items():
        if value: current[key] = value.replace("\r", "").replace("\n", "")
    ENV_FILE.write_text("\n".join(f"{k}={v}" for k, v in current.items()) + "\n", encoding="utf-8")


async def json_body(request: web.Request) -> dict[str, Any]:
    try: return await request.json()
    except Exception as exc: raise web.HTTPBadRequest(text=json.dumps({"error": f"JSON 格式错误: {exc}"}, ensure_ascii=False), content_type="application/json")


async def get_config(_: web.Request) -> web.Response:
    return web.json_response(read_config())


async def put_config(request: web.Request) -> web.Response:
    data = await json_body(request)
    room_id = int(data.get("app", {}).get("room_id", 0))
    if room_id < 0: raise web.HTTPBadRequest(text='{"error":"直播间号不能为负数"}', content_type="application/json")
    # 以下配置已迁移到角色工作台。直播网页即使长时间未刷新，也不能用旧值覆盖它们。
    current = read_config()
    for section in ("llm", "tts", "image_generation", "memory_write", "workspace"):
        if section in current: data[section] = current[section]
    write_config(data)
    return web.json_response({"ok": True})


async def get_secrets(_: web.Request) -> web.Response:
    env = read_env()
    return web.json_response({k: bool(env.get(k)) for k in ("BILIBILI_COOKIE", "DEEPSEEK_API_KEY", "MIMO_API_KEY", "CUSTOM_API_KEY", "IMAGE_API_KEY")})


async def put_secrets(request: web.Request) -> web.Response:
    data = await json_body(request)
    # 模型、图像和 STT 密钥由角色工作台管理；直播控制台只保留 B 站登录信息。
    write_env({"BILIBILI_COOKIE": str(data.get("BILIBILI_COOKIE", ""))})
    return web.json_response({"ok": True})


async def get_docs(_: web.Request) -> web.Response:
    folder = ROOT / read_config().get("workspace", {}).get("path", "workspace")
    return web.json_response({key: (folder / name).read_text(encoding="utf-8") for key, name in DOCS.items()})


async def get_messages(request: web.Request) -> web.Response:
    path = ROOT / "logs" / "messages.jsonl"
    limit = min(max(int(request.query.get("limit", "100")), 1), 500)
    rows: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try: rows.append(json.loads(line))
            except json.JSONDecodeError: continue
    return web.json_response({"messages": list(reversed(rows))})


def memory_folder() -> Path:
    cfg = read_config().get("workspace", {})
    folder = ROOT / cfg.get("path", "workspace") / cfg.get("memory_dir", "memory")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def character_paths() -> tuple[Path, Path, Path]:
    cfg = read_config().get("workspace", {})
    workspace = ROOT / cfg.get("path", "workspace")
    folder = workspace / cfg.get("character_image_dir", "character_images")
    profile = workspace / cfg.get("character_profile_file", "CHARACTER.md")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "manifest.json"
    if not manifest.exists(): manifest.write_text('{"primary":null,"images":[]}\n', encoding="utf-8")
    return folder, profile, manifest


def character_manifest() -> dict[str, Any]:
    _, _, path = character_paths()
    try: data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): data = {"primary": None, "images": []}
    data.setdefault("primary", None); data.setdefault("images", [])
    return data


def save_character_manifest(data: dict[str, Any]) -> None:
    _, _, path = character_paths()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def get_character(_: web.Request) -> web.Response:
    _, profile, _ = character_paths(); data = character_manifest()
    return web.json_response({"profile": profile.read_text(encoding="utf-8") if profile.exists() else "", **data})


async def get_character_image(request: web.Request) -> web.StreamResponse:
    folder, _, _ = character_paths(); path = folder / Path(request.match_info["filename"]).name
    if not path.is_file() or path.parent.resolve() != folder.resolve(): raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


async def upload_character_image(request: web.Request) -> web.Response:
    reader = await request.multipart(); upload = None; label = ""; tags: list[str] = []
    async for field in reader:
        if field.name == "file":
            original = Path(field.filename or "image").name; ext = Path(original).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}: raise web.HTTPBadRequest(text='{"error":"仅支持 PNG、JPEG、WebP 或 GIF"}', content_type="application/json")
            content = bytearray()
            while chunk := await field.read_chunk():
                content.extend(chunk)
                if len(content) > 15 * 1024 * 1024: raise web.HTTPBadRequest(text='{"error":"图片不能超过15MB"}', content_type="application/json")
            upload = (original, ext, bytes(content))
        elif field.name == "label": label = (await field.text()).strip()
        elif field.name == "tags": tags = [x.strip() for x in (await field.text()).replace("，", ",").split(",") if x.strip()]
    if not upload: raise web.HTTPBadRequest(text='{"error":"请选择图片文件"}', content_type="application/json")
    original, ext, content = upload; folder, _, _ = character_paths(); image_id = uuid.uuid4().hex; filename = image_id + ext
    (folder / filename).write_bytes(content)
    data = character_manifest(); item = {"id": image_id, "filename": filename, "original_name": original, "label": label or Path(original).stem, "tags": tags, "created_at": datetime.now().isoformat(timespec="seconds")}
    data["images"].append(item)
    if not data.get("primary"): data["primary"] = image_id
    save_character_manifest(data)
    return web.json_response({"ok": True, "image": item})


async def update_character(request: web.Request) -> web.Response:
    body = await json_body(request); folder, profile, _ = character_paths(); data = character_manifest()
    if "profile" in body: profile.write_text(str(body["profile"]), encoding="utf-8")
    if "primary" in body:
        requested = str(body["primary"])
        if requested and not any(str(x.get("id")) == requested for x in data["images"]): raise web.HTTPBadRequest(text='{"error":"主形象不存在"}', content_type="application/json")
        data["primary"] = requested or None
    if body.get("image_id"):
        item = next((x for x in data["images"] if str(x.get("id")) == str(body["image_id"])), None)
        if not item: raise web.HTTPNotFound(text='{"error":"图片不存在"}', content_type="application/json")
        if "label" in body: item["label"] = str(body["label"]).strip()
        if "tags" in body: item["tags"] = body["tags"] if isinstance(body["tags"], list) else []
    save_character_manifest(data)
    return web.json_response({"ok": True})


async def delete_character_image(request: web.Request) -> web.Response:
    image_id = request.match_info["image_id"]; folder, _, _ = character_paths(); data = character_manifest()
    item = next((x for x in data["images"] if str(x.get("id")) == image_id), None)
    if not item: raise web.HTTPNotFound(text='{"error":"图片不存在"}', content_type="application/json")
    path = folder / Path(str(item.get("filename", ""))).name
    if path.is_file(): path.unlink()
    data["images"] = [x for x in data["images"] if str(x.get("id")) != image_id]
    if data.get("primary") == image_id: data["primary"] = data["images"][0]["id"] if data["images"] else None
    save_character_manifest(data)
    return web.json_response({"ok": True})


def memory_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(memory_folder().glob("*.jsonl"), reverse=True):
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            try: record = json.loads(line)
            except json.JSONDecodeError: continue
            if not isinstance(record, dict): continue
            item = dict(record)
            item["_id"] = str(record.get("id") or f"legacy:{path.name}:{index}")
            item["_file"] = path.name
            item["_index"] = index
            rows.append(item)
    return sorted(rows, key=lambda x: str(x.get("time", "")), reverse=True)


async def get_memories(request: web.Request) -> web.Response:
    query = request.query.get("q", "").strip().lower()
    limit = min(max(int(request.query.get("limit", "300")), 1), 1000)
    rows = memory_rows()
    if query: rows = [row for row in rows if query in json.dumps(row, ensure_ascii=False).lower()]
    return web.json_response({"memories": rows[:limit], "total": len(rows)})


async def create_memory(request: web.Request) -> web.Response:
    data = await json_body(request)
    content = str(data.get("content", "")).strip()
    if not content: raise web.HTTPBadRequest(text='{"error":"记忆内容不能为空"}', content_type="application/json")
    now = datetime.now()
    record = {
        "id": uuid.uuid4().hex, "time": now.isoformat(timespec="seconds"),
        "type": str(data.get("type", "manual")), "user": str(data.get("user", "")),
        "content": content, "tags": data.get("tags", []), "source": "manual",
    }
    path = memory_folder() / f"{now:%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f: f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return web.json_response({"ok": True, "memory": record})


def find_memory(memory_id: str) -> tuple[Path, int, list[str], dict[str, Any]] | None:
    for path in memory_folder().glob("*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            try: record = json.loads(line)
            except json.JSONDecodeError: continue
            candidate = str(record.get("id") or f"legacy:{path.name}:{index}")
            if candidate == memory_id: return path, index, lines, record
    return None


async def update_memory(request: web.Request) -> web.Response:
    found = find_memory(request.match_info["memory_id"])
    if not found: raise web.HTTPNotFound(text='{"error":"记忆不存在或已被删除"}', content_type="application/json")
    path, index, lines, record = found; data = await json_body(request)
    for key in ("type", "user", "content", "message", "reply", "tags"):
        if key in data: record[key] = data[key]
    if not str(record.get("content") or record.get("message") or "").strip():
        raise web.HTTPBadRequest(text='{"error":"记忆内容不能为空"}', content_type="application/json")
    record.setdefault("id", uuid.uuid4().hex); record["updated_at"] = datetime.now().isoformat(timespec="seconds")
    lines[index] = json.dumps(record, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return web.json_response({"ok": True, "memory": record})


async def delete_memory(request: web.Request) -> web.Response:
    found = find_memory(request.match_info["memory_id"])
    if not found: raise web.HTTPNotFound(text='{"error":"记忆不存在或已被删除"}', content_type="application/json")
    path, index, lines, _ = found; lines.pop(index)
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return web.json_response({"ok": True})


async def put_docs(request: web.Request) -> web.Response:
    data = await json_body(request); folder = ROOT / read_config().get("workspace", {}).get("path", "workspace")
    folder.mkdir(parents=True, exist_ok=True)
    for key, name in DOCS.items():
        if key in data: (folder / name).write_text(str(data[key]), encoding="utf-8")
    return web.json_response({"ok": True})


def running() -> bool:
    return assistant_process is not None and assistant_process.poll() is None


async def status(_: web.Request) -> web.Response:
    svc = False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(read_config().get("tts", {}).get("health_url", "http://127.0.0.1:9879/api/options"), timeout=aiohttp.ClientTimeout(total=1.5)) as r: svc = r.status == 200
    except Exception: pass
    return web.json_response({"assistant": running(), "svc": svc, "pid": assistant_process.pid if running() else None})


async def start_assistant(_: web.Request) -> web.Response:
    global assistant_process, assistant_log
    if running(): return web.json_response({"ok": True, "message": "直播助手已在运行"})
    cfg = read_config()
    if int(cfg.get("app", {}).get("room_id", 0)) <= 0:
        raise web.HTTPBadRequest(text='{"error":"请先设置有效的B站直播间号"}', content_type="application/json")
    (ROOT / "logs").mkdir(exist_ok=True)
    assistant_log = (ROOT / "logs" / "assistant.log").open("a", encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    assistant_process = subprocess.Popen([sys.executable, "-m", "modules.live.main", "--config", str(CONFIG)], cwd=ROOT, stdout=assistant_log, stderr=subprocess.STDOUT, creationflags=flags)
    await asyncio.sleep(.4)
    if assistant_process.poll() is not None: raise web.HTTPInternalServerError(text='{"error":"启动失败，请查看 logs/assistant.log"}', content_type="application/json")
    return web.json_response({"ok": True, "pid": assistant_process.pid})


async def stop_assistant(_: web.Request) -> web.Response:
    global assistant_process, assistant_log
    if running():
        if os.name == "nt": subprocess.run(["taskkill", "/PID", str(assistant_process.pid), "/T", "/F"], capture_output=True)
        else: assistant_process.terminate()
    assistant_process = None
    if assistant_log: assistant_log.close(); assistant_log = None
    return web.json_response({"ok": True})


async def test_tts(request: web.Request) -> web.Response:
    from .ai_live_assistant.tts import TTSClient
    text = str((await json_body(request)).get("text", "你好，欢迎来到直播间"))[:80]
    cfg = read_config()
    async with aiohttp.ClientSession() as session:
        client = TTSClient(session, cfg["tts"], ROOT / "audio")
        path = await client.speak(text)
    return web.json_response({"ok": True, "file": path.name if path else None})


async def test_llm(request: web.Request) -> web.Response:
    from dotenv import dotenv_values
    from .ai_live_assistant.llm import LLMClient
    for k, v in dotenv_values(ENV_FILE).items():
        if v: os.environ[k] = v
    cfg = read_config(); text = str((await json_body(request)).get("text", "用一句话欢迎新观众"))[:200]
    async with aiohttp.ClientSession() as session:
        answer = await LLMClient(session, cfg["llm"]).reply([{"role": "user", "content": text}])
    return web.json_response({"ok": True, "answer": answer})


@web.middleware
async def errors(request: web.Request, handler):
    try: return await handler(request)
    except web.HTTPException: raise
    except Exception as exc: return web.json_response({"error": str(exc)}, status=500)


def create_app() -> web.Application:
    app = web.Application(middlewares=[errors])
    app.add_routes([
        web.get("/api/config", get_config), web.put("/api/config", put_config),
        web.get("/api/secrets", get_secrets), web.put("/api/secrets", put_secrets),
        web.get("/api/docs", get_docs), web.put("/api/docs", put_docs),
        web.get("/api/messages", get_messages),
        web.get("/api/memories", get_memories), web.post("/api/memories", create_memory),
        web.put(r"/api/memories/{memory_id:.+}", update_memory), web.delete(r"/api/memories/{memory_id:.+}", delete_memory),
        web.get("/api/character", get_character), web.put("/api/character", update_character), web.post("/api/character/images", upload_character_image),
        web.get(r"/api/character/image/{filename}", get_character_image), web.delete(r"/api/character/images/{image_id}", delete_character_image),
        web.get("/api/status", status), web.post("/api/assistant/start", start_assistant), web.post("/api/assistant/stop", stop_assistant),
        web.post("/api/test/tts", test_tts), web.post("/api/test/llm", test_llm),
        web.get("/", lambda _: web.FileResponse(WEB / "index.html")), web.static("/assets", WEB),
    ])
    return app


def manager_is_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:9888/api/status", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> None:
    ensure_files()
    if manager_is_running():
        print("AI Live Console is already running. Opening the existing page...")
        webbrowser.open("http://127.0.0.1:9888")
        return
    webbrowser.open("http://127.0.0.1:9888")
    web.run_app(create_app(), host="127.0.0.1", port=9888, print=lambda x: print(x))


if __name__ == "__main__": main()
