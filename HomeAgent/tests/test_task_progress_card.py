import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from qt_app import TaskProgressCard


class TaskProgressCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_details_are_collapsed_by_default_and_can_be_toggled(self):
        card = TaskProgressCard()
        self.assertTrue(card.details.isHidden())
        self.assertEqual(card.toggle.text(), "›")

        card.toggle.click()
        self.assertFalse(card.details.isHidden())
        self.assertEqual(card.toggle.text(), "⌄")

        card.toggle.click()
        self.assertTrue(card.details.isHidden())

    def test_running_summary_and_finished_state_remain_compact(self):
        card = TaskProgressCard()
        card.update_progress({
            "current": "正在检查网页点击后的页面状态",
            "completed": ["打开网页", "定位搜索框"],
            "elapsed": 12,
        })
        self.assertEqual(card.summary.text(), "正在检查网页点击后的页面状态")
        self.assertIn("• 打开网页", card.done.text())
        self.assertTrue(card.details.isHidden())

        card.finish()
        self.assertEqual(card.title.text(), "任务已完成")
        self.assertEqual(card.summary.text(), "执行完成 · 2 个步骤")
        self.assertTrue(card.details.isHidden())


if __name__ == "__main__":
    unittest.main()
