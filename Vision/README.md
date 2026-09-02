# Vision — GUI 网页/桌面控制 Agent

用本地视觉模型控制 Playwright 浏览器与 Windows 桌面，封装成 **MCP 工具**，
可用自然语言识别、点击、输入和滚动界面。全部本地运行，截图不上云。

## 识别后端（仅 GUI-Owl，Qwen3-VL）

视觉识别唯一后端为 **GUI-Owl-1.5-2B-Instruct（Qwen3-VL 原生 GUI agent）**。
`agent.ground_image()` 直接返回 **0~1 归一化坐标**，上层 `click / type_text / 窗口 / 桌面`
工具无需关心坐标细节。

> 历史：早期默认 `microsoft/GUI-Actor-2B-Qwen2-VL`（Qwen2-VL pointer head，仅坐标
> grounding）。该老模型基于已被取代的 Qwen2-VL 架构，**已于 2026-09 彻底移除**——
> 模型目录与 `Vision/GUI-Actor` 仓库均已删除。`agent.py` 不再含任何 GUI-Actor 分支，
> `VISION_BACKEND` 只识别 `gui_owl`，即便误传 `gui_actor` 也会归一化到 `gui_owl` 并告警。

环境变量：

- `VISION_BACKEND`（可省略；缺省即 `gui_owl`。传其他值会归一化为 `gui_owl` 并告警）
- `GUI_OWL_MODEL=<本地目录或 HF repo id>`（默认 `Vision/models/GUI-Owl-1.5-2B-Instruct`，不存在时回退 `mPLUG/GUI-Owl-1.5-2B-Instruct`）

## 目录结构
```
Vision/
├── models/GUI-Owl-1.5-2B-Instruct/  # 识别后端权重 (Qwen3-VL, ~4GB)
├── agent.py                   # 控制核心: 截图→grounding→点击/输入/滚动 (GUI-Owl)
├── mcp_server.py              # MCP server(暴露为工具)
├── download_model.py          # 下载模型: --model gui-owl (gui-actor 已废弃)
├── smoke_test.py / demo_baidu.py / timed_baidu.py  # 验证/演示脚本
└── logs/                      # 日志
```
> 注意：本目录**不再单独持有 venv**，统一使用项目根目录的共享环境 `.venv`
> （Python 3.12 + torch cu130，详见父目录 README「共享环境」一节）。
> 固定 `transformers==4.57.1` + `qwen-vl-utils>=0.0.14`（已验证 GUI-Owl 在 4.57/cu130 下正常加载，无需分离 venv）。

## GUI-Owl 运行环境（共享 .venv）  ★已验证可用

GUI-Owl（Qwen3-VL）运行于项目根目录共享 `.venv`（torch 2.13 + CUDA 13.0），
固定 `transformers==4.57.1` + `qwen-vl-utils>=0.0.14`（已验证 GUI-Owl 在 4.57/cu130 下正常加载）。
所有启动脚本与 `HomeAgent/agent.py` 均直接调用 `.venv`。

```bat
REM 依赖安装（共享 .venv；torch 走 cu130 索引，切勿用 --index-url 覆盖 PyPI）
.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu130
.venv\Scripts\python.exe -m pip install "transformers>=4.57" "qwen-vl-utils>=0.0.14" accelerate pillow numpy opencv-python-headless playwright psutil

REM MCP 服务端依赖需 mcp<2：mcp 2.x 把 FastMCP 改名为 MCPServer，会直接 ImportError
.venv\Scripts\python.exe -m pip install "mcp<2"
```

运行/验证（仅 grounding，不触发任何鼠标键盘动作）：

```bat
set VISION_BACKEND=gui_owl
.venv\Scripts\python.exe Vision\test_gui_owl.py
```

实测（RTX 5070 Ti / 16GB 显存）：模型加载 **~7.5s**、占用 **~3.96GB** VRAM；
合成图定位偏差 **~0.5%**（贪心解码，确定性可复现），找不到元素时正确输出 `terminate`（工具返回"未找到"）。
`mcp_server.py` 在 `.venv` 下可干净导入并注册 32 个工具（含 `vision_backend`/`click`/`desktop_click`/`window_click`/`ground_page`），
后端切换为 `gui_owl` 时由 `HomeAgent/agent.py` 自动用 `.venv` 拉起该服务（见 `config.yaml` 的 `vision_mcp.backend: gui_owl`）。

### 推理优化：torch.compile / CUDA Graphs（实测不适用，默认关闭）

GUI-Owl 是 2B 自回归 grounding 模型，单次 `generate()` 序列逐 token 增长、图像 token 数随分辨率变化，
**shape 高度动态**。在此负载下实测（RTX 5070 Ti / torch 2.13 cu130，固定图稳态 6 次中位）：

| 配置 | 延迟/次 | 对比 |
|------|---------|------|
| eager（默认） | **~539 ms** | 基准 |
| `torch.compile` mode=default (dynamic=True) | ~795 ms | 慢 ~47% |
| `torch.compile` mode=reduce-overhead（CUDA Graphs） | ~787 ms | 慢 ~46% |

