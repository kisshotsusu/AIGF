# 数据、记忆与上下文

## 角色和人格工作区

- `workspace/IDENTITY.yaml`：角色、主人、直播用户名关联。
- `workspace/CHARACTER.md`：固定外观。
- `workspace/SOUL.md`：灵魂与人格。
- `workspace/RULES.md`：通用安全规则。
- `workspace/LIVE_RULES.md`：直播短回复规则。
- `workspace/HOME_RULES.md`：家庭长对话规则。
- `workspace/ABILITIES.md`：能力说明。
- `workspace/HOME.md`：家庭场景。
- `workspace/HOME_CONTEXT_SUMMARY.md`：家庭压缩摘要。
- `workspace/character_images/manifest.json`：角色图片库和主图。

## 三类记忆/上下文

1. **直播短期模型上下文**：`state/live-context.json`，含角色、内容、创建时间；运行时同步到 deque。默认清理超过 120 分钟内容。
2. **旧式每日共享记忆**：`workspace/memory/YYYY-MM-DD.jsonl`，由 Workspace 读写，角色管理器可编辑标签、隐私和附件。
3. **结构化长期记忆**：`LongTermMemory/memory.db`，只存身体、情绪、重大事件、偏好、习惯、关系和约定等高价值内容。
4. **未完成任务恢复状态**：`HomeAgent/state/task-recovery.json`，记录任务原文、当前步骤、已完成内容、是否属于自升级和重启次数；完成或取消后清理。

## 长期记忆记录

核心字段：`id`、`created_at`、`user_id`、`scene`、`category`、`tags`、`summary`、`detail`、`importance`、`privacy`、`source`。

写入原则：普通闲聊不存；高价值内容提炼 3–5 个标签、20 字以内摘要和关键原句。用户询问“之前”“记得吗”时先按标签检索。

## 用户身份

`IDENTITY.yaml` 中家庭别名和 `live_usernames` 都映射到规范用户 ID `owner`。不同直播用户名默认是独立观众，不能互相套用记忆。私密标签/附件只允许家庭场景读取。

## 清理直播上下文

HomeAgent 识别“清理/清空 + 直播 + 上下文/聊天记录/近期对话”后：

1. 直接把 `state/live-context.json` 原子写为 `[]`，不依赖直播助手运行。
2. 写 `state/live-context-control.json` 控制请求。
3. 若直播助手运行，每秒读取请求并清空内存副本，回写状态和移除条数。

该操作不删除 `logs/messages.jsonl`、每日记忆或 SQLite 长期记忆。

## 日志

- `logs/messages.jsonl`：直播收到、跳过、回复、欢迎、语音排队结果和错误事件。欢迎事件可区分 `received`、`success`、`error`、`cooldown`、`already_queued`。
- `HomeAgent/logs/agent-events.jsonl`：家庭路由、工具、TTS、Codex、网页 Agent 事件。
- `Vision/logs/`：MCP 与浏览器服务日志。
