# 接口与配置

> 核对依据：实际代码（`modules/live/manager.py`、`Vision/mcp_server.py`、`Sound/mcp_server.py`、`HomeAgent/config.yaml`、`CharacterManager/service.py`）。代码与 YAML 高于本文。

## 配置文件

工程有两类 YAML：根 `config.yaml`（直播 + 共享工作区）与 `HomeAgent/config.yaml`（家庭桌宠与自动化）。`HomeAgent/config.d/` 是后者的“维护项拆分视图”，由角色管理器经 `CharacterService` 与 `HomeAgent/config.yaml` 按修改时间双向同步，详见 `05_OPERATIONS_AND_RULES.md`。

### 根 `config.yaml`

- `app`：房间号、dry-run、是否发送弹幕、日志级别。
- `bilibili`：Cookie 环境变量名、欢迎、频率和长度。
- `reply`：触发模式、名称/前缀、冷却、上下文和回复长度。
- `gift_reply`：礼物回复开关、冷却、最低币值与模板。
- `llm`：供应商、超时，以及 `live`/`home`/`memory` 场景各自的温度与 token；`providers` 中 `deepseek`/`mimo`/`custom` 的 URL、模型与密钥环境变量。MiMo 额外使用 `auth_header: api-key`、`max_tokens_field: max_completion_tokens` 与 `extra_body.thinking.type: disabled`。
- `tts`：9879 URL、健康接口、启动批处理、请求模板、音频响应、播放、健康超时和重试策略（重试 4 次、指数退避、上限 15 秒）。
- `singing`：唱歌模式（`local_tts` 朗读歌词，MiMo `mimo-v2.5-tts` 为关闭的备用分支）、男女声预设。
- `memory_write`：重要度判断、每日上限、强制/忽略关键词。
- `context_cleanup`：直播上下文保留 120 分钟、检查间隔。
- `image_generation` / `image_understanding`：图像生成与旧兼容图片理解配置。
- `mimo_multimodal`：统一 MiMo 多模态配置，包括总开关、Base URL、密钥环境变量、图片模型、ASR 模型/语言、完成检查模型、失败重试、超时和 `fail_closed`。当前图片模型 `mimo-v2.5`，语音模型 `mimo-v2.5-asr`，完成检查默认启用并重试 2 次。
- `workspace`：人格、身份、记忆和图片相对路径。

> 注意：直播管理后台的 `PUT /api/config` 与 `PUT /api/secrets` 已在服务端保护 `llm`/`tts`/`image_generation`/`memory_write`/`workspace` 以及除 `BILIBILI_COOKIE` 外的所有密钥——这些项已迁移到角色工作台（见下）。直播网页即使长时间未刷新，也不能用旧值覆盖它们。

### `HomeAgent/config.yaml`

- `home`：助理名、主人称呼、场景文件、上下文上限、是否自动播报。
- `microphone`：采样率、声道、设备 ID、识别后自动发送、本地 STT 运行时路径。
- `desktop_pet`：置顶、坐标、桌宠图标。
- `stt`：模式支持 `sound_mcp`、`mimo`、通用 `api` 与本地识别；MiMo 模式读取根 `mimo_multimodal` 配置。
- `agent`：`max_tool_rounds` 现表示失败轮次预算（当前 28），成功工具轮不计入；`max_tool_iterations` 是独立总迭代安全上限（当前 112）；`operation_retry_rounds`（电脑操作重试，当前 4）、模型驱动电脑动作、本地工具优先、是否允许角色图片 Skill、Skill 根目录。
- `semantic_planner`：**当前文档此前未记录的配置节**，控制 MiMo 语义计划步骤：`enabled`、`timeout_seconds`（10）、`minimum_confidence`（0.55）。低于置信度时回退或要求澄清。
- `progress_reporting`：**此前未记录的配置节**，控制长任务进度汇报：`enabled`、`long_task_seconds`（60）、`tts_cooldown_seconds`（90）、`max_reports_per_task`（3）。
- `screen_care`：运行时主动屏幕关怀。默认 `enabled: true`、`interval_seconds: 300`；设置窗口的“屏幕关怀”页可实时启停，并以分钟为单位设置 1～1440 分钟的问候频率。`skip_while_busy` 防止打断用户任务，`all_screens: false` 只截主屏，`show_message` 控制是否写入对话区，`popup_enabled` 控制桌宠消息气泡，`popup_duration_seconds` 控制气泡显示时长（默认 12 秒），`speak` 与 `home.auto_speak` 共同控制播报，`max_chars` 限制关怀语长度。截图仅存于系统临时目录并在每轮结束后删除。
- `prompt_wake`：**新增配置节**，控制提示词唤醒直接输入功能。`enabled`（默认 `false`）启用/禁用唤醒功能；`wake_words` 为唤醒词列表（默认 `["苏苏", "小助手", "你好苏苏", "嘿苏苏"]`）；`auto_send_after_wake`（默认 `true`）控制唤醒后是否自动发送命令；`wake_confirmation_sound`（默认 `true`）控制唤醒时是否播放确认音；`wake_timeout_seconds`（默认 10）控制唤醒超时时间。启用后，语音输入以唤醒词开头时会自动提取后面的命令并执行。设置窗口新增"唤醒"标签页，可直接配置唤醒词、启用/禁用及自动发送选项。
- `context_maintenance`：家庭上下文每天 03:00 压缩；可清理家庭闲聊、清理 `work/` 并保留 3 天。
- `codex_cli`：CLI、隔离 `CODEX_HOME`、沙箱、超时、触发模式与关键词。
- `vision_mcp`：HTTP 服务、现有浏览器 CDP 端点、GUI 模型开关。当前 `gui_enabled: true`、`preload_model: false`，即允许按需懒加载视觉模型，服务启动不预占显存。
- `computer_control`：权限、确认策略、软件名称到路径映射。
- `computer_control.full_access` 授权代码工具对绝对路径进行列举、读取、搜索、原子写入和精确替换；关闭时绝对路径仅限 `allowed_roots`。外部改动纳入变更跟踪与语法校验；`.env`、PEM、KEY、PFX 密钥文件始终拒绝。
- `self_upgrade`：是否允许自编辑、重启恢复、重启前校验和最大连续重启次数。

