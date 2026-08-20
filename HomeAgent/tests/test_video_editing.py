import shutil
import tempfile
import unittest
from pathlib import Path

from home_modules.video_editing import VideoEditor


class SrtTests(unittest.TestCase):
    def setUp(self):
        self.editor = VideoEditor()

    def test_build_srt_expected_format(self):
        srt = self.editor.build_srt([
            {"start_time": "00:00:01", "end_time": "00:00:02.5", "text": "第一句"},
            {"start_time": "00:00:03", "end_time": "00:00:04", "text": "第二句"},
        ])
        self.assertEqual(
            srt,
            "1\n00:00:01,000 --> 00:00:02,500\n第一句\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n第二句\n",
        )

    def test_build_srt_distributes_plain_lines_over_duration(self):
        srt = self.editor.build_srt(["你好", "世界"], duration=2)
        self.assertIn("00:00:00,000 --> 00:00:01,000\n你好", srt)
        self.assertIn("00:00:01,000 --> 00:00:02,000\n世界", srt)

    def test_build_srt_requires_duration_for_plain_lines(self):
        with self.assertRaisesRegex(ValueError, "duration"):
            self.editor.build_srt(["你好"])

    def test_escape_filter_path_uses_forward_slashes_and_escaped_colon(self):
        escaped = VideoEditor._escape_filter_path(r"C:\My Folder\subs.srt")
        self.assertEqual(escaped, "C\\:/My Folder/subs.srt")

    def test_segments_reject_invalid_range(self):
        with self.assertRaisesRegex(ValueError, "结束"):
            self.editor._normalize_segments([{"start_time": "00:00:05", "end_time": "00:00:02"}])

    def test_concat_requires_at_least_one_video(self):
        with self.assertRaisesRegex(ValueError, "至少需要"):
            self.editor.concat_videos([])

    def test_concat_rejects_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            self.editor.concat_videos([Path("not_exists.mp4")])


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe 不可用",
)
class VideoEditorEndToEndTests(unittest.TestCase):
    def _make_source(self, editor, root: Path) -> Path:
        source = root / "src.mp4"
        editor._run([
            editor.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=5",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(source),
        ])
        return source

    def test_ffmpeg_pipeline_cut_subtitle_voiceover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editor = VideoEditor({"project_root": root, "output_dir": "out"})
            source = self._make_source(editor, root)
            info = editor.probe(source)
            self.assertGreaterEqual(info["duration_seconds"], 1.9)

            cut = editor.cut_segments(
                source,
                [{"start_time": "00:00:00", "end_time": "00:00:01", "event": "开头"}],
            )
            self.assertEqual(cut["count"], 1)
            self.assertTrue(Path(cut["clips"][0]["path"]).is_file())

            srt_path = editor.build_srt(
                [
                    {"start_time": "00:00:00", "end_time": "00:00:01", "text": "你好"},
                    {"start_time": "00:00:01", "end_time": "00:00:02", "text": "世界"},
                ],
                output_path=root / "subs.srt",
            )
            burned = editor.burn_subtitles(source, srt_path, root / "burned.mp4")
            self.assertTrue(Path(burned["path"]).is_file())

            voice = root / "voice.wav"
            editor._run([
                editor.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=660:duration=1", str(voice),
            ])
            mixed = editor.mix_voiceover(source, voice, root / "vo.mp4", mode="replace")
            self.assertTrue(Path(mixed["path"]).is_file())

            final = editor.finalize(source, srt_path, voice, root / "final.mp4")
            self.assertTrue(Path(final["path"]).is_file())

    def test_concat_videos_joins_two_clips_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editor = VideoEditor({"project_root": root, "output_dir": "out"})
            source = self._make_source(editor, root)
            clips = editor.cut_segments(
                source,
                [
                    {"start_time": "00:00:00", "end_time": "00:00:00.6", "event": "A"},
                    {"start_time": "00:00:00.6", "end_time": "00:00:01.2", "event": "B"},
                ],
            )
            paths = [clip["path"] for clip in clips["clips"]]
            merged = editor.concat_videos(paths, output_path=root / "merged.mp4")
            self.assertTrue(Path(merged["path"]).is_file())
            self.assertEqual(merged["count"], 2)
            duration = editor.probe(merged["path"]).get("duration_seconds") or 0
            self.assertGreaterEqual(duration, 1.1)


if __name__ == "__main__":
    unittest.main()
