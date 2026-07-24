# 开发者参考（细化补充）

## 本地重启指令契约

- `HomeAgent.is_restart_request(text)` 只接受明确的当前执行命令；否定、咨询和功能开发语句必须返回 `False`。
- `HomeAgent.chat` 必须在 `_acknowledge_common_response`、历史注入、规划器和供应商请求之前处理直接重启，设置 `restart_requested` 并返回固定本地文案。
- `finalize_task_recovery` 必须保留已经设置的直接重启标志，不能被普通任务的 `SelfUpgradeManager.finalize(False)` 覆盖。
- 直接重启不得调用 `SelfUpgradeManager.begin`，收尾必须调用 `clear()`。禁止写成 `direct_restart or finalize()`：布尔短路会在直接重启时跳过状态清理。`resume_prompt` 还需拒绝旧版遗留的纯重启提示词。
- Qt 使用 `_restart_if_requested`，Tk 使用 `_restart_agent`；两者都只能通过 `launch_restart_watchdog` 接力，避免新旧实例并存。

## 自主升级完成门禁

- 自升级是否成立只读取语义规划器的 `domain=code` 与 `code_scope=self`，禁止恢复 `CodeEditorModule.is_code_edit_request`、关键词表、正则或常见表达枚举。
- `SelfUpgradeManager.finalize` 对 `is_self_upgrade=true` 调用 `validate_current_changes(require_changes=True)`。空变更或语法/配置错误必须写入 `validation_failed` 并阻止重启；是否需要同步 `AI Read` 由模型按实际影响决定，不作为固定验证门槛。
- 模型返回的普通 `content` 即使包含 `<tool_call>` 也不是工具调用。`HomeAgent._contains_unexecuted_tool_markup` 会拒绝此类回答，只有 API `tool_calls` 数组中的调用才能执行。
- `_speak_home` 是 TTS 的统一安全门：伪工具标记、Markdown 代码块或超长源码不得播报。
- 自身代码任务的 subject 不仅包括 HomeAgent，也包括本仓库的直播/B站/弹幕、CharacterManager、Vision、Sound 等组件；对应修复请求必须令 `current_code_self_edit=true`。
- `aiohttp.ClientSession` 仅在其 `async with` 作用域内有效。工具循环退出后的失败或后备结果播报必须调用 `_speak_with_fresh_session`，不得继续引用循环中的 `session`。
- `agent.max_tool_rounds` 是失败预算而非总调用数；每次模型迭代最多累计一个失败轮。成功工具结果不增加 `failed_rounds`。`max_tool_iterations` 是强制总上限，两者必须分别写入 `tool_round_limit_reached` 日志。
- `_speak_with_fresh_session` 仍调用 `_speak_home`，后者首先执行 `TTSClient`（GPT-SoVITS）；只有 `_speak_home_unlocked` 抛出异常时才允许 `_windows_sapi_speak` 降级。

## HomeAgent 主动屏幕关怀契约

- `HomeAgent.proactive_screen_care() -> str`：后台抓取屏幕并调用 `MiMoMultimodalClient.analyze_image`；成功返回简短关怀语，关闭、接口失败或无结果返回空字符串。该方法不向外暴露截图路径，`finally` 必须删除临时 PNG。
- `ScreenCareWorker` 在独立 `QThread` 中运行独立 asyncio 事件循环；`HomeAgentWindow.run_screen_care` 是唯一 Qt 调度入口。不得复用 `Bridge.finished`，否则会错误结束用户任务卡片。
- 调度器必须保持“忙时跳过、单实例运行”的约束。`SettingsDialog` 将频率以分钟展示并保存为 `interval_seconds`；`HomeAgentWindow.apply_screen_care_settings()` 负责保存后即时启动、停止或重置定时器。最小值为 60 秒，默认值为 300 秒。
- 关怀提示不得要求模型转录或复述屏幕内容；新增输出渠道时仍须服从 `screen_care.show_message`、`screen_care.speak` 和 `home.auto_speak`。
- `_show_screen_care` 同时路由对话区与 `DesktopPetWindow.show_care_message`；桌宠气泡受 `popup_enabled` 控制。`CareMessagePopup` 必须保持 `WA_ShowWithoutActivating`，自动隐藏且位置限制在当前屏幕可用区域内。

