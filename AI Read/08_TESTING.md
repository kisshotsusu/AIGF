# 测试项目与验证流程

最后核对：2026-07-24。所有命令默认在 `E:\Doc\AIAgent` 执行；除特别说明外均使用项目虚拟环境 `.venv\Scripts\python.exe`。

## 1. 测试层级与通过标准

测试按以下顺序执行，前一层失败时不要用后一层的成功掩盖问题：

1. 静态检查：修改过的 Python 必须可以编译，YAML 必须可以解析。
2. 组件自动测试：HomeAgent、直播和 Vision 分别运行自己的测试集。
3. 真实冒烟：调用本机截图、MiMo 或 MCP，验证模拟测试覆盖不到的系统边界。
4. 重启验证：仅在前面全部通过后重启受影响组件，检查逻辑实例数、端口和最新启动日志。
5. 文档门禁：实现或配置变化必须同步 `AI Read`，并通过 `git diff --check`。

通过必须同时满足：命令退出码为 0、没有失败/错误用例、真实服务返回预期结果、重启后没有新增异常日志。不能只依据“进程存在”或“工具没有抛异常”判定成功。

## 2. 一键自动测试

### HomeAgent（当前 97 项）

必须从 `HomeAgent` 目录运行，否则 `agent` 和本地模块导入路径不正确：

```powershell
Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

测试文件与主要测试项：

| 文件 | 数量 | 覆盖内容 |
|---|---:|---|
| `tests/test_command_executor.py` | 4 | PowerShell/CMD 执行、工作目录、非零退出码、CMD 内部双引号保持 |
| `tests/test_input_queue.py` | 11 | 剪贴板多图片、文件选择器、用户原文件保护、独立缩略图与单张移除、后台原图编码、保存完成后发送、稳定预览面板、边缘/四角缩放与最小尺寸、忙碌时 FIFO 排队、任务完成续跑、QThread 完整退出后再释放、重启门禁 |
| `tests/test_mimo_multimodal.py` | 17 | MiMo 单图/多图/ASR 请求、完成检查、严格布尔值、关闭 thinking、证据时序、媒体 Stop 防反转、代码任务媒体工具越权拦截、Vision 请求时间、截图重试、GB18030、关怀上下文与消息/TTS 顺序 |
| `tests/test_self_programming_and_delivery.py` | 54 | 本地代码工具、按行读取、Codex stdin、规划路由、实现修改禁止误读屏、角色图库绝对路径输出、三视图文件名/ID/标签/主形象解析、图片分析路径传递、窗口/屏幕活动摘要脱敏、网易云通用播放/具体搜索区分、模型 UI 原子工具集、按计划裁剪媒体工具、停止/强制终止区分、绝对路径权限、伪工具调用、失败轮预算、重启恢复清理、自升级验证、原子写入与测试门禁 |
| `tests/test_system_startup.py` | 4 | 开机启动标记、手动启动保护、联网失败最多重启 5 次、成功后计数清零 |
| `tests/test_task_progress_card.py` | 7 | 默认展开的任务活动卡片、判断摘要、编号计划、最近 8 条工具活动、窄窗口自适应、完成状态、关怀设置即时生效、提醒/关怀消息与桌宠气泡同步显示 |

单文件或单用例定位方法：

```powershell
Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -m unittest tests.test_command_executor -v
& ..\.venv\Scripts\python.exe -m unittest tests.test_mimo_multimodal.MiMoMultimodalTests.test_screen_capture_retries_transient_gdi_failure -v
```

### 直播（当前 5 项）

必须从工程根目录运行：

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe -m unittest discover -s modules\live\tests -p "test_*.py"
```

### 角色管理器（当前 4 项）

