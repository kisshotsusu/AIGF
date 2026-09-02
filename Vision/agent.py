#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Vision 视觉控制核心。

后端固定为 GUI-Owl-1.5-2B-Instruct (Qwen3-VL 原生 GUI agent) —— 当前唯一支持的后端。

历史: 曾默认 microsoft/GUI-Actor-2B-Qwen2-VL (Qwen2-VL pointer head, 需
Vision/GUI-Actor 仓库)。该模型基于已被取代的 Qwen2-VL 老架构、且仅做坐标 grounding,
2026-09 起已彻底移除: 模型目录与仓库均已删除, 运行时代码不再包含任何回退到
gui_actor 的分支, VISION_BACKEND 只识别 gui_owl, 遇到其他值会归一化到 gui_owl 并告警。

统一流程: 截图当前视口 -> ground_image() 视觉 grounding 得到归一化坐标(0~1)
        -> 映射到视口像素 -> Playwright 点击/输入/滚动 (或 Win32 桌面动作)。
"""
import os
import sys
import io
import ctypes
import ctypes.wintypes
from ctypes import wintypes
import subprocess
import time
import json
import urllib.request
import threading
from datetime import datetime

import torch
from PIL import Image, ImageChops, ImageGrab, ImageStat

# ---- 路径(可通过环境变量覆盖) ----
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.environ.get("VISION_BACKEND", "gui_owl").strip().lower()
if BACKEND != "gui_owl":
    # GUI-Actor 已于 2026-09 移除(目录/仓库已删除), 不再支持回退到旧后端。
    # 防御: 即便调用方仍传 gui_actor, 也归一化为 gui_owl 并告警, 避免拉起失败。
    print(f"[agent] 警告: VISION_BACKEND={BACKEND!r} 已弃用, 强制使用 gui_owl (GUI-Owl-1.5-2B/Qwen3-VL)",
          file=sys.stderr, flush=True)
    BACKEND = "gui_owl"
# 推理前输入降采样: 把最长边缩到该像素值 (16:9 下 1280 ≈ 720p)。
# 实测 1080p->720p 端到端 1.70s->1.30s 且归一化坐标零损失 (坐标为 0~1 相对值,
# 不受缩放影响)。设 0 或负值 = 关闭降采样 (原生分辨率推理)。
# 注意(2026-09 诊断): grounding 真实瓶颈在 decode(~45ms/token, GPU 仅 35-40% 占用,
# host/launch 开销主导), 而非 prefill。1280->1024 只省 prefill 那 ~200ms 里的 ~30-40ms,
# 对总延迟(<1s, 其中 decode ~480ms)影响有限。进一步降分辨率收益递减, 勿过度追求。
try:
    VISION_MAX_SIDE = int(os.environ.get("VISION_MAX_SIDE", "1024").strip() or "0")
except ValueError:
    VISION_MAX_SIDE = 1024
# processor 端视觉 token 硬上限 (像素): 收紧后即使原生分辨率推理也被钳制,
# 与 VISION_MAX_SIDE 双保险, 控制 prefill 成本。2MP ≈ 可覆盖 1024 边长的常规截图。
# 用 VISION_MAX_PIXELS 覆盖; 0/负值=不限。
try:
    VISION_MAX_PIXELS = int(os.environ.get("VISION_MAX_PIXELS", str(2000 * 1000)).strip() or "0")
except ValueError:
    VISION_MAX_PIXELS = 2000 * 1000
GUI_OWL_MODEL = os.environ.get("GUI_OWL_MODEL", "").strip()
if not GUI_OWL_MODEL:
    _default_gui_owl = os.path.join(HERE, "models", "GUI-Owl-1.5-2B-Instruct")
    GUI_OWL_MODEL = _default_gui_owl if os.path.isdir(_default_gui_owl) else "mPLUG/GUI-Owl-1.5-2B-Instruct"

# GUI-Owl grounding 使用官方评测(ScreenSpot 系列)相同的系统提示词:
# 只允许 left_click / mouse_move, 屏幕按 1000x1000 归一化, 输出 <tool_call> JSON。
_GUI_OWL_SYSTEM_PROMPT = r'''# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name": "computer_use", "description": "Use a mouse to interact with a computer.\n* The screen's resolution is 1000x1000.\n* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.\n* don't use any other computer use tool like type, key, scroll, left_click_drag and so on.\n* you can only use the left_click and mouse_move action to interact with the computer. if you can't find the element, you should terminate the task and report the failure.", "parameters": {"properties": {"action": {"description": "The action to perform. The available actions are:\n* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.\n* `left_click`: Click the left mouse button with coordinate (x, y) pixel coordinate on the screen.", "enum": ["mouse_move", "left_click"], "type": "string"}, "coordinate": {"description": "(x, y): The x (pixels from the left edge) and y (pixels from the top edge) coordinates to move the mouse to. Required only by `action=mouse_move` and `action=left_click`.", "type": "array"}}, "required": ["action"], "type": "object"}}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>
'''
_GUI_OWL_INFEASIBLE_SUFFIX = (
    "\nAdditionally, if you think the task is infeasible (e.g., the task is not related to the image), "
    'return <tool_call>\n{"name": "computer_use", "arguments": {"action": "terminate", "status": "failure"}}\n</tool_call>'
)

# 紧凑输出格式: 单行 JSON, 输出 token 35->22, 1080p 生成 1678ms->1187ms,
# 真值精度无损 (0.21% vs 0.18%, 见 Vision/bench_format.py)。
# 坐标解析: _parse_gui_owl_points 的正则兜底天然兼容 "[x, y]" 形式。
_OWL_COMPACT_SYSTEM_PROMPT = (
    "# Task\n\n"
    "You control a computer mouse. The screen is 1000x1000 normalized.\n"
    "Locate the UI element described by the user and respond with ONLY one line:\n"
    '{"action": "left_click", "coordinate": [x, y]}\n'
    "where x, y are integers in 0~1000 marking the element center.\n"
    'If the element does not exist in the image, respond with ONLY: '
    '{"action": "terminate", "status": "failure"}\n'
    "No explanation, no markdown, no XML tags."
)

# GUI-Owl 输出格式选择: compact (默认, 更快) | tool_call (官方格式)
OWL_OUTPUT_FORMAT = os.environ.get("VISION_OWL_OUTPUT_FORMAT", "compact").strip().lower()

# GUI-Owl 推理优化: torch.compile (+ 可选 CUDA Graphs)
# 实测结论 (RTX 5070 Ti / torch 2.13 cu130, 固定图稳态 6 次中位):
#   eager (默认)             ~539 ms/次
#   torch.compile default    ~795 ms/次  (慢 ~47%)
#   torch.compile reduce-overhead (CUDA Graphs) ~787 ms/次 (慢 ~46%)
# 原因: 单次 generate() 序列逐 token 增长, shape 不断变化, CUDA Graphs 无法跨解码步复用,
#       仅 prefill 静态; inductor 图分派开销反而盖过 kernel 融合收益。对 2B 小模型净负收益。
# => 默认关闭; 仅在更大模型 / 静态 shape 批量场景实验性开启。
#   GUI_OWL_TORCH_COMPILE=1 开启; =0 关闭 (默认)。
#   GUI_OWL_COMPILE_MODE: default | reduce-overhead (后者内部用 CUDA Graphs 重放静态前向)。
#   GUI_OWL_STATIC_SHAPE=1 把推理图 letterbox 到固定 1280x720, 让 reduce-overhead 的 CUDA Graphs
#       有可能生效 (实验性: 黑边可能轻微影响贴边元素定位)。
TORCH_COMPILE = os.environ.get("GUI_OWL_TORCH_COMPILE", "0").strip().lower() not in ("0", "false", "no")
COMPILE_MODE = os.environ.get("GUI_OWL_COMPILE_MODE", "default").strip().lower()
STATIC_SHAPE = os.environ.get("GUI_OWL_STATIC_SHAPE", "0").strip().lower() in ("1", "true", "yes")

from playwright.sync_api import Error as PlaywrightError, sync_playwright  # noqa: E402

# ---- 配置 ----
VIEWPORT = {"width": 1280, "height": 800}
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
HEADLESS = os.environ.get("GUI_AGENT_HEADLESS", "0") == "1"
if os.name == "nt":
    try: ctypes.windll.user32.SetProcessDPIAware()
    except OSError: pass

# ---- 全局状态(懒加载) ----
_model = None
_processor = None
_tokenizer = None
_loaded_backend = None
_last_raw_output = ""
_pw = None
_browser = None
_page = None
_owns_browser = False
_browser_source = "none"
_SCREENSHOT_LOCK = threading.Lock()
# 置顶 (HWND_TOPMOST) 状态: 记录当前被钉在最上层的窗口, 供显式置顶/解除。
_ACTIVE_PIN_HWND = None
# 每次桌面/窗口操作前是否强制把目标窗口抬到最上层, 保证点击像素命中目标而非被遮挡误触。
# 默认开启; 置顶只在"操作瞬间"生效, 不长期占用最上层(避免打扰用户)。
_RAISE_ON_OPERATE = os.environ.get("GUI_RAISE_ON_OPERATE", "1").strip().lower() not in ("0", "false", "no")
# 视觉推理锁: torch generate 非线程安全, 且 _last_raw_output 是进程级共享。
# MCP(多 session)/多线程同时调用 ground 时必须串行化, 避免并发生成崩坏或结果串扰。
_INFERENCE_LOCK = threading.RLock()

_BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe", "firefox.exe"}
_CDP_ENDPOINTS = tuple(
    item.strip() for item in os.environ.get(
        "BROWSER_CDP_ENDPOINTS", "http://127.0.0.1:9222,http://127.0.0.1:9223,http://127.0.0.1:9333"
    ).split(",") if item.strip()
)


def _foreground_window():
    """Return the active Windows window without loading the vision model."""
    if os.name != "nt":
        return {"title": "", "pid": 0, "process_name": "", "is_browser": False}
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title, length + 1)
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process_name = ""
    try:
        import psutil
        process_name = psutil.Process(pid.value).name().lower()
    except Exception:
        pass
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "hwnd": int(hwnd),
        "title": title.value,
        "pid": int(pid.value),
        "process_name": process_name,
        "is_browser": process_name in _BROWSER_PROCESSES,
        "bounds": [rect.left, rect.top, rect.right, rect.bottom],
    }


def _available_cdp_endpoint():
    for endpoint in _CDP_ENDPOINTS:
        try:
            with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=0.35) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            if payload.get("webSocketDebuggerUrl"):
                return endpoint
        except Exception:
            continue
    return None


def _select_existing_page(browser, foreground_title: str = ""):
    pages = [p for context in browser.contexts for p in context.pages if not p.is_closed()]
    usable = [p for p in pages if p.url and not p.url.startswith(("devtools://", "chrome-extension://"))]
    pages = usable or pages
    if not pages:
        return None
    needle = foreground_title.casefold()
    if needle:
        for page in reversed(pages):
            try:
                title = page.title().casefold()
                if title and (title in needle or needle in title):
                    return page
            except Exception:
                continue
    return pages[-1]


def inspect_active_target():
    """Classify the active target and report whether its live DOM is readable."""
    window = _foreground_window()
    endpoint = _available_cdp_endpoint() if window["is_browser"] else None
    if window["is_browser"] and endpoint:
        mode, reason = "browser_dom", "active browser exposes a CDP DOM"
    elif window["is_browser"]:
        mode, reason = "browser_visual", "active browser does not expose a CDP DOM"
    else:
        mode, reason = "desktop_visual", "active program is not a supported browser page"
    return {**window, "mode": mode, "cdp_endpoint": endpoint or "", "reason": reason}


def backend_info() -> dict:
    """返回当前识别后端与模型信息, 供 MCP 工具/日志诊断。"""
    model = _gui_owl_model_path()
    return {
        "backend": BACKEND,
        "model": model,
        "arch": "qwen3_vl",
        "loaded": _model is not None,
        "device": str(getattr(_model, "device", "")),
        "owl_output_format": OWL_OUTPUT_FORMAT,
    }


def _gui_owl_model_path() -> str:
    """解析 GUI-Owl 模型来源: 本地目录优先, 否则回退 Hugging Face repo id。"""
    candidate = GUI_OWL_MODEL
    if os.path.isdir(candidate):
        return candidate
    if os.path.sep in candidate or ("/" in candidate or "\\" in candidate):
        # 环境变量指向了一个不存在的本地路径时, 回退官方 repo id
        return "mPLUG/GUI-Owl-1.5-2B-Instruct"
    return candidate


def load_model():
    """懒加载 GUI-Owl 模型 (仅 gui_owl 后端)。线程安全: 用锁防止并发首载双份。"""
    global _model, _processor, _tokenizer, _loaded_backend
    with _INFERENCE_LOCK:
        if _model is not None and _loaded_backend == BACKEND:
            return
        _model = _processor = _tokenizer = None
        _load_gui_owl()
        _loaded_backend = BACKEND


def _load_gui_owl():
    """加载 GUI-Owl-1.5 (Qwen3-VL) 标准生成式模型。"""
    global _model, _processor, _tokenizer
    try:
        from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
    except ImportError as exc:
        raise RuntimeError(
            "GUI-Owl 后端需要 transformers>=4.57 与 qwen-vl-utils>=0.0.14, "
            "请先升级依赖 (pip install -U 'transformers>=4.57' 'qwen-vl-utils>=0.0.14')"
        ) from exc
    model_path = _gui_owl_model_path()
    print(f"[agent] loading GUI-Owl ({model_path}) ...", file=sys.stderr, flush=True)
    # min/max_pixels 与官方 grounding 评测一致
    _processor = AutoProcessor.from_pretrained(
        model_path,
        min_pixels=196 * 32 * 32,
        # max_pixels 作为视觉 token 数量硬闸: 收紧到 ~2MP, 与 VISION_MAX_SIDE 双保险。
        max_pixels=VISION_MAX_PIXELS if VISION_MAX_PIXELS > 0 else 9800 * 32 * 32,
    )
    _tokenizer = _processor.tokenizer
    _model = Qwen3VLForConditionalGeneration.from_pretrained(
        model_path,
        dtype=torch.bfloat16,
        device_map="auto",
        attn_implementation="sdpa",
    ).eval()
    # 推理优化: torch.compile 编译前向 (reduce-overhead 模式内部使用 CUDA Graphs 重放
    # 静态 shape 前向)。动态截图场景用 default+dynamic=True 最稳; 整图编译失败则优雅退回 eager。
    if TORCH_COMPILE and _model.device.type == "cuda":
        try:
            _model = torch.compile(_model, mode=COMPILE_MODE, dynamic=(COMPILE_MODE != "reduce-overhead"))
            print(f"[agent] GUI-Owl torch.compile enabled (mode={COMPILE_MODE}, "
                  f"dynamic={COMPILE_MODE != 'reduce-overhead'})", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - 编译失败不应阻断推理
            print(f"[agent] torch.compile failed, fallback to eager: {exc!r}", file=sys.stderr, flush=True)
    print(f"[agent] GUI-Owl loaded on {_model.device}", file=sys.stderr, flush=True)


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def ensure_browser(prefer_existing: bool = True, allow_launch: bool = False):
    global _pw, _browser, _page, _owns_browser, _browser_source
    if (_page is not None and not _page.is_closed()
            and _browser is not None and _browser.is_connected()):
        return _page
    reset_browser()
    _pw = sync_playwright().start()
    if prefer_existing:
        endpoint = _available_cdp_endpoint()
        if endpoint:
            try:
                _browser = _pw.chromium.connect_over_cdp(endpoint)
                _page = _select_existing_page(_browser, _foreground_window().get("title", ""))
                if _page is not None:
                    _owns_browser = False
                    _browser_source = "existing_cdp"
                    return _page
            except PlaywrightError:
                _browser = None
                _page = None
    if not allow_launch:
        reset_browser()
        raise RuntimeError("current browser DOM is unavailable; new browser launch is forbidden, use existing browser window vision fallback")
    _browser = _pw.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    ctx = _browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=1,
        accept_downloads=False,
        user_agent=_USER_AGENT,
    )
    _page = ctx.new_page()
    _owns_browser = True
    _browser_source = "playwright_new"
    # 抹掉 webdriver 标记, 降低被风控识别的概率
    _page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return _page


def reset_browser():
    """Dispose a stale Playwright session without affecting the MCP server."""
    global _pw, _browser, _page, _owns_browser, _browser_source
    try:
        if _owns_browser and _page is not None and not _page.is_closed(): _page.context.close()
    except Exception:
        pass
    try:
        if _owns_browser and _browser is not None and _browser.is_connected(): _browser.close()
    except Exception:
        pass
    try:
        if _pw is not None: _pw.stop()
    except Exception:
        pass
    _page = None; _browser = None; _pw = None
    _owns_browser = False; _browser_source = "none"


def _adopt_latest_page(page):
    """Follow target=_blank/popups so subsequent steps verify the page that opened."""
    global _page
    pages = [item for item in page.context.pages if not item.is_closed()]
    latest = pages[-1] if pages else page
    if latest is not page:
        _page = latest
        latest.bring_to_front()
        try: latest.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightError: pass
    return _page or page


# ---------------- 公开 API ----------------
def navigate(url: str) -> str:
    last_error = None
    for attempt in range(2):
        try:
            page = ensure_browser()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            return page.url
        except PlaywrightError as exc:
            last_error = exc
            reset_browser()
            if attempt == 0: continue
    raise RuntimeError(f"browser navigation failed after session recovery: {last_error}")


def screenshot_pil() -> Image.Image:
    page = ensure_browser()
    png = page.screenshot()
    return Image.open(io.BytesIO(png)).convert("RGB")


def ground_image(instruction: str, img: Image.Image, topk: int = 3):
    """图像识别入口: 在给定图像上用 GUI-Owl 返回最多 topk 个归一化坐标 (0~1)。

    上层 click/type_text/窗口/桌面动作无需关心后端细节。
    线程安全: 整个推理在 _INFERENCE_LOCK 内串行执行(torch generate 非线程安全)。
    """
    load_model()
    with _INFERENCE_LOCK:
        return _gui_owl_ground(instruction, img, topk)


def last_raw_output() -> str:
    """返回最近一次视觉推理的模型原始输出文本, 供调试/诊断。
    若尚未推理过则返回空字符串。"""
    return _last_raw_output


def _downscale_for_inference(img: Image.Image) -> Image.Image:
    """按 VISION_MAX_SIDE 把输入图最长边降采样, 控制视觉 token 数以降低延迟。

    STATIC_SHAPE=1 时额外 letterbox 到固定 1280x720, 使前向 shape 稳定,
    让 torch.compile(mode=reduce-overhead) 的 CUDA Graphs 能稳定重放 (实验性)。
    """
    if VISION_MAX_SIDE > 0:
        w, h = img.size
        longest = max(w, h)
        if longest > VISION_MAX_SIDE:
            scale = VISION_MAX_SIDE / longest
            img = img.resize((max(32, round(w * scale)), max(32, round(h * scale))), Image.LANCZOS)
    if STATIC_SHAPE:
        TW, TH, PAD = 1280, 720, (248, 249, 250)  # 接近白底, 减少对定位的视觉干扰
        canvas = Image.new("RGB", (TW, TH), PAD)
        sw, sh = img.size
        scale = min(TW / sw, TH / sh)
        nw, nh = max(32, round(sw * scale)), max(32, round(sh * scale))
        img = img.resize((nw, nh), Image.LANCZOS)
        canvas.paste(img, ((TW - nw) // 2, (TH - nh) // 2))
        return canvas
    return img


def _gui_owl_ground(instruction: str, img: Image.Image, topk: int):
    """GUI-Owl 后端: 生成式 <tool_call> JSON, 解析 0~1000 相对坐标。"""
    global _last_raw_output
    img = _downscale_for_inference(img)
    if OWL_OUTPUT_FORMAT == "compact":
        sys_prompt = _OWL_COMPACT_SYSTEM_PROMPT
    else:
        sys_prompt = _GUI_OWL_SYSTEM_PROMPT + _GUI_OWL_INFEASIBLE_SUFFIX
    try:
        from qwen_vl_utils import process_vision_info
    except ImportError as exc:
        raise RuntimeError("GUI-Owl 后端需要 qwen-vl-utils>=0.0.14") from exc

    messages = [
        {
            "role": "system",
            "content": [{
                "type": "text",
                "text": sys_prompt,
            }],
        },
        {
            "role": "user",
            "content": [
                {"type": "image", "image": img},
                {"type": "text", "text": str(instruction)},
            ],
        },
    ]
    text = _processor.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = _processor(
        text=[text],
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    )
    inputs = inputs.to(_model.device)
    with torch.inference_mode():
        generated_ids = _model.generate(
            **inputs,
            max_new_tokens=256,
            # 视觉 grounding 是确定性定位任务: 用贪心解码(等价的 top_k=1 写法
            # 仍走采样分支且更慢), 结果可复现、首 token 延迟更低。
            do_sample=False,
            repetition_penalty=1.0,
        )
    generated_ids_trimmed = [
        out_ids[len(in_ids):]
        for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    output_text = _processor.batch_decode(
        generated_ids_trimmed,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]
    _last_raw_output = output_text
    return _parse_gui_owl_points(output_text, topk)


def _parse_gui_owl_points(output_text: str, topk: int = 3):
    """解析 GUI-Owl 输出中的点击坐标, 统一归一化到 0~1。

    优先解析 <tool_call> JSON (computer_use.arguments.coordinate, 0~1000),
    失败时回退正则匹配 "(x, y)" / "[x, y]"。
    terminate/answer 等非点击动作返回空列表。
    """
    import ast
    import re

    raw_points = []
    block_re = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL | re.IGNORECASE)
    for block in block_re.findall(output_text):
        block = block.strip()
        parsed = None
        try:
            parsed = json.loads(block)
        except Exception:
            try:
                parsed = ast.literal_eval(block)
            except Exception:
                parsed = None
        if not isinstance(parsed, dict):
            continue
        args = parsed.get("arguments")
        if not isinstance(args, dict):
            continue
        action = str(args.get("action") or "").lower()
        if action in ("terminate", "answer", "interact", "wait", "stop", "done"):
            continue
        coord = args.get("coordinate")
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            raw_points.append((coord[0], coord[1]))
    if not raw_points:
        pair_re = re.compile(
            r"[\(\[]\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*[\)\]]"
        )
        raw_points = pair_re.findall(output_text)

    points = []
    for x, y in raw_points:
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if fx > 1.0 or fy > 1.0:
            # GUI-Owl 屏幕按 1000x1000 归一化
            fx, fy = fx / 1000.0, fy / 1000.0
        points.append([
            max(0.0, min(1.0, fx)),
            max(0.0, min(1.0, fy)),
        ])
        if len(points) >= max(1, int(topk)):
            break
    return points


def ground(instruction: str, topk: int = 3):
    """在独立 Chromium 当前页面上定位。"""
    return ground_image(instruction, screenshot_pil(), topk)


def cuda_memory_status():
    """Return this Vision process' CUDA allocator usage in MiB."""
    if not torch.cuda.is_available():
        return {"available": False}
    device = torch.cuda.current_device()
    mib = 1024 * 1024
    return {
        "available": True,
        "device": device,
        "allocated_mib": round(torch.cuda.memory_allocated(device) / mib, 1),
        "reserved_mib": round(torch.cuda.memory_reserved(device) / mib, 1),
        "max_allocated_mib": round(torch.cuda.max_memory_allocated(device) / mib, 1),
        "max_reserved_mib": round(torch.cuda.max_memory_reserved(device) / mib, 1),
    }


