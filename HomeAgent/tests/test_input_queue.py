import os
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import Mock, patch
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HOME_AGENT = Path(__file__).resolve().parents[1]
if str(HOME_AGENT) not in sys.path:
    sys.path.insert(0, str(HOME_AGENT))

from PySide6.QtCore import QMimeData, QPoint, QRect, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QScrollArea, QWidget
from qt_app import ChatWorker, ClipboardImageSaveWorker, ClipboardImageTextEdit, HomeAgentWindow


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
        window.pending_images = {}
        window.pending_send_after_images = False
        window.image_save_workers = {}
        window.max_pending_images = 8
        window.selected_attachment_queue = deque()
        window.adding_selected_attachments = False
        window.set_status = Mock()
        self.add_attachment_ui(window)
        return window

    def add_attachment_ui(self, window):
        window.attachment_panel = QFrame()
        window.attachment_scroll = QScrollArea()
        window.attachment_strip = QWidget()
        window.attachment_layout = QHBoxLayout(window.attachment_strip)
        window.attachment_layout.addStretch()
        window.attachment_scroll.setWidget(window.attachment_strip)
        window.attachment_placeholder = QLabel()

    def test_text_editor_emits_clipboard_image_instead_of_inserting_text(self):
        editor = ClipboardImageTextEdit()
        received = []
        editor.image_pasted.connect(received.append)
        mime = QMimeData(); image = QImage(12, 8, QImage.Format_ARGB32); image.fill(0xFFFFFFFF); mime.setImageData(image)
        editor.insertFromMimeData(mime)
        self.assertEqual(len(received), 1)
        self.assertEqual((received[0].width(), received[0].height()), (12, 8))
        self.assertEqual(editor.toPlainText(), "")

    def test_multiple_pasted_images_have_independent_previews_and_remove(self):
        window = self.make_window_stub()
        window.bridge = Mock()
        self.add_attachment_ui(window)
        image = QImage(320, 180, QImage.Format_ARGB32)
        image.fill(0xFF3A7F72)
        with tempfile.TemporaryDirectory() as folder, patch("qt_app.tempfile.gettempdir", return_value=folder):
            window.accept_pasted_image(image)
            window.accept_pasted_image(image)
            self.assertEqual(len(window.pending_images), 2)
            tokens = list(window.pending_images)
            self.assertTrue(all(not item["card"].preview.pixmap().isNull() for item in window.pending_images.values()))
            deadline = time.monotonic() + 3
            while any(item["saving"] for item in window.pending_images.values()) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.processEvents()
            saved = [Path(item["path"]) for item in window.pending_images.values()]
            self.assertTrue(all(path.is_file() for path in saved))
            window.remove_pending_image(tokens[0])
            self.assertEqual(list(window.pending_images), [tokens[1]])
            self.assertFalse(saved[0].exists())
            self.assertTrue(saved[1].exists())
            window.remove_pending_attachment()
            self.assertFalse(window.pending_images)
            self.assertFalse(saved[1].exists())

    def test_send_waits_for_background_image_save(self):
        window = self.make_window_stub()
        window.pending_images["saving"] = {"saving": True, "path": None, "card": Mock()}
        window.input.toPlainText.return_value = "分析图片"
        window._start_task = Mock()
        window.send()
        self.assertTrue(window.pending_send_after_images)
        window._start_task.assert_not_called()
        window.set_status.assert_called_with("附件正在后台处理，全部完成后自动发送…")

    def test_large_clipboard_image_is_not_encoded_on_ui_thread(self):
        window = self.make_window_stub()
        window.bridge = Mock()
        self.add_attachment_ui(window)
        image = QImage(3840, 2160, QImage.Format_ARGB32); image.fill(0xFF486F68)
        with tempfile.TemporaryDirectory() as folder, \
             patch("qt_app.tempfile.gettempdir", return_value=folder), \
             patch.object(ClipboardImageSaveWorker, "start") as start:
            window.accept_pasted_image(image)
            start.assert_called_once()
            item = next(iter(window.pending_images.values()))
            self.assertTrue(item["saving"])
            self.assertIsNone(item["path"])
            self.assertFalse(item["card"].preview.pixmap().isNull())
            self.assertEqual(list(Path(folder).rglob("*.png")), [])
            window.remove_pending_attachment()
            window.image_save_workers.clear()

    def test_file_picker_adds_image_and_file_without_deleting_originals(self):
        window = self.make_window_stub(); window.bridge = Mock()
        with tempfile.TemporaryDirectory() as folder:
            image_path = Path(folder) / "photo.png"
            image = QImage(32, 20, QImage.Format_ARGB32); image.fill(0xFF486F68); image.save(str(image_path))
            file_path = Path(folder) / "notes.txt"; file_path.write_text("hello", encoding="utf-8")
            with patch("qt_app.QFileDialog.getOpenFileNames", return_value=([str(image_path), str(file_path)], "")):
                window.choose_attachments()
            deadline = time.monotonic() + 2
            while window.adding_selected_attachments and time.monotonic() < deadline:
                self.app.processEvents()
            attachments = window._take_pending_attachments()
            self.assertEqual([item["kind"] for item in attachments], ["image", "file"])
            self.assertTrue(all(item["owned"] is False for item in attachments))
            self.assertTrue(image_path.exists())
            self.assertTrue(file_path.exists())

    def test_resize_edges_and_minimum_geometry(self):
        self.assertEqual(HomeAgentWindow.resize_edges_for_position(1, 1, 800, 500), Qt.LeftEdge | Qt.TopEdge)
        self.assertEqual(HomeAgentWindow.resize_edges_for_position(799, 250, 800, 500), Qt.RightEdge)
        self.assertFalse(HomeAgentWindow.resize_edges_for_position(400, 250, 800, 500))
        resized = HomeAgentWindow.resized_geometry(
            QRect(100, 100, 800, 500), QPoint(900, 700),
            Qt.LeftEdge | Qt.TopEdge, 640, 300,
        )
        self.assertEqual((resized.width(), resized.height()), (640, 300))
        self.assertEqual((resized.right(), resized.bottom()), (899, 599))

    def test_busy_image_only_send_keeps_attachment_in_queue(self):
        window = self.make_window_stub(); window.worker = Mock(); window.worker.isRunning.return_value = True
        window.input.toPlainText.return_value = ""
        window.append_message = Mock(); window._start_task = Mock()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            image_path = handle.name
        card = QFrame()
        window.pending_images["one"] = {"path": image_path, "saving": False, "card": card}
        window.send()
        self.assertEqual(list(window.input_queue), [(
            "请分析这个附件。",
            [{"path": image_path, "kind": "image", "owned": False}],
        )])
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

    def test_chat_worker_constructor_does_not_scan_workspace_on_ui_thread(self):
        agent = Mock()
        worker = ChatWorker(agent, "检查代码", Mock(), Mock())
        agent.begin_task.assert_not_called()
        worker.deleteLater()

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

    @patch("qt_app.QTimer.singleShot", side_effect=lambda _delay, callback: callback())
    def test_bridge_completion_keeps_running_worker_alive_until_qthread_finishes(self, _single_shot):
        window = self.make_window_stub()
        worker = Mock()
        worker.isRunning.return_value = True
        window.worker = worker
        window.input_queue.append("下一项")
        window._start_task = Mock()

        window.finish_task()

        self.assertIs(window.worker, worker)
        worker.deleteLater.assert_not_called()
        window._start_task.assert_not_called()

        window._worker_thread_finished(worker)

        self.assertIsNone(window.worker)
        worker.deleteLater.assert_called_once()
        window._start_task.assert_called_once_with("下一项")


if __name__ == "__main__":
    unittest.main()
