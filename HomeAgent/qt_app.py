from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import yaml
from PySide6.QtCore import QLockFile, QObject, QPoint, QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSpinBox, QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from agent import HOME_AGENT, ROOT, HomeAgent


COLORS = {
    "window": "#F5F7F8", "panel": "#FFFFFF", "ink": "#172326",
    "muted": "#718083", "accent": "#16766F", "accent_hover": "#115F59",
    "soft": "#E7F4F1", "line": "#DFE7E7", "danger": "#C54D48",
}


class Bridge(QObject):
    answer = Signal(str)
    error = Signal(str)
    status = Signal(str)
    finished = Signal()
    transcription = Signal(str)
    confirm = Signal(str, object)
    progress = Signal(object)


class ChatWorker(QThread):
    def __init__(self, agent: HomeAgent, prompt: str, bridge: Bridge, confirm):
        super().__init__(); self.agent = agent; self.prompt = prompt; self.bridge = bridge; self.confirm = confirm
        self.loop = None; self.task = None; self.clock = None; self.report_tasks = set(); self.started_at = 0.0; self.current_step = ""; self.completed_steps = []; self.last_report_at = 0.0; self.report_count = 0

    async def progress_clock(self):
        while True:
            await asyncio.sleep(5)
            self.report_status(self.current_step or "正在处理任务…")

    @staticmethod
    async def drain_tasks(tasks):
        await asyncio.gather(*tasks, return_exceptions=True)

    def report_status(self, text):
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
        snapshot = {"current": self.current_step, "completed": list(self.completed_steps), "elapsed": elapsed, "state": "running"}
        self.bridge.status.emit(value); self.bridge.progress.emit(snapshot)
        cfg = self.agent.config.get("progress_reporting", {})
        threshold = max(15, int(cfg.get("long_task_seconds", 60))); cooldown = max(30, int(cfg.get("tts_cooldown_seconds", 90))); limit = max(0, int(cfg.get("max_reports_per_task", 3)))
        reportable = not any(word in value for word in ("语音", "播放", "录音", "识别"))
        if cfg.get("enabled", True) and reportable and elapsed >= threshold and self.report_count < limit and time.monotonic() - self.last_report_at >= cooldown and self.loop:
            self.last_report_at = time.monotonic(); self.report_count += 1
            report = self.loop.create_task(self.agent.speak_progress_report(self.prompt, list(self.completed_steps), self.current_step, elapsed)); self.report_tasks.add(report); report.add_done_callback(self.report_tasks.discard)

    def run(self):
        self.loop = asyncio.new_event_loop(); self.started_at = time.monotonic()
        try:
            asyncio.set_event_loop(self.loop)
            self.task = self.loop.create_task(self.agent.chat(self.prompt, self.report_status, self.confirm))
            self.clock = self.loop.create_task(self.progress_clock())
            self.bridge.answer.emit(self.loop.run_until_complete(self.task))
        except asyncio.CancelledError:
            self.bridge.answer.emit("当前任务已停止。")
        except Exception as exc:
            self.bridge.error.emit(str(exc))
        finally:
            if self.clock:
                self.clock.cancel(); self.loop.run_until_complete(self.drain_tasks([self.clock]))
            if self.report_tasks:
                for report in self.report_tasks: report.cancel()
                self.loop.run_until_complete(self.drain_tasks(list(self.report_tasks))); self.report_tasks.clear()
            self.task = None; self.clock = None; self.loop.close(); self.bridge.finished.emit()

    def cancel_task(self):
        self.agent.stop_current_task()
        if self.loop and self.task and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)


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
        super().__init__(); self.setObjectName("progressCard"); layout=QVBoxLayout(self); layout.setContentsMargins(14,11,14,12); layout.setSpacing(5)
        top=QHBoxLayout(); self.title=QLabel("任务进行中"); self.title.setObjectName("progressTitle"); self.elapsed=QLabel("0 秒"); self.elapsed.setObjectName("muted"); top.addWidget(self.title); top.addStretch(); top.addWidget(self.elapsed); layout.addLayout(top)
        self.current=QLabel("正在分析任务…"); self.current.setWordWrap(True); self.current.setObjectName("progressCurrent"); layout.addWidget(self.current)
        self.done=QLabel("已完成：等待第一个阶段"); self.done.setWordWrap(True); self.done.setObjectName("progressDone"); layout.addWidget(self.done); self.started=time.monotonic(); self.timer=QTimer(self); self.timer.timeout.connect(lambda:self.elapsed.setText(f"{int(time.monotonic()-self.started)} 秒")); self.timer.start(1000)
    def update_progress(self, data):
        self.elapsed.setText(f"{int(data.get('elapsed',0))} 秒"); self.current.setText("当前："+str(data.get("current") or "正在处理…")); completed=data.get("completed") or []; self.done.setText("已完成："+("  ·  ".join(map(str,completed[-5:])) if completed else "暂无"))
    def finish(self, cancelled=False):
        self.timer.stop(); self.title.setText("任务已停止" if cancelled else "任务已完成"); self.current.setText("已结束"); self.setProperty("finished",True); self.style().unpolish(self); self.style().polish(self)


class SettingsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner); self.owner = owner; self.agent = owner.agent; self._saving = False
        self.setWindowTitle("Home Agent 设置"); self.resize(680, 650)
        root = QVBoxLayout(self); root.setContentsMargins(22, 20, 22, 20); root.setSpacing(12)
        title = QLabel("设置"); title.setObjectName("dialogTitle"); root.addWidget(title)
        self.status = QLabel("所有修改都会实时保存"); self.status.setObjectName("muted"); root.addWidget(self.status)
        tabs = QTabWidget(); root.addWidget(tabs, 1)
        general = QWidget(); form = QFormLayout(general); form.setContentsMargins(18, 18, 18, 18); form.setSpacing(14)
        cfg = self.agent.config; mic = cfg.get("microphone", {}); control = cfg.get("computer_control", {}); codex = cfg.get("codex_cli", {})
        self.always_top = QCheckBox("窗口始终置顶"); self.always_top.setChecked(bool(cfg.get("desktop_pet", {}).get("always_on_top", True)))
        self.auto_send = QCheckBox("语音识别完成后自动发送"); self.auto_send.setChecked(bool(mic.get("auto_send_after_transcription", True)))
        self.control = QCheckBox("允许 Home Agent 使用电脑工具"); self.control.setChecked(bool(control.get("enabled", True)))
        self.full_access = QCheckBox("完整磁盘访问权限"); self.full_access.setChecked(bool(control.get("full_access", False)))
        self.confirm_file = QCheckBox("打开文件和网页前请求确认"); self.confirm_file.setChecked(bool(control.get("confirm_before_action", True)))
        self.confirm_app = QCheckBox("启动应用前请求确认"); self.confirm_app.setChecked(bool(control.get("confirm_launch_app", False)))
        self.codex = QCheckBox("启用 Codex CLI / MCP"); self.codex.setChecked(bool(codex.get("enabled", False)))
        for label, widget in (("界面", self.always_top), ("语音", self.auto_send), ("电脑控制", self.control), ("权限", self.full_access), ("确认", self.confirm_file), ("确认", self.confirm_app), ("自动化", self.codex)):
            form.addRow(label, widget); widget.toggled.connect(self.save)
        tabs.addTab(general, "常规")

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
        close = QPushButton("完成"); close.setObjectName("primaryButton"); close.clicked.connect(self.accept); root.addWidget(close, 0, Qt.AlignRight)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.save)

    def defer_save(self): self.status.setText("正在保存…"); self.timer.start(450)

    def save(self):
        if self._saving: return
        self._saving = True
        try:
            path = HOME_AGENT / "config.yaml"; cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            cfg.setdefault("desktop_pet", {})["always_on_top"] = self.always_top.isChecked()
            cfg.setdefault("microphone", {})["auto_send_after_transcription"] = self.auto_send.isChecked()
            control = cfg.setdefault("computer_control", {}); control["enabled"] = self.control.isChecked(); control["full_access"] = self.full_access.isChecked(); control["confirm_before_action"] = self.confirm_file.isChecked(); control["confirm_launch_app"] = self.confirm_app.isChecked()
            cfg.setdefault("codex_cli", {})["enabled"] = self.codex.isChecked()
            stt = cfg.setdefault("stt", {}); stt["mode"] = self.stt_mode.currentText(); stt["api_url"] = self.stt_url.text().strip(); stt["model"] = self.stt_model.text().strip(); stt["language"] = self.stt_language.text().strip() or "auto"
            progress=cfg.setdefault("progress_reporting",{});progress["enabled"]=self.progress_enabled.isChecked();progress["long_task_seconds"]=self.progress_seconds.value();progress["tts_cooldown_seconds"]=self.progress_cooldown.value();progress["max_reports_per_task"]=self.progress_reports.value()
            temp = path.with_suffix(".yaml.tmp"); temp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"); temp.replace(path)
            self.agent.config = cfg; self.owner.apply_always_on_top(); self.status.setText(f"已实时保存 · {datetime.now():%H:%M:%S}")
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
        super().__init__(); self.agent = HomeAgent(); self.bridge = Bridge(); self.worker = None; self.recording = False; self.stream = None; self.frames = []; self.drag_pos = None; self.force_quit = False; self.pet = None; self.progress_card = None; self.task_cancelled = False
        self.setWindowTitle(f"{self.agent.character_name} · Home Agent"); self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window); self.setAttribute(Qt.WA_TranslucentBackground); self.resize(860, 380); self.setMinimumSize(640, 300)
        self._build(); self._connect(); self.apply_always_on_top()
        self.scheduler = QTimer(self); self.scheduler.timeout.connect(self.poll_tasks); self.scheduler.start(10000)

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
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True); self.scroll.setFrameShape(QFrame.NoFrame); self.messages = QWidget(); self.message_layout = QVBoxLayout(self.messages); self.message_layout.setContentsMargins(18, 10, 18, 8); self.message_layout.setSpacing(6); self.message_layout.addStretch(); self.scroll.setWidget(self.messages); root.addWidget(self.scroll, 1)
        composer = QFrame(); composer.setObjectName("composer"); c = QHBoxLayout(composer); c.setContentsMargins(14, 10, 14, 12); c.setSpacing(8)
        self.input = QTextEdit(); self.input.setObjectName("input"); self.input.setPlaceholderText("输入任务…  Ctrl + Enter 发送"); self.input.setMaximumHeight(76); self.input.setMinimumHeight(56); c.addWidget(self.input, 1)
        actions = QVBoxLayout(); actions.setSpacing(8); top = QHBoxLayout(); self.voice_btn = QPushButton("语音"); self.voice_btn.setObjectName("softButton"); self.send_btn = QPushButton("发送"); self.send_btn.setObjectName("primaryButton"); top.addWidget(self.voice_btn); top.addWidget(self.send_btn); actions.addLayout(top)
        self.stop_btn = QPushButton("停止当前任务"); self.stop_btn.setObjectName("stopButton"); self.stop_btn.setEnabled(False); actions.addWidget(self.stop_btn); c.addLayout(actions); root.addWidget(composer)

    def _connect(self):
        self.send_btn.clicked.connect(self.send); self.stop_btn.clicked.connect(self.stop_task); self.voice_btn.clicked.connect(self.toggle_record); QShortcut(QKeySequence("Ctrl+Return"), self.input, activated=self.send)
        self.bridge.answer.connect(lambda text: self.append_message("assistant", self.agent.character_name, text)); self.bridge.error.connect(lambda text: self.append_message("error", "错误", text)); self.bridge.status.connect(self.set_status); self.bridge.progress.connect(self.update_task_progress); self.bridge.finished.connect(self.finish_task); self.bridge.transcription.connect(self.accept_transcription); self.bridge.confirm.connect(self.show_confirmation)

    def append_message(self, role, name, text):
        self.message_layout.insertWidget(self.message_layout.count() - 1, MessageBubble(role, name, str(text)))
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def set_status(self, text): self.status.setText(str(text))
    def update_task_progress(self, data):
        if self.progress_card: self.progress_card.update_progress(data)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))
    def send(self):
        text = self.input.toPlainText().strip()
        if not text or (self.worker and self.worker.isRunning()): return
        self.input.clear(); self.append_message("user", self.agent.config.get("home", {}).get("user_name", "你"), text); self.progress_card=TaskProgressCard(); self.message_layout.insertWidget(self.message_layout.count()-1,self.progress_card); self.task_cancelled=False; self.agent.begin_task(); self.send_btn.setEnabled(False); self.stop_btn.setEnabled(True); self.set_status("正在思考…")
        self.worker = ChatWorker(self.agent, text, self.bridge, self.confirm_action); self.worker.start()

    def stop_task(self):
        if self.worker and self.worker.isRunning(): self.task_cancelled=True; self.worker.cancel_task(); self.set_status("正在停止…")

    def finish_task(self):
        if self.progress_card:self.progress_card.finish(self.task_cancelled)
        self.send_btn.setEnabled(True); self.stop_btn.setEnabled(False); self.set_status("就绪"); self.input.setFocus()

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
    def poll_tasks(self): threading.Thread(target=lambda: asyncio.run(self.agent.run_due_tasks()), daemon=True).start()
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() <= 56: self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft(); event.accept()
    def mouseMoveEvent(self, event):
        if self.drag_pos is not None and event.buttons() & Qt.LeftButton: self.move(event.globalPosition().toPoint() - self.drag_pos); event.accept()
    def mouseReleaseEvent(self, event): self.drag_pos = None
    def closeEvent(self, event):
        if self.pet is not None and not self.force_quit:
            self.hide(); event.ignore(); return
        if self.stream:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
        event.accept()


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
        self.restore_position()

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
        self.save_position(); self.chat.force_quit = True; self.chat.close(); QApplication.quit()


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
#progressCard {{ background: #EDF7F4; border: 1px solid #9BCDC2; border-radius: 14px; margin: 4px 10px; }} #progressCard[finished="true"] {{ background: #F4F7F6; border-color: #CCD9D6; }} #progressTitle {{ color: #115F59; font-size: 14px; font-weight: 700; }} #progressCurrent {{ color: #173D37; font-weight: 600; }} #progressDone {{ color: #425C56; font-size: 12px; }}
#composer {{ background: {COLORS['panel']}; border-top: 1px solid {COLORS['line']}; border-bottom-left-radius: 22px; border-bottom-right-radius: 22px; }}
#input, QLineEdit, QComboBox, QTextBrowser {{ background: white; border: 1px solid {COLORS['line']}; border-radius: 12px; padding: 10px; selection-background-color: {COLORS['accent']}; }} #input:focus, QLineEdit:focus {{ border: 1px solid {COLORS['accent']}; }}
QPushButton {{ min-height: 34px; padding: 0 15px; border-radius: 10px; font-weight: 600; }} #primaryButton {{ background: {COLORS['accent']}; color: white; border: 0; }} #primaryButton:hover {{ background: {COLORS['accent_hover']}; }} #softButton {{ background: {COLORS['soft']}; color: {COLORS['accent']}; border: 0; }} #stopButton {{ background: white; color: {COLORS['danger']}; border: 1px solid #EBC1BF; }} #stopButton:disabled {{ color: #AAB4B5; border-color: {COLORS['line']}; }}
QDialog {{ background: {COLORS['window']}; }} #dialogTitle {{ font-size: 22px; font-weight: 700; }} QTabWidget::pane {{ border: 1px solid {COLORS['line']}; border-radius: 12px; background: white; }} QTabBar::tab {{ padding: 9px 18px; }} QTabBar::tab:selected {{ color: {COLORS['accent']}; font-weight: 700; }}
"""


def run():
    app = QApplication.instance() or QApplication([])
    lock_path = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation)) / "ai-home-agent.lock"
    lock = QLockFile(str(lock_path)); lock.setStaleLockTime(30000)
    if not lock.tryLock(100):
        lock.removeStaleLockFile()
        if not lock.tryLock(100):
            return 0
    app._home_agent_lock = lock
    font_path = Path(r"C:\Windows\Fonts\NotoSansSC.ttf")
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families: app.setFont(QFont(families[0], 10))
    app.setStyle("Fusion"); app.setStyleSheet(STYLE)
    window = HomeAgentWindow(); pet = DesktopPetWindow(window); window.pet = pet; pet.show()
    return app.exec()