def click(instruction: str, topk: int = 3, idx: int = 0, region: str = "full"):
    page = ensure_browser()
    image = screenshot_pil()
    regions = {
        "full": (0, 0, image.width, image.height),
        "left": (0, 0, int(image.width * 0.68), image.height),
        "right": (int(image.width * 0.32), 0, image.width, image.height),
        "top": (0, 0, image.width, int(image.height * 0.55)),
        "bottom": (0, int(image.height * 0.45), image.width, image.height),
    }
    box = regions.get(str(region).lower(), regions["full"])
    cropped = image.crop(box)
    pts = ground_image(instruction, cropped, topk=topk)
    if not pts:
        return {"clicked": False, "reason": "model returned no point", "all_points": [], "raw_output": _last_raw_output}
    x, y = pts[min(idx, len(pts) - 1)]
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    px = box[0] + int(x * cropped.width)
    py = box[1] + int(y * cropped.height)
    global_x, global_y = px / image.width, py / image.height
    snap = page.evaluate("""({x,y}) => {
      const direct = document.elementFromPoint(x, y)?.closest('a,button,input,[role=button]');
      const usable = e => e && (() => { const r=e.getBoundingClientRect(); return r.width>2 && r.height>2; })();
      let best = usable(direct) ? direct : null, bestDistance = Infinity;
      if (!best) for (const e of document.querySelectorAll('a,button,input,[role=button]')) {
        const r=e.getBoundingClientRect();
        if (r.width<=2 || r.height<=2 || r.bottom<0 || r.top>innerHeight) continue;
        const cx=Math.max(r.left,Math.min(x,r.right)), cy=Math.max(r.top,Math.min(y,r.bottom));
        const d=Math.hypot(cx-x,cy-y);
        if (d<bestDistance && d<=90) { best=e; bestDistance=d; }
      }
      if (!best) return null;
      const r=best.getBoundingClientRect();
      return {x:r.left+r.width/2,y:r.top+r.height/2,tag:best.tagName,text:(best.innerText||best.getAttribute('aria-label')||'').trim().slice(0,120),href:best.href||''};
    }""", {"x": px, "y": py})
    if snap:
        px, py = int(snap["x"]), int(snap["y"])
        global_x, global_y = px / image.width, py / image.height
    page.mouse.click(px, py)
    page.wait_for_timeout(600)
    page = _adopt_latest_page(page)
    return {
        "clicked": True,
        "instruction": instruction,
        "backend": BACKEND,
        "norm": [round(global_x, 4), round(global_y, 4)],
        "pixel": [px, py],
        "region": str(region).lower(),
        "snapped": snap,
        "all_points": [[round((box[0] + p[0] * cropped.width) / image.width, 4), round((box[1] + p[1] * cropped.height) / image.height, 4)] for p in pts],
        "url": page.url,
    }


