# AI 直播工具箱

Windows 本地 AI 角色系统，围绕一个共享角色同时提供：B站直播互动、家庭桌宠、网页与桌面自动化、语音识别与合成、角色配置、长期记忆和可复用 Skill。

> 文档核对日期：2026-07-20。运行代码与 YAML 配置优先于文档；面向后续开发会话的详细资料位于 [`AI Read/`](AI%20Read/00_START_HERE.md)。

## 当前能力

| 子系统 | 当前实现 | 入口 |
|---|---|---|
| 直播助手 | B站事件、进场欢迎、礼物回复、MiMo/兼容 API 回复、GPT-SoVITS 播放、记忆写入 | `启动管理页面.bat` |
| HomeAgent | PySide6 无边框桌宠、文字/麦克风输入、实时任务进度、TTS、工具循环、任务恢复与自主升级 | `HomeAgent/启动家庭Agent.bat` |
| 角色管理器 | PySide6 配置工作台；Qt UI 经 `CharacterService` 读写，Tk 仅保留为兼容后备 | `启动角色管理器.bat` |
| Vision | 现有浏览器优先、DOM/HTML 优先，必要时懒加载 GUI-Actor 操作浏览器或 Windows 窗口 | MCP `127.0.0.1:8765/mcp` |
| Sound | SenseVoice/FunASR 本地语音识别 | MCP `127.0.0.1:8766/mcp` |
| 长期记忆 | 每日 JSONL + SQLite 高价值记忆，家庭/直播分场景读取 | 被直播与 HomeAgent 共用 |
| Skill | 网页多步任务、角色图片、定时任务、MiMo 唱歌 | `Skill/*/SKILL.md` |

## 总体架构

```text
B站事件 ──> LiveAssistant ──> LLM ──> 优先语音队列 ──> GPT-SoVITS ──> 播放
                  │                         │
                  └── 日志 / 上下文 / 记忆 ┘

文字/麦克风 ──> HomeAgent ──> 语义计划 ──> 本地工具 / Skill / Vision MCP
                       │                       │
                       ├── Codex（复杂任务后备）│
                       ├── 实时进度 / TTS 汇报  │
                       └── 任务恢复 / 自主升级 ─┘

CharacterManager(Qt) ──> CharacterService ──> config / workspace / LongTermMemory
```

设计原则：

- 模型负责理解目标、参数和完成条件，本地执行器负责权限、调用、重试和结果验证。
- 网页任务优先读取现有浏览器的 DOM/HTML；没有 DOM 时保留当前登录浏览器并使用窗口视觉，不为登录任务强制新开 Chrome。
- 每次点击、输入、快捷键或滚动后默认等待约 550 ms 并重新截图，返回页面是否变化以及下一步建议。
- 本地 HomeAgent 工具优先；Codex/MCP 网络链只处理本地工具无法完成的复杂任务。
- 直播和家庭共享身份与高价值记忆，但私密记忆、私人图片不得注入直播提示词。

## 目录

| 路径 | 职责 |
|---|---|
| `modules/live/` | 直播助手核心、管理 API 与网页控制台；根 `main.py`、`manager.py` 是兼容入口 |
| `HomeAgent/` | 家庭 Agent、PySide6 桌宠、STT/TTS、工具链、任务调度与恢复 |
| `HomeAgent/home_modules/` | 与主程序隔离的能力模块；当前包含代码编辑准备、变更追踪和校验 |
| `Projects/` | HomeAgent 创建的独立项目，每个项目包含自己的源码、README 和测试，不受 `work/` 清理影响 |
| `CharacterManager/` | `service.py` 数据接口、Qt 默认 UI、Tk 兼容 UI |
| `Vision/` | 浏览器 DOM、CDP 复用、GUI-Actor 和 Windows 窗口工具 |
| `Sound/` | SenseVoice/FunASR 识别服务 |
| `Skill/` | `web-agent-operator`、角色图片、定时任务、唱歌技能 |
| `workspace/` | 身份、人格、场景规则、每日记忆、角色图片 |
| `LongTermMemory/` | SQLite 长期记忆数据库 |
| `Task/` | 一次性和周期任务 |
| `state/` | 跨进程上下文、维护和任务控制状态 |
| `logs/` | 直播、HomeAgent、Vision、Sound 日志 |
| `audio/` | TTS 输出，自动保留最新 20 个音频 |
| `AI Read/` | 架构、接口、数据、运维和开发进度文档（`07_DEVELOPER_REFERENCE.md` 为函数级 API 与已知坑） |

## 环境与部署

所有组件共用根目录 `.venv`（Python 3.12）。RTX 50 系使用 PyTorch cu128；不要给各模块另建互相隔离的虚拟环境。

新机器部署：

1. 运行 `set_env.bat`，创建环境、安装依赖和 Playwright 内核。
2. 运行 `down_model.bat`，下载 GUI-Actor 与 SenseVoice 模型。
3. 从示例生成 `config.yaml` 和 `.env`，只在 `.env` 保存密钥。
4. 先运行配置检查：`.venv\Scripts\python.exe main.py --config config.yaml --check`。
5. 分别启动管理页面、角色管理器或 HomeAgent。

