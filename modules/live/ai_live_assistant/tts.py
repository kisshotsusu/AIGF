from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp


AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}


def cleanup_audio_files(directory: Path, keep: int = 20) -> int:
    """删除最旧音频，使目录内的音频文件数不超过 keep。"""
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS: continue
        try: files.append((path.stat().st_mtime_ns, path.name.lower(), path))
        except OSError: continue
    files.sort(reverse=True)
    deleted = 0
    for _, _, path in files[max(0, int(keep)):]:
        try: path.unlink(); deleted += 1
        except OSError: continue
    return deleted


def _fill(value: Any, text: str, speaker: str) -> Any:
    if isinstance(value, str): return value.replace("{text}", text).replace("{speaker}", speaker)
    if isinstance(value, dict): return {k: _fill(v, text, speaker) for k, v in value.items()}
    if isinstance(value, list): return [_fill(v, text, speaker) for v in value]
    return value


def _tts_safe_text(text: str) -> str:
    """SVC 子进程使用 Windows GBK 时，移除 emoji 等不可编码字符。"""
    return str(text).encode("gbk", errors="ignore").decode("gbk").strip()


class TTSClient:
    def __init__(self, session: aiohttp.ClientSession, cfg: dict[str, Any], audio_dir: Path):
        self.session, self.cfg, self.audio_dir = session, cfg, audio_dir
        self.service_process: subprocess.Popen | None = None
        self._service_start_lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()
        self._cached_options: dict[str, Any] | None = None
        audio_dir.mkdir(parents=True, exist_ok=True)
        cleanup_audio_files(audio_dir, 20)

    async def _options(self) -> dict[str, Any] | None:
        url = self.cfg.get("health_url")
        if not url:
            return None
        try:
            timeout = max(2.0, float(self.cfg.get("health_timeout_seconds", 6)))
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200: return None
                value = await r.json()
                if isinstance(value, dict): self._cached_options = value
                return value
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def _service_reachable(self) -> bool:
        """A busy inference server may time out on /options but must not be started twice."""
        parsed = urlparse(str(self.cfg.get("url", "")))
        if not parsed.hostname: return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(parsed.hostname, port), timeout=1.5)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def ensure_service(self) -> dict[str, Any] | None:
        options = await self._options()
        if options is not None: return options
        if await self._service_reachable():
            # The existing process is accepting connections. Reuse cached model data
            # and let synthesis retry instead of launching another GPU-heavy service.
            if self._cached_options is not None: return self._cached_options
            raise TimeoutError("语音服务端口存在，但健康接口正忙")
        if not self.cfg.get("auto_start", False): return None
        async with self._service_start_lock:
            options = await self._options()
            if options is not None: return options
            if await self._service_reachable(): return self._cached_options
            command = self.cfg.get("start_command")
            if not command or not Path(command).exists():
                raise RuntimeError(f"找不到语音服务启动文件: {command}")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            args = ["cmd.exe", "/c", command] if os.name == "nt" else [command]
            self.service_process = subprocess.Popen(args, cwd=str(Path(command).parent), creationflags=flags)
            deadline = time.monotonic() + float(self.cfg.get("startup_timeout_seconds", 240))
            while time.monotonic() < deadline:
                if self.service_process.poll() is not None:
                    raise RuntimeError("语音服务启动失败，请检查 GPT-SoVITS 服务日志")
                await asyncio.sleep(1)
                options = await self._options()
                if options is not None: return options
            raise TimeoutError("等待 SVC 语音服务启动超时")

    async def _synthesize_once(self, text: str) -> Path | None:
        """只生成音频文件，不播放，供流水线提前推理后续分段。"""
        if not self.cfg.get("enabled", True): return None
        safe_text = _tts_safe_text(text)
        if not safe_text:
            logging.getLogger("ai_live").warning("回复只包含 TTS 不支持的字符，已跳过语音生成")
            return None
        if safe_text != text:
            logging.getLogger("ai_live").info("已移除 SVC/GBK 不支持的字符后生成语音")
        speaker = str(self.cfg.get("speaker", "default"))
        options = await self.ensure_service()
        request_json = _fill(self.cfg.get("request_json", {"text": "{text}"}), safe_text, speaker)
        if self.cfg.get("svc_auto_options", False):
            if not options:
                raise RuntimeError("无法读取 SVC 的模型和参考音频选项")
            models, references = options.get("models", []), options.get("references", [])
            model = str(self.cfg.get("model", ""))
            reference = str(self.cfg.get("reference", ""))
            if not model and models: model = str(models[0]["id"])
            if not reference and references: reference = str(references[0]["path"])
            if not model or not reference:
                raise RuntimeError("SVC 没有找到可用的模型或参考音频")
            request_json.update({"model": model, "reference": reference})
        kwargs: dict[str, Any] = {"json": request_json}
        timeout = aiohttp.ClientTimeout(total=self.cfg.get("timeout_seconds", 60))
        async with self.session.request(self.cfg.get("method", "POST"), self.cfg["url"], timeout=timeout, **kwargs) as r:
            if r.status >= 400: raise RuntimeError(f"TTS HTTP {r.status}: {(await r.text())[:300]}")
            mode = self.cfg.get("response_type", "audio")
            if mode == "audio": content = await r.read()
            else:
                data = await r.json()
                value = data[self.cfg.get("response_field", "audio")]
                if mode == "json_base64": content = base64.b64decode(value)
                elif mode == "json_url":
                    async with self.session.get(value) as ar: content = await ar.read()
                else: raise ValueError(f"未知 TTS response_type: {mode}")
        path = self.audio_dir / f"reply_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
        path.write_bytes(content)
        cleanup_audio_files(self.audio_dir, 20)
        return path

    async def synthesize(self, text: str) -> Path | None:
        """Serialize GPU work and retry transient overload/timeouts without dropping speech."""
        attempts = max(1, int(self.cfg.get("retry_attempts", 4)))
        base_delay = max(0.1, float(self.cfg.get("retry_delay_seconds", 2.0)))
        async with self._synthesis_lock:
            for attempt in range(1, attempts + 1):
                try:
                    return await self._synthesize_once(text)
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, asyncio.LimitOverrunError, OSError, RuntimeError) as exc:
                    if attempt >= attempts: raise
                    delay = min(base_delay * (2 ** (attempt - 1)), 15.0)
                    logging.getLogger("ai_live").warning(
                        "TTS 暂时不可用，第 %s/%s 次失败，%.1f 秒后重试: %s",
                        attempt, attempts, delay, exc,
                    )
                    await asyncio.sleep(delay)

    async def play(self, path: Path) -> None:
        """等待当前文件播放完毕；由单消费者顺序调用可避免声音重叠。"""
        await asyncio.to_thread(self._play, path)

    async def speak(self, text: str) -> Path | None:
        """兼容原有调用：生成后同步等待播放完成。"""
        path = await self.synthesize(text)
        if path and self.cfg.get("play_audio", True): await self.play(path)
        return path

    @staticmethod
    def _play(path: Path) -> None:
        if os.name == "nt":
            # SoundPlayer via Windows PowerShell is more reliable for WAV files
            # generated by GPT-SoVITS than winsound in a background Python process.
            escaped = str(path).replace("'", "''")
            script = f"$p=New-Object System.Media.SoundPlayer '{escaped}';$p.Load();$p.PlaySync()"
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Windows 音频播放失败: {result.stderr.strip()}")
            logging.getLogger("ai_live").info("语音播放完成: %s", path.name)
        else:
            subprocess.run(["ffplay", "-nodisp", "-autoexit", str(path)], check=False)
