from __future__ import annotations

from copy import deepcopy
from typing import Any


CUSTOM_PRESET = "custom"

IMAGE_API_PRESETS: dict[str, dict[str, Any]] = {
    CUSTOM_PRESET: {
        "label": "自定义",
        "description": "保留并手动编辑当前接口参数。",
        "config": {},
    },
    "qwen": {
        "label": "千问图像（阿里云百炼）",
        "description": "使用 DashScope 原生多模态接口；请把 Base URL 中的 {WorkspaceId} 替换为百炼工作空间 ID。",
        "config": {
            "provider": "qwen",
            "mode": "dashscope_multimodal",
            "base_url": "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1",
            "model": "qwen-image-2.0-pro",
            "size": "2048*2048",
            "timeout_seconds": 300,
            "api_key_env": "DASHSCOPE_API_KEY",
        },
    },
    "grok": {
        "label": "Grok Imagine（xAI）",
        "description": "使用 xAI Images API；默认采用高质量 Grok Imagine 图像模型。",
        "config": {
            "provider": "xai",
            "mode": "xai_images",
            "base_url": "https://api.x.ai/v1",
            "model": "grok-imagine-image-quality",
            "size": "",
            "timeout_seconds": 300,
            "api_key_env": "XAI_API_KEY",
        },
    },
}


def preset_items() -> list[tuple[str, str]]:
    return [(key, str(value["label"])) for key, value in IMAGE_API_PRESETS.items()]


def preset_config(key: str) -> dict[str, Any]:
    item = IMAGE_API_PRESETS.get(str(key), IMAGE_API_PRESETS[CUSTOM_PRESET])
    config = deepcopy(item["config"])
    config["preset"] = str(key) if key in IMAGE_API_PRESETS else CUSTOM_PRESET
    return config


def preset_description(key: str) -> str:
    return str(IMAGE_API_PRESETS.get(str(key), IMAGE_API_PRESETS[CUSTOM_PRESET])["description"])

