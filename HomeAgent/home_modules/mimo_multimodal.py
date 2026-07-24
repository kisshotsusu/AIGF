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
        choice = data["choices"][0]
        content = str(choice["message"].get("content") or "").strip()
        if not content:
            raise RuntimeError(
                "MiMo returned an empty response"
                f" (finish_reason={choice.get('finish_reason', 'unknown')})"
            )
        return content

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
        compact_evidence = self._compact_completion_evidence(evidence)
        prompt = (
            "你是独立任务完成核验器。只根据工具证据判断任务是否真正完成，不能依据助手的口头声明。"
            "先按任务计划区分类型：observe、查询、读取、分析等只读任务，只要成功的工具证据已取得用户所问信息，"
            "就应判定完成，不得额外要求被观察对象达到终态；点击、输入、播放、提交、修改等操作任务，"
            "没有成功状态、终态字段或可验证观察时必须判定失败。证据包含 task_submitted_at、tool_submitted_at、"
            "tool_completed_at 和 tool_sequence；必须按 tool_sequence/完成时间判断新旧，同一对象的较新状态覆盖较早状态，"
            "不得用操作前或分析耗时期间已经过期的窗口/进程状态否定较新的终态证据。视觉分析只代表其截图采集时刻，"
            "返回较晚不等于画面仍然新鲜。媒体停止命令若明确为 idempotent、requested_state=stopped 且已成功送达，"
            "不得要求用可反转的播放切换键再次确认。核验音乐播放时，窗口标题是否变化不是成功条件；应以任务提交后"
            "最新的窗口视觉分析为准。视觉分析若同时确认目标歌曲名称和正在播放状态（例如暂停按钮、播放进度或播放详情），"
            "即可作为终态证据；目标在任务开始时已经播放也视为幂等完成，不得为了制造标题变化而停止、重播或重复双击。"
            "若较新的停止、暂停或其他歌曲证据覆盖了该状态，则必须判定失败。只输出 JSON："
            '{"passed":true或false,"reason":"简短依据","next_action":"失败时给出下一步工具动作"}。\n'
            f"用户任务：{task}\n任务计划：{json.dumps(plan, ensure_ascii=False)}\n候选回复：{answer}\n工具证据：{compact_evidence}"
        )
        payload = {
            "model": self.config["completion_model"],
            "messages": [{"role": "system", "content": "只输出合法 JSON，不要 Markdown。"}, {"role": "user", "content": prompt}],
            "temperature": 0,
            "max_completion_tokens": max(500, min(2048, int(self.config["max_completion_tokens"]))),
            "thinking": {"type": "disabled"},
            "stream": False,
        }
        content = await self._post(session, payload)
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise RuntimeError(f"MiMo 完成检查返回了非 JSON 内容：{content[:300]}")
        result = json.loads(match.group(0))
        if not isinstance(result, dict) or not isinstance(result.get("passed"), bool):
            raise RuntimeError("MiMo 完成检查的 passed 必须是 JSON boolean")
        return {"passed": result["passed"], "reason": str(result.get("reason") or "未提供原因"), "next_action": str(result.get("next_action") or "")}

    @staticmethod
    def _compact_completion_evidence(
        evidence: list[dict[str, Any]],
        *,
        max_chars: int = 18000,
    ) -> str:
        """Compact newest-first so long old observations cannot hide the final state."""
        def compact(value: Any, depth: int = 0) -> Any:
            if depth >= 6:
                return str(value)[:300]
            if isinstance(value, str):
                return value if len(value) <= 1600 else value[:1599] + "…"
            if isinstance(value, dict):
                return {str(key): compact(item, depth + 1) for key, item in value.items()}
            if isinstance(value, list):
                return [compact(item, depth + 1) for item in value[-12:]]
            return value

        selected_newest: list[dict[str, Any]] = []
        used = 2
        for item in reversed(evidence[-30:]):
            candidate = compact(item)
            encoded = json.dumps(candidate, ensure_ascii=False, default=str)
            if selected_newest and used + len(encoded) + 1 > max_chars:
                continue
            if not selected_newest and len(encoded) + 2 > max_chars:
                result = item.get("result") if isinstance(item, dict) else {}
                result = result if isinstance(result, dict) else {}
                candidate = {
                    "tool": str(item.get("tool") or "") if isinstance(item, dict) else "",
                    "result": {
                        "status": str(result.get("status") or ""),
                        "tool_sequence": result.get("tool_sequence"),
                        "summary": encoded[: max(200, max_chars - 300)],
                    },
                }
                encoded = json.dumps(candidate, ensure_ascii=False, default=str)
            selected_newest.append(candidate)
            used += len(encoded) + 1
        return json.dumps(list(reversed(selected_newest)), ensure_ascii=False, default=str)
