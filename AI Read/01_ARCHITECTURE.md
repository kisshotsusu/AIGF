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
  Vision MCP | Sound MCP | Skill | Codex 后备 | LLM | GPT-SoVITS
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
- `INTERACT_WORD` 与 `ENTRY_EFFECT` 同用户并发时通过 `_welcoming` 合并。
- 只有语音成功才写 `welcomed` 冷却；最终失败保留错误日志，允许后续事件再试。
- `/api/options` 短暂超时但 9879 端口仍存活时不重复启动 GPT-SoVITS，避免显存被第二个进程占用。
- TTS 默认 4 次尝试、指数退避；合成和播放严格串行，避免声音重叠。

## HomeAgent 链路

```text
文字 / Sound MCP
  -> HomeAgent.chat
  -> MiMo 语义计划：目标、参数、步骤、成功条件、浏览器策略
  -> 路由
     ├─ 本地确定性工具
     ├─ web-agent-operator
     ├─ Vision MCP（DOM 或视觉）
     ├─ 普通 LLM 工具循环
     └─ Codex CLI / 其他 MCP（复杂任务后备）
  -> 操作后观察与结果验证
  -> Qt 实时进度 / 长任务 TTS 汇报
  -> 家庭历史 / 高价值记忆 / task-recovery
```

模型负责语义，执行器负责机械可靠性。站点名称、歌曲名、收藏夹名等目标不应靠不断增加正则硬编码；执行器可以验证必填参数、权限、范围和业务返回值。

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
- Codex 使用隔离 `CODEX_HOME`，校验 JSONL 完成事件和必需 MCP 调用；网络错误不影响本地工具主路径。
- 自主升级通过持久化任务状态、重启次数限制和重启前校验恢复任务。
