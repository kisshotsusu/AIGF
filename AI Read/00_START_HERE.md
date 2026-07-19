# AI 直播工程：新会话入口

最后核对：2026-07-18。文档中的相对路径均以工程根目录为基准。

新会话开始处理本工程前，按顺序读取：

1. 本文件。
2. `01_ARCHITECTURE.md`：系统边界和数据流。
3. `02_COMPONENTS.md`：组件、入口和主要源码。
4. `03_INTERFACES_AND_CONFIG.md`：HTTP、MCP、TTS、配置。
5. `04_DATA_MEMORY_CONTEXT.md`：记忆、上下文和持久化。
6. `05_OPERATIONS_AND_RULES.md`：启动、验证、修改约束。
7. `06_CURRENT_STATE.md`：当前启用状态、已知限制和近期设计决策。

## 一句话概括

这是一个 Windows 本地 AI 角色系统，包含 B站直播弹幕语音回复、家庭桌宠 Agent、角色管理器、共享人格与记忆、TTS/STT、Skill、MCP 和网页自动化。

## 当前最重要的事实

- 直播助手、HomeAgent、角色管理器共享 `config.yaml`、`.env`、`workspace`、`LongTermMemory`。
- 当前不发送 B站弹幕：`send_danmaku: false`，只生成并播放语音。
- HomeAgent 使用系统 Python 启动；需要 `mcp` 的调用通过项目 `.venv` 桥接。
- `vision-gui` HTTP MCP 地址为 `http://127.0.0.1:8765/mcp`。
- 当前已禁用 GUI-Actor 图像识别：`gui_enabled: false`、`preload_model: false`。网页任务使用 DOM/文本工具，不加载视觉模型、不占模型显存。
- Bilibili 多步任务由 `Skill/web-agent-operator` 执行，必须持续到搜索、选择、打开、播放、验证完成。
- 直播短期上下文持久化在 `state/live-context.json`，无需直播助手运行也能清空。
- TTS 服务在 `127.0.0.1:9879`，由 `E:\Doc\SVC\启动推理.bat` 启动。
- 管理网页在 `127.0.0.1:9888`。

## 信息优先级

代码与当前 YAML 配置 > `AI Read` > 根目录旧 `README.md`。旧 README 中“Vision 默认预加载”等描述可能已经过时。

## 安全

- 不在文档、日志或回复中输出 `.env` 密钥。
- 修改配置时保留用户已有字段，不用整份默认配置覆盖。
- 不删除 `workspace`、`LongTermMemory`、`Task`、角色图片或用户日志。
- GUI 图像识别关闭时，不得暴露或调用 `vision_gui_task`。
