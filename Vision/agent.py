#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GUI-Actor-2B + Playwright 网页控制核心。

流程: 截图当前视口 -> GUI-Actor 视觉 grounding 得到归一化坐标(0~1)
      -> 映射到视口像素 -> Playwright 点击/输入/滚动。

GUI-Actor 推理接口(来自 microsoft/GUI-Actor):
    from gui_actor.modeling import Qwen2VLForConditionalGenerationWithPointer
    from gui_actor.inference import inference
    pred = inference(conv, model, tokenizer, processor, use_placeholder=True, topk=N)
    px, py = pred["topk_points"][0]   # 归一化 0~1
"""
import os
import sys
import io
import ctypes
import ctypes.wintypes
import subprocess
import time

import torch
from PIL import Image, ImageGrab

# ---- 路径(可通过环境变量覆盖) ----
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.environ.get("GUI_ACTOR_REPO", os.path.join(HERE, "GUI-Actor"))
MODEL_DIR = os.environ.get(
    "GUI_ACTOR_MODEL", os.path.join(HERE, "models", "GUI-Actor-2B-Qwen2-VL")
)
sys.path.insert(0, os.path.join(REPO_DIR, "src"))

from transformers import AutoProcessor  # noqa: E402
from gui_actor.modeling import Qwen2VLForConditionalGenerationWithPointer  # noqa: E402
from gui_actor.inference import inference  # noqa: E402
from gui_actor.constants import grounding_system_message  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

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
_pw = None
_browser = None
_page = None


def load_model():
    global _model, _processor, _tokenizer
    if _model is not None:
        return
    print("[agent] loading GUI-Actor-2B ...", file=sys.stderr, flush=True)
    _processor = AutoProcessor.from_pretrained(MODEL_DIR)
    _tokenizer = _processor.tokenizer
    _model = Qwen2VLForConditionalGenerationWithPointer.from_pretrained(
        MODEL_DIR,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        attn_implementation="sdpa",
    ).eval()
    print(f"[agent] model loaded on {_model.device}", file=sys.stderr, flush=True)


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def ensure_browser():
    global _pw, _browser, _page
    if _page is not None:
        return _page
    _pw = sync_playwright().start()
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
    # 抹掉 webdriver 标记, 降低被风控识别的概率
    _page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return _page


# ---------------- 公开 API ----------------
def navigate(url: str) -> str:
    page = ensure_browser()
    page.goto(url, wait_until="load", timeout=60000)
    page.wait_for_timeout(1500)
    return page.url


def screenshot_pil() -> Image.Image:
    page = ensure_browser()
    png = page.screenshot()
    return Image.open(io.BytesIO(png)).convert("RGB")


def ground_image(instruction: str, img: Image.Image, topk: int = 3):
    """在给定图像上返回 topk 个归一化坐标。"""
    load_model()
    conversation = [
        {
            "role": "system",
            "content": [{"type": "text", "text": grounding_system_message}],
        },
        {
            "role": "user",
            "content": [
                {"type": "image", "image": img},
                {"type": "text", "text": instruction},
            ],
        },
    ]
    pred = inference(
        conversation, _model, _tokenizer, _processor,
        use_placeholder=True, topk=topk,
    )
    return pred.get("topk_points") or []


def ground(instruction: str, topk: int = 3):
    """在独立 Chromium 当前页面上定位。"""
    return ground_image(instruction, screenshot_pil(), topk)


def click(instruction: str, topk: int = 3, idx: int = 0):
    page = ensure_browser()
    pts = ground(instruction, topk=topk)
    if not pts:
        return {"clicked": False, "reason": "model returned no point", "all_points": []}
    x, y = pts[min(idx, len(pts) - 1)]
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    px = int(x * VIEWPORT["width"])
    py = int(y * VIEWPORT["height"])
    page.mouse.click(px, py)
    page.wait_for_timeout(600)
    return {
        "clicked": True,
        "instruction": instruction,
        "norm": [round(x, 4), round(y, 4)],
        "pixel": [px, py],
        "all_points": [[round(p[0], 4), round(p[1], 4)] for p in pts],
    }


def type_text(instruction: str, text: str, topk: int = 3):
    page = ensure_browser()
    pts = ground(instruction, topk=topk)
    if not pts:
        return {"typed": False, "reason": "model returned no point"}
    x, y = pts[0]
    px = int(max(0.0, min(1.0, x)) * VIEWPORT["width"])
    py = int(max(0.0, min(1.0, y)) * VIEWPORT["height"])
    page.mouse.click(px, py)
    page.wait_for_timeout(300)
    page.keyboard.type(text, delay=20)
    return {"typed": True, "pixel": [px, py], "text": text}


def scroll(direction: str = "down", amount: int = 400):
    page = ensure_browser()
    dy = amount if direction.lower() == "down" else -amount
    page.mouse.wheel(0, dy)
    page.wait_for_timeout(500)
    return {"scrolled": direction, "amount": amount}


def get_url() -> str:
    return ensure_browser().url


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


def desktop_screenshot_pil() -> Image.Image:
    """只截取 Windows 主显示器，降低视觉推理开销。"""
    if os.name != "nt": raise RuntimeError("桌面视觉控制目前只支持 Windows")
    return ImageGrab.grab(all_screens=False).convert("RGB")


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
        results.append({"hwnd": int(hwnd), "pid": int(pid.value), "title": title,
                        "bounds": [rect.left, rect.top, rect.right, rect.bottom]})
        return True

    user32.EnumWindows(callback, 0)
    return results


def _find_window(title_contains: str):
    matches = list_windows(title_contains)
    if not matches: raise RuntimeError(f"window not found: {title_contains}")
    return matches[0]


def activate_window(title_contains: str):
    window = _find_window(title_contains); hwnd = window["hwnd"]
    ctypes.windll.user32.ShowWindow(hwnd, 9)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return {"activated": True, **window}


def window_screenshot_pil(title_contains: str) -> Image.Image:
    window = _find_window(title_contains)
    left, top, right, bottom = window["bounds"]
    _, _, width, height = _primary_screen()
    bbox = (max(0, left), max(0, top), min(width, right), min(height, bottom))
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise RuntimeError("target window is outside the primary screen")
    return ImageGrab.grab(bbox=bbox, all_screens=False).convert("RGB")


def window_click(title_contains: str, instruction: str, topk: int = 3, idx: int = 0):
    window = _find_window(title_contains); activate_window(title_contains)
    img = window_screenshot_pil(title_contains); points = ground_image(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "window": window}
    x, y = points[min(max(0, idx), len(points) - 1)]
    left, top, _, _ = window["bounds"]
    px = max(0, left) + int(x * img.width); py = max(0, top) + int(y * img.height)
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.3)
    return {"clicked": True, "pixel": [px, py], "window": window, "all_points": points}


def window_type_text(title_contains: str, instruction: str, text: str):
    result = window_click(title_contains, instruction)
    if not result.get("clicked"): return {"typed": False, **result}
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Set-Clipboard -Value $args[0]", str(text)], check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    desktop_hotkey(["ctrl", "v"])
    return {"typed": True, "text_length": len(str(text)), **result}


def desktop_click(instruction: str, topk: int = 3, idx: int = 0):
    img = desktop_screenshot_pil(); points = ground_image(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "all_points": []}
    x, y = points[min(idx, len(points) - 1)]; left, top, width, height = _primary_screen()
    px = left + int(max(0.0, min(1.0, x)) * width); py = top + int(max(0.0, min(1.0, y)) * height)
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0); ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.4)
    return {"clicked": True, "instruction": instruction, "pixel": [px, py], "primary_screen": [width, height], "all_points": points}


def _key_event(vk: int, up: bool = False):
    ctypes.windll.user32.keybd_event(vk, 0, 0x0002 if up else 0, 0)


def desktop_hotkey(keys: list[str]):
    mapping = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E}
    codes = []
    for key in keys:
        value = str(key).lower(); code = mapping.get(value, ord(value.upper()) if len(value) == 1 else None)
        if code is None: raise ValueError(f"不支持的按键：{key}")
        codes.append(code)
    for code in codes: _key_event(code)
    for code in reversed(codes): _key_event(code, True)
    return {"pressed": keys}


def desktop_type_text(instruction: str, text: str):
    clicked = desktop_click(instruction)
    if not clicked.get("clicked"): return {"typed": False, **clicked}
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value $args[0]", str(text)], check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    desktop_hotkey(["ctrl", "v"])
    return {"typed": True, "pixel": clicked["pixel"], "text_length": len(str(text))}


def desktop_scroll(direction: str = "down", amount: int = 600):
    delta = -120 * max(1, abs(int(amount)) // 120) if direction.lower() == "down" else 120 * max(1, abs(int(amount)) // 120)
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)
    return {"scrolled": direction, "amount": abs(int(amount))}


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