```powershell
Set-Location E:\Doc\AIAgent\CharacterManager
& ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

覆盖千问/Grok 预设字段、预设对象隔离、DashScope 原生请求体与输出图片提取，以及 xAI `/images/generations` 请求不携带通用 `size` 字段。

`modules/live/tests/test_reliable_speech.py` 覆盖：登录 Cookie 身份、不同进场事件、GPT-SoVITS 暂态失败重试、欢迎成功后才写冷却、重复进场只保留一个待播欢迎。测试中出现预期的 `GPU busy` 重试输出不代表失败，以最终 `OK` 和退出码为准。

### Vision（当前 7 项）

Vision 导入依赖其目录下的 GUI-Actor 包，因此从根目录运行时要设置 `PYTHONPATH`：

```powershell
Set-Location E:\Doc\AIAgent
$env:PYTHONPATH = "E:\Doc\AIAgent\Vision"
& .\.venv\Scripts\python.exe -m unittest Vision\test_window_resolution.py -v
```

覆盖标题、进程路径、进程名和 HWND 窗口解析；HWND 截图失败或纯黑时边界截图兜底；操作后截图失败必须返回未验证失败；Win32 Media Stop 必须返回幂等 stopped 状态。

## 3. 静态检查

只编译本次修改过的 Python 文件。示例：

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe -m py_compile HomeAgent\agent.py HomeAgent\app.py HomeAgent\qt_app.py HomeAgent\home_modules\mimo_multimodal.py HomeAgent\home_modules\command_executor.py Vision\agent.py modules\live\ai_live_assistant\app.py
```

配置解析检查不得打印密钥或完整配置：

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe -c "from pathlib import Path; import yaml; [yaml.safe_load(Path(p).read_text(encoding='utf-8')) for p in ('config.yaml','HomeAgent/config.yaml')]; print('yaml-ok')"
```

文档和补丁检查：

```powershell
git diff --check
git status --short
```

`git status` 用于确认范围，不要求工作区必须干净；已有用户修改和 `Vision/GUI-Actor/` 不得擅自删除。

## 4. 真实冒烟测试

真实冒烟可能调用屏幕、模型或本机服务，只在对应功能有改动时运行。密钥从 `.env` 加载到进程环境，禁止打印密钥值、请求头或完整 Base64。

### Windows 截图

同时验证 Vision 整屏/前台窗口兜底和 HomeAgent 截图入口：

```powershell
Set-Location E:\Doc\AIAgent
$env:PYTHONPATH = "E:\Doc\AIAgent\Vision"
& .\.venv\Scripts\python.exe -c "import agent; im=agent.desktop_screenshot_pil(); print(im.size, im.mode); im.close()"

Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -c "import agent; im=agent._grab_screen_with_retry(); print(im.size, im.mode); im.close()"
```

预期：输出非零尺寸和 `RGB`；不得出现 `screen grab failed`。窗口截图改动还应调用 `list_windows` 取得真实 HWND，再用 `window_screenshot_pil(str(hwnd))` 验证。

### 运行中的 Vision MCP

先检查唯一监听者，再调用不会执行点击的只读截图工具：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8765 | Select-Object LocalPort,OwningProcess
$json = '{}'
& E:\Doc\AIAgent\.venv\Scripts\python.exe E:\Doc\AIAgent\Vision\mcp_call.py http://127.0.0.1:8765/mcp desktop_screenshot $json
```

预期：只有一个 8765 监听 PID，MCP 调用返回 `{"ok": true, ...}`。图片内容块不一定被 `mcp_call.py` 打印成文本，但 `isError=false` 才算成功。

### MiMo 完成检查

使用一个明确的只读观察证据调用 `MiMoMultimodalClient.verify_completion`。任务计划须包含 `interaction_mode=observe`，证据包含成功的 `ui_analyze_screen` 观察。预期返回 `passed=true`；请求体必须有 `thinking.type=disabled`、`stream=false`，且没有 `response_format`。

真实 API 测试与单元测试要分开：单元测试使用假 Session 验证请求结构，真实冒烟才验证网络、认证和模型行为。真实 API 暂时不可用时应明确记录为环境失败，不能把它改成自动测试中的静默跳过。

