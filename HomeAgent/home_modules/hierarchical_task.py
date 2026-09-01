"""父子层级任务树。

把语义规划器产出的扁平 task_plan（含 steps 列表）转换为一棵「目标 -> 子步骤」
的任务树，并在工具循环执行过程中逐步推进、实时回报进度。

设计目标（对齐 架构/02_任务排序系统.md 的「模型驱动语义规划」）：
- 顶层节点 = 用户目标（父任务）
- 每个 step = 一个子任务（可再递归拆出孙任务）
- 执行时按序把 pending 子任务标记为 running，完成后 marking completed
- 任意时刻可产出 `progress_payload()` 供 UI / status 回调做实时反馈

该模块只依赖标准库，可独立 import 与单元测试，不依赖 agent.py。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

__all__ = [
    "TaskStatus",
    "TaskNode",
    "HierarchicalTaskManager",
]


class TaskStatus(str, Enum):
    """子任务生命周期状态。"""

    PENDING = "pending"      # 待执行
    RUNNING = "running"      # 执行中
    COMPLETED = "completed"  # 已完成（取得终态证据）
    FAILED = "failed"        # 执行失败
    BLOCKED = "blocked"      # 被前置条件阻塞
    SKIPPED = "skipped"      # 跳过（如被后续步骤取代）


# 进度条计算中各状态对应的权重（1=视作已推进完毕）
_STATUS_PROGRESS_WEIGHT = {
    TaskStatus.PENDING: 0.0,
    TaskStatus.RUNNING: 0.5,
    TaskStatus.COMPLETED: 1.0,
    TaskStatus.FAILED: 1.0,
    TaskStatus.BLOCKED: 0.0,
    TaskStatus.SKIPPED: 1.0,
}


@dataclass
class TaskNode:
    """任务树节点。"""

    id: str
    title: str
    parent_id: Optional[str] = None
    detail: str = ""
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0  # 0..1，父节点的 progress 由其子节点聚合
    children: list["TaskNode"] = field(default_factory=list)
    tool: str = ""          # 最近一次负责执行的工具（可选）
    result_summary: str = ""  # 结果摘要（可选）
    error: str = ""         # 失败原因（可选）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "progress": round(self.progress, 3),
            "detail": self.detail,
            "tool": self.tool,
            "result_summary": self.result_summary,
            "error": self.error,
            "children": [c.to_dict() for c in self.children],
        }


class HierarchicalTaskManager:
    """管理一棵父子任务树，并提供进度快照。"""

    def __init__(self, root: TaskNode) -> None:
        self.root = root
        self._by_id: dict[str, TaskNode] = {}
        self._index(root)

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    @classmethod
    def build_from_plan(
        cls, plan: dict[str, Any], user_text: str, *, max_steps: int = 12
    ) -> "HierarchicalTaskManager":
        """从语义规划器产出的 task_plan 构建任务树。

        - 根节点 = 用户目标（父任务）
        - 每个 step 成为根的直接子任务
        """
        goal = (str(user_text or "").strip()) or str(plan.get("success_criteria") or "用户任务")
        reasoning = str(plan.get("reasoning_short") or "").strip()
        root = TaskNode(
            id=_new_id(),
            title=_truncate(goal, 140),
            detail=reasoning,
            status=TaskStatus.RUNNING if plan.get("actionable") else TaskStatus.PENDING,
        )
        steps = [str(s).strip() for s in (plan.get("steps") or []) if str(s).strip()]
        for index, step in enumerate(steps[:max_steps], start=1):
            root.children.append(
                TaskNode(
                    id=_new_id(),
                    title=_truncate(step, 160),
                    parent_id=root.id,
                    detail=f"步骤 {index}",
                )
            )
        return cls(root)

    # ------------------------------------------------------------------ #
    # 索引与查找
    # ------------------------------------------------------------------ #
    def _index(self, node: TaskNode) -> None:
        self._by_id[node.id] = node
        for child in node.children:
            self._index(child)

    def get(self, node_id: str) -> Optional[TaskNode]:
        return self._by_id.get(node_id)

    def add_subtask(
        self, parent_id: str, title: str, detail: str = "", *, max_children: int = 24
    ) -> Optional[TaskNode]:
        """给某个节点（通常是某个子步骤）再拆出孙任务。返回新建节点。"""
        parent = self._by_id.get(parent_id)
        if parent is None or len(parent.children) >= max_children:
            return None
        node = TaskNode(
            id=_new_id(),
            title=_truncate(title, 160),
            parent_id=parent.id,
            detail=detail,
        )
        parent.children.append(node)
        self._by_id[node.id] = node
        return node

    # ------------------------------------------------------------------ #
    # 状态推进
    # ------------------------------------------------------------------ #
    def set_status(
        self,
        node_id: str,
        status: TaskStatus,
        *,
        tool: str = "",
        result_summary: str = "",
        error: str = "",
    ) -> None:
        node = self._by_id.get(node_id)
        if node is None:
            return
        node.status = status
        node.updated_at = time.time()
        if tool:
            node.tool = tool
        if result_summary:
            node.result_summary = result_summary
        if error:
            node.error = error
        if status == TaskStatus.RUNNING and node.started_at is None:
            node.started_at = time.time()
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED):
            node.completed_at = time.time()
        self._recompute(node.parent_id)

    def begin_next_step(self) -> Optional[TaskNode]:
        """推进游标：先把当前 running 的子任务标为 completed，
        再把下一个 pending 子任务标为 running，返回它。无更多步骤返回 None。"""
        for child in self.root.children:
            if child.status == TaskStatus.RUNNING:
                self.set_status(child.id, TaskStatus.COMPLETED)
        nxt = self.next_pending_child()
        if nxt is None:
            return None
        self.set_status(nxt.id, TaskStatus.RUNNING)
        return nxt

    def next_pending_child(self) -> Optional[TaskNode]:
        for child in self.root.children:
            if child.status == TaskStatus.PENDING:
                return child
        return None

    def complete_current_step(self) -> None:
        for child in self.root.children:
            if child.status == TaskStatus.RUNNING:
                self.set_status(child.id, TaskStatus.COMPLETED)

    def fail_current_step(self, reason: str = "") -> None:
        for child in self.root.children:
            if child.status == TaskStatus.RUNNING:
                self.set_status(child.id, TaskStatus.FAILED, error=reason)

    def complete_all(self) -> None:
        """任务整体成功时，把残留的 pending/running 一并标为 completed。"""
        for child in self.root.children:
            if child.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                self.set_status(child.id, TaskStatus.COMPLETED)

    def fail_all_remaining(self, reason: str = "") -> None:
        for child in self.root.children:
            if child.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                self.set_status(child.id, TaskStatus.FAILED, error=reason)

    # ------------------------------------------------------------------ #
    # 进度计算
    # ------------------------------------------------------------------ #
    def _recompute(self, parent_id: Optional[str]) -> None:
        if parent_id is None:
            return
        parent = self._by_id.get(parent_id)
        if parent is None or not parent.children:
            return
        weights = [_STATUS_PROGRESS_WEIGHT.get(c.status, 0.0) for c in parent.children]
        parent.progress = sum(weights) / len(weights)

    def progress_ratio(self) -> float:
        """整体进度 0..1。"""
        if not self.root.children:
            return 1.0 if self.root.status == TaskStatus.COMPLETED else 0.0
        return self.root.progress

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in TaskStatus}
        for child in self.root.children:
            out[child.status.value] += 1
        return out

    def current_step(self) -> Optional[TaskNode]:
        for child in self.root.children:
            if child.status == TaskStatus.RUNNING:
                return child
        return self.next_pending_child()

    # ------------------------------------------------------------------ #
    # 对外快照
    # ------------------------------------------------------------------ #
    def progress_payload(self, note: str = "") -> dict[str, Any]:
        """供 status 回调 / UI 实时渲染的进度快照。"""
        counts = self.counts()
        total = len(self.root.children)
        done = counts[TaskStatus.COMPLETED.value] + counts[TaskStatus.SKIPPED.value]
        cur = self.current_step()
        completed_titles = [
            c.title for c in self.root.children if c.status == TaskStatus.COMPLETED
        ]
        return {
            "type": "task_progress",
            "title": self.root.title,
            "detail": note or self.root.detail,
            "progress": round(self.progress_ratio(), 3),
            "total_steps": total,
            "completed_steps": done,
            "failed_steps": counts[TaskStatus.FAILED.value],
            "current": cur.title if cur else "",
            "completed": completed_titles,
            "status_counts": counts,
            "tree": self.root.to_dict(),
        }

    def one_line(self) -> str:
        """给语音/通知用的极简短句。"""
        cur = self.current_step()
        done = self.counts()[TaskStatus.COMPLETED.value]
        total = len(self.root.children)
        if cur:
            return f"正在做第 {done + 1}/{total} 步：{cur.title}"
        if total:
            return f"已完成 {done}/{total} 步"
        return self.root.title


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
