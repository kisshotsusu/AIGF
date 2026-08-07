import tempfile
import unittest
import wave
from pathlib import Path

from home_modules.cosyvoice_tts import CosyVoiceTTS
from agent import HomeAgent


class CosyVoiceTTSTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _client(self):
        return CosyVoiceTTS({
            "reference_dir": str(self.root / "refs"),
            "output_dir": str(self.root / "out"),
            "auto_start": False,
        })

    def _make_ref(self, name="ref.wav"):
        directory = self.root / "refs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000)
        return path

    def test_parse_stage_directions_strips_brackets_and_builds_instruction(self):
        parsed = CosyVoiceTTS.parse_stage_directions(
            "（声音破碎又绵长，带着颤意和满足，却依然温柔地回应着）嗯……啊……主人……我好舒服……（呼吸急促，声音又软又黏）",
        )
        self.assertNotIn("（", parsed["spoken_text"])
        self.assertNotIn("）", parsed["spoken_text"])
        self.assertIn("嗯", parsed["spoken_text"])
        self.assertIn("温柔", parsed["keywords"])
        self.assertIn("急促", parsed["keywords"])
        self.assertIn("温柔", parsed["instruct_text"])
        self.assertIn("黏腻", parsed["instruct_text"])
        self.assertIn("软", parsed["instruct_text"])
        self.assertIn("满足", parsed["instruct_text"])
        self.assertIn("语速缓慢", parsed["instruct_text"])
        self.assertNotIn("语速稍快", parsed["instruct_text"])

    def test_parse_stage_directions_without_brackets_keeps_text(self):
        parsed = CosyVoiceTTS.parse_stage_directions("今天天气真好")
        self.assertEqual(parsed["spoken_text"], "今天天气真好")
        self.assertEqual(parsed["directions"], [])
        self.assertEqual(parsed["instruct_text"], "")

    def test_parse_stage_directions_empty_after_removal(self):
        parsed = CosyVoiceTTS.parse_stage_directions("（只有括号，没有台词）")
        self.assertEqual(parsed["spoken_text"], "")
        self.assertEqual(len(parsed["directions"]), 1)

    def test_parse_stage_directions_maps_shy_and_weak_moods(self):
        parsed = CosyVoiceTTS.parse_stage_directions("（声音柔弱，语气娇羞）主人，我在呢。")
        self.assertIn("柔弱", parsed["instruct_text"])
        self.assertIn("娇羞", parsed["instruct_text"])
        self.assertEqual(parsed["spoken_text"], "主人，我在呢。")

    def test_resolve_reference_default_picks_first_wav(self):
        ref = self._make_ref()
        self._make_ref("second.wav")
        client = self._client()
        self.assertEqual(client._resolve_reference(""), ref.resolve())

    def test_resolve_reference_by_name(self):
        ref = self._make_ref("角色_温柔.wav")
        client = self._client()
        self.assertEqual(client._resolve_reference("角色_温柔.wav"), ref.resolve())

    def test_resolve_reference_missing_raises(self):
        self._make_ref()
        client = self._client()
        with self.assertRaises(FileNotFoundError):
            client._resolve_reference("不存在.wav")

    def test_resolve_reference_empty_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._client()._resolve_reference("")

    def test_save_wav_writes_24k_mono_wav(self):
        client = self._client()
        pcm = b"\x00\x00" * 24000
        target = client._save_wav(pcm, "test/voice")
        self.assertTrue(target.is_file())
        with wave.open(str(target), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 24000)
            self.assertEqual(handle.getnframes(), 24000)

    def test_relative_output_dir_resolves_against_project_root(self):
        client = CosyVoiceTTS({"output_dir": "outputs/cosyvoice", "project_root": str(self.root), "auto_start": False})
        self.assertTrue(Path(client.config["output_dir"]).is_absolute())
        self.assertEqual(Path(client.config["output_dir"]), self.root / "outputs" / "cosyvoice")

    def test_list_reference_audios_counts_wavs(self):
        self._make_ref("a.wav")
        self._make_ref("b.wav")
        result = self._client().list_reference_audios()
        self.assertEqual(result["count"], 2)

    def test_publish_media_accepts_audio(self):
        received = []
        HomeAgent._publish_media([{"path": "a.wav", "kind": "audio"}], received.append)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0]["kind"], "audio")


if __name__ == "__main__":
    unittest.main()
