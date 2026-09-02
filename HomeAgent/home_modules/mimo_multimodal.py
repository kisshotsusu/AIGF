from __future__ import annotations

import asyncio
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
        # DeepSeek 图像识别配置：DeepSeek 官方 API（deepseek-chat）是纯文本模型，
        # 官方推荐“视觉代理”方案——先由视觉模型把图片转成文字描述，再交给 DeepSeek 推理。
        "deepseek_image_enabled": False,
        "deepseek_image_model": "deepseek-chat",
        "deepseek_image_base_url": "https://api.deepseek.com",
        "deepseek_image_api_key_env": "DEEPSEEK_API_KEY",
        # 视觉代理：未显式配置时复用本客户端的 MiMo 图片模型配置。
        "deepseek_image_proxy_enabled": True,
        "deepseek_image_proxy_base_url": "",
        "deepseek_image_proxy_api_key_env": "",
        "deepseek_image_proxy_model": "",
        "deepseek_image_proxy_auth_header": "",
        "deepseek_image_proxy_max_tokens_field": "",
        "deepseek_image_proxy_extra_body": {},
    }

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**self.DEFAULTS, **(config or {})}

    def _key(self, env_key: str = None) -> str:
        key_env = env_key or self.config["api_key_env"]
        key = os.getenv(str(key_env), "").strip()
        if not key:
            raise RuntimeError(f"未配置 {key_env}，请在角色管理器中填写 API Key")
        return key

    async def _post(self, session: aiohttp.ClientSession, payload: dict[str, Any], base_url: str = None, api_key_env: str = None, auth_header: str = "api-key") -> str:
        url = str(base_url or self.config["base_url"]).rstrip("/") + "/chat/completions"
        key = self._key(api_key_env)
        headers = {auth_header: self._auth_value(auth_header, key), "Content-Type": "application/json"}
        async with session.post(url, json=payload, headers=headers) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {raw[:600]}")
        data = json.loads(raw)
        choice = data["choices"][0]
        content = str(choice["message"].get("content") or "").strip()
        if not content:
            raise RuntimeError(
                "API returned an empty response"
                f" (finish_reason={choice.get('finish_reason', 'unknown')})"
            )
        return content

    @staticmethod
    def _auth_value(auth_header: str, key: str) -> str:
        """OpenAI 风格接口统一用 Bearer 前缀，MiMo 的 api-key 头保持原样。"""
        if auth_header.lower() == "authorization" and not str(key).lower().startswith("bearer "):
            return f"Bearer {key}"
        return key

    @staticmethod
    def _looks_like_mimo(base_url: str = "", model: str = "") -> bool:
        return "xiaomimimo" in str(base_url).lower() or str(model).lower().startswith("mimo-")

    async def analyze_image(self, session: aiohttp.ClientSession, image_path: Path, prompt: str) -> dict[str, Any]:
        """使用MiMo分析图片"""
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

    async def analyze_image_with_deepseek(self, session: aiohttp.ClientSession, image_path: Path, prompt: str) -> dict[str, Any]:
        """使用 DeepSeek 分析图片（视觉代理方案）。

        DeepSeek 官方 API 是纯文本模型，不能直接接收图片。按官方推荐做法：
        先用配置的视觉代理模型把图片转成准确文字描述，再把描述与用户问题
        一起交给 DeepSeek 推理，返回 DeepSeek 的回答。
        """
        if not self.config.get("enabled") or not self.config.get("deepseek_image_enabled"):
            raise RuntimeError("DeepSeek 图像理解未启用")
        path = image_path.resolve()
        if not path.is_file():
            raise RuntimeError(f"图片不存在：{path}")
        proxy = self._vision_proxy_config()
        question = str(prompt or "请准确描述图片内容。").strip()
        description = await self._describe_image_with_proxy(
            session, path,
            "请准确、详细地描述图片中的可见内容、文字、布局、颜色与关键细节，不要推测图片之外的任何信息。",
            proxy,
        )
        deepseek_prompt = (
            "你收到的是视觉模型对用户图片的文字描述，而不是图片本身。"
            "请基于这份描述回答用户的问题；描述中没有足够信息时，明确说明缺少什么，不要编造画面细节。\n\n"
            f"【视觉模型对图片的描述】\n{description}\n\n"
            f"【用户的问题】\n{question}"
        )
        payload = {
            "model": self.config["deepseek_image_model"],
            "messages": [{"role": "user", "content": deepseek_prompt}],
            "max_tokens": int(self.config["max_completion_tokens"]),
            "stream": False,
        }
        text = await self._post(
            session, payload,
            base_url=self.config["deepseek_image_base_url"],
            api_key_env=self.config["deepseek_image_api_key_env"],
            auth_header="Authorization"
        )
        return {
            "ok": True,
            "text": text,
            "model": self.config["deepseek_image_model"],
            "vision_model": proxy["model"],
            "description": description,
            "path": str(path),
            "provider": "deepseek",
        }

    def _vision_proxy_config(self) -> dict[str, Any]:
        """解析 DeepSeek 视觉代理配置；未显式设置时复用本项目 MiMo 图片模型。"""
        return {
            "enabled": bool(self.config.get("deepseek_image_proxy_enabled", True)),
            "base_url": str(self.config.get("deepseek_image_proxy_base_url") or "").strip() or str(self.config.get("base_url") or ""),
            "api_key_env": str(self.config.get("deepseek_image_proxy_api_key_env") or "").strip() or str(self.config.get("api_key_env") or "MIMO_API_KEY"),
            "model": str(self.config.get("deepseek_image_proxy_model") or "").strip() or str(self.config.get("image_model") or "mimo-v2.5"),
            "auth_header": str(self.config.get("deepseek_image_proxy_auth_header") or "").strip() or None,
            "max_tokens_field": str(self.config.get("deepseek_image_proxy_max_tokens_field") or "").strip() or None,
            "extra_body": self.config.get("deepseek_image_proxy_extra_body") or {},
        }

    async def _describe_image_with_proxy(self, session: aiohttp.ClientSession, image_path: Path, prompt: str, proxy: dict[str, Any]) -> str:
        """用视觉代理模型把图片转成文字描述（DeepSeek 官方 API 不支持直接输入图片）。"""
        if not proxy.get("enabled"):
            raise RuntimeError("DeepSeek 视觉代理未启用，无法把图片转换为文字描述")
        path = image_path.resolve()
        if not path.is_file():
            raise RuntimeError(f"图片不存在：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith("image/"):
            raise RuntimeError("文件不是可识别的图片")
        data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        base_url = str(proxy.get("base_url") or self.config["base_url"])
        is_mimo = self._looks_like_mimo(base_url, proxy.get("model", ""))
        auth_header = str(proxy.get("auth_header") or ("api-key" if is_mimo else "Authorization"))
        token_field = str(proxy.get("max_tokens_field") or ("max_completion_tokens" if is_mimo else "max_tokens"))
        payload: dict[str, Any] = {
            "model": proxy["model"],
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt or "请准确描述图片内容。"},
            ]}],
            token_field: int(self.config.get("max_completion_tokens", 1024)),
        }
        extra = proxy.get("extra_body") or {}
        if isinstance(extra, dict):
            for option_key, option_value in extra.items():
                payload.setdefault(str(option_key), option_value)
        return await self._post(
            session, payload,
            base_url=base_url,
            api_key_env=proxy.get("api_key_env") or self.config["api_key_env"],
            auth_header=auth_header,
        )

    async def analyze_image_auto(self, session: aiohttp.ClientSession, image_path: Path, prompt: str,
                                 allow_deepseek: bool | None = None) -> dict[str, Any]:
        """自动选择可用的图像分析服务（优先 DeepSeek 视觉代理，其次 MiMo）。

        allow_deepseek: None=沿用配置 deepseek_image_enabled；False=跳过 DeepSeek 两段代理，
        直接走 MiMo 单段（更快，适合对延迟敏感的屏幕/窗口实时观察）；
        True=即使配置关闭也尝试 DeepSeek 代理。返回结果附带实际使用的 provider。
        """
        use_deepseek = (
            bool(self.config.get("deepseek_image_enabled"))
            if allow_deepseek is None else bool(allow_deepseek)
        )
        failures: list[str] = []
        if use_deepseek:
            try:
                return await self.analyze_image_with_deepseek(session, image_path, prompt)
            except Exception as exc:
                failures.append(f"DeepSeek: {exc}")
        if self.config.get("image_enabled"):
            retries = max(0, int(self.config.get("image_retries", 1)))
            last: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    result = await self.analyze_image(session, image_path, prompt)
                    result["provider"] = "mimo"
                    if attempt:
                        result["retried"] = True
                    return result
                except Exception as exc:
                    last = exc
                    # 空响应/HTTP 5xx/连接类瞬态故障值得立刻重试一次; 本地参数错误无需重试。
                    text = str(exc).lower()
                    transient = (
                        "empty response" in text or "length" in text
                        or "http 5" in text or "http 429" in text
                        or "connection" in text or "timed out" in text or "timeout" in text
                        or "reset" in text or "server" in text
                    )
                    if attempt < retries and transient:
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    failures.append(f"MiMo: {exc}")
                    break
            if last is not None and not failures:
                failures.append(f"MiMo: {last}")
        detail = "；".join(failures) if failures else "未启用任何图像分析服务"
        raise RuntimeError(f"没有可用的图像分析服务：{detail}")

    async def describe_images_for_chat(self, session: aiohttp.ClientSession, image_paths, user_text: str) -> list[dict[str, Any]]:
        """为 DeepSeek 等纯文本模型构造图片消息。

        先用视觉代理把每张图片转成文字描述，再与用户问题合并为 text-only 内容数组，
        让 DeepSeek 在没有多模态输入的情况下也能“看图”回答问题。
        """
        values = [image_paths] if isinstance(image_paths, (str, Path)) else list(image_paths or [])
        if not values:
            raise ValueError("没有可提交的图片")
        proxy = self._vision_proxy_config()
        if not proxy.get("enabled"):
            raise RuntimeError("视觉代理未启用，无法把图片转换为文字描述")
        descriptions: list[str] = []
        for index, value in enumerate(values, start=1):
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"粘贴的图片不存在：{path}")
            description = await self._describe_image_with_proxy(
                session, path,
                "请准确、详细地描述这张图片中的可见内容、文字、布局与关键细节，不要推测图片之外的信息。",
                proxy,
            )
            descriptions.append(f"[图片 {index}] {path.name}：{description}")
        prompt = str(user_text or "").strip()
        body = (
            "用户附带了图片，图片已由视觉模型识别为文字描述。请基于以下描述回答用户的问题，"
            "不要声称直接看到了图片本身。\n\n" + "\n\n".join(descriptions)
        )
        body += f"\n\n用户问题：{prompt}" if prompt else "\n\n用户问题：请根据图片描述回答。"
        return [{"type": "text", "text": body}]

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
            raise RuntimeError("MiMo 完成检查未启用")
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
