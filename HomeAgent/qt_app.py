from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import yaml
from PySide6.QtCore import QObject, QPoint, QRect, QSize, QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontDatabase, QIcon, QImage, QImageReader, QImageWriter, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from agent import HOME_AGENT, ROOT, HomeAgent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from modules.live.ai_live_assistant.instance_lock import InstanceLock
from home_modules.system_startup import AUTOSTART_ARGUMENT, run_network_guard, set_windows_autostart


COLORS = {
    "window": "#F5F7F8", "panel": "#FFFFFF", "ink": "#172326",
    "muted": "#718083", "accent": "#16766F", "accent_hover": "#115F59",
    "soft": "#E7F4F1", "line": "#DFE7E7", "danger": "#C54D48",
}


class WakeWordListener(QThread):
    """Background thread that listens for wake words and triggers recording.
    
    Uses a two-stage approach:
    1. Energy-based voice activity detection (low CPU)
    2. Full STT transcription only when voice is detected
    """
    wake_detected = Signal(str)  # Emits the command after wake word
    
    def __init__(self, agent, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.running = False
        self.stream = None
        self.buffer = []
        self.buffer_duration = 2.0  # seconds of audio to keep in buffer
        self.sample_rate = 16000
        self.channels = 1
        self.energy_threshold = int(self.agent.config.get('prompt_wake', {}).get('energy_threshold', 50))
        self.silence_timeout = 1.5  # seconds of silence before processing
        self.cooldown_after_detect = 3.0  # seconds to wait after detection
        self.last_detection_time = 0
        
    def run(self):
        """Main listener loop."""
        self.running = True
        cfg = self.agent.config.get("microphone", {})
        device_id = cfg.get("device_id")
        self.sample_rate = cfg.get("sample_rate", 16000)
        self.channels = cfg.get("channels", 1)
        
        # Buffer to hold recent audio
        max_frames = int(self.buffer_duration * self.sample_rate / 1024)
        silence_frames = 0
        frames_needed = int(self.silence_timeout * self.sample_rate / 1024)
        is_speaking = False
        
        def audio_callback(indata, frames, time_info, status):
            if self.running:
                self.buffer.append(indata.copy())
                if len(self.buffer) > max_frames:
                    self.buffer.pop(0)
        
        try:
            self.stream = sd.InputStream(
                device=device_id,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=audio_callback,
                blocksize=1024
            )
            self.stream.start()
            self.agent.log_event("wake_listener_started", device=device_id)
            
            while self.running:
                time.sleep(0.1)  # Check every 100ms
                
                # Cooldown check
                if time.time() - self.last_detection_time < self.cooldown_after_detect:
                    continue
                
                if len(self.buffer) < 2:
                    continue
                
                # Calculate energy of recent audio
                recent_audio = np.concatenate(self.buffer[-2:], axis=0)
                energy = np.abs(recent_audio.astype(float)).mean()
                
                if energy > self.energy_threshold:
                    # Voice detected
                    silence_frames = 0
                    if not is_speaking:
                        is_speaking = True
                        self.agent.log_event("wake_listener_voice_started", energy=float(energy))
                else:
                    # Silence
                    if is_speaking:
                        silence_frames += 1
                        if silence_frames >= frames_needed:
                            # Speech ended, process the buffer
                            is_speaking = False
                            silence_frames = 0
                            self._process_audio()
                            
        except Exception as e:
            self.agent.log_event("wake_listener_error", error=str(e))
        finally:
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except:
                    pass
            self.agent.log_event("wake_listener_stopped")
                
    def _process_audio(self):
        """Process buffered audio for wake word detection."""
        if len(self.buffer) < 3:  # Need at least ~1.5 seconds
            return
            
        # Concatenate audio
        audio_data = np.concatenate(self.buffer, axis=0)
        self.buffer.clear()
        
        # Save to temp file
        temp_path = HOME_AGENT / "recordings" / "wake_temp.wav"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with wave.open(str(temp_path), "wb") as wf:
                wf.setnchannels(self.channels)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio_data.tobytes())
        except Exception as e:
            self.agent.log_event("wake_listener_save_error", error=str(e))
            return
        
        # Transcribe
        try:
            text = asyncio.run(self.agent.transcribe(temp_path))
            if not text:
                return
                
            self.agent.log_event("wake_listener_transcribed", text=text)
            
            # Check for wake words
            is_wake, command = self.agent.detect_wake_word(text)
            if is_wake and command:
                self.last_detection_time = time.time()
                self.agent.log_event("wake_word_detected", text=text, command=command)
                self.wake_detected.emit(command)
        except Exception as e:
            self.agent.log_event("wake_listener_transcribe_error", error=str(e))
        finally:
            # Clean up temp file
            try:
                temp_path.unlink(missing_ok=True)
            except:
                pass
                    
    def stop(self):
        """Stop the listener."""
        self.running = False
        self.wait(3000)


class Bridge(QObject):
    answer = Signal(str)
    error = Signal(str)
    status = Signal(str)
    finished = Signal()
    transcription = Signal(str)
    confirm = Signal(str, object)
    progress = Signal(object)
    reminder = Signal(str)


class ClipboardImageTextEdit(QTextEdit):
    """Text editor that turns a clipboard screenshot into an attachment."""
    image_pasted = Signal(object)

    def insertFromMimeData(self, source):
        if source is not None and source.hasImage():
            image = source.imageData()
            if isinstance(image, QPixmap): image = image.toImage()
            if isinstance(image, QImage) and not image.isNull():
                self.image_pasted.emit(image.copy())
                return
        super().insertFromMimeData(source)


class ClipboardImageSaveWorker(QThread):
    """Encode a clipboard image without blocking Qt's GUI event loop."""
    saved = Signal(str, str, int, int)
    failed = Signal(str, str)

    def __init__(self, token: str, image: QImage, path: Path):
        super().__init__()
        self.token = token
        self.image = image.copy()
        self.path = Path(path)

    def run(self):
        try:
            writer = QImageWriter(str(self.path), b"PNG")
            writer.setCompression(3)
            if not writer.write(self.image):
                raise RuntimeError(writer.errorString() or "PNG 编码失败")
            self.saved.emit(self.token, str(self.path), self.image.width(), self.image.height())
        except Exception as exc:
            try: self.path.unlink(missing_ok=True)
            except OSError: pass
            self.failed.emit(self.token, str(exc))


class FramelessResizeHandle(QWidget):
    """Transparent edge/corner handle for a frameless top-level window."""
    def __init__(self, owner, edges, cursor):
        super().__init__(owner)
        self.owner = owner; self.edges = edges; self.setCursor(QCursor(cursor))
        self.setAttribute(Qt.WA_StyledBackground, False)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton: return super().mousePressEvent(event)
        self.owner.resize_edges = self.edges
        self.owner.resize_start_global = event.globalPosition().toPoint()
        self.owner.resize_start_geometry = self.owner.geometry()
        self.owner.native_resizing = False
        handle = self.owner.windowHandle()
        if handle is not None:
            try: self.owner.native_resizing = bool(handle.startSystemResize(self.edges))
            except (AttributeError, RuntimeError): self.owner.native_resizing = False
        event.accept()

    def mouseMoveEvent(self, event):
        if self.owner.resize_edges and event.buttons() & Qt.LeftButton and not self.owner.native_resizing:
            delta = event.globalPosition().toPoint() - self.owner.resize_start_global
            self.owner.setGeometry(self.owner.resized_geometry(
                self.owner.resize_start_geometry, delta, self.edges,
                self.owner.minimumWidth(), self.owner.minimumHeight(),
            ))
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.owner._finish_resize()
        event.accept()


class ImagePreviewCard(QFrame):
    """One independently removable image or file card."""
    remove_requested = Signal(str)

    def __init__(self, token: str, image: QImage | None = None, file_name: str = ""):
        super().__init__(); self.token = token
        self.setObjectName("attachmentCard"); self.setFixedSize(128, 82)
        layout = QVBoxLayout(self); layout.setContentsMargins(4, 4, 4, 4)
        self.preview = QLabel(); self.preview.setObjectName("attachmentPreview"); self.preview.setAlignment(Qt.AlignCenter); self.preview.setWordWrap(True)
        if image is not None and not image.isNull():
            thumbnail = image.scaled(120, 74, Qt.KeepAspectRatio, Qt.FastTransformation)
            self.preview.setPixmap(QPixmap.fromImage(thumbnail))
        else:
            suffix = Path(file_name).suffix.upper().lstrip(".") or "FILE"
            self.preview.setText(f"📄 {suffix}\n{Path(file_name).name[:18]}")
        layout.addWidget(self.preview)
        self.close_button = QPushButton("×", self); self.close_button.setObjectName("attachmentClose")
        self.close_button.setToolTip("移除这个附件"); self.close_button.setFixedSize(22, 22)
        self.close_button.clicked.connect(lambda: self.remove_requested.emit(self.token)); self.close_button.raise_()

    def resizeEvent(self, event):
        self.close_button.move(self.width() - self.close_button.width() - 3, 3)
        super().resizeEvent(event)

    def mark_ready(self, width: int, height: int):
        self.setToolTip(f"已附加图片 · {width}×{height}")