### 文本编码与 CMD

编码测试应实际读取近期失败过的 `logs/assistant.log` 和 `Vision/logs/vision-mcp.log`，确认返回 `gb18030` 且内容可读。CMD 测试使用：

```powershell
Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -m unittest tests.test_command_executor.CommandExecutorTests.test_cmd_preserves_embedded_filter_quotes -v
```

预期：`tasklist /fi "imagename eq ..."` 退出码为 0，不出现 `Invalid argument/option - 'eq'`。

## 5. GUI 人工验收

涉及界面、队列、气泡或系统剪贴板时，自动测试后执行以下检查：

1. 空状态确认输入框上方没有附件栏；点击输入框左侧 `＋`，一次选择图片和普通文件，确认附件栏才展开且分别显示缩略图、类型和文件名。再连续粘贴图片，逐个点击右上角 `×`，确认只移除目标附件；全部移除后附件栏再次隐藏。任务结束后确认手动选择的原文件仍存在。
2. 一次选择多张 4K 图片，确认文件窗口关闭后缩略图逐张出现，期间窗口可拖动、输入框可输入、按钮悬停状态可刷新；点击发送时如队列尚未处理完，应等待全部附件加入后自动发送。
3. 连续发送文字和图片，确认忙碌时按 FIFO 执行，输入框仍可用。
4. 启用屏幕关怀并缩短频率，确认消息气泡与对话消息先同步出现，TTS 随后播放。
5. 回复关怀语“好的”，确认不会继承先前代码任务上下文。
6. 修改关怀开关和频率，确认保存后计时器立即启动、重启或停止。
7. 执行明确的“重启你自己”，确认不经过大模型；重启后不重复执行已完成任务。
8. 在角色工作台缩放窗口，确认“模型 API → MiMo 多模态”表单可滚动、保存按钮始终可见。
9. 将鼠标放到聊天窗口四边和四角，确认光标变化并能连续拖动缩放；窗口不能缩小到 640×300 以下，标题栏仍可移动窗口。
10. 粘贴一张 4K 截图，确认缩略图立即出现、窗口仍可拖动和输入；原图准备完成后状态变化，期间点击发送会自动在准备完成后继续，附件区不挤乱操作按钮。
11. 只调用 `_plan_task`（不要进入 `chat` 或工具循环）分别规划“打开网易云音乐播放音乐”和“打开网易云音乐播放稻香”：两者均应使用 `model_ui`，前者必须为空查询且步骤中没有搜索，后者必须保留“稻香”并规划识别搜索框、输入、重新识别结果、点击及终态验证。确认执行工具只有通用窗口/视觉原子工具，不存在网易云一键搜索播放工具。
12. 只调用 `_plan_task` 规划“执行命令后优先读取了屏幕，这是硬编码错误，检查并修复；检查 Home Agent 程序页面并减少任务过程细节”，并附一段窗口 observation JSON。必须得到 `implementation_change=true`、`domain=code`、`visual_required=false`、`execution_strategy=code_loop`，且不能实际调用任何视觉工具。
13. 给 `_tool_activity_result("ui_list_windows", ...)` 传入含 hwnd、PID、bounds 和 process_path 的结果，任务卡摘要只能是“找到 N 个可用窗口”；给屏幕识别结果传入私人画面描述，活动摘要不得回显该内容。任务卡超过 8 条活动时只显示最近 8 条。
14. 给任务卡填入超长、无空格的状态、计划和活动内容，把卡片宽度缩到 380 像素。确认任务卡最小宽度不随文本增长，详情标签采用可收缩尺寸策略，消息区不出现横向滚动或反向撑大聊天窗口。
15. 让 `ChatWorker` 发出界面完成信号时仍保持 `isRunning=true`，确认窗口继续持有该 worker、不会启动下一项；随后模拟 `QThread.finished`，确认才释放引用并继续队列。真实运行中发送一个会产生多段 TTS 的普通问题，等待最后一段语音完成后确认桌宠进程和图标仍存在。

