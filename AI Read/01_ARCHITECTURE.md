# 设计架构

## 组件关系

```text
B站直播间
  -> BilibiliLive 事件/历史弹幕
  -> LiveAssistant
  -> LLM（DeepSeek / MiMo / Custom）
  -> TTS 9879 -> audio -> 顺序播放
  -> 直播日志 / 短期上下文 / 长期记忆

用户（家庭）
  -> HomeAgent 桌宠（文字 / 麦克风）
  -> LLM 工具循环
  -> 本地工具 / Skill / Codex CLI / MCP
  -> TTS 9879 -> 顺序播放
  -> 家庭上下文 / 共享记忆 / 长期记忆 / 定时任务

CharacterManager
  -> 统一编辑身份、人格规则、记忆、模型、语音、软件映射、MCP、维护策略、角色图片

vision-gui MCP :8765
  -> 当前：Playwright DOM/文本网页 Agent（无模型）
  -> 可选：GUI-Actor 图像定位和 Windows 窗口操作（当前关闭）
```

## 设计边界

- 直播和家庭是不同场景规则，但共用角色身份和高价值记忆。
- 私密记忆只能进入家庭场景，直播提示词不能注入私密内容或私人照片。
- 直播回复短；家庭回复可长文本切分，TTS 生成和播放流水线并行但播放严格串行。
- 网页操作优先 DOM/文本；只有明确开启 GUI 图像识别时才可截图定位。
- 软件/App 操作由 `computer_control.applications` 名称映射到程序或目录。

## 关键数据流

### 直播回复

`bilibili.py` 收到事件 → `LiveAssistant.handle_event` 去重和触发判断 → 构造含用户名身份的上下文 → `LLMClient.reply` → 截短直播回复 → `TTSClient` → 记录 `logs/messages.jsonl` → 按记忆规则决定是否写入。

### 家庭 Agent

桌宠输入 → `HomeAgent.chat` → 先处理确定性路由（清理上下文、B站网页 Agent等）→ 再决定普通 LLM 工具循环、Codex 或 MCP → 更新家庭历史 → 高价值记忆判断 → 分段 TTS。

### Bilibili 网页 Agent

自然语言提取搜索词 → 运行 `Skill/web-agent-operator/scripts/web_agent.py` → `navigate` 搜索页 → `web_read` → 多关键词评分结果 → 打开视频 → `get_url` 验证 → `web_play_media` → `web_read` 最终验证。

### 上下文清理

直播短期上下文同时存在于运行时 deque 和 `state/live-context.json`。HomeAgent 可直接原子清空状态文件，并写 `state/live-context-control.json`；直播助手运行时每秒消费控制信号并同步清空内存。

