import os
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME_AGENT = Path(__file__).resolve().parents[1]
if str(HOME_AGENT) not in sys.path:
    sys.path.insert(0, str(HOME_AGENT))

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from qt_app import ClipboardImageTextEdit, HomeAgentWindow


class InputQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window_stub(self):
        window = HomeAgentWindow.__new__(HomeAgentWindow)
        window.input_queue = deque()
        window.worker = None
        window.progress_card = None
        window.task_cancelled = False
        window.agent = Mock()
        window.agent.restart_requested = False
        window.send_btn = Mock()
        window.stop_btn = Mock()
        window.input = Mock()
        window.pending_image_path = None
        window.set_status = Mock()
        return window

    def test_text_editor_emits_clipboard_image_instead_of_inserting_text(self):
        editor = ClipboardImageTextEdit()
        received = []
        editor.image_pasted.connect(received.append)
        mime = QMimeData(); image = QImage(12, 8, QImage.Format_ARGB32); image.fill(0xFFFFFFFF); mime.setImageData(image)
        editor.insertFromMimeData(mime)
        self.assertEqual(len(received), 1)
        self.assertEqual((received[0].width(), received[0].height()), (12, 8))
        self.assertEqual(editor.toPlainText(), "")

    def test_busy_image_only_send_keeps_attachment_in_queue(self):
        window = self.make_window_stub(); window.worker = Mock(); window.worker.isRunning.return_value = True
        window.input.toPlainText.return_value = ""
        window.append_message = Mock(); window._start_task = Mock()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            image_path = handle.name
        window.pending_image_path = image_path
        window.send()
        self.assertEqual(list(window.input_queue), [("请分析这张截图。", image_path)])
        window._start_task.assert_not_called()
        Path(image_path).unlink(missing_ok=True)

    def test_busy_send_is_queued_and_input_remains_available(self):
        window = self.make_window_stub()
        window.worker = Mock()
        window.worker.isRunning.return_value = True
        window.input.toPlainText.return_value = "下一轮任务"
        window.append_message = Mock()
        window._start_task = Mock()

        window.send()

        self.assertEqual(["下一轮任务"], list(window.input_queue))
        window.input.clear.assert_called_once()
        window._start_task.assert_not_called()
        window.set_status.assert_called_with("已排队 1 项，当前任务结束后执行")

    @patch("qt_app.QTimer.singleShot", side_effect=lambda _delay, callback: callback())
    def test_finished_task_starts_next_queued_item(self, _single_shot):
        window = self.make_window_stub()
        window.input_queue.extend(["任务二", "任务三"])
        window._start_task = Mock()

        window.finish_task()

        window._start_task.assert_called_once_with("任务二")
        self.assertEqual(["任务三"], list(window.input_queue))

    @patch("qt_app.QTimer.singleShot")
    def test_restart_does_not_start_queued_item(self, single_shot):
        window = self.make_window_stub()
        window.agent.restart_requested = True
        window.input_queue.append("稍后任务")

        window.finish_task()

        single_shot.assert_not_called()
        self.assertEqual(["稍后任务"], list(window.input_queue))


if __name__ == "__main__":
    unittest.main()
