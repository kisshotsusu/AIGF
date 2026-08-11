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

## 开机自动启动打招呼契约

- `HomeAgent/home_modules/system_startup.py` 提供 `greeting_enabled(config)` 与 `greeting_text(config)`：前者判断 `system_startup.greeting_on_startup`（默认真），后者返回 `system_startup.greeting_text` 或默认欢迎语。二者独立于 Qt，可单测。
- 开机自启动登记统一走 `configure_system_autostart(enabled, launcher, *, startup_target=None, runner=None) -> list[str]`：同时返回并（当传入 `runner` 时）执行注册表 Run 键与任务计划程序两条命令，并始终调用 `set_windows_autostart` 写启动文件夹。命令构造函数 `registry_autostart_command(enabled, launcher)` 与 `scheduled_task_command(enabled, launcher)` 纯字符串、可单测；`runner` 用于注入命令执行器以便测试。常量 `REGISTRY_RUN_KEY`、`REGISTRY_VALUE_NAME`、`SCHEDULED_TASK_NAME`。
- 注册表项为 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\HomeAgent`（`reg add/delete`，无需管理员）；任务计划为登录触发 `HomeAgentAutostart`（`schtasks /create /tn ... /sc onlogon /rl limited /f`，创建需管理员权限，非提权环境会访问被拒并跳过）。设置页保存时调用 `configure_system_autostart(startup["enabled"], HOME_AGENT / "启动家庭Agent.bat")`。
- `AutostartGreetingWorker(QThread)`（在 `qt_app.py`）在独立线程中延迟约 3 秒后调用 `asyncio.run(agent._speak_with_fresh_session(text))` 播放打招呼；失败只发 `failed` 信号并记日志，不阻塞 Home Agent 启动。
- 打招呼仅在 `run()` 检测到 `AUTOSTART_ARGUMENT` 且 `greeting_enabled` 为真时启动；手动启动绝不触发。`greeting_text` 默认"主人，早上好呀，苏苏已经准备好陪你了。"

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
- `begin_task` 只清除取消标志并调用 `SelfUpgradeManager.begin_tracking()` 建立代码变更基线，不创建恢复文件。`HomeAgent.chat` 只有在模型计划为 `domain=code`、`code_scope=self` 时才调用 `SelfUpgradeManager.begin(..., track_changes=False)` 创建自升级恢复状态。`update_task_recovery`、`finalize_task_recovery` 和 `recover_interrupted_task` 只处理该自升级状态；`stop_current_task` 会终止当前活跃子进程但保留常驻服务。
- `SelfUpgradeManager.clear()` 是完成/取消状态的唯一清理入口。`resume_prompt()` 只能恢复 `running`；`restart_pending` 是已完成升级的进程接力标记，读取后必须清理并返回空字符串，禁止再次提交原任务。
- `CodeEditorModule._resolve_read_path` 与 `_resolve_edit_path` 负责路径规范化；自我修改可访问整个仓库源码，`computer_control.full_access` 授权外部绝对路径读写。外部结果返回规范绝对路径，写入后加入 `_external_changed` 并参与语法校验。
- `log_event(event, **data)`：写 `HomeAgent/logs/agent-events.jsonl`，密钥按正则脱敏（`bearer ...` / `sk-...` 截断为 `***`），单字段 ≤4000 字符。
- 工具循环收集最近工具返回作为 `completion_evidence`；执行类任务生成候选答案后调用 `MiMoMultimodalClient.verify_completion`。只读观察/查询在成功证据已包含所问信息时即通过，不额外要求被观察对象达到终态；变更/交互任务仍必须有可验证终态。失败时把 `reason/next_action` 作为新一轮指令，超过 `completion_max_retries` 后返回明确未通过而不是成功措辞。

### `MiMoMultimodalClient`（`HomeAgent/home_modules/mimo_multimodal.py`）
- `analyze_image(session, path, prompt)`：图片编码为 data URL，通过 `chat/completions` 的 `image_url` + `text` 内容调用 `mimo-v2.5`。
- `analyze_image_auto(session, path, prompt)`：优先走 DeepSeek 视觉代理（`deepseek_image_enabled` 时），失败自动回退 MiMo；结果带 `provider`，全部失败抛出带各服务原因的 `RuntimeError`。
- `analyze_image_with_deepseek(session, path, prompt)`：DeepSeek 官方 API 为纯文本模型，先经 `_describe_image_with_proxy` 用视觉代理（默认 MiMo，可配 `deepseek_image_proxy_*`）把图片转成文字描述，再把描述与问题交给 `deepseek-chat` 推理；返回 `{ok, text, model, vision_model, description, path, provider:"deepseek"}`。
- `describe_images_for_chat(session, image_paths, user_text)`：把每张图片经视觉代理转成文字描述，返回 text-only 内容数组，供 DeepSeek 等纯文本聊天模型“看图”使用。
- `_post` 对 `Authorization` 头自动加 `Bearer` 前缀；MiMo 的 `api-key` 头保持原样。
- `transcribe_audio(session, path, language)`：只接受 WAV/MP3，Base64 后不超过 10 MB，通过 `input_audio` 和 `asr_options.language` 调用 `mimo-v2.5-asr`。
- `verify_completion(session, task, plan, answer, evidence)`：要求模型只返回 `{passed, reason, next_action}`；核验依据是工具证据，默认接口异常关闭成功路径。请求固定 `thinking.type=disabled`、`stream=false`，不设置 `response_format`；空响应错误必须包含 `finish_reason`。

### 工具循环消息顺序约束（2026-08-07）
- OpenAI 兼容接口要求：assistant 消息带 `tool_calls` 时，其后的 tool 消息必须连续紧跟（DeepSeek 会以 “insufficient tool messages following tool_calls message” 拒绝）。
- `chat()` 工具循环把轮内 `post_tool_instruction`（如“连续8次只读检查”）先收集到 `round_post_tool_instructions`，等该轮全部 tool 消息追加完后再统一插入 system 消息，避免插在 tool 消息之间。
- 单次 `_run_tool` 出现未预期异常时，转为 `{"status":"failed","error":"工具执行异常…"}` 的 tool 消息继续循环，而不是中断会话。

### HomeAgent 图片消息与供应商能力
- `HomeAgent._provider_supports_images(provider)`：判断供应商能否直接接收图片（MiMo 或显式 `supports_images: true`）；DeepSeek 官方 API 为纯文本模型，返回 `false`。
- `chat` 带附件时：供应商支持图片则构造 `image_url + text` 消息；纯文本供应商（DeepSeek）调用 `_text_only_image_message`，经 `describe_images_for_chat` 把图片转成文字描述后提交，代理失败退化为只列文件名的文本消息（`image_vision_proxy_failed` 事件）。
- `chat` 带附件时会把每张图片的**绝对路径**注入系统提示（【本次消息附带的图片】），供执行模型直接传给 `comfy_edit_image` 的 `image` 参数；纯文本回退消息也带完整路径，不再只有文件名。
- `analyze_image` 工具与 `analyze_current_screen`（`ui_analyze_screen`）改用 `analyze_image_auto`，按配置优先 DeepSeek 并自动回退 MiMo；`proactive_screen_care` 仍固定使用 MiMo `analyze_image`。
- `comfy_edit_image` 工具支持已登记角色图别名（`primary`、`角色三视图`、`正面照片`、文件名/标签），与 `analyze_image` 一样先经 `_resolve_character_image` 解析，再交给 ComfyUI 编辑。
- 绘图路由按“有无参考图”执行：有用户附件、角色设定图（`primary`/角色三视图/正面照片）、或上一轮生成结果时，必须 `comfy_edit_image` 并把路径/别名/上一轮 `media.path` 传给 `image`；多张参考图按“设定图改姿势→结果图改背景”分步串联；只有完全无参考图才允许 `comfy_generate_image`。用户给的风格词、构图词、负面词必须原样传入 `prompt` / `negative_prompt`。

### `CodeEditorModule` 变更与验证
- 跟踪范围覆盖整个仓库中的源码、配置、README 与 `AI Read`，不再依赖固定模块目录清单。
- `validate_current_changes` 检查真实变更以及 Python/YAML/JSON 等文件语法，不会因为缺少 `AI Read` 或项目 README 变更而人为失败。
- 执行模型仍应按实际影响维护文档，但这是交付判断，不是代码工具内部的任务类型硬编码。

### `CodeValidator`（`HomeAgent/home_modules/code_validator.py`）
- 独立验证模块，不包含编辑、变更追踪或任务恢复；`CodeEditorModule` 在 `__init__` 创建 `self.validator` 并委托 `validate_files` / `run_autonomous_tests` / `git_diff_check`。
- `validate_files(changed)`：按扩展名执行语法/结构检查——Python `py_compile`、JSON `json.loads`、YAML `safe_load`、TOML `tomllib`、INI `configparser`、JS `node --check`（无 node 时跳过）、HTML 根标签、CSS 大括号平衡；未知扩展名跳过。任一失败返回 `{ok:false, checked, error}`。
- `validate_repo()`：运行 `git diff --check`；非 git 仓库返回 `{ok:true, skipped:true}`。
- `run_autonomous_tests(changed, timeout)`：按变更检测工程——非 Projects 的 Python 先 `py_compile`；`Projects/<项目>` 内运行 `compileall`、pytest/unittest、node `--check`、静态 HTML/CSS 检查、`npm test`、`tsc --noEmit`；HomeAgent 变更跑 `HomeAgent/tests` 全套，`modules/live/` 变更跑直播测试。返回 `{ok, commands, failed, changed, error}`。
- `code_validate_project` 工具与 Codex 代码任务完成门禁现在同时要求：`validate_current_changes` 通过、`git_diff_check` 通过、`run_autonomous_tests` 通过。

### `ComfyUIClient`（`HomeAgent/home_modules/comfyui_client.py`）
- 独立 ComfyUI 生成模块，主程序不包含任何 ComfyUI 调用细节；`HomeAgent.__init__` 用根 `config.yaml` 的 `comfyui` 节创建 `self.comfyui`。
- `ensure_running()`：探测 `/system_stats`；未运行且 `auto_start=true` 时按桌面版参数拉起（`.venv` Python + `--extra-model-paths-config` + 共享 input/output 目录），最多等待 `startup_timeout_seconds`。
- `generate_image(prompt, negative_prompt, model, width, height, steps, cfg, seed, use_lora)`：`qwen-image-2512`（写实）或 `anima`（动漫）API 工作流；尺寸自动吸附到模型支持分辨率。
- `_enrich_prompts(preset, prompt, negative_prompt)`：自动在正向提示词后追加预设质量/风格后缀（`positive_suffix`，如 masterpiece/clean lineart），并始终应用默认负面提示词（`negative_default`）；用户传入的负面提示词会与默认合并。模块配置可用 `positive_suffix` / `negative_prompt` 全局覆盖。
- `edit_image(image_path, prompt, ...)`：上传图片到 `/upload/image`，用 `TextEncodeQwenImageEditPlus` + VAEEncode 构造编辑工作流。
- 编辑步数策略（`_edit_steps`）：启用 Lightning LoRA 时用其原生 4 步，避免高步数过度处理；关闭 LoRA 时用 20 步并强制下限 `min_steps=8`。`edit_image` 新增 `denoise` 参数（0.3～1.0，默认 1.0），越低越贴近原图。
- `generate_video(prompt, model, width, height, frames, steps, fps, first_frame, use_int8)`：MiniMax-H3 `MiniMaxH3ImageToVideo` 管线（SigmaShift + SamplerCustomAdvanced + VAEDecode/VAEDecodeAudio + CreateVideo + SaveVideo）；`first_frame` 非空时自动图生视频，默认视频超时 1800 秒。
- 视频模型自动探测与回退：`_available_diffusion_models()` 查询 ComfyUI 实际注册的 diffusion_models，nvfp4 缺失时自动改用 int8（`_select_video_unet`）；`list_models` 返回的预设带 `available` 标记；若校验仍报 `value_not_in_list`（unet_name），会自动用另一版本重试一次。
- 输出通过 `/history/{id}` 轮询、`/view` 下载到 `outputs/comfyui/`，返回 `{ok, status, prompt_id, media:[{path, kind, caption}], message}`；`media` 由工具循环收集并经 `media_ready` 回调交给 Qt 界面。
- `chat(..., media_ready=None)`：`_run_tool` 返回的 `media` 列表被去重收集，在最终答案发布前调用 `_publish_media` 推送；`qt_app.Bridge.media` 信号驱动 `MediaBubble` 渲染（图片缩略图可点击、视频卡片可打开）。
- **已知坑（已修复）**：`__init__` 中实例属性 `self._object_info = None` 与同名异步方法 `_object_info()` 冲突，实例属性遮蔽方法，导致 `list_models()` 调用 `self._object_info()` 抛 `'NoneType' object is not callable`。缓存属性已改名为 `self._object_info_cache`，方法保持 `_object_info()`。改此模块时不得再用 `self._object_info` 作为属性名。

### ComfyUI 技能（`Skill/comfyui-image-video/`）
- `SKILL.md`：使用契约、模型预设表、输出位置、调用要点与“防鬼图规范”（先分析参考图→按模板写提示词→AI 智能填充负面词→选稳定参数→生成→`analyze_image` 验收；内置美术优化/改姿势模板、负面词库和验收提示词模板；明确本地视觉只用于桌面点击、验收只能走 MiMo）；`scripts/comfy_cli.py` 提供 `status/models/generate-image/edit-image/generate-video` 命令行入口。
- 编辑预设内置防鬼保险：`qwen-image-edit-2511` 的 `positive_suffix` 含 single subject / clean lineart / no jagged edges / no pixelation / no stray lines，`negative_default` 含线条断裂、锯齿、像素化、多余线条、杂线、细红线、重复主体、多余人物、双人、无关物体等；用户提示词与默认词自动合并。
- ComfyUI 生成验收铁律：工具循环中，若任务计划含 `comfy_generate_image/comfy_edit_image/comfy_generate_video`，操作契约强制生成后用 `analyze_image`（MiMo）验收 `media.path`，不通过只允许改提示词/参数重试一次；本地视觉（`ui_analyze_screen` 等）只用于桌面点击操作，禁止用于生成质量判断。

### `CosyVoiceTTS`（`HomeAgent/home_modules/cosyvoice_tts.py`）
- 独立 CosyVoice2 情绪 TTS 模块；`HomeAgent.__init__` 用根 `config.yaml` 的 `cosyvoice` 节创建 `self.cosyvoice`。
- `ensure_running()`：探测 `/openapi.json`；未运行且 `auto_start=true` 时用 `.venv` Python 启动 `runtime/python/fastapi/server.py --port 50000 --model_dir ...`，日志在 `E:\OtherProgram\CosyVoice\logs\`。
- `parse_stage_directions(text)`：剥离全角/半角括号里的语气描写，按 `MOOD_HINTS` 映射表生成 `instruct_text`（如“请用语气温柔、声音黏腻绵软、带微微喘息、语速缓慢的语气说这段话”）；无括号时原样返回。
- 情绪指令防机械感：情绪词最多保留 4 条 + 1 条语速提示（共 ≤5），并消除“语速缓慢/稍快”冲突；提示词堆叠过多会让 CosyVoice2 产出机械感。
- `synthesize(text, instruct_text, reference, filename_prefix)`：调用 `/inference_instruct2`（multipart：`tts_text` + `instruct_text` + `prompt_wav`），流式返回 16bit PCM，封装为 24kHz 单声道 WAV，保存到 `outputs/cosyvoice/`，返回 `{ok, media:[{path, kind:"audio"}], instruct_text, directions, reference, duration_seconds}`。
- `list_reference_audios()`：枚举 `outputs/cosyvoice_refs/` 下的 wav/mp3/flac/ogg 作为参考音色；`reference` 支持文件名或绝对路径，默认取第一个。
- 音频媒体（`kind:"audio"`）与 image/video 一样经 `media_ready` 回调进入 Qt 媒体气泡（音频卡片可点击打开）。

### CosyVoice2 本地服务（`E:\OtherProgram\CosyVoice`）
- 官方仓库 Python 3.12 venv；torch/torchaudio 为 `2.10.0+cu130`（适配 RTX 5070 Ti）；依赖按 Windows 裁剪（去掉 torch/torchaudio/tensorrt/deepspeed/wetext/grpcio 固定版本，安装 torchcodec、pyworld 0.3.5 等 cp312 轮子）。
- 两处官方代码兼容补丁（升级/重装时需重新应用）：
  1. `cosyvoice/utils/file_utils.py::load_wav`：改用 `soundfile.read`（含 `seek(0)` 以支持文件对象重复读取），避免 torchaudio 2.10 强依赖 torchcodec；
     并增加多声道均值转单声道（`speech.mean(dim=0, keepdim=True)`），保证立体声参考也能正常推理。
  2. `runtime/python/fastapi/server.py`：zero_shot/cross_lingual/instruct2 端点把上传音频读入 `io.BytesIO` 后直传模型，不再预加载成 16k 张量（当前 frontend 期望原始音频源），避免 FastAPI 关闭上传文件导致惰性生成失败。
- 服务缺省端口 50000；模型 `CosyVoice2-0.5B` 位于 `pretrained_models/`。

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
- `resolve_input_settings(sd, device, requested_rate, requested_channels, dtype)`：依次验证首选采样率、设备默认采样率和常见采样率；配置设备不可用时再验证系统默认输入。Qt、Tk 和 Sound MCP 必须用返回的真实 `sample_rate/channels` 创建流并写 WAV，禁止仍用配置中的 16 kHz 伪造 WAV 头。
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
- `SelfUpgradeManager.resume_prompt()` 只恢复 `is_self_upgrade=true` 的 `running` 状态；旧版本遗留的普通/定时任务状态直接清除。`create_scheduled_task` 始终通过 `TaskStore(ROOT / "Task")` 写入独立 JSON，不能写入或依赖 `task-recovery.json`。
