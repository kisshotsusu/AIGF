from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import ctypes
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml
from PIL import Image, ImageTk

ROOT = Path(r"E:\Doc\AI直播")
CONFIG = ROOT / "config.yaml"
WORKSPACE = ROOT / "workspace"
IDENTITY = WORKSPACE / "IDENTITY.yaml"
PROFILE = WORKSPACE / "CHARACTER.md"
IMAGES = WORKSPACE / "character_images"
MANIFEST = IMAGES / "manifest.json"
MEMORY = WORKSPACE / "memory"
ENV_FILE = ROOT / ".env"
AUDIO = ROOT / "audio"
HOME_AGENT_CONFIG = ROOT / "HomeAgent" / "config.yaml"
MCP_CONFIG = WORKSPACE / "MCP_SERVERS.yaml"
WORKBUDDY_MCP = Path.home() / ".workbuddy" / "mcp.json"
DOCUMENTS = {
    "灵魂与人格": WORKSPACE / "SOUL.md",
    "通用安全规则": WORKSPACE / "RULES.md",
    "直播模式规则": WORKSPACE / "LIVE_RULES.md",
    "家庭模式规则": WORKSPACE / "HOME_RULES.md",
    "能力文档": WORKSPACE / "ABILITIES.md",
    "家庭场景": WORKSPACE / "HOME.md",
}


