# 开发者参考（细化补充）

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
- `handle_event(event)`：按 `cmd` 分流 `DANMU_MSG` / `SEND_GIFT` / `INTERACT_WORD`+`ENTRY_EFFECT`。
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
- `recent_live_conversations(limit)`：从 `logs/messages.jsonl` 读成功回复，供家庭模式共享近期对话。

### 管理后台（`modules/live/manager.py`）
- `create_app()`：aiohttp 路由表见 `03_INTERFACES_AND_CONFIG.md`。`put_config` 会丢弃传入的 `llm/tts/image_generation/memory_write/workspace`，用磁盘当前值回填；`put_secrets` 只接受 `BILIBILI_COOKIE`。
- `start_assistant` 子进程：`python -m modules.live.main --config <ROOT>/config.yaml`，stdout 接 `logs/assistant.log`。

### HomeAgent（`HomeAgent/agent.py`）
- 运行期读取 `HomeAgent/config.yaml`、`config.yaml`、`workspace`、`Task`、`LongTermMemory`；`__init__` 后台线程 `ensure_vision_service` / `ensure_sound_service` 自动拉起 MCP。
- `begin_task` / `update_task_recovery` / `finalize_task_recovery` / `recover_interrupted_task` / `stop_current_task`：围绕 `SelfUpgradeManager` 做任务持久化与重启恢复；`stop_current_task` 会 `taskkill` 当前活跃子进程但保留常驻服务。
- `log_event(event, **data)`：写 `HomeAgent/logs/agent-events.jsonl`，密钥按正则脱敏（`bearer ...` / `sk-...` 截断为 `***`），单字段 ≤4000 字符。

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
- **Codex 隔离**：`codex_cli.isolated_home: true` 使用独立 `CODEX_HOME`；校验 JSONL 完成事件与必需 MCP 调用。网络任务不得把 Codex 当首选。
- **私密边界**：直播模型上下文只注入 `include_private=False`；`LIVE_RULES.md`/`HOME_RULES.md` 分离场景行为；私密记忆/附件/照片只允许 `scene=home` 读取。
