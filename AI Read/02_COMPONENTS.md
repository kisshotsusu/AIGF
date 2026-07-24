# 组件与目录

| 路径 | 职责 | 主要入口 |
|---|---|---|
| `main.py` | 直播助手 CLI 入口、配置检查 | `python main.py --config config.yaml` |
| `modules/live/ai_live_assistant/` | 直播业务核心 | `app.py`、`bilibili.py`、`llm.py`、`tts.py`、`workspace.py` |
| `modules/live/manager.py` + `modules/live/web/` | 直播管理后台和 REST API；根 `manager.py` 为兼容入口 | `启动管理页面.bat`，端口 9888 |
| `CharacterManager/` | 角色数据服务、默认 PySide6 UI、Tk 兼容后备 | `启动角色管理器.bat` |
| `HomeAgent/` | 家庭桌宠、工具循环、语音输入、任务调度 | `HomeAgent/启动家庭Agent.bat` |
| `Vision/` | 网页 DOM MCP、可选 GUI 图像识别 | `mcp_server.py`，端口 8765 |
| `Sound/` | SenseVoice 本地 STT MCP | `Sound/mcp_server.py` |
| `Skill/` | 可复用 Agent 技能 | 每个目录的 `SKILL.md` 与 `scripts/` |

`Skill/hatch-pet/` 是从 Codex 本地技能复制并项目化的完整 v2 桌宠生产工具包，包含技能合同、17 个确定性图片处理脚本、3 份参考规范、代理配置、许可证和 6 个回归测试文件。它负责 8×11 动画图集的准备、帧提取、方向检查、透明边缘处理、校验和打包；视觉生成仍按技能合同调用 `$imagegen`。
| `workspace/` | 身份、人格、场景规则、旧式每日记忆、角色图片 | 动态读取 |
| `LongTermMemory/` | SQLite 长期记忆 | `memory.db` |
| `Task/` | 一次性与周期提醒任务 | HomeAgent 调度器 |
| `Projects/` | HomeAgent 独立项目的持久目录 | 每个子目录为一个独立项目 |
| `state/` | 可跨进程修改的运行状态 | 直播上下文、维护状态 |
| `logs/` | 直播消息、管理器日志 | `messages.jsonl` 等 |
| `audio/` | TTS 输出 | 所有程序启动/运行时只保留最新 20 个 |

## 核心 Python 模块

- `modules/live/ai_live_assistant/app.py`：`LiveAssistant`，直播事件、回复、上下文、记忆、礼物、欢迎去重与优先语音队列。
- `modules/live/ai_live_assistant/bilibili.py`：直播事件和历史弹幕来源、弹幕发送。
- `modules/live/ai_live_assistant/llm.py`：OpenAI 兼容 Chat Completions 客户端。
- `modules/live/ai_live_assistant/tts.py`：9879 服务探活/防重复启动、GPU 串行合成、退避重试、播放和音频清理。
- `modules/live/ai_live_assistant/config.py`：`load_config` 注入 `_root` 并校验 `room_id>0`；`secret_from_env` 从环境变量读密钥。
- `modules/live/ai_live_assistant/workspace.py`：人格文档、用户身份映射、每日记忆、近期直播记录。
- `modules/live/ai_live_assistant/long_term_memory.py`：SQLite 高价值长期记忆（`store` 有硬校验，见 `07`）。

> **单一真相源**：`src/ai_live_assistant/` 只是 `from modules.live.ai_live_assistant.xxx import *` 的兼容再导出 shim，供 `HomeAgent/agent.py` 通过 `src.ai_live_assistant.*` 引用。改直播核心代码只改 `modules/live/ai_live_assistant/`。
> `task_manager`（HomeAgent 的任务调度）不在 `HomeAgent/` 本地，而在 `Skill/schedule-home-task/scripts/task_manager.py`；`agent.py` 启动时把该目录加入 `sys.path` 后以 `from task_manager import TaskStore` 使用。
- `HomeAgent/agent.py`：家庭 Agent、MiMo 语义计划、模型驱动工具循环、本地工具与 Codex/MCP 并列执行、TTS 和上下文维护。
- `HomeAgent/qt_app.py`：默认桌宠 UI、任务进度、文字/语音输入 FIFO、设置和重启恢复；任务或最终 TTS 播放期间仍可排队输入，由单个 `ChatWorker` 依次消费；`app.py` 保留 Tk 后备和 Qt 转发入口。
- `HomeAgent/qt_app.py` 中的 `ScreenCareWorker`：按“屏幕关怀”设置的频率低优先级触发关怀，忙碌时跳过且不允许并发实例；设置窗口可实时启停和修改频率。
- `HomeAgent/qt_app.py` 中的 `CareMessagePopup`：在桌宠旁显示不抢输入焦点的关怀气泡，默认 12 秒后自动隐藏并自动适配屏幕边缘。
- `HomeAgent/self_upgrade.py`：未完成任务持久化、自升级校验与重启恢复。
- `HomeAgent/restart_watchdog.py`：直接重启与升级后重启共用的进程接力器；等待旧 HomeAgent 退出后启动新实例。
- `HomeAgent/home_modules/code_editor.py` 与 `self_upgrade.py` 共同保证自升级意图识别、真实变更要求、文档同步、语法校验和重启门禁；空变更不能完成自升级。
- `HomeAgent/home_modules/code_editor.py`：隔离的代码工程模块，负责自身/独立项目识别、工程合同、文件追踪、Python/YAML/JSON/Node/TypeScript/静态网页校验和自主测试。
- `HomeAgent/home_modules/mimo_multimodal.py`：MiMo 图片理解、WAV/MP3 语音识别和基于工具证据的任务完成独立核验。
- `HomeAgent/agent.py::proactive_screen_care`：临时截取主屏幕、用 MiMo 生成隐私安全的简短关怀语、按配置播报并保证截图清理。
- `HomeAgent/home_modules/command_executor.py`：执行模型规划后的 PowerShell/CMD 命令，统一工作目录、超时、输出和失败状态。
- `CharacterManager/service.py`：UI 无关的数据接口、原子保存、配置文档拆分和未知字段保留。
- `CharacterManager/qt_app.py`：默认角色工作台；`app.py --legacy-tk` 启动旧 Tk 前端。
- 角色工作台的“模型 API”页面包含 DeepSeek、MiMo、Custom 与“MiMo 多模态”标签；多模态不再占用独立侧栏入口。
- `Vision/agent.py`：现有浏览器 CDP/DOM、独立 Playwright、懒加载 GUI-Actor、Windows 窗口工具和操作后截图验证。
- `Vision/mcp_server.py`：FastMCP 工具注册；网页工具固定在单一专用线程共享浏览器会话。
- `Vision/mcp_call.py`：系统 Python 到项目 `.venv` MCP 客户端桥。

## Skills

- `ai-live-character-image`：角色图片生成、编辑和登记。
- `schedule-home-task`：提醒、闹钟、一次性/周期任务。
- `sing-with-mimo`：默认本地 TTS 朗读歌词，MiMo 为关闭的备用分支。
- `web-agent-operator`：无图像识别的多步网页操作与最终状态验证。

## 回归测试

- `modules/live/tests/test_reliable_speech.py`：直播 TTS 暂态失败重试、欢迎冷却提交时机、重复进场合并。
## 常驻组件单实例约束（2026-07-21）

- HomeAgent、角色管理器、直播控制台、直播助手、Sound MCP、Vision MCP 均使用跨进程 `InstanceLock`；锁句柄覆盖整个进程生命周期，第二实例在初始化、占用端口或加载模型前退出。