class ChatWorker(QThread):
    def __init__(self, agent: HomeAgent, prompt: str, bridge: Bridge, confirm, attachments=None):
        super().__init__(); self.agent = agent; self.prompt = prompt; self.bridge = bridge; self.confirm = confirm; self.attachments = list(attachments or [])
        self.image_paths = [item["path"] for item in self.attachments if item.get("kind") == "image"]
        self.file_paths = [item["path"] for item in self.attachments if item.get("kind") == "file"]
        self.cleanup_paths = [item["path"] for item in self.attachments if item.get("owned")]
        self.loop = None; self.task = None; self.clock = None; self.report_tasks = set(); self.started_at = 0.0; self.current_step = ""; self.completed_steps = []; self.activity_events = []; self.plan_steps = []; self.reasoning_summary = ""; self.success_criteria = ""; self.last_report_at = 0.0; self.report_count = 0; self.answer_emitted = False
        self.agent.begin_task(prompt, resumed=prompt.startswith("这是重启或异常退出后自动恢复的未完成任务"))

    def publish_answer(self, answer: str) -> None:
        """Show the final text as soon as it exists; TTS may continue afterwards."""
        text = str(answer or "").strip()
        if text and not self.answer_emitted:
            self.answer_emitted = True
            self.bridge.answer.emit(text)

    async def progress_clock(self):
        while True:
            await asyncio.sleep(5)
            self.report_status(self.current_step or "正在处理任务…")

    @staticmethod
    async def drain_tasks(tasks):
        await asyncio.gather(*tasks, return_exceptions=True)

    def report_status(self, text):
        if isinstance(text, dict):
            event = dict(text)
            value = str(event.get("title") or event.get("current") or "正在处理任务").strip()
            detail = str(event.get("detail") or "").strip()
            event["title"] = value; event["detail"] = detail
            event.setdefault("elapsed", max(0, int(time.monotonic() - self.started_at)))
            event.setdefault("state", "running")
            if event.get("type") == "plan":
                self.plan_steps = [str(step) for step in event.get("steps", []) if str(step).strip()]
                self.reasoning_summary = str(event.get("reasoning_summary") or "")
                self.success_criteria = str(event.get("success_criteria") or "")
            else:
                self.activity_events.append(event)
                self.activity_events = self.activity_events[-16:]
            text = value
        value = str(text).strip()
        if not value: return
        if self.current_step and self.current_step != value and self.current_step not in self.completed_steps:
            self.completed_steps.append(self.current_step)
            self.completed_steps = self.completed_steps[-8:]
        if value.startswith("已完成："):
            done = value.removeprefix("已完成：").strip()
            if done and done not in self.completed_steps: self.completed_steps.append(done)
        else: self.current_step = value
        elapsed = max(0, int(time.monotonic() - self.started_at))
        snapshot = {
            "current": self.current_step, "completed": list(self.completed_steps), "elapsed": elapsed, "state": "running",
            "events": list(self.activity_events), "plan_steps": list(self.plan_steps),
            "reasoning_summary": self.reasoning_summary, "success_criteria": self.success_criteria,
        }
        self.agent.update_task_recovery(self.current_step, self.completed_steps)
        self.bridge.status.emit(value); self.bridge.progress.emit(snapshot)
        cfg = self.agent.config.get("progress_reporting", {})
        threshold = max(15, int(cfg.get("long_task_seconds", 60))); cooldown = max(30, int(cfg.get("tts_cooldown_seconds", 90))); limit = max(0, int(cfg.get("max_reports_per_task", 3)))
        reportable = not any(word in value for word in ("语音", "播放", "录音", "识别"))
        if cfg.get("enabled", True) and reportable and elapsed >= threshold and self.report_count < limit and time.monotonic() - self.last_report_at >= cooldown and self.loop:
            self.last_report_at = time.monotonic(); self.report_count += 1
            report = self.loop.create_task(self.agent.speak_progress_report(self.prompt, list(self.completed_steps), self.current_step, elapsed)); self.report_tasks.add(report); report.add_done_callback(self.report_tasks.discard)

    report_status.supports_structured_status = True

    def run(self):
        self.loop = asyncio.new_event_loop(); self.started_at = time.monotonic()
        try:
            asyncio.set_event_loop(self.loop)
            prompt = self.prompt
            if self.file_paths:
                listing = "\n".join(f"- {path}" for path in self.file_paths)
                prompt += f"\n\n[用户本轮手动附加了以下本地文件。请按任务需要使用 read_text_file 或代码读取工具读取，不要忽略附件：\n{listing}\n]"
            self.task = self.loop.create_task(self.agent.chat(prompt, self.report_status, self.confirm, self.publish_answer, image_path=self.image_paths))
            self.clock = self.loop.create_task(self.progress_clock())
            answer = self.loop.run_until_complete(self.task)
            self.agent.finalize_task_recovery(answer)
            self.publish_answer(answer)
        except asyncio.CancelledError:
            self.bridge.answer.emit("当前任务已停止。")
        except Exception as exc:
            self.agent.log_event("chat_error", error=str(exc), prompt=self.prompt)
            self.agent.self_upgrade.fail(str(exc))
            self.bridge.error.emit(str(exc))
        finally:
            if self.clock:
                self.clock.cancel(); self.loop.run_until_complete(self.drain_tasks([self.clock]))
            if self.report_tasks:
                for report in self.report_tasks: report.cancel()
                self.loop.run_until_complete(self.drain_tasks(list(self.report_tasks))); self.report_tasks.clear()
            self.task = None; self.clock = None; self.loop.close()
            for cleanup_path in self.cleanup_paths:
                try: Path(cleanup_path).unlink(missing_ok=True)
                except OSError: pass
            self.bridge.finished.emit()

    def cancel_task(self):
        self.agent.stop_current_task()
        if self.loop and self.task and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)


class ScreenCareWorker(QThread):
    cared = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: HomeAgent):
        super().__init__(); self.agent = agent

    def run(self):
        try:
            asyncio.run(self.agent.proactive_screen_care(self.cared.emit))
        except Exception as exc:
            self.failed.emit(str(exc))


class MessageBubble(QFrame):
    def __init__(self, role: str, name: str, text: str):
        super().__init__(); mine = role == "user"
        bubble_name = "bubbleUser" if mine else ("bubbleError" if role == "error" else "bubbleAgent")
        outer = QHBoxLayout(self); outer.setContentsMargins(10, 4, 10, 4)
        card = QFrame(); card.setMaximumWidth(620); layout = QVBoxLayout(card); layout.setContentsMargins(15, 11, 15, 12); layout.setSpacing(4)
        body = QLabel(text); body.setWordWrap(True); body.setTextInteractionFlags(Qt.TextSelectableByMouse); body.setObjectName("bubbleText")
        layout.addWidget(body)
        if mine: outer.addStretch(); outer.addWidget(card)
        else: outer.addWidget(card); outer.addStretch()
        card.setObjectName(bubble_name)


class TaskProgressCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("progressCard")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._expanded = True
        self._completed_count = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 12, 8)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(7)
        self.toggle = QPushButton("›")
        self.toggle.setObjectName("progressToggle")
        self.toggle.setFixedSize(22, 22)
        self.toggle.setToolTip("展开任务进度")
        self.toggle.clicked.connect(self.toggle_details)
        self.title = QLabel("正在执行任务")
        self.title.setObjectName("progressTitle")
        self.summary = QLabel("正在分析任务…")
        self.summary.setObjectName("progressSummary")
        self.summary.setMinimumWidth(0)
        self.summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.elapsed = QLabel("0 秒")
        self.elapsed.setObjectName("progressElapsed")
        top.addWidget(self.toggle)
        top.addWidget(self.title)
        top.addWidget(self.summary, 1)
        top.addWidget(self.elapsed)
        layout.addLayout(top)

        self.details = QFrame()
        self.details.setObjectName("progressDetails")
        self.details.setMinimumWidth(0)
        self.details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        detail_layout = QVBoxLayout(self.details)
        detail_layout.setContentsMargins(29, 2, 4, 2)
        detail_layout.setSpacing(5)
        self.reasoning = QLabel("判断摘要：正在理解任务…")
        self.reasoning.setWordWrap(True); self.reasoning.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reasoning.setObjectName("progressReasoning")
        self.plan = QLabel("计划：正在生成…")
        self.plan.setWordWrap(True); self.plan.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.plan.setObjectName("progressPlan")
        self.current = QLabel("当前：正在分析任务…")
        self.current.setWordWrap(True)
        self.current.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.current.setObjectName("progressCurrent")
        self.done = QLabel("已完成：暂无")
        self.done.setWordWrap(True)
        self.done.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.done.setObjectName("progressDone")
        self.activity = QLabel("活动记录：暂无")
        self.activity.setWordWrap(True); self.activity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.activity.setObjectName("progressActivity")
        for label in (self.reasoning, self.plan, self.current, self.activity, self.done):
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        detail_layout.addWidget(self.reasoning)
        detail_layout.addWidget(self.plan)
        detail_layout.addWidget(self.current)
        detail_layout.addWidget(self.activity)
        detail_layout.addWidget(self.done)
        layout.addWidget(self.details)
        self.details.show()
        self.toggle.setText("⌄")
        self.toggle.setToolTip("收起任务进度")

        self.started = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.elapsed.setText(f"{int(time.monotonic()-self.started)} 秒"))
        self.timer.start(1000)

    @staticmethod
    def _compact(text, limit=72):
        value = " ".join(str(text or "").split()) or "正在处理…"
        return value if len(value) <= limit else value[: limit - 1] + "…"

    def toggle_details(self):
        self._expanded = not self._expanded
        self.details.setVisible(self._expanded)
        self.toggle.setText("⌄" if self._expanded else "›")
        self.toggle.setToolTip("收起任务进度" if self._expanded else "展开任务进度")

    def update_progress(self, data):
        current = str(data.get("current") or "正在处理…")
        completed = data.get("completed") or []
        plan_steps = data.get("plan_steps") or []
        reasoning_raw = str(data.get("reasoning_summary") or "").strip()
        success_raw = str(data.get("success_criteria") or "").strip()
        reasoning = self._compact(reasoning_raw, 120) if reasoning_raw else ""
        success = self._compact(success_raw, 100) if success_raw else ""
        events = data.get("events") or []
        self._completed_count = len(completed)
        self.elapsed.setText(f"{int(data.get('elapsed', 0))} 秒")
        self.summary.setText(self._compact(current))
        self.summary.setToolTip(current if len(current) > 72 else "")
        self.reasoning.setText("判断摘要：" + (reasoning or "等待任务规划"))
        plan_lines = "\n".join(
            f"{index}. {self._compact(step, 90)}"
            for index, step in enumerate(plan_steps[:6], 1)
        ) or "等待任务规划"
        if success:
            plan_lines += f"\n完成标准：{success}"
        self.plan.setText("计划：\n" + plan_lines)
        self.current.setText("当前：" + current)
        activity_lines = []
        icons = {"tool_start": "→", "tool_complete": "✓", "tool_failed": "!", "verification": "◇", "decision": "•"}
        for event in events[-8:]:
            icon = icons.get(str(event.get("type")), "•")
            elapsed = int(event.get("elapsed", 0))
            title = str(event.get("title") or "活动")
            detail = str(event.get("detail") or "").strip()
            activity_lines.append(
                f"{icon} {elapsed}s  {self._compact(title, 42)}"
                + (f" · {self._compact(detail, 88)}" if detail else "")
            )
        self.activity.setText("活动记录：\n" + ("\n".join(activity_lines) if activity_lines else "暂无"))
        self.done.setText("已完成：" + ("\n".join(f"• {item}" for item in completed[-5:]) if completed else "暂无"))

    def finish(self, cancelled=False):
        self.timer.stop()
        self.title.setText("任务已停止" if cancelled else "任务已完成")
        if cancelled:
            self.summary.setText("已停止")
        else:
            count_text = f" · {self._completed_count} 个步骤" if self._completed_count else ""
            self.summary.setText("执行完成" + count_text)
        self.current.setText("当前：已结束")
        self.setProperty("finished", True)
        self.style().unpolish(self)
        self.style().polish(self)


class SettingsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner); self.owner = owner; self.agent = owner.agent; self._saving = False
        self.setWindowTitle("Home Agent 设置"); self.resize(680, 650)
        root = QVBoxLayout(self); root.setContentsMargins(22, 20, 22, 20); root.setSpacing(12)
        title = QLabel("设置"); title.setObjectName("dialogTitle"); root.addWidget(title)
        self.status = QLabel("所有修改都会实时保存"); self.status.setObjectName("muted"); root.addWidget(self.status)
        tabs = QTabWidget(); root.addWidget(tabs, 1)
        general = QWidget(); form = QFormLayout(general); form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
        cfg = self.agent.config; mic = cfg.get("microphone", {}); control = cfg.get("computer_control", {}); codex = cfg.get("codex_cli", {}); startup = cfg.get("system_startup", {})
        self.always_top = QCheckBox("窗口始终置顶"); self.always_top.setChecked(bool(cfg.get("desktop_pet", {}).get("always_on_top", True)))
        self.auto_send = QCheckBox("语音识别完成后自动发送"); self.auto_send.setChecked(bool(mic.get("auto_send_after_transcription", True)))
        self.control = QCheckBox("允许 Home Agent 使用电脑工具"); self.control.setChecked(bool(control.get("enabled", True)))
        self.full_access = QCheckBox("完整磁盘访问权限"); self.full_access.setChecked(bool(control.get("full_access", False)))
        self.confirm_file = QCheckBox("打开文件和网页前请求确认"); self.confirm_file.setChecked(bool(control.get("confirm_before_action", True)))
        self.confirm_app = QCheckBox("启动应用前请求确认"); self.confirm_app.setChecked(bool(control.get("confirm_launch_app", False)))
        self.codex = QCheckBox("启用 Codex CLI / MCP"); self.codex.setChecked(bool(codex.get("enabled", False)))
        self.system_autostart = QCheckBox("跟随 Windows 自动启动"); self.system_autostart.setChecked(bool(startup.get("enabled", False)))
        self.network_restart = QCheckBox("自动启动时持续断网则重启电脑"); self.network_restart.setChecked(bool(startup.get("restart_on_network_failure", False)))
        self.network_attempts = QSpinBox(); self.network_attempts.setRange(1, 5); self.network_attempts.setValue(min(5, max(1, int(startup.get("max_restart_attempts", 5))))); self.network_attempts.setSuffix(" 次")
        self.network_note = QLabel("仅在系统自动启动时检测；Bilibili、百度、腾讯均不可达才计为断网。")
        self.network_note.setWordWrap(True); self.network_note.setObjectName("muted")
        for label, widget in (("界面", self.always_top), ("语音", self.auto_send), ("电脑控制", self.control), ("权限", self.full_access), ("确认", self.confirm_file), ("确认", self.confirm_app), ("自动化", self.codex), ("系统启动", self.system_autostart), ("断网保护", self.network_restart)):
            form.addRow(label, widget); widget.toggled.connect(self.save)
        form.addRow("最多重启", self.network_attempts)
        form.addRow("说明", self.network_note)
        self.network_attempts.valueChanged.connect(self.save)
        self.system_autostart.toggled.connect(self._sync_startup_controls)
        self.network_restart.toggled.connect(self._sync_startup_controls)
        self._sync_startup_controls()
        tabs.addTab(general, "常规")

        shell_page = QWidget(); shell_form = QFormLayout(shell_page); shell_form.setContentsMargins(18, 18, 18, 18); shell_cfg = cfg.get("shell_execution", {})
        self.shell_enabled = QCheckBox("允许模型调用 PowerShell / Shell"); self.shell_enabled.setChecked(bool(shell_cfg.get("shell_enabled", True)))
        self.cmd_enabled = QCheckBox("允许模型调用 CMD"); self.cmd_enabled.setChecked(bool(shell_cfg.get("cmd_enabled", True)))
        self.shell_confirm = QCheckBox("每次执行命令前请求确认"); self.shell_confirm.setChecked(bool(shell_cfg.get("confirm_before_execute", False)))
        self.shell_timeout = QSpinBox(); self.shell_timeout.setRange(1, 300); self.shell_timeout.setSuffix(" 秒"); self.shell_timeout.setValue(int(shell_cfg.get("timeout_seconds", 60)))
        shell_note = QLabel("命令内容由主模型结合任务和工具反馈自主决定；本地执行器只负责权限、超时和输出回传。")
        shell_note.setWordWrap(True); shell_note.setObjectName("muted")
        shell_form.addRow("PowerShell", self.shell_enabled); shell_form.addRow("CMD", self.cmd_enabled); shell_form.addRow("执行确认", self.shell_confirm); shell_form.addRow("默认超时", self.shell_timeout); shell_form.addRow("说明", shell_note)
        tabs.addTab(shell_page, "命令")

        # Prompt wake settings tab
        wake_page = QWidget(); wake_form = QFormLayout(wake_page); wake_form.setContentsMargins(18, 18, 18, 18); wake_form.setSpacing(14)
        wake_cfg = cfg.get("prompt_wake", {})
        self.wake_enabled = QCheckBox("启用提示词唤醒"); self.wake_enabled.setChecked(bool(wake_cfg.get("enabled", False)))
        self.wake_auto_send = QCheckBox("唤醒后自动发送命令"); self.wake_auto_send.setChecked(bool(wake_cfg.get("auto_send_after_wake", True)))
        self.wake_confirmation = QCheckBox("唤醒时播放确认音"); self.wake_confirmation.setChecked(bool(wake_cfg.get("wake_confirmation_sound", True)))
        self.wake_timeout = QSpinBox(); self.wake_timeout.setRange(5, 60); self.wake_timeout.setSuffix(" 秒"); self.wake_timeout.setValue(int(wake_cfg.get("wake_timeout_seconds", 10)))
        self.wake_energy = QSpinBox(); self.wake_energy.setRange(10, 500); self.wake_energy.setSuffix(" (越小越灵敏)"); self.wake_energy.setValue(int(wake_cfg.get("energy_threshold", 50)))
        self.wake_words_input = QLineEdit(); self.wake_words_input.setPlaceholderText("输入唤醒词，用逗号分隔"); self.wake_words_input.setText(", ".join(wake_cfg.get("wake_words", ["苏苏", "小助手"])))
        wake_note = QLabel("启用后，语音输入以唤醒词开头时会自动提取后面的命令并执行。例如：苏苏，打开浏览器")
        wake_note.setWordWrap(True); wake_note.setObjectName("muted")
        wake_form.addRow("启用唤醒", self.wake_enabled); wake_form.addRow("自动发送", self.wake_auto_send); wake_form.addRow("确认音", self.wake_confirmation); wake_form.addRow("唤醒超时", self.wake_timeout); wake_form.addRow("灵敏度", self.wake_energy); wake_form.addRow("唤醒词列表", self.wake_words_input); wake_form.addRow("说明", wake_note)
        self.wake_enabled.toggled.connect(self._sync_wake_controls)
        self.wake_enabled.toggled.connect(self.defer_save)
        self.wake_auto_send.toggled.connect(self.defer_save)
        self.wake_confirmation.toggled.connect(self.defer_save)
        self.wake_timeout.valueChanged.connect(self.defer_save)
        self.wake_energy.valueChanged.connect(self.defer_save)
        self.wake_words_input.textChanged.connect(self.defer_save)
        self._sync_wake_controls()
        tabs.addTab(wake_page, "唤醒")
        self.shell_enabled.toggled.connect(self.save); self.cmd_enabled.toggled.connect(self.save); self.shell_confirm.toggled.connect(self.save); self.shell_timeout.valueChanged.connect(self.save)

        stt_page = QWidget(); stt_form = QFormLayout(stt_page); stt_form.setContentsMargins(18, 18, 18, 18); stt = cfg.get("stt", {})
        self.stt_mode = QComboBox(); self.stt_mode.addItems(["sound_mcp", "api", "faster_whisper"]); self.stt_mode.setCurrentText(str(stt.get("mode", "sound_mcp")))
        self.stt_url = QLineEdit(str(stt.get("api_url", ""))); self.stt_model = QLineEdit(str(stt.get("model", ""))); self.stt_language = QLineEdit(str(stt.get("language", "auto")))
        for label, widget in (("识别方式", self.stt_mode), ("API 地址", self.stt_url), ("模型", self.stt_model), ("语言", self.stt_language)):
            stt_form.addRow(label, widget)
            if isinstance(widget, QLineEdit): widget.textChanged.connect(self.defer_save)
            else: widget.currentTextChanged.connect(self.save)
        tabs.addTab(stt_page, "语音识别")
        progress_page=QWidget(); progress_form=QFormLayout(progress_page); progress_cfg=cfg.get("progress_reporting",{}); self.progress_enabled=QCheckBox("长任务自动进行语音进度汇报");self.progress_enabled.setChecked(bool(progress_cfg.get("enabled",True)));self.progress_seconds=QSpinBox();self.progress_seconds.setRange(15,1800);self.progress_seconds.setSuffix(" 秒");self.progress_seconds.setValue(int(progress_cfg.get("long_task_seconds",60)));self.progress_cooldown=QSpinBox();self.progress_cooldown.setRange(30,3600);self.progress_cooldown.setSuffix(" 秒");self.progress_cooldown.setValue(int(progress_cfg.get("tts_cooldown_seconds",90)));self.progress_reports=QSpinBox();self.progress_reports.setRange(0,10);self.progress_reports.setValue(int(progress_cfg.get("max_reports_per_task",3)));progress_form.addRow("进度播报",self.progress_enabled);progress_form.addRow("长任务判定",self.progress_seconds);progress_form.addRow("播报冷却",self.progress_cooldown);progress_form.addRow("单任务最多播报",self.progress_reports);tabs.addTab(progress_page,"任务进度")
        self.progress_enabled.toggled.connect(self.save);self.progress_seconds.valueChanged.connect(self.save);self.progress_cooldown.valueChanged.connect(self.save);self.progress_reports.valueChanged.connect(self.save)
        care_page = QWidget(); care_form = QFormLayout(care_page); care_form.setContentsMargins(18, 18, 18, 18)
        care_cfg = cfg.get("screen_care", {})
        self.screen_care_enabled = QCheckBox("定时观察屏幕并主动问候或关心")
        self.screen_care_enabled.setChecked(bool(care_cfg.get("enabled", True)))
        self.screen_care_minutes = QSpinBox(); self.screen_care_minutes.setRange(1, 1440); self.screen_care_minutes.setSuffix(" 分钟")
        self.screen_care_minutes.setValue(max(1, int(care_cfg.get("interval_seconds", 300)) // 60))
        care_note = QLabel("保存后立即生效；Home Agent 忙碌时会跳过本轮。截图经 MiMo 分析后立即删除。")
        care_note.setWordWrap(True); care_note.setObjectName("muted")
        care_form.addRow("屏幕关怀", self.screen_care_enabled); care_form.addRow("问候频率", self.screen_care_minutes); care_form.addRow("说明", care_note)
        tabs.addTab(care_page, "屏幕关怀")
        self.screen_care_enabled.toggled.connect(self._sync_screen_care_controls)
        self.screen_care_enabled.toggled.connect(self.save); self.screen_care_minutes.valueChanged.connect(self.save)
        self._sync_screen_care_controls()
        close = QPushButton("完成"); close.setObjectName("primaryButton"); close.clicked.connect(self.accept); root.addWidget(close, 0, Qt.AlignRight)
        upgrade_page = QWidget(); upgrade_form = QFormLayout(upgrade_page); upgrade_cfg = cfg.get("self_upgrade", {})
        self.upgrade_enabled = QCheckBox("允许 Home Agent 编辑和升级自身"); self.upgrade_enabled.setChecked(bool(upgrade_cfg.get("enabled", True)))
        self.upgrade_restart = QCheckBox("升级代码后自动重启并继续任务"); self.upgrade_restart.setChecked(bool(upgrade_cfg.get("auto_restart", True)))
        self.upgrade_validation = QCheckBox("重启前强制校验代码和配置"); self.upgrade_validation.setChecked(bool(upgrade_cfg.get("require_validation", True)))
        self.upgrade_attempts = QSpinBox(); self.upgrade_attempts.setRange(1, 5); self.upgrade_attempts.setValue(int(upgrade_cfg.get("max_restart_attempts", 2)))
        upgrade_form.addRow("自主升级", self.upgrade_enabled); upgrade_form.addRow("自动恢复", self.upgrade_restart); upgrade_form.addRow("安全校验", self.upgrade_validation); upgrade_form.addRow("最多连续重启", self.upgrade_attempts); tabs.addTab(upgrade_page, "自主升级")
        self.upgrade_enabled.toggled.connect(self.save); self.upgrade_restart.toggled.connect(self.save); self.upgrade_validation.toggled.connect(self.save); self.upgrade_attempts.valueChanged.connect(self.save)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.save)

    def defer_save(self): self.status.setText("正在保存…"); self.timer.start(450)

    def _sync_startup_controls(self):
        enabled = self.system_autostart.isChecked()
        self.network_restart.setEnabled(enabled)
        self.network_attempts.setEnabled(enabled and self.network_restart.isChecked())

    def _sync_screen_care_controls(self):
        self.screen_care_minutes.setEnabled(self.screen_care_enabled.isChecked())

    def _sync_wake_controls(self):
        enabled = self.wake_enabled.isChecked()
        self.wake_auto_send.setEnabled(enabled)
        self.wake_confirmation.setEnabled(enabled)
        self.wake_timeout.setEnabled(enabled)
        self.wake_energy.setEnabled(enabled)
        self.wake_words_input.setEnabled(enabled)

    def save(self):
        if self._saving: return
        self._saving = True
        try:
            path = HOME_AGENT / "config.yaml"; cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cfg.setdefault("desktop_pet", {})["always_on_top"] = self.always_top.isChecked()
            cfg.setdefault("microphone", {})["auto_send_after_transcription"] = self.auto_send.isChecked()
            control = cfg.setdefault("computer_control", {}); control["enabled"] = self.control.isChecked(); control["full_access"] = self.full_access.isChecked(); control["confirm_before_action"] = self.confirm_file.isChecked(); control["confirm_launch_app"] = self.confirm_app.isChecked()
            shell = cfg.setdefault("shell_execution", {}); shell["shell_enabled"] = self.shell_enabled.isChecked(); shell["cmd_enabled"] = self.cmd_enabled.isChecked(); shell["confirm_before_execute"] = self.shell_confirm.isChecked(); shell["timeout_seconds"] = self.shell_timeout.value(); shell.setdefault("max_output_chars", 20000)
            cfg.setdefault("codex_cli", {})["enabled"] = self.codex.isChecked()
            startup = cfg.setdefault("system_startup", {}); startup["enabled"] = self.system_autostart.isChecked(); startup["restart_on_network_failure"] = self.network_restart.isChecked(); startup["max_restart_attempts"] = min(5, self.network_attempts.value())
            stt = cfg.setdefault("stt", {}); stt["mode"] = self.stt_mode.currentText(); stt["api_url"] = self.stt_url.text().strip(); stt["model"] = self.stt_model.text().strip(); stt["language"] = self.stt_language.text().strip() or "auto"
            progress=cfg.setdefault("progress_reporting",{});progress["enabled"]=self.progress_enabled.isChecked();progress["long_task_seconds"]=self.progress_seconds.value();progress["tts_cooldown_seconds"]=self.progress_cooldown.value();progress["max_reports_per_task"]=self.progress_reports.value()
            care = cfg.setdefault("screen_care", {}); care["enabled"] = self.screen_care_enabled.isChecked(); care["interval_seconds"] = self.screen_care_minutes.value() * 60
            upgrade = cfg.setdefault("self_upgrade", {}); upgrade["enabled"] = self.upgrade_enabled.isChecked(); upgrade["auto_restart"] = self.upgrade_restart.isChecked(); upgrade["require_validation"] = self.upgrade_validation.isChecked(); upgrade["max_restart_attempts"] = self.upgrade_attempts.value()
            wake = cfg.setdefault("prompt_wake", {}); wake["enabled"] = self.wake_enabled.isChecked(); wake["auto_send_after_wake"] = self.wake_auto_send.isChecked(); wake["wake_confirmation_sound"] = self.wake_confirmation.isChecked(); wake["wake_timeout_seconds"] = self.wake_timeout.value(); wake["energy_threshold"] = self.wake_energy.value(); wake["wake_words"] = [w.strip() for w in self.wake_words_input.text().split(",") if w.strip()]
            temp = path.with_suffix(".yaml.tmp"); temp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"); temp.replace(path)
            set_windows_autostart(startup["enabled"], HOME_AGENT / "启动家庭Agent.bat")
            self._sync_startup_controls()
            self.agent.config = cfg; self.owner.apply_always_on_top(); self.owner.apply_screen_care_settings(); self.owner.apply_wake_listener_settings(); self.status.setText(f"已实时保存 · {datetime.now():%H:%M:%S}")
        except Exception as exc: self.status.setText(f"保存失败：{exc}")
        finally: self._saving = False


class InspectorDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner); self.owner = owner; self.setWindowTitle("日志与上下文"); self.resize(900, 650)
        layout = QVBoxLayout(self); tabs = QTabWidget(); layout.addWidget(tabs)
        logs = QTextBrowser(); context = QTextBrowser(); tools = QTextBrowser(); tabs.addTab(logs, "运行日志"); tabs.addTab(context, "模型上下文"); tabs.addTab(tools, "工具")
        files = owner.agent_log_files()
        if files:
            try: logs.setPlainText(files[0].read_text(encoding="utf-8", errors="replace")[-300000:])
            except OSError as exc: logs.setPlainText(str(exc))
        try: context.setPlainText(owner.agent.context_snapshot())
        except Exception as exc: context.setPlainText(str(exc))
        tools.setPlainText("\n\n".join(f"{x.get('function', {}).get('name')}\n{x.get('function', {}).get('description', '')}" for x in owner.agent._tools()))


class HomeAgentWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.agent = HomeAgent(); self.bridge = Bridge(); self.worker = None; self.input_queue = deque(); self.screen_care_worker = None; self.recording = False; self.stream = None; self.frames = []; self.drag_pos = None; self.force_quit = False; self.pet = None; self.progress_card = None; self.task_cancelled = False
        self.pending_images = {}; self.pending_send_after_images = False; self.image_save_workers = {}; self.max_pending_images = 8
        self.selected_attachment_queue = deque(); self.adding_selected_attachments = False
        self.resize_margin = 9; self.resize_edges = Qt.Edges(); self.resize_start_global = None; self.resize_start_geometry = None; self.native_resizing = False
        self.setWindowTitle(f"{self.agent.character_name} · Home Agent"); self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window); self.setAttribute(Qt.WA_TranslucentBackground); self.resize(860, 380); self.setMinimumSize(640, 300)
        self._build(); self._create_resize_handles(); self._connect(); self.apply_always_on_top()
        self.bridge.finished.connect(self._restart_if_requested)
        self.scheduler = QTimer(self); self.scheduler.timeout.connect(self.poll_tasks); self.scheduler.start(10000)
        care_cfg = self.agent.config.get("screen_care", {})
        self.screen_care_timer = QTimer(self); self.screen_care_timer.timeout.connect(self.run_screen_care)
        if care_cfg.get("enabled", True): self.screen_care_timer.start(max(60, int(care_cfg.get("interval_seconds", 300))) * 1000)
        QTimer.singleShot(1800, self.resume_interrupted_task)
        
        # Initialize wake word listener
        self.wake_listener = None
        self._start_wake_listener()

    def _build(self):
        shell = QFrame(); shell.setObjectName("shell"); self.setCentralWidget(shell)
        shadow = QGraphicsDropShadowEffect(self); shadow.setBlurRadius(35); shadow.setOffset(0, 8); shadow.setColor(QColor(20, 50, 50, 85)); shell.setGraphicsEffect(shadow)
        root = QVBoxLayout(shell); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        header = QFrame(); header.setObjectName("header"); header.setFixedHeight(56); h = QHBoxLayout(header); h.setContentsMargins(18, 0, 12, 0)
        icon = QLabel(); icon.setFixedSize(40, 40); image_path = ROOT / "workspace" / "character_images" / "桌宠图标.png"
        if image_path.exists(): icon.setPixmap(QPixmap(str(image_path)).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand = QVBoxLayout(); self.name_label = QLabel(self.agent.character_name); self.name_label.setObjectName("brand"); self.status = QLabel("就绪"); self.status.setObjectName("headerStatus"); brand.addWidget(self.name_label); brand.addWidget(self.status)
        h.addWidget(icon); h.addSpacing(8); h.addLayout(brand); h.addStretch()
        for text, tip, slot, obj, width in (("☰", "日志与上下文", self.open_inspector, "titleButton", 42), ("⚙", "设置", self.open_settings, "titleButton", 42), ("—", "最小化", self.showMinimized, "titleButton", 38), ("×", "关闭", self.close, "closeButton", 38)):
            button = QPushButton(text); button.setToolTip(tip); button.setObjectName(obj); button.setFixedSize(width, 38); button.clicked.connect(slot); h.addWidget(button)
        root.addWidget(header)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.messages = QWidget(); self.messages.setMinimumWidth(0); self.messages.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.message_layout = QVBoxLayout(self.messages); self.message_layout.setContentsMargins(18, 10, 18, 8); self.message_layout.setSpacing(6); self.message_layout.addStretch(); self.scroll.setWidget(self.messages); root.addWidget(self.scroll, 1)
        composer = QFrame(); composer.setObjectName("composer"); c = QHBoxLayout(composer); c.setContentsMargins(14, 10, 14, 12); c.setSpacing(8)
        input_box = QVBoxLayout(); input_box.setSpacing(4)
        self.attachment_panel = QFrame(); self.attachment_panel.setObjectName("attachmentPanel"); self.attachment_panel.setFixedHeight(106)
        panel_layout = QVBoxLayout(self.attachment_panel); panel_layout.setContentsMargins(4, 2, 4, 2)
        self.attachment_scroll = QScrollArea(); self.attachment_scroll.setFrameShape(QFrame.NoFrame); self.attachment_scroll.setWidgetResizable(True)
        self.attachment_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded); self.attachment_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.attachment_strip = QWidget(); self.attachment_strip.setMinimumHeight(82); self.attachment_layout = QHBoxLayout(self.attachment_strip)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0); self.attachment_layout.setSpacing(7); self.attachment_layout.addStretch()
        self.attachment_scroll.setWidget(self.attachment_strip); panel_layout.addWidget(self.attachment_scroll)
        self.attachment_panel.hide(); input_box.addWidget(self.attachment_panel)
        input_row = QHBoxLayout(); input_row.setContentsMargins(0, 0, 0, 0); input_row.setSpacing(7)
        self.add_attachment_btn = QPushButton("+"); self.add_attachment_btn.setObjectName("addAttachment"); self.add_attachment_btn.setToolTip("选择图片或文件"); self.add_attachment_btn.setFixedSize(42, 56)
        self.input = ClipboardImageTextEdit(); self.input.setObjectName("input"); self.input.setPlaceholderText("输入任务、粘贴图片或点击左侧＋添加文件…  Ctrl + Enter 发送"); self.input.setMaximumHeight(76); self.input.setMinimumHeight(56)
        input_row.addWidget(self.add_attachment_btn, 0, Qt.AlignBottom); input_row.addWidget(self.input, 1); input_box.addLayout(input_row)
        c.addLayout(input_box, 1)
        actions = QVBoxLayout(); actions.setSpacing(8); top = QHBoxLayout(); self.voice_btn = QPushButton("语音"); self.voice_btn.setObjectName("softButton"); self.send_btn = QPushButton("发送"); self.send_btn.setObjectName("primaryButton"); top.addWidget(self.voice_btn); top.addWidget(self.send_btn); actions.addLayout(top)
        self.stop_btn = QPushButton("停止当前任务"); self.stop_btn.setObjectName("stopButton"); self.stop_btn.setEnabled(False); actions.addWidget(self.stop_btn); c.addLayout(actions); root.addWidget(composer)

    def _connect(self):
        self.send_btn.clicked.connect(self.send); self.stop_btn.clicked.connect(self.stop_task); self.voice_btn.clicked.connect(self.toggle_record); QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self.send)
        self.input.image_pasted.connect(self.accept_pasted_image); self.add_attachment_btn.clicked.connect(self.choose_attachments)
        self.bridge.answer.connect(lambda text: self.append_message("assistant", self.agent.character_name, text)); self.bridge.error.connect(lambda text: self.append_message("error", "错误", text)); self.bridge.status.connect(self.set_status); self.bridge.progress.connect(self.update_task_progress); self.bridge.reminder.connect(self._show_reminder); self.bridge.finished.connect(self.finish_task); self.bridge.transcription.connect(self.accept_transcription); self.bridge.confirm.connect(self.show_confirmation)

    def append_message(self, role, name, text):
        self.message_layout.insertWidget(self.message_layout.count() - 1, MessageBubble(role, name, str(text)))
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def set_status(self, text): self.status.setText(str(text))
    def update_task_progress(self, data):
        if self.progress_card: self.progress_card.update_progress(data)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))
    def send(self):
        text = self.input.toPlainText().strip()
        if self.adding_selected_attachments or self.selected_attachment_queue or any(item["saving"] for item in self.pending_images.values()):
            self.pending_send_after_images = True
            self.set_status("附件正在后台处理，全部完成后自动发送…")
            return
        attachments = self._take_pending_attachments()
        if not text and not attachments: return
        if not text: text = "请分析这些附件。" if len(attachments) > 1 else "请分析这个附件。"
        suffix = f"\n[已附加 {len(attachments)} 个图片/文件]" if attachments else ""
        self.input.clear(); self.append_message("user", self.agent.config.get("home", {}).get("user_name", "你"), text + suffix)
        if self.worker and self.worker.isRunning():
            self.input_queue.append((text, attachments) if attachments else text)
            self.set_status(f"已排队 {len(self.input_queue)} 项，当前任务结束后执行")
            self.input.setFocus()
            return
        self._start_task(text, attachments)

    def _start_task(self, text, attachments=None):
        self.progress_card=TaskProgressCard(); self.message_layout.insertWidget(self.message_layout.count()-1,self.progress_card); self.task_cancelled=False; self.agent.begin_task(); self.send_btn.setEnabled(True); self.stop_btn.setEnabled(True); self.set_status("正在思考…")
        worker = ChatWorker(self.agent, text, self.bridge, self.confirm_action, attachments=attachments)
        self.worker = worker
        worker.finished.connect(lambda worker=worker: self._worker_thread_finished(worker))
        worker.start()

    def choose_attachments(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择图片或文件", "", "所有文件 (*.*)")
        capacity = max(0, self.max_pending_images - len(self.pending_images) - len(self.selected_attachment_queue))
        accepted = paths[:capacity]
        self.selected_attachment_queue.extend(accepted)
        if len(accepted) < len(paths):
            self.bridge.error.emit(f"一次最多附加 {self.max_pending_images} 个图片或文件")
        if accepted and not self.adding_selected_attachments:
            self.adding_selected_attachments = True
            self.set_status(f"正在添加 {len(accepted)} 个附件…")
            QTimer.singleShot(0, self._add_next_selected_attachment)

    @staticmethod
    def _read_thumbnail(path: Path) -> QImage:
        """Decode only thumbnail-sized pixels instead of blocking on a full image."""
        reader = QImageReader(str(path))
        if not reader.canRead():
            return QImage()
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(QSize(120, 74), Qt.KeepAspectRatio))
        reader.setAutoTransform(True)
        return reader.read()

    def _add_next_selected_attachment(self):
        if not self.selected_attachment_queue:
            self.adding_selected_attachments = False
            if self.pending_images:
                self.set_status(f"已附加 {len(self.pending_images)} 个图片或文件")
            if self.pending_send_after_images and not any(item["saving"] for item in self.pending_images.values()):
                self.pending_send_after_images = False
                QTimer.singleShot(0, self.send)
            return
        selected = self.selected_attachment_queue.popleft()
        path = Path(selected).expanduser().resolve()
        if path.is_file() and len(self.pending_images) < self.max_pending_images:
            image = self._read_thumbnail(path)
            token = uuid.uuid4().hex
            kind = "image" if not image.isNull() else "file"
            card = ImagePreviewCard(token, image if kind == "image" else None, path.name)
            card.remove_requested.connect(self.remove_pending_image); card.setToolTip(str(path))
            self.pending_images[token] = {"path": str(path), "saving": False, "card": card, "kind": kind, "owned": False}
            self.attachment_layout.insertWidget(self.attachment_layout.count() - 1, card)
            self.attachment_panel.show(); self._resize_attachment_strip()
        QTimer.singleShot(0, self._add_next_selected_attachment)

    def accept_pasted_image(self, image):
        if not isinstance(image, QImage) or image.isNull():
            self.bridge.error.emit("剪贴板图片无效，无法预览")
            return
        if len(self.pending_images) >= self.max_pending_images:
            self.bridge.error.emit(f"一次最多附加 {self.max_pending_images} 张图片，请先移除或发送已有图片")
            return
        folder = Path(tempfile.gettempdir()) / "home-agent-clipboard"; folder.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        path = folder / f"clipboard_{token}.png"
        card = ImagePreviewCard(token, image); card.remove_requested.connect(self.remove_pending_image)
        self.pending_images[token] = {"path": None, "saving": True, "card": card, "kind": "image", "owned": True}
        self.attachment_layout.insertWidget(self.attachment_layout.count() - 1, card)
        self.attachment_panel.show(); self._resize_attachment_strip()
        worker = ClipboardImageSaveWorker(token, image, path)
        worker.saved.connect(self._pasted_image_saved)
        worker.failed.connect(self._pasted_image_failed)
        worker.finished.connect(lambda token=token: self.image_save_workers.pop(token, None))
        self.image_save_workers[token] = worker
        worker.start()
        self.set_status(f"已预览 {len(self.pending_images)} 张图片，正在后台准备原图…")

    def _pasted_image_saved(self, token, path, width, height):
        item = self.pending_images.get(token)
        if item is None:
            try: Path(path).unlink(missing_ok=True)
            except OSError: pass
            return
        item["path"] = path; item["saving"] = False; item["card"].mark_ready(width, height)
        self.set_status(f"已附加 {len(self.pending_images)} 张图片，可继续粘贴或发送")
        if self.pending_send_after_images and not any(entry["saving"] for entry in self.pending_images.values()):
            self.pending_send_after_images = False
            QTimer.singleShot(0, self.send)

    def _pasted_image_failed(self, token, error):
        if token not in self.pending_images: return
        self.remove_pending_image(token)
        self.bridge.error.emit(f"无法保存剪贴板截图：{error}")
        if self.pending_send_after_images and not any(entry["saving"] for entry in self.pending_images.values()):
            self.pending_send_after_images = False
            QTimer.singleShot(0, self.send)

    def _resize_attachment_strip(self):
        self.attachment_strip.setMinimumWidth(max(self.attachment_scroll.viewport().width(), len(self.pending_images) * 135))

    def remove_pending_image(self, token):
        item = self.pending_images.pop(token, None)
        if item is None: return
        card = item["card"]; self.attachment_layout.removeWidget(card); card.deleteLater()
        if item.get("path") and item.get("owned"):
            try: Path(item["path"]).unlink(missing_ok=True)
            except OSError: pass
        self._resize_attachment_strip()
        if not self.pending_images:
            self.pending_send_after_images = False
            self.attachment_panel.hide()

    def _take_pending_attachments(self):
        attachments = [
            {"path": item["path"], "kind": item.get("kind", "image"), "owned": bool(item.get("owned"))}
            for item in self.pending_images.values() if item.get("path")
        ]
        for item in list(self.pending_images.values()):
            card = item["card"]; self.attachment_layout.removeWidget(card); card.deleteLater()
        self.pending_images.clear(); self.pending_send_after_images = False
        self.attachment_panel.hide(); self._resize_attachment_strip()
        return attachments

    def remove_pending_attachment(self):
        for token in list(self.pending_images):
            self.remove_pending_image(token)

    def stop_task(self):
        if self.worker and self.worker.isRunning(): self.task_cancelled=True; self.worker.cancel_task(); self.set_status("正在停止…")

    def finish_task(self):
        if self.progress_card:self.progress_card.finish(self.task_cancelled)
        self.send_btn.setEnabled(True); self.stop_btn.setEnabled(False); self.set_status("就绪"); self.input.setFocus()
        # bridge.finished is emitted from ChatWorker.run() just before run()
        # returns. Dropping the last Python reference here can destroy a QThread
        # that Qt still considers running and abort the whole application.
        if self.worker is None or not self.worker.isRunning():
            self._worker_thread_finished(self.worker)

    def _worker_thread_finished(self, worker):
        """Release a worker only after QThread.finished confirms it has stopped."""
        if worker is not None and self.worker is not worker:
            return
        if worker is not None:
            worker.deleteLater()
        self.worker = None
        if self.input_queue and not self.agent.restart_requested:
            next_item = self.input_queue.popleft()
            if isinstance(next_item, tuple):
                next_text, next_image = next_item
                QTimer.singleShot(0, lambda: self._start_task(next_text, next_image))
            else:
                QTimer.singleShot(0, lambda: self._start_task(next_item))

    def resume_interrupted_task(self):
        prompt = self.agent.recover_interrupted_task()
        if not prompt or (self.worker and self.worker.isRunning()):
            return
        self.append_message("assistant", self.agent.character_name, "检测到重启前未完成的任务，正在继续执行。")
        self.input.setPlainText(prompt)
        self.send()

    def _restart_if_requested(self):
        if self.agent.restart_requested:
            self.set_status("正在重启 Home Agent…")
            QTimer.singleShot(800, self.restart_for_upgrade)

    def restart_for_upgrade(self):
        self.agent.self_upgrade.launch_restart_watchdog(os.getpid())
        self.force_quit = True
        QApplication.quit()

    def confirm_action(self, description):
        request = {"event": threading.Event(), "ok": False}
        self.bridge.confirm.emit(str(description), request); request["event"].wait(); return bool(request["ok"])

    def show_confirmation(self, description, request):
        request["ok"] = QMessageBox.question(self, "允许电脑操作？", f"AI 请求执行：\n\n{description}", QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes
        request["event"].set()

    def toggle_record(self): self.stop_record() if self.recording else self.start_record()
    def start_record(self):
        try:
            cfg = self.agent.config.get("microphone", {}); self.frames = []; self.stream = sd.InputStream(device=cfg.get("device_id"), samplerate=cfg.get("sample_rate", 16000), channels=cfg.get("channels", 1), dtype="int16", callback=lambda data, *_: self.frames.append(data.copy())); self.stream.start(); self.recording = True; self.voice_btn.setText("停止并识别"); self.set_status("正在录音…")
        except Exception as exc: QMessageBox.critical(self, "录音启动失败", str(exc))

    def stop_record(self):
        if self.stream: self.stream.stop(); self.stream.close(); self.stream = None
        self.recording = False; self.voice_btn.setText("语音")
        if not self.frames: self.set_status("没有录到声音"); return
        data = np.concatenate(self.frames, axis=0); cfg = self.agent.config.get("microphone", {}); folder = HOME_AGENT / "recordings"; folder.mkdir(parents=True, exist_ok=True); path = folder / f"voice_{datetime.now():%Y%m%d_%H%M%S}.wav"
        with wave.open(str(path), "wb") as handle: handle.setnchannels(cfg.get("channels", 1)); handle.setsampwidth(2); handle.setframerate(cfg.get("sample_rate", 16000)); handle.writeframes(data.tobytes())
        self.set_status("正在识别语音…")
        def work():
            try: self.bridge.transcription.emit(asyncio.run(self.agent.transcribe(path)))
            except Exception as exc: self.bridge.error.emit(str(exc)); self.bridge.status.emit("识别失败")
        threading.Thread(target=work, daemon=True).start()

    def accept_transcription(self, text): self.input.setPlainText(text); self.send()
    def open_settings(self): SettingsDialog(self).exec()
    def open_inspector(self): InspectorDialog(self).exec()
    def agent_log_files(self):
        files = []
        for folder in (HOME_AGENT / "logs", ROOT / "logs"):
            if folder.exists(): files.extend(x for x in folder.iterdir() if x.is_file() and x.suffix.lower() in {".log", ".jsonl", ".txt"})
        return sorted(files, key=lambda x: x.stat().st_mtime_ns, reverse=True)
    def apply_always_on_top(self):
        enabled = bool(self.agent.config.get("desktop_pet", {}).get("always_on_top", True)); was_visible = self.isVisible(); self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        if was_visible: self.show()
    def apply_screen_care_settings(self):
        cfg = self.agent.config.get("screen_care", {})
        if cfg.get("enabled", True):
            self.screen_care_timer.start(max(60, int(cfg.get("interval_seconds", 300))) * 1000)
        else:
            self.screen_care_timer.stop()
    def poll_tasks(self):
        threading.Thread(
            target=lambda: asyncio.run(self.agent.run_due_tasks(self.bridge.reminder.emit)),
            daemon=True,
            name="scheduled-task-poller",
        ).start()
    def _show_reminder(self, message):
        self.append_message("assistant", self.agent.character_name, message)
        if self.pet is not None:
            self.pet.show_care_message(message)
        self.set_status("提醒已送达，语音播放中…")
    def run_screen_care(self):
        cfg = self.agent.config.get("screen_care", {})
        if not cfg.get("enabled", True): return
        if cfg.get("skip_while_busy", True) and self.worker and self.worker.isRunning():
            self.agent.log_event("proactive_screen_care_skipped", reason="user_task_running"); return
        if self.screen_care_worker and self.screen_care_worker.isRunning():
            self.agent.log_event("proactive_screen_care_skipped", reason="previous_run_active"); return
        self.screen_care_worker = ScreenCareWorker(self.agent)
        self.screen_care_worker.cared.connect(self._show_screen_care)
        self.screen_care_worker.failed.connect(lambda error: self.agent.log_event("proactive_screen_care_worker_failed", error=error))
        self.screen_care_worker.start()
    def _show_screen_care(self, message):
        if self.agent.config.get("screen_care", {}).get("show_message", True):
            self.append_message("assistant", self.agent.character_name, message)
        if self.pet is not None and self.agent.config.get("screen_care", {}).get("popup_enabled", True):
            self.pet.show_care_message(message)
        self.set_status("就绪")
    @staticmethod
    def resize_edges_for_position(x, y, width, height, margin=9):
        edges = Qt.Edges()
        if x <= margin: edges |= Qt.LeftEdge
        elif x >= width - margin: edges |= Qt.RightEdge
        if y <= margin: edges |= Qt.TopEdge
        elif y >= height - margin: edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def resized_geometry(start: QRect, delta: QPoint, edges, minimum_width, minimum_height):
        result = QRect(start)
        if edges & Qt.LeftEdge:
            left = min(start.right() - minimum_width + 1, start.left() + delta.x()); result.setLeft(left)
        if edges & Qt.RightEdge:
            result.setRight(max(start.left() + minimum_width - 1, start.right() + delta.x()))
        if edges & Qt.TopEdge:
            top = min(start.bottom() - minimum_height + 1, start.top() + delta.y()); result.setTop(top)
        if edges & Qt.BottomEdge:
            result.setBottom(max(start.top() + minimum_height - 1, start.bottom() + delta.y()))
        return result

    @staticmethod
    def cursor_for_resize_edges(edges):
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge): return Qt.SizeFDiagCursor
        if edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge): return Qt.SizeBDiagCursor
        if edges & (Qt.LeftEdge | Qt.RightEdge): return Qt.SizeHorCursor
        if edges & (Qt.TopEdge | Qt.BottomEdge): return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def _create_resize_handles(self):
        definitions = (
            ("top", Qt.TopEdge), ("bottom", Qt.BottomEdge),
            ("left", Qt.LeftEdge), ("right", Qt.RightEdge),
            ("top_left", Qt.TopEdge | Qt.LeftEdge),
            ("top_right", Qt.TopEdge | Qt.RightEdge),
            ("bottom_left", Qt.BottomEdge | Qt.LeftEdge),
            ("bottom_right", Qt.BottomEdge | Qt.RightEdge),
        )
        self.resize_handles = {
            name: FramelessResizeHandle(self, edges, self.cursor_for_resize_edges(edges))
            for name, edges in definitions
        }
        self._layout_resize_handles()

    def _layout_resize_handles(self):
        if not hasattr(self, "resize_handles"): return
        width, height, margin = self.width(), self.height(), self.resize_margin
        corner = margin * 2
        geometries = {
            "top": QRect(corner, 0, max(0, width - corner * 2), margin),
            "bottom": QRect(corner, height - margin, max(0, width - corner * 2), margin),
            "left": QRect(0, corner, margin, max(0, height - corner * 2)),
            "right": QRect(width - margin, corner, margin, max(0, height - corner * 2)),
            "top_left": QRect(0, 0, corner, corner),
            "top_right": QRect(width - corner, 0, corner, corner),
            "bottom_left": QRect(0, height - corner, corner, corner),
            "bottom_right": QRect(width - corner, height - corner, corner, corner),
        }
        for name, handle in self.resize_handles.items():
            handle.setGeometry(geometries[name]); handle.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_resize_handles()

    def _finish_resize(self):
        self.resize_edges = Qt.Edges(); self.resize_start_global = None; self.resize_start_geometry = None; self.native_resizing = False; self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton: return super().mousePressEvent(event)
        point = event.position().toPoint()
        edges = self.resize_edges_for_position(point.x(), point.y(), self.width(), self.height(), self.resize_margin)
        if edges:
            self.resize_edges = edges; self.resize_start_global = event.globalPosition().toPoint(); self.resize_start_geometry = self.geometry(); self.native_resizing = False
            handle = self.windowHandle()
            if handle is not None:
                try: self.native_resizing = bool(handle.startSystemResize(edges))
                except (AttributeError, RuntimeError): self.native_resizing = False
            event.accept(); return
        if point.y() <= 56:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft(); event.accept(); return
        super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if self.resize_edges and event.buttons() & Qt.LeftButton:
            if not self.native_resizing and self.resize_start_global is not None and self.resize_start_geometry is not None:
                delta = event.globalPosition().toPoint() - self.resize_start_global
                self.setGeometry(self.resized_geometry(self.resize_start_geometry, delta, self.resize_edges, self.minimumWidth(), self.minimumHeight()))
            event.accept(); return
        if self.drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_pos); event.accept(); return
        point = event.position().toPoint(); edges = self.resize_edges_for_position(point.x(), point.y(), self.width(), self.height(), self.resize_margin)
        self.setCursor(QCursor(self.cursor_for_resize_edges(edges)))
        super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        self.drag_pos = None; self._finish_resize(); super().mouseReleaseEvent(event)
    def leaveEvent(self, event):
        if not self.resize_edges: self.unsetCursor()
        super().leaveEvent(event)
    def _start_wake_listener(self):
        """Start the wake word listener if enabled."""
        if self.agent.is_prompt_wake_enabled() and not self.wake_listener:
            self.wake_listener = WakeWordListener(self.agent, self)
            self.wake_listener.wake_detected.connect(self._on_wake_detected)
            self.wake_listener.start()
            self.agent.log_event("wake_listener_started")
            
    def _stop_wake_listener(self):
        """Stop the wake word listener."""
        if self.wake_listener:
            self.wake_listener.stop()
            self.wake_listener = None
            self.agent.log_event("wake_listener_stopped")
            
    def _on_wake_detected(self, command):
        """Handle wake word detection."""
        self.append_message("user", self.agent.config.get("home", {}).get("user_name", "主人"), f"[唤醒] {command}")
        self.set_status("唤醒词已检测到，正在处理…")
        self._start_task(command)
        
    def apply_wake_listener_settings(self):
        """Apply wake listener settings changes."""
        if self.agent.is_prompt_wake_enabled():
            self._start_wake_listener()
        else:
            self._stop_wake_listener()

    def closeEvent(self, event):
        if self.pet is not None and not self.force_quit:
            self.hide(); event.ignore(); return
        if self.stream:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
        self.remove_pending_attachment()
        for save_worker in list(getattr(self, "image_save_workers", {}).values()):
            if save_worker.isRunning(): save_worker.wait(2000)
            try: save_worker.path.unlink(missing_ok=True)
            except OSError: pass
        for queued in self.input_queue:
            if isinstance(queued, tuple) and len(queued) > 1 and queued[1]:
                for attachment in queued[1]:
                    if not attachment.get("owned"): continue
                    try: Path(attachment["path"]).unlink(missing_ok=True)
                    except OSError: pass
        event.accept()


