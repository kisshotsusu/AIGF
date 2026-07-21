import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
