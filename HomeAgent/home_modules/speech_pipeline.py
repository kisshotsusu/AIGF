"""并发语音流水线（语音生成与任务处理并行）。

把「语音合成 + 播放」从 chat() 的主流程里剥离出来：主流程（LLM 工具循环）
负责干活，流水线在后台异步消费一个文本队列，边干边播，互不阻塞。

用法：
    pipeline = SpeechPipeline(agent, enabled=True)
    pipeline.start()                       # 启动后台消费协程
    pipeline.acknowledge("好的，我来帮你看看")  # 开场接单（即时播）
    pipeline.enqueue("已完成第一步：打开浏览器")  # 进度播报
    ...
    await pipeline.flush()                 # 等队列清空后关闭后台任务

agent 只需提供 `agent._speak_with_fresh_session(text, status, ignore_cancel)` 能力，
该方法的会话由它自己创建，因此流水线内部无需持有外部 session。

所有异常都被吞掉并记录，绝不影响主任务循环。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

__all__ = ["SpeechPipeline"]

logger = logging.getLogger("home_agent.speech_pipeline")


class SpeechPipeline:
    """基于 asyncio.Queue 的并发 TTS 流水线。"""

    def __init__(self, agent: Any, *, enabled: bool = True, max_queue: int = 32) -> None:
        self.agent = agent
        self.enabled = enabled
        self._queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue(maxsize=max_queue)
        self._task: Optional[asyncio.Task] = None
        self.spoken_count = 0
        self.dropped_count = 0
        self.errors = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动后台消费协程（幂等）。"""
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.ensure_future(self._drain())

    def _ensure_started(self) -> None:
        if self.enabled and self._task is None:
            self.start()

    async def flush(self, timeout: float = 45.0) -> None:
        """等待队列清空并关闭后台任务。"""
        if not self.enabled or self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except asyncio.TimeoutError:
            logger.warning("speech pipeline flush 超时，仍有 %d 条未播", self._queue.qsize())
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------ #
    # 入队
    # ------------------------------------------------------------------ #
    def acknowledge(self, text: str) -> None:
        """开场接单播报（与后续进度在队列中按序播放）。"""
        self.enqueue(text)

    def enqueue(self, text: str, *, urgent: bool = False) -> None:
        """把一段要朗读的文本送入队列。"""
        text = _clean(text)
        if not text:
            return
        if not self.enabled:
            return
        self._ensure_started()
        try:
            if urgent:
                self._queue.put_nowait(text)
            else:
                self._queue.put_nowait(text)
        except asyncio.QueueFull:
            # 队列积压过多时丢弃最旧的非紧急播报，避免无限增长
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(text)
            except asyncio.QueueFull:
                self.dropped_count += 1

    # ------------------------------------------------------------------ #
    # 后台消费
    # ------------------------------------------------------------------ #
    async def _drain(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self.agent._speak_with_fresh_session(text, None, ignore_cancel=True)
                self.spoken_count += 1
            except asyncio.CancelledError:
                self._queue.task_done()
                raise
            except Exception as exc:  # noqa: BLE001 - 语音失败绝不能影响主流程
                self.errors += 1
                logger.warning("speech pipeline 播报失败: %s", exc)
            finally:
                self._queue.task_done()

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def _clean(text: str) -> str:
    """去掉可能破坏 TTS 的标记符号与多余空白。"""
    if not text:
        return ""
    for ch in ("`", "*", "#", ">", "<", "|", "{", "}", "[", "]"):
        text = text.replace(ch, "")
    return " ".join(text.split()).strip()
