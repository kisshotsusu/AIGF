from __future__ import annotations

import asyncio
import json
import os
import threading
import wave
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import sounddevice as sd
import yaml

from agent import HomeAgent, ROOT, HOME_AGENT


class DesktopPet:
    TRANSPARENT = "#ff00ff"

    def __init__(self):
        self.agent = HomeAgent()
        self.recording = False
        self.stream = None
        self.frames = []
        self.busy = False
        self.chat_loop = None
        self.chat_task = None
        self.scheduler_busy = False
        self.closing = False
        self.panel = None
        self.settings_window = None
        self.inspector_window = None
        self.drag_start = None
        self.mic_devices = []

        self.root = tk.Tk()
        self.root.title(f"{self.agent.character_name} · 家庭桌宠")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.agent.config.get("desktop_pet", {}).get("always_on_top", True)))
        self.root.configure(bg=self.TRANSPARENT)
        try:
            self.root.wm_attributes("-transparentcolor", self.TRANSPARENT)
        except tk.TclError:
            pass
        self._place_pet()
        self._build_pet()
        self._build_menu()
        self.refresh_microphones()
        self.root.after(1000, self._poll_scheduled_tasks)

    def _poll_scheduled_tasks(self):
        if self.closing: return
        if not self.scheduler_busy:
            self.scheduler_busy = True
            def worker():
                try:
                    asyncio.run(self.agent.run_due_tasks())
                finally:
                    if not self.closing:
                        try: self.root.after(0, lambda: setattr(self, "scheduler_busy", False))
                        except tk.TclError: pass
            threading.Thread(target=worker, daemon=True, name="home-task-scheduler").start()
        self.root.after(10_000, self._poll_scheduled_tasks)

    def _place_pet(self):
        cfg = self.agent.config.get("desktop_pet", {})
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = int(cfg.get("x", sw - 210)); y = int(cfg.get("y", sh - 280))
        self.root.geometry(f"170x170+{max(0, x)}+{max(0, y)}")

    def _build_pet(self):
        self.canvas = tk.Canvas(self.root, width=170, height=170, bg=self.TRANSPARENT, highlightthickness=0)
        self.canvas.pack()
        # Q版图标是家庭桌宠的内置默认形象，配置字段缺失时也不能退回绿色备用球。
        default_icon = ROOT / "workspace" / "character_images" / "桌宠图标.png"
        image_setting = self.agent.config.get("desktop_pet", {}).get("image_path", "")
        image_path = ROOT / image_setting if image_setting else default_icon
        if image_path and image_path.is_file():
            self.pet_image = tk.PhotoImage(file=str(image_path))
            self.canvas.create_image(85, 85, image=self.pet_image, tags="pet")
        else:
            self.canvas.create_oval(35, 38, 137, 142, fill="#d8fff5", outline="#287568", width=4, tags="pet")
            self.canvas.create_oval(62, 75, 74, 89, fill="#263234", outline="", tags="pet")
            self.canvas.create_oval(99, 75, 111, 89, fill="#263234", outline="", tags="pet")
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Button-3>", self._show_menu)

    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="打开对话", command=self.toggle_panel)
        self.menu.add_command(label="停止当前任务", command=self.stop_current_task)
        self.menu.add_command(label="日志与上下文", command=self.open_inspector)
        self.menu.add_command(label="设置…", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="退出桌宠", command=self.close)

    def _drag_begin(self, event):
        self.drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event):
        if not self.drag_start:
            return
        sx, sy, wx, wy = self.drag_start
        self.root.geometry(f"+{wx + event.x_root - sx}+{wy + event.y_root - sy}")
        self._position_panel()

    def _drag_end(self, event):
        if not self.drag_start:
            return
        sx, sy, _, _ = self.drag_start
        moved = abs(event.x_root - sx) + abs(event.y_root - sy)
        self.drag_start = None
        if moved < 7:
            self.toggle_panel()

    def _show_menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def toggle_panel(self):
        self._sync_character_name()
        if self.panel and self.panel.winfo_exists():
            if self.panel.state() == "withdrawn":
                self.panel.deiconify(); self._position_panel(); self.input.focus_set()
            else:
                self.panel.withdraw()
            return
        self._create_panel()

    def _create_panel(self):
        self.panel = tk.Toplevel(self.root)
        self.panel.title(f"和{self.agent.character_name}聊天")
        self.panel.geometry("720x122")
        self.panel.resizable(True, False)
        self.panel.minsize(520, 122)
        self.panel.configure(bg="#f2efe8")
        self.panel.protocol("WM_DELETE_WINDOW", self.panel.withdraw)
        self._position_panel()

        header = tk.Frame(self.panel, bg="#155e59", height=38)
        header.pack(fill="x"); header.pack_propagate(False)
        self.name_label = tk.Label(header, text=self.agent.character_name, bg="#155e59", fg="white", font=("Microsoft YaHei UI", 10, "bold")); self.name_label.pack(side="left", padx=(12, 7))
        self.chat = tk.Text(header, height=1, wrap="none", state="disabled", bg="#155e59", fg="#eafff9", relief="flat", font=("Microsoft YaHei UI", 9), pady=8)
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_config("user", foreground="#155e59", font=("Microsoft YaHei UI", 10, "bold"))
        self.chat.tag_config("assistant", foreground="#eafff9", font=("Microsoft YaHei UI", 9))
        self.chat.tag_config("error", foreground="#ffd0c8")
        self.status = tk.Label(header, text="就绪", bg="#155e59", fg="#9debd9", font=("Microsoft YaHei UI", 8))
        self.status.pack(side="right", padx=10)

        bottom = tk.Frame(self.panel, bg="#f2efe8")
        bottom.pack(fill="both", expand=True, padx=9, pady=9)
        buttons = tk.Frame(bottom, bg="#f2efe8", width=270)
        buttons.pack(side="right", fill="y", padx=(8, 0))
        buttons.pack_propagate(False)
        self.record_btn = ttk.Button(buttons, text="● 开始语音", command=self.toggle_record, width=12)
        self.record_btn.pack(side="left", fill="y")

        # Keep the two task actions in one stable group. The voice button must
        # not separate or squeeze them when Windows font scaling changes.
        task_actions = tk.Frame(buttons, bg="#f2efe8")
        task_actions.pack(side="right", fill="y")
        self.send_btn = ttk.Button(task_actions, text="发送", command=self.send, width=8)
        self.send_btn.pack(side="left", fill="y")
        self.stop_btn = ttk.Button(task_actions, text="停止", command=self.stop_current_task, width=7, state="disabled")
        self.stop_btn.pack(side="left", fill="y", padx=(2, 0))
        self.input = tk.Text(bottom, height=2, wrap="word", relief="solid", bd=1, font=("Microsoft YaHei UI", 10), padx=6, pady=5)
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Control-Return>", lambda _: self.send())
        self._append("assistant", self.agent.character_name, "我在这里。可以输入文字，或点击“开始语音”。")
        self.input.focus_set()

    def _position_panel(self):
        if not self.panel or not self.panel.winfo_exists():
            return
        self.panel.update_idletasks()
        width, height = self.panel.winfo_width(), self.panel.winfo_height()
        if width <= 1: width, height = 720, 122
        px, py = self.root.winfo_x(), self.root.winfo_y()
        x = px - width - 8
        if x < 0: x = px + 178
        y = max(10, min(py - height + 165, self.root.winfo_screenheight() - height - 50))
        self.panel.geometry(f"{width}x{height}+{x}+{y}")

    def _append(self, tag, name, text):
        if not self.panel or not self.panel.winfo_exists():
            self.toggle_panel()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.insert("end", f"{name}：{text}", tag)
        self.chat.configure(state="disabled"); self.chat.see("end")

    def set_status(self, text):
        if self.panel and self.panel.winfo_exists():
            self.root.after(0, lambda: self.status.configure(text=text))

    def send(self):
        self._sync_character_name()
        if self.busy or not self.panel:
            return
        text = self.input.get("1.0", "end").strip()
        if not text:
            return
        self.agent.begin_task()
        self.busy = True; self.send_btn.configure(state="disabled"); self.stop_btn.configure(state="normal")
        self.input.delete("1.0", "end")
        self._append("user", self.agent.config["home"].get("user_name", "你"), text)
        self.set_status("正在思考…")
        threading.Thread(target=self._chat_worker, args=(text,), daemon=True).start()

    def stop_current_task(self):
        if not self.busy:
            self.set_status("当前没有运行中的任务")
            return
        self.agent.stop_current_task()
        loop, task = self.chat_loop, self.chat_task
        if loop and task and not task.done():
            loop.call_soon_threadsafe(task.cancel)
        self.set_status("正在停止…")

    def _chat_worker(self, text):
        loop = asyncio.new_event_loop()
        self.chat_loop = loop
        try:
            asyncio.set_event_loop(loop)
            self.chat_task = loop.create_task(self.agent.chat(text, self.set_status, self.confirm_computer_action))
            answer = loop.run_until_complete(self.chat_task)
            self.root.after(0, lambda: self._append("assistant", self.agent.character_name, answer))
            self.set_status("就绪")
        except asyncio.CancelledError:
            self.agent.log_event("chat_cancelled")
            self.root.after(0, lambda: self._append("assistant", self.agent.character_name, "当前任务已停止。"))
            self.set_status("已停止")
        except Exception as exc:
            self.agent.log_event("chat_error", error=str(exc))
            self.root.after(0, lambda e=str(exc): self._append("error", "错误", e))
            self.set_status("发生错误")
        finally:
            self.chat_task = None
            self.chat_loop = None
            loop.close()
            self.root.after(0, self._finish_chat)

    def _finish_chat(self):
        self.busy = False
        if self.panel and self.panel.winfo_exists():
            self.send_btn.configure(state="normal"); self.stop_btn.configure(state="disabled"); self.input.focus_set()

    def _sync_character_name(self):
        name = self.agent.refresh_identity()
        self.root.title(f"{name} · 家庭桌宠")
        if self.panel and self.panel.winfo_exists():
            self.panel.title(f"和{name}聊天")
            if hasattr(self, "name_label"): self.name_label.configure(text=name)

    def confirm_computer_action(self, description):
        """从 Agent 后台线程安全地请求主界面确认。"""
        event = threading.Event(); result = {"allowed": False}
        def ask():
            result["allowed"] = messagebox.askyesno("允许电脑操作？", f"AI 请求执行以下操作：\n\n{description}\n\n是否允许？", parent=self.root)
            event.set()
        self.root.after(0, ask); event.wait()
        return result["allowed"]

    def refresh_microphones(self):
        try:
            devices = sd.query_devices()
            self.mic_devices = [(i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0]
        except Exception:
            self.mic_devices = []

    def selected_device(self):
        wanted = self.agent.config.get("microphone", {}).get("device_id")
        if wanted is not None and any(i == int(wanted) for i, _ in self.mic_devices):
            return int(wanted)
        return self.mic_devices[0][0] if self.mic_devices else None

    def toggle_record(self):
        if self.recording: self.stop_record()
        else: self.start_record()

    def start_record(self):
        device_id = self.selected_device()
        if device_id is None:
            return messagebox.showerror("没有麦克风", "未检测到可用的语音输入设备。")
        cfg = self.agent.config["microphone"]
        try:
            self.frames = []
            self.stream = sd.InputStream(device=device_id, samplerate=cfg.get("sample_rate", 16000), channels=cfg.get("channels", 1), dtype="int16", callback=lambda data, *_: self.frames.append(data.copy()))
            self.stream.start(); self.recording = True
            self.record_btn.configure(text="■ 停止并识别"); self.set_status("正在录音…")
        except Exception as exc:
            messagebox.showerror("录音启动失败", str(exc))

    def stop_record(self):
        if self.stream:
            self.stream.stop(); self.stream.close()
        self.recording = False; self.record_btn.configure(text="● 开始语音")
        if not self.frames:
            return self.set_status("没有录到声音")
        data = np.concatenate(self.frames, axis=0)
        path = HOME_AGENT / "recordings" / f"voice_{datetime.now():%Y%m%d_%H%M%S}.wav"
        cfg = self.agent.config["microphone"]
        with wave.open(str(path), "wb") as f:
            f.setnchannels(cfg.get("channels", 1)); f.setsampwidth(2); f.setframerate(cfg.get("sample_rate", 16000)); f.writeframes(data.tobytes())
        self.set_status("正在识别语音…")
        threading.Thread(target=self._stt_worker, args=(path,), daemon=True).start()

    def _stt_worker(self, path):
        try:
            text = asyncio.run(self.agent.transcribe(path))
            self.root.after(0, lambda: self._set_transcription(text)); self.set_status("识别完成")
        except Exception as exc:
            self.root.after(0, lambda e=str(exc): self._append("error", "语音识别", e)); self.set_status("识别失败")

    def _set_transcription(self, text):
        self.input.delete("1.0", "end"); self.input.insert("1.0", text)
        # Voice input is a submit action: once recognition succeeds, send it.
        # Keeping recognized text stranded in the editor made the voice button
        # appear broken when stale settings overwrote the preference flag.
        self.send()

    def _agent_log_files(self):
        files = []
        for folder in (HOME_AGENT / "logs", ROOT / "logs"):
            if folder.exists():
                files.extend(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in {".log", ".jsonl", ".txt"})
        return sorted(dict.fromkeys(files), key=lambda path: path.stat().st_mtime_ns, reverse=True)

    def open_inspector(self):
        if self.inspector_window and self.inspector_window.winfo_exists():
            self.inspector_window.deiconify(); self.inspector_window.lift(); self._refresh_inspector(); return
        win = self.inspector_window = tk.Toplevel(self.root)
        win.title(f"{self.agent.character_name} · Agent 日志与上下文")
        win.geometry("980x720"); win.minsize(760, 520); win.configure(bg="#f2efe8")
        toolbar = ttk.Frame(win, padding=(12, 10)); toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Agent 调试中心", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ttk.Button(toolbar, text="刷新", command=self._refresh_inspector).pack(side="right")
        ttk.Button(toolbar, text="检测 CLI / MCP", command=self._inspect_codex_status).pack(side="right", padx=(0, 8))
        tabs = ttk.Notebook(win); tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        log_tab = ttk.Frame(tabs, padding=10); context_tab = ttk.Frame(tabs, padding=10); tools_tab = ttk.Frame(tabs, padding=10)
        tabs.add(log_tab, text="运行日志"); tabs.add(context_tab, text="模型上下文"); tabs.add(tools_tab, text="工具与状态")

        log_bar = ttk.Frame(log_tab); log_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(log_bar, text="日志文件").pack(side="left", padx=(0, 8))
        self.inspector_log_choice = ttk.Combobox(log_bar, state="readonly", width=78)
        self.inspector_log_choice.pack(side="left", fill="x", expand=True)
        self.inspector_log_choice.bind("<<ComboboxSelected>>", lambda _: self._load_selected_log())
        self.inspector_log_text = tk.Text(log_tab, wrap="none", font=("Consolas", 9), bg="#ffffff", fg="#1f2937", relief="flat", padx=10, pady=10)
        self.inspector_log_text.pack(fill="both", expand=True)
        self.inspector_context_text = tk.Text(context_tab, wrap="word", font=("Microsoft YaHei UI", 9), bg="#ffffff", fg="#1f2937", relief="flat", padx=12, pady=10)
        self.inspector_context_text.pack(fill="both", expand=True)
        self.inspector_tools_text = tk.Text(tools_tab, wrap="word", font=("Consolas", 9), bg="#ffffff", fg="#1f2937", relief="flat", padx=12, pady=10)
        self.inspector_tools_text.pack(fill="both", expand=True)
        self._refresh_inspector()

    @staticmethod
    def _replace_text(widget, content):
        widget.configure(state="normal"); widget.delete("1.0", "end"); widget.insert("1.0", content); widget.configure(state="disabled")

    def _refresh_inspector(self):
        if not self.inspector_window or not self.inspector_window.winfo_exists(): return
        files = self._agent_log_files(); current = self.inspector_log_choice.get()
        values = [str(path) for path in files]; self.inspector_log_choice["values"] = values
        self.inspector_log_choice.set(current if current in values else (values[0] if values else ""))
        self._load_selected_log()
        try: context = self.agent.context_snapshot()
        except Exception as exc: context = f"读取上下文失败：{exc}"
        self._replace_text(self.inspector_context_text, context)
        lines = ["当前可用工具："]
        for item in self.agent._tools():
            function = item.get("function", {})
            lines.append(f"\n\n• {function.get('name')}\n  {function.get('description', '')}")
        lines.append("\n\nCodex CLI / MCP：\n点击右上角“检测 CLI / MCP”读取实时状态。")
        self._replace_text(self.inspector_tools_text, "".join(lines))

    def _load_selected_log(self):
        value = self.inspector_log_choice.get().strip()
        if not value:
            return self._replace_text(self.inspector_log_text, "暂时没有日志。与 Agent 对话后会生成 agent-events.jsonl。")
        path = Path(value)
        try:
            with path.open("rb") as file:
                size = path.stat().st_size; file.seek(max(0, size - 300_000)); content = file.read().decode("utf-8", "replace")
            if size > 300_000: content = "（仅显示日志末尾 300KB）\n" + content
        except OSError as exc: content = f"无法读取日志：{exc}"
        self._replace_text(self.inspector_log_text, content); self.inspector_log_text.see("end")

    def _inspect_codex_status(self):
        self._replace_text(self.inspector_tools_text, "正在检测 Codex CLI 与 MCP 服务……")
        def worker():
            try:
                result = asyncio.run(self.agent.codex_status())
                content = "Codex CLI / MCP 实时状态：\n\n" + json.dumps(result, ensure_ascii=False, indent=2)
            except Exception as exc: content = f"检测失败：{exc}"
            self.root.after(0, lambda: self._replace_text(self.inspector_tools_text, content) if self.inspector_window and self.inspector_window.winfo_exists() else None)
        threading.Thread(target=worker, daemon=True).start()

    def open_settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift(); return
        self.refresh_microphones()
        win = self.settings_window = tk.Toplevel(self.root)
        win.title("桌宠设置"); win.geometry("720x900"); win.resizable(False, True)
        win.attributes("-topmost", True)
        frame = ttk.Frame(win, padding=18); frame.pack(fill="both", expand=True)
        stt = self.agent.config.get("stt", {}); mic = self.agent.config.get("microphone", {})
        fields = {}

        ttk.Label(frame, text="语音输入设备", font=("Microsoft YaHei UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        mic_box = ttk.Combobox(frame, state="readonly", width=66)
        mic_box["values"] = [f"{i} · {name}" for i, name in self.mic_devices]
        mic_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        selected = mic.get("device_id")
        match = next((n for n, (i, _) in enumerate(self.mic_devices) if selected is not None and i == int(selected)), 0)
        if self.mic_devices: mic_box.current(match)

        ttk.Label(frame, text="语音识别", font=("Microsoft YaHei UI", 11, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        labels = [("mode", "识别方式（api / faster_whisper）"), ("api_url", "语音识别 API 地址"), ("api_key", "API Key"), ("model", "模型名称"), ("language", "语言"), ("local_python", "本地 Python"), ("local_model", "本地模型目录")]
        for offset, (key, label) in enumerate(labels, start=3):
            ttk.Label(frame, text=label).grid(row=offset, column=0, sticky="w", padx=(0, 12), pady=5)
            if key == "mode":
                widget = ttk.Combobox(frame, values=["sound_mcp", "api", "faster_whisper"], state="readonly", width=45); widget.set(stt.get(key, "sound_mcp"))
            else:
                widget = ttk.Entry(frame, width=48, show="•" if key == "api_key" else "")
                value = os.getenv(stt.get("api_key_env", "STT_API_KEY"), "") if key == "api_key" else stt.get(key, "")
                widget.insert(0, value)
            widget.grid(row=offset, column=1, sticky="ew", pady=5); fields[key] = widget

        auto_var = tk.BooleanVar(value=bool(mic.get("auto_send_after_transcription", False)))
        top_var = tk.BooleanVar(value=bool(self.agent.config.get("desktop_pet", {}).get("always_on_top", True)))
        ttk.Checkbutton(frame, text="识别完成后自动发送", variable=auto_var, command=lambda: save()).grid(row=10, column=1, sticky="w", pady=(9, 2))
        ttk.Checkbutton(frame, text="桌宠始终置顶", variable=top_var, command=lambda: save()).grid(row=11, column=1, sticky="w", pady=2)

        control = self.agent.config.get("computer_control", {})
        control_var = tk.BooleanVar(value=bool(control.get("enabled", True)))
        full_access_var = tk.BooleanVar(value=bool(control.get("full_access", False)))
        confirm_var = tk.BooleanVar(value=bool(control.get("confirm_before_action", True)))
        confirm_launch_var = tk.BooleanVar(value=bool(control.get("confirm_launch_app", False)))
        ttk.Label(frame, text="电脑控制", font=("Microsoft YaHei UI", 11, "bold")).grid(row=12, column=0, columnspan=2, sticky="w", pady=(14, 5))
        ttk.Checkbutton(frame, text="允许家庭 Agent 使用电脑工具", variable=control_var, command=lambda: save()).grid(row=13, column=1, sticky="w", pady=2)
        ttk.Checkbutton(frame, text="完整磁盘访问权限", variable=full_access_var, command=lambda: save()).grid(row=14, column=1, sticky="w", pady=2)
        ttk.Checkbutton(frame, text="打开文件和网页前请求确认", variable=confirm_var, command=lambda: save()).grid(row=15, column=1, sticky="w", pady=2)
        ttk.Checkbutton(frame, text="启动应用前请求确认", variable=confirm_launch_var, command=lambda: save()).grid(row=16, column=1, sticky="w", pady=2)
        ttk.Label(frame, text="限制目录（关闭完整权限时使用）").grid(row=17, column=0, sticky="w", padx=(0, 12), pady=5)
        roots_entry = ttk.Entry(frame, width=48); roots_entry.insert(0, "；".join(control.get("allowed_roots", []))); roots_entry.grid(row=17, column=1, sticky="ew", pady=5)

        codex = self.agent.config.get("codex_cli", {})
        codex_enabled_var = tk.BooleanVar(value=bool(codex.get("enabled", False)))
        ttk.Label(frame, text="Codex CLI 与 MCP", font=("Microsoft YaHei UI", 11, "bold")).grid(row=18, column=0, columnspan=2, sticky="w", pady=(14, 5))
        ttk.Checkbutton(frame, text="启用 Codex CLI / MCP 工具", variable=codex_enabled_var, command=lambda: save()).grid(row=19, column=1, sticky="w", pady=2)
        codex_fields = {}
        codex_rows = [
            ("executable", "Codex 可执行文件"),
            ("working_directory", "Codex 工作目录"),
            ("sandbox", "CLI 权限模式"),
            ("trigger_mode", "触发模式"),
            ("trigger_keywords", "自动触发关键词（分号分隔）"),
            ("timeout_seconds", "任务超时秒数"),
        ]
        for row, (key, label) in enumerate(codex_rows, start=20):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            if key == "sandbox":
                widget = ttk.Combobox(frame, values=["read-only", "workspace-write", "danger-full-access"], state="readonly", width=45)
                widget.set(str(codex.get(key, "danger-full-access")))
            elif key == "trigger_mode":
                widget = ttk.Combobox(frame, values=["manual", "auto", "always"], state="readonly", width=45)
                widget.set(str(codex.get(key, "auto")))
            else:
                widget = ttk.Entry(frame, width=48)
                value = "；".join(str(x) for x in codex.get(key, [])) if key == "trigger_keywords" else str(codex.get(key, ""))
                widget.insert(0, value)
            widget.grid(row=row, column=1, sticky="ew", pady=4); codex_fields[key] = widget

        def test_codex():
            def worker():
                try:
                    result = asyncio.run(self.agent.codex_status())
                    text = f"CLI：{result.get('version', '')}\n\nMCP 服务：\n{result.get('mcp_servers') or '未配置'}"
                    self.root.after(0, lambda: messagebox.showinfo("Codex CLI / MCP 测试", text, parent=win))
                except Exception as exc:
                    self.root.after(0, lambda e=str(exc): messagebox.showerror("连接失败", e, parent=win))
            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(frame, text="测试 CLI 与 MCP", command=test_codex).grid(row=26, column=1, sticky="w", pady=(7, 2))

        save_timer = None
        save_status = tk.StringVar(value="修改后自动保存")

        def save():
            nonlocal save_timer
            save_timer = None
            # Always start from the latest file. CharacterManager and other
            # HomeAgent windows may have changed unrelated settings since this
            # window was opened, so writing the startup snapshot can resurrect
            # stale values.
            try:
                cfg = yaml.safe_load((HOME_AGENT / "config.yaml").read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                save_status.set(f"保存失败：{exc}")
                return False
            if self.mic_devices and mic_box.current() >= 0:
                cfg.setdefault("microphone", {})["device_id"] = self.mic_devices[mic_box.current()][0]
            cfg.setdefault("microphone", {})["auto_send_after_transcription"] = auto_var.get()
            for key in ("mode", "api_url", "model", "language", "local_python", "local_model"):
                cfg.setdefault("stt", {})[key] = fields[key].get().strip()
            cfg.setdefault("desktop_pet", {})["always_on_top"] = top_var.get()
            cfg.setdefault("computer_control", {})["enabled"] = control_var.get()
            cfg["computer_control"]["full_access"] = full_access_var.get()
            cfg["computer_control"]["confirm_before_action"] = confirm_var.get()
            cfg["computer_control"]["confirm_launch_app"] = confirm_launch_var.get()
            cfg["computer_control"]["allowed_roots"] = [x.strip() for x in roots_entry.get().replace("；", ";").split(";") if x.strip()]
            cfg.setdefault("codex_cli", {})["enabled"] = codex_enabled_var.get()
            cfg["codex_cli"]["executable"] = codex_fields["executable"].get().strip() or "codex"
            cfg["codex_cli"]["working_directory"] = codex_fields["working_directory"].get().strip() or str(ROOT)
            cfg["codex_cli"]["sandbox"] = codex_fields["sandbox"].get().strip() or "danger-full-access"
            cfg["codex_cli"]["trigger_mode"] = codex_fields["trigger_mode"].get().strip() or "auto"
            cfg["codex_cli"]["trigger_keywords"] = [x.strip() for x in codex_fields["trigger_keywords"].get().replace("；", ";").split(";") if x.strip()]
            try:
                cfg["codex_cli"]["timeout_seconds"] = max(10, int(codex_fields["timeout_seconds"].get().strip() or "600"))
            except ValueError:
                save_status.set("尚未保存：任务超时秒数必须是整数")
                return False
            cfg["codex_cli"].setdefault("skip_git_repo_check", True)
            try:
                saved = self._write_config_preserving_unknown(cfg)
                expected_confirm = bool(confirm_var.get())
                actual_confirm = bool(saved.get("computer_control", {}).get("confirm_before_action", True))
                if actual_confirm != expected_confirm:
                    raise OSError("打开文件和网页前确认设置写入后校验不一致")
                # Make the change effective immediately; no desktop-pet restart
                # should be required for computer-control permission settings.
                self.agent.config = saved
                self._save_env_value(saved["stt"].get("api_key_env", "STT_API_KEY"), fields["api_key"].get().strip())
                os.environ[saved["stt"].get("api_key_env", "STT_API_KEY")] = fields["api_key"].get().strip()
            except (OSError, yaml.YAMLError) as exc:
                save_status.set(f"保存失败：{exc}")
                return False
            self.root.attributes("-topmost", top_var.get())
            save_status.set(f"已自动保存 · {datetime.now():%H:%M:%S}")
            return True

        def schedule_save(_event=None):
            """Debounce text editing while keeping toggles truly immediate."""
            nonlocal save_timer
            if save_timer is not None:
                win.after_cancel(save_timer)
            save_status.set("正在编辑…")
            save_timer = win.after(500, save)

        mic_box.bind("<<ComboboxSelected>>", lambda _event: save())
        for widget in fields.values():
            widget.bind("<<ComboboxSelected>>", lambda _event: save())
            widget.bind("<KeyRelease>", schedule_save)
            widget.bind("<FocusOut>", lambda _event: save())
        roots_entry.bind("<KeyRelease>", schedule_save)
        roots_entry.bind("<FocusOut>", lambda _event: save())
        for widget in codex_fields.values():
            widget.bind("<<ComboboxSelected>>", lambda _event: save())
            widget.bind("<KeyRelease>", schedule_save)
            widget.bind("<FocusOut>", lambda _event: save())

        def close_settings():
            if save_timer is not None:
                win.after_cancel(save_timer)
            save()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_settings)
        ttk.Label(frame, textvariable=save_status, foreground="#287568").grid(row=27, column=0, columnspan=2, sticky="e", pady=(14, 0))
        frame.columnconfigure(1, weight=1)

    @staticmethod
    def _save_env_value(key, value):
        env_path = ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        replacement = f"{key}={value}"
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = replacement; break
        else:
            lines.append(replacement)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _write_config_preserving_unknown(cfg):
        """Keep sections added by a newer HomeAgent while an older UI is running."""
        config_path = HOME_AGENT / "config.yaml"
        try:
            current = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            current = {}
        for key, value in current.items():
            cfg.setdefault(key, value)
        # 视觉模式由角色管理器维护；旧桌宠进程不得用启动时的陈旧值覆盖它。
        if "vision_mcp" in current:
            cfg["vision_mcp"] = current["vision_mcp"]
        temporary = config_path.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(config_path)
        saved = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return saved

    def close(self):
        self.closing = True
        cfg = self.agent.config
        cfg.setdefault("desktop_pet", {})["x"] = self.root.winfo_x(); cfg["desktop_pet"]["y"] = self.root.winfo_y()
        self.agent.config = self._write_config_preserving_unknown(cfg)
        if self.stream:
            try: self.stream.stop(); self.stream.close()
            except Exception: pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    from qt_app import run
    raise SystemExit(run())
