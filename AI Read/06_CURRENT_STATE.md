# 当前实现状态

核对日期：2026-08-10。本文件只记录当前有效行为，不保存逐次修复流水；历史问题应从 Git 和运行日志查询。

## 任务理解与执行

- 普通消息由 MiMo 语义规划器生成结构化计划，本地代码不再用关键词重新判断网页、桌面、文件或代码任务。
- 任务类型、目标参数、执行步骤、工具选择、工具结果解释和下一步均由大模型负责；工具层只执行原子操作、记录时间并返回结构化事实，禁止新增手写任务解析和站点固定流程。
- 代码计划包含 `code_scope`：`self`、`external`、`new_project` 或 `none`。它决定代码工具的可写范围以及是否进入自升级恢复流程。
- 规划器不可用时使用不执行外部动作的保守回退，不通过关键词猜测任务。
- 桌面和网页操作由模型组合窗口枚举、画面分析、点击、输入、快捷键及终态验证等原子工具。
- 已删除旧网易云/B站固定流程、组合式网易云搜索播放工具、Codex 触发关键词和代码任务关键词分类器。
- 规划结果只做字段类型、枚举和跨字段一致性校验；本地代码不再按 `site/operation` 改写 handler、搜索词、执行策略或步骤。
- 所有已启用的代码工具持续向执行模型开放，不再根据本地 `current_code_task` 分类隐藏；Tool 描述只说明能力、参数、副作用和返回值。
- 工具结果规范化不再注入业务 `next_action`，视觉工具也不再根据媒体任务类型阻断快捷键或命令；模型结合最新证据自行决定后续操作。
- 家庭与直播长期记忆的“是否值得保存、类别和摘要”均由记忆模型判断；已移除 `always_keywords`、`ignore_keywords`、消息长度和“生日/喜欢/讨厌”等本地语义分类兜底。
- Codex 与本地工具并列开放，由模型根据任务选择，不由用户消息中的单个词自动触发，也不要求先耗尽本地工具。
- 直接重启、停止当前任务和实例锁属于程序生命周期控制；普通业务任务不得新增确定性分类或固定业务步骤。
- 工具循环保证单轮 assistant `tool_calls` 后 tool 消息连续（轮内提示词延迟到该轮末尾统一插入），避免 DeepSeek 的 “insufficient tool messages” 400 拒绝；单工具异常转为失败 tool 消息继续循环。

## Home Agent

- 使用 PySide6 无边框聊天窗口和桌宠，可拖动、边缘/四角缩放、收起与展开。
- 输入区支持粘贴和手动选择多张图片或文件；预览区无附件时隐藏，每张图片可独立预览和移除。
- 语音输入不再以固定 16 kHz 强行打开麦克风。当前设备 21 的 WASAPI 原生采样率为 48 kHz，录音会自动协商到 48 kHz并按真实采样率保存，识别阶段再重采样；设备 ID 失效时回退默认输入，避免 PortAudio `-9997 Invalid sample rate`。
- 点击发送后立即更新界面并启动 `ChatWorker`；任务恢复初始化和代码变更基线扫描在工作线程执行，不再由工作线程构造函数阻塞 Qt 主线程。
- 忙碌时新输入进入 FIFO 队列；重启过程中不启动下一项。
- 任务活动卡仅展示摘要、计划、最近八条工具活动和最终状态，不回显原始窗口 JSON 或屏幕内容。
- 消息气泡与聊天消息在 TTS 播放前显示；GPT-SoVITS 失败时才降级 Windows SAPI。
- 主动屏幕关怀可启停并配置频率，任务忙碌时跳过，不排队抢占。

## 视觉、网页与媒体

- 视觉任务只由计划中的 `visual_required` 和 `interaction_mode` 决定。
- DeepSeek 图像识别采用视觉代理方案：`analyze_image` 工具、屏幕分析和带图聊天会优先把图片交给视觉代理模型（默认 MiMo）转成文字描述，再由 DeepSeek `deepseek-chat` 推理；DeepSeek 失败时自动回退 MiMo 直接分析。聊天供应商为纯文本模型时，附件图片不再直接发送给聊天模型。
- 截图和窗口操作带提交、采集、完成时间；较新的同对象证据覆盖旧证据。
- Vision 窗口识别会记录截图时与返回时的窗口标题和画面变化；分析期间状态已变化的结果会立即废弃。完成核验还会淘汰被后续操作覆盖或距当前超过配置时限的视觉证据，并始终优先保留最新证据。
- 是否存在搜索对象由规划模型输出的 `query/query_is_explicit` 决定；本地代码不从“播放音乐”等原始文本重新生成搜索词。
- 音乐播放完成状态由任务后的最新视觉证据核验；窗口标题无需发生变化。若目标歌曲已在播放则幂等完成，不会为了制造标题变化而停止或重复播放。
- 网易云等原生程序文本输入优先绑定目标窗口，但活动窗口输入和 Enter 均可正常使用；不会用平台安全门槛阻断常规操作，结果由操作后窗口与新截图验证。
- 停止媒体使用幂等 `media_stop`，不会用 Space 切换状态，也不会把停止播放变成退出应用。
- B站等网页任务由模型组合通用 DOM、窗口和导航工具执行；不再暴露收藏夹专用组合工具或站点专用完成门槛。

