# 当前状态与开发进度

核对日期：2026-07-20。本文件记录“现在是什么”，历史实现不应覆盖此处事实。

## 当前配置快照

| 项目 | 当前值 |
|---|---|
| B站房间 | `21248701` |
| 直播触发 | `all` |
| 真实弹幕 | `send_danmaku: false`；关闭 |
| 根 LLM | MiMo `mimo-v2.5` |
| Token | Live 300 / Home 800 |
| TTS | 9879，自动启动，60 秒请求超时，4 次尝试，2 秒起始退避 |
| STT | `sound_mcp`，8766 |
| Vision MCP | 8765，`gui_enabled: true`，`preload_model: false` |
| HomeAgent 执行 | 模型语义规划、本地工具优先、默认 4 轮操作重试 |
| Codex | 已启用，隔离 Home，600 秒普通任务超时，仅作后备 |
| 上下文维护 | 直播 120 分钟；家庭每天 03:00；`work/` 保留 3 天 |

## 已完成的主要阶段

### 1. 项目和配置整理

- 直播代码归入 `modules/live/`，根入口保留兼容转发。
- 主要模块目录使用英文；项目根目录现为 `AIAgent`，运行代码必须基于自身文件位置解析根目录，不得依赖固定盘符路径。
- 大部分项目内路径改为相对根目录解析；外部 GPT-SoVITS 启动路径仍由配置明确给出。
- 工具维护拆分到 `HomeAgent/config.d/`，角色管理服务原子保存并保留未知配置。

### 2. Qt 界面迁移

- HomeAgent 默认使用 PySide6 无边框圆角窗口，保留桌宠、收起/展开、文字、语音、停止和设置功能。
- 发送与停止按钮并列；任务执行时展示当前步骤、完成内容和耗时。
- 角色管理器默认使用 PySide6，数据访问抽象到 `CharacterService`；Qt 已补身份、外观、记忆规则、模型 API、麦克风设备、Vision/MCP、工具维护和路径选择等配置页。
- Tk 前端仍保留 `--legacy-tk` 后备，不再作为默认界面。

### 3. 语音链路

- HomeAgent 连接 Sound MCP/SenseVoice，语音识别结果可自动发送。
- 处理中文路径、编码乱码和不支持字符；TTS 输出自动清理为最新 20 个。
- 直播增加独立优先语音队列：欢迎语优先、合成与播放串行。
- 修复欢迎冷却提前写入、双进场事件重复、显卡繁忙单次超时即丢任务，以及健康检查超时重复启动语音服务的问题。
- 直播 TTS 暂态错误默认重试 4 次；欢迎、语音排队、成功与最终失败写入 `messages.jsonl`。

### 4. 网页与电脑自动化

- `web-agent-operator` 从“打开网页”升级为完整状态机，搜索、选择、播放、提交必须验证最终结果。
- 优先检查现有浏览器和 CDP DOM；登录任务不强制创建新 Chrome。
- B站收藏夹读取真实数据和真实顺序，导航已打开浏览器并验证最终 BVID。
- 网易云搜索播放要求精确选择歌曲结果，避免点击底部全局播放导致继续当前歌曲。
- 点击、双击、输入、快捷键和滚动后约 550 ms 自动截图，返回状态变化与下一步。
- GUI-Actor 当前允许懒加载；服务启动不预加载，降低常驻显存占用。

### 5. Agent 任务能力

- MiMo 先生成语义任务合同；本地代码验证并执行，不再把目标理解主要建立在站点正则上。
- HomeAgent 本地工具优先，Codex 只处理复杂编程、终端、自升级或本地能力缺失场景。
- 自编程请求会在委派前实际读取 README 和核心 `AI Read` 文档，并强制检查工作区、写入文件、运行校验；零变更任务按失败处理。
- 自编程实现已从主程序抽离到 `HomeAgent/home_modules/code_editor.py`；`agent.py` 只负责路由，`self_upgrade.py` 只负责状态和重启恢复。
- Agent 可创建持久化独立项目，默认位于 `Projects/<project-name>/`；代码写入后由本地模块独立执行编译、Python 测试、Node 语法/npm 测试、TypeScript 检查或静态页面结构检查。
- 代码任务不再自动转交 Codex：MiMo 优先调用 HomeAgent 内置的列目录、读取、搜索、原子写入、精确替换和测试工具；Codex 只在明确点名或本地工具耗尽后后备调用。
- 本地代码校验或测试失败时会把真实输出反馈给编码执行器，自主修复并重新测试，默认最多 2 轮，耗尽后才报告失败。
- 最终回答通过 UI 回调先于 TTS 播放显示，Qt 默认界面和 Tk 后备界面均不会再等待整段语音结束。
- Codex 工具链修复强制 MCP 验证、超时边界、stdin/JSONL、CLI 模型缓存版本兼容和实时进度解析。
- 长任务提供 UI 实时进度和可选 TTS 汇报；失败保留阶段、尝试次数和原因。
- 自主升级支持重启前验证、任务状态持久化、重启后继续和最大重启次数限制。

