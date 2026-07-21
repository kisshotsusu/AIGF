import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from qt_app import HomeAgentWindow, TaskProgressCard


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

    def test_screen_care_settings_restart_or_stop_timer_immediately(self):
        class Timer:
            def __init__(self): self.started = None; self.stopped = False
            def start(self, milliseconds): self.started = milliseconds; self.stopped = False
            def stop(self): self.stopped = True

        target = SimpleNamespace(agent=SimpleNamespace(config={"screen_care": {"enabled": True, "interval_seconds": 600}}), screen_care_timer=Timer())
        HomeAgentWindow.apply_screen_care_settings(target)
        self.assertEqual(target.screen_care_timer.started, 600000)

        target.agent.config["screen_care"]["enabled"] = False
        HomeAgentWindow.apply_screen_care_settings(target)
        self.assertTrue(target.screen_care_timer.stopped)

    def test_screen_care_displays_pet_popup_and_chat_message(self):
        pet = SimpleNamespace(show_care_message=Mock())
        target = SimpleNamespace(
            agent=SimpleNamespace(config={"screen_care": {"show_message": True, "popup_enabled": True}}, character_name="苏苏"),
            pet=pet, append_message=Mock(), set_status=Mock(),
        )
        HomeAgentWindow._show_screen_care(target, "主人，记得喝水呀。")
        target.append_message.assert_called_once_with("assistant", "苏苏", "主人，记得喝水呀。")
        pet.show_care_message.assert_called_once_with("主人，记得喝水呀。")
        target.set_status.assert_called_once_with("就绪")

    def test_reminder_displays_chat_message_and_pet_popup_immediately(self):
        pet = SimpleNamespace(show_care_message=Mock())
        target = SimpleNamespace(
            agent=SimpleNamespace(character_name="苏苏"), pet=pet,
            append_message=Mock(), set_status=Mock(),
        )
        HomeAgentWindow._show_reminder(target, "主人，该喝水啦。")
        target.append_message.assert_called_once_with("assistant", "苏苏", "主人，该喝水啦。")
        pet.show_care_message.assert_called_once_with("主人，该喝水啦。")
        target.set_status.assert_called_once_with("提醒已送达，语音播放中…")


if __name__ == "__main__":
    unittest.main()
