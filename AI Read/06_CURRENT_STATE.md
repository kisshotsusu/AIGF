# 当前状态与近期决策

## 当前配置快照

- B站房间：`21248701`。
- 直播回复触发：`all`。
- 真实弹幕发送：关闭。
- 当前 LLM：DeepSeek `deepseek-chat`；Home 最大 600 tokens，Live 最大 160 tokens。
- TTS：本地 9879，自动启动 `E:\Doc\SVC\启动推理.bat`。
- 家庭 Agent：自动语音回复，Codex CLI 启用，完整电脑权限配置存在。
- GUI 图像识别：关闭。
- 网页 MCP：常驻 8765、不加载 GUI-Actor。
- 直播上下文：保留 120 分钟，可独立清空。
- 家庭上下文：每天 03:00 压缩，错过后按一分钟策略重试。

## 已验证的关键能力

- `web-agent-operator` 已端到端找到并播放 `hanser--芒种（口胡警告！）`，包括搜索、结果评分、打开、播放和最终读取验证。
- 系统 Python 没有 `mcp` 时，`Vision/mcp_call.py` 可通过项目 `.venv` 正常调用 MCP。
- GUI 关闭时 `_tools()` 不暴露 `vision_gui_task`。
- 直播状态文件可在直播助手不运行时直接清空。
- 视觉网页服务未加载模型时日志中无 `loading GUI-Actor`/`model loaded` 标记。

## 已知限制

- Playwright 使用独立 Chromium 会话，不等同于用户日常浏览器的登录会话。
- B站反自动化或登录验证可能使 DOM 自动化失败；失败必须报告实际原因。
- GUI-Actor 关闭后不能操作网易云等原生桌面 App 的图像界面；需要重新开启开关或增加该软件的原生/API 自动化。
- Codex CLI 曾出现远端 MCP 502、模型缓存字段不兼容和长时间无响应；常见网页任务不要依赖 Codex 规划。
- 根 `README.md` 部分 Vision 描述过时，维护时优先更新本目录并视情况同步 README。

## 下一会话常见检查

1. 读 `HomeAgent/logs/agent-events.jsonl` 最近事件，确认实际路由。
2. 检查 `HomeAgent/config.yaml` 中 `gui_enabled` 是否仍为 false。
3. 检查 8765 端口和 MCP 工具列表。
4. 网页任务只打开首页时，先检查 `simple_bilibili_open` 是否误吞“找/搜/播放”请求。
5. 上下文清理异常时检查 `state/live-context.json` 和 `state/live-context-control.json`。