### 6. 记忆与维护

- 直播、家庭共享身份和高价值记忆，私密内容与直播提示词隔离。
- 直播上下文可在助手未运行时清空，并通过控制文件同步运行时 deque。
- 重要度滑条、每日上限、API 已配置提示、设备名称和工具文档等配置已进入角色管理 UI。
- HomeAgent 定期清理 `work/`，默认保留 3 天。

## 本轮新增验证

`modules/live/tests/test_reliable_speech.py` 已覆盖：

- TTS 前两次模拟 GPU busy、第三次恢复后成功。
- TTS 失败不写欢迎冷却，再次进场仍能尝试。
- 同一用户双进场事件只产生一个待播放欢迎。

运行：

```powershell
.venv\Scripts\python.exe -m unittest discover -s modules/live/tests -v
```

## 已知限制与风险

- 普通 Chrome/Edge 未以调试端口启动时无法读取其 DOM，只能保留该窗口并使用视觉操作。
- B站登录、风控、广告和页面实验可能改变 DOM；收藏夹 API 或登录 Cookie 失效时必须报告真实失败。
- GUI-Actor 在双 4K 屏幕和复杂界面上仍可能误定位；操作后截图只能证明状态变化，不能单独证明业务目标正确，仍需读取文本/URL/媒体状态。
- GPT-SoVITS 长时间显存不足时，4 次重试后仍会最终失败，但不会静默丢失：日志会记录具体错误，欢迎冷却不会被消耗。
- `INTERACT_WORD`/`ENTRY_EFFECT` 依赖 WebSocket；历史弹幕接口只能补弹幕，不能完整补回断线期间所有进场事件。
- Codex 网络和远端 MCP 可能返回 5xx 或不完整 JSONL，普通网页任务不得依赖它作为首选。
- 项目中仍保留 Tk 兼容代码与部分历史说明，修改时要确认真实启动入口。
- `[2026-07-20 文档细化]` `HomeAgent/config.yaml` 的 `computer_control.applications` 有 6 项（含 `网易云音乐`/`cloudmusic`），而 `HomeAgent/config.d/computer_control.yaml` 只有 4 项；两者未对齐，`CharacterService` 下次读取会按 mtime 取其一。建议统一基准后再改。
- `[2026-07-20 文档细化]` 新增 `07_DEVELOPER_REFERENCE.md`：函数级 API、数据契约、配置同步机制与已知坑；`03` 补全完整 Vision MCP 工具集并新增 `semantic_planner`/`progress_reporting` 两节；`02` 标注 `src/ai_live_assistant` 为 shim、`task_manager` 实际位置。

## 下一步优先级

1. 在真实直播间做低风险进场测试，统计 `welcome: received/success/error/skipped` 比例与语音队列等待时间。
2. 为语音队列增加可视化长度、当前重试轮次和最近错误，便于区分事件缺失与 GPU 阻塞。
3. 增加 B站 WebSocket 连接/断线日志与重连计数，评估无法由历史接口补回的进场事件比例。
4. 对网易云、B站收藏夹和通用网页状态机增加无声音/无外部写入的自动回归测试。
5. 做一次双 4K、GUI-Actor 懒加载与 GPT-SoVITS 并存时的显存基线测试。

## 排障入口

1. 直播欢迎：先查 `logs/messages.jsonl` 的 `welcome` 和 `speech`，再查 `logs/assistant.log` 的重试记录。
2. HomeAgent 路由：查 `HomeAgent/logs/agent-events.jsonl` 的任务计划、路由、步骤和失败阶段。
3. 网页/视觉：查 `Vision/logs/vision-mcp.log`，确认使用 existing CDP、独立 Playwright 还是 window visual。
4. STT：查 `Sound/logs/sound-mcp.log`。
5. 重启恢复：查 `HomeAgent/state/task-recovery.json`。
