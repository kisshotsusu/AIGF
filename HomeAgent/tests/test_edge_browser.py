import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent import HomeAgent
from home_modules.edge_browser import (
    EdgeBrowserClient,
    find_edge_executable,
    is_extension_target,
    target_label,
)


class EdgeBrowserClientTests(unittest.TestCase):
    def test_default_port_and_endpoint(self):
        client = EdgeBrowserClient()
        self.assertEqual(client.endpoint, "http://127.0.0.1:9223")
        self.assertTrue(client.config["enabled"])

    def test_custom_port_endpoint(self):
        client = EdgeBrowserClient({"port": 9222})
        self.assertEqual(client.endpoint, "http://127.0.0.1:9222")

    def test_find_edge_executable_returns_string(self):
        self.assertIsInstance(find_edge_executable(), str)

    def test_extension_target_detection(self):
        self.assertTrue(is_extension_target({"url": "chrome-extension://abc/index.html"}))
        self.assertFalse(is_extension_target({"url": "https://chatgpt.com/"}))
        self.assertFalse(is_extension_target({}))

    def test_target_label_falls_back_to_url(self):
        self.assertEqual(target_label({"title": "ChatGPT"}), "ChatGPT")
        self.assertEqual(target_label({"url": "https://example.com"}), "https://example.com")
        self.assertEqual(target_label({}), "（未命名标签）")

    def test_output_dir_resolves_under_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            client = EdgeBrowserClient({"project_root": directory, "output_dir": "out/edge"})
            output = client._output_dir()
            self.assertTrue(output.is_dir())
            self.assertEqual(output, (Path(directory) / "out" / "edge").resolve())


class EdgeToolSurfaceTests(unittest.TestCase):
    def _agent(self, enabled: bool):
        agent = HomeAgent.__new__(HomeAgent)
        agent.config = {"agent": {}, "vision_mcp": {"enabled": False}}
        agent.comfyui = None
        agent.cosyvoice = None
        agent.qwen_video = None
        agent.edge_browser = SimpleNamespace(config={"enabled": enabled})
        return agent

    def test_edge_tools_exposed_when_enabled(self):
        names = [tool["function"]["name"] for tool in self._agent(True)._tools(scoped=True)]
        for expected in (
            "edge_status",
            "edge_open_url",
            "edge_open_chatgpt",
            "edge_eval_js",
            "edge_screenshot",
        ):
            self.assertIn(expected, names)

    def test_edge_tools_hidden_when_disabled(self):
        names = [tool["function"]["name"] for tool in self._agent(False)._tools(scoped=True)]
        self.assertNotIn("edge_status", names)
        self.assertNotIn("edge_open_chatgpt", names)


if __name__ == "__main__":
    unittest.main()