## 文件、代码与自升级

- 普通文档和角色资产使用文件工具读取、原子写入并重新读取验证，不进入代码验证。
- 本地代码工具支持目录枚举、按行读取、搜索、原子写入、精确替换和自动测试；代码验证由独立的 `home_modules/code_validator.py` 模块执行。
- `code_validate_project` 与 Codex 代码任务完成门禁包含三层验证：文件语法检查、`git diff --check` 静态检查和项目自动测试；任何一层失败都会进入自主修复或明确失败，不把无验证结果当作成功。

## ComfyUI 图像与视频生成

- HomeAgent 通过 `home_modules/comfyui_client.py` 调用本地 ComfyUI（`http://127.0.0.1:8188`）；服务未运行时按桌面版相同参数自动启动，逻辑全部封装在独立模块中，主程序只做薄委托。
- 支持 `comfy_generate_image`（Qwen-Image-2512 写实 / Anima 动漫）、`comfy_edit_image`（Qwen-Image-Edit-2511）、`comfy_generate_video`（MiniMax-H3 文生视频/图生视频，带音频），以及 `comfy_status` / `comfy_list_models`。
- 视频生成防死循环：MiniMax nvfp4 模型缺失时自动回退 int8（`list_models` 带 available 标记）；工具与规划器明确“视频只能用 comfy_generate_video、失败就报告并停止、禁止改用画图工具冒充视频”；本机 16GB 显存下建议 frames≤24、steps≤10。
- 图像/编辑生成会自动追加质量与风格正向提示词（masterpiece、highly detailed 等），并始终应用预设负面提示词（Anima 用英文质量标签，Qwen 用中文画质/畸形/水印描述）；用户显式传入的负面提示词与默认合并，模块配置可全局覆盖。
- 角色绘图采用“设定图优先”流程：`comfy_edit_image` 支持 primary/角色三视图/正面照片等别名，Agent 被引导先用设定图改姿势、再用结果改背景，避免文生图导致角色细节丢失或多出无关主体。
- 绘图路由按“有无参考图”执行：用户粘贴的附件路径会注入系统提示，有参考图/角色设定图/上一轮输出时一律 `comfy_edit_image`（`image` 用绝对路径、角色别名或上一轮 `media.path`），多张参考图分步串联，只有完全无参考图才允许 `comfy_generate_image`；用户风格词与负面词必须原样传入。
- 防鬼图逻辑：skill 内置“先分析→按模板写提示词→选参数→生成→验收”五步规范与美术优化/改姿势提示词模板；负面词由 AI 智能填充（用户未给时自动补画质、脸部/手部/肢体、重复主体、多余人物、水印等，禁止留空）；编辑预设自动追加 single subject / clean lineart / no pixelation / no stray lines 正向词和线条断裂/锯齿/杂线/重复主体等负面词；编辑优先 4 步 Lightning LoRA（16GB 显存下 20 步无 LoRA 实测会中断/OOM）。
- 图像/视频生成验收**只能**走 `analyze_image`（MiMo 视觉 API），验收模板检查身份锚点、构图、画质、崩坏（脸/手/肢体、重复主体）、文字水印；不通过只允许修改提示词或参数重试一次，禁止同参数重试和未验收交付。本地视觉识别只用于桌面点击操作，禁止用于生成质量判断。
- Codex 用户级技能已安装第三方 `comfyui`（ComfyUI-Agent-Kit，含 minimax-h3/krea/seedance 配套）与 `auto-skill-installer`（find-skill），位于 `~/.codex/skills` 与 `~/.agents/skills`；HomeAgent 自身仍走 `Skill/comfyui-image-video` + `home_modules/comfyui_client.py`。
- 改图预设强化：正向词条加入身份保持（same face/hairstyle/outfit），负面词条加入五官崩坏/多余手指/身体结构错误等解剖保护词，且编辑步数下限 8 步（默认 20 步），避免低步数导致五官肢体画崩。
- 编辑步数策略修正：Lightning LoRA 按原生 4 步采样（此前 20 步 + LoRA 会过度处理导致与原图偏差大）；不带 LoRA 才用 20 步。`comfy_edit_image` 增加 `denoise` 参数（0.3～1.0，越低越保留原图）。
- 生成结果复制到 `outputs/comfyui/`；工具结果携带 `media` 列表，聊天回复通过 Qt 媒体气泡直接展示图片缩略图（点击打开原图）和视频卡片（点击“打开”）。
- 工作流已验证：Anima 8 步生图、Qwen-Edit 4 步改图、MiniMax-H3 8 帧/6 步视频均真实生成成功。