> 面向改代码的会话。本文是对 `01~06` 的“落地层”补充：具体函数签名、数据契约、同步逻辑与已知坑。所有内容已对照项目根目录代码核对；项目移动或改名后无需修改本文路径。

## 1. 代码位置与“单一真相源”

直播核心代码**只有一份**，位于 `modules/live/ai_live_assistant/`：

```
app.py  bilibili.py  config.py  llm.py  tts.py  workspace.py  long_term_memory.py
```

`src/ai_live_assistant/` 是**纯兼容再导出 shim**（`from modules.live.ai_live_assistant.xxx import *`），供 `HomeAgent/agent.py` 通过 `src.ai_live_assistant.tts/workspace/long_term_memory` 引用。**不要在两个目录分别改同一文件**——改 `modules/live/ai_live_assistant/` 即可，`src/` 会自动透传。

`task_manager` 不在 `HomeAgent/` 本地，而在 `Skill/schedule-home-task/scripts/task_manager.py`；`HomeAgent/agent.py` 启动时把该目录插入 `sys.path`，再以 `from task_manager import TaskStore` 使用。`self_upgrade` 仍是 `HomeAgent/self_upgrade.py`。

## 2. 直播核心模块 API（签名级别）

### `LiveAssistant`（`app.py`）
- `run()`：建立 `aiohttp` 会话，启动 `BilibiliLive` 事件流、历史弹幕轮询、`_context_cleanup_loop`、`_speech_worker` 四个生产者，主循环 `handle_event`。
- `handle_event(event)`：按 `cmd` 分流弹幕、礼物及 `INTERACT_WORD(_V2)` / `ENTRY_EFFECT(_MUST_RECEIVE)` 新旧进场事件。
- `_welcome(uid, user)`：冷却（`welcome_cooldown_seconds`）+ `_welcoming` 去重集合；仅当 `_emit(..., speech_priority=0)` 返回 `True`（语音成功）才写 `welcomed` 冷却。
- `_emit(text, speech_priority=10) -> bool`：`send_danmaku` 与 `dry_run` 双重门控（须同时为 true 才发弹幕）；返回语音是否成功。
- `_speech_worker()`：单消费者，从 `asyncio.PriorityQueue` 串行取 `(priority, seq, text, future)` 调用 `tts.speak`，写 `completed` future。优先级 0=欢迎，10=普通。
- `_maybe_remember(...)`：`memory_write.mode` 为 `important` 时用 LLM 判重要度，低于 `importance_threshold`（70）不写；`always_keywords` 强制写。同时写每日 JSONL 与 SQLite。

### `TTSClient`（`tts.py`）
- `synthesize(text) -> Path|None`：持 `_synthesis_lock` 串行；`retry_attempts` 次指数退避（上限 15s）；抛 `CancelledError` 会取消 future。
- `speak(text)`：`synthesize` + 同步 `play`。
- `ensure_service()`：先 `/api/options` 探活；不可达但端口可连时**不**重复启动，返回缓存 options 或抛超时；`auto_start` 才拉起 `start_command` 批处理。
- 模块级 `cleanup_audio_files(dir, keep=20)`：`audio/` 始终只保留最新 20 个。
- `_tts_safe_text(text)`：GBK 过滤 emoji 等不可编码字符。

### `LLMClient`（`llm.py`）
- `reply(messages, profile="live") -> str`：`profile` 取 `live/home/memory` 覆盖温度与 token；MiMo 自动用 `api-key` 头、`max_completion_tokens` 字段并注入 `thinking.type=disabled`。

### `LongTermMemoryStore`（`long_term_memory.py`）
- `store(*, tags, summary, detail, category, importance=80, user_id="owner", scene, privacy, source)` 的**硬约束**（不满足直接抛 `ValueError`，调用方需捕获）：
  - `category` 必须是高价值集合：`health/emotion/major_event/preference/habit/relationship/agreement`，否则拒绝。
  - `tags` 必须 3–5 个、每个 ≤24 字符。
  - `summary` 必须 1–20 字符。
  - `detail` 非空；`importance` 经校验后钳到 70–100。
  - 寒暄短语（"今天天气不错""你好"等）或 ≤4 字非健康/情绪内容被拒。
