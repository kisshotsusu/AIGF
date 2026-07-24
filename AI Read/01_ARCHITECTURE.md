# 设计架构

## 分层

```text
呈现层
  HomeAgent PySide6 桌宠 | CharacterManager PySide6 | 直播 Web 控制台
        │
应用层
  HomeAgent                 LiveAssistant                 CharacterService
  任务计划/工具循环          直播事件/回复/欢迎/礼物         配置/身份/记忆原子读写
        │                         │
能力层
  Vision MCP | Sound MCP | Skill | Codex | LLM | GPT-SoVITS
        │
数据层
  config.yaml | HomeAgent/config.yaml | workspace | LongTermMemory | state | logs
```

UI 不直接拥有业务数据。角色管理器 Qt/Tk 前端经 `CharacterService` 访问配置；HomeAgent Qt 界面经 `HomeAgent` 接口执行任务、接收进度和保存状态。

## 直播链路

```text
B站 WebSocket / 历史弹幕
  -> BilibiliLive
  -> LiveAssistant.handle_event
  -> 用户名过滤 / 去重 / 冷却 / 事件分类
     ├─ 弹幕 -> LLMClient -> 截短回复
     ├─ 送礼 -> 模板回复
     └─ 进场 -> 去重后的欢迎语（最高语音优先级）
  -> speech PriorityQueue（单消费者）
  -> TTSClient（单 GPU 合成锁 + 暂态错误退避重试）
  -> Windows 顺序播放
  -> messages.jsonl / live-context / memory
```

可靠性约束：

- 事件处理不等待 GPU 合成，不因一段音频阻塞后续 B站事件接收。
- `INTERACT_WORD`、`INTERACT_WORD_V2`、`ENTRY_EFFECT` 与 `ENTRY_EFFECT_MUST_RECEIVE` 同用户并发时通过 `_welcoming` 合并；WebSocket 鉴权复用 Cookie 的 uid/buvid，直播主进程由跨进程文件锁限制为单实例。
- 只有语音成功才写 `welcomed` 冷却；最终失败保留错误日志，允许后续事件再试。
- `/api/options` 短暂超时但 9879 端口仍存活时不重复启动 GPT-SoVITS，避免显存被第二个进程占用。
- TTS 默认 4 次尝试、指数退避；合成和播放严格串行，避免声音重叠。

## HomeAgent 链路

```text
文字 / 剪贴板截图 / Sound MCP
  -> HomeAgent.chat
  -> MiMo 总规划：是否为任务、回答/执行/追问、执行策略、能力、工具、步骤、成功条件
  -> 路由
     ├─ 本地确定性工具
     ├─ web-agent-operator
     ├─ Vision MCP（DOM 或视觉）
     ├─ 普通 LLM 工具循环
     ├─ CodeEditorModule（本地读写 / 自身代码 / Projects / 自动测试）
     └─ Codex CLI / 其他 MCP（与本地工具并列，由模型按任务选择）
  -> 操作后观察与本地证据收集
  -> MiMo 独立完成检查（失败原因回灌工具循环，默认最多 2 次）
  -> Qt 实时进度 / 长任务 TTS 汇报
  -> 家庭历史 / 高价值记忆 / task-recovery
```

语义规划器是普通消息的唯一任务意图与执行路线来源。本地代码不得使用网页、站点、屏幕、代码、音乐等关键词或正则覆盖模型结论，也不得手写任务类型、目标抽取、步骤拆分或站点状态机。代码任务由计划中的 `code_scope` 区分本工程、外部工程和新项目。直接重启、停止和单实例锁仅属于程序生命周期控制，不得演变为普通业务任务分类。

执行模型持有完整工具表，读取每个原子工具的结构化结果后自行决定下一次调用。工具层只做操作、格式校验、时间记录和事实回传，不解释用户意图、不判断业务完成、不指定固定后续流程。最终是否完成由独立模型基于最新有效证据判断。

剪贴板图片由 Qt/Tk 输入层保存为系统临时 PNG；Qt 可在同一轮维护多张独立附件，规划上下文只记录附件数量。执行请求把全部图片按顺序编码为本轮多个 `image_url` 数据项直接交给 MiMo。Base64 和临时路径不进入持久历史，任务完成、取消、单张移除或窗口退出后删除对应临时文件。

HomeAgent 运行期间还有一条独立的低优先级关怀链路：Qt 每 300 秒触发一次后台任务，截取主屏幕到系统临时文件，交给 `MiMoMultimodalClient` 的 `mimo-v2.5` 生成一句不暴露屏幕隐私的问候或关心，然后立即删除截图。用户任务执行中或上一轮尚未结束时跳过本轮，不排队、不抢占正常对话；生成结果显示在对话区和桌宠旁的非抢焦点消息气泡中，并服从 `home.auto_speak` 与 `screen_care.speak` 播报。

Qt 文字和语音识别输入共用一个进程内 FIFO。当前 `ChatWorker` 执行任务或播放最终 TTS 时，新输入仍可提交并立即显示；队列保持单消费者，只有当前 worker 连同语音播放真正结束后才启动下一项，避免多个任务同时改写 Agent 状态或争用语音。停止操作只取消当前任务，后续队列继续等待；重启请求不会在旧进程中启动下一项。

