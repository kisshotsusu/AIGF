# 测试项目与验证流程

核对日期：2026-07-24。默认工程根目录为 `E:\Doc\AIAgent`，Python 命令必须使用项目虚拟环境。

## 通过标准

按“静态检查 → 组件测试 → 真实冒烟 → 必要的重启验证”执行。通过必须同时满足：

- 命令退出码为 0，测试没有 failure 或 error。
- 修改过的 Python 可编译，YAML/JSON 可解析。
- 工具返回可验证的终态，不能只依据进程存在或没有抛异常。
- 重启后只有一个逻辑实例，最新启动日志没有新增异常。
- 实现、配置和测试数量已同步到 AI Read。

## 自动测试

### Home Agent：107 项

```powershell
Set-Location E:\Doc\AIAgent\HomeAgent
& ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

| 文件 | 数量 | 主要覆盖 |
|---|---:|---|
| `test_command_executor.py` | 4 | PowerShell/CMD、工作目录、引号与退出码 |
| `test_input_queue.py` | 12 | 多附件、异步预览、发送线程非阻塞、缩放、FIFO、QThread 与重启门禁 |
| `test_mimo_multimodal.py` | 19 | 图片/ASR 请求、完成检查、音乐视觉终态、原生窗口输入保护、最新证据压缩、证据时间、截图重试、媒体停止与关怀 |
| `test_self_programming_and_delivery.py` | 61 | 模型计划约束、定时任务与自升级状态隔离、完整代码路径与 Codex 权限、过期视觉证据淘汰、音乐完成门禁、自升级恢复、文件/代码工具、角色图片路径、消息/TTS 顺序 |
| `test_system_startup.py` | 4 | 开机启动、手动启动保护与重启计数 |
| `test_task_progress_card.py` | 7 | 紧凑任务卡、窄窗口、关怀设置、消息与气泡同步 |

单项定位：

```powershell
& ..\.venv\Scripts\python.exe -m unittest tests.test_self_programming_and_delivery -v
& ..\.venv\Scripts\python.exe -m unittest tests.test_mimo_multimodal.MiMoMultimodalTests.test_screen_capture_retries_transient_gdi_failure -v
```

### 角色管理器：4 项

```powershell
Set-Location E:\Doc\AIAgent\CharacterManager
& ..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

### 直播助手：5 项

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe -m unittest discover -s modules\live\tests -p "test_*.py" -v
```

覆盖登录身份、进场去重、GPT-SoVITS 暂态重试、成功后冷却及失败后可重试。

### Vision：7 项

```powershell
Set-Location E:\Doc\AIAgent\Vision
& ..\.venv\Scripts\python.exe -m unittest test_window_resolution.py -v
```

覆盖窗口解析、截图兜底、幂等媒体停止和操作后验证。

## 静态检查

```powershell
Set-Location E:\Doc\AIAgent
& .\.venv\Scripts\python.exe -m py_compile `
  HomeAgent\agent.py HomeAgent\app.py HomeAgent\qt_app.py `
  HomeAgent\self_upgrade.py HomeAgent\home_modules\code_editor.py `
  HomeAgent\home_modules\mimo_multimodal.py `
  Vision\agent.py modules\live\ai_live_assistant\app.py

& .\.venv\Scripts\python.exe -c "from pathlib import Path; import yaml; [yaml.safe_load(Path(p).read_text(encoding='utf-8')) for p in ('config.yaml','HomeAgent/config.yaml')]; print('yaml-ok')"

git diff --check
```

不要使用系统裸 `python` 跑 Qt 测试；它可能缺少 PySide6。

## 规划器真实冒烟

使用真实 MiMo 但不执行工具，至少验证以下语义：

| 请求 | 预期计划 |
|---|---|
| 修改当前 Home Agent 的任务理解代码 | `domain=code`、`code_scope=self` |
| 修改用户指定的其他代码库 | `domain=code`、`code_scope=external` |
| 创建独立新项目 | `domain=code`、`code_scope=new_project` |
| 完善角色固定外观文档 | `domain=file`、`implementation_change=false` |
| 清理直播消息 | `domain=memory`、`preferred_tools=[clear_live_context]` |
| 不要清理直播消息，只解释功能 | `actionable=false`，不得调用清理工具 |
| 打开网易云播放音乐 | `site=cloudmusic`、`query=""`、`query_is_explicit=false` |
| 打开网易云播放《稻香》 | `query="稻香"`、`query_is_explicit=true` |
| 看屏幕上的题目并作答 | `visual_required=true`、`interaction_mode=solve` |

规划冒烟只检查计划，不允许产生点击、写入或启动应用等副作用。

## 真实组件冒烟

### MiMo 图片

使用小型临时 PNG 调用 `MiMoMultimodalClient.analyze_image`，确认返回文本和模型名。不得把密钥、Base64 或完整图片写入日志。

### 截图与 Vision

分别验证整屏截图、目标窗口截图、暂态 GDI 重试和前台窗口兜底。操作类测试必须检查：

- `task_submitted_at`
- `tool_submitted_at`
- `screenshot_captured_at`
- `analysis_completed_at` 与识别年龄
- `tool_completed_at`
- `state_changed`
- `post_action_verified`

若截图后发生点击、输入、快捷键、媒体命令或明显窗口变化，旧识别必须标记为过期并重新读取；完成核验不得因证据文本截断而丢失最新状态。

### 音频

GPT-SoVITS 成功时不得调用 Windows SAPI；只有主 TTS 真实失败才允许降级。提醒和关怀消息必须先显示，再开始播放。

## 人工 UI 检查

自动测试后只检查受本轮改动影响的项目：

1. 缩放窄窗口，任务卡和长文本不能撑宽窗口。
2. 粘贴多张图片，预览区应在输入框上方，每张图片可独立打开和关闭。
3. 没有附件时预览区完全隐藏；加号可选择图片或普通文件。
4. 发送大图时 UI 不阻塞、不跳位，任务忙碌时输入进入队列。
5. 提醒或关怀触发时，聊天消息和桌宠气泡同步出现，TTS 随后播放。
6. 打开上下文调试页，确认家庭系统提示不包含原始直播聊天流水。

## 重启与日志

只重启发生代码变化的组件。重启后检查：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'AIAgent' } |
  Select-Object ProcessId, ParentProcessId, Name, CommandLine

Get-Content E:\Doc\AIAgent\HomeAgent\logs\agent-events.jsonl -Tail 30
Get-Content E:\Doc\AIAgent\Vision\logs\vision-mcp.log -Tail 30
```

历史日志不能当作本轮新错误；以修改或重启时间为界。失败时保存完整测试名、异常和退出码，先重跑失败项，再跑所属组件全套测试。

## 禁止事项

- 不删除断言、放宽失败条件或把异常改成成功来“修绿”测试。
- 不以模型文字承诺替代真实工具结果。
- 不在测试中读取或打印 `.env`、令牌、Cookie 和私密附件。
- 不为无关改动中断直播、Sound、Vision 或角色管理器。
