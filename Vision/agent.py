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
import json
import urllib.request
import threading

import torch
from PIL import Image, ImageChops, ImageGrab, ImageStat

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
_pw = None
_browser = None
_page = None
_owns_browser = False
_browser_source = "none"
_SCREENSHOT_LOCK = threading.Lock()

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
    # Grounding is inference-only.  Disabling autograd prevents an accidental
    # graph from surviving in the returned prediction and growing VRAM usage.
    with torch.inference_mode():
        pred = inference(
            conversation, _model, _tokenizer, _processor,
            use_placeholder=True, topk=topk,
        )
    points = pred.get("topk_points") or []
    return [[float(point[0]), float(point[1])] for point in points]


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
        return {"clicked": False, "reason": "model returned no point", "all_points": []}
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
        return {"typed": False, "reason": "model returned no point"}
    x, y = pts[0]
    px = int(max(0.0, min(1.0, x)) * VIEWPORT["width"])
    py = int(max(0.0, min(1.0, y)) * VIEWPORT["height"])
    page.mouse.click(px, py)
    page.wait_for_timeout(300)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(text, delay=20)
    return {"typed": True, "pixel": [px, py], "text": text}


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


def _grab_windows_image(*, hwnd: int | None = None, bbox=None, all_screens: bool = False, attempts: int = 3) -> Image.Image:
    """Serialize and retry Pillow GDI captures; prefer HWND capture for windows."""
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
    detail = errors[-1] if errors else "unknown capture error"
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


def _find_window(title_contains: str):
    """Resolve a window from its title, HWND, process name, or process path.

    `list_windows` exposes all four fields.  A model may legitimately return the
    process field it just observed, so activation must not interpret every value
    as title text only.
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
    available = [str(item.get("title") or "") for item in windows[:8]]
    raise RuntimeError(f"window not found: {reference}; available titles: {available}")


def activate_window(title_contains: str):
    window = _find_window(title_contains); hwnd = window["hwnd"]
    ctypes.windll.user32.ShowWindow(hwnd, 9)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return {"activated": True, **window}


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


def window_click(title_contains: str, instruction: str, topk: int = 3, idx: int = 0):
    window = _find_window(title_contains); activate_window(title_contains)
    img = window_screenshot_pil(title_contains); points = ground_image(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "window": window}
    x, y = points[min(max(0, idx), len(points) - 1)]
    left, top, _, _ = window["bounds"]
    px = left + int(x * img.width); py = top + int(y * img.height)
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    evidence = _wait_and_compare_window(window, img, str(window.get("title", "")))
    return {"clicked": True, "pixel": [px, py], "window": window, "all_points": points, **evidence}


def window_double_click(title_contains: str, instruction: str, topk: int = 3, idx: int = 0):
    """Ground a window element once, then double-click that exact point."""
    window = _find_window(title_contains); activate_window(title_contains)
    img = window_screenshot_pil(title_contains); points = ground_image(instruction, img, topk)
    if not points: return {"clicked": False, "reason": "model returned no point", "window": window}
    x, y = points[min(max(0, idx), len(points) - 1)]
    left, top, _, _ = window["bounds"]
    px = left + int(x * img.width); py = top + int(y * img.height)
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
        "pixel": [px, py],
        "window": window,
        "before_title": window.get("title", ""),
        "after_title": after_title,
        "title_changed": bool(after_title and after_title != window.get("title", "")),
        "all_points": points,
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
    if not points: return {"clicked": False, "reason": "model returned no point", "all_points": []}
    x, y = points[min(idx, len(points) - 1)]; left, top, width, height = _primary_screen()
    px = left + int(max(0.0, min(1.0, x)) * width); py = top + int(max(0.0, min(1.0, y)) * height)
    ctypes.windll.user32.SetCursorPos(px, py)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0); ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    delay = max(100, int(os.environ.get("GUI_POST_ACTION_WAIT_MS", "550"))); time.sleep(delay / 1000.0)
    after = desktop_screenshot_pil(); evidence = _visual_change_evidence(img, after); evidence["waited_ms"] = delay
    return {"clicked": True, "instruction": instruction, "pixel": [px, py], "primary_screen": [width, height], "all_points": points, **evidence}


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