- `retrieve(query_tags, limit, user_id)`：按标签重叠(×10)+文本命中(×3)打分排序，最多返回 20 条。
- `migrate_legacy(memory_dir)`：幂等，把每日 JSONL 中 `importance≥70` 的高价值记忆并入 SQLite（按 `category_map` 转类别）。

### `Workspace`（`workspace.py`）
- `resolve_user(value)`：把家庭称呼（`aliases`）与直播用户名（`live_usernames`）都解析为规范 `id`（`owner` 或 `viewer:<name>`），避免同一人被存成两个人。`IDENTITY.yaml` 是唯一真值。
- `remember(event)` / `recent_memories(limit, include_private)`：每日 JSONL 读写；直播回复 `include_private=False` 不注入私密记忆。
- `cleanup_home_chatter()`：只删 `source` 以 `home-` 开头且为普通对话/回复且 `importance<70` 的条目，保留重要/手动/隐私。

### 管理后台（`modules/live/manager.py`）
- `create_app()`：aiohttp 路由表见 `03_INTERFACES_AND_CONFIG.md`。`put_config` 会丢弃传入的 `llm/tts/image_generation/memory_write/workspace`，用磁盘当前值回填；`put_secrets` 只接受 `BILIBILI_COOKIE`。
- `start_assistant` 子进程：`python -m modules.live.main --config <ROOT>/config.yaml`，stdout 接 `logs/assistant.log`。

### HomeAgent（`HomeAgent/agent.py`）
- `HomeAgentWindow.send()` 在 worker 忙碌时必须清空编辑框、显示用户消息并把文本追加到 `input_queue`，不得静默丢弃；`finish_task()` 仅在当前 worker（包含最终 TTS）结束后通过零延迟 Qt 回调启动队首任务。队列为进程内 FIFO，重启时不在旧进程继续消费。
- 运行期读取 `HomeAgent/config.yaml`、`config.yaml`、`workspace`、`Task`、`LongTermMemory`；`__init__` 后台线程 `ensure_vision_service` / `ensure_sound_service` 自动拉起 MCP。
- `begin_task` / `update_task_recovery` / `finalize_task_recovery` / `recover_interrupted_task` / `stop_current_task`：围绕 `SelfUpgradeManager` 做任务持久化与重启恢复；`stop_current_task` 会 `taskkill` 当前活跃子进程但保留常驻服务。
- `SelfUpgradeManager.clear()` 是完成/取消状态的唯一清理入口。`resume_prompt()` 只能恢复 `running`；`restart_pending` 是已完成升级的进程接力标记，读取后必须清理并返回空字符串，禁止再次提交原任务。
- `CodeEditorModule._resolve_read_path` 与 `_resolve_edit_path` 负责路径规范化；自我修改可访问整个仓库源码，`computer_control.full_access` 授权外部绝对路径读写。外部结果返回规范绝对路径，写入后加入 `_external_changed` 并参与语法校验。
- `log_event(event, **data)`：写 `HomeAgent/logs/agent-events.jsonl`，密钥按正则脱敏（`bearer ...` / `sk-...` 截断为 `***`），单字段 ≤4000 字符。
- 工具循环收集最近工具返回作为 `completion_evidence`；执行类任务生成候选答案后调用 `MiMoMultimodalClient.verify_completion`。只读观察/查询在成功证据已包含所问信息时即通过，不额外要求被观察对象达到终态；变更/交互任务仍必须有可验证终态。失败时把 `reason/next_action` 作为新一轮指令，超过 `completion_max_retries` 后返回明确未通过而不是成功措辞。

### `MiMoMultimodalClient`（`HomeAgent/home_modules/mimo_multimodal.py`）
- `analyze_image(session, path, prompt)`：图片编码为 data URL，通过 `chat/completions` 的 `image_url` + `text` 内容调用 `mimo-v2.5`。
- `transcribe_audio(session, path, language)`：只接受 WAV/MP3，Base64 后不超过 10 MB，通过 `input_audio` 和 `asr_options.language` 调用 `mimo-v2.5-asr`。
- `verify_completion(session, task, plan, answer, evidence)`：要求模型只返回 `{passed, reason, next_action}`；核验依据是工具证据，默认接口异常关闭成功路径。请求固定 `thinking.type=disabled`、`stream=false`，不设置 `response_format`；空响应错误必须包含 `finish_reason`。

