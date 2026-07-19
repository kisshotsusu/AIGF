# 启动、验证与维护规则

## 启动入口

- 直播管理页：`启动管理页面.bat`
- 角色管理器：`启动角色管理器.bat`
- 家庭桌宠：`HomeAgent\启动家庭Agent.bat`
- 环境安装：`set_env.bat`
- 模型下载：`down_model.bat`

批处理默认隐藏控制台，仅保留 GUI。端口 9888 被占用通常表示管理器已运行，不要重复启动。

## 修改后的最低验证

- Python：从项目根目录使用 `.venv\Scripts\python.exe -m py_compile ...`。
- YAML：用 `yaml.safe_load` 读取根配置和 HomeAgent 配置。
- MCP：检查 8765 监听，然后初始化会话并 `list_tools`。
- 网页 Agent：运行 `Skill/web-agent-operator/scripts/web_agent.py` 做无截图端到端测试。
- 直播语音：运行 `.venv\Scripts\python.exe -m unittest discover -s modules/live/tests -v`，至少覆盖重试、冷却和重复进场。
- GUI 设置：特别检查控件不会被可扩展表格或图片挤出窗口。

## 配置写入规则

- 写入前读取最新磁盘配置，只改负责的字段。
- CharacterManager 负责 `vision_mcp.gui_enabled/preload_model`；旧 HomeAgent UI 不得覆盖整段视觉配置。
- 禁用 GUI 时立即停止旧视觉模型进程；`preload_model: false` 表示允许视觉但启动服务时不加载模型，两者不要混淆。
- MCP 配置同步到 `workspace/MCP_SERVERS.yaml`、WorkBuddy 和 Codex 注册表时保留未修改项。

## Agent 行为规则

- 用户描述的是最终目标，不是第一步。打开网站后还有搜索/选择/播放时必须继续并验证。
- 模型负责理解任务、目标参数和完成条件；确定性本地工具负责执行和验证，例如直播上下文清理、B站收藏夹真实顺序读取。
- 网页任务优先 `web-agent-operator` 和 DOM 工具。
- 登录态网页优先现有浏览器与 DOM；无 CDP 时保留浏览器并回退窗口视觉，不得为登录任务启动临时 Chrome。
- 图像 GUI 禁用时，不回退截图、窗口图像或 GUI-Actor；仅 `preload_model` 关闭时可以按需懒加载。
- 每个点击、输入、快捷键或滚动后读取操作后截图证据；页面变化不等同于业务完成，仍要验证 URL、文本或媒体状态。
- 操作可停止；HomeAgent 右键和对话条均有“停止当前任务”。
- 不虚构成功。只有验证最终 URL/状态或媒体实际播放后才能汇报完成。

## 音频

- `audio/` 始终只保留最新 20 个音频，旧文件优先删除。
- 家庭长文本分块：生成一段即入播放队列，同时生成下一段；播放严格按序且不重叠。
- TTS 文本过滤不支持 GBK 的 emoji，避免把音乐符号等字符传给外部批处理。
- 直播语音通过单消费者优先队列串行；欢迎成功后才进入冷却。健康接口忙时不得重复启动 GPT-SoVITS。