`HomeAgent/config.d/` 把 `computer_control`、`vision_mcp`、`context_maintenance`、`context_cleanup`（直播上下文清理）另存为独立文档。角色管理器写入时经 `CharacterService` 同时更新 `HomeAgent/config.yaml` 与对应 `config.d` 文件，并保留 UI 未识别字段。

密钥只存 `.env`，常见变量：`DEEPSEEK_API_KEY`、`MIMO_API_KEY`、`CUSTOM_API_KEY`、`IMAGE_API_KEY`、`STT_API_KEY`、`BILIBILI_COOKIE`。角色工作台负责 `llm`/`tts`/图像/STT 密钥；直播控制台只维护 `BILIBILI_COOKIE`。

角色管理器中，普通对话供应商和 MiMo 多模态均位于“模型 API”页面；“MiMo 多模态”标签管理图片、语音和任务完成检查，并只显示 API Key 是否已配置，不回显明文。

## 管理后台 REST API（127.0.0.1:9888）

由 `modules/live/manager.py` 实现，根 `manager.py` 为兼容入口。

- `GET/PUT /api/config`
- `GET/PUT /api/secrets`（仅 `BILIBILI_COOKIE` 可写）
- `GET/PUT /api/docs`（SOUL.md / RULES.md / ABILITIES.md）
- `GET /api/messages`（倒序、最多 500 条）
- `GET /api/memories`、`POST /api/memories`、`PUT/DELETE /api/memories/{memory_id}`（`memory_id` 可含 `.` 等字符，路由用正则 `.+` 匹配）
- `GET /api/character`、`PUT /api/character`（更新 CHARACTER.md 正文、主形象、单图标签/标签组）
- `POST /api/character/images`、`GET /api/character/image/{filename}`、`DELETE /api/character/images/{image_id}`
- `GET /api/status`、`POST /api/assistant/start`、`POST /api/assistant/stop`
- `POST /api/test/tts`、`POST /api/test/llm`

启动/停止逻辑：端口已占用时直接打开现有页面，不重复拉起；`start` 以 `python -m modules.live.main --config config.yaml` 子进程方式运行，并把 stdout/stderr 接到 `logs/assistant.log`。

## TTS（127.0.0.1:9879）

- 健康/模型选项：`GET /api/options`
- 合成：`POST /api/tts`
- 默认请求核心字段：`text`、`speed`、`top_k`、`top_p`、`temperature`；`svc_auto_options: true` 时客户端自动从 `/api/options` 补模型和参考音频。
- 当前可靠性参数：`health_timeout_seconds: 6`、`timeout_seconds: 60`、`retry_attempts: 4`、`retry_delay_seconds: 2`（指数退避，单次上限 15 秒）。
- 健康接口超时（/_options 5xx/超时）但端口仍可 `open_connection` 时，**不**自动启动第二个服务，避免显存被第二个进程占用；合成暂态错误按指数退避重试，最终失败仍记日志、不静默丢弃。
- 文本在传给外部 SVC 批处理前按 GBK 过滤，移除 emoji 等不可编码字符，避免中文路径/编码乱码。