def type_text(instruction: str, text: str, topk: int = 3):
    page = ensure_browser()
    pts = ground(instruction, topk=topk)
    if not pts:
        return {"typed": False, "reason": "model returned no point", "raw_output": _last_raw_output}
    x, y = pts[0]
    px = int(max(0.0, min(1.0, x)) * VIEWPORT["width"])
    py = int(max(0.0, min(1.0, y)) * VIEWPORT["height"])
    page.mouse.click(px, py)
    page.wait_for_timeout(300)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=20)
    return {"typed": True, "backend": BACKEND, "pixel": [px, py], "text": text}


def type_active_text(text: str, clear: bool = True):
    """Type into the element focused by a preceding visual click."""
    page = ensure_browser()
    if clear:
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
    page.keyboard.type(str(text), delay=20)
    return {"typed": True, "focused": True, "cleared": bool(clear), "text_length": len(str(text)), "url": page.url}


def scroll(direction: str = "down", amount: int = 400):
    page = ensure_browser()
    dy = amount if direction.lower() == "down" else -amount
    page.mouse.wheel(0, dy)
    page.wait_for_timeout(500)
    return {"scrolled": direction, "amount": amount}


def get_url() -> str:
    return ensure_browser().url


def web_read(max_chars: int = 12000):
    """Read DOM text and interactive element metadata without image inference."""
    page = ensure_browser()
    data = page.evaluate("""() => ({
      title: document.title,
      url: location.href,
      text: (document.body?.innerText || '').slice(0, 30000),
      links: [...document.querySelectorAll('a')].slice(0, 150).map((e, i) => ({i, text:(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,160), href:e.href})),
      buttons: [...document.querySelectorAll('button,[role=button]')].slice(0,100).map((e, i) => ({i, text:(e.innerText||e.getAttribute('aria-label')||e.title||'').trim().slice(0,160)})),
      inputs: [...document.querySelectorAll('input,textarea,[contenteditable=true]')].slice(0,80).map((e, i) => ({i, type:e.type||'', name:e.name||'', placeholder:e.placeholder||'', aria:e.getAttribute('aria-label')||''}))
    })""")
    data["text"] = str(data.get("text", ""))[:max(1000, min(int(max_chars), 30000))]
    data["browser_source"] = _browser_source
    data["dom_available"] = True
    return data


