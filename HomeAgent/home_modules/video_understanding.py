from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import aiohttp


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.6-flash"
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5"


def parse_time(value: Any) -> float | None:
    """Parse a timestamp into seconds.

    Accepts float seconds, "SS", "MM:SS", "HH:MM:SS", optional fractional
    seconds, or numeric strings.  Returns None when the value is not a valid
    timestamp so callers can apply their own fallback rules.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+):([0-5]?\d)(?::([0-5]?\d(?:\.\d+)?))?", text)
    if match:
        if match.group(3) is None:
            # 两段时间按视频时间轴惯例解释为 MM:SS（1:02 = 62 秒），
            # 而不是 HH:MM，避免“01:02 - 01:05”被误判成 1 小时以上。
            minutes = int(match.group(1))
            seconds = float(match.group(2))
            return max(0.0, minutes * 60 + seconds)
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        return max(0.0, hours * 3600 + minutes * 60 + seconds)
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return None


def _fmt_hms(seconds: float) -> str:
    value = max(0.0, float(seconds))
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    secs = int(value % 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _srt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(millis, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _first_value(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def normalize_event(raw: Any, index: int, previous_end: float | None = None,
                    fallback_duration: float | None = None) -> dict[str, Any] | None:
    """Normalize one event/segment dict into the canonical timeline shape."""
    if not isinstance(raw, dict):
        return None
    start = parse_time(_first_value(
        raw, ("start_time", "start", "start_seconds", "start_sec", "from", "begin")
    ))
    end = parse_time(_first_value(
        raw, ("end_time", "end", "end_seconds", "end_sec", "to", "until")
    ))
    text = str(_first_value(
        raw, ("event", "text", "content", "description", "caption", "subtitle"), ""
    )).strip()
    if start is None:
        start = previous_end if previous_end is not None else 0.0
    if end is None or end <= start:
        if fallback_duration is not None:
            end = min(start + 5.0, max(start, float(fallback_duration)))
        else:
            end = start + 5.0
    if end <= start:
        end = start + 1.0
    if not text:
        text = f"片段 {index + 1}"
    return {
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "start_time": _fmt_hms(start),
        "end_time": _fmt_hms(end),
        "event": text,
    }


def _extract_json(text: str) -> Any:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    candidates: list[str] = []
    for opener in ("[", "{"):
        idx = cleaned.find(opener)
        if idx >= 0:
            candidates.append(cleaned[idx:])
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


_LINE_TIMELINE_RE = re.compile(
    r"^\s*\[?([0-9:.]+)\s*[-–~→,，]\s*([0-9:.]+)\]?\s*[:：]?\s*(.+?)\s*$"
)


def _parse_timeline_lines(text: str, fallback_duration: float | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous_end: float | None = None
    index = 0
    for line in str(text or "").splitlines():
        match = _LINE_TIMELINE_RE.match(line)
        if not match:
            continue
        start = parse_time(match.group(1))
        end = parse_time(match.group(2))
        if start is None or end is None or end <= start:
            continue
        index += 1
        event = normalize_event(
            {
                "start_time": start,
                "end_time": end,
                "event": match.group(3),
            },
            index - 1,
            previous_end,
            fallback_duration,
        )
        if event is not None:
            previous_end = event["end_seconds"]
            events.append(event)
    return events


def parse_events(content: Any, fallback_duration: float | None = None) -> list[dict[str, Any]]:
    """Parse Qwen video-understanding output into a normalized event timeline."""
    if not content:
        return []
    data = _extract_json(str(content))
    raw_items: Any = None
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        for key in ("events", "timeline", "segments", "clips", "items"):
            value = data.get(key)
            if isinstance(value, list):
                raw_items = value
                break
        if raw_items is None and data:
            raw_items = [data]
    if not isinstance(raw_items, list):
        return _parse_timeline_lines(str(content), fallback_duration)
    events: list[dict[str, Any]] = []
    previous_end: float | None = None
    for index, raw in enumerate(raw_items):
        event = normalize_event(raw, index, previous_end, fallback_duration)
        if event is None:
            continue
        previous_end = event["end_seconds"]
        events.append(event)
    events.sort(key=lambda item: item["start_seconds"])
    return events


class VideoUnderstandingClient:
    """视频理解客户端：默认 MiMo V2.5，可选 DashScope Qwen 兜底。

    MiMo 与 Qwen 都走 OpenAI 兼容接口，只是认证头、token 字段和 extra_body
    不同；通过 `provider` / `fallback_provider` 切换，首个成功的 provider 胜出。
    """

    PROVIDERS: dict[str, dict[str, Any]] = {
        "mimo": {
            "base_url": MIMO_BASE_URL,
            "api_key_env": "MIMO_API_KEY",
            "model": MIMO_MODEL,
            "auth_header": "api-key",
            "max_tokens_field": "max_completion_tokens",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "qwen": {
            "base_url": DEFAULT_BASE_URL,
            "api_key_env": "DASHSCOPE_API_KEY",
            "model": DEFAULT_MODEL,
            "auth_header": "Authorization",
            "max_tokens_field": "max_completion_tokens",
            "extra_body": {},
        },
    }

    DEFAULTS: dict[str, Any] = {
        "enabled": True,
        "provider": "mimo",
        "fallback_provider": "qwen",
        "fps": 2.0,
        "max_completion_tokens": 4096,
        "timeout_seconds": 180,
        "max_video_mb": 500,
        "output_dir": "outputs/video_understanding",
    }

    DEFAULT_PROMPT = (
        "请按时间顺序分析视频内容，找出每个可独立成段的事件，"
        '输出 JSON：{"events": [{"start_time": "HH:mm:ss", '
        '"end_time": "HH:mm:ss", "event": "事件描述"}]}。'
        "只输出 JSON，不要输出解释或 Markdown。"
    )

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**self.DEFAULTS, **(config or {})}

    def _key(self, env_key: str | None = None) -> str:
        key_env = env_key or str(self.config.get("api_key_env") or "MIMO_API_KEY")
        key = os.getenv(key_env, "").strip()
        if not key:
            raise RuntimeError(f"未配置 {key_env}，请在 .env 或角色管理器中填写 API Key")
        return key

    def _provider_meta(self, name: str) -> dict[str, Any]:
        """返回某 provider 的生效配置；顶层显式配置优先于内置预设。"""
        meta = dict(self.PROVIDERS.get(str(name or "").strip().lower()) or self.PROVIDERS["qwen"])
        for key in ("base_url", "api_key_env", "model", "auth_header", "max_tokens_field", "extra_body"):
            value = self.config.get(key)
            if value not in (None, ""):
                meta[key] = value
        return meta

    def _resolve_video(self, video: str) -> tuple[str, Path | None]:
        value = str(video or "").strip()
        if not value:
            raise ValueError("视频参数不能为空")
        if value.lower().startswith(("http://", "https://")):
            return value, None
        path = Path(value).expanduser()
        if not path.is_absolute():
            base = Path(self.config.get("project_root") or Path.cwd())
            path = (base / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"视频文件不存在：{path}")
        max_bytes = int(float(self.config.get("max_video_mb", 500)) * 1024 * 1024)
        if path.stat().st_size > max_bytes:
            size_mb = path.stat().st_size / 1048576
            raise ValueError(
                f"视频大小 {size_mb:.1f} MB 超过限制 "
                f"{max_bytes / 1048576:.0f} MB"
            )
        mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
        if not str(mime).startswith("video/"):
            raise ValueError(f"文件不是视频：{path}")
        data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return data_url, path

    def _build_payload(self, video_url: str, prompt: str, fps: float,
                       model: str, token_field: str = "max_completion_tokens",
                       extra_body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": video_url}, "fps": fps},
                    {"type": "text", "text": prompt},
                ],
            }],
            "stream": False,
        }
        payload[str(token_field)] = int(self.config.get("max_completion_tokens", 4096))
        if isinstance(extra_body, dict):
            for option_key, option_value in extra_body.items():
                payload.setdefault(str(option_key), option_value)
        return payload

    async def analyze(self, session: aiohttp.ClientSession, video: str,
                      prompt: str | None = None) -> dict[str, Any]:
        """分析本地视频或 http(s) URL，返回规范化事件时间轴。

        依次尝试 provider 与 fallback_provider，首个成功的 provider 胜出；
        全部失败时抛出汇总各 provider 原因的错误。
        """
        if not self.config.get("enabled"):
            raise RuntimeError("视频理解未启用")
        video_url, local_path = self._resolve_video(video)
        text = str(prompt or "").strip() or self.DEFAULT_PROMPT
        fps = max(0.1, min(10.0, float(self.config.get("fps", 2.0))))
        providers: list[str] = []
        for name in (self.config.get("provider"), self.config.get("fallback_provider")):
            name = str(name or "").strip().lower()
            if name and name not in providers:
                providers.append(name)
        if not providers:
            providers = ["mimo"]
        failures: list[str] = []
        for name in providers:
            try:
                meta = self._provider_meta(name)
                payload = self._build_payload(
                    video_url, text, fps,
                    str(meta["model"]),
                    str(meta.get("max_tokens_field") or "max_completion_tokens"),
                    meta.get("extra_body") or {},
                )
                url = str(meta["base_url"]).rstrip("/") + "/chat/completions"
                auth_header = str(meta.get("auth_header") or "Authorization")
                key = self._key(str(meta.get("api_key_env") or "MIMO_API_KEY"))
                headers: dict[str, str] = {"Content-Type": "application/json"}
                if auth_header.lower() == "authorization" and not str(key).lower().startswith("bearer "):
                    headers[auth_header] = f"Bearer {key}"
                else:
                    headers[auth_header] = key
                async with session.post(url, json=payload, headers=headers) as response:
                    raw = await response.text()
                    if response.status >= 400:
                        raise RuntimeError(f"HTTP {response.status}: {raw[:600]}")
                data = json.loads(raw)
                choice = data["choices"][0]
                content_text = str(choice["message"].get("content") or "").strip()
                if not content_text:
                    raise RuntimeError(
                        "API 返回空内容"
                        f"（finish_reason={choice.get('finish_reason', 'unknown')}）"
                    )
                duration: float | None = None
                if local_path is not None:
                    try:
                        duration = probe_video_duration(local_path)
                    except Exception:
                        duration = None
                events = parse_events(content_text, duration)
                return {
                    "ok": True,
                    "provider": name,
                    "model": payload["model"],
                    "source": str(video),
                    "event_count": len(events),
                    "events": events,
                    "raw_text": content_text[:20000],
                    "fps": fps,
                }
            except Exception as exc:
                failures.append(f"{name}: {exc}")
        detail = "；".join(failures) if failures else "未配置任何视频理解服务"
        raise RuntimeError(f"所有视频理解服务都失败：{detail}")

    async def analyze_async(self, video: str, prompt: str | None = None) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout_seconds", 120)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            return await self.analyze(session, video, prompt)


# 兼容 HomeAgent 早期自改代码的类名引用。
QwenVideoClient = VideoUnderstandingClient




def probe_video_duration(path: str | Path, ffprobe: str = "ffprobe") -> float | None:
    """用 ffprobe 读取视频时长（秒）；失败返回 None，不抛异常。"""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        return None
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=duration:format=duration",
                "-of", "json", str(source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            return None
        data = json.loads(completed.stdout.decode("utf-8", "replace") or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        value = float(stream.get("duration") or fmt.get("duration") or 0.0)
        return value if value > 0 else None
    except Exception:
        return None
