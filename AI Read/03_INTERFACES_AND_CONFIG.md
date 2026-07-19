# 接口与配置

## 配置文件

### 根 `config.yaml`

- `app`：房间号、dry-run、是否发送弹幕、日志级别。
- `bilibili`：Cookie 环境变量名、欢迎、频率和长度。
- `reply`：触发模式、名称/前缀、冷却、上下文和回复长度。
- `llm`：供应商、超时、家庭/直播/记忆场景温度与 token、供应商 URL/模型/密钥环境变量。
- `tts`：9879 URL、健康接口、启动批处理、请求模板、音频响应、播放、健康超时和重试策略。
- `memory_write`：重要度判断、每日上限、强制/忽略关键词。
- `context_cleanup`：直播上下文保留 120 分钟、检查间隔。
- `workspace`：人格、身份、记忆和图片相对路径。

### `HomeAgent/config.yaml`

- `home`、`microphone`、`desktop_pet`、`stt`。
- `agent`：工具轮次和 Skill 根目录。
- `context_maintenance`：家庭上下文每天 03:00 压缩。
- `codex_cli`：CLI、沙箱、超时、触发模式。
- `vision_mcp`：HTTP 服务、现有浏览器 CDP 端点和 GUI 模型开关。当前 `gui_enabled: true`、`preload_model: false`，表示允许按需加载视觉模型。
- `computer_control`：权限、确认策略、软件名称到路径映射。
- `self_upgrade`：是否允许自编辑、重启恢复、重启前校验和最大连续重启次数。
- `agent`：模型驱动电脑动作、本地工具优先和操作重试轮数。

`HomeAgent/config.d/` 将 `computer_control`、`vision_mcp`、`context_maintenance` 等维护项另存为独立文档。角色管理服务写入时同步这些文档，并保留 UI 未识别字段。

密钥只存 `.env`，常见变量：`DEEPSEEK_API_KEY`、`MIMO_API_KEY`、`CUSTOM_API_KEY`、`IMAGE_API_KEY`、`STT_API_KEY`、`BILIBILI_COOKIE`。

## 管理后台 REST API（127.0.0.1:9888）

- `GET/PUT /api/config`
- `GET/PUT /api/secrets`
- `GET/PUT /api/docs`
- `GET /api/messages`
- `GET/POST /api/memories`
- `PUT/DELETE /api/memories/{memory_id}`
- `GET/PUT /api/character`
- `POST /api/character/images`
- `GET /api/character/image/{filename}`
- `DELETE /api/character/images/{image_id}`
- `GET /api/status`
- `POST /api/assistant/start`
- `POST /api/assistant/stop`
- `POST /api/test/tts`
- `POST /api/test/llm`

## TTS（127.0.0.1:9879）

- 健康/模型选项：`GET /api/options`
- 合成：`POST /api/tts`
- 默认请求核心字段：`text`、`speed`、`top_k`、`top_p`、`temperature`；程序可自动补模型和参考音频。
- 当前可靠性参数：`health_timeout_seconds: 6`、`timeout_seconds: 60`、`retry_attempts: 4`、`retry_delay_seconds: 2`。
- 健康接口超时但端口仍可连接时，不自动启动第二个服务；合成暂态错误按指数退避重试。

## vision-gui MCP（127.0.0.1:8765/mcp）

### 当前推荐的无模型网页工具

- `navigate(url)`
- `get_url()`
- `web_read(max_chars)`：正文、链接、按钮、输入框。
- `web_click_text(text, exact)`
- `web_fill(field, text, submit)`
- `web_press(key)`
- `web_play_media()`

### GUI 图像工具（`gui_enabled` 启用后，当前为按需懒加载）

`click`、`type_text`、`screenshot`、`play_video`、`desktop_screenshot`、`list_windows`、`activate_window`、`window_screenshot`、`window_click`、`window_type_text`、`desktop_click`、`desktop_type_text`、`desktop_scroll`、`desktop_hotkey`。

GUI 关闭时 HomeAgent 不应在 LLM 工具列表暴露 `vision_gui_task`。当前每次窗口/桌面动作后默认等待约 550 ms，再截取新图并返回状态变化证据。

## sound-asr MCP（127.0.0.1:8766/mcp）

- `transcribe_file(path, language="auto")`
- `record_and_transcribe(duration=5, language="auto")`
