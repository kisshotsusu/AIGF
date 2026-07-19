# AI 直播工具箱

一套围绕「B站 AI 直播弹幕助手」的本地 AI 工具集合，包含直播弹幕回复、角色管理、家庭桌宠、
GUI 网页/桌面视觉控制、本地语音识别、长期记忆与子技能。所有组件**共享同一份 Python 环境**与
**同一份工作区记忆**，互相联动。

## 目录总览

| 目录 / 文件 | 功能 | 启动方式 |
|------|------|------|
| `modules/live/`（根目录 `main.py` / `manager.py` 为兼容入口） | **B站 AI 直播弹幕助手**（主程序 + 可视化管理后台 :9888） | `启动管理页面.bat` |
| `CharacterManager/` | **角色管理器**（Tkinter GUI，管理身份/记忆/人格/模型/语音/图片） | `启动角色管理器.bat` |
| `HomeAgent/` | **家庭桌宠**（透明悬浮窗，语音对话、操控电脑、调度任务） | `HomeAgent\启动家庭Agent.bat` |
| `Vision/` | **GUI 网页/桌面控制 Agent**（本地 GUI-Actor-2B 视觉 grounding，MCP 服务） | HTTP :8765（被 HomeAgent 托管） |
| `Sound/` | **语音识别**（本地 SenseVoiceSmall / FunASR，MCP 工具） | stdio `sound-asr` |
| `LongTermMemory/` | **长期记忆库**（SQLite `memory.db`，跨场景记忆） | 被主程序/HomeAgent 读写 |
| `Skill/` | **子技能**：角色出图、家庭定时任务、MiMo 唱歌 | 被 HomeAgent 按需调用 |
| `workspace/` | 共享工作区：SOUL.md / RULES.md / HOME.md / memory/ 等人格与每日记忆文档 | — |
| `audio/` | TTS 生成的回复语音（.wav） | — |
| `.venv/` | **共享 Python 环境**（Python 3.12 + torch cu128） | 所有组件共用 |
| `config.yaml` / `.env` | 主程序配置与密钥 | — |

---

## 共享环境（重要）

所有子项目（Vision、Sound、主程序、HomeAgent、CharacterManager）**都使用同一个 venv**：
`.venv\Scripts\python.exe`（从项目根目录运行，Python 3.12）。

该环境已安装：
- 直播/角色：`aiohttp`、`PyYAML`、`python-dotenv`、`Pillow`、`sounddevice`、`numpy`
- Vision（GUI 控制）：`torch` **cu128**（适配 RTX 5070 Ti 的 Blackwell sm_120）、`transformers` 4.51.3、`qwen-vl-utils`、`playwright`
- Sound（语音识别）：`funasr`、`modelscope`、`librosa`、`soundfile`、`mcp`
- 通用：`mcp`（FastMCP）

> 不再为每个子目录单独建 venv（早期版本曾拆成 `Version/.venv`、`Sound/.venv`，已合并清理）。
> 重装依赖统一用父 `.venv`；CUDA 版 torch 必须走 cu128（`download.pytorch.org/whl/cu128`），
> 因 5070 Ti 是 Blackwell 架构，cu124 会报 `no kernel image is available`。

---

## 0. 给别人部署（共享环境 + 模型一键拉起）

把本目录整体拷贝/打包给他人时，**无需带 `.venv/`（约 5GB+）和 `models/`（约 5.5GB）**——
这两块用脚本一键重建即可。分享时建议包含：项目源码、`set_env.bat`、`down_model.bat`、`requirements.txt`、
以及各子目录里的 `download_model.py`（已就绪）。

**接收方只需两步：**

1. **双击 `set_env.bat`** —— 自动检测 Python、创建 `.venv`、安装全部依赖：
   - PyTorch 走官方 **cu128** 源（适配 RTX 50 系 Blackwell；普通 pip 会装成 CPU 版）；
   - 其余依赖按 `requirements.txt` 安装；
   - 安装 Playwright Chromium 浏览器内核。
   - 若缺少 `Vision/GUI-Actor` 源码会自动告警（需随包保留或 `git clone`）。
2. **双击 `down_model.bat`** —— 调两个 `download_model.py` 补全模型权重：
   - `Vision/models/GUI-Actor-2B-Qwen2-VL`（约 4.5 GB，curl 断点续传）；
   - `Sound/models/SenseVoiceSmall`（约 1 GB，走 ModelScope）。

