# 测试项目与验证流程

最后核对：2026-07-22。所有命令默认在 `E:\Doc\AIAgent` 执行；除特别说明外均使用项目虚拟环境 `.venv\Scripts\python.exe`。

## 1. 测试层级与通过标准

测试按以下顺序执行，前一层失败时不要用后一层的成功掩盖问题：

1. 静态检查：修改过的 Python 必须可以编译，YAML 必须可以解析。
2. 组件自动测试：HomeAgent、直播和 Vision 分别运行自己的测试集。
3. 真实冒烟：调用本机截图、MiMo 或 MCP，验证模拟测试覆盖不到的系统边界。
4. 重启验证：仅在前面全部通过后重启受影响组件，检查逻辑实例数、端口和最新启动日志。
5. 文档门禁：实现或配置变化必须同步 `AI Read`，并通过 `git diff --check`。

通过必须同时满足：命令退出码为 0、没有失败/错误用例、真实服务返回预期结果、重启后没有新增异常日志。不能只依据“进程存在”或“工具没有抛异常”判定成功。

## 2. 一键自动测试

### HomeAgent（当前 78 项）

必须从 `HomeAgent` 目录运行，否则 `agent` 和本地模块导入路径不正确：

```powershell
Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

测试文件与主要测试项：

| 文件 | 数量 | 覆盖内容 |
|---|---:|---|
| `tests/test_command_executor.py` | 4 | PowerShell/CMD 执行、工作目录、非零退出码、CMD 内部双引号保持 |
| `tests/test_input_queue.py` | 6 | 剪贴板截图、实际缩略图预览与移除清理、忙碌时 FIFO 排队、任务完成续跑、重启时不误启动队列 |
| `tests/test_mimo_multimodal.py` | 15 | MiMo 图片/ASR 请求、完成检查、严格布尔值、关闭 thinking、证据时序、媒体 Stop 防反转、Vision 请求时间、截图重试、GB18030、关怀上下文与消息/TTS 顺序 |
| `tests/test_self_programming_and_delivery.py` | 44 | 本地代码工具、按行读取、Codex stdin、规划路由、停止/强制终止区分、绝对路径权限、伪工具调用、失败轮预算、重启恢复清理、自升级验证、原子写入与测试门禁 |
| `tests/test_system_startup.py` | 4 | 开机启动标记、手动启动保护、联网失败最多重启 5 次、成功后计数清零 |
| `tests/test_task_progress_card.py` | 5 | 任务卡片、关怀设置即时生效、提醒/关怀消息与桌宠气泡同步显示 |

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

1. 在 HomeAgent 输入框直接粘贴截图，确认出现预览，可以移除，也可以无文字发送。
2. 连续发送文字和图片，确认忙碌时按 FIFO 执行，输入框仍可用。
3. 启用屏幕关怀并缩短频率，确认消息气泡与对话消息先同步出现，TTS 随后播放。
4. 回复关怀语“好的”，确认不会继承先前代码任务上下文。
5. 修改关怀开关和频率，确认保存后计时器立即启动、重启或停止。
6. 执行明确的“重启你自己”，确认不经过大模型；重启后不重复执行已完成任务。
7. 在角色工作台缩放窗口，确认“模型 API → MiMo 多模态”表单可滚动、保存按钮始终可见。

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