class CareMessagePopup(QFrame):
    """Non-activating speech bubble shown next to the desktop pet."""
    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setObjectName("carePopup")
        layout = QVBoxLayout(self); layout.setContentsMargins(16, 12, 16, 12)
        self.message = QLabel(); self.message.setObjectName("carePopupText"); self.message.setWordWrap(True); self.message.setFixedWidth(280)
        layout.addWidget(self.message)
        self.hide_timer = QTimer(self); self.hide_timer.setSingleShot(True); self.hide_timer.timeout.connect(self.hide)

    def show_message(self, text: str, anchor: QWidget, duration_seconds: int = 12):
        self.message.setText(str(text)); self.adjustSize()
        screen = anchor.screen().availableGeometry(); gap = 8
        x = anchor.x() - self.width() - gap
        if x < screen.left(): x = anchor.x() + anchor.width() + gap
        y = anchor.y() + max(0, (anchor.height() - self.height()) // 2)
        x = max(screen.left() + gap, min(x, screen.right() - self.width() - gap))
        y = max(screen.top() + gap, min(y, screen.bottom() - self.height() - gap))
        self.move(x, y); self.show(); self.raise_()
        self.hide_timer.start(max(3, int(duration_seconds)) * 1000)


class DesktopPetWindow(QWidget):
    """Transparent always-on-top launcher that restores the original desktop-pet workflow."""
    def __init__(self, chat: HomeAgentWindow):
        super().__init__(); self.chat = chat; self.drag_origin = None; self.window_origin = None; self.moved = False
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground); self.setFixedSize(170, 170); self.setToolTip("左键打开对话 · 拖动移动 · 右键菜单")
        layout = QVBoxLayout(self); layout.setContentsMargins(8, 8, 8, 8)
        self.image = QLabel(); self.image.setAlignment(Qt.AlignCenter); self.image.setObjectName("petImage"); layout.addWidget(self.image)
        image_setting = chat.agent.config.get("desktop_pet", {}).get("image_path", "")
        image_path = ROOT / image_setting if image_setting else ROOT / "workspace" / "character_images" / "桌宠图标.png"
        if image_path.exists(): self.image.setPixmap(QPixmap(str(image_path)).scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else: self.image.setText("◉‿◉"); self.image.setStyleSheet("font-size:42px;color:#16766F;background:#E7F4F1;border-radius:70px;")
        self.menu = QMenu(self)
        self.menu.addAction("打开对话", self.toggle_chat); self.menu.addAction("停止当前任务", chat.stop_task); self.menu.addSeparator(); self.menu.addAction("日志与上下文", chat.open_inspector); self.menu.addAction("设置", chat.open_settings); self.menu.addSeparator(); self.menu.addAction("退出 Home Agent", self.quit_agent)
        self.care_popup = CareMessagePopup()
        self.restore_position()

    def show_care_message(self, message: str):
        duration = int(self.chat.agent.config.get("screen_care", {}).get("popup_duration_seconds", 12))
        self.care_popup.show_message(message, self, duration)

    def restore_position(self):
        cfg = self.chat.agent.config.get("desktop_pet", {}); screen = QApplication.primaryScreen().availableGeometry()
        x = int(cfg.get("x", screen.right() - self.width() - 20)); y = int(cfg.get("y", screen.bottom() - self.height() - 20))
        self.move(max(screen.left(), min(x, screen.right() - self.width())), max(screen.top(), min(y, screen.bottom() - self.height())))

    def toggle_chat(self):
        if self.chat.isVisible(): self.chat.hide(); return
        self.chat.agent.refresh_identity(); self.chat.name_label.setText(self.chat.agent.character_name)
        screen = self.screen().availableGeometry(); x = self.x() - self.chat.width() - 12
        if x < screen.left(): x = self.x() + self.width() + 12
        y = max(screen.top() + 10, min(self.y() + self.height() - self.chat.height(), screen.bottom() - self.chat.height()))
        self.chat.move(x, y); self.chat.show(); self.chat.raise_(); self.chat.activateWindow(); self.chat.input.setFocus()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_origin = event.globalPosition().toPoint(); self.window_origin = self.pos(); self.moved = False; event.accept()
        elif event.button() == Qt.RightButton: self.menu.popup(event.globalPosition().toPoint()); event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_origin is not None and event.buttons() & Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self.drag_origin
            if delta.manhattanLength() > 5: self.moved = True
            self.move(self.window_origin + delta); event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self.moved: self.toggle_chat()
            self.drag_origin = None; self.save_position(); event.accept()

    def save_position(self):
        path = HOME_AGENT / "config.yaml"
        try:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}; pet = cfg.setdefault("desktop_pet", {}); pet["x"] = self.x(); pet["y"] = self.y()
            temp = path.with_suffix(".yaml.tmp"); temp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"); temp.replace(path); self.chat.agent.config = cfg
        except OSError: pass

    def quit_agent(self):
        self.care_popup.close(); self.save_position(); self.chat.force_quit = True; self.chat.close(); QApplication.quit()


