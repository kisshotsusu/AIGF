"""ComfyUI image/video generation client for HomeAgent.

独立功能模块：只负责与本地 ComfyUI 服务通信——检查/启动服务、枚举模型、
构造 API 工作流、提交任务、轮询进度、下载输出文件。不包含 UI、TTS、
任务恢复或模型决策逻辑；HomeAgent 工具循环只做薄委托。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import aiohttp


class ComfyUIClient:
    """OpenAI-style thin wrapper around a local ComfyUI HTTP API."""

    DEFAULTS = {
        "enabled": True,
        "base_url": "http://127.0.0.1:8188",
        "auto_start": True,
        "install_dir": r"D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI",
        "python": r"D:\Comfy-Desktop\ComfyUI-Installs\ComfyUI\ComfyUI\.venv\Scripts\python.exe",
        "extra_model_paths_config": r"C:\Users\21018\AppData\Roaming\Comfy Desktop\shared_model_paths.yaml",
        "input_dir": r"D:\Comfy-Desktop\ComfyUI-Shared\input",
        "output_dir": r"D:\Comfy-Desktop\ComfyUI-Shared\output",
        "home_output_dir": r"E:\Doc\AIAgent\outputs\comfyui",
        "startup_timeout_seconds": 180,
        "timeout_seconds": 900,
        "poll_interval_seconds": 2,
        "max_image_bytes": 40 * 1024 * 1024,
    }

    IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".gif"}

    # 与已安装模型对应的预设（文件名来自 ComfyUI-Shared/models）。
    PRESETS = {
        "qwen-image-2512": {
            "kind": "image",
            "unet": "qwen_image_2512_fp8_e4m3fn.safetensors",
            "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "clip_type": "qwen_image",
            "vae": "qwen_image_vae.safetensors",
            "lora": "Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors",
            "positive_suffix": "masterpiece, best quality, highly detailed, sharp focus, natural lighting, cinematic composition, photorealistic",
            "negative_default": "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，过度光滑，AI感，构图混乱，文字模糊，扭曲，水印，签名，模糊，噪点",
            "shift": 3.1,
            "default_steps": 40,
            "lightning_steps": 4,
            "cfg": 4.0,
            "sizes": [(1024, 1024), (1328, 1328), (1024, 768), (768, 1024)],
        },
        "anima": {
            "kind": "image",
            "unet": "anima-base-v1.0.safetensors",
            "clip": "qwen_3_06b_base.safetensors",
            "clip_type": "stable_diffusion",
            "vae": "qwen_image_vae.safetensors",
            "lora": "anima-turbo-lora-v0.2.safetensors",
            "positive_suffix": "masterpiece, best quality, highly detailed, clean lineart, vibrant colors, beautiful composition, intricate details",
            "negative_default": "worst quality, low quality, bad anatomy, bad hands, missing fingers, extra digits, fewer digits, cropped, jpeg artifacts, signature, watermark, username, blurry, text, deformed, mutated, ugly, duplicate, morbid, mutilated, out of frame, extra limbs, fused fingers, long neck, bad proportions, bad feet, extra fingers, disfigured, poorly drawn face, bad face, missing limb, extra leg, extra arm, deformed fingers, lowres",
            "shift": None,
            "default_steps": 30,
            "lightning_steps": 8,
            "cfg": 4.0,
            "sizes": [(1024, 1024), (768, 1024), (1024, 768), (832, 1216)],
        },
        "qwen-image-edit-2511": {
            "kind": "edit",
            "unet": "qwen_image_edit_2511_int8_convrot.safetensors",
            "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
            "clip_type": "qwen_image",
            "vae": "qwen_image_vae.safetensors",
            "lora": "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
            "positive_suffix": "high quality, highly detailed, sharp focus, consistent lighting, coherent composition, keep the exact same character identity, same face, same hairstyle, same outfit",
            "negative_default": "低分辨率，低画质，五官崩坏，脸部变形，眼睛错位，嘴歪，多余手指，手指数量错误，手臂扭曲，肢体比例失调，多手多脚，身体结构错误，透视错误，肢体畸形，手指畸形，文字模糊，构图混乱，画面过饱和，过度光滑，AI感，水印，签名，模糊，噪点，边缘破损，低清，模糊不清",
            "min_steps": 8,
            "shift": 3.1,
            "default_steps": 20,
            "lightning_steps": 4,
            "cfg": 3.0,
        },
        "minimax-h3": {
            "kind": "video",
            "unet": "MiniMax_H3_FL2VA_pruned_nvfp4.safetensors",
            "unet_int8": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "clip": "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors",
            "clip_type": "minimax",
            "video_vae": "minimax_h3_video_vae_fp16.safetensors",
            "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
            "shift_video": 12.0,
            "shift_audio": 3.0,
            "default_steps": 20,
            "default_fps": 24,
            "sizes": [(1344, 768), (768, 1344), (1024, 1024)],
        },
    }

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = {**self.DEFAULTS, **(config or {})}
        home_output = str(self.config.get("home_output_dir") or "").strip()
        if home_output and not Path(home_output).is_absolute():
            root = str(self.config.get("project_root") or "").strip() or str(Path.cwd())
            self.config["home_output_dir"] = str(Path(root) / home_output)
        self._process: subprocess.Popen | None = None
        self._object_info_cache: dict[str, Any] | None = None
        self._object_info_at = 0.0

    # ---------- 基础连接 ----------

    async def _request(self, session: aiohttp.ClientSession, method: str, path: str, **kwargs) -> Any:
        url = str(self.config["base_url"]).rstrip("/") + path
        timeout = aiohttp.ClientTimeout(total=kwargs.pop("timeout", 60))
        async with session.request(method, url, timeout=timeout, **kwargs) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"ComfyUI HTTP {response.status}: {raw[:600]}")
            if not raw:
                return None
            return json.loads(raw)

    async def status(self) -> dict[str, Any]:
        """返回服务状态与队列信息。"""
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            stats = await self._request(session, "GET", "/system_stats")
            queue = await self._request(session, "GET", "/queue")
        return {
            "ok": True,
            "ready": True,
            "comfyui_version": str(stats.get("system", {}).get("comfyui_version", "")),
            "pytorch_version": str(stats.get("system", {}).get("pytorch_version", "")),
            "device": stats.get("devices", [{}])[0].get("name", "") if stats.get("devices") else "",
            "queue_running": len(queue.get("queue_running", [])),
            "queue_pending": len(queue.get("queue_pending", [])),
            "base_url": str(self.config["base_url"]),
        }

    async def ensure_running(self) -> dict[str, Any]:
        """探测服务；未启动且允许自动启动时按桌面版参数拉起 ComfyUI。"""
        try:
            return await self.status()
        except Exception:
            pass
        if not self.config.get("auto_start"):
            raise RuntimeError("ComfyUI 未运行且 auto_start=false，请先启动 ComfyUI Desktop")
        self._launch()
        deadline = time.monotonic() + int(self.config.get("startup_timeout_seconds", 180))
        while time.monotonic() < deadline:
            try:
                return await self.status()
            except Exception:
                await asyncio.sleep(3)
        raise RuntimeError(f"ComfyUI 在 {self.config.get('startup_timeout_seconds')} 秒内未能就绪，请检查 D:\\Comfy-Desktop 桌面版日志")

    def _launch(self) -> None:
        install = Path(str(self.config["install_dir"]))
        python = Path(str(self.config["python"]))
        if not python.is_file():
            fallback = install / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
            python = fallback if fallback.is_file() else python
        if not python.is_file():
            raise RuntimeError(f"未找到 ComfyUI Python：{python}")
        args = ["-s", "ComfyUI/main.py", "--enable-manager"]
        extra = str(self.config.get("extra_model_paths_config") or "").strip()
        if extra:
            args += ["--extra-model-paths-config", extra]
        args += [
            "--input-directory", str(self.config.get("input_dir") or install / "ComfyUI" / "input"),
            "--output-directory", str(self.config.get("output_dir") or install / "ComfyUI" / "output"),
        ]
        log_dir = install / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stdout = open(log_dir / f"comfyui-homeagent-{stamp}.log", "w", encoding="utf-8")
        stderr = open(log_dir / f"comfyui-homeagent-{stamp}.err.log", "w", encoding="utf-8")
        self._process = subprocess.Popen(
            [str(python), *args],
            cwd=str(install),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    # ---------- 模型与节点信息 ----------

    async def _object_info(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._object_info_cache and now - self._object_info_at < 300:
            return self._object_info_cache
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self._object_info_cache = await self._request(session, "GET", "/object_info")
        self._object_info_at = now
        return self._object_info_cache

    async def list_models(self) -> dict[str, Any]:
        """枚举 ComfyUI 可用的模型文件与支持的生成预设。"""
        info = await self._object_info()
        choices = {}
        for node_name, node in info.items():
            required = (node.get("input", {}).get("required") or {})
            for key, spec in required.items():
                if isinstance(spec, list) and spec and isinstance(spec[0], list):
                    values = [str(v) for v in spec[0] if isinstance(v, str) and v.endswith((".safetensors", ".ckpt", ".pt", ".pth", ".bin"))]
                    if values:
                        choices.setdefault(node_name, []).extend(values)
        presets = []
        for name, preset in self.PRESETS.items():
            presets.append({
                "name": name,
                "kind": preset["kind"],
                "unet": preset.get("unet") or preset.get("unet_int8"),
                "default_size": preset.get("sizes", [None])[0],
            })
        return {
            "ok": True,
            "presets": presets,
            "available_models": choices,
            "note": "文件名来自 ComfyUI /object_info；实际可用性以生成结果为准",
        }

    # ---------- 上传与输出 ----------

    async def _upload_image(self, session: aiohttp.ClientSession, image_path: Path) -> str:
        path = image_path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"图片不存在：{path}")
        data = aiohttp.FormData()
        data.add_field("image", path.read_bytes(), filename=path.name, content_type="image/png")
        data.add_field("type", "input")
        data.add_field("overwrite", "true")
        url = str(self.config["base_url"]).rstrip("/") + "/upload/image"
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout_seconds", 900)))
        async with session.post(url, data=data, timeout=timeout) as response:
            raw = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"上传图片失败 HTTP {response.status}: {raw[:400]}")
            payload = json.loads(raw)
        name = str(payload.get("name") or path.name)
        return name

    async def _wait_and_fetch_outputs(self, session: aiohttp.ClientSession, prompt_id: str, status=None, timeout_seconds: int | None = None) -> list[dict[str, Any]]:
        deadline = time.monotonic() + int(timeout_seconds or self.config.get("timeout_seconds", 900))
        last_progress = ""
        while time.monotonic() < deadline:
            history = await self._request(session, "GET", f"/history/{prompt_id}", timeout=30)
            entry = history.get(prompt_id)
            if entry:
                if entry.get("status", {}).get("completed") is True or entry.get("status", {}).get("status_str") == "success":
                    outputs = self._collect_outputs(entry)
                    if outputs:
                        return outputs
                    raise RuntimeError("任务已完成但没有找到输出文件")
                if entry.get("status", {}).get("status_str") in {"error", "failed"}:
                    messages = entry.get("status", {}).get("messages", [])
                    detail = json.dumps(messages[-3:], ensure_ascii=False)[:800]
                    raise RuntimeError(f"ComfyUI 任务失败：{detail}")
            queue = await self._request(session, "GET", "/queue", timeout=30)
            progress = ""
            for item in queue.get("queue_running", []):
                if len(item) > 1 and str(item[1]) == prompt_id:
                    progress = "运行中"
            for item in queue.get("queue_pending", []):
                if len(item) > 1 and str(item[1]) == prompt_id:
                    progress = "排队等待"
            if progress and progress != last_progress:
                last_progress = progress
                if status:
                    status(f"ComfyUI 生成中（{progress}）…")
            await asyncio.sleep(float(self.config.get("poll_interval_seconds", 2)))
        raise TimeoutError(f"ComfyUI 任务超时（>{timeout_seconds or self.config.get('timeout_seconds')} 秒），prompt_id={prompt_id}")

    @staticmethod
    def _collect_outputs(entry: dict[str, Any]) -> list[dict[str, Any]]:
        """递归收集历史输出中的 {filename, subfolder, type}，兼容图片与视频。"""
        found: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("filename"), str) and "type" in value:
                    found.append({
                        "filename": value["filename"],
                        "subfolder": str(value.get("subfolder") or ""),
                        "type": str(value.get("type") or "output"),
                    })
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(entry.get("outputs"))
        return found

    async def _download_output(self, session: aiohttp.ClientSession, output: dict[str, Any]) -> Path:
        home_dir = Path(str(self.config["home_output_dir"]))
        if not home_dir.is_absolute():
            raise RuntimeError(f"home_output_dir 必须是绝对路径：{home_dir}")
        home_dir.mkdir(parents=True, exist_ok=True)
        params = {
            "filename": output["filename"],
            "subfolder": output.get("subfolder", ""),
            "type": output.get("type", "output"),
        }
        url = str(self.config["base_url"]).rstrip("/") + "/view"
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout_seconds", 900)))
        async with session.get(url, params=params, timeout=timeout) as response:
            if response.status >= 400:
                raise RuntimeError(f"下载输出失败 HTTP {response.status}")
            data = await response.read()
        if len(data) > int(self.config.get("max_image_bytes", 40 * 1024 * 1024)):
            raise RuntimeError("输出文件超过大小上限")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", output["filename"])
        target = home_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{safe_name}"
        target.write_bytes(data)
        return target

    async def _run_workflow(self, workflow: dict[str, Any], filename_prefix: str, status=None, timeout_seconds: int | None = None) -> dict[str, Any]:
        await self.ensure_running()
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            result = await self._request(session, "POST", "/prompt", json={"prompt": workflow})
            prompt_id = str(result["prompt_id"])
            outputs = await self._wait_and_fetch_outputs(session, prompt_id, status, timeout_seconds)
            media: list[dict[str, Any]] = []
            for output in outputs:
                path = await self._download_output(session, output)
                suffix = path.suffix.lower()
                kind = "video" if suffix in self.VIDEO_SUFFIXES else ("image" if suffix in self.IMAGE_SUFFIXES else "file")
                media.append({"path": str(path), "kind": kind, "caption": filename_prefix})
            return {
                "ok": True,
                "status": "success",
                "prompt_id": prompt_id,
                "filename_prefix": filename_prefix,
                "media": media,
                "message": f"已生成 {len(media)} 个文件：{', '.join(str(Path(item['path']).name) for item in media)}",
            }

    # ---------- 工作流构造 ----------

    def _base_image_nodes(self, preset: dict[str, Any], positive: str, negative: str, width: int, height: int, steps: int, cfg: float, seed: int, use_lora: bool) -> dict[str, Any]:
        nodes: dict[str, Any] = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": preset["unet"], "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": preset["clip"], "type": preset["clip_type"]}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": preset["vae"]}},
        }
        model_ref = ["1", 0]
        if preset.get("shift") is not None:
            nodes["4"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": float(preset["shift"])}}
            model_ref = ["4", 0]
        if use_lora and preset.get("lora"):
            nodes["5"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": model_ref, "lora_name": preset["lora"], "strength_model": 1.0}}
            model_ref = ["5", 0]
        nodes["6"] = {"class_type": "CLIPTextEncode", "inputs": {"text": positive, "clip": ["2", 0]}}
        nodes["7"] = {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 0]}}
        latent_class = "EmptySD3LatentImage" if preset["clip_type"] == "qwen_image" else "EmptyLatentImage"
        nodes["8"] = {"class_type": latent_class, "inputs": {"width": width, "height": height, "batch_size": 1}}
        nodes["9"] = {
            "class_type": "KSampler",
            "inputs": {
                "model": model_ref, "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": "euler", "scheduler": "simple",
                "positive": ["6", 0], "negative": ["7", 0],
                "latent_image": ["8", 0], "denoise": 1.0,
            },
        }
        nodes["10"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}}
        return nodes

    def _build_image_workflow(self, preset: dict[str, Any], prompt: str, negative: str, width: int, height: int, steps: int, cfg: float, seed: int, use_lora: bool, filename_prefix: str) -> dict[str, Any]:
        nodes = self._base_image_nodes(preset, prompt, negative, width, height, steps, cfg, seed, use_lora)
        nodes["11"] = {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": filename_prefix}}
        return nodes

    def _build_edit_workflow(self, preset: dict[str, Any], image_name: str, prompt: str, negative: str, steps: int, cfg: float, seed: int, use_lora: bool, filename_prefix: str, denoise: float = 1.0) -> dict[str, Any]:
        nodes: dict[str, Any] = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": preset["unet"], "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": preset["clip"], "type": preset["clip_type"]}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": preset["vae"]}},
            "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
            "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["3", 0]}},
        }
        model_ref = ["1", 0]
        if preset.get("shift") is not None:
            nodes["6"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": float(preset["shift"])}}
            model_ref = ["6", 0]
        if use_lora and preset.get("lora"):
            nodes["7"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": model_ref, "lora_name": preset["lora"], "strength_model": 1.0}}
            model_ref = ["7", 0]
        nodes["8"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "prompt": prompt, "vae": ["3", 0], "image1": ["4", 0]}}
        nodes["9"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "prompt": negative, "vae": ["3", 0], "image1": ["4", 0]}}
        nodes["10"] = {
            "class_type": "KSampler",
            "inputs": {
                "model": model_ref, "seed": seed, "steps": steps, "cfg": cfg,
                "sampler_name": "euler", "scheduler": "simple",
                "positive": ["8", 0], "negative": ["9", 0],
                "latent_image": ["5", 0], "denoise": float(denoise),
            },
        }
        nodes["11"] = {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}}
        nodes["12"] = {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": filename_prefix}}
        return nodes

    def _build_video_workflow(self, preset: dict[str, Any], prompt: str, width: int, height: int, frames: int, steps: int, seed: int, fps: int, first_frame_name: str | None, filename_prefix: str, use_int8: bool) -> dict[str, Any]:
        unet = preset.get("unet_int8") if use_int8 else preset.get("unet")
        nodes: dict[str, Any] = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": preset["clip"], "type": preset["clip_type"]}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": preset["video_vae"]}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": preset["audio_vae"]}},
            "5": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["1", 0], "shift_video": float(preset["shift_video"]), "shift_audio": float(preset["shift_audio"])}},
        }
        video_inputs: dict[str, Any] = {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
            "width": width, "height": height, "length": frames,
        }
        if first_frame_name:
            nodes["20"] = {"class_type": "LoadImage", "inputs": {"image": first_frame_name}}
            video_inputs["first_frame"] = ["20", 0]
        nodes["6"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": video_inputs}
        nodes["7"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}}
        nodes["8"] = {"class_type": "BasicScheduler", "inputs": {"model": ["5", 0], "scheduler": "simple", "steps": steps, "denoise": 1.0}}
        nodes["9"] = {"class_type": "BasicGuider", "inputs": {"model": ["5", 0], "conditioning": ["6", 0]}}
        nodes["10"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
        nodes["11"] = {
            "class_type": "SamplerCustomAdvanced",
            "inputs": {
                "noise": ["10", 0], "guider": ["9", 0], "sampler": ["7", 0],
                "sigmas": ["8", 0], "latent_image": ["6", 1],
            },
        }
        nodes["12"] = {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}}
        nodes["13"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["11", 0], "vae": ["4", 0]}}
        nodes["14"] = {"class_type": "CreateVideo", "inputs": {"images": ["12", 0], "fps": fps, "audio": ["13", 0], "bit_depth": 8}}
        nodes["15"] = {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}}
        return nodes

    # ---------- 对外生成接口 ----------

    def _enrich_prompts(self, preset: dict[str, Any], prompt: str, negative_prompt: str) -> tuple[str, str]:
        """自动追加质量/风格正向提示词，并始终应用默认负面提示词。"""
        suffix = str(self.config.get("positive_suffix") or preset.get("positive_suffix") or "").strip()
        default_negative = str(self.config.get("negative_prompt") or preset.get("negative_default") or "").strip()
        positive = str(prompt or "").strip()
        if suffix:
            existing_tags = {tag.strip().lower() for tag in re.split(r"[,，、]+", positive) if tag.strip()}
            extra_tags = [tag.strip() for tag in re.split(r"[,，]+", suffix) if tag.strip() and tag.strip().lower() not in existing_tags]
            if extra_tags:
                positive = f"{positive}, {', '.join(extra_tags)}" if positive else ", ".join(extra_tags)
        negative = str(negative_prompt or "").strip()
        if negative and default_negative:
            negative = f"{negative}, {default_negative}"
        elif not negative:
            negative = default_negative
        return positive, negative

    async def generate_image(self, prompt: str, negative_prompt: str = "", model: str = "qwen-image-2512", width: int = 1024, height: int = 1024, steps: int | None = None, cfg: float | None = None, seed: int | None = None, use_lora: bool = True, filename_prefix: str = "homeagent/image", status=None, timeout_seconds: int | None = None) -> dict[str, Any]:
        preset = self.PRESETS.get(model) or self.PRESETS["qwen-image-2512"]
        if preset["kind"] != "image":
            raise ValueError(f"模型预设 {model} 不是图像模型")
        prompt, negative_prompt = self._enrich_prompts(preset, prompt, negative_prompt)
        steps = steps or int(preset.get("default_steps", 40))
        cfg = float(cfg if cfg is not None else preset.get("cfg", 4.0))
        seed = int(seed if seed is not None else time.time_ns() % (2**31))
        width, height = self._clamp_size(width, height, preset.get("sizes"))
        workflow = self._build_image_workflow(preset, prompt, negative_prompt, width, height, steps, cfg, seed, bool(use_lora), filename_prefix)
        return await self._run_workflow(workflow, filename_prefix, status, timeout_seconds)

    async def edit_image(self, image_path: str, prompt: str, negative_prompt: str = "", model: str = "qwen-image-edit-2511", steps: int | None = None, cfg: float | None = None, seed: int | None = None, use_lora: bool = True, filename_prefix: str = "homeagent/edit", denoise: float = 1.0, status=None, timeout_seconds: int | None = None) -> dict[str, Any]:
        preset = self.PRESETS.get(model) or self.PRESETS["qwen-image-edit-2511"]
        if preset["kind"] != "edit":
            raise ValueError(f"模型预设 {model} 不是编辑模型")
        prompt, negative_prompt = self._enrich_prompts(preset, prompt, negative_prompt)
        await self.ensure_running()
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            image_name = await self._upload_image(session, Path(image_path))
        use_lora = bool(use_lora)
        steps = self._edit_steps(preset, steps, use_lora)
        cfg = float(cfg if cfg is not None else preset.get("cfg", 3.0))
        seed = int(seed if seed is not None else time.time_ns() % (2**31))
        denoise = max(0.3, min(1.0, float(denoise)))
        workflow = self._build_edit_workflow(preset, image_name, prompt, negative_prompt, steps, cfg, seed, use_lora, filename_prefix, denoise)
        return await self._run_workflow(workflow, filename_prefix, status, timeout_seconds)

    @staticmethod
    def _edit_steps(preset: dict[str, Any], steps: int | None, use_lora: bool) -> int:
        """编辑步数策略：Lightning LoRA 用其原生步数，否则用完整质量步数（下限保护）。"""
        if steps is not None:
            value = int(steps)
            return value if use_lora else max(int(preset.get("min_steps", 1)), value)
        return int(preset.get("lightning_steps", 4)) if use_lora else int(preset.get("default_steps", 20))

    async def generate_video(self, prompt: str, model: str = "minimax-h3", width: int = 1344, height: int = 768, frames: int = 49, steps: int | None = None, seed: int | None = None, fps: int = 24, first_frame: str | None = None, use_int8: bool = False, filename_prefix: str = "homeagent/video", status=None, timeout_seconds: int | None = None) -> dict[str, Any]:
        preset = self.PRESETS.get(model) or self.PRESETS["minimax-h3"]
        if preset["kind"] != "video":
            raise ValueError(f"模型预设 {model} 不是视频模型")
        await self.ensure_running()
        steps = steps or int(preset.get("default_steps", 20))
        seed = int(seed if seed is not None else time.time_ns() % (2**31))
        fps = int(fps or preset.get("default_fps", 24))
        width, height = self._clamp_size(width, height, preset.get("sizes"))
        frames = max(1, min(1024, int(frames)))
        first_frame_name = None
        if first_frame:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                first_frame_name = await self._upload_image(session, Path(first_frame))
        workflow = self._build_video_workflow(preset, prompt, width, height, frames, steps, seed, fps, first_frame_name, filename_prefix, bool(use_int8))
        default_video_timeout = 1800
        return await self._run_workflow(workflow, filename_prefix, status, timeout_seconds or default_video_timeout)

    @staticmethod
    def _clamp_size(width: int, height: int, allowed: list[tuple[int, int]] | None) -> tuple[int, int]:
        width = max(64, int(width)); height = max(64, int(height))
        if not allowed:
            return width, height
        best = min(allowed, key=lambda item: abs(item[0] - width) + abs(item[1] - height))
        return best
