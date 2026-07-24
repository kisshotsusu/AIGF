import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSizePolicy

from qt_app import HomeAgentWindow, TaskProgressCard


class TaskProgressCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_details_are_expanded_by_default_and_can_be_toggled(self):
        card = TaskProgressCard()
        self.assertFalse(card.details.isHidden())
        self.assertEqual(card.toggle.text(), "⌄")

        card.toggle.click()
        self.assertTrue(card.details.isHidden())
        self.assertEqual(card.toggle.text(), "›")

        card.toggle.click()
        self.assertFalse(card.details.isHidden())

    def test_running_summary_and_finished_state_remain_compact(self):
        card = TaskProgressCard()
        card.update_progress({
            "current": "正在检查网页点击后的页面状态",
            "completed": ["打开网页", "定位搜索框"],
            "elapsed": 12,
            "reasoning_summary": "用户要求打开目标并确认点击后的结果。",
            "plan_steps": ["打开网页", "定位搜索框", "验证页面状态"],
            "success_criteria": "页面显示目标结果",
            "events": [
                {"type": "tool_start", "title": "读取网页", "detail": '{"max_chars":12000}', "elapsed": 2},
                {"type": "tool_complete", "title": "读取网页 · 完成", "detail": '{"status":"success"}', "elapsed": 3},
            ],
        })
        self.assertEqual(card.summary.text(), "正在检查网页点击后的页面状态")
        self.assertIn("• 打开网页", card.done.text())
        self.assertIn("用户要求打开目标", card.reasoning.text())
        self.assertIn("3. 验证页面状态", card.plan.text())
        self.assertIn("✓ 3s", card.activity.text())
        self.assertFalse(card.details.isHidden())
        card.finish()
        self.assertEqual(card.title.text(), "任务已完成")
        self.assertEqual(card.summary.text(), "执行完成 · 2 个步骤")
        self.assertFalse(card.details.isHidden())

    def test_activity_only_displays_latest_eight_summaries(self):
        card = TaskProgressCard()
        events = [
            {"type": "tool_complete", "title": f"步骤 {index}", "detail": "简短摘要", "elapsed": index}
            for index in range(10)
        ]
        card.update_progress({"current": "处理中", "events": events})
        self.assertNotIn("步骤 0", card.activity.text())
        self.assertNotIn("步骤 1", card.activity.text())
        self.assertIn("步骤 2", card.activity.text())
        self.assertIn("步骤 9", card.activity.text())

    def test_long_task_content_does_not_force_a_wide_card(self):
        card = TaskProgressCard()
        card.update_progress({
            "current": "正在处理" * 80,
            "reasoning_summary": "很长的判断摘要" * 80,
            "plan_steps": ["没有空格的超长任务步骤" * 80],
            "events": [{"type": "tool_complete", "title": "窗口读取", "detail": "技术结果" * 100}],
        })
        self.assertEqual(card.minimumWidth(), 0)
        self.assertEqual(card.summary.sizePolicy().horizontalPolicy(), QSizePolicy.Ignored)
        for label in (card.reasoning, card.plan, card.current, card.activity, card.done):
            self.assertEqual(label.minimumWidth(), 0)
            self.assertEqual(label.sizePolicy().horizontalPolicy(), QSizePolicy.Ignored)

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