def web_click_text(text: str, exact: bool = False):
    page = ensure_browser(); locator = page.get_by_text(text, exact=exact)
    count = locator.count()
    if not count: return {"clicked": False, "reason": "text not found", "text": text}
    locator.first.click(timeout=10000)
    page.wait_for_timeout(500)
    page = _adopt_latest_page(page)
    return {"clicked": True, "text": text, "matches": count, "url": page.url}


def web_fill(field: str, text: str, submit: bool = False):
    page = ensure_browser(); candidates = [
        page.get_by_placeholder(field, exact=False), page.get_by_label(field, exact=False),
        page.locator(f'input[name="{field}"], textarea[name="{field}"]'),
    ]
    locator = next((item.first for item in candidates if item.count()), None)
    if locator is None:
        locator = page.locator("input:not([type=hidden]), textarea, [contenteditable=true]").first
    if not locator.count(): return {"filled": False, "reason": "input not found", "field": field}
    locator.fill(text); locator.press("Enter") if submit else None
    page.wait_for_timeout(500)
    return {"filled": True, "field": field, "text_length": len(text), "submitted": submit, "url": page.url}


def web_press(key: str):
    page = ensure_browser(); page.keyboard.press(key); page.wait_for_timeout(300)
    return {"pressed": key, "url": page.url}


def web_play_media():
    """Start the first HTML media element without visual grounding."""
    page = ensure_browser()
    result = page.evaluate("""async () => {
      const media = document.querySelector('video, audio');
      if (!media) return {played:false, reason:'media element not found'};
      media.muted = false;
      try { await media.play(); return {played:!media.paused, currentTime:media.currentTime, duration:media.duration}; }
      catch (e) { return {played:false, reason:String(e)}; }
    }""")
    return {**result, "url": page.url}


def wait(ms: int = 1000):
    ensure_browser().wait_for_timeout(ms)
    return True


def play_video(instruction: str = "click the play button to start the video"):
    """便捷封装: 找并点击播放按钮, 开始播放视频。"""
    return click(instruction)


# ---------------- Windows 全桌面视觉控制 ----------------
def _primary_screen():
    user32 = ctypes.windll.user32
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _session_screen_unavailable() -> str:
    """检测屏幕当前是否不可截取(锁屏/安全桌面/会话断开), 返回原因或空字符串。"""
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        if user32.GetForegroundWindow() == 0:
            # 前台窗口为 0 不一定代表锁屏：RDP/自动化场景下后台进程自身没有前台窗口，
            # 但桌面上仍有可见窗口。仅当完全没有可见顶层窗口时才判定为不可用。
            if not list_windows():
                return "屏幕当前不可用(可能已锁定、显示安全桌面或会话断开), 无法截图"
    except OSError:
        pass
    return ""


def _grab_windows_image(*, hwnd: int | None = None, bbox=None, all_screens: bool = False, attempts: int = 3) -> Image.Image:
    """Serialize and retry Pillow GDI captures; prefer HWND capture for windows."""
    locked_reason = _session_screen_unavailable()
    if locked_reason:
        # 屏幕不可用时重试没有意义, 一次失败后直接给出明确原因
        attempts = 1
    errors = []
    with _SCREENSHOT_LOCK:
        for attempt in range(max(1, attempts)):
            strategies = []
            if hwnd:
                strategies.append(("hwnd", {"window": int(hwnd), "include_layered_windows": True}))
            if bbox is not None:
                strategies.append(("bbox", {"bbox": bbox, "all_screens": True}))
            if not strategies:
                strategies.append(("desktop", {"all_screens": all_screens}))
            for label, kwargs in strategies:
                source = None
                try:
                    source = ImageGrab.grab(**kwargs)
                    converted = source.convert("RGB")
                    if label == "hwnd":
                        sample = converted.resize((32, 32))
                        stats = ImageStat.Stat(sample)
                        if max(stats.mean) < 2.0 and max(stats.stddev) < 1.0:
                            errors.append("hwnd: captured image is blank; falling back to screen bounds")
                            if sample is not converted:
                                sample.close()
                            if converted is not source:
                                converted.close()
                            continue
                        if sample is not converted:
                            sample.close()
                    if converted is source:
                        if hasattr(converted, "copy"):
                            return converted.copy()
                        source = None
                    return converted
                except OSError as exc:
                    errors.append(f"{label}: {exc}")
                finally:
                    if source is not None and hasattr(source, "close"):
                        source.close()
            if attempt + 1 < attempts:
                time.sleep(0.15 * (attempt + 1))
    detail = locked_reason or (errors[-1] if errors else "unknown capture error")
    raise RuntimeError(f"screen grab failed after {max(1, attempts)} attempts: {detail}")


def desktop_screenshot_pil() -> Image.Image:
    """只截取 Windows 主显示器，降低视觉推理开销。"""
    if os.name != "nt": raise RuntimeError("桌面视觉控制目前只支持 Windows")
    try:
        return _grab_windows_image(all_screens=False)
    except RuntimeError:
        # A detached/transitioning desktop can make BitBlt fail while PrintWindow
        # still works. The active window is the most relevant safe fallback.
        window = _foreground_window()
        left, top, right, bottom = window.get("bounds", [0, 0, 0, 0])
        bbox = (left, top, right, bottom) if right > left and bottom > top else None
        return _grab_windows_image(hwnd=int(window.get("hwnd") or 0), bbox=bbox, all_screens=True)


