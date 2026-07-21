from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import aiohttp


class MiMoMultimodalClient:
    """Small OpenAI-compatible MiMo client for vision, ASR and completion checks."""

    DEFAULTS = {
        "enabled": True,
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key_env": "MIMO_API_KEY",
        "image_enabled": True,
        "image_model": "mimo-v2.5",
        "speech_enabled": True,
        "speech_model": "mimo-v2.5-asr",
        "speech_language": "auto",
        "completion_check_enabled": True,
        "completion_model": "mimo-v2.5",
        "completion_max_retries": 2,
        "timeout_seconds": 60,
        "max_completion_tokens": 1024,
        "fail_closed": True,
    }

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**self.DEFAULTS, **(config or {})}

    def _key(self) -> str:
        key = os.getenv(str(self.config["api_key_env"]), "").strip()
        if not key:
            raise RuntimeError(f"未配置 {self.config['api_key_env']}，请在角色管理器的 MiMo 多模态页面填写 API Key")
        return key

    async def _post(self, session: aiohttp.ClientSession, payload: dict[str, Any]) -> str:
        url = str(self.config["base_url"]).rstrip("/") + "/chat/completions"
        async with session.post(url, json=payload, headers={"api-key": self._key(), "Content-Type": "application/json"}) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"MiMo HTTP {response.status}: {raw[:600]}")
        data = json.loads(raw)
        return str(data["choices"][0]["message"].get("content") or "").strip()

    async def analyze_image(self, session: aiohttp.ClientSession, image_path: Path, prompt: str) -> dict[str, Any]:
        if not self.config.get("enabled") or not self.config.get("image_enabled"):
            raise RuntimeError("MiMo 图像理解未启用")
        path = image_path.resolve()
        if not path.is_file():
            raise RuntimeError(f"图片不存在：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            raise RuntimeError("文件不是可识别的图片")
        data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        payload = {
            "model": self.config["image_model"],
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt or "请准确描述图片内容。"},
            ]}],
            "max_completion_tokens": int(self.config["max_completion_tokens"]),
        }
        return {"ok": True, "text": await self._post(session, payload), "model": self.config["image_model"], "path": str(path)}

    async def transcribe_audio(self, session: aiohttp.ClientSession, audio_path: Path, language: str = "auto") -> dict[str, Any]:
        if not self.config.get("enabled") or not self.config.get("speech_enabled"):
            raise RuntimeError("MiMo 语音识别未启用")
        path = audio_path.resolve()
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in {".wav", ".mp3"}:
            raise RuntimeError("MiMo 语音识别仅接受存在的 WAV 或 MP3 文件")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if len(encoded.encode("ascii")) > 10 * 1024 * 1024:
            raise RuntimeError("音频 Base64 编码后超过 MiMo 10 MB 上限")
        mime = "audio/mpeg" if suffix == ".mp3" else "audio/wav"
        payload = {
            "model": self.config["speech_model"],
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": f"data:{mime};base64,{encoded}"}}]}],
            "asr_options": {"language": language or self.config.get("speech_language", "auto")},
            "max_completion_tokens": int(self.config["max_completion_tokens"]),
        }
        return {"ok": True, "text": await self._post(session, payload), "model": self.config["speech_model"], "path": str(path)}

    async def verify_completion(self, session: aiohttp.ClientSession, task: str, plan: dict[str, Any], answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.config.get("enabled") or not self.config.get("completion_check_enabled"):
            return {"passed": True, "reason": "完成检查已关闭", "next_action": ""}
        compact_evidence = json.dumps(evidence[-12:], ensure_ascii=False, default=str)[:12000]
        prompt = (
            "你是独立任务完成核验器。只根据工具证据判断任务是否真正完成，不能依据助手的口头声明。"
            "操作任务没有成功状态、终态字段或可验证观察时必须判定失败。只输出 JSON："
            '{"passed":true或false,"reason":"简短依据","next_action":"失败时给出下一步工具动作"}。\n'
            f"用户任务：{task}\n任务计划：{json.dumps(plan, ensure_ascii=False)}\n候选回复：{answer}\n工具证据：{compact_evidence}"
        )
        payload = {
            "model": self.config["completion_model"],
            "messages": [{"role": "system", "content": "只输出合法 JSON，不要 Markdown。"}, {"role": "user", "content": prompt}],
            "temperature": 0,
            "max_completion_tokens": 500,
        }
        content = await self._post(session, payload)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise RuntimeError(f"MiMo 完成检查返回了非 JSON 内容：{content[:300]}")
        result = json.loads(match.group(0))
        return {"passed": bool(result.get("passed")), "reason": str(result.get("reason") or "未提供原因"), "next_action": str(result.get("next_action") or "")}
