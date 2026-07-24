# AI 直播工程：新会话入口

最后核对：2026-07-24。路径均以工程根目录为基准，代码和当前 YAML 始终高于文档。

## 阅读顺序

1. 本文件：边界、当前事实和文档索引。
2. `01_ARCHITECTURE.md`：组件关系、控制流和可靠性设计。
3. `02_COMPONENTS.md`：目录、入口和主要源码。
4. `03_INTERFACES_AND_CONFIG.md`：HTTP、MCP、模型、TTS/STT 和配置。
5. `04_DATA_MEMORY_CONTEXT.md`：身份、记忆、上下文和日志。
6. `05_OPERATIONS_AND_RULES.md`：启动、验证和修改约束。
7. `06_CURRENT_STATE.md`：开发进度、当前配置、已验证能力和下一步。
8. `07_DEVELOPER_REFERENCE.md`：函数级 API、数据契约、配置同步机制与已知坑（改代码前必读）。
9. `08_TESTING.md`：自动测试项目、真实冒烟、GUI 验收、重启验证和失败定位流程。

## 一句话概括

这是一个 Windows 本地 AI 角色运行平台：同一角色可在 B站直播、家庭桌宠和桌面自动化场景工作，并共享身份、人格、语音与经过隐私隔离的长期记忆。

## 当前必须知道的事实

- **最高优先级开发规则：禁止任务类型和任务流程硬编码。** 大模型负责理解用户意图、生成计划、选择工具、解释工具结果并决定下一步；工具只执行原子操作并返回结构化事实。禁止关键词/正则分类器、固定站点流程、组合式业务工具和本地成功硬门槛。
- 默认 UI 已迁移到 PySide6：HomeAgent 为无边框桌宠；角色管理器通过 `CharacterManager/service.py` 访问数据。旧 Tk 代码只是兼容后备。
- 根模型供应商为 MiMo `mimo-v2.5`，并兼容 DeepSeek 和自定义 OpenAI 风格接口。
- `send_danmaku: false`，当前只播报、不发送 B站弹幕。
- 直播欢迎和普通回复进入独立语音队列；欢迎优先，TTS 显卡繁忙会重试，成功后才记欢迎冷却。
- HomeAgent 使用模型生成语义计划；本地工具与 Codex 都是模型可直接选择的执行能力，不靠本地任务类型隐藏工具或强制固定顺序。
- 网页先检查当前浏览器。能通过 CDP 读取 DOM 就用 HTML；否则保留现有登录浏览器并使用窗口视觉，不强制新开 Chrome。
- Vision 当前 `gui_enabled: true`、`preload_model: false`：视觉能力允许使用，但 GUI-Actor 只在需要时懒加载。
- 所有点击、输入、快捷键和滚动操作默认约 550 ms 后重新截图，返回 `state_changed`、`post_action_verified`、采集时间与实际作用对象；后续动作由模型决定。
- Sound MCP 为 `http://127.0.0.1:8766/mcp`；Vision MCP 为 `http://127.0.0.1:8765/mcp`；直播管理页为 `http://127.0.0.1:9888`。
- 直播短期上下文位于 `state/live-context.json`；提醒/闹钟只存入 `Task/*.json`；`HomeAgent/state/task-recovery.json` 只保存尚未完成的自身代码升级。

## 安全与修改边界

- 不读取、输出或写入文档中的 `.env` 密钥。
- 修改 YAML 前先读最新文件，只改负责字段并保留未知项。
- 不删除 `workspace`、`LongTermMemory`、`Task`、角色图片和用户日志。
- 登录态网页任务不得启动临时浏览器或关闭用户现有浏览器。
- 自动化必须验证最终状态，不能把部分进度或无报错调用当作成功。
- 工作区可能有用户未提交修改；只处理当前任务涉及的文件。
- 代码变化影响现有设计事实时，同一任务应重写 `AI Read` 对应章节；不能只追加流水账。代码验证本身不再以“必须修改文档”为硬门槛。

## 信息优先级

`当前代码与 YAML` > `AI Read` > 根 `README.md` > 各模块历史说明。此优先级用于排查旧信息，不代表允许文档长期落后：完成代码任务前必须更新对应 `01~08` 章节；影响当前能力的结果写入 `06_CURRENT_STATE.md`，测试数量、方法或验收流程变化同步 `08_TESTING.md`，入口或用户使用方式变化再同步 README。

## 维护约定

- 直播核心代码位于 `modules/live/ai_live_assistant/`；`src/ai_live_assistant/` 仅为兼容再导出。
- 当前能力写入 `06_CURRENT_STATE.md`，测试方法写入 `08_TESTING.md`，不要在 AI Read 中追加逐次故障和修复流水。
- 角色工作台管理共享模型和密钥配置；直播控制台只维护直播运行参数与 `BILIBILI_COOKIE`。