CUDA Graphs 仅能重放静态 shape 前向，而解码步 shape 持续变化 → 无法跨步复用，inductor 图分派开销反而盖过
kernel 融合收益。故 `agent.py` 中 `GUI_OWL_TORCH_COMPILE` **默认 `0`（关闭）**。

如需在更大模型 / 静态 shape 批量场景实验，可开启：`GUI_OWL_TORCH_COMPILE=1`、
`GUI_OWL_COMPILE_MODE=reduce-overhead`（配合 `GUI_OWL_STATIC_SHAPE=1` 把图 letterbox 到固定 1280×720 以稳定 shape）。
这是**可实验开关**，非默认优化路径。当前更有效的推理提速来自 `VISION_MAX_SIDE` 降采样与贪心解码（已默认开启）。

### 经 HomeAgent 自动启用（推荐）

在 `HomeAgent/config.yaml` 中已含 `vision_mcp` 段：

```yaml
vision_mcp:
  enabled: true
  auto_start: true
  backend: gui_owl          # 用 GUI-Owl (Qwen3-VL) 后端
  gui_enabled: true
  url: http://127.0.0.1:8765/mcp
  port: 8765
  preload_model: true
```

HomeAgent 启动即拉起本服务并注册 `ui_*` 视觉工具。后端固定 GUI-Owl，无切换入口。

## 下载模型
```bat
.venv\Scripts\python.exe Vision\download_model.py --model gui-owl
```
或直接运行项目根目录 `down_model.bat`（默认下载 GUI-Owl + SenseVoice）。
下载使用 curl 断点续传，中断后重跑即可续传。

## 运行方式（两种）
`mcp_server.py` 通过环境变量 `VISION_MCP_TRANSPORT` 选择传输：
- `stdio`（默认）：由 MCP 宿主直接拉起。
- `http` / `streamable-http`：作为常驻 HTTP 服务，监听 `VISION_MCP_HOST`(默认 127.0.0.1) : `VISION_MCP_PORT`(默认 8765)。
  设 `VISION_PRELOAD_MODEL=1` 可在启动时预加载模型，避免首次调用卡顿。

### 方式 A：常驻 HTTP 服务（HomeAgent 使用，默认）
HomeAgent 的 `config.yaml` 中 `vision_mcp.auto_start: true` 会自动拉起本服务
（URL `http://127.0.0.1:8765/mcp`），并自动注入 `VISION_BACKEND=gui_owl`、
`GUI_OWL_MODEL=Vision\models\GUI-Owl-1.5-2B-Instruct`。
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
    "VISION_BACKEND": "gui_owl",
    "GUI_OWL_MODEL": "Vision\\models\\GUI-Owl-1.5-2B-Instruct"
  }
}
```
默认 `headless=false`，弹出可见浏览器便于观看；无头模式设 `GUI_AGENT_HEADLESS=1`。

## 工具列表
| 工具 | 说明 |
|------|------|
| `vision_backend()` | 返回当前识别后端与模型来源 |
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

## 注意事项
- **坐标映射**：GUI-Owl 输出 0~1000 相对坐标，代码里统一 `/1000` 归一化后按原图尺寸
  映射为像素，对外返回 0~1 归一化坐标。
- **GUI-Owl grounding 提示词**：与官方 ScreenSpot 评测一致，只允许
  `left_click / mouse_move`，找不到元素会返回 `terminate`（对应空点，工具返回失败原因）。
- **窗口点击找不到元素**：`window_click / window_double_click` 整窗识别失败后会自动
  放大顶部 45% 工具栏区域重试一次（常见应用的搜索框/菜单都在顶部），
  结果里带 `grounding_region: full|top` 标明命中区域。
- **动画界面误判过期**：`analyze_window.py` 只把“标题变化或整窗级大变化
  (change_ratio≥0.45 / mean≥0.20)”判定为截图过期，音乐播放器/视频的进度、
  封面动画不再导致识别结果被丢弃；阈值可用环境变量 `ANALYZE_STALE_RATIO` /
  `ANALYZE_STALE_MEAN` 调整。
- **屏幕锁定/会话断开**：锁屏或安全桌面时截图会快速失败并返回明确原因
  （“屏幕当前不可用(可能已锁定…)”），不再无意义地重试 3 次。
- **显存**：GUI-Owl-1.5-2B BF16 约 5GB，与 TTS 等其它模型共用 16GB 显存仍可运行；
  GUI-Owl-4B 约 8GB，若 TTS 占用较高建议保持 2B。
- **专业软件/超高分辨率界面（ScreenSpot-Pro）**：2B 相对大模型仍偏弱，复杂长任务
  建议上 4B/8B（改 `GUI_OWL_MODEL` 环境变量即可）。
- 路径含中文，Python 以 UTF-8 处理无碍；MCP 命令用 Windows 反斜杠路径。
