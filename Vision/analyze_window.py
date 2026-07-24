from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
from pathlib import Path
from datetime import datetime

import aiohttp
import yaml
from dotenv import dotenv_values

import agent as vision_agent


ROOT = Path(__file__).resolve().parents[1]


async def analyze(title: str, prompt: str, request_submitted_at: str = "") -> dict:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    settings = config.get("image_understanding", {})
    key_name = str(settings.get("api_key_env", "MIMO_API_KEY"))
    env = dotenv_values(ROOT / ".env")
    key = str(env.get(key_name) or os.getenv(key_name) or "").strip()
    if not key:
        raise RuntimeError(f"Missing {key_name}")
    window = vision_agent._find_window(title)
    image = vision_agent._capture_window_info(window)
    captured_time = datetime.now().astimezone()
    captured_at = captured_time.isoformat(timespec="milliseconds")
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
    completed_time = datetime.now().astimezone()
    completion_title = vision_agent._window_title_by_hwnd(int(window["hwnd"]))
    visual_change_ratio = 0.0
    visual_mean_delta = 0.0
    state_changed_during_analysis = bool(
        completion_title and completion_title != str(window.get("title") or "")
    )
    try:
        current_image = vision_agent._capture_window_info(window)
        change = vision_agent._visual_change_evidence(
            image, current_image, str(window.get("title") or ""), completion_title,
        )
        visual_change_ratio = float(change.get("visual_change_ratio") or 0.0)
        visual_mean_delta = float(change.get("visual_mean_delta") or 0.0)
        # Ignore tiny progress/animation changes. A title change or substantial
        # layout change means the model described a frame that is no longer current.
        state_changed_during_analysis = bool(
            state_changed_during_analysis
            or visual_change_ratio >= 0.08
            or visual_mean_delta >= 0.04
        )
        current_image.close()
    except Exception:
        # HomeAgent can still expire this evidence from its timestamps.
        pass
    image.close()
    analysis_age_ms = max(0, int((completed_time - captured_time).total_seconds() * 1000))
    return {
        "ok": True, "analysis": text, "window": title,
        "window_at_capture": {
            "hwnd": window.get("hwnd"),
            "pid": window.get("pid"),
            "title": window.get("title"),
            "process_name": window.get("process_name"),
        },
        "title_at_completion": completion_title,
        "request_submitted_at": request_submitted_at,
        "screenshot_captured_at": captured_at,
        "analysis_completed_at": completed_time.isoformat(timespec="milliseconds"),
        "analysis_age_ms": analysis_age_ms,
        "visual_change_ratio_during_analysis": visual_change_ratio,
        "visual_mean_delta_during_analysis": visual_mean_delta,
        "stale": state_changed_during_analysis,
        "stale_reason": (
            "截图后目标窗口状态发生明显变化，已废弃本次识别内容"
            if state_changed_during_analysis else ""
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--request-submitted-at", default="")
    args = parser.parse_args()
    try:
        result = asyncio.run(analyze(args.title, args.prompt, args.request_submitted_at))
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