### `CodeEditorModule` 变更与验证
- 跟踪范围覆盖整个仓库中的源码、配置、README 与 `AI Read`，不再依赖固定模块目录清单。
- `validate_current_changes` 检查真实变更以及 Python/YAML/JSON 等文件语法，不会因为缺少 `AI Read` 或项目 README 变更而人为失败。
- 执行模型仍应按实际影响维护文档，但这是交付判断，不是代码工具内部的任务类型硬编码。

### 角色管理器 MiMo 多模态布局
- `MiMoMultimodalPage(embedded=True)` 嵌入 `ModelPage.provider_tabs`，内部使用 `QScrollArea` 承载三组表单，避免较小窗口裁切输入项。
- 保存按钮位于滚动区外并始终可见；图片/语音模型输入框保持最小宽度，语言下拉显示中文含义但保存稳定值 `auto/zh/en`。

## 3. 数据契约

### `logs/messages.jsonl`（直播）
每行一个事件对象，关键 `event` 与 `status`：

| event | status 取值 | 说明 |
|---|---|---|
| `received` | `triggered` / `skipped`(reason: masked_username / cooldown / not_matched) | 收到弹幕 |
| `reply` | `success` / `error` | 模型回复结果 |
| `gift` | `triggered` / `skipped`(cooldown / below_min_total_coin / masked_or_missing_username) | 礼物 |
| `welcome` | `received` / `success` / `error`(tts_failed) / `skipped`(cooldown / already_queued / masked_username) | 进场欢迎 |
| `speech` | `success` / `error` | 语音队列最终结果 |
| `memory` | `success` / `skipped`(daily_limit / ignored_keyword / too_short / importance:N) | 记忆写入判定 |

### `state/live-context.json`
直播短期模型上下文数组（`role`/`content`/`_created_at`）。`HomeAgent` 通过 `state/live-context-control.json` 下发 `{action:"clear", token}` 请求清空；`LiveAssistant._apply_context_control` 每秒读取、原子清空内存 deque 并回写完成状态。**不**删除 `messages.jsonl`、每日记忆或 SQLite。

### `LongTermMemory/memory.db`
表 `memories`：`id, created_at, user_id, scene, category, tags(JSON), summary, detail, importance, privacy, source`。`scene` ∈ {live, home}，`privacy` ∈ {public, private}，`WAL` 模式。

### 角色图片 `workspace/character_images/manifest.json`
`{"primary": <image_id|null>, "images": [ {id, filename, original_name, label, tags, created_at} ]}`。

## 4. 配置同步机制（务必理解再改配置）

`CharacterService`（角色管理器后端）对 `computer_control`/`vision_mcp`/`context_maintenance`/`context_cleanup` 维护 `config.d/*.yaml` 与 `HomeAgent/config.yaml`（或根 `config.yaml`）的**按修改时间双向同步**：

- `get_config_section(section, home)`：若主配置 `mtime >` 拆分文件，用主配置覆盖拆分文件；反之用拆分文件回填主配置并落盘。
- `save_config_section(section, value, home)`：同时写主配置与拆分文件。
- 所有 `_write_yaml` 走 `sort_keys=False` 的原子临时文件替换，保留 UI 不认识的字段。

**推论**：直接手改 `HomeAgent/config.yaml` 的某节后，下次经服务读取可能被 `config.d` 的旧值覆盖（或反之）。改配置应通过角色工作台，或在两处一并修改。

### 已知的同步漂移（待人工核对）
- `HomeAgent/config.yaml` 的 `computer_control.applications` 含 6 项（含 `网易云音乐`/`cloudmusic`），但 `HomeAgent/config.d/computer_control.yaml` 只有 4 项（缺网易云两条）。二者未对齐，服务下次读取会按 mtime 取其一。建议统一后只保留一处来源或确认哪份为基准。

## 5. 常见坑