def list_windows(title_contains: str = ""):
    """List visible top-level windows with PID, title and screen bounds."""
    user32 = ctypes.windll.user32
    results = []
    needle = str(title_contains).lower().strip()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd): return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0: return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or (needle and needle not in title.lower()): return True
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)): return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_path = ""
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                size = ctypes.c_ulong(32768); path_buf = ctypes.create_unicode_buffer(size.value)
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                    process_path = path_buf.value
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        results.append({"hwnd": int(hwnd), "pid": int(pid.value), "title": title,
                        "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                        "process_name": os.path.basename(process_path).lower(), "process_path": process_path})
        return True

    user32.EnumWindows(callback, 0)
    return results


def _enum_hidden_main_windows(process_path: str = "", process_name: str = "", hwnd: int = 0):
    """枚举一个进程名下“真实存在但被隐藏/不可见”的顶层主窗体窗口。

    桌面应用（如网易云、部分 Electron/CEF 壳）常会“关闭即驻留后台/托盘”：进程健康运行、
    主窗口也真实存在（甚至带当前内容标题），但 WS_VISIBLE 位被清掉，导致 list_windows(只列
    IsWindowVisible) 永久看不见它。此类窗口不能靠 IsWindowVisible 判定“应用没开”，否则 agent
    会误判未启动→反复 kill/重启(常因权限被拒)→空转烧预算。

    这里按进程映像定位其真正的 UI 主窗体：过滤掉 IME/消息钩子/媒体SMTC/迷你类等辅助窗，
    只保留“有正常屏幕尺寸、是应用主窗体类(带 Chrome/Orpheus 或普通顶层框)或有非空标题”的候选，
    返回带 hidden=True 标记的窗口字典（兼容 _find_window/activate 的字段结构）。
    """
    user32 = ctypes.windll.user32
    pid_targets: set[int] = set()

    def _pid_of(hwnd_candidate: int) -> int:
        p = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd_candidate), ctypes.byref(p))
        return int(p.value)

    # 需要先解析 hwnd/进程名→PID；进程映像名用 tasklist 或 OpenProcess 逐窗核对较繁，
    # 这里统一用“进程路径 basename”/hwnd 过滤，避免跨进程误伤。
    match_basename = ""
    if process_name:
        match_basename = os.path.basename(os.path.normcase(os.path.normpath(str(process_name))))
    if process_path:
        match_basename = os.path.basename(os.path.normcase(os.path.normpath(str(process_path))))

    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd_candidate, _):
        pid = _pid_of(int(hwnd_candidate))
        if hwnd:
            if int(hwnd_candidate) != int(hwnd):
                return True
        else:
            # 只在没有 hwnd 精确值时按进程过滤
            if pid not in pid_targets:
                return True
        if user32.IsWindowVisible(hwnd_candidate):
            return True  # 可见窗口走常规 list_windows 路径
        ln = user32.GetWindowTextLengthW(hwnd_candidate)
        buf = ctypes.create_unicode_buffer(ln + 1)
        user32.GetWindowTextW(hwnd_candidate, buf, ln + 1)
        title = buf.value.strip()
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd_candidate, cls, 256)
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd_candidate, ctypes.byref(rect)):
            return True
        w = rect.right - rect.left; h = rect.bottom - rect.top
        # 过滤辅助/系统窗：几乎必然不是可操作主窗体
        lowcls = cls.value.casefold()
        # 明确是桌面歌词/迷你播放器/托盘图标/系统辅助等，不可能是主操作界面
        aux_classes = ("desktoplyrics", "miniplayer", "icon", "msctfime", "ime",
                       "messagewindow", "systemmessagewindow", "gdi+ hook", "minidump",
                       "powermessagewindow", "smtextitlehost", "titlebar", "tooltips_class32")
        if any(blk in lowcls for blk in aux_classes):
            return True
        if w <= 80 or h <= 60:      # 尺寸过小(IME/SMTC/系统小窗等)
            return True
        results.append({
            "hwnd": int(hwnd_candidate), "pid": pid, "title": title,
            "bounds": [rect.left, rect.top, rect.right, rect.bottom],
            "process_name": "", "process_path": "", "hidden": True,
            "window_class": cls.value,
        })
        return True

    # 解析进程名/路径 → PID（用 tasklist；Windows 控制台默认 OEM 编码，须显式指定避免中文环境乱码）
    import subprocess
    if match_basename and not hwnd:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {match_basename}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=20,
            )
            text = out.stdout.decode("oem", errors="replace")
            for line in text.splitlines():
                parts = [p.strip().strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[1].isdigit():
                    pid_targets.add(int(parts[1]))
        except Exception:
            pass

    # hwnd 精确值时无需 pid 过滤，直接枚举全部顶层窗找该 hwnd
    user32.EnumWindows(callback, 0)
    if not hwnd and not pid_targets:
        return []
    return results


def _find_window(title_contains: str):
    """Resolve a window from its title, HWND, process name, or process path.

    `list_windows` exposes all four fields.  A model may legitimately return the
    process field it just observed, so activation must not interpret every value
    as title text only.

    兜底: 可见窗口找不到时, 若 reference 是精确标识(hwnd / 完整路径 / 进程映像 basename),
    会回退去枚举该进程名下“隐藏但真实存在”的主窗体, 使“进程健康但主窗被隐藏(WS_VISIBLE未置位)”
    的应用(如网易云后台驻留)仍可被按进程/hwnd 激活, 而不是被误判为未启动。
    """
    reference = str(title_contains or "").strip()
    if not reference:
        raise RuntimeError("window reference is empty")
    matches = list_windows(reference)
    if matches:
        return matches[0]

    windows = list_windows()
    folded = os.path.normcase(os.path.normpath(reference))
    basename = os.path.basename(folded)
    for window in windows:
        if reference.isdigit() and int(reference) == int(window.get("hwnd", 0)):
            return window
        process_path = os.path.normcase(os.path.normpath(str(window.get("process_path") or "")))
        process_name = str(window.get("process_name") or "").casefold()
        if folded and process_path == folded:
            return window
        if basename and (process_name == basename.casefold() or os.path.basename(process_path) == basename):
            return window
    # —— 隐藏主窗体兜底 ——
    if basename or reference.isdigit():
        try:
            hidden = _enum_hidden_main_windows(
                hwnd=int(reference) if reference.isdigit() else 0,
                process_path=folded if os.path.sep in reference else "",
                process_name=basename,
            )
        except Exception:
            hidden = []
        if hidden:
            # 主窗体通常是同进程下屏幕面积最大的窗口(桌面歌词/迷你播放器等均为小浮窗)，
            # 按面积取最大最稳；带当前内容标题(如网易云"歌名-歌手")的候选可作次级偏好。
            def _area(w):
                b = w.get("bounds") or [0, 0, 0, 0]
                return max(0, (b[2] - b[0])) * max(0, (b[3] - b[1]))
            best = max(hidden, key=_area)
            big = [w for w in hidden if _area(w) >= 0.5 * _area(best)]
            titled = [w for w in big if w.get("title")]
            if titled:
                return max(titled, key=_area)
            return best
    available = [str(item.get("title") or "") for item in windows[:8]]
    raise RuntimeError(f"window not found: {reference}; available titles: {available}")


def _raise_window_above(window: dict) -> bool:
    """把窗口可靠地抬到其它窗口之上、并争取拿到前台焦点。

    Windows 会拦截非前台进程的 SetForegroundWindow(焦点窃取保护), 仅靠它常常抬不动被遮挡的窗口,
    导致后续按屏幕坐标的点击落到上面那层 → 误操作。这里用 "闪现置顶(HWND_TOPMOST 亮一下再撤)"
    强制把窗口调到 Z 序最前, 即使 SetForegroundWindow 被拒也能确保它可见在最上层。
    返回是否成功拿到前台。
    """
    user32 = ctypes.windll.user32
    hwnd = int(window["hwnd"])
    SWP_NOSIZE = 0x0001; SWP_NOMOVE = 0x0002; SWP_SHOWWINDOW = 0x0040; SWP_NOACTIVATE = 0x0010
    HWND_TOPMOST = -1; HWND_NOTOPMOST = -2
    try:
        # 对“进程健康但主窗体被隐藏(WS_VISIBLE 未置位)”的窗口，先强制显示再抬升。
        # 这类窗口来自 _find_window 的隐藏主窗体兜底(带 hidden=True)；它不透明地躲过了
        # list_windows(只列可见)，若只 SetForeground 而不 ShowWindow(SW_SHOW)，画面不会真正回到屏幕。
        if window.get("hidden") or not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE (顺带还原最小化)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # 被 pin_window_topmost 持久置顶的窗口要保持 TOPMOST, 否则下面的"闪现置顶"把它撤成
        # NOTOPMOST 会悄悄让持久置顶失效(窗口回到普通层级, 之后被其它窗口遮挡)。
        persist_topmost = _ACTIVE_PIN_HWND == hwnd
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        if not persist_topmost:
            # 普通窗口: 闪现置顶后立即撤掉, 避免长期置顶占用。
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    # 尽量拿前台; 拿不到也已在最上层, 点击坐标已可命中。
    try:
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.35)
    try:
        return int(user32.GetForegroundWindow()) == hwnd
    except Exception:
        return False


def _set_topmost(hwnd: int, enable: bool) -> bool:
    """设置/解除窗口的持久置顶状态(HWND_TOPMOST)。返回操作是否成功。"""
    user32 = ctypes.windll.user32
    SWP_NOSIZE = 0x0001; SWP_NOMOVE = 0x0002; SWP_SHOWWINDOW = 0x0040; SWP_NOACTIVATE = 0x0010
    flag = -1 if enable else -2  # HWND_TOPMOST / HWND_NOTOPMOST
    try:
        ok = bool(user32.SetWindowPos(hwnd, flag, 0, 0, 0, 0,
                                      SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE))
    except Exception:
        ok = False
    return ok


