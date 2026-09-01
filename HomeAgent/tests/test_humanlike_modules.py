"""人类化改造三大模块的单元测试（stdlib + aiohttp，不依赖 agent.py）。

运行：
    E:/Doc/AIAgent/.venv/Scripts/python.exe -m pytest tests/test_humanlike_modules.py -q
或：
    E:/Doc/AIAgent/.venv/Scripts/python.exe tests/test_humanlike_modules.py
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

# 让 home_modules 可被导入
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from home_modules.hierarchical_task import (  # noqa: E402
    HierarchicalTaskManager,
    TaskStatus,
)
from home_modules.knowledge_base import (  # noqa: E402
    KnowledgeBase,
    KnowledgeEntry,
    LocalKnowledgeBase,
    create_knowledge_base,
)
from home_modules.speech_pipeline import SpeechPipeline  # noqa: E402


class FakeAgent:
    """记录语音调用，用于测试并发流水线。"""

    def __init__(self):
        self.spoken = []

    async def _speak_with_fresh_session(self, text, status=None, ignore_cancel=False):
        await asyncio.sleep(0.001)
        self.spoken.append(text)


class TestHierarchicalTask(unittest.TestCase):
    def _plan(self):
        return {
            "is_task": True,
            "actionable": True,
            "domain": "desktop",
            "reasoning_short": "打开浏览器并搜索",
            "steps": ["打开 Edge 浏览器", "在搜索框输入关键词", "点击第一个结果"],
            "success_criteria": "搜索结果已打开",
        }

    def test_build_and_progress(self):
        mgr = HierarchicalTaskManager.build_from_plan(self._plan(), "帮我在网上搜一下猫")
        self.assertEqual(len(mgr.root.children), 3)
        self.assertAlmostEqual(mgr.progress_ratio(), 0.0)
        payload = mgr.progress_payload()
        self.assertEqual(payload["total_steps"], 3)
        self.assertEqual(payload["completed_steps"], 0)

    def test_begin_and_complete_steps(self):
        mgr = HierarchicalTaskManager.build_from_plan(self._plan(), "任务")
        n1 = mgr.begin_next_step()
        self.assertEqual(n1.status, TaskStatus.RUNNING)
        self.assertAlmostEqual(mgr.progress_ratio(), 0.5 / 3, places=3)
        # 完成当前并推进
        n2 = mgr.begin_next_step()
        self.assertEqual(n1.status, TaskStatus.COMPLETED)
        self.assertEqual(n2.status, TaskStatus.RUNNING)
        # 全部完成
        mgr.complete_current_step()
        mgr.complete_all()
        self.assertAlmostEqual(mgr.progress_ratio(), 1.0)
        self.assertEqual(mgr.counts()[TaskStatus.COMPLETED.value], 3)

    def test_subtask(self):
        mgr = HierarchicalTaskManager.build_from_plan(self._plan(), "任务")
        child = mgr.root.children[0]
        sub = mgr.add_subtask(child.id, "先激活窗口再输入")
        self.assertIsNotNone(sub)
        self.assertEqual(len(child.children), 1)
        self.assertEqual(child.children[0].parent_id, child.id)

    def test_fail_remaining(self):
        mgr = HierarchicalTaskManager.build_from_plan(self._plan(), "任务")
        mgr.fail_all_remaining("网络不可达")
        self.assertEqual(mgr.counts()[TaskStatus.FAILED.value], 3)


class TestKnowledgeBase(unittest.TestCase):
    def _kb(self) -> KnowledgeBase:
        tmp = tempfile.mkdtemp(prefix="kb_test_")
        return LocalKnowledgeBase(
            db_path=Path(tmp) / "kb.db",
            jsonl_path=Path(tmp) / "kb.jsonl",
        )

    def test_add_and_search(self):
        kb = self._kb()
        kb.add(KnowledgeEntry(
            id="k1", goal="打开网易云音乐播放稻香",
            approach="launch_app 网易云音乐; 搜索稻香; 点击播放",
            outcome="成功播放", domain="desktop",
            tags=["desktop", "网易云音乐", "播放"],
        ))
        kb.add(KnowledgeEntry(
            id="k2", goal="生成一张猫的动漫图",
            approach="comfy_edit_image", outcome="ok", domain="file",
            tags=["file", "comfy"],
        ))
        hits = kb.search("网易云音乐 播放", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["id"], "k1")

    def test_record_completed_task(self):
        kb = self._kb()
        rid = kb.record_completed_task(
            goal="打开浏览器搜索天气",
            plan={"domain": "desktop", "steps": ["打开Edge", "搜索天气"]},
            answer="已显示天气结果", success=True,
        )
        self.assertTrue(rid)
        hits = kb.search("搜索天气", k=3)
        self.assertEqual(len(hits), 1)
        self.assertIn("天气", hits[0]["goal"])

    def test_factory_local(self):
        tmp = tempfile.mkdtemp(prefix="kbf_")
        kb = create_knowledge_base({
            "backend": "local",
            "local": {"db_path": str(Path(tmp) / "x.db")},
        })
        self.assertIsInstance(kb, LocalKnowledgeBase)

    def test_factory_weaviate_fallback(self):
        # 不可达的 weaviate 应自动降级到本地
        tmp = tempfile.mkdtemp(prefix="kbf2_")
        kb = create_knowledge_base({
            "backend": "weaviate",
            "weaviate": {"url": "http://127.0.0.1:9", "timeout_seconds": 1},
            "local": {"db_path": str(Path(tmp) / "y.db")},
        })
        self.assertIsInstance(kb, LocalKnowledgeBase)


class TestSpeechPipeline(unittest.TestCase):
    def test_parallel_speech(self):
        async def run():
            agent = FakeAgent()
            pipe = SpeechPipeline(agent, enabled=True)
            pipe.start()
            pipe.acknowledge("好的，我来帮你处理")
            pipe.enqueue("已完成第一步：打开浏览器")
            pipe.enqueue("全部搞定啦")
            # 并发：主流程不必等待，直接 flush 时语音在后台播完
            await pipe.flush(timeout=10)
            return agent.spoken

        spoken = asyncio.run(run())
        self.assertEqual(spoken, [
            "好的，我来帮你处理",
            "已完成第一步：打开浏览器",
            "全部搞定啦",
        ])

    def test_disabled_noop(self):
        agent = FakeAgent()
        pipe = SpeechPipeline(agent, enabled=False)
        pipe.start()
        pipe.enqueue("不会播放")
        # 禁用时 enqueue 直接丢弃，flush 也无后台任务
        self.assertEqual(agent.spoken, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
