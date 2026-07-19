from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from src.ai_live_assistant.tts import cleanup_audio_files
CONFIG = ROOT / "config.yaml"
IDENTITY = ROOT / "workspace" / "IDENTITY.yaml"
PRESET_VOICES = {"mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def choose_voice(config: dict, identity: dict, requested: str = "") -> str:
    singing = config.get("singing", {})
    voice = requested.strip() or str(singing.get("voice", "")).strip()
    if voice:
        return voice
    speaker = str(config.get("tts", {}).get("speaker", "")).strip()
    if speaker in PRESET_VOICES:
        return speaker
    gender = str(identity.get("character", {}).get("gender", "")).lower()
    if any(word in gender for word in ("男", "male", "man")):
        return str(singing.get("male_voice", "苏打"))
    return str(singing.get("female_voice", "冰糖"))


def normalize_lyrics(text: str) -> str:
    lines = [line.strip() for line in text.replace("。", "。\n").splitlines() if line.strip()]
    lines = lines[:10]
    if not lines:
        raise ValueError("演唱文本不能为空")
    return "\n".join(lines)


def synthesize(song: str, lyrics: str, style: str, voice: str = "") -> dict:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    identity = yaml.safe_load(IDENTITY.read_text(encoding="utf-8")) if IDENTITY.exists() else {}
    singing = config.get("singing", {})
    load_env(ROOT / ".env")
    key = os.getenv(str(singing.get("api_key_env", "MIMO_API_KEY")), "").strip()
    if not key:
        raise RuntimeError("缺少 MIMO_API_KEY，请先在角色管理器或 .env 中配置")
    selected_voice = choose_voice(config, identity, voice)
    character = identity.get("character", {})
    direction = (
        f"演唱一段关于《{song}》的短歌。风格：{style or '自然、富有感情'}。"
        f"角色是{character.get('name', '角色')}，性格为{character.get('personality', '自然亲切')}。"
        "保持旋律感、吐字清楚，按每行一句演唱。"
    )
    payload = {
        "model": str(singing.get("model", "mimo-v2.5-tts")),
        "messages": [
            {"role": "user", "content": direction},
            {"role": "assistant", "content": "(唱歌)" + normalize_lyrics(lyrics)},
        ],
        "audio": {"format": "wav", "voice": selected_voice},
    }
    base_url = str(singing.get("base_url", "https://api.xiaomimimo.com/v1")).rstrip("/")
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "api-key": key, "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(singing.get("timeout_seconds", 180))) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"MiMo TTS HTTP {exc.code}: {detail[:1000]}") from exc
    audio_data = result["choices"][0]["message"]["audio"]["data"]
    output_dir = ROOT / str(singing.get("output_dir", "audio")); output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"singing_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
    output.write_bytes(base64.b64decode(audio_data))
    cleanup_audio_files(output_dir, 20)
    played = False
    if singing.get("play_audio", True) and os.name == "nt":
        import winsound
        winsound.PlaySound(str(output), winsound.SND_FILENAME | winsound.SND_ASYNC)
        played = True
    return {"ok": True, "path": str(output), "voice": selected_voice, "lines": len(normalize_lyrics(lyrics).splitlines()), "played": played}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--song", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--lyrics")
    group.add_argument("--lyrics-file")
    parser.add_argument("--style", default="")
    parser.add_argument("--voice", default="")
    args = parser.parse_args()
    lyrics = args.lyrics if args.lyrics is not None else Path(args.lyrics_file).read_text(encoding="utf-8")
    print(json.dumps(synthesize(args.song, lyrics, args.style, args.voice), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