> 注：`.bat` 内部全英文（ASCII），避免中文 Windows 代码页把脚本读成乱码；
> 中文路径靠 `%~dp0` 运行时变量传入，不受影响。

---

## 1. 主程序：B站 AI 直播弹幕助手

读取直播弹幕和进房事件、自动欢迎、DeepSeek/MiMo 智能短回复、本地 HTTP 语音生成与播放、按天保存工作区记忆。

### 三套程序共用关系
- `启动管理页面.bat`：直播控制台，只负责直播间、触发回复、运行状态和消息日志。
- `启动角色管理器.bat`：统一管理角色身份、共享记忆、人格规则、模型 API、语音服务、角色图片和图片 API。
- `HomeAgent\启动家庭Agent.bat`：家庭桌宠模式。

三套程序共同读取 `workspace`。长期记忆保存在 `workspace\memory`；家庭桌宠还会读取 `logs\messages.jsonl`
中最近成功的直播对话，以保持跨场景连续性。

### 快速开始
1. 复制 `config.example.yaml` 为 `config.yaml`，填写 `app.room_id`。
2. 复制 `.env.example` 为 `.env`，填入模型 Key；需要真正发送弹幕时再填 B站 Cookie。
3. 双击 `run.bat`。首次运行会自动创建虚拟环境并安装依赖。
4. 只需要语音回复时，保持 `send_danmaku: false`。此时程序会监听弹幕、生成 AI 回复和播放语音，但永远不会发送回复弹幕。

只有同时满足 `send_danmaku: true` 和 `dry_run: false` 时，程序才会发出真实弹幕。

配置检查：`.venv\Scripts\python.exe main.py --config config.yaml --check`

### 可视化管理页面
双击 `启动管理页面.bat`，浏览器会自动打开 `http://127.0.0.1:9888`。页面可管理全部常用配置、API 密钥、
人格文档，测试 AI/SVC，并启动或停止直播助手。

### B站登录信息
发送弹幕需要当前主播账号浏览器 Cookie 中的 `SESSDATA`、`bili_jct`，通常也建议保留 `buvid3`。
把它们放在 `.env` 的 `BILIBILI_COOKIE` 中，不要提交或分享该文件。Cookie 过期后需要重新获取。
读取弹幕不强制要求登录；发送弹幕会受到账号状态、频率、房间权限和平台风控限制。请遵守 B站规则，避免刷屏。

### 智能回复触发
- `mention`：弹幕包含 `reply.bot_names` 中任一名称。
- `prefix`：弹幕以 `reply.prefixes` 中任一前缀开头。
- `all`：每条弹幕都可能回复（不推荐，容易刷屏并产生费用）。

### 9879 语音服务适配
程序已适配 `E:\Doc\SVC\启动推理.bat`：如果 `9879` 服务没有运行，会自动启动该批处理并等待 GPT-SoVITS
模型加载完成。实际请求发送到 `http://127.0.0.1:9879/api/tts`。
程序会通过 `/api/options` 自动选择最新训练模型和默认参考音频，请求大致如下：
```json
{"text":"回复内容","model":"训练轮次","reference":"参考音频路径","speed":1.0}
```
如果想固定模型或音色参考，可在 `tts.model` 和 `tts.reference` 中填写对应值。
若服务返回 JSON：base64 音频设 `response_type: json_base64` 填 `response_field`；音频 URL 设
`response_type: json_url` 填 `response_field`。
若以后换成其他本机接口，可关闭 `svc_auto_options` 并修改 `tts.request_json`（其中 `{text}`、`{speaker}` 会被替换）。

### 工作区
`workspace/SOUL.md` 定义性格，`RULES.md` 定义注意事项和回复规则，`ABILITIES.md` 定义能力边界，
`memory/` 保存每日互动。这些文档在每次智能回复前动态加载。

### 安全提示
默认 `send_danmaku: false` 和 `dry_run: true` 是双重保护。建议限制模型 Key 额度、定期清理记忆，
并在正式直播前用测试房间验证。AI 回复仍可能出错，主播应保留人工停用手段（关闭窗口或 Ctrl+C）。

---

## 2. Vision — GUI 网页/桌面控制 Agent（MCP）

本地 **GUI-Actor-2B（Qwen2-VL）** 视觉模型，看截图定位界面元素并点击/输入/滚动。封装为 MCP 服务，
可被 WorkBuddy、HomeAgent 用自然语言调用。详见 `Vision/README.md`。