STYLE = f"""
* {{ font-family: 'Noto Sans SC'; color: {COLORS['ink']}; font-size: 14px; }}
QMainWindow {{ background: transparent; }}
#shell {{ background: {COLORS['window']}; border: 1px solid {COLORS['line']}; border-radius: 22px; }}
#header {{ background: {COLORS['panel']}; border-top-left-radius: 22px; border-top-right-radius: 22px; border-bottom: 1px solid {COLORS['line']}; }}
#brand {{ font-size: 17px; font-weight: 700; }} #headerStatus, #muted {{ color: {COLORS['muted']}; font-size: 12px; }}
#titleButton, #closeButton {{ border: 0; border-radius: 10px; background: transparent; font-size: 19px; }} #titleButton:hover {{ background: {COLORS['soft']}; }} #closeButton:hover {{ background: #FBE9E8; color: {COLORS['danger']}; }}
QScrollArea {{ background: {COLORS['window']}; }} QScrollArea > QWidget > QWidget {{ background: {COLORS['window']}; }}
#bubbleAgent {{ background: {COLORS['panel']}; border: 1px solid {COLORS['line']}; border-radius: 15px; }} #bubbleUser {{ background: {COLORS['accent']}; border-radius: 15px; }} #bubbleError {{ background: #FFF0EF; border: 1px solid #F5C9C7; border-radius: 15px; }}
#bubbleUser QLabel {{ color: white; }} #bubbleName {{ font-size: 11px; font-weight: 700; color: {COLORS['muted']}; }} #bubbleText {{ font-size: 14px; }}
#carePopup {{ background: white; border: 1px solid {COLORS['line']}; border-radius: 16px; }} #carePopupText {{ color: {COLORS['ink']}; font-size: 14px; }}
#progressCard {{ background: #F7F9F9; border: 1px solid #DFE7E7; border-radius: 12px; margin: 4px 10px; }} #progressCard[finished="true"] {{ background: #FAFBFB; border-color: #E4EAEA; }} #progressToggle {{ min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px; padding: 0; border: 0; border-radius: 6px; background: transparent; color: #526568; font-size: 19px; font-weight: 500; }} #progressToggle:hover {{ background: #E7EFEE; color: #16766F; }} #progressTitle {{ color: #263638; font-size: 13px; font-weight: 700; }} #progressSummary {{ color: #657578; font-size: 12px; }} #progressElapsed {{ color: #809093; font-size: 11px; }} #progressDetails {{ border-top: 1px solid #E4EAEA; }} #progressReasoning {{ color: #355E5A; font-size: 12px; background: #EDF6F4; border-radius: 7px; padding: 6px; }} #progressPlan {{ color: #34484B; font-size: 12px; }} #progressCurrent {{ color: #1E6761; font-size: 12px; font-weight: 600; }} #progressActivity {{ color: #42575A; font-size: 12px; font-family: Consolas, "Microsoft YaHei UI"; }} #progressDone {{ color: #657578; font-size: 12px; }}
#composer {{ background: {COLORS['panel']}; border-top: 1px solid {COLORS['line']}; border-bottom-left-radius: 22px; border-bottom-right-radius: 22px; }}
#input, QLineEdit, QComboBox, QTextBrowser {{ background: white; border: 1px solid {COLORS['line']}; border-radius: 12px; padding: 10px; selection-background-color: {COLORS['accent']}; }} #input:focus, QLineEdit:focus {{ border: 1px solid {COLORS['accent']}; }}
#attachmentPanel {{ background: #F7FBFA; border: 1px solid {COLORS['line']}; border-radius: 10px; }}
#attachmentCard {{ background: white; border: 1px solid {COLORS['line']}; border-radius: 8px; }}
#attachmentPreview {{ background: white; border: 0; border-radius: 7px; }}
#attachmentLabel {{ color: {COLORS['accent']}; font-size: 12px; }}
#attachmentClose {{ background: rgba(30, 45, 45, 190); color: white; border: 0; border-radius: 11px; font-size: 16px; padding: 0; }}
#attachmentClose:hover {{ background: {COLORS['danger']}; }}
#addAttachment {{ background: {COLORS['soft']}; color: {COLORS['accent']}; border: 1px solid {COLORS['line']}; border-radius: 12px; font-size: 25px; padding: 0; }}
#addAttachment:hover {{ background: #D7ECE8; }}
QPushButton {{ min-height: 34px; padding: 0 15px; border-radius: 10px; font-weight: 600; }} #primaryButton {{ background: {COLORS['accent']}; color: white; border: 0; }} #primaryButton:hover {{ background: {COLORS['accent_hover']}; }} #softButton {{ background: {COLORS['soft']}; color: {COLORS['accent']}; border: 0; }} #stopButton {{ background: white; color: {COLORS['danger']}; border: 1px solid #EBC1BF; }} #stopButton:disabled {{ color: #AAB4B5; border-color: {COLORS['line']}; }}
QDialog {{ background: {COLORS['window']}; }} #dialogTitle {{ font-size: 22px; font-weight: 700; }} QTabWidget::pane {{ border: 1px solid {COLORS['line']}; border-radius: 12px; background: white; }} QTabBar::tab {{ padding: 9px 18px; }} QTabBar::tab:selected {{ color: {COLORS['accent']}; font-weight: 700; }}
"""


def run():
    app = QApplication.instance() or QApplication([])
    lock_path = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation)) / "ai-home-agent.lock"
    lock = InstanceLock(lock_path)
    if not lock.acquire():
        return 0
    app._home_agent_lock = lock
    font_path = Path(r"C:\Windows\Fonts\NotoSansSC.ttf")
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families: app.setFont(QFont(families[0], 10))
    app.setStyle("Fusion"); app.setStyleSheet(STYLE)
    window = HomeAgentWindow(); pet = DesktopPetWindow(window); window.pet = pet; pet.show()
    startup_cfg = window.agent.config.get("system_startup", {})
    if AUTOSTART_ARGUMENT in sys.argv:
        threading.Thread(
            target=run_network_guard,
            args=(startup_cfg, HOME_AGENT / "state" / "network-startup.json"),
            kwargs={"is_autostart": True},
            daemon=True,
            name="network-startup-guard",
        ).start()
    return app.exec()