def pin_window_topmost(title_contains: str, pin: bool = True) -> dict:
    """把指定窗口显式置顶(始终显示在最上层)或解除置顶。

    适合多步骤连续操作同一窗口(避免每步都要重新激活/反复确认窗口在最前,
    直接削减中间检查步骤); 操作结束后应传 pin=False 解除, 避免长期遮挡用户。
    返回当前置顶的窗口信息与置顶状态。
    """
    global _ACTIVE_PIN_HWND
    window = _find_window(title_contains); hwnd = int(window["hwnd"])
    if pin:
        if _ACTIVE_PIN_HWND and _ACTIVE_PIN_HWND != hwnd:
            _set_topmost(_ACTIVE_PIN_HWND, False)  # 先解除上一个, 避免多个窗口同时置顶打架
        _set_topmost(hwnd, True)
        _ACTIVE_PIN_HWND = hwnd
    else:
        if _ACTIVE_PIN_HWND == hwnd or title_contains:
            _set_topmost(hwnd, False)
            if _ACTIVE_PIN_HWND == hwnd:
                _ACTIVE_PIN_HWND = None
    return {"pinned": bool(pin), "topmost": _ACTIVE_PIN_HWND is not None,
            "window": window, "next_action": None if pin else "已解除置顶, 窗口恢复普通层级"}


def unpin_topmost() -> dict:
    """解除当前所有置顶窗口, 让桌面恢复普通层级。"""
    global _ACTIVE_PIN_HWND
    if _ACTIVE_PIN_HWND:
        _set_topmost(_ACTIVE_PIN_HWND, False)
        _ACTIVE_PIN_HWND = None
        return {"ok": True, "unpinned": True, "message": "已解除置顶"}
    return {"ok": True, "unpinned": False, "message": "当前没有置顶窗口"}


def activate_window(title_contains: str):
    window = _find_window(title_contains)
    got_fg = _raise_window_above(window)
    return {"activated": True, "foreground": bool(got_fg), "note": None if got_fg else "已抬到最上层但未拿到前台焦点(仍可安全点击)", **window}


def _ensure_raise_on_operate(window: dict) -> None:
    """桌面/窗口操作落点前, 若配置开启, 再抬一次目标窗口确保它在最前不被遮挡。

    截图与点击之间若有别的窗口弹出抢焦点, 会让后续屏幕坐标点击落到错误窗口(误操作)。
    在真正发鼠标事件前补一次抬升, 命中率更高、事后也无需额外截图核对层级。
    """
    if _RAISE_ON_OPERATE and window and window.get("hwnd"):
        try:
            _raise_window_above(window)
        except Exception:
            pass


def window_screenshot_pil(title_contains: str) -> Image.Image:
    window = _find_window(title_contains)
    left, top, right, bottom = window["bounds"]
    bbox = (left, top, right, bottom)
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise RuntimeError("target window has invalid bounds")
    return _grab_windows_image(hwnd=int(window["hwnd"]), bbox=bbox, all_screens=True)


def _capture_window_info(window: dict) -> Image.Image:
    left, top, right, bottom = window["bounds"]
    if right <= left or bottom <= top: raise RuntimeError("target window has invalid bounds")
    return _grab_windows_image(hwnd=int(window["hwnd"]), bbox=(left, top, right, bottom), all_screens=True)


_UIA_TARGET_TYPES = {"Button", "Hyperlink", "TabItem", "Edit", "MenuItem", "ListItem", "RadioButton", "CheckBox", "ComboBox", "SplitButton"}
# UIA 读取窗口结构依赖 pywinauto(纯 Python + Windows UIAutomation COM)，非必需。
# 缺失时所有"读窗口内容"工具回退为结构不可读(仅视觉)。
_uia_import_error = None
try:
    from pywinauto import Desktop as _UiaDesktop  # noqa: E402
except Exception as _exc:  # pragma: no cover
    _uia_import_error = _exc
    _UiaDesktop = None


def _normalize_control_name(name: str) -> str:
    """去掉控件可访问名里的装饰/图标说明，只留用于匹配的关键词。"""
    if not name:
        return ""
    lowered = name.lower()
    for token in (" 图片", " 链接", " (alt+", "按钮", "资料"):
        lowered = lowered.replace(token, " ")
    return " ".join(lowered.split())


def _matches_target(name: str, target: str) -> float:
    """判断控件可访问名与目标描述是否命中，返回 0~1 置信度(0=未命中)。

    策略: 目标通常是短词/短语(如 '发动态'、'投稿'、'背包')。只要目标里的
    核心词整体出现在控件名里即算强命中; 允许逐字包含的弱命中兜底。
    """
    if not name or not target:
        return 0.0
    norm_name = _normalize_control_name(name).lower()
    if not norm_name:
        return 0.0
    # 逐字(非标点)拆目标，去掉空白与常见填充
    import re as _re
    tokens = [t for t in _re.split(r"[\s，,。/]+", str(target).lower()) if t]
    if not tokens:
        return 0.0
    matched = sum(1 for t in tokens if t in norm_name)
    if matched == len(tokens) and tokens:
        return 1.0 if len(tokens) == 1 else 0.95
    if matched:
        return 0.5 * matched / len(tokens)
    return 0.0