人工验收不能包含真实点击、提交、发弹幕或修改外部文件，除非本次任务明确授权了对应副作用。

## 6. 重启与运行状态验证

只重启本次修改涉及且原本正在运行的组件。Windows 虚拟环境常出现启动器父进程和系统 Python 子进程，这是一份逻辑实例；以命令行、父子关系、进程锁和监听端口共同判断，不能按 PID 数量误杀。

重启后检查：

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 8765,8766,9888 } | Select-Object LocalPort,OwningProcess
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'E:\\Doc\\AIAgent' } | Select-Object ProcessId,ParentProcessId,Name,CommandLine
Get-Content E:\Doc\AIAgent\HomeAgent\logs\agent-events.jsonl -Tail 20
Get-Content E:\Doc\AIAgent\Vision\logs\vision-mcp.log -Tail 20
```

HomeAgent 应新增 `long_term_memory_migration`；由它拉起 Vision 时应新增 `vision_service_started`。Vision 日志应出现 Uvicorn 监听 8765，且重启后没有新的 Traceback。Sound 或直播没有改动时不要为了“全重启”无条件中断它们。

## 7. 失败定位顺序

1. 保存完整的失败测试名、异常和退出码，不要只记录最后一行。
2. 单独运行失败文件，再精确运行失败用例。
3. 判断是实现错误、测试隔离错误，还是外部环境错误（网络、显存、桌面会话、端口）。
4. 查看与失败时间一致的最新日志；历史日志不能当成本轮新错误。
5. 修复后先重跑失败用例，再跑所属组件全套测试，最后执行相关真实冒烟。
6. 更新本文件中的数量、命令或流程，以及其他受影响的 `AI Read` 当前事实。

禁止通过删除断言、放宽失败条件、把异常改成成功、跳过真实终态验证来“修绿”测试。

## 8. 音乐停止、证据时序与子升级专项（2026-07-22）

专项用例必须覆盖：stop-media 拒绝 Space 且在启动 Vision 前返回；`media_stop` 结果含请求/完成时间；强制终止计划不被误识别为媒体停止；完成核验提示要求较新证据覆盖较早证据；纯黑 HWND 截图退回边界截图；按行读取搜索命中；Codex 命令以 `-` 读取 stdin；失败升级不自动恢复。

```powershell
Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -m unittest tests.test_mimo_multimodal tests.test_self_programming_and_delivery -v

Set-Location E:\Doc\AIAgent\Vision
& ..\.venv\Scripts\python.exe -m unittest test_window_resolution -v
```

Codex stdin 真实冒烟应使用 `--sandbox read-only -` 和不会修改文件的短提示。通过标准是输出含 `thread.started`、最终 assistant message 与 `turn.completed`；网络重连或 HTTP 回退应单独记录为环境延迟，不能把缺少 `turn.completed` 当成功。

## 9. hatch-pet 技能（2026-07-23）

必须使用 `load_workspace_dependencies` 返回的 Python，不能使用裸 `python`。从技能目录运行自带测试，并确认 Home Agent 能在 `list_skills()` 中发现 `hatch-pet`：

```powershell
$PYTHON = "<load_workspace_dependencies 返回的 Python 路径>"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location E:\Doc\AIAgent\Skill\hatch-pet
& $PYTHON -m unittest discover -s tests -p "test_*.py" -v

Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -c "from agent import HomeAgent; print(any(x['name']=='hatch-pet' for x in HomeAgent.__new__(HomeAgent).list_skills()))"
```

通过标准：6 个技能测试文件中的 28 个用例全部通过，发现检查输出 `True`，项目副本没有 `__pycache__`、`.pyc` 或缺失的脚本/参考文件。Windows 上必须启用 UTF-8 模式，否则测试中的无参数 `Path.read_text()` 会按系统 GBK 解码生成的 UTF-8 提示文件。