## 当前配置摘要

- 直播模型：MiMo `mimo-v2.5`；直播最大 300 tokens，家庭最大 800 tokens。
- B站：`send_danmaku: false`，因此不会发送真实弹幕；`dry_run: false` 不会绕过此保护。
- TTS：`127.0.0.1:9879`，按需自动启动 GPT-SoVITS。
- STT：HomeAgent 使用 `sound_mcp`，地址 `127.0.0.1:8766/mcp`。
- Vision MCP：启用；`gui_enabled: true`、`preload_model: false`，即允许视觉但默认懒加载模型。
- HomeAgent：本地工具优先，电脑操作默认最多 4 轮；代码任务也优先使用内置文件编辑/校验工具，Codex CLI 仅为低优先级后备。

配置可能随 UI 实时保存而改变，排障时应重新读取磁盘 YAML。

## 关键运行流程

### 直播互动

`BilibiliLive` 接收 WebSocket 事件，历史弹幕轮询作为弹幕补充。`LiveAssistant` 处理用户名、去重、回复触发、礼物和进场欢迎；模型回复被截短后进入独立语音优先队列。

欢迎语优先于普通回复。同一用户同时收到 `INTERACT_WORD` 与 `ENTRY_EFFECT` 时只排队一次；只有播放成功才写入欢迎冷却。TTS 在超时、连接错误或显卡繁忙时默认重试 4 次，健康接口短暂超时不会重复启动第二个 GPT-SoVITS 进程。

### HomeAgent 任务

1. 模型生成语义任务计划和可观察完成条件。
2. 优先选择本地确定性工具、网页 Skill 或 Vision MCP。
3. 网页先检查当前目标：`browser_dom`、`browser_visual` 或 `desktop_visual`。
4. 操作后重新观察并验证；失败保留阶段、证据与重试原因。
5. 最终回答一生成就立即显示在消息页，TTS 在其后继续播放，不再等整段语音结束才显示。
6. 任务执行时 UI 实时显示当前步骤和已完成内容；长任务可以生成 TTS 进度汇报。
7. 自主升级任务会先读取 README 与 `AI Read`、检查工作区、实际编辑并校验；没有文件变更不能报告成功，并可通过 `HomeAgent/state/task-recovery.json` 在重启后继续。
8. “创建/开发/编写独立项目”会由 MiMo 规划并调用 HomeAgent 本地 `code_*` 工具，默认写入 `Projects/<project-name>/`；生成后本地执行编译和测试。仅当本地多轮失败或用户明确要求时才回退 Codex。

### 网页自动化

`Skill/web-agent-operator` 强制执行：

`NAVIGATED → SEARCH_SUBMITTED → RESULTS_VERIFIED → RESULT_SELECTED → ACTION_EXECUTED → FINAL_VERIFIED`

只有打开页面就是最终目标时才允许停在导航。B站收藏夹任务读取真实收藏夹顺序，并优先导航用户已经打开的浏览器；搜索、选择或播放任务不得把“打开首页”报告成完成。

## 配置与数据

- 根 `config.yaml`：直播、模型、TTS、记忆和共享工作区配置。
- `HomeAgent/config.yaml`：桌宠、麦克风、STT、Vision、Codex、权限、维护与自主升级。
- `HomeAgent/config.d/`：电脑控制、Vision MCP、上下文维护等独立配置文档。
- `.env`：API Key、B站 Cookie；禁止提交、打印或复制到文档。
- `workspace/*.md`：人格与规则，程序运行时动态读取。
- `state/live-context.json`：直播短期上下文。
- `LongTermMemory/memory.db`：结构化长期记忆。

角色管理器通过 `CharacterManager/service.py` 原子保存配置并保留 UI 不认识的字段；HomeAgent 设置页采用实时保存。

## 验证与排障

- Python 语法：`.venv\Scripts\python.exe -m py_compile <files>`
- 回归测试：`.venv\Scripts\python.exe -m unittest discover -s modules/live/tests -v`
- 直播日志：`logs/assistant.log`、`logs/messages.jsonl`
- HomeAgent：`HomeAgent/logs/agent-events.jsonl`
- Vision：`Vision/logs/vision-mcp.log`
- Sound：`Sound/logs/sound-mcp.log`

任何自动化任务都不得只凭“调用没有报错”宣称成功；应检查最终 URL、正文、截图变化、媒体状态或对应业务返回值。

## 安全边界

- 发送 B站弹幕必须同时满足 `send_danmaku: true` 与 `dry_run: false`。
- 登录态任务不启动临时浏览器，不关闭用户的浏览器会话。
- 视觉开关关闭时不得暴露或调用 GUI-Actor 图像工具。
- 配置写入必须保留未知字段；禁止整段默认配置覆盖用户设置。
- 不删除 `workspace`、`LongTermMemory`、`Task`、角色图片和用户日志。

## 文档导航

新开发会话从 [`AI Read/00_START_HERE.md`](AI%20Read/00_START_HERE.md) 开始。当前完成度、近期修复和后续重点集中在 [`AI Read/06_CURRENT_STATE.md`](AI%20Read/06_CURRENT_STATE.md)。
