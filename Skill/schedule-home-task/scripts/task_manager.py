from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any


class TaskStore:
    """One JSON file per task, using local naive datetimes."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.replace(second=0, microsecond=0)

    @staticmethod
    def _parse_time(value: str) -> time:
        return datetime.strptime(str(value).strip(), "%H:%M").time()

    def _path(self, task_id: str) -> Path:
        safe = "".join(ch for ch in str(task_id) if ch.isalnum() or ch in "-_")
        if not safe or safe != task_id:
            raise ValueError("无效任务 ID")
        return self.root / f"{safe}.json"

    def _write(self, task: dict[str, Any]) -> None:
        path = self._path(str(task["id"]))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def create(self, *, title: str, message: str, recurrence: str = "once", scheduled_at: str = "", at_time: str = "", weekdays: list[int] | None = None, action: str = "tts", now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now()).replace(microsecond=0)
        recurrence = str(recurrence).lower().strip()
        if recurrence not in {"once", "daily", "weekdays", "weekly"}:
            raise ValueError("recurrence 必须是 once/daily/weekdays/weekly")
        if not str(message).strip():
            raise ValueError("提醒内容不能为空")
        schedule: dict[str, Any] = {"type": recurrence}
        if recurrence == "once":
            next_run = self._parse_iso(scheduled_at)
            if next_run <= now:
                raise ValueError("一次性任务时间必须晚于当前时间")
            schedule["scheduled_at"] = next_run.isoformat(timespec="minutes")
        else:
            clock = self._parse_time(at_time)
            days = list(range(1, 6)) if recurrence == "weekdays" else sorted({int(x) for x in (weekdays or [])})
            if recurrence == "weekly" and (not days or any(x < 1 or x > 7 for x in days)):
                raise ValueError("weekly 必须提供 1（周一）到 7（周日）的 weekdays")
            schedule["time"] = clock.strftime("%H:%M")
            if days: schedule["weekdays"] = days
            next_run = self._next_recurring(schedule, now - timedelta(seconds=1))
        task = {
            "id": uuid.uuid4().hex,
            "title": str(title).strip() or str(message).strip()[:30],
            "message": str(message).strip(),
            "action": str(action).strip().lower() or "tts",
            "schedule": schedule,
            "next_run_at": next_run.isoformat(timespec="minutes"),
            "delete_after_success": recurrence == "once",
            "status": "pending",
            "created_at": now.isoformat(timespec="seconds"),
            "last_run_at": None,
            "last_error": None,
            "reminder_attempts": 0,
            "retry_interval_minutes": 1,
            "max_reminder_retries": 2,
            "awaiting_acknowledgement": False,
        }
        with self._lock: self._write(task)
        return task

    def list(self) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            for path in self.root.glob("*.json"):
                try: result.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError): continue
        return sorted(result, key=lambda item: str(item.get("next_run_at", "")))

    def delete(self, task_id: str) -> bool:
        with self._lock:
            path = self._path(task_id)
            if not path.exists(): return False
            path.unlink(); return True

    def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = (now or datetime.now()).replace(microsecond=0)
        claimed = []
        with self._lock:
            for task in self.list():
                try: due = self._parse_iso(task["next_run_at"]) <= now
                except (KeyError, TypeError, ValueError): continue
                if task.get("status") == "running":
                    started = task.get("run_started_at")
                    if started and now - self._parse_iso(started) < timedelta(minutes=10): continue
                if not due: continue
                task["status"] = "running"
                task["run_started_at"] = now.isoformat(timespec="seconds")
                self._write(task); claimed.append(task)
        return claimed

    def finish(self, task_id: str, success: bool, error: str = "", now: datetime | None = None) -> dict[str, Any] | None:
        now = (now or datetime.now()).replace(microsecond=0)
        with self._lock:
            path = self._path(task_id)
            if not path.exists(): return None
            task = json.loads(path.read_text(encoding="utf-8"))
            task["last_run_at"] = now.isoformat(timespec="seconds")
            task["run_started_at"] = None
            task["last_error"] = None if success else str(error)[:1000]
            if success:
                task["reminder_attempts"] = int(task.get("reminder_attempts", 0)) + 1
                max_attempts = 1 + int(task.get("max_reminder_retries", 2))
                if task["reminder_attempts"] < max_attempts:
                    task["status"] = "awaiting_acknowledgement"
                    task["awaiting_acknowledgement"] = True
                    task["next_run_at"] = (now + timedelta(minutes=int(task.get("retry_interval_minutes", 1)))).isoformat(timespec="minutes")
                else:
                    return self._complete_cycle(path, task, now, acknowledged=False)
            else:
                task["status"] = "pending"
                task["next_run_at"] = (now + timedelta(minutes=1)).isoformat(timespec="minutes")
            self._write(task); return task

    def awaiting_acknowledgements(self) -> list[dict[str, Any]]:
        return [task for task in self.list() if task.get("awaiting_acknowledgement") or task.get("status") == "awaiting_acknowledgement"]

    def acknowledge(self, task_id: str = "", response: str = "", now: datetime | None = None) -> dict[str, Any] | None:
        now = (now or datetime.now()).replace(microsecond=0)
        with self._lock:
            waiting = self.awaiting_acknowledgements()
            if task_id: waiting = [task for task in waiting if task.get("id") == task_id]
            if not waiting: return None
            task = sorted(waiting, key=lambda item: str(item.get("last_run_at", "")))[-1]
            path = self._path(str(task["id"]))
            task["acknowledgement"] = str(response).strip()[:500]
            task["acknowledged_at"] = now.isoformat(timespec="seconds")
            return self._complete_cycle(path, task, now, acknowledged=True)

    def _complete_cycle(self, path: Path, task: dict[str, Any], now: datetime, acknowledged: bool) -> dict[str, Any]:
        task["awaiting_acknowledgement"] = False
        task["status"] = "pending"
        task["reminder_attempts"] = 0
        task["cycle_completed_at"] = now.isoformat(timespec="seconds")
        task["cycle_acknowledged"] = acknowledged
        if task.get("delete_after_success"):
            path.unlink()
            return {**task, "deleted": True}
        task["next_run_at"] = self._next_recurring(task["schedule"], now).isoformat(timespec="minutes")
        self._write(task)
        return task

    def _next_recurring(self, schedule: dict[str, Any], after: datetime) -> datetime:
        clock = self._parse_time(schedule["time"])
        allowed = schedule.get("weekdays")
        for offset in range(0, 15):
            day = after.date() + timedelta(days=offset)
            candidate = datetime.combine(day, clock)
            if candidate <= after: continue
            if allowed and candidate.isoweekday() not in allowed: continue
            return candidate
        raise ValueError("无法计算下一次执行时间")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[3] / "Task"))
    parser.add_argument("command", choices=["list"])
    args = parser.parse_args()
    print(json.dumps(TaskStore(args.root).list(), ensure_ascii=False, indent=2))