- MCP 服务名 `vision-gui`，默认 **streamable-http 常驻于 `127.0.0.1:8765`**（由 HomeAgent 自动拉起并预加载模型）。
- 工具：`navigate` / `click` / `type_text` / `scroll` / `screenshot` / `get_url` / `wait` / `play_video`
  以及桌面控制 `desktop_screenshot` / `desktop_click` / `desktop_type_text` / `desktop_scroll` / `desktop_hotkey`。
- 模型：`Vision/models/GUI-Actor-2B-Qwen2-VL`。
- 能力边界：普通网页/App（ScreenSpot-v2 ~88.6）足够；专业软件高分辨率界面（ScreenSpot-Pro ~36.7）偏弱。

---

## 3. Sound — 语音识别（MCP）

本地 **SenseVoiceSmall / FunASR**，支持中/英/日/韩/粤多语种、带标点与情绪标签。详见 `Sound/README.md`。

- MCP 工具名 `sound-asr`（stdio），工具：`transcribe_file(path, language)`、`record_and_transcribe(duration, language)`。
- 模型：`Sound/models/SenseVoiceSmall`。
- 已处理中文路径与 mp3 兼容性（模型自动复制到 ASCII 临时目录加载；音频用 librosa 加载）。

---

## 4. CharacterManager — 角色管理器

基于 Tkinter 的可视化「AI 角色工作台」（`启动角色管理器.bat`）。统一管理：
- 角色身份（`workspace/IDENTITY.yaml`、`CHARACTER.md`）
- 共享记忆（`workspace/memory`）
- 人格规则文档（`SOUL.md` / `RULES.md` / `LIVE_RULES.md` / `HOME_RULES.md` / `ABILITIES.md` / `HOME.md`）
- 模型 API、语音服务（SVC）、角色图片与图片 API
- 角色图片库（`workspace/character_images`，含 `manifest.json`）

---

## 5. HomeAgent — 家庭桌宠

透明悬浮窗式家庭 AI 桌宠（`HomeAgent\启动家庭Agent.bat`），详见 `HomeAgent\使用说明.txt`。

- 单击展开/收起对话框，拖动改位置；语音按钮录音识别。
- **语音识别**可配置两种：① `api`（OpenAI 兼容接口，Key 在 `.env` 的 `STT_API_KEY`）；
  ② `faster_whisper`（本地，见 `HomeAgent\transcribe_local.py`，走 CUDA）。
- 回复后自动调用 SVC 服务播音；可说"记住…""查找关于…的记忆""生成一张角色在家里的图片"等。
- **操控电脑**：连接 `vision-gui` MCP（HTTP :8765，预加载模型），命中 `操作电脑/点击屏幕/看屏幕` 等
  关键词时由视觉模型接管桌面/浏览器。
- **Codex CLI / MCP**：可启用，按 `auto`/`always`/`manual` 模式转交命令行与工具调用。
- 配置：`HomeAgent/config.yaml`（助手名、麦克风、STT、vision_mcp、codex_cli、computer_control 等）。
- 录音存于 `HomeAgent/recordings`；本程序**不会发送直播弹幕**。

---

## 6. LongTermMemory — 长期记忆库

SQLite 数据库 `LongTermMemory/memory.db`，表 `memories` 字段：
`id, created_at, user_id, scene, category, tags, summary, detail, importance, privacy, source`。
主程序与 HomeAgent 跨场景读写，实现"直播对话 ↔ 家庭对话"的记忆延续。

---

## 7. Skill — 子技能

`Skill/` 下放置可复用技能（每个含 `SKILL.md` / `agents/` / `scripts/`）：
- `ai-live-character-image`：生成角色在直播/家庭场景的图片。
- `schedule-home-task`：家庭定时任务。
- `sing-with-mimo`：用 MiMo 唱歌。

HomeAgent 通过 `HomeAgent/config.yaml` 的 `agent.skill_root`（默认 `Skill`）按需调用。

---

## 联动架构（一句话）

`HomeAgent 桌宠` ⇄ 语音(`Sound`/`faster_whisper`) → 模型决策 → 操控电脑(`Vision` vision-gui MCP)
⇄ 长期记忆(`LongTermMemory`) ⇄ 角色人格(`CharacterManager` / `workspace`)；
主程序`直播助手`独立运行弹幕回复与 TTS，并共享同一份 `workspace` 记忆与 `.venv` 环境。

## 安全提示（全局）
默认 `send_danmaku: false` + `dry_run: true` 是弹幕双重保护；HomeAgent 的 `computer_control` 也建议保留
`confirm_before_action: true`。AI 仍可能出错，请保留人工停用手段。
