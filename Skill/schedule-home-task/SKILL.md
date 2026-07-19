---
name: schedule-home-task
description: Create, inspect, and manage local spoken reminders, alarms, and recurring schedules for HomeAgent. Use when the user asks to remind them at a time, wake them up, set an alarm, schedule a one-time action, or repeat something daily, on weekdays, weekly, or on selected weekdays.
---

# Schedule Home Task

Use HomeAgent's scheduling tools instead of merely promising to remind the user.

## Create a task

1. Resolve relative or natural-language time using the current local time in the system prompt.
2. Call `create_scheduled_task` with a concise title and the exact text TTS should speak.
3. Choose the recurrence:
   - `once`: provide `scheduled_at` as local ISO time. It is deleted only after successful execution.
   - `daily`: provide `time` as `HH:MM`.
   - `weekdays`: provide `time`; it runs Monday through Friday.
   - `weekly`: provide `time` and `weekdays`, where Monday is 1 and Sunday is 7.
4. Confirm creation briefly, for example “好，喝水提醒设置好了”。Do not read the ISO trigger time, task ID, storage path, or queue length aloud. Show detailed scheduling data only when the user explicitly asks for it.

Treat the current JSON files and tool result's `active_count` as the only authoritative task state. Never infer how many tasks exist from earlier assistant messages because completed one-time tasks may already have been deleted.

Never create a recurring schedule when the user requested a one-time reminder. Never claim a task exists without a successful tool result.

## Manage tasks

- Call `list_scheduled_tasks` when the user asks what reminders or alarms exist.
- Call `delete_scheduled_task` only when the user asks to cancel or remove one.
- After a reminder fires, call `acknowledge_scheduled_task` when the user's response meaningfully confirms it, such as “知道了”“喝了”“已经起来了” or an equivalent contextual response.
- Keep recurring tasks after execution. The scheduler automatically advances their next trigger time.

## Reminder interaction

Wait one minute after each spoken reminder for acknowledgement. If none is received, speak again up to two additional times. Complete the current cycle after acknowledgement or after the third total reminder. Delete a completed one-time task; retain a recurring task and calculate its next scheduled occurrence.

Task JSON files live in the project-relative `Task/` directory. The deterministic storage and recurrence implementation is in `scripts/task_manager.py`.