def _uia_walk_controls(win, max_items: int = 120):
    """深度遍历 UIA 控件树，收集可点控件(带屏幕矩形)。失败时返回 []。"""
    if _UiaDesktop is None:
        return []
    collected = []
    _seen = set()

    def _rect_key(r):
        try:
            return (r.left // 16, r.top // 16, r.width() // 16, r.height() // 16)
        except Exception:
            return None

    def _walk(el, depth: int = 0):
        if depth > 22 or len(collected) >= max_items:
            return
        try:
            info = el.element_info
            ctrl_type = info.control_type or ""
            name = (info.name or "").strip()
        except Exception:
            return
        if ctrl_type in _UIA_TARGET_TYPES and name:
            try:
                r = el.rectangle()
                # 跳过完全在屏幕外 / 零尺寸的伪控件
                if r.width() <= 0 or r.height() <= 0 or r.left < -60000 or r.top < -60000:
                    pass
                else:
                    key = _rect_key(r)
                    item = {
                        "type": ctrl_type, "name": name[:80],
                        "left": int(r.left), "top": int(r.top),
                        "width": int(r.width()), "height": int(r.height()),
                        "cx": int(r.left + r.width() / 2), "cy": int(r.top + r.height() / 2),
                    }
                    if key is None or key not in _seen:
                        _seen.add(key)
                        collected.append(item)
            except Exception:
                pass
        try:
            children = el.children()
        except Exception:
            return
        for child in children:
            _walk(child, depth + 1)

    try:
        _walk(win)
    except Exception:
        return []
    return collected


_BROWSER_CHROME_NAMES = {"最小化", "最大化", "关闭", "还原", "返回", "刷新", "主页", "查看站点信息", "地址和搜索栏", "搜索标签页", "新建标签页", "标签页组", "包含隐藏的收藏夹的菜单", "其他收藏夹", "编辑此页面的收藏夹(Ctrl+D)", "设置及其他 (Alt+F)", "历史记录", "屏幕截图", "扩展", "个人 个人资料", "个人资料"}


def _uia_walk_with_warmup(win, max_items: int = 160, warmup: bool = True):
    """枚举控件；若结果几乎全是浏览器 chrome(工具栏) 而无页面内容，说明 Chromium
    尚未为页面构建可访问性树，等待后重试一次以触发页面树生成。返回 (controls, was_retried)。"""
    controls = _uia_walk_controls(win, max_items=max_items)
    if not warmup or not controls:
        return controls, False
    # 统计"非 chrome"的页面控件数
    page_like = [c for c in controls if _normalize_control_name(c["name"]) not in _BROWSER_CHROME_NAMES]
    if len(page_like) < 4 and len(controls) <= 25:
        time.sleep(1.0)
        try:
            controls2 = _uia_walk_controls(win, max_items=max_items)
        except Exception:
            controls2 = controls
        if controls2:
            controls = controls2
            return controls, True
    return controls, False


def window_read_targets(title_contains: str, keywords: str = "", max_items: int = 80):
    """读取目标窗口的"内容结构"(UIA 可点控件: 文本+屏幕矩形)，供文本精确点击。

    优先于全屏截图: 原生窗口与 Chromium/Edge 网页都能枚举出按钮/链接的文本与坐标，
    文本命中即可零漂移点击。对游戏/自绘全屏(无 UIA 结构)返回 structure_readable=False。
    """
    result = {
        "title_contains": title_contains, "structure_readable": False,
        "method": "uia", "targets": [], "count": 0, "note": "",
    }
    if _UiaDesktop is None:
        result["note"] = f"pywinauto 不可用: {_uia_import_error}"
        return result
    window = _find_window(title_contains)
    if not window or not window.get("hwnd"):
        result["note"] = f"找不到窗口: {title_contains}"
        return result
    try:
        activate_window(title_contains)
    except Exception:
        pass
    try:
        win = _UiaDesktop(backend="uia").window(handle=int(window["hwnd"]))
        win.wait("exists", timeout=8)
        # 有关键词过滤时内部遍历预算放宽到 400，避免浏览器 chrome 抢先占满预算导致页面目标漏采
        walk_cap = 400 if keywords.strip() else max_items
        controls, _was_warm = _uia_walk_with_warmup(win, max_items=max(walk_cap, max_items))
    except Exception as exc:
        # 失败多为游戏/无 UIA 提供者的自绘窗口
        result["note"] = f"UIA 读取失败(可能为游戏/自绘全屏无控件树): {type(exc).__name__}: {exc}"
        result["structure_readable"] = False
        return result
    result["note"] = ("已读取到窗口 UIA 控件树(含预热重试)，可用文本精确定位点击，无需视觉猜像素。"
                      if _was_warm else
                      "已读取到窗口 UIA 控件树，可用文本精确定位点击，无需视觉猜像素。")
    needle = _normalize_control_name(keywords)
    if needle:
        controls = [c for c in controls if _matches_target(c["name"], needle) > 0.0]
    # 去重 + 排序(按名称可读性优先展示 Button/Hyperlink)
    _order = {"Button": 0, "Hyperlink": 1, "TabItem": 2, "MenuItem": 3, "Edit": 4, "ListItem": 5}
    controls.sort(key=lambda c: (_order.get(c["type"], 9), c["top"], c["left"]))
    result["targets"] = controls[:max_items]
    result["count"] = len(result["targets"])
    result["structure_readable"] = True
    return result


def window_text_click(title_contains: str, target: str, candidate_index: int = 0):
    """在目标窗口 UIA 控件树里按文本精确命中 target，点其矩形中心。

    返回 (ok, payload)。ok=True 表示已按文本坐标点击; ok=False 且 structure_readable=False
    表示该窗口无结构可读(游戏等)只能视觉; ok=False 且 matched=0 表示没找到该文本控件。
    """
    base = {
        "instruction": target, "window_title_contains": title_contains,
        "method": "uia_text", "clicked": False,
    }
    if _UiaDesktop is None:
        return False, {**base, "reason": f"pywinauto 不可用: {_uia_import_error}", "structure_readable": False}
    window = _find_window(title_contains)
    if not window or not window.get("hwnd"):
        return False, {**base, "reason": f"找不到窗口: {title_contains}", "structure_readable": False}
    try:
        activate_window(title_contains)
    except Exception:
        pass
    try:
        win = _UiaDesktop(backend="uia").window(handle=int(window["hwnd"]))
        win.wait("exists", timeout=8)
        controls, _was_warm = _uia_walk_with_warmup(win, max_items=160)
    except Exception as exc:
        return False, {**base, "reason": f"UIA 读取失败(可能为游戏/自绘全屏无控件树): {type(exc).__name__}", "structure_readable": False}
    # 对每个控件算匹配分，选最高分候选
    needle = _normalize_control_name(target)
    scored = []
    for c in controls:
        score = _matches_target(c["name"], needle)
        if score > 0.0:
            scored.append((score, c))
    if not scored:
        return False, {**base, "reason": f"控件树可读，但未找到文本命中 '{target}' 的可点元素", "structure_readable": True, "count": len(controls)}
    scored.sort(key=lambda x: (-x[0], x[1]["cy"], x[1]["cx"]))
    chosen = scored[min(candidate_index, len(scored) - 1)][1]
    px, py = chosen["cx"], chosen["cy"]
    _ensure_raise_on_operate(window)  # 文本精确点击前也抬升, 杜绝被遮挡落错窗
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    img_before = None
    try:
        img_before = window_screenshot_pil(title_contains)
    except Exception:
        img_before = None
    evidence = {}
    if img_before is not None:
        try:
            evidence = _wait_and_compare_window(window, img_before, str(window.get("title", "")))
        except Exception as exc:
            evidence = {"execution_likely_succeeded": False, "reason": f"验证截图失败: {exc}"}
    return True, {
        **base, "clicked": True, "pixel": [px, py], "matched_name": chosen["name"],
        "match_score": round(scored[0][0], 2), "count": len(controls),
        "all_matches": [{"name": c["name"], "score": round(s, 2), "pixel": [c["cx"], c["cy"]]} for s, c in scored[:6]],
        "structure_readable": True, **evidence,
    }


def _window_title_by_hwnd(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value.strip()


def _visual_change_evidence(before: Image.Image, after: Image.Image, before_title: str = "", after_title: str = "") -> dict:
    """Compare two screenshots cheaply; this does not invoke the vision model again."""
    target = (320, 180)
    left = before.convert("L").resize(target)
    right = after.convert("L").resize(target)
    difference = ImageChops.difference(left, right)
    mean_delta = float(ImageStat.Stat(difference).mean[0]) / 255.0
    histogram = difference.histogram()
    changed_pixels = sum(histogram[12:])
    change_ratio = changed_pixels / float(target[0] * target[1])
    title_changed = bool(after_title and after_title != before_title)
    state_changed = bool(title_changed or change_ratio >= 0.0015 or mean_delta >= 0.001)
    return {
        "post_screenshot_captured": True,
        "waited_ms": max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))),
        "state_changed": state_changed,
        "title_changed": title_changed,
        "visual_change_ratio": round(change_ratio, 6),
        "visual_mean_delta": round(mean_delta, 6),
        "execution_likely_succeeded": state_changed,
        "next_action": (
            "操作后画面已变化；重新读取当前页面或窗口语义，再根据新状态继续下一步"
            if state_changed else
            "操作后画面没有明显变化；不要假设成功，应重新识别目标、切换候选点或改用其他操作方式"
        ),
    }


def _wait_and_compare_window(window: dict, before: Image.Image, before_title: str = "", wait_ms: int | None = None) -> dict:
    delay = max(100, int(wait_ms if wait_ms is not None else os.environ.get("GUI_POST_ACTION_WAIT_MS", "550")))
    time.sleep(delay / 1000.0)
    try:
        after = _capture_window_info(window)
        after_title = _window_title_by_hwnd(int(window["hwnd"]))
        evidence = _visual_change_evidence(before, after, before_title, after_title)
        evidence["waited_ms"] = delay
        evidence["after_title"] = after_title
        return evidence
    except Exception as exc:
        return {"post_screenshot_captured": False, "waited_ms": delay, "state_changed": False, "execution_likely_succeeded": False, "reason": f"操作后截图失败，无法验证状态变化：{exc}", "next_action": "重新列出窗口并截图验证；验证成功前不得假设操作成功"}


def _window_ground_points(instruction: str, img: Image.Image, topk: int):
    """窗口截图 grounding; 整窗找不到时自动放大顶部工具栏区域重试一次。

    返回 (points, used_height): points 为该识别区域内的归一化坐标,
    used_height 为实际识别图的高度(整窗=img.height, 顶部裁剪=裁剪高度)。
    常见桌面应用(音乐/IM/办公)的搜索框、菜单都在窗口顶部, 放大后 2B 模型
    定位成功率明显更高。
    """
    points = ground_image(instruction, img, topk)
    if points:
        return points, img.height
    top_h = max(120, int(img.height * 0.45))
    if top_h >= img.height:
        return points, img.height
    top_img = img.crop((0, 0, img.width, top_h))
    top_points = ground_image(instruction, top_img, topk)
    if top_points:
        return top_points, top_img.height
    return points, img.height


def window_click(title_contains: str, instruction: str, topk: int = 3, idx: int = 0):
    """优先用 UIA 控件树按文本精确命中点击; 无结构或文本未命中才回退视觉 grounding。

    对原生窗口与 Chromium/Edge 网页，UI 按钮/链接暴露为可访问控件(文本+矩形)，
    文本坐标点击零漂移，避免全屏视觉猜像素导致的点错/误触退出全屏。
    游戏/自绘全屏无控件树(structure_readable=False)，自动回退视觉并给出护栏提示。
    """
    window = _find_window(title_contains); activate_window(title_contains)
    # 第一优先: UIA 文本定位
    if _UiaDesktop is not None:
        ok, res = window_text_click(title_contains, instruction, candidate_index=idx)
        if ok:
            res["backend"] = BACKEND; res["window"] = window
            res["via"] = "uia_text"  # 标记走文本定位，供编排/日志识别
            return res
        # 未命中文本: 若该窗口其实没有结构可读(游戏等)，给出护栏提示，避免视觉乱点
        if not res.get("structure_readable"):
            return {
                "clicked": False, "window": window, "reason": res.get("reason", ""),
                "via": "uia_unavailable", "structure_readable": False,
                "guidance": "该目标窗口没有可读的 UIA 控件树(通常是游戏或全屏自绘画面)。"
                            "若目标是全屏游戏，切勿先按 Esc/F11 或点退出全屏; 只有确认目标确实在该全屏画面内才允许视觉点击，"
                            "否则应切换回窗口化/桌面再操作。",
            }
        # 结构可读但没匹配到目标文本 → 记录原因后回退视觉，供模型换描述
        visual_hint = res.get("reason", "")
    else:
        visual_hint = f"pywinauto 不可用，纯视觉定位: {_uia_import_error}"
    # 回退: 视觉 grounding
    img = window_screenshot_pil(title_contains)
    points, used_height = _window_ground_points(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "window": window, "raw_output": _last_raw_output, "text_match_hint": visual_hint}
    x, y = points[min(max(0, idx), len(points) - 1)]
    left, top, _, _ = window["bounds"]
    px = left + int(x * img.width); py = top + int(y * used_height)
    _ensure_raise_on_operate(window)  # 点击前保证窗口在最前, 避免被遮挡点错
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    evidence = _wait_and_compare_window(window, img, str(window.get("title", "")))
    return {"clicked": True, "backend": BACKEND, "pixel": [px, py], "window": window,
            "all_points": points, "grounding_region": "full" if used_height == img.height else "top",
            "via": "visual_fallback", "text_match_hint": visual_hint,
            **evidence}