## CosyVoice2 情绪 TTS

- HomeAgent 通过 `home_modules/cosyvoice_tts.py` 调用本地 CosyVoice2-0.5B（`http://127.0.0.1:50000`，`/inference_instruct2` 指令式情绪合成）；服务未运行时按配置自动启动。
- **当前状态：暂时屏蔽**（`cosyvoice.enabled: false`）。工具 `cosyvoice_speak` / `cosyvoice_references` 不再暴露给模型，服务已停止；模块、音色库与文档保留，恢复时把配置改回 `enabled: true` 并重启即可。
- 台词中的（括号）语气描写会被自动解析为情绪指令（温柔/黏腻/喘息/颤抖/急促等映射表），括号内容剥离后朗读；`cosyvoice_speak` 工具返回音频文件，聊天界面以音频卡片展示。
- 参考音色目录 `outputs/cosyvoice_refs/`：默认音色为 `0_甘雨_温柔.wav`（取自 GPT-SoVITS `logs/甘雨-v2ProPlus/5-wav32k` 数据集，6 秒 24kHz 单声道归一化），可继续放入 3～10 秒干净人声切换；输出 WAV 保存到 `outputs/cosyvoice/`。
- 本地安装：`E:\OtherProgram\CosyVoice`（官方仓库 + `.venv` + CosyVoice2-0.5B 模型），并对官方代码做了两处 Windows 兼容补丁（见 07）。
- 已验证：角色台词真实合成 22.8 秒 WAV，情绪指令生效。
- 自我修改可编辑仓库内任意源码或配置路径，不再受预设模块目录限制；代码任务同时保留本地工具、Codex、命令和其他已启用工具，由模型选择执行方式。
- Codex 使用用户现有 `CODEX_HOME`，以完全跳过审批和沙盒的模式执行；Home Agent 的完整磁盘访问及命令确认均已关闭限制。
- 自升级只有在模型计划为 `domain=code` 且 `code_scope=self` 时启用；完成前必须有真实变更与测试证据。
- 直接重启不创建恢复任务；已完成任务会清除恢复文件，失败任务只保留诊断，不在重启时自动重跑。
- 普通聊天和定时请求不再创建 `task-recovery.json`。定时任务成功调用 `create_scheduled_task` 后只写入 `Task/<id>.json`；只有模型确认的自身代码升级会创建可跨重启恢复的当前任务状态。
- 角色图库返回规范绝对路径；图片分析支持 `primary`、图片 ID、文件名和标签。

## 上下文与记忆

- 家庭短期历史、家庭摘要、长期记忆、直播短期上下文和直播审计日志彼此分层。
- 家庭系统提示不读取原始直播聊天；旧 `search_memories` 工具已删除，个人经历统一通过结构化长期记忆检索。
- 模型在用户明确要求时调用 `clear_live_context` 清空直播持久状态并通知运行中的直播助手；否定或讨论语境不会靠关键词误触发。
- 粘贴图片 Base64 只存在于当前请求，不进入历史、日志、恢复文件或长期记忆。
- 提醒任务以任务文件为唯一状态来源；一次性提醒完成后删除，重复任务保留。

## 常驻服务

- 总控制台、Home Agent、角色管理器、直播助手、Vision 和 Sound 使用跨进程文件锁防止重复实例。
- Home Agent 可按配置拉起 Vision 与 Sound；组件重启应只影响发生代码变化的服务。
- 开机自启动通过 `configure_system_autostart` 同时登记启动文件夹、注册表 Run 键（`HKCU\...\Run\HomeAgent`）和登录触发任务计划（`HomeAgentAutostart`），自启动入口带 `--system-autostart`；`greeting_on_startup` 为真时 Home Agent 在后台线程用 TTS 向主人播放 `greeting_text` 打招呼，不阻塞启动。
- 主形象、三视图和固定外观文档位于 `workspace/character_images/` 与 `workspace/CHARACTER.md`。

## 当前验证基线

- Home Agent：172 项自动测试。
- 角色管理器：4 项自动测试。
- Vision：7 项窗口、截图与媒体停止测试。
- 直播助手：5 项欢迎、队列和 TTS 可靠性测试。
- `Skill/hatch-pet`：28 项技能包测试。

具体命令、人工检查和通过标准见 `08_TESTING.md`。
