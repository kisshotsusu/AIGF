"""CosyVoice2 emotion-controllable TTS client for HomeAgent.

独立功能模块：只负责与本地 CosyVoice2 FastAPI 服务通信——探测/自动启动服务、
枚举参考音色、把角色台词中的括号语气描写解析为情绪指令、调用
/inference_instruct2 合成语音并保存为 WAV。不包含 UI、任务恢复或模型决策逻辑。
"""
from __future__ import annotations

import asyncio
import io
import re
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import aiohttp


class CosyVoiceTTS:
    """OpenAI-style thin wrapper around the local CosyVoice2 FastAPI server."""

    DEFAULTS = {
        "enabled": True,
        "base_url": "http://127.0.0.1:50000",
        "auto_start": True,
        "install_dir": r"E:\OtherProgram\CosyVoice",
        "python": r"E:\OtherProgram\CosyVoice\.venv\Scripts\python.exe",
        "model_dir": r"E:\OtherProgram\CosyVoice\pretrained_models\CosyVoice2-0.5B",
        "server_port": 50000,
        "reference_dir": r"E:\Doc\AIAgent\outputs\cosyvoice_refs",
        "output_dir": r"E:\Doc\AIAgent\outputs\cosyvoice",
        "startup_timeout_seconds": 240,
        "timeout_seconds": 180,
        "sample_rate": 24000,
    }

    # 常见语气词 → CosyVoice2 指令片段；括号里出现的词会拼进指令。
    MOOD_HINTS = {
        "温柔": "语气温柔",
        "轻柔": "声音轻柔",
        "柔弱": "声音柔弱轻柔",
        "娇羞": "语气娇羞",
        "羞": "语气娇羞",
        "黏腻": "声音黏腻绵软",
        "黏": "声音黏腻绵软",
        "软": "声音发软",
        "喘息": "带微微喘息",
        "喘": "带微微喘息",
        "娇喘": "声音娇媚，带喘息",
        "满足": "带着满足感",
        "颤抖": "声音微微颤抖",
        "颤": "声音微微颤抖",
        "破碎": "气息断续破碎",
        "急促": "呼吸急促",
        "酥": "声音发酥发软",
        "低语": "轻声低语",
        "耳语": "像耳语一样轻",
        "哭腔": "带哭腔",
        "哽咽": "声音哽咽",
        "笑": "带着笑意",
        "撒娇": "撒娇的语气",
        "沙哑": "声音沙哑",
        "磁性": "声音低沉有磁性",
        "兴奋": "语气兴奋",
        "害羞": "语气害羞",
        "诱惑": "语气诱惑撩人",
        "勾引": "语气诱惑撩人",
        "痴迷": "语气痴迷沉醉",
        "不舍": "语气不舍缠绵",
    }

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**self.DEFAULTS, **(config or {})}
        for key in ("reference_dir", "output_dir"):
            value = str(self.config.get(key) or "").strip()
            if value and not Path(value).is_absolute():
                root = str(self.config.get("project_root") or "").strip() or str(Path.cwd())
                self.config[key] = str(Path(root) / value)
        self._process: subprocess.Popen | None = None

    # ---------- 基础连接 ----------

    async def status(self) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(str(self.config["base_url"]).rstrip("/") + "/openapi.json") as response:
                if response.status >= 400:
                    raise RuntimeError(f"CosyVoice HTTP {response.status}")
                await response.read()
        return {
            "ok": True,
            "ready": True,
            "base_url": str(self.config["base_url"]),
            "model": "CosyVoice2-0.5B",
            "mode": "instruct2（情绪指令 + 参考音色）",
        }

    async def ensure_running(self) -> dict[str, Any]:
        try:
            return await self.status()
        except Exception:
            pass
        if not self.config.get("auto_start"):
            raise RuntimeError("CosyVoice2 未运行且 auto_start=false，请先启动服务")
        self._launch()
        deadline = time.monotonic() + int(self.config.get("startup_timeout_seconds", 240))
        while time.monotonic() < deadline:
            try:
                return await self.status()
            except Exception:
                await asyncio.sleep(4)
        raise RuntimeError(f"CosyVoice2 在 {self.config.get('startup_timeout_seconds')} 秒内未能就绪，请查看 {self.config.get('install_dir')}\\server.log")

    def _launch(self) -> None:
        install = Path(str(self.config["install_dir"]))
        python = Path(str(self.config["python"]))
        if not python.is_file():
            python = install / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            raise RuntimeError(f"未找到 CosyVoice Python：{python}")
        port = int(self.config.get("server_port", 50000))
        args = [
            "runtime\\python\\fastapi\\server.py",
            "--port", str(port),
            "--model_dir", str(self.config.get("model_dir") or install / "pretrained_models" / "CosyVoice2-0.5B"),
        ]
        log_dir = install / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stdout = open(log_dir / f"cosyvoice-server-{stamp}.log", "w", encoding="utf-8")
        stderr = open(log_dir / f"cosyvoice-server-{stamp}.err.log", "w", encoding="utf-8")
        self._process = subprocess.Popen(
            [str(python), *args],
            cwd=str(install),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )

    # ---------- 参考音色 ----------

    def list_reference_audios(self) -> dict[str, Any]:
        directory = Path(str(self.config["reference_dir"]))
        files = []
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".flac", ".ogg"}:
                    files.append({"name": path.name, "path": str(path), "duration_hint": "请使用 3～10 秒干净人声"})
        return {"ok": True, "reference_dir": str(directory), "references": files, "count": len(files)}

    # ---------- 括号语气词解析 ----------

    @classmethod
    def parse_stage_directions(cls, text: str) -> dict[str, Any]:
        """把 （…） 括号里的语气描写提取为情绪指令，并从朗读文本中剥离。"""
        spoken = str(text or "")
        directions: list[str] = []
        pattern = re.compile(r"[（(]([^（）()]*)[）)]")
        while True:
            match = pattern.search(spoken)
            if not match:
                break
            content = match.group(1).strip()
            if content:
                directions.append(content)
            spoken = spoken[: match.start()] + spoken[match.end():]
        keywords = "，".join(directions)
        hints: list[str] = []
        for word, hint in cls.MOOD_HINTS.items():
            if word in keywords:
                if hint not in hints:
                    hints.append(hint)
        if not hints and directions:
            hints.append("语气贴合角色当前情绪")
        # 情绪词先保留最相关的几条，避免与语速提示互相挤占。
        hints = hints[:4]
        if directions and "缓慢" in keywords or "绵长" in keywords or "长" in keywords:
            hints.append("语速缓慢")
        if "急促" in keywords or "急切" in keywords:
            hints.append("语速稍快")
        if "语速稍快" in hints and "语速缓慢" in hints:
            hints.remove("语速稍快")
        # 提示词过多且互相冲突会让模型产出机械感，保留最相关的几条即可。
        hints = hints[:5]
        instruction = "请用" + "、".join(hints) + "的语气说这段话" if hints else ""
        return {
            "spoken_text": re.sub(r"\s+", " ", spoken).strip(),
            "directions": directions,
            "keywords": keywords,
            "instruct_text": instruction,
        }

    # ---------- 合成 ----------

    async def synthesize(self, text: str, instruct_text: str = "", reference: str = "", filename_prefix: str = "homeagent/cosyvoice", status=None, timeout_seconds: int | None = None) -> dict[str, Any]:
        """调用 /inference_instruct2 合成语音，保存为 24kHz WAV。"""
        parsed = self.parse_stage_directions(text)
        tts_text = parsed["spoken_text"] or str(text).strip()
        if not tts_text:
            raise ValueError("没有可朗读的文本")
        instruction = str(instruct_text or "").strip() or parsed["instruct_text"]
        if not instruction:
            instruction = "自然、温柔地说"
        reference_path = self._resolve_reference(reference)
        await self.ensure_running()
        data = aiohttp.FormData()
        data.add_field("tts_text", tts_text)
        data.add_field("instruct_text", instruction)
        data.add_field("prompt_wav", reference_path.read_bytes(), filename=reference_path.name, content_type="audio/wav")
        timeout = aiohttp.ClientTimeout(total=int(timeout_seconds or self.config.get("timeout_seconds", 180)))
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(str(self.config["base_url"]).rstrip("/") + "/inference_instruct2", data=data) as response:
                raw = await response.read()
                if response.status >= 400:
                    raise RuntimeError(f"CosyVoice 合成失败 HTTP {response.status}: {raw[:300]}")
        if len(raw) < 44:
            raise RuntimeError("CosyVoice 返回的音频过短")
        target = self._save_wav(raw, filename_prefix)
        return {
            "ok": True,
            "status": "success",
            "media": [{"path": str(target), "kind": "audio", "caption": filename_prefix}],
            "instruct_text": instruction,
            "directions": parsed["directions"],
            "reference": str(reference_path),
            "duration_seconds": round(len(raw) / 2 / int(self.config.get("sample_rate", 24000)), 2),
            "message": f"已生成语音：{target.name}（{instruction}）",
        }

    def _resolve_reference(self, reference: str) -> Path:
        directory = Path(str(self.config["reference_dir"]))
        if reference:
            candidate = Path(reference).expanduser()
            if not candidate.is_absolute():
                candidate = directory / reference
            if candidate.is_file():
                return candidate.resolve()
            for path in directory.rglob("*"):
                if path.name == reference and path.is_file():
                    return path.resolve()
            raise FileNotFoundError(f"参考音色不存在：{reference}")
        files = sorted(directory.rglob("*")) if directory.is_dir() else []
        wavs = [path for path in files if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".flac", ".ogg"}]
        if not wavs:
            raise FileNotFoundError(f"参考音色目录为空：{directory}，请放入 3～10 秒干净人声")
        return wavs[0].resolve()

    def _save_wav(self, pcm: bytes, filename_prefix: str) -> Path:
        directory = Path(str(self.config["output_dir"]))
        directory.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename_prefix)
        target = directory / f"{time.strftime('%Y%m%d_%H%M%S')}_{safe}.wav"
        sample_rate = int(self.config.get("sample_rate", 24000))
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(pcm)
        return target