## vision-gui MCP（127.0.0.1:8765/mcp）

默认 `stdio` 传输；`VISION_MCP_TRANSPORT=http`（或 `streamable-http`）时以 HTTP（`streamable-http`）暴露。`VISION_PRELOAD_MODEL=1` 在启动时加载 GUI-Actor 模型，会覆盖 `preload_model: false` 的默认懒加载行为。`HomeAgent` 通过 `http://127.0.0.1:8765/mcp` 连接。

所有网页/GUI 工具固定在单一专用线程（`_web_executor`，`max_workers=1`）内执行，共享同一浏览器会话，避免并发抢占。

### 当前推荐的无模型网页工具（DOM/HTML 优先）

- `navigate(url)` → 返回最终跳转 URL。
- `get_url()`
- `web_read(max_chars=12000)`：正文、链接、按钮、输入框。
- `web_click_text(text, exact=False)`：按可见文字点击链接/按钮。
- `web_fill(field, text, submit=False)`：按 placeholder/label/name 填写，可回车提交。
- `web_press(key)`：向网页发送按键（Enter / Escape / Control+L 等）。
- `web_play_media()`：播放当前页第一个 video/audio。

### GUI-Actor 视觉工具（需 `gui_enabled` 且模型已加载/懒加载）

- `click(instruction, topk=3, region="full")`：看截图并点击匹配元素，返回归一化与像素坐标。
- `type_text(instruction, text, topk=3)`：点击输入框再输入文字。
- `type_active_text(text, clear=True)`：向已聚焦的输入框输入，避免重复视觉定位（适合 Ctrl+L 后输入网址）。
- `ground_page(instruction, topk=3)`：只定位并返回坐标，不点击，用于观察与安全检查。
- `scroll(direction="down", amount=400)`、`wait(ms=1000)`、`play_video(instruction=...)`、`screenshot()`（返回 MCP 图片）。
- `inspect_active_target()`：先把活动窗口分类为 `browser_dom` / `browser_visual` / `desktop_visual`，再决定走 DOM、窗口视觉还是桌面工具。

### 窗口工具（激活 + 窗口内视觉）

- `list_windows(title_contains="")`：可见窗口、PID、标题、边界，可按标题过滤。
- `activate_window(title_contains)`：恢复并切到前台。
- `window_screenshot(title_contains)`、`window_click(title_contains, instruction, topk=3, idx=0)`、`window_double_click(...)`、`window_type_text(title_contains, instruction, text)`。

### 桌面工具（整屏视觉）

- `desktop_screenshot()`：主显示器截图（返回 MCP 图片）。
- `desktop_click(instruction, topk=3)`、`desktop_type_text(instruction, text)`、`desktop_type_active_text(text, clear=True)`。
- `desktop_read_clipboard()`：读取用户刚明确复制的文本。
- `desktop_scroll(direction="down", amount=600)`、`desktop_hotkey(keys)`（如 `['ctrl','l']`）。

### 诊断/资源

- `vision_memory_status()`：返回 Vision 进程自身的 CUDA 已分配/缓存/峰值显存，便于评估与 GPT-SoVITS 共存的显存基线。

GUI 关闭时 HomeAgent 不应在 LLM 工具列表暴露 `vision_gui_task`；`preload_model: false` 时 GUI-Actor 仅在被调用时按需加载。每次窗口/桌面动作后默认约 550 ms 重新截图，返回状态变化证据与下一步建议。

## sound-asr MCP（127.0.0.1:8766/mcp）

- `transcribe_file(path, language="auto")`：`auto/zh/en/ja/ko/yue`，只返回清洗后的纯文本。
- `record_and_transcribe(duration=5.0, language="auto")`：麦克风录音并转写。

## 跨进程服务发现小结

| 服务 | 地址 | 说明 |
|---|---|---|
| 直播管理后台 | `127.0.0.1:9888` | REST + 静态 Web 控制台 |
| 直播助手子进程 | 由 9888 拉起 | `python -m modules.live.main` |
| GPT-SoVITS TTS | `127.0.0.1:9879` | `/api/tts`、`/api/options` |
| vision-gui MCP | `127.0.0.1:8765/mcp` | `stdio` 或 `streamable-http` |
| sound-asr MCP | `127.0.0.1:8766/mcp` | SenseVoice 本地识别 |