- **不要删/改 `src/ai_live_assistant/*`**：它们是 shim，改了也会被 `modules/live/...` 的真实实现覆盖。
- **手改配置后 YAML 校验**：从项目根用 `.venv\Scripts\python.exe -m py_compile ...` 与 `yaml.safe_load`；改 `HomeAgent` 配置要同时考虑 `config.d`。
- **TTS 重复启动**：`/api/options` 超时但 9879 端口存活时，客户端不会拉起第二个 GPT-SoVITS，请勿在此时手动再启动。
- **Codex 权限**：`codex_cli.isolated_home: false` 复用用户现有 `CODEX_HOME`；`bypass_approvals_and_sandbox: true` 使用 CLI 的完全跳过审批与沙盒模式。JSONL 完成事件与必需 MCP 调用仍用于判断执行是否真正完成。
- **私密边界**：直播模型上下文只注入 `include_private=False`；`LIVE_RULES.md`/`HOME_RULES.md` 分离场景行为；私密记忆/附件/照片只允许 `scene=home` 读取。
## 屏幕任务 API（2026-07-22）

- `HomeAgent.analyze_current_screen(question, status=None) -> dict`：全屏临时截图加 MiMo 问答；`question` 必须由当前任务模型生成，不得恢复固定活动描述提示。`_grab_screen_with_retry` 串行抓图并重试 3 次，整屏失败时尝试当前前台 HWND，返回的 PIL 图必须在保存后关闭。
- `HomeAgent._should_route_to_vision(task_plan)`：只读取已验证计划的 `visual_required`，不得重新增加自然语言关键词匹配。
- `_run_tool("ui_analyze_screen", {"question": ...})`：视觉执行循环的全屏观察入口；窗口级后续操作继续使用 `ui_list_windows/ui_analyze_window/ui_click_window/ui_hotkey`。
- Vision 的 `_grab_windows_image` 对目标窗口先用 HWND/PrintWindow，再使用窗口边界截图；`_wait_and_compare_window` 无法取得操作后截图时必须返回 `state_changed=false` 和 `execution_likely_succeeded=false`。
- `read_text_file` 支持 UTF-8、带 BOM 的 UTF-16、文本扩展名的 GB18030；含 NUL 或未知非 UTF-8 扩展名继续按二进制拒绝。返回值包含实际 `encoding`。
- `CommandExecutor.execute("cmd", ...)` 必须用 `shell=True` 的字符串命令路径保留 CMD 内部引号；不得恢复为参数列表，否则 Python 的 `\"` 转义会破坏 `/fi "... eq ..."` 等过滤器。

## 总任务规划 API（2026-07-22）

- `HomeAgent._plan_task(text, context)`：调用 MiMo 输出完整任务判定与执行合同。本地只做字段类型、枚举和结构一致性校验，不允许用原始文本关键词把模型的 `is_task/actionable/domain/site/query/steps` 覆盖回去。
- 代码计划必须包含 `code_scope`。执行器据此选择本工程、外部工程或新项目权限，不再调用关键词分类器。
- `HomeAgent._planner_context(history, limit=8)`：序列化最近用户/助手消息并保留 `source`，供规划器识别主动关怀后的短回复。
- `HomeAgent._should_route_to_web(task_plan)`：只在模型计划同时满足 `is_task=true`、`actionable=true`、`domain=web` 时路由网页能力。
- `_analyze_task` 仅是规划接口不可用时的保守非执行合同，不负责语义识别或站点路由。

## 模型驱动与 Tool 边界（强制）