class CharacterManager:
    def __init__(self):
        self._mutex = None
        if os.name == "nt":
            try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                try: ctypes.windll.user32.SetProcessDPIAware()
                except Exception: pass
            self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AI-Live-Character-Manager")
            if ctypes.windll.kernel32.GetLastError() == 183:
                ctypes.windll.user32.MessageBoxW(None, "角色管理器已经在运行。", "AI 角色管理器", 0x40)
                raise SystemExit(0)
        self.startup_warnings = []
        WORKSPACE.mkdir(parents=True, exist_ok=True); IMAGES.mkdir(parents=True, exist_ok=True); MEMORY.mkdir(parents=True, exist_ok=True)
        self.cleanup_audio()
        self.config = self._load_yaml(CONFIG, {})
        self.identity = self._load_yaml(IDENTITY, {}) if IDENTITY.exists() else {}
        self.manifest = self._load_manifest()
        self.root = tk.Tk(); self.root.title("AI 角色工作台"); self.root.geometry("1180x820"); self.root.minsize(920, 680)
        self.root.configure(bg="#edf3f1")
        self.root.report_callback_exception = self._report_callback_exception
        self._style(); self._build()
        self.root.after(30_000, self._periodic_audio_cleanup)
        if self.startup_warnings: self.root.after(300, self._show_startup_warnings)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _load_yaml(self, path: Path, fallback):
        try: return yaml.safe_load(path.read_text(encoding="utf-8")) or fallback
        except (OSError, yaml.YAMLError, UnicodeError) as exc:
            backup = path.with_name(f"{path.name}.broken-{datetime.now():%Y%m%d-%H%M%S}")
            try: shutil.copy2(path, backup)
            except OSError: backup = None
            self.startup_warnings.append(f"{path.name} 读取失败：{exc}" + (f"\n已备份到 {backup}" if backup else ""))
            return fallback.copy() if isinstance(fallback, dict) else fallback

    @staticmethod
    def _atomic_write_text(path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        temporary.replace(path)

    @classmethod
    def _atomic_write_yaml(cls, path: Path, value):
        cls._atomic_write_text(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False))

    @classmethod
    def _atomic_write_json(cls, path: Path, value):
        cls._atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def _report_callback_exception(self, exc_type, exc, tb):
        log_dir = ROOT / "CharacterManager" / "logs"; log_dir.mkdir(parents=True, exist_ok=True)
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with (log_dir / "character-manager-errors.log").open("a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{detail}")
        except OSError: pass
        self.set_status(f"操作失败：{exc}", error=True)
        messagebox.showerror("操作失败", f"{exc}\n\n详细信息已写入日志。", parent=self.root)

    def _show_startup_warnings(self):
        self.set_status("部分配置读取失败，已使用安全默认值", error=True)
        messagebox.showwarning("配置恢复", "\n\n".join(self.startup_warnings), parent=self.root)

    def _periodic_audio_cleanup(self):
        try: self.cleanup_audio()
        except Exception as exc: self.set_status(f"音频清理已跳过：{exc}", error=True)
        try: self.root.after(30_000, self._periodic_audio_cleanup)
        except tk.TclError: pass

    @staticmethod
    def cleanup_audio(directory: Path = AUDIO, keep: int = 20) -> int:
        """启动时删除旧音频，只保留最后修改时间最新的指定数量。"""
        extensions = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}
        if not directory.exists():
            return 0
        files = []
        for path in directory.iterdir():
            try:
                if path.is_file() and path.suffix.lower() in extensions: files.append(path)
            except OSError: continue
        def modified(path):
            try: return path.stat().st_mtime_ns
            except OSError: return 0
        files.sort(key=lambda path: (modified(path), path.name.lower()), reverse=True)
        deleted = 0
        for path in files[max(0, keep):]:
            try:
                path.unlink(); deleted += 1
            except OSError:
                # 单个文件可能正在被播放器占用，不应阻止角色管理器启动。
                continue
        return deleted

    def _rounded_asset(self, fill, outline=None, radius=9, size=32):
        """生成可由 ttk 九宫格拉伸的透明圆角底图。"""
        image = tk.PhotoImage(master=self.root, width=size, height=size)
        inner = 1 if outline else 0
        for y in range(size):
            for x in range(size):
                dx = max(radius - x, 0, x - (size - radius - 1))
                dy = max(radius - y, 0, y - (size - radius - 1))
                if dx * dx + dy * dy <= radius * radius:
                    color = outline or fill
                    if inner and 1 <= x < size - 1 and 1 <= y < size - 1:
                        ix, iy = x - 1, y - 1; inner_size = size - 2; inner_radius = radius - 1
                        idx = max(inner_radius - ix, 0, ix - (inner_size - inner_radius - 1))
                        idy = max(inner_radius - iy, 0, iy - (inner_size - inner_radius - 1))
                        if idx * idx + idy * idy <= inner_radius * inner_radius: color = fill
                    image.put(color, (x, y))
        self._style_images.append(image)
        return image

    def _style(self):
        style = ttk.Style(); style.theme_use("clam")
        self._style_images = []
        self.colors = {"surface": "#f6f9f9", "surface2": "#ffffff", "container": "#e4f2ef", "primary": "#16766f", "primary_dark": "#105c57", "coral": "#d97863", "orange": "#f0b36a", "on_primary": "#ffffff", "text": "#172326", "muted": "#718083", "outline": "#dce7e5"}
        c = self.colors; self.root.configure(bg=c["surface"])
        style.configure("TFrame", background=c["surface"]); style.configure("Card.TFrame", background=c["surface2"], relief="solid", borderwidth=1)
        style.configure("TButton", padding=(14, 9), borderwidth=1, shiftrelief=0, font=("Microsoft YaHei UI", 9, "bold"), background=c["container"], foreground=c["text"], bordercolor=c["outline"])
        style.map("TButton", background=[("active", c["container"]), ("pressed", "#bcd8d2")])
        style.configure("TLabel", background=c["surface"], foreground=c["text"], font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background=c["surface2"], foreground=c["text"], font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background=c["surface"], foreground=c["text"], font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("TEntry", padding=8, fieldbackground="#ffffff", bordercolor=c["outline"], lightcolor=c["outline"], darkcolor=c["outline"])
        style.configure("TCombobox", padding=7, fieldbackground="#ffffff")
        style.configure("TNotebook", background=c["surface"], borderwidth=0); style.configure("TNotebook.Tab", padding=(18, 11), borderwidth=0, font=("Microsoft YaHei UI", 10), background=c["surface2"], foreground=c["muted"])
        style.map("TNotebook.Tab", padding=[("selected", (24, 15)), ("!selected", (18, 11))], background=[("selected", c["container"])], foreground=[("selected", c["text"])])
        style.configure("Treeview", rowheight=36, borderwidth=0, font=("Microsoft YaHei UI", 9), background="#ffffff", fieldbackground="#ffffff", foreground=c["text"])
        style.configure("Treeview.Heading", padding=8, borderwidth=0, font=("Microsoft YaHei UI", 9, "bold"), background=c["surface2"], foreground=c["primary"])
        style.configure("Accent.TButton", background=c["primary"], foreground=c["on_primary"], shiftrelief=0, font=("Microsoft YaHei UI", 9, "bold"))
        style.map("Accent.TButton", background=[("active", "#126861"), ("pressed", c["primary_dark"])])
        style.configure("TCheckbutton", background=c["surface2"], foreground=c["text"], font=("Microsoft YaHei UI", 9))

        # clam 默认是硬直角；以下底图使用九宫格拉伸，控件放大后仍保持圆角。
        button = self._rounded_asset(c["container"], c["outline"], 10, 34)
        button_hover = self._rounded_asset("#d1d5db", "#9ca3af", 10, 34)
        button_down = self._rounded_asset("#cbd0d7", "#9ca3af", 10, 34)
        accent = self._rounded_asset(c["primary"], c["primary"], 10, 34)
        accent_hover = self._rounded_asset("#126861", "#126861", 10, 34)
        accent_down = self._rounded_asset(c["primary_dark"], c["primary_dark"], 10, 34)
        entry = self._rounded_asset("#faf9f5", "#d7d4cc", 9, 32)
        card = self._rounded_asset(c["surface2"], c["outline"], 14, 42)
        tab = self._rounded_asset(c["surface2"], c["outline"], 10, 34)
        tab_selected = self._rounded_asset(c["container"], "#9ca3af", 11, 38)
        style.element_create("RoundButton.border", "image", button, ("pressed", button_down), ("active", button_hover), border=11, sticky="nswe")
        style.element_create("RoundAccent.border", "image", accent, ("pressed", accent_down), ("active", accent_hover), border=11, sticky="nswe")
        style.element_create("RoundEntry.field", "image", entry, border=10, sticky="nswe")
        style.element_create("RoundCombo.field", "image", entry, border=10, sticky="nswe")
        style.element_create("RoundSpin.field", "image", entry, border=10, sticky="nswe")
        style.element_create("RoundCard.background", "image", card, border=15, sticky="nswe")
        style.element_create("RoundTab.background", "image", tab, ("selected", tab_selected), border=11, sticky="nswe")
        style.layout("TButton", [("RoundButton.border", {"sticky": "nswe", "children": [("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
        style.layout("Accent.TButton", [("RoundAccent.border", {"sticky": "nswe", "children": [("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
        # 输入控件保留原生布局，确保所有 Windows/Tk 版本都能稳定获得焦点和编辑文本。
        style.layout("TEntry", [("Entry.field", {"sticky": "nswe", "border": "1", "children": [("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
        style.layout("TCombobox", [("Combobox.downarrow", {"side": "right", "sticky": "ns"}), ("Combobox.field", {"sticky": "nswe", "children": [("Combobox.padding", {"sticky": "nswe", "children": [("Combobox.textarea", {"sticky": "nswe"})]})]})])
        style.layout("TSpinbox", [("Spinbox.field", {"side": "top", "sticky": "we", "children": [("Spinbox.uparrow", {"side": "right", "sticky": "ne"}), ("Spinbox.downarrow", {"side": "right", "sticky": "se"}), ("Spinbox.padding", {"sticky": "nswe", "children": [("Spinbox.textarea", {"sticky": "nswe"})]})]})])
        style.layout("Card.TFrame", [("RoundCard.background", {"sticky": "nswe"})])
        style.layout("TNotebook.Tab", [("RoundTab.background", {"sticky": "nswe", "children": [("Notebook.padding", {"sticky": "nswe", "children": [("Notebook.label", {"sticky": ""})]})]})])

    def _build(self):
        c = self.colors
        head = tk.Frame(self.root, bg=c["surface"], padx=26, pady=16); head.pack(fill="x")
        tk.Label(head, text="角", width=3, bg=c["primary"], fg="white", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left", padx=(0, 12), ipady=5)
        ttk.Label(head, text="AI 角色管理器", style="Title.TLabel").pack(side="left")
        tk.Label(head, text="CHARACTER STUDIO\n角色 · 人格 · 记忆 · 能力", justify="right", bg=c["surface"], fg=c["primary"], font=("Microsoft YaHei UI", 9)).pack(side="right")
        tk.Frame(self.root, bg=c["outline"], height=1).pack(fill="x")
        footer = tk.Frame(self.root, bg=c["surface2"], height=34); footer.pack(fill="x", side="bottom"); footer.pack_propagate(False)
        self.status_dot = tk.Label(footer, text="●", bg=c["surface2"], fg=c["primary"], font=("Microsoft YaHei UI", 9)); self.status_dot.pack(side="left", padx=(22, 7))
        self.status_var = tk.StringVar(value="就绪 · 所有关键配置采用安全写入")
        tk.Label(footer, textvariable=self.status_var, bg=c["surface2"], fg=c["muted"], font=("Microsoft YaHei UI", 9)).pack(side="left")
        tk.Label(footer, text=str(WORKSPACE), bg=c["surface2"], fg=c["muted"], font=("Consolas", 8)).pack(side="right", padx=20)
        tabs = self.tabs = ttk.Notebook(self.root); tabs.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        identity_page, identity_tab = self._scrollable_page(tabs); memory_tab = ttk.Frame(tabs, padding=18); docs_tab = ttk.Frame(tabs, padding=18)
        model_tab = ttk.Frame(tabs, padding=18); voice_tab = ttk.Frame(tabs, padding=18)
        image_tab = ttk.Frame(tabs, padding=18); api_tab = ttk.Frame(tabs, padding=18); tools_tab = ttk.Frame(tabs, padding=18)
        tabs.add(identity_page, text="身份"); tabs.add(memory_tab, text="记忆"); tabs.add(docs_tab, text="人格规则")
        tabs.add(model_tab, text="模型 API"); tabs.add(voice_tab, text="语音")
        tabs.add(tools_tab, text="工具维护"); tabs.add(image_tab, text="形象"); tabs.add(api_tab, text="图片 API")
        self._build_identity(identity_tab); self._build_memory(memory_tab); self._build_documents(docs_tab)
        self._build_model_api(model_tab); self._build_voice(voice_tab)
        self._build_tools_maintenance(tools_tab)
        self._build_images(image_tab); self._build_api(api_tab)
        tabs.bind("<<NotebookTabChanged>>", lambda _event: self.set_status(f"正在编辑：{tabs.tab(tabs.select(), 'text')}"))

    def _scrollable_page(self, parent):
        outer = ttk.Frame(parent); canvas = tk.Canvas(outer, bg=self.colors["surface"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview); canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas, padding=18); window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
        return outer, inner

    def set_status(self, text: str, error: bool = False):
        if hasattr(self, "status_var"): self.status_var.set(str(text))
        if hasattr(self, "status_dot"): self.status_dot.configure(fg=self.colors["coral"] if error else self.colors["primary"])

    def _build_identity(self, tab):
        char = self.identity.get("character", {}); user = self.identity.get("user", {})
        save_bar = self.identity_save_bar = ttk.Frame(tab, style="Card.TFrame", padding=(12, 8))
        save_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(save_bar, text="角色身份资料", style="Card.TLabel", font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.identity_save_state = ttk.Label(save_bar, text="修改后自动保存", style="Card.TLabel", foreground=self.colors["muted"])
        self.identity_save_state.pack(side="right", padx=(12, 0))
        ttk.Button(save_bar, text="保存身份", style="Accent.TButton", command=self.save_identity).pack(side="right")
        specs = [
            ("name", "角色名称", char.get("name", "小助手")), ("identity", "角色身份", char.get("identity", "")),
            ("gender", "性别设定", char.get("gender", "")), ("visual_age", "视觉年龄", char.get("visual_age", "")),
            ("personality", "核心性格", char.get("personality", "")), ("relationship_to_user", "与用户关系", char.get("relationship_to_user", "")),
            ("user_title", "对用户称呼", char.get("user_title", "主人")), ("user_name", "用户姓名", user.get("name", "主人")),
            ("user_aliases", "家庭称呼别名", "，".join(user.get("aliases", ["主人", "老公"]))),
            ("live_usernames", "主人直播用户名", "，".join(user.get("live_usernames", []))),
        ]
        self.identity_fields = {}
        for row, (key, label, value) in enumerate(specs, start=1):
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=6)
            entry = ttk.Entry(tab); entry.insert(0, value); entry.grid(row=row, column=1, sticky="ew", pady=6)
            entry.bind("<FocusOut>", lambda _event: self.save_identity(False), add="+")
            self.identity_fields[key] = entry
        notes_row = len(specs) + 1; profile_row = notes_row + 1; button_row = profile_row + 1
        ttk.Label(tab, text="身份补充说明").grid(row=notes_row, column=0, sticky="nw", padx=(0, 14), pady=6)
        self.notes = tk.Text(tab, height=4, wrap="word", bg="#ffffff", fg=self.colors["text"], relief="flat", padx=10, pady=8, font=("Microsoft YaHei UI", 10)); self.notes.grid(row=notes_row, column=1, sticky="nsew", pady=6)
        self.notes.insert("1.0", self.identity.get("notes", ""))
        ttk.Label(tab, text="固定外观文档（CHARACTER.md）").grid(row=profile_row, column=0, sticky="nw", padx=(0, 14), pady=6)
        self.profile = tk.Text(tab, height=10, wrap="word", bg="#ffffff", fg=self.colors["text"], relief="flat", padx=10, pady=8, font=("Microsoft YaHei UI", 10)); self.profile.grid(row=profile_row, column=1, sticky="nsew", pady=6)
        self.profile.insert("1.0", PROFILE.read_text(encoding="utf-8") if PROFILE.exists() else "# 角色形象说明\n")
        ttk.Button(tab, text="保存角色身份", command=self.save_identity).grid(row=button_row, column=1, sticky="e", pady=(12, 0))
        tab.columnconfigure(1, weight=1); tab.rowconfigure(profile_row, weight=1)

    def _build_memory(self, tab):
        cfg = self.config.setdefault("memory_write", {})
        strategy = ttk.Frame(tab, style="Card.TFrame", padding=16); strategy.pack(fill="x", pady=(0, 14))
        ttk.Label(strategy, text="家庭与直播共享记忆策略", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(strategy, text="两种模式使用相同的开关、阈值、每日上限和关键词规则", style="Card.TLabel", foreground=self.colors["muted"]).grid(row=0, column=1, columnspan=5, sticky="w", padx=12)
        self.memory_rule_fields = {}
        ttk.Label(strategy, text="写入模式", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(14, 4))
        mode = ttk.Combobox(strategy, values=["important", "off", "all"], state="readonly", width=15); mode.set(cfg.get("mode", "important")); mode.grid(row=2, column=0, sticky="ew", padx=(0, 10)); self.memory_rule_fields["mode"] = mode
        ttk.Label(strategy, text="重要度阈值", style="Card.TLabel").grid(row=1, column=1, sticky="w", pady=(14, 4))
        threshold_var = tk.IntVar(value=int(cfg.get("importance_threshold", 70)))
        threshold = ttk.Scale(strategy, from_=0, to=100, variable=threshold_var); threshold.grid(row=2, column=1, sticky="ew", padx=(0, 6)); self.memory_threshold_label = ttk.Label(strategy, text=str(threshold_var.get()), style="Card.TLabel", width=3); self.memory_threshold_label.grid(row=2, column=2, sticky="w")
        threshold.configure(command=lambda value: self.memory_threshold_label.configure(text=str(round(float(value)))))
        self.memory_rule_fields["importance_threshold"] = threshold_var
        ttk.Label(strategy, text="每日上限", style="Card.TLabel").grid(row=1, column=3, sticky="w", pady=(14, 4))
        daily = ttk.Spinbox(strategy, from_=0, to=500, width=8); daily.set(cfg.get("max_daily_writes", 20)); daily.grid(row=2, column=3, sticky="ew", padx=(0, 10)); self.memory_rule_fields["max_daily_writes"] = daily
        ttk.Label(strategy, text="最短消息长度", style="Card.TLabel").grid(row=1, column=4, sticky="w", pady=(14, 4))
        minimum = ttk.Spinbox(strategy, from_=1, to=200, width=8); minimum.set(cfg.get("min_message_length", 4)); minimum.grid(row=2, column=4, sticky="ew", padx=(0, 10)); self.memory_rule_fields["min_message_length"] = minimum
        ai_var = tk.BooleanVar(value=cfg.get("analyze_with_llm", True)); self.memory_rule_fields["analyze_with_llm"] = ai_var
        ttk.Checkbutton(strategy, text="使用 AI 判断重要度", variable=ai_var).grid(row=2, column=5, sticky="w")
        ttk.Label(strategy, text="强制记忆关键词", style="Card.TLabel").grid(row=3, column=0, sticky="w", pady=(12, 4))
        always = ttk.Entry(strategy); always.insert(0, "，".join(cfg.get("always_keywords", []))); always.grid(row=4, column=0, columnspan=3, sticky="ew", padx=(0, 10)); self.memory_rule_fields["always_keywords"] = always
        ttk.Label(strategy, text="忽略关键词", style="Card.TLabel").grid(row=3, column=3, sticky="w", pady=(12, 4))
        ignore = ttk.Entry(strategy); ignore.insert(0, "，".join(cfg.get("ignore_keywords", []))); ignore.grid(row=4, column=3, columnspan=2, sticky="ew", padx=(0, 10)); self.memory_rule_fields["ignore_keywords"] = ignore
        ttk.Button(strategy, text="保存策略", style="Accent.TButton", command=self.save_memory_strategy).grid(row=4, column=5, sticky="e")
        for column in (0, 1, 3, 4): strategy.columnconfigure(column, weight=1)
        toolbar = ttk.Frame(tab); toolbar.pack(fill="x", pady=(0, 12))
        ttk.Label(toolbar, text="直播助手与家庭桌宠共用的长期记忆", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ttk.Button(toolbar, text="＋ 添加记忆", style="Accent.TButton", command=lambda: self.open_memory_editor()).pack(side="right")
        self.memory_search = ttk.Entry(toolbar, width=28); self.memory_search.pack(side="right", padx=8)
        self.memory_search.bind("<Return>", lambda _: self.refresh_memories())
        ttk.Button(toolbar, text="搜索", command=self.refresh_memories).pack(side="right")
        columns = ("time", "privacy", "type", "user", "content", "tags", "attachments")
        self.memory_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headings = {"time": "时间", "privacy": "可见范围", "type": "类型", "user": "相关用户", "content": "记忆内容", "tags": "标签", "attachments": "回忆附件"}
        widths = {"time": 135, "privacy": 75, "type": 80, "user": 100, "content": 350, "tags": 150, "attachments": 90}
        for key in columns:
            self.memory_tree.heading(key, text=headings[key]); self.memory_tree.column(key, width=widths[key], anchor="w")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.memory_tree.yview); self.memory_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y"); self.memory_tree.pack(fill="both", expand=True)
        self.memory_tree.bind("<Double-1>", lambda _: self.edit_selected_memory())
        actions = ttk.Frame(tab); actions.pack(fill="x", pady=(10, 0))
        self.memory_count = ttk.Label(actions, text="0 条记忆", foreground="#52615f"); self.memory_count.pack(side="left")
        ttk.Button(actions, text="删除", command=self.delete_selected_memory).pack(side="right")
        ttk.Button(actions, text="编辑", command=self.edit_selected_memory).pack(side="right", padx=7)
        ttk.Button(actions, text="编辑标签", command=self.edit_selected_memory_tags).pack(side="right")
        ttk.Button(actions, text="打开附件", command=self.open_memory_attachments).pack(side="right")
        ttk.Button(actions, text="刷新", command=self.refresh_memories).pack(side="right")
        self.refresh_memories()

    def save_memory_strategy(self):
        # 保存前重新读取，避免角色工作台覆盖直播控制台刚刚修改的其他配置。
        latest = self._load_yaml(CONFIG, {})
        fields = self.memory_rule_fields; cfg = latest.setdefault("memory_write", {})
        cfg.update({
            "mode": fields["mode"].get(),
            "importance_threshold": round(fields["importance_threshold"].get()),
            "analyze_with_llm": fields["analyze_with_llm"].get(),
            "max_daily_writes": int(fields["max_daily_writes"].get() or 0),
            "min_message_length": int(fields["min_message_length"].get() or 1),
            "always_keywords": [x.strip() for x in fields["always_keywords"].get().replace("，", ",").split(",") if x.strip()],
            "ignore_keywords": [x.strip() for x in fields["ignore_keywords"].get().replace("，", ",").split(",") if x.strip()],
        })
        self._atomic_write_yaml(CONFIG, latest)
        self.config = latest
        messagebox.showinfo("策略已保存", "直播助手和家庭桌宠将在下次启动时共同使用新的记忆规则。")

    def _build_documents(self, tab):
        side = tk.Frame(tab, bg=self.colors["surface2"], width=190); side.pack(side="left", fill="y", padx=(0, 14)); side.pack_propagate(False)
        tk.Label(side, text="工作区文档", bg=self.colors["surface2"], fg=self.colors["primary"], font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=14, pady=(16, 10))
        self.doc_buttons = {}; self.active_document = next(iter(DOCUMENTS))
        self._document_loading = True; self._document_dirty = False; self._doc_save_job = None
        for name in DOCUMENTS:
            button = tk.Button(side, text=name, anchor="w", relief="flat", bd=0, bg=self.colors["surface2"], fg=self.colors["text"], activebackground=self.colors["container"], font=("Microsoft YaHei UI", 10), command=lambda n=name: self.select_document(n))
            button.pack(fill="x", padx=8, pady=3, ipady=8); self.doc_buttons[name] = button
        editor_area = ttk.Frame(tab); editor_area.pack(side="left", fill="both", expand=True)
        self.doc_title = ttk.Label(editor_area, text="", font=("Microsoft YaHei UI", 14, "bold")); self.doc_title.pack(anchor="w")
        self.doc_hint = ttk.Label(editor_area, text="", foreground="#52615f"); self.doc_hint.pack(anchor="w", pady=(3, 10))
        self.doc_save_state = ttk.Label(editor_area, text="自动保存已启用", foreground=self.colors["muted"]); self.doc_save_state.pack(anchor="e", pady=(0, 5))
        self.doc_editor = tk.Text(editor_area, wrap="word", undo=True, bg="#ffffff", fg=self.colors["text"], insertbackground=self.colors["primary"], relief="flat", padx=16, pady=14, font=("Microsoft YaHei UI", 10), spacing2=4)
        self.doc_editor.pack(fill="both", expand=True)
        self.doc_editor.bind("<<Modified>>", self._on_document_modified, add="+")
        self.doc_editor.bind("<FocusOut>", lambda _event: self.save_document(False), add="+")
        ttk.Button(editor_area, text="保存当前文档", style="Accent.TButton", command=self.save_document).pack(anchor="e", pady=(10, 0))
        self.select_document(self.active_document)

    def _on_document_modified(self, _event=None):
        if not self.doc_editor.edit_modified(): return
        self.doc_editor.edit_modified(False)
        if self._document_loading: return
        self._document_dirty = True
        self.doc_save_state.configure(text="正在编辑 · 即将自动保存", foreground="#9a6700")
        if self._doc_save_job:
            try: self.root.after_cancel(self._doc_save_job)
            except tk.TclError: pass
        self._doc_save_job = self.root.after(800, lambda: self.save_document(False))

    def _memory_rows(self):
        rows = []
        for path in sorted(MEMORY.glob("*.jsonl"), reverse=True):
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                try: item = json.loads(line)
                except json.JSONDecodeError: continue
                if not isinstance(item, dict): continue
                row = dict(item); row["_file"] = path; row["_index"] = index
                row["_key"] = str(item.get("id") or f"{path.name}:{index}"); rows.append(row)
        return sorted(rows, key=lambda x: str(x.get("updated_at") or x.get("time") or ""), reverse=True)

    @staticmethod
    def _memory_content(item):
        return str(item.get("content") or item.get("message") or "")

    def refresh_memories(self):
        self.memory_rows = self._memory_rows(); query = self.memory_search.get().strip().lower() if hasattr(self, "memory_search") else ""
        if query: self.memory_rows = [x for x in self.memory_rows if query in json.dumps(x, ensure_ascii=False, default=str).lower()]
        self.memory_tree.delete(*self.memory_tree.get_children())
        for item in self.memory_rows:
            attachments = item.get("attachments", []) if isinstance(item.get("attachments"), list) else []
            visibility = "私密" if item.get("privacy") == "private" else "共享"
            tags = item.get("tags", []) if isinstance(item.get("tags"), list) else []
            self.memory_tree.insert("", "end", iid=item["_key"], values=(str(item.get("time", "")).replace("T", " "), visibility, item.get("type", "memory"), item.get("user", ""), self._memory_content(item), " · ".join(tags), f"{len(attachments)} 个文件" if attachments else ""))
        self.memory_count.configure(text=f"{len(self.memory_rows)} 条记忆")

    def selected_memory(self):
        selection = self.memory_tree.selection()
        return next((x for x in self.memory_rows if selection and x["_key"] == selection[0]), None)

    def edit_selected_memory(self):
        item = self.selected_memory()
        if not item: return messagebox.showwarning("未选择", "请先选择一条记忆。")
        self.open_memory_editor(item)

    def edit_selected_memory_tags(self):
        item = self.selected_memory()
        if not item: return messagebox.showwarning("未选择", "请先选择一条记忆。")
        win = tk.Toplevel(self.root); win.title("编辑记忆标签"); win.geometry("520x250"); win.resizable(False, False); win.transient(self.root); win.grab_set()
        box = ttk.Frame(win, padding=20); box.pack(fill="both", expand=True)
        ttk.Label(box, text="手动标签", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(box, text="使用逗号分隔多个标签，例如：童年、旅行、家人、重要", foreground=self.colors["muted"]).pack(anchor="w", pady=(4, 12))
        entry = ttk.Entry(box); entry.pack(fill="x"); entry.insert(0, "，".join(item.get("tags", [])) if isinstance(item.get("tags"), list) else "")
        private_var = tk.BooleanVar(value=item.get("privacy") == "private")
        ttk.Checkbutton(box, text="同时标记为私密记忆", variable=private_var).pack(anchor="w", pady=14)
        def save_tags():
            values = [x.strip() for x in entry.get().replace("，", ",").split(",") if x.strip() and x.strip() != "隐私"]
            if private_var.get(): values.append("隐私")
            # 去重但保留用户输入顺序。
            values = list(dict.fromkeys(values))
            updated = {k: v for k, v in item.items() if not k.startswith("_")}
            updated.update({"tags": values, "privacy": "private" if private_var.get() else "shared", "updated_at": datetime.now().isoformat(timespec="seconds")})
            self._replace_memory_line(item, updated); win.destroy(); self.refresh_memories()
        ttk.Button(box, text="保存标签", style="Accent.TButton", command=save_tags).pack(anchor="e", pady=(4, 0))
        entry.focus_set()

    def open_memory_editor(self, item=None):
        win = tk.Toplevel(self.root); win.title("编辑记忆" if item else "添加记忆"); win.geometry("650x560"); win.transient(self.root); win.grab_set()
        box = ttk.Frame(win, padding=18); box.pack(fill="both", expand=True)
        ttk.Label(box, text="记忆类型").grid(row=0, column=0, sticky="w", pady=6)
        kind = ttk.Combobox(box, values=["manual", "identity", "preference", "relationship", "agreement", "event"], state="readonly"); kind.set(item.get("type", "manual") if item else "manual"); kind.grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(box, text="相关用户").grid(row=1, column=0, sticky="w", pady=6)
        user = ttk.Entry(box); user.insert(0, item.get("user", "") if item else ""); user.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(box, text="记忆内容").grid(row=2, column=0, sticky="nw", pady=6)
        content = tk.Text(box, height=10, wrap="word", font=("Microsoft YaHei UI", 10)); content.insert("1.0", self._memory_content(item) if item else ""); content.grid(row=2, column=1, sticky="nsew", pady=6)
        ttk.Label(box, text="标签").grid(row=3, column=0, sticky="w", pady=6)
        tags = ttk.Entry(box); tags.insert(0, "，".join(item.get("tags", [])) if item and isinstance(item.get("tags"), list) else ""); tags.grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Label(box, text="照片/文件链接").grid(row=4, column=0, sticky="w", pady=6)
        attachment_row = ttk.Frame(box); attachment_row.grid(row=4, column=1, sticky="ew", pady=6); attachment_row.columnconfigure(0, weight=1)
        attachments = ttk.Entry(attachment_row); attachments.insert(0, "；".join(item.get("attachments", [])) if item and isinstance(item.get("attachments"), list) else ""); attachments.grid(row=0, column=0, sticky="ew")
        def choose_files():
            selected = filedialog.askopenfilenames(parent=win, title="选择回忆照片或文件", filetypes=[("照片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("所有文件", "*.*")])
            if selected:
                current = [x.strip() for x in attachments.get().replace("；", ";").split(";") if x.strip()]
                attachments.delete(0, "end"); attachments.insert(0, "；".join(dict.fromkeys([*current, *selected])))
        ttk.Button(attachment_row, text="选择文件", command=choose_files).grid(row=0, column=1, padx=(7, 0))
        private_var = tk.BooleanVar(value=bool(item and item.get("privacy") == "private"))
        ttk.Checkbutton(box, text="标记为私密记忆（仅家庭模式可读取，直播模式不可读取）", variable=private_var).grid(row=5, column=1, sticky="w", pady=8)
        def save():
            text = content.get("1.0", "end").strip()
            if not text: return messagebox.showerror("无法保存", "记忆内容不能为空。", parent=win)
            tag_values = [x.strip() for x in tags.get().replace("，", ",").split(",") if x.strip() and x.strip() != "隐私"]
            if private_var.get(): tag_values.append("隐私")
            attachment_values = [x.strip() for x in attachments.get().replace("；", ";").split(";") if x.strip()]
            common = {"type": kind.get(), "user": user.get().strip(), "content": text, "tags": tag_values, "privacy": "private" if private_var.get() else "shared", "attachments": attachment_values}
            if item:
                updated = {k: v for k, v in item.items() if not k.startswith("_")}; updated.update(common); updated["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._replace_memory_line(item, updated)
            else:
                now = datetime.now(); record = {"id": uuid.uuid4().hex, "time": now.isoformat(timespec="seconds"), **common, "source": "character-manager"}
                with (MEMORY / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f: f.write(json.dumps(record, ensure_ascii=False) + "\n")
            win.destroy(); self.refresh_memories()
        ttk.Button(box, text="保存记忆", style="Accent.TButton", command=save).grid(row=6, column=1, sticky="e", pady=(12, 0))
        box.columnconfigure(1, weight=1); box.rowconfigure(2, weight=1)

    def open_memory_attachments(self):
        item = self.selected_memory()
        if not item: return messagebox.showwarning("未选择", "请先选择一条记忆。")
        links = item.get("attachments", []) if isinstance(item.get("attachments"), list) else []
        if not links: return messagebox.showinfo("没有附件", "这条记忆没有照片或文件链接。")
        failed = []
        for link in links:
            try:
                if str(link).lower().startswith(("http://", "https://")): webbrowser.open(str(link))
                else: __import__("os").startfile(str(Path(link)))
            except OSError: failed.append(str(link))
        if failed: messagebox.showwarning("部分附件无法打开", "\n".join(failed))

    @staticmethod
    def _replace_memory_line(item, replacement=None):
        path = item["_file"]; lines = path.read_text(encoding="utf-8").splitlines()
        if replacement is None: lines.pop(item["_index"])
        else: lines[item["_index"]] = json.dumps(replacement, ensure_ascii=False)
        self._atomic_write_text(path, ("\n".join(lines) + "\n") if lines else "")

    def delete_selected_memory(self):
        item = self.selected_memory()
        if not item: return messagebox.showwarning("未选择", "请先选择一条记忆。")
        if messagebox.askyesno("确认删除", "确定删除所选共享记忆吗？此操作会同时影响直播和家庭模式。"):
            self._replace_memory_line(item); self.refresh_memories()

    def select_document(self, name):
        if hasattr(self, "doc_editor") and self._document_dirty:
            self.save_document(False)
        self.active_document = name; path = DOCUMENTS[name]
        hints = {"灵魂与人格": "定义两个场景共享的角色身份、关系、性格和价值取向。", "通用安全规则": "定义直播和家庭场景共同遵守的隐私、安全和事实边界。", "直播模式规则": "仅直播助手读取：控制公开回复长度、口语风格和观众互动。", "家庭模式规则": "仅家庭 Agent 读取：允许长对话、深入交流以及分段语音。", "能力文档": "声明角色能调用的工具、技能以及不能假装完成的事情。", "家庭场景": "定义桌宠在家中对话时的环境和表达方式。"}
        self.doc_title.configure(text=name); self.doc_hint.configure(text=hints[name])
        self._document_loading = True
        self.doc_editor.delete("1.0", "end"); self.doc_editor.insert("1.0", path.read_text(encoding="utf-8") if path.exists() else f"# {name}\n")
        self.doc_editor.edit_reset(); self.doc_editor.edit_modified(False)
        self._document_loading = False; self._document_dirty = False
        self.doc_save_state.configure(text=f"已加载 · {path.name}", foreground=self.colors["muted"])
        for key, button in self.doc_buttons.items():
            selected = key == name
            button.configure(bg=self.colors["container"] if selected else self.colors["surface2"], fg=self.colors["text"])
            # 选中项实际占用更大面积，而不是通过内缩制造选中感。
            button.pack_configure(padx=2 if selected else 8, ipady=11 if selected else 8)

    def save_document(self, notify=True):
        if not hasattr(self, "doc_editor") or self._document_loading: return
        if self._doc_save_job:
            try: self.root.after_cancel(self._doc_save_job)
            except tk.TclError: pass
            self._doc_save_job = None
        path = DOCUMENTS[self.active_document]
        self._atomic_write_text(path, self.doc_editor.get("1.0", "end").strip() + "\n")
        self._document_dirty = False
        self.doc_save_state.configure(text=f"已保存 · {datetime.now():%H:%M:%S}", foreground="#4a826d")
        if notify: messagebox.showinfo("保存成功", f"{self.active_document}已保存，下一次模型回复时生效。")

    def _build_model_api(self, tab):
        llm = self.config.setdefault("llm", {}); providers = llm.setdefault("providers", {})
        intro = ttk.Frame(tab, style="Card.TFrame", padding=16); intro.pack(fill="x", pady=(0, 14))
        ttk.Label(intro, text="模型与 API", style="Card.TLabel", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(intro, text="共用模型供应商，并可分别控制家庭、直播和记忆分析的回复随机度与长度。API Key 留空会保留现有值。", style="Card.TLabel", foreground=self.colors["muted"]).pack(anchor="w", pady=(4, 0))
        general = ttk.Frame(tab); general.pack(fill="x", pady=(0, 12))
        ttk.Label(general, text="当前供应商").grid(row=0, column=0, sticky="w")
        self.current_provider = ttk.Combobox(general, values=list(providers) or ["deepseek", "mimo", "custom"], state="readonly", width=18); self.current_provider.set(llm.get("provider", "deepseek")); self.current_provider.grid(row=1, column=0, sticky="ew", padx=(0, 12), pady=(5, 0))
        ttk.Label(general, text="场景").grid(row=0, column=1, sticky="w")
        ttk.Label(general, text="温度（0稳定，1更活泼）").grid(row=0, column=2, sticky="w")
        ttk.Label(general, text="最大 Tokens（控制长度）").grid(row=0, column=3, sticky="w")
        self.llm_tuning_fields = {}
        defaults = {"home": (0.7, 600), "live": (0.55, 160), "memory": (0.2, 180)}
        labels = {"home": "家庭对话", "live": "直播回复", "memory": "记忆判断"}
        for row, profile in enumerate(("home", "live", "memory"), start=1):
            values = llm.get(profile, {}); default_temp, default_tokens = defaults[profile]
            ttk.Label(general, text=labels[profile]).grid(row=row, column=1, sticky="w", padx=(0, 12), pady=5)
            temp = ttk.Entry(general, width=12); temp.insert(0, values.get("temperature", default_temp)); temp.grid(row=row, column=2, sticky="ew", padx=(0, 12), pady=5)
            tokens = ttk.Entry(general, width=14); tokens.insert(0, values.get("max_tokens", default_tokens)); tokens.grid(row=row, column=3, sticky="ew", pady=5)
            self.llm_tuning_fields[profile] = (temp, tokens)
        ttk.Label(general, text="约算：600 tokens 通常相当于约 450～600 个汉字，具体取决于标点、英文和格式。", foreground=self.colors["muted"]).grid(row=4, column=1, columnspan=3, sticky="w", pady=(4, 0))
        general.columnconfigure(0, weight=1); general.columnconfigure(2, weight=1); general.columnconfigure(3, weight=1)
        table = ttk.Frame(tab, style="Card.TFrame", padding=14); table.pack(fill="both", expand=True)
        for col, text in enumerate(("供应商", "Base URL", "模型名称", "API Key")): ttk.Label(table, text=text, style="Card.TLabel", font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=col, sticky="w", padx=5, pady=(0, 8))
        self.provider_fields = {}; self.provider_status_labels = {}
        env = self._read_env()
        labels = {"deepseek": "DeepSeek", "mimo": "小米 MiMo", "custom": "自定义接口"}
        for row, name in enumerate(("deepseek", "mimo", "custom"), start=1):
            provider = providers.setdefault(name, {}); key_env = provider.get("api_key_env", f"{name.upper()}_API_KEY")
            ttk.Label(table, text=labels[name], style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=5, pady=7)
            base = ttk.Entry(table); base.insert(0, provider.get("base_url", "")); base.grid(row=row, column=1, sticky="ew", padx=5, pady=7)
            model = ttk.Entry(table); model.insert(0, provider.get("model", "")); model.grid(row=row, column=2, sticky="ew", padx=5, pady=7)
            key = ttk.Entry(table, show="•"); key.grid(row=row, column=3, sticky="ew", padx=5, pady=7)
            key.insert(0, ""); self.provider_fields[name] = (base, model, key, key_env)
            saved_key = env.get(key_env, "")
            state = f"已保存 · ***{saved_key[-4:]}" if saved_key else "未设置"
            status_label = ttk.Label(table, text=state, style="Card.TLabel", foreground="#4a826d" if saved_key else self.colors["muted"])
            status_label.grid(row=row, column=4, sticky="w", padx=4); self.provider_status_labels[name] = status_label
        table.columnconfigure(1, weight=2); table.columnconfigure(2, weight=1); table.columnconfigure(3, weight=1)
        ttk.Button(tab, text="保存模型与 API", style="Accent.TButton", command=self.save_model_api).pack(anchor="e", pady=(12, 0))

    def save_model_api(self):
        latest = self._load_yaml(CONFIG, {}); llm = latest.setdefault("llm", {}); providers = llm.setdefault("providers", {})
        try:
            tuning = {name: {"temperature": float(fields[0].get()), "max_tokens": int(fields[1].get())} for name, fields in self.llm_tuning_fields.items()}
            if any(not 0 <= values["temperature"] <= 2 or values["max_tokens"] < 1 for values in tuning.values()): raise ValueError
        except ValueError:
            return messagebox.showerror("配置错误", "温度必须是数字，最大 Tokens 必须是整数。")
        llm["provider"] = self.current_provider.get(); llm.update(tuning)
        changed_keys = []
        for name, (base, model, key, key_env) in self.provider_fields.items():
            provider = providers.setdefault(name, {}); provider.update({"base_url": base.get().strip().rstrip("/"), "model": model.get().strip(), "api_key_env": key_env})
            value = key.get().strip()
            if value:
                self._save_env(key_env, value)
                stored = self._read_env().get(key_env, "")
                if stored != value: return messagebox.showerror("密钥保存失败", f"{key_env} 写入后校验不一致，请检查 .env 文件权限。")
                key.delete(0, "end"); changed_keys.append(name)
            stored = self._read_env().get(key_env, "")
            label = self.provider_status_labels.get(name)
            if label: label.configure(text=f"已保存 · ***{stored[-4:]}" if stored else "未设置", foreground="#4a826d" if stored else self.colors["muted"])
        self._atomic_write_yaml(CONFIG, latest); self.config = latest
        detail = f"\n本次更新密钥：{', '.join(changed_keys)}" if changed_keys else "\nAPI Key 输入框留空，已保留现有密钥。"
        messagebox.showinfo("保存成功", "模型与 API 配置已保存并校验。" + detail + "\n重启正在运行的助手后生效。")

    def _build_voice(self, tab):
        tts = self.config.setdefault("tts", {}); serve = self._load_yaml(HOME_AGENT_CONFIG, {}) if HOME_AGENT_CONFIG.exists() else {}; stt = serve.setdefault("stt", {})
        ttk.Button(tab, text="保存语音配置", style="Accent.TButton", command=self.save_voice).pack(side="bottom", anchor="e", pady=(12, 0))
        body = ttk.Frame(tab); body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Card.TFrame", padding=16); left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(body, style="Card.TFrame", padding=16); right.pack(side="right", fill="both", expand=True, padx=(8, 0))
        ttk.Label(left, text="语音回复 · GPT-SoVITS / SVC", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.tts_vars = {"enabled": tk.BooleanVar(value=tts.get("enabled", True)), "auto_start": tk.BooleanVar(value=tts.get("auto_start", True)), "play_audio": tk.BooleanVar(value=tts.get("play_audio", True))}
        ttk.Checkbutton(left, text="启用语音回复", variable=self.tts_vars["enabled"]).grid(row=1, column=0, sticky="w"); ttk.Checkbutton(left, text="自动启动服务", variable=self.tts_vars["auto_start"]).grid(row=1, column=1, sticky="w"); ttk.Checkbutton(left, text="自动播放", variable=self.tts_vars["play_audio"]).grid(row=2, column=0, sticky="w", pady=(0, 8))
        self.tts_fields = {}; tts_specs = (("url", "语音接口"), ("health_url", "检测接口"), ("start_command", "启动批处理"), ("startup_timeout_seconds", "启动等待秒数"), ("model", "模型轮次"), ("reference", "参考音频"))
        for row, (key, label) in enumerate(tts_specs, start=3):
            ttk.Label(left, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=6); entry = ttk.Entry(left); entry.insert(0, tts.get(key, "")); entry.grid(row=row, column=1, sticky="ew", pady=6); self.tts_fields[key] = entry
        left.columnconfigure(1, weight=1)
        ttk.Label(right, text="语音输入 · STT", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.stt_fields = {}; stt_specs = (("mode", "识别方式"), ("api_url", "API 地址"), ("model", "模型名称"), ("language", "语言"), ("local_python", "本地 Python"), ("local_model", "本地模型目录"))
        for row, (key, label) in enumerate(stt_specs, start=1):
            ttk.Label(right, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=6)
            if key == "mode": entry = ttk.Combobox(right, values=["api", "faster_whisper"], state="readonly"); entry.set(stt.get(key, "api"))
            else: entry = ttk.Entry(right); entry.insert(0, stt.get(key, ""))
            entry.grid(row=row, column=1, sticky="ew", pady=6); self.stt_fields[key] = entry
        ttk.Label(right, text="STT API Key", style="Card.TLabel").grid(row=7, column=0, sticky="w", pady=6); self.stt_key = ttk.Entry(right, show="•"); self.stt_key.grid(row=7, column=1, sticky="ew", pady=6)
        status = "已保存" if self._read_env().get(stt.get("api_key_env", "STT_API_KEY")) else "未设置"; ttk.Label(right, text=f"密钥状态：{status}（留空保留）", style="Card.TLabel", foreground=self.colors["muted"]).grid(row=8, column=1, sticky="w")
        right.columnconfigure(1, weight=1)

    def save_voice(self):
        latest = self._load_yaml(CONFIG, {}); tts = latest.setdefault("tts", {})
        for key, var in self.tts_vars.items(): tts[key] = var.get()
        for key, widget in self.tts_fields.items():
            value = widget.get().strip(); tts[key] = int(value or 0) if key == "startup_timeout_seconds" else value
        self._atomic_write_yaml(CONFIG, latest); self.config = latest
        serve = self._load_yaml(HOME_AGENT_CONFIG, {}) if HOME_AGENT_CONFIG.exists() else {}; stt = serve.setdefault("stt", {})
        for key, widget in self.stt_fields.items(): stt[key] = widget.get().strip()
        stt.setdefault("api_key_env", "STT_API_KEY"); self._atomic_write_yaml(HOME_AGENT_CONFIG, serve)
        if self.stt_key.get().strip(): self._save_env(stt["api_key_env"], self.stt_key.get().strip()); self.stt_key.delete(0, "end")
        messagebox.showinfo("保存成功", "语音回复和语音识别配置已保存。")

    def _build_tools_maintenance(self, tab):
        home = self._load_yaml(HOME_AGENT_CONFIG, {}) if HOME_AGENT_CONFIG.exists() else {}
        self.gui_vision_enabled = tk.BooleanVar(value=home.get("vision_mcp", {}).get("gui_enabled", False))
        vision_card = ttk.Frame(tab, style="Card.TFrame", padding=14); vision_card.pack(fill="x", pady=(0, 12))
        ttk.Label(vision_card, text="图像 GUI 识别", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ttk.Checkbutton(vision_card, text="启用 GUI-Actor 图像识别（会占用显存）", variable=self.gui_vision_enabled).pack(side="left", padx=20)
        ttk.Label(vision_card, text="关闭时网页使用 DOM/文本 Agent；保存后立即释放视觉模型显存", style="Card.TLabel", foreground=self.colors["muted"]).pack(side="left")
        top = ttk.Frame(tab); top.pack(fill="both", expand=True)
        software = ttk.Frame(top, style="Card.TFrame", padding=14); software.pack(side="left", fill="both", expand=True, padx=(0, 7))
        mcp = ttk.Frame(top, style="Card.TFrame", padding=14); mcp.pack(side="right", fill="both", expand=True, padx=(7, 0))
        ttk.Label(software, text="软件目录映射", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(software, text="Agent 可通过软件名称找到可执行文件或目录", style="Card.TLabel", foreground=self.colors["muted"]).pack(anchor="w", pady=(2, 8))
        self.software_tree = ttk.Treeview(software, columns=("name", "path"), show="headings", height=7)
        self.software_tree.heading("name", text="软件名称"); self.software_tree.heading("path", text="程序/目录路径")
        self.software_tree.column("name", width=110); self.software_tree.column("path", width=330)
        self.software_tree.pack(fill="both", expand=True)
        for name, path in home.get("computer_control", {}).get("applications", {}).items(): self.software_tree.insert("", "end", values=(name, path))
        row = ttk.Frame(software, style="Card.TFrame"); row.pack(fill="x", pady=(8, 0))
        self.software_name = ttk.Entry(row, width=14); self.software_name.pack(side="left", padx=(0, 5))
        self.software_path = ttk.Entry(row); self.software_path.pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(row, text="浏览", command=self._browse_software).pack(side="left", padx=5)
        ttk.Button(row, text="添加/更新", command=self._upsert_software).pack(side="left", padx=5)
        ttk.Button(row, text="删除", command=lambda: self._delete_tree_selection(self.software_tree)).pack(side="left")
        self.software_tree.bind("<<TreeviewSelect>>", lambda _: self._load_software_selection())

        ttk.Label(mcp, text="MCP 管理", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(mcp, text="支持 HTTP 地址或 stdio 命令，保存后同步 WorkBuddy 与 Codex", style="Card.TLabel", foreground=self.colors["muted"]).pack(anchor="w", pady=(2, 8))
        self.mcp_tree = ttk.Treeview(mcp, columns=("name", "type", "target"), show="headings", height=7)
        for key, title, width in (("name", "名称", 105), ("type", "类型", 65), ("target", "地址或命令", 290)):
            self.mcp_tree.heading(key, text=title); self.mcp_tree.column(key, width=width)
        self.mcp_tree.pack(fill="both", expand=True)
        loaded_mcp = self._load_mcp_servers(); self.initial_mcp_names = set(loaded_mcp)
        for name, item in loaded_mcp.items():
            target = item.get("url", "") if item.get("url") else " ".join([str(item.get("command", "")), *map(str, item.get("args", []))]).strip()
            self.mcp_tree.insert("", "end", values=(name, "http" if item.get("url") else "stdio", target))
        mrow = ttk.Frame(mcp, style="Card.TFrame"); mrow.pack(fill="x", pady=(8, 0))
        self.mcp_name = ttk.Entry(mrow, width=12); self.mcp_name.pack(side="left", padx=(0, 5))
        self.mcp_type = ttk.Combobox(mrow, values=["http", "stdio"], state="readonly", width=7); self.mcp_type.set("http"); self.mcp_type.pack(side="left", padx=5)
        self.mcp_target = ttk.Entry(mrow); self.mcp_target.pack(side="left", fill="x", expand=True, padx=5)
        ttk.Button(mrow, text="添加/更新", command=self._upsert_mcp).pack(side="left", padx=5)
        ttk.Button(mrow, text="删除", command=lambda: self._delete_tree_selection(self.mcp_tree)).pack(side="left")
        self.mcp_tree.bind("<<TreeviewSelect>>", lambda _: self._load_mcp_selection())

        maintenance = ttk.Frame(tab, style="Card.TFrame", padding=14); maintenance.pack(fill="x", pady=(14, 0))
        ttk.Label(maintenance, text="模型上下文自动清理", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, columnspan=6, sticky="w")
        root_cleanup = self.config.get("context_cleanup", {}); home_cleanup = home.get("context_maintenance", {})
        self.maintenance_vars = {
            "home_enabled": tk.BooleanVar(value=home_cleanup.get("enabled", True)),
            "live_enabled": tk.BooleanVar(value=root_cleanup.get("live_enabled", True)),
            "gui_vision_enabled": self.gui_vision_enabled,
        }
        ttk.Checkbutton(maintenance, text="启用家庭上下文每日压缩", variable=self.maintenance_vars["home_enabled"]).grid(row=1, column=0, sticky="w", pady=(12, 5))
        ttk.Label(maintenance, text="家庭压缩时间", style="Card.TLabel").grid(row=1, column=1, sticky="e", padx=(15, 5))
        self.home_cleanup_time = ttk.Entry(maintenance, width=9); self.home_cleanup_time.insert(0, home_cleanup.get("time", "03:00")); self.home_cleanup_time.grid(row=1, column=2, sticky="w")
        ttk.Checkbutton(maintenance, text="启用直播上下文过期清理", variable=self.maintenance_vars["live_enabled"]).grid(row=2, column=0, sticky="w", pady=5)
        ttk.Label(maintenance, text="保留时长（分钟）", style="Card.TLabel").grid(row=2, column=1, sticky="e", padx=(15, 5))
        self.live_context_minutes = ttk.Spinbox(maintenance, from_=10, to=1440, width=8); self.live_context_minutes.set(root_cleanup.get("live_max_age_minutes", 120)); self.live_context_minutes.grid(row=2, column=2, sticky="w")
        ttk.Label(maintenance, text="直播助手运行时会持续清理超过该时长的短期对话；长期记忆不受影响。", style="Card.TLabel", foreground=self.colors["muted"]).grid(row=2, column=3, sticky="w", padx=14)
        ttk.Button(tab, text="保存工具与维护设置", style="Accent.TButton", command=self.save_tools_maintenance).pack(anchor="e", pady=(12, 0))

    def _browse_software(self):
        path = filedialog.askopenfilename(title="选择程序") or filedialog.askdirectory(title="选择软件目录")
        if path: self.software_path.delete(0, "end"); self.software_path.insert(0, path)

    @staticmethod
    def _delete_tree_selection(tree):
        for item in tree.selection(): tree.delete(item)

    def _upsert_tree(self, tree, values):
        name = str(values[0]).strip()
        if not name: return
        for item in tree.get_children():
            if str(tree.item(item, "values")[0]).strip() == name: tree.item(item, values=values); return
        tree.insert("", "end", values=values)

    def _upsert_software(self): self._upsert_tree(self.software_tree, (self.software_name.get().strip(), self.software_path.get().strip()))
    def _upsert_mcp(self): self._upsert_tree(self.mcp_tree, (self.mcp_name.get().strip(), self.mcp_type.get(), self.mcp_target.get().strip()))

    def _load_software_selection(self):
        selected = self.software_tree.selection()
        if selected:
            name, path = self.software_tree.item(selected[0], "values"); self.software_name.delete(0, "end"); self.software_name.insert(0, name); self.software_path.delete(0, "end"); self.software_path.insert(0, path)

    def _load_mcp_selection(self):
        selected = self.mcp_tree.selection()
        if selected:
            name, kind, target = self.mcp_tree.item(selected[0], "values"); self.mcp_name.delete(0, "end"); self.mcp_name.insert(0, name); self.mcp_type.set(kind); self.mcp_target.delete(0, "end"); self.mcp_target.insert(0, target)

    def _load_mcp_servers(self):
        if WORKBUDDY_MCP.exists():
            try: return (json.loads(WORKBUDDY_MCP.read_text(encoding="utf-8")) or {}).get("mcpServers", {})
            except (OSError, json.JSONDecodeError): pass
        if MCP_CONFIG.exists():
            try: return (yaml.safe_load(MCP_CONFIG.read_text(encoding="utf-8")) or {}).get("mcpServers", {})
            except (OSError, yaml.YAMLError): pass
        return {}

    def save_tools_maintenance(self):
        home = self._load_yaml(HOME_AGENT_CONFIG, {}) if HOME_AGENT_CONFIG.exists() else {}
        applications = {str(self.software_tree.item(i, "values")[0]): str(self.software_tree.item(i, "values")[1]) for i in self.software_tree.get_children()}
        home.setdefault("computer_control", {})["applications"] = applications
        maintenance = home.setdefault("context_maintenance", {}); maintenance["enabled"] = self.maintenance_vars["home_enabled"].get(); maintenance["time"] = self.home_cleanup_time.get().strip() or "03:00"
        vision = home.setdefault("vision_mcp", {}); gui_enabled = self.maintenance_vars["gui_vision_enabled"].get()
        vision["enabled"] = True; vision["gui_enabled"] = gui_enabled; vision["preload_model"] = gui_enabled
        self._atomic_write_yaml(HOME_AGENT_CONFIG, home)
        latest = self._load_yaml(CONFIG, {}); cleanup = latest.setdefault("context_cleanup", {})
        try: minutes = max(10, int(self.live_context_minutes.get()))
        except ValueError: return messagebox.showerror("配置错误", "直播上下文保留分钟数必须是整数。")
        cleanup.update({"live_enabled": self.maintenance_vars["live_enabled"].get(), "live_max_age_minutes": minutes, "check_interval_seconds": 60})
        self._atomic_write_yaml(CONFIG, latest); self.config = latest
        servers = {}
        for item in self.mcp_tree.get_children():
            name, kind, target = map(str, self.mcp_tree.item(item, "values"))
            if kind == "http": servers[name] = {"url": target, "disabled": False}
            else:
                parts = target.split(); servers[name] = {"command": parts[0] if parts else "", "args": parts[1:], "disabled": False}
        self._atomic_write_yaml(MCP_CONFIG, {"mcpServers": servers})
        WORKBUDDY_MCP.parent.mkdir(parents=True, exist_ok=True); self._atomic_write_json(WORKBUDDY_MCP, {"mcpServers": servers})
        removed = self.initial_mcp_names - set(servers); self.initial_mcp_names = set(servers)
        self.set_status("配置已安全保存，正在后台同步 Codex MCP…")
        def worker():
            sync_errors = self._sync_codex_mcp(servers, removed)
            if not gui_enabled: self._stop_vision_service()
            def finish():
                message = "软件映射、MCP 和上下文清理设置已保存。重启对应助手后生效。"
                if sync_errors: message += "\n\nCodex MCP 同步提示：\n" + "\n".join(sync_errors[:5])
                self.set_status("工具与维护设置已保存" if not sync_errors else "设置已保存，部分 MCP 同步失败", error=bool(sync_errors))
                messagebox.showinfo("保存完成", message, parent=self.root)
            try: self.root.after(0, finish)
            except tk.TclError: pass
        threading.Thread(target=worker, daemon=True, name="character-manager-mcp-sync").start()

    @staticmethod
    def _stop_vision_service():
        """Stop the current model-hosting process so CUDA memory is released immediately."""
        try:
            query = "(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue).OwningProcess"
            found = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=8)
            pid = found.stdout.strip().splitlines()[0].strip() if found.stdout.strip() else ""
            if pid.isdigit(): subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=8)
        except Exception: pass

    @staticmethod
    def _sync_codex_mcp(servers, removed=()):
        errors = []
        for name in removed:
            try: subprocess.run(["codex", "mcp", "remove", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            except Exception as exc: errors.append(f"{name}: {exc}")
        for name, item in servers.items():
            try:
                subprocess.run(["codex", "mcp", "remove", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                command = ["codex", "mcp", "add", name]
                if item.get("url"): command += ["--url", item["url"]]
                else: command += ["--", item.get("command", ""), *item.get("args", [])]
                result = subprocess.run(command, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
                if result.returncode: errors.append(f"{name}: {(result.stderr or result.stdout).strip()[-200:]}")
            except Exception as exc: errors.append(f"{name}: {exc}")
        return errors

    def _build_images(self, tab):
        left = ttk.Frame(tab, width=350); left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(tab, style="Card.TFrame", padding=16, width=470); right.pack(side="right", fill="both", expand=True, padx=(16, 0)); right.pack_propagate(False)
        self.image_list = tk.Listbox(left, font=("Microsoft YaHei UI", 10), activestyle="none")
        self.image_list.pack(fill="both", expand=True); self.image_list.bind("<<ListboxSelect>>", lambda _: self._show_image_info())
        ttk.Button(left, text="＋ 添加角色图片", command=self.add_image).pack(anchor="w", pady=(10, 0))
        ttk.Label(right, text="图片预览", style="Card.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        # 底部控件先布局，确保窗口变矮或图片尺寸较大时操作按钮始终可见。
        actions = ttk.Frame(right, style="Card.TFrame"); actions.pack(side="bottom", fill="x")
        ttk.Button(actions, text="设为主形象", command=self.set_primary).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(actions, text="删除图片", command=self.delete_image).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(actions, text="打开文件夹", command=lambda: __import__("os").startfile(IMAGES)).pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.image_info = ttk.Label(right, text="请选择图片", style="Card.TLabel", wraplength=420, foreground="#52615f"); self.image_info.pack(side="bottom", anchor="w", fill="x", pady=(10, 10))
        preview_box = tk.Frame(right, bg="#ffffff", height=400)
        preview_box.pack(side="top", fill="both", expand=True, pady=(10, 0)); preview_box.pack_propagate(False)
        self.image_preview = tk.Label(preview_box, text="从左侧选择一张图片", bg="#ffffff", fg=self.colors["muted"], relief="flat", bd=0, anchor="center")
        self.image_preview.pack(fill="both", expand=True)
        self.image_preview_photo = None
        self.refresh_images()

    def _build_api(self, tab):
        cfg = self.config.get("image_generation", {}); self.api_fields = {}
        for row, (key, label) in enumerate((("mode", "接口模式"), ("base_url", "Base URL"), ("model", "模型名称"), ("size", "输出尺寸"))):
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=8)
            if key == "mode": widget = ttk.Combobox(tab, values=["images", "chat_multimodal"], state="readonly")
            else: widget = ttk.Entry(tab)
            widget.insert(0, str(cfg.get(key, ""))); widget.grid(row=row, column=1, sticky="ew", pady=8); self.api_fields[key] = widget
        ttk.Label(tab, text="API Key").grid(row=4, column=0, sticky="w", padx=(0, 14), pady=8)
        self.image_key = ttk.Entry(tab, show="•"); self.image_key.grid(row=4, column=1, sticky="ew", pady=8)
        ttk.Label(tab, text="密钥保存在主项目 .env，角色图片 Skill 与家庭 Agent 会共同使用。", foreground="#52615f").grid(row=5, column=1, sticky="w")
        ttk.Button(tab, text="保存图片 API", command=self.save_api).grid(row=6, column=1, sticky="e", pady=18)
        tab.columnconfigure(1, weight=1)

    def _load_manifest(self):
        if not MANIFEST.exists(): return {"primary": None, "images": []}
        try: return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return {"primary": None, "images": []}

    def _save_manifest(self):
        self._atomic_write_json(MANIFEST, self.manifest)

    def save_identity(self, notify=True):
        f = self.identity_fields
        live_names = [x.strip() for x in f["live_usernames"].get().replace("，", ",").split(",") if x.strip()]
        aliases = [x.strip() for x in f["user_aliases"].get().replace("，", ",").split(",") if x.strip()]
        data = {"character": {k: f[k].get().strip() for k in ("name", "identity", "gender", "visual_age", "personality", "relationship_to_user", "user_title")},
                "user": {"id": "owner", "name": f["user_name"].get().strip() or "主人", "aliases": aliases, "live_usernames": live_names}, "notes": self.notes.get("1.0", "end").strip()}
        self._atomic_write_yaml(IDENTITY, data)
        self._atomic_write_text(PROFILE, self.profile.get("1.0", "end").strip() + "\n")
        self.identity = data
        if hasattr(self, "identity_save_state"):
            self.identity_save_state.configure(text=f"已保存 · {datetime.now():%H:%M:%S}", foreground="#4a826d")
        if notify: messagebox.showinfo("保存成功", "角色身份已保存，直播助手和家庭 Agent 下次对话都会读取。")

    def refresh_images(self, select_id=None):
        self.image_list.delete(0, "end")
        selected_index = None
        for index, item in enumerate(self.manifest.get("images", [])):
            mark = "★ " if item.get("id") == self.manifest.get("primary") else "   "
            self.image_list.insert("end", mark + (item.get("label") or item.get("filename", "")))
            if select_id and item.get("id") == select_id: selected_index = index
        if selected_index is not None:
            self.image_list.selection_set(selected_index); self.image_list.see(selected_index); self._show_image_info()

    def selected_image(self):
        selection = self.image_list.curselection()
        return self.manifest.get("images", [])[selection[0]] if selection else None

    def _show_image_info(self):
        item = self.selected_image()
        self.image_info.configure(text=(f"名称：{item.get('label')}\n文件：{item.get('filename')}\n标签：{'、'.join(item.get('tags', []))}" if item else "请选择图片"))
        self.image_preview_photo = None
        if not item:
            self.image_preview.configure(image="", text="从左侧选择一张图片")
            return
        path = Path(str(item.get("filename") or item.get("path") or ""))
        if not path.is_absolute(): path = IMAGES / path
        if not path.is_file():
            self.image_preview.configure(image="", text=f"图片文件不存在\n{path.name}")
            return
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
                image.thumbnail((420, 390), Image.Resampling.LANCZOS)
            self.image_preview_photo = ImageTk.PhotoImage(image, master=self.root)
            self.image_preview.configure(image=self.image_preview_photo, text="")
        except (OSError, ValueError) as exc:
            self.image_preview.configure(image="", text=f"无法预览此图片\n{exc}")

    def add_image(self):
        source = filedialog.askopenfilename(filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif")])
        if not source: return
        source = Path(source); image_id = uuid.uuid4().hex; filename = image_id + source.suffix.lower()
        shutil.copy2(source, IMAGES / filename)
        item = {"id": image_id, "filename": filename, "original_name": source.name, "label": source.stem, "tags": [], "created_at": datetime.now().isoformat(timespec="seconds")}
        self.manifest.setdefault("images", []).append(item)
        if not self.manifest.get("primary"): self.manifest["primary"] = image_id
        self._save_manifest(); self.refresh_images(image_id)

    def set_primary(self):
        item = self.selected_image()
        if not item: return messagebox.showwarning("未选择", "请先选择一张图片。")
        self.manifest["primary"] = item["id"]; self._save_manifest(); self.refresh_images(item["id"])

    def delete_image(self):
        item = self.selected_image()
        if not item or not messagebox.askyesno("确认删除", "确定删除所选角色图片吗？"): return
        path = IMAGES / Path(item.get("filename", "")).name
        if path.exists(): path.unlink()
        self.manifest["images"] = [x for x in self.manifest["images"] if x.get("id") != item.get("id")]
        if self.manifest.get("primary") == item.get("id"): self.manifest["primary"] = self.manifest["images"][0]["id"] if self.manifest["images"] else None
        self._save_manifest(); self.refresh_images(); self.image_info.configure(text="请选择图片")
        self.image_preview_photo = None; self.image_preview.configure(image="", text="从左侧选择一张图片")

    def save_api(self):
        cfg = self.config.setdefault("image_generation", {})
        for key, widget in self.api_fields.items(): cfg[key] = widget.get().strip()
        cfg.setdefault("api_key_env", "IMAGE_API_KEY")
        self._atomic_write_yaml(CONFIG, self.config)
        key = self.image_key.get().strip()
        if key: self._save_env("IMAGE_API_KEY", key); self.image_key.delete(0, "end")
        messagebox.showinfo("保存成功", "图片 API 配置已保存。")

    @staticmethod
    def _save_env(key, value):
        path = ROOT / ".env"; lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        for i, line in enumerate(lines):
            if line.startswith(key + "="): lines[i] = f"{key}={value}"; break
        else: lines.append(f"{key}={value}")
        CharacterManager._atomic_write_text(path, "\n".join(lines) + "\n")

    @staticmethod
    def _read_env():
        result = {}
        if not ENV_FILE.exists(): return result
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1); result[key.strip()] = value.strip()
        return result

    def close(self):
        try:
            if getattr(self, "_doc_save_job", None):
                try: self.root.after_cancel(self._doc_save_job)
                except tk.TclError: pass
            self.save_document(False)
            self.save_identity(False)
        except Exception as exc:
            if not messagebox.askyesno("保存失败", f"关闭前保存角色身份或规则失败：\n{exc}\n\n仍然关闭吗？"): return
        if self._mutex:
            try: ctypes.windll.kernel32.CloseHandle(self._mutex)
            except Exception: pass
            self._mutex = None
        self.root.destroy()

    def run(self): self.root.mainloop()


if __name__ == "__main__": CharacterManager().run()
