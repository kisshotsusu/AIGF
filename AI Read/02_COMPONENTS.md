# 组件与目录

| 路径 | 职责 | 主要入口 |
|---|---|---|
| `main.py` | 直播助手 CLI 入口、配置检查 | `python main.py --config config.yaml` |
| `src/ai_live_assistant/` | 直播业务核心 | `app.py`、`bilibili.py`、`llm.py`、`tts.py`、`workspace.py` |
| `manager.py` + `web/` | 直播管理后台和 REST API | `启动管理页面.bat`，端口 9888 |
| `CharacterManager/` | 角色管理 Tkinter 程序 | `启动角色管理器.bat` |
| `HomeAgent/` | 家庭桌宠、工具循环、语音输入、任务调度 | `HomeAgent/启动家庭Agent.bat` |
| `Vision/` | 网页 DOM MCP、可选 GUI 图像识别 | `mcp_server.py`，端口 8765 |
| `Sound/` | SenseVoice 本地 STT MCP | `Sound/mcp_server.py` |
| `Skill/` | 可复用 Agent 技能 | 每个目录的 `SKILL.md` 与 `scripts/` |
| `workspace/` | 身份、人格、场景规则、旧式每日记忆、角色图片 | 动态读取 |
| `LongTermMemory/` | SQLite 长期记忆 | `memory.db` |
| `Task/` | 一次性与周期提醒任务 | HomeAgent 调度器 |
| `state/` | 可跨进程修改的运行状态 | 直播上下文、维护状态 |
| `logs/` | 直播消息、管理器日志 | `messages.jsonl` 等 |
| `audio/` | TTS 输出 | 所有程序启动/运行时只保留最新 20 个 |

## 核心 Python 模块

- `src/ai_live_assistant/app.py`：`LiveAssistant`，直播事件、回复、上下文、记忆。
- `src/ai_live_assistant/bilibili.py`：直播事件和历史弹幕来源、弹幕发送。
- `src/ai_live_assistant/llm.py`：OpenAI 兼容 Chat Completions 客户端。
- `src/ai_live_assistant/tts.py`：9879 服务启动、合成、播放、音频清理。
- `src/ai_live_assistant/workspace.py`：人格文档、用户身份映射、每日记忆、近期直播记录。
- `src/ai_live_assistant/long_term_memory.py`：SQLite 高价值长期记忆。
- `HomeAgent/agent.py`：家庭 Agent、确定性路由、工具、Codex/MCP、TTS、上下文维护。
- `HomeAgent/app.py`：桌宠 UI、右键菜单、停止任务、设置和日志页。
- `CharacterManager/app.py`：角色工作台所有管理页面。
- `Vision/agent.py`：Playwright DOM 操作、可选 GUI-Actor、Windows 窗口工具。
- `Vision/mcp_server.py`：FastMCP 工具注册；网页工具固定在单一专用线程共享浏览器会话。
- `Vision/mcp_call.py`：系统 Python 到项目 `.venv` MCP 客户端桥。

## Skills

- `ai-live-character-image`：角色图片生成、编辑和登记。
- `schedule-home-task`：提醒、闹钟、一次性/周期任务。
- `sing-with-mimo`：默认本地 TTS 朗读歌词，MiMo 为关闭的备用分支。
- `web-agent-operator`：无图像识别的多步网页操作与最终状态验证。