- 禁止在 `HomeAgent.chat`、`_plan_task`、`_run_tool`、`CodeEditorModule`、Vision 或 Skill 中新增普通任务的关键词分类、正则意图识别、固定动作拆分和站点专用业务流程。
- 模型输出计划，执行模型选择工具；工具只接收明确参数、执行一个原子动作并返回事实。工具实现不能读取整段用户消息后自行判断任务类型。
- `_tools()` 应向执行模型暴露所有已启用能力，不得因本地猜测的任务类型隐藏本可用工具。工具描述说明能力与副作用，不写“遇到某句话必须调用”的路由规则。
- `_plan_task` 对模型计划只做 schema 和一致性校验；矛盾计划直接视为规划失败，不得通过站点 handler、关键词清洗或固定步骤在本地“修正”成另一种业务计划。
- `_normalize_tool_result` 只统一 `status/tool/evidence` 等事实字段，不生成 `next_action`。快捷键、Shell 和界面工具不读取当前业务任务类型来阻止或替换模型指定的操作。
- `HomeAgent._maybe_remember_home` 与直播助手 `_maybe_remember` 在模型不可用时默认不写长期记忆；不得用关键词、消息长度或固定类别词表替代模型的记忆价值判断。
- `ChatWorker.__init__` 必须保持轻量，不能调用 `HomeAgent.begin_task(prompt)`；该调用会经 `CodeEditorModule.begin_tracking()` 扫描工程文件，必须在 `ChatWorker.run()` 的工作线程中执行，避免点击发送后冻结 Qt 事件循环。
- 工具返回至少包含 `status/ok` 与真实结果；涉及状态的工具还要包含对象标识、提交/观察/完成时间和序号。任何分析文本都必须与其 `screenshot_captured_at/observed_at` 绑定。
- 后续操作发生后，旧视觉证据由通用时间规则淘汰。禁止针对网易云、B站或某个按钮手写“标题变化才成功”“固定第一个候选”“固定坐标”等完成条件。
- 模型负责消费工具结果并决定下一步；本地循环只能处理工具协议、取消、异常、证据时序与最大资源边界，不能替模型插入业务动作。
- 独立完成检查由模型读取经过压缩且保留最新项的有效证据。工具不得返回业务 `next_action`；核验模型可以根据任务目标和证据生成下一步建议。
- 新增工具时必须同时补充：清晰名称、单一职责、JSON 参数 schema、结构化返回、时间字段、失败语义、是否产生副作用及对应回归测试。

## MiMo 多轮工具调用约束（2026-07-22）

- 主循环把模型返回的完整 assistant message（包括可选 `reasoning_content` 和 `tool_calls`）加入本轮消息链，并用原始 `tool_call_id` 回传每个工具结果。
- `_is_incomplete_model_response` 拒绝 `length/content_filter/repetition_truncation`，被拒响应中的文本和工具均不得执行。
- `_parse_tool_arguments` 只接受 JSON 对象；解析失败必须生成 `executed=false` 的工具失败消息，不允许用空字典调用工具。
- `MiMoMultimodalClient.verify_completion` 只接受实际布尔类型的 `passed`。本项目按要求不传 `response_format`，继续使用提示词、JSON 解析和本地字段校验。

## 剪贴板图片输入 API（2026-07-22）

- `ClipboardImageTextEdit.image_pasted(QImage)`：Qt 粘贴图片信号；图片粘贴不把富文本写入输入框，文本粘贴继续走父类实现。
- `ChatWorker(..., image_path=None)` 与 `HomeAgent.chat(..., image_path=None)`：图片随队列项进入工作线程，工作线程 `finally` 负责删除文件。
- `HomeAgent._image_message_content(text, image_path)`：验证图片、Base64 上限并返回 MiMo/OpenAI 兼容的 `image_url` 与 `text` 内容数组。
- 当前用户历史仍是纯文本字典；构造 API `messages` 时只替换本轮最后一条用户消息，禁止把数据 URL写回 `self.history`。

## 停止语义与子升级执行合同（2026-07-22）

- `_is_media_stop_plan(plan)` 与 `_allows_application_termination(plan)` 必须互斥：前者控制幂等播放停止，后者仅接受 `close_app/terminate_process` 或对应能力字段。修改规划枚举时必须同步两处安全检查和测试。
- `_run_tool` 在调用 Vision 前拒绝 stop-media 计划中的 Space/Alt+F4，并在 shell/cmd 层阻止仅针对媒体停止的进程终止命令；显式进程终止计划不受此阻止。
- `CodeEditorModule.read_file(path, start_line, max_lines, max_chars)` 用搜索返回的行号读取局部内容。代码循环的只读计数在成功写入/替换后清零，验证成功才设置 `current_code_verified`。该模块只执行文件操作与验证，不读取用户自然语言，也不判断是否为代码任务。
- `_codex_exec_command` 的最后一个参数固定为 `-`，完整提示通过 asyncio 子进程 stdin 写入。自升级失败必须调用 `SelfUpgradeManager.fail`；`status=failed` 的恢复文件只保留诊断，不会由 `resume_prompt()` 重放。
- `finalize_task_recovery` 对自升级实行 fail-closed：没有写入并通过代码验证的证据时不得清除为成功、触发重启或声称升级完成。