def window_double_click(title_contains: str, instruction: str, topk: int = 3, idx: int = 0):
    """Ground a window element once, then double-click that exact point."""
    window = _find_window(title_contains); activate_window(title_contains)
    img = window_screenshot_pil(title_contains)
    points, used_height = _window_ground_points(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "window": window, "raw_output": _last_raw_output}
    x, y = points[min(max(0, idx), len(points) - 1)]
    left, top, _, _ = window["bounds"]
    px = left + int(x * img.width); py = top + int(y * used_height)
    _ensure_raise_on_operate(window)  # 双击前保证窗口在最前, 避免双击落到上层窗口
    ctypes.windll.user32.SetCursorPos(px, py)
    for _ in range(2):
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.12)
    evidence = _wait_and_compare_window(window, img, str(window.get("title", "")), wait_ms=max(800, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))))
    after_title = str(evidence.get("after_title", ""))
    return {
        "double_clicked": True,
        "instruction": instruction,
        "backend": BACKEND,
        "pixel": [px, py],
        "window": window,
        "before_title": window.get("title", ""),
        "after_title": after_title,
        "title_changed": bool(after_title and after_title != window.get("title", "")),
        "all_points": points,
        "grounding_region": "full" if used_height == img.height else "top",
        **evidence,
    }


def _set_clipboard_text(text: str):
    """Set Unicode clipboard text directly, avoiding shell/session quoting issues."""
    data = str(text).encode("utf-16-le") + b"\x00\x00"
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    handle = kernel32.GlobalAlloc(0x0002, len(data))
    if not handle: raise RuntimeError("GlobalAlloc failed for clipboard")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle); raise RuntimeError("GlobalLock failed for clipboard")
    ctypes.memmove(pointer, data, len(data)); kernel32.GlobalUnlock(handle)
    for _ in range(10):
        if user32.OpenClipboard(None): break
        time.sleep(0.05)
    else:
        kernel32.GlobalFree(handle); raise RuntimeError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(13, handle): raise RuntimeError("SetClipboardData failed")
        handle = None
    finally:
        user32.CloseClipboard()
        if handle: kernel32.GlobalFree(handle)


def desktop_read_clipboard():
    """Read Unicode text from the Windows clipboard after an explicit copy action."""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode: raise RuntimeError(completed.stderr.strip() or "Get-Clipboard failed")
    return {"text": completed.stdout.strip()}


def window_type_text(title_contains: str, instruction: str, text: str):
    result = window_click(title_contains, instruction)
    if not result.get("clicked"): return {"typed": False, **result}
    window = result["window"]
    before = _capture_window_info(window)
    before_title = _window_title_by_hwnd(int(window["hwnd"]))
    _desktop_hotkey_raw(["ctrl", "a"])
    _desktop_hotkey_raw(["backspace"])
    _set_clipboard_text(text)
    _desktop_hotkey_raw(["ctrl", "v"])
    evidence = _wait_and_compare_window(window, before, before_title)
    return {"typed": True, "text_length": len(str(text)), **result, **evidence}


def desktop_click(instruction: str, topk: int = 3, idx: int = 0):
    img = desktop_screenshot_pil(); points = ground_image(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "all_points": [], "raw_output": _last_raw_output}
    x, y = points[min(idx, len(points) - 1)]; left, top, width, height = _primary_screen()
    px = left + int(max(0.0, min(1.0, x)) * width); py = top + int(max(0.0, min(1.0, y)) * height)
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0); ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    delay = max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))); time.sleep(delay / 1000.0)
    after = desktop_screenshot_pil(); evidence = _visual_change_evidence(img, after); evidence["waited_ms"] = delay
    return {"clicked": True, "instruction": instruction, "backend": BACKEND, "pixel": [px, py], "primary_screen": [width, height], "all_points": points, **evidence}


def _key_event(vk: int, up: bool = False):
    ctypes.windll.user32.keybd_event(vk, 0, 0x0002 if up else 0, 0)


def _desktop_hotkey_raw(keys: list[str]):
    mapping = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E}
    codes = []
    for key in keys:
        value = str(key).lower(); code = mapping.get(value, ord(value.upper()) if len(value) == 1 else None)
        if code is None: raise ValueError(f"不支持的按键：{key}")
        codes.append(code)
    for code in codes: _key_event(code)
    for code in reversed(codes): _key_event(code, True)
    return {"pressed": keys}


def desktop_hotkey(keys: list[str]):
    window = _foreground_window()
    try: before = _capture_window_info(window)
    except Exception: before = desktop_screenshot_pil()
    result = _desktop_hotkey_raw(keys)
    if window.get("hwnd") and window.get("bounds"):
        evidence = _wait_and_compare_window(window, before, str(window.get("title", "")))
    else:
        delay = max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))); time.sleep(delay / 1000.0)
        evidence = _visual_change_evidence(before, desktop_screenshot_pil()); evidence["waited_ms"] = delay
    return {**result, **evidence}


def desktop_media_stop():
    """Send the idempotent Windows media-stop command without closing an app."""
    if os.name != "nt":
        raise RuntimeError("media stop currently only supports Windows")
    user32 = ctypes.windll.user32
    send_message_timeout = user32.SendMessageTimeoutW
    send_message_timeout.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    send_message_timeout.restype = wintypes.LPARAM
    hwnd = int(user32.GetForegroundWindow())
    if not hwnd:
        raise RuntimeError("no foreground window is available for media stop")
    result = ctypes.c_size_t()
    delivered = bool(send_message_timeout(
        hwnd, 0x0319, hwnd, 13 << 16, 0x0002, 1500, ctypes.byref(result),
    ))
    if not delivered:
        raise RuntimeError("Windows media-stop command was not delivered")
    return {
        "ok": True, "requested_state": "stopped", "command": "media_stop",
        "idempotent": True, "target_hwnd": hwnd,
        "action_sent_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "execution_likely_succeeded": True,
        "next_action": "媒体停止命令已送达；不要再发送 Space、播放命令或终止应用进程",
    }


def desktop_type_text(instruction: str, text: str):
    clicked = desktop_click(instruction)
    if not clicked.get("clicked"): return {"typed": False, **clicked}
    before = desktop_screenshot_pil()
    _set_clipboard_text(text)
    _desktop_hotkey_raw(["ctrl", "v"])
    delay = max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))); time.sleep(delay / 1000.0)
    evidence = _visual_change_evidence(before, desktop_screenshot_pil()); evidence["waited_ms"] = delay
    return {"typed": True, "pixel": clicked["pixel"], "text_length": len(str(text)), **evidence}


def desktop_type_active_text(text: str, clear: bool = True):
    """Paste text into the currently focused native control without visual relocation."""
    window = _foreground_window()
    try: before = _capture_window_info(window)
    except Exception: before = desktop_screenshot_pil()
    if clear:
        _desktop_hotkey_raw(["ctrl", "a"]); _desktop_hotkey_raw(["backspace"])
    _set_clipboard_text(text)
    _desktop_hotkey_raw(["ctrl", "v"])
    if window.get("hwnd") and window.get("bounds"):
        evidence = _wait_and_compare_window(window, before, str(window.get("title", "")))
    else:
        delay = max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))); time.sleep(delay / 1000.0)
        evidence = _visual_change_evidence(before, desktop_screenshot_pil()); evidence["waited_ms"] = delay
    return {"typed": True, "focused": True, "cleared": bool(clear), "text_length": len(str(text)), **evidence}


def desktop_scroll(direction: str = "down", amount: int = 600):
    window = _foreground_window()
    try: before = _capture_window_info(window)
    except Exception: before = desktop_screenshot_pil()
    delta = -120 * max(1, abs(int(amount)) // 120) if direction.lower() == "down" else 120 * max(1, abs(int(amount)) // 120)
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)
    if window.get("hwnd") and window.get("bounds"):
        evidence = _wait_and_compare_window(window, before, str(window.get("title", "")))
    else:
        delay = max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))); time.sleep(delay / 1000.0)
        evidence = _visual_change_evidence(before, desktop_screenshot_pil()); evidence["waited_ms"] = delay
    return {"scrolled": direction, "amount": abs(int(amount)), **evidence}


def close():
    global _pw, _browser, _page
    try:
        if _page is not None:
            _page.close()
        if _browser is not None:
            _browser.close()
        if _pw is not None:
            _pw.stop()
    finally:
        _pw = _browser = _page = None


if __name__ == "__main__":
    # 简单自测: 打开一个页面并 grounding 一次
    navigate("https://www.bing.com")
    r = click("click the search box")
    print(r)
    close()
