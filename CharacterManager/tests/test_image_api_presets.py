from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CHARACTER_MANAGER = Path(__file__).resolve().parents[1]
ROOT = CHARACTER_MANAGER.parent
for path in (CHARACTER_MANAGER, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PySide6.QtWidgets import QApplication

from image_api_presets import preset_config
from qt_app import ImageApiPage


def load_image_script():
    path = ROOT / "Skill" / "ai-live-character-image" / "scripts" / "character_image_api.py"
    spec = importlib.util.spec_from_file_location("character_image_api_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class FakeService:
    def get_config_section(self, name, *_):
        if name == "image_generation":
            return {
                "preset": "custom", "mode": "images", "base_url": "https://example.invalid/v1",
                "model": "custom-image", "size": "1024x1024", "timeout_seconds": 180,
                "api_key_env": "IMAGE_API_KEY",
            }
        return {}


class FakeResponse:
    status = 200

    def __init__(self, data):
        self.data = data

    async def text(self):
        return json.dumps(self.data)


class FakeSession:
    def __init__(self, data):
        self.data = data
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.data)


class ImageApiPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_qwen_and_grok_presets_fill_provider_specific_fields(self):
        page = ImageApiPage(FakeService())

        page.preset.setCurrentIndex(page.preset.findData("qwen"))
        page.apply_preset()
        self.assertEqual(page.mode.currentText(), "dashscope_multimodal")
        self.assertIn("{WorkspaceId}", page.base.text())
        self.assertEqual(page.model.text(), "qwen-image-2.0-pro")
        self.assertEqual(page.env.text(), "DASHSCOPE_API_KEY")
        self.assertEqual(page.size.currentText(), "2048*2048")

        page.preset.setCurrentIndex(page.preset.findData("grok"))
        page.apply_preset()
        self.assertEqual(page.mode.currentText(), "xai_images")
        self.assertEqual(page.base.text(), "https://api.x.ai/v1")
        self.assertEqual(page.model.text(), "grok-imagine-image-quality")
        self.assertEqual(page.env.text(), "XAI_API_KEY")
        self.assertEqual(page.size.currentText(), "")

    def test_preset_values_are_independent_copies(self):
        first = preset_config("qwen")
        first["model"] = "changed"
        self.assertEqual(preset_config("qwen")["model"], "qwen-image-2.0-pro")

    def test_qwen_request_uses_dashscope_shape_and_extracts_output_image(self):
        module = load_image_script()
        image = b"qwen-image"
        response = {
            "output": {"choices": [{"message": {"content": [
                {"image": "data:image/png;base64," + base64.b64encode(image).decode()}
            ]}}]}
        }
        session = FakeSession(response)
        settings = preset_config("qwen")
        with patch.object(module.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(module.call_api(settings, "key", "画一只猫", "generate", None))
        self.assertEqual(result, image)
        url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/services/aigc/multimodal-generation/generation"))
        self.assertEqual(kwargs["json"]["input"]["messages"][0]["content"], [{"text": "画一只猫"}])
        self.assertEqual(kwargs["json"]["parameters"]["size"], "2048*2048")

    def test_grok_request_uses_images_endpoint_without_generic_size(self):
        module = load_image_script()
        image = b"grok-image"
        response = {"data": [{"b64_json": base64.b64encode(image).decode()}]}
        session = FakeSession(response)
        settings = preset_config("grok")
        with patch.object(module.aiohttp, "ClientSession", return_value=session):
            result = asyncio.run(module.call_api(settings, "key", "a cat", "generate", None))
        self.assertEqual(result, image)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.x.ai/v1/images/generations")
        self.assertEqual(kwargs["json"]["model"], "grok-imagine-image-quality")
        self.assertNotIn("size", kwargs["json"])


if __name__ == "__main__":
    unittest.main()
