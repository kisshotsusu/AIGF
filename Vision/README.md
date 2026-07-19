# Vision — GUI 网页/桌面控制 Agent (GUI-Actor-2B)

用本地 **GUI-Actor-2B（Qwen2-VL backbone）** 视觉 grounding 模型控制 Playwright 浏览器与 Windows 桌面，
封装成 **MCP 工具**，可用自然语言识别、点击、输入和滚动界面。

全部本地运行，权重在 `models/`，不把截图发到云端。

## 目录结构
```
Vision/
├── models/GUI-Actor-2B-Qwen2-VL/   # 模型权重(~4.5GB, 已下载)
├── GUI-Actor/                 # microsoft/GUI-Actor 仓库(gui_actor 包)
├── agent.py                   # 控制核心: 截图→grounding→点击/输入/滚动
├── mcp_server.py              # MCP server(暴露为工具)
├── download_model.py          # 重新下载模型用
├── timed_baidu.py / demo_baidu.py / smoke_test.py  # 验证/演示脚本
└── logs/                      # 日志
```
> 注意：本目录**不再单独持有 venv**，统一使用项目根目录的共享环境 `.venv`
> （Python 3.12 + torch cu128，详见父目录 README「共享环境」一节）。

## 运行方式（两种）
`mcp_server.py` 通过环境变量 `VISION_MCP_TRANSPORT` 选择传输：
- `stdio`（默认）：由 MCP 宿主直接拉起。
- `http` / `streamable-http`：作为常驻 HTTP 服务，监听 `VISION_MCP_HOST`(默认 127.0.0.1) : `VISION_MCP_PORT`(默认 8765)。
  设 `VISION_PRELOAD_MODEL=1` 可在启动时预加载模型，避免首次调用卡顿。

### 方式 A：常驻 HTTP 服务（HomeAgent 使用，默认）
HomeAgent 的 `config.yaml` 中 `vision_mcp.auto_start: true` 会自动拉起本服务（URL `http://127.0.0.1:8765/mcp`）。
手动启动：
```bat
set VISION_MCP_TRANSPORT=http
set VISION_PRELOAD_MODEL=1
.venv\Scripts\python.exe Vision\mcp_server.py
```

### 方式 B：注册到 WorkBuddy 作为 stdio 工具
在 `~/.workbuddy/mcp.json` 增加：
```json
"vision-gui": {
  "command": ".venv\\Scripts\\python.exe",
  "args": ["Vision\\mcp_server.py"],
  "env": {
    "GUI_ACTOR_MODEL": "Vision\\models\\GUI-Actor-2B-Qwen2-VL",
    "GUI_ACTOR_REPO": "Vision\\GUI-Actor"
  }
}
```
（当前 `mcp.json` 里 `vision-gui` 已以 URL `http://127.0.0.1:8765/mcp` 形式注册，由 HomeAgent 托管；
两种注册方式可并存，按需启用。）

默认 `headless=false`，弹出可见浏览器便于观看；无头模式设 `GUI_AGENT_HEADLESS=1`。

## 工具列表
| 工具 | 说明 |
|------|------|
| `navigate(url)` | 打开网页，返回最终地址 |
| `click(instruction)` | 看当前截图，点击符合描述的元素（如 "click the play button"） |
| `type_text(instruction, text)` | 点击输入框并输入文字 |
| `scroll(direction, amount)` | 滚动页面 |
| `screenshot()` | 返回当前页面截图（图片），供模型观察 |
| `get_url()` | 当前网址 |
| `wait(ms)` | 等待页面加载 |
| `play_video(instruction)` | 便捷：点击播放按钮开始播放 |
| `desktop_screenshot()` | 截取 Windows 主显示器 |
| `desktop_click(instruction)` | 视觉定位并点击桌面控件 |
| `desktop_type_text(instruction, text)` | 视觉定位桌面输入框并输入 |
| `desktop_scroll(direction, amount)` | 滚动桌面活动窗口 |
| `desktop_hotkey(keys)` | 发送快捷键，如 `['ctrl','l']` |

## 典型用法
> 「打开 https://www.bilibili.com ，搜索『周杰伦』，点开第一个视频并播放」

模型自动：navigate → 用 click/type_text 操作搜索框 → 进入视频页 → play_video 点击播放。
已实测：识别百度搜索框坐标并输入回车成功（GPU 上单次 grounding 约 1~3 秒）。

## 注意事项
- **坐标映射**：GUI-Actor 输出 0~1 归一化坐标，按 1280×800 视口映射为像素点击。
- **专业软件/超高分辨率界面（ScreenSpot-Pro）较弱**：2B 在该榜仅 ~36.7 分，
  面对 CAD/工程类软件长任务基本不可用；普通网页/App 场景（ScreenSpot-v2 ~88.6）足够。
- **多步任务复利**：单步差几分，在 10~20 步任务里会放大成显著端到端差距，
  长链路建议上 3B 或带 Verifier 的版本（代码已支持换 backbone，改环境变量即可）。
- 路径含中文，Python 以 UTF-8 处理无碍；MCP 命令用 Windows 反斜杠路径。
