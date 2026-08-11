import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from home_modules.comfyui_client import ComfyUIClient
from agent import HomeAgent


class ComfyUIClientTests(unittest.TestCase):
    def setUp(self):
        self.client = ComfyUIClient()

    def test_presets_cover_image_edit_video(self):
        self.assertEqual(self.client.PRESETS["qwen-image-2512"]["kind"], "image")
        self.assertEqual(self.client.PRESETS["anima"]["kind"], "image")
        self.assertEqual(self.client.PRESETS["qwen-image-edit-2511"]["kind"], "edit")
        self.assertEqual(self.client.PRESETS["minimax-h3"]["kind"], "video")
        self.assertEqual(self.client.PRESETS["qwen-image-edit-2511"]["min_steps"], 8)

    def test_qwen_image_workflow_has_core_chain(self):
        workflow = self.client._build_image_workflow(
            self.client.PRESETS["qwen-image-2512"], "a cat", "bad quality",
            1024, 1024, 4, 4.0, 123, True, "test/qwen",
        )
        self.assertEqual(workflow["1"]["class_type"], "UNETLoader")
        self.assertEqual(workflow["6"]["class_type"], "CLIPTextEncode")
        self.assertEqual(workflow["9"]["class_type"], "KSampler")
        self.assertEqual(workflow["9"]["inputs"]["steps"], 4)
        self.assertEqual(workflow["9"]["inputs"]["positive"], ["6", 0])
        self.assertEqual(workflow["10"]["class_type"], "VAEDecode")
        self.assertEqual(workflow["11"]["class_type"], "SaveImage")
        self.assertIn("qwen_image_2512_fp8", workflow["1"]["inputs"]["unet_name"])

    def test_anima_workflow_uses_stable_diffusion_clip(self):
        workflow = self.client._build_image_workflow(
            self.client.PRESETS["anima"], "anime girl", "worst quality",
            1024, 1024, 8, 4.0, 7, True, "test/anima",
        )
        self.assertEqual(workflow["2"]["inputs"]["type"], "stable_diffusion")
        self.assertEqual(workflow["8"]["class_type"], "EmptyLatentImage")

    def test_edit_workflow_loads_image_and_uses_edit_encoder(self):
        workflow = self.client._build_edit_workflow(
            self.client.PRESETS["qwen-image-edit-2511"], "photo.png", "make it pop art",
            "low quality", 4, 3.0, 9, True, "test/edit",
        )
        self.assertEqual(workflow["4"]["class_type"], "LoadImage")
        self.assertEqual(workflow["4"]["inputs"]["image"], "photo.png")
        self.assertEqual(workflow["8"]["class_type"], "TextEncodeQwenImageEditPlus")
        self.assertEqual(workflow["5"]["class_type"], "VAEEncode")
        self.assertEqual(workflow["10"]["inputs"]["latent_image"], ["5", 0])

    def test_edit_workflow_honors_denoise(self):
        workflow = self.client._build_edit_workflow(
            self.client.PRESETS["qwen-image-edit-2511"], "photo.png", "change pose",
            "low quality", 8, 3.0, 9, False, "test/edit", denoise=0.7,
        )
        self.assertEqual(workflow["10"]["inputs"]["denoise"], 0.7)

    def test_edit_steps_use_lightning_native_count_with_lora(self):
        preset = self.client.PRESETS["qwen-image-edit-2511"]
        self.assertEqual(self.client._edit_steps(preset, None, True), 4)
        self.assertEqual(self.client._edit_steps(preset, 12, True), 12)

    def test_edit_steps_use_full_quality_without_lora_and_clamp_minimum(self):
        preset = self.client.PRESETS["qwen-image-edit-2511"]
        self.assertEqual(self.client._edit_steps(preset, None, False), 20)
        self.assertEqual(self.client._edit_steps(preset, 2, False), 8)

    def test_video_workflow_builds_minimax_chain(self):
        workflow = self.client._build_video_workflow(
            self.client.PRESETS["minimax-h3"], "a cat walking", 1344, 768, 24, 6, 5, 24,
            None, "test/video", False,
        )
        self.assertEqual(workflow["1"]["class_type"], "UNETLoader")
        self.assertEqual(workflow["5"]["class_type"], "MiniMaxH3SigmaShift")
        self.assertEqual(workflow["6"]["class_type"], "MiniMaxH3ImageToVideo")
        self.assertEqual(workflow["6"]["inputs"]["length"], 24)
        self.assertEqual(workflow["11"]["inputs"]["latent_image"], ["6", 1])
        self.assertEqual(workflow["13"]["class_type"], "VAEDecodeAudio")
        self.assertEqual(workflow["14"]["class_type"], "CreateVideo")
        self.assertEqual(workflow["15"]["class_type"], "SaveVideo")
        self.assertNotIn("first_frame", workflow["6"]["inputs"])

    def test_video_workflow_supports_first_frame(self):
        workflow = self.client._build_video_workflow(
            self.client.PRESETS["minimax-h3"], "continue motion", 1344, 768, 24, 6, 5, 24,
            "start.png", "test/video", True,
        )
        self.assertEqual(workflow["20"]["class_type"], "LoadImage")
        self.assertEqual(workflow["6"]["inputs"]["first_frame"], ["20", 0])
        self.assertIn("int8", workflow["1"]["inputs"]["unet_name"])

    def test_video_workflow_accepts_explicit_unet_name(self):
        workflow = self.client._build_video_workflow(
            self.client.PRESETS["minimax-h3"], "continue motion", 1344, 768, 24, 6, 5, 24,
            None, "test/video", use_int8=False, unet_name="custom_video.safetensors",
        )
        self.assertEqual(workflow["1"]["inputs"]["unet_name"], "custom_video.safetensors")

    def test_select_video_unet_prefers_nvfp4_when_available(self):
        preset = self.client.PRESETS["minimax-h3"]
        unet, use_int8 = self.client._select_video_unet(preset, [preset["unet"], preset["unet_int8"]])
        self.assertEqual(unet, preset["unet"])
        self.assertFalse(use_int8)

    def test_select_video_unet_falls_back_to_int8_when_nvfp4_missing(self):
        preset = self.client.PRESETS["minimax-h3"]
        unet, use_int8 = self.client._select_video_unet(preset, [preset["unet_int8"]])
        self.assertEqual(unet, preset["unet_int8"])
        self.assertTrue(use_int8)

    async def test_list_models_marks_video_preset_availability(self):
        client = self.client
        client._object_info = AsyncMock(return_value={"UNETLoader": {"input": {"required": {"unet_name": [[], {}]}}}})
        client._available_diffusion_models = AsyncMock(return_value=[
            "anima-base-v1.0.safetensors",
            "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        ])
        result = await client.list_models()
        video = next(item for item in result["presets"] if item["name"] == "minimax-h3")
        self.assertTrue(video["available"])
        client._available_diffusion_models = AsyncMock(return_value=["anima-base-v1.0.safetensors"])
        result = await client.list_models()
        video = next(item for item in result["presets"] if item["name"] == "minimax-h3")
        self.assertFalse(video["available"])

    def test_clamp_size_snaps_to_supported(self):
        width, height = self.client._clamp_size(1300, 700, self.client.PRESETS["minimax-h3"]["sizes"])
        self.assertEqual((width, height), (1344, 768))
        width, height = self.client._clamp_size(1024, 1024, self.client.PRESETS["qwen-image-2512"]["sizes"])
        self.assertEqual((width, height), (1024, 1024))

    def test_collect_outputs_finds_images_and_videos(self):
        entry = {
            "outputs": {
                "10": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]},
                "15": {"video": [{"filename": "b.mp4", "subfolder": "video", "type": "output"}]},
            }
        }
        outputs = self.client._collect_outputs(entry)
        self.assertEqual(len(outputs), 2)
        names = {item["filename"] for item in outputs}
        self.assertEqual(names, {"a.png", "b.mp4"})

    def test_publish_media_invokes_callback_with_valid_entries(self):
        received = []
        HomeAgent._publish_media([
            {"path": r"E:\out\a.png", "kind": "image", "caption": ""},
            {"path": r"E:\out\b.mp4", "kind": "video", "caption": ""},
            {"path": "", "kind": "image"},
        ], received.append)
        self.assertEqual(len(received), 1)
        self.assertEqual(len(received[0]), 2)
        self.assertEqual(received[0][1]["kind"], "video")

    def test_publish_media_skips_missing_callback(self):
        HomeAgent._publish_media([{"path": "x.png", "kind": "image"}], None)

    def test_media_normalization_preserves_media_field(self):
        result = HomeAgent._normalize_tool_result("comfy_generate_image", {
            "ok": True, "media": [{"path": "a.png", "kind": "image"}],
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["media"][0]["path"], "a.png")

    def test_enrich_prompts_appends_suffix_and_default_negative(self):
        positive, negative = self.client._enrich_prompts(self.client.PRESETS["anima"], "a cat", "")
        self.assertIn("a cat", positive)
        self.assertIn("masterpiece", positive)
        self.assertIn("clean lineart", positive)
        self.assertIn("worst quality", negative)
        self.assertIn("bad hands", negative)

    def test_enrich_prompts_merges_user_negative_with_default(self):
        positive, negative = self.client._enrich_prompts(self.client.PRESETS["qwen-image-2512"], "cat", "blurry")
        self.assertIn("blurry", negative)
        self.assertIn("水印", negative)
        self.assertIn("cat", positive)

    def test_enrich_prompts_avoids_duplicate_suffix(self):
        positive, _ = self.client._enrich_prompts(self.client.PRESETS["anima"], "a cat, masterpiece, best quality", "")
        self.assertEqual(positive.count("masterpiece"), 1)

    def test_edit_enrich_protects_identity_and_anatomy(self):
        positive, negative = self.client._enrich_prompts(
            self.client.PRESETS["qwen-image-edit-2511"], "改成坐姿", "",
        )
        self.assertIn("same face", positive)
        self.assertIn("same outfit", positive)
        self.assertIn("五官崩坏", negative)
        self.assertIn("多余手指", negative)
        self.assertIn("身体结构错误", negative)

    async def test_generate_image_applies_enrichment_to_workflow(self):
        client = self.client
        captured = {}

        async def fake_run(workflow, filename_prefix, status=None, timeout_seconds=None):
            captured["workflow"] = workflow
            return {"ok": True, "media": []}

        client._run_workflow = fake_run
        await client.generate_image("a red apple", model="anima")
        positive = captured["workflow"]["6"]["inputs"]["text"]
        negative = captured["workflow"]["7"]["inputs"]["text"]
        self.assertIn("a red apple", positive)
        self.assertIn("masterpiece", positive)
        self.assertIn("worst quality", negative)


if __name__ == "__main__":
    unittest.main()