“重启自己/HomeAgent/桌宠”属于本地控制指令，在 `HomeAgent.chat` 进入任务规划、记忆和大模型请求前确定性拦截。Qt 与 Tk 前端都复用现有重启看门狗：先显示本地确认消息，再退出当前进程，由看门狗等待旧 PID 消失后启动新实例。
直接重启也在 `begin_task` 之前绕过任务恢复持久化，且任务收尾显式清理状态；不能使用布尔短路跳过清理。启动读取到旧版本遗留的“重启自己”任务时直接删除，不生成恢复提示。

模型负责语义，执行器负责机械可靠性。站点名称、歌曲名、收藏夹名等目标不应靠不断增加正则硬编码；执行器可以验证必填参数、权限、范围和业务返回值。

图片理解和语音识别共用 `MiMoMultimodalClient`：图片使用 `mimo-v2.5`，ASR 使用 `mimo-v2.5-asr`。任务完成检查只消费本地工具证据，不能把候选回复本身当作成功证明；核验失败会继续执行，接口异常时默认 fail-closed。

## 浏览器与视觉决策

`inspect_active_target` 将目标分成：

- `browser_dom`：现有 Chrome/Edge 暴露 CDP，直接 `web_read`、`web_fill`、`web_click_text`。
- `browser_visual`：浏览器存在但没有 CDP；保留该浏览器，使用窗口截图和 GUI-Actor。
- `desktop_visual`：当前程序不是网页；使用 Windows 窗口/桌面工具。

所有视觉动作执行后等待默认 550 ms，重新截图比较状态。下一轮必须读取变化证据再决定继续、替代点击还是失败退出。GUI-Actor 为懒加载，不在服务启动时预占模型显存。

## 网页任务状态机

```text
NAVIGATED
  -> SEARCH_SUBMITTED
  -> RESULTS_VERIFIED
  -> RESULT_SELECTED
  -> ACTION_EXECUTED
  -> FINAL_VERIFIED
```

每个状态最多尝试有限替代方案；失败必须返回失败阶段和证据。依赖登录态的 B站收藏夹优先使用真实收藏夹数据与现有浏览器导航，不能根据页面视觉顺序猜视频。

## 数据与隐私边界

- 身份和人格共享；`LIVE_RULES.md` 与 `HOME_RULES.md` 分离场景行为。
- 私密记忆、家庭附件和私人照片只允许家庭模式读取。
- 直播短期上下文、每日记忆、SQLite 长期记忆是三个不同生命周期，不互相当作备份。
- 设置保存采用原子临时文件替换，UI 必须保留未知字段。

## 故障隔离

- Vision、Sound、GPT-SoVITS 是独立本地服务；单个服务失败应返回可诊断错误而不是让 UI 假死。
- Codex 复用用户 `CODEX_HOME` 并以跳过审批与沙盒模式运行；本地工具与 Codex 均由模型直接选择，JSONL 完成事件和实际测试仍用于确认是否真正完成。
- 自主升级通过持久化任务状态、重启次数限制和重启前校验恢复任务。
- 任务恢复文件只表示真正未完成的 `running` 任务：普通任务完成或取消后立即删除；自升级完成后的 `restart_pending` 只用于跨进程标记“等待加载新代码”，新进程启动时清理且绝不重新提交原任务。
- 自升级意图覆盖“升级自己/自己的代码/自动升级”等自然表达；代码任务必须产生真实变更并通过 `code_validate_project`，文本形式的伪 `<tool_call>` 会被拒绝且不会进入显示成功或 TTS 链路。
- 直播、弹幕、B站、角色管理器、Vision、Sound 等工程组件的“失效/修复/修改”请求同样属于自身代码任务，直接进入本地 `code_*` 工具链，不能误走桌面 UI 工具循环。
- 工具循环的 28 轮现在是“失败轮次预算”：成功读取、搜索、检查或写入不消耗预算；工具失败、状态不确定、伪工具调用、证据不足或未验证结束才累计一次。另有默认 112 次总迭代安全上限防止无失败但无进展的无限循环。
- `computer_control.full_access` 开启时，代码列举、读取、搜索、写入和替换均可使用用户指定的绝对路径；仓库内自我修改不受预设模块目录限制，外部写入仍进入变更跟踪和文件语法校验。
- 自编程把实现、测试和必要文档视为同一交付内容，但 `CodeEditorModule` 不再用“本轮必须修改 AI Read”作为代码验证硬门槛。
## 模型驱动屏幕任务（2026-07-22）

`HomeAgent.chat` 不再用“看看我在做什么”等固定短语进入固定截图回答。所有请求先交给语义规划器，模型输出 `visual_required`、`interaction_mode`（`observe/solve/game`）、步骤和成功条件；本地循环再调用 `ui_analyze_screen`、窗口观察、点击和按键工具。观察模式只读，做题模式先识别并推理，游戏模式每次操作后必须重新读取最新画面。
