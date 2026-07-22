import unittest
from unittest.mock import patch
from PIL import Image

import agent


WINDOWS = [
    {
        "hwnd": 101,
        "title": "Home Agent",
        "process_name": "python.exe",
        "process_path": r"E:\Doc\AIAgent\.venv\Scripts\python.exe",
    },
    {
        "hwnd": 202,
        "title": "命令提示符",
        "process_name": "cmd.exe",
        "process_path": r"C:\Windows\System32\cmd.exe",
    },
]


class WindowResolutionTests(unittest.TestCase):
    def resolve(self, value):
        def fake_list_windows(query=""):
            if not query:
                return WINDOWS
            needle = str(query).casefold()
            return [item for item in WINDOWS if needle in item["title"].casefold()]

        with patch.object(agent, "list_windows", side_effect=fake_list_windows):
            return agent._find_window(value)

    def test_resolves_real_title(self):
        self.assertEqual(self.resolve("Home Agent")["hwnd"], 101)

    def test_resolves_process_path_returned_by_window_listing(self):
        self.assertEqual(self.resolve(r"E:\Doc\AIAgent\.venv\Scripts\python.exe")["hwnd"], 101)

    def test_resolves_process_name_and_hwnd(self):
        self.assertEqual(self.resolve("cmd.exe")["hwnd"], 202)
        self.assertEqual(self.resolve("101")["title"], "Home Agent")

    def test_window_capture_falls_back_from_hwnd_to_bounds(self):
        captured = Image.new("RGB", (8, 6), "white")
        with patch.object(agent.ImageGrab, "grab", side_effect=[OSError("PrintWindow failed"), captured]) as grab:
            result = agent._grab_windows_image(hwnd=101, bbox=(0, 0, 8, 6), attempts=1)
        self.assertEqual(result.size, (8, 6))
        self.assertEqual(grab.call_count, 2)
        result.close()

    def test_failed_post_action_capture_is_not_reported_as_success(self):
        before = Image.new("RGB", (8, 6), "white")
        with patch.object(agent, "_capture_window_info", side_effect=OSError("screen grab failed")), \
             patch.object(agent.time, "sleep"):
            evidence = agent._wait_and_compare_window({"hwnd": 101}, before)
        self.assertFalse(evidence["state_changed"])
        self.assertFalse(evidence["execution_likely_succeeded"])
        self.assertFalse(evidence["post_screenshot_captured"])
        before.close()


if __name__ == "__main__":
    unittest.main()
