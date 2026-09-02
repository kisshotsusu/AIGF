#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Vision 外部调试 HTTP 服务 (独立于 MCP 8765 端口)。

用途: 用 curl / Python / 浏览器直接调用, 排查视觉 grounding、窗口结构读取、
截图与点击是否正确, 而不必经过 MCP 工具或 HomeAgent。

设计:
  - 复用 Vision/agent.py 同一套能力 (GUI-Owl 视觉 + Playwright + Win32/UIA 窗口)。
  - 通过进程内"单 worker 队列"把每个请求串行化执行, 避免并发触发模型/屏幕竞争。
  - 不影响 8765 的 MCP 服务, 可独立启停。默认端口 8790 (env VISION_DEBUG_PORT)。
  - 若 VISION_PRELOAD_MODEL=1 则启动即加载模型, 否则首次调用时懒加载。

端点 (均返回 JSON, 除 /screenshot* 返回 image/png):
  GET  /health                    存活/后端/模型/GPU 状态
  GET  /backend                   后端与模型来源 (含显存)
  GET  /active                    前台窗口分类 (browser_dom/browser_visual/desktop_visual)
  GET  /windows?contains=标题      列出可见窗口
  GET  /screenshot_desktop        主显示器 PNG
  GET  /screenshot_window?title=标题  指定窗口 PNG
  POST /read_window   {title_contains, keywords, max_items}   读窗口可点控件(UIA文本优先)
  POST /ground        {instruction, img:"desktop"|"window:<title>"|base64png, topk}
                     视觉定位, 返回归一化坐标+像素+base64标注? (当前返回归一化坐标与像素)
  POST /click_text    {title_contains, target, candidate_index}  UIA文本精确点击
  POST /click_window  {title_contains, instruction, topk}        窗口视觉点击(文本优先)
  POST /activate      {title_contains}                           激活窗口到前台
  GET  /tools         列出全部端点

示例:
  curl http://127.0.0.1:8790/health
  curl http://127.0.0.1:8790/windows?contains=哔哩
  curl -X POST http://127.0.0.1:8790/read_window -H "Content-Type: application/json" \
       -d '{"title_contains":"哔哩哔哩","keywords":"动态"}'
  curl -X POST http://127.0.0.1:8790/click_text -H "Content-Type: application/json" \
       -d '{"title_contains":"哔哩哔哩","target":"动态"}'

运行:  <root>/.venv/Scripts/python.exe Vision/debug_server.py
"""
import io
import os
import sys
import json
import base64
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 环境预置 (须在 import agent 前) ----
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # 使 `import agent` 生效
sys.path.insert(0, HERE)

PORT = int(os.environ.get("VISION_DEBUG_PORT", "8790"))
PRELOAD = os.environ.get("VISION_PRELOAD_MODEL", "0") == "1"

import agent  # noqa: E402  (torch + playwright 较重, import 需数秒)


# 单 worker: 所有真正调用 agent 的 handler 都投递到这里串行执行, 规避并发模型竞争。
_work_queue = queue.Queue()
_worker_started = False


def _worker_loop():
    while True:
        fn, result_q = _work_queue.get()
        try:
            result_q.put(("ok", fn()))
        except Exception as exc:  # noqa: BLE001 - 调试服务不应因单请求异常退出
            result_q.put(("error", {"error": type(exc).__name__, "message": str(exc)}))
        finally:
            _work_queue.task_done()


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        threading.Thread(target=_worker_loop, daemon=True, name="vision-debug-worker").start()
        _worker_started = True


def _call(fn):
    """投递一个可调用对象到 worker 串行执行并等待结果。"""
    result_q = queue.Queue()
    _work_queue.put((fn, result_q))
    status, payload = result_q.get()
    return payload


# ---- 具体的 agent 操作(线程安全: 经 worker 串行调用) ----
def op_health():
    info = agent.backend_info()
    return {
        "ok": True,
        "pid": os.getpid(),
        "backend_info": info,
        "cuda": agent.cuda_memory_status(),
        "preload": PRELOAD,
        "loaded": info.get("loaded"),
    }


def op_active():
    return agent.inspect_active_target()


def op_windows(contains: str = ""):
    raw = agent.list_windows(contains)
    # list_windows 返回 str; 若可解析则结构化为列表, 否则原样返回
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {"text": raw}
    return raw


def op_screenshot_desktop():
    img = agent.desktop_screenshot_pil()
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def op_screenshot_window(title: str):
    img = agent.window_screenshot_pil(title)
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def op_read_window(title_contains, keywords="", max_items=80):
    return agent.window_read_targets(title_contains, keywords=keywords, max_items=int(max_items or 80))


def _resolve_source_image(img_spec):
    """把请求里的图片来源解析成 PIL.Image: desktop / window:<title> / data:base64。"""
    spec = str(img_spec or "").strip()
    low = spec.lower()
    if low == "desktop":
        return agent.desktop_screenshot_pil()
    if low.startswith("window:"):
        return agent.window_screenshot_pil(spec.split(":", 1)[1])
    if low.startswith("data:"):
        # data:image/png;base64,<...>
        if "base64," in spec:
            b64 = spec.split("base64,", 1)[1]
            from PIL import Image
            return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    raise ValueError(f"无法解析图片来源 img={img_spec!r} (支持 desktop | window:<title> | data:image/png;base64,...)")


def op_ground(instruction, img_spec="desktop", topk=3, want_b64=False):
    image = _resolve_source_image(img_spec)
    points = agent.ground_image(str(instruction), image, int(topk or 3))
    w, h = image.size
    pixels = [
        {"normalized": [round(p[0], 4), round(p[1], 4)],
         "pixel": [round(p[0] * w), round(p[1] * h)]}
        for p in points
    ]
    result = {
        "instruction": str(instruction),
        "img_source": str(img_spec),
        "img_size": [w, h],
        "points_normalized": points,
        "points_pixel": pixels,
        "count": len(points),
    }
    if want_b64 and points:
        # 标注在 image 上回传, 便于肉眼核对落点 (仅第一候选)
        try:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(image)
            x, y = points[0][0] * w, points[0][1] * h
            r = 12
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 0, 0), width=4)
            draw.line([x - 2 * r, y, x + 2 * r, y], fill=(255, 0, 0), width=3)
            draw.line([x, y - 2 * r, x, y + 2 * r], fill=(255, 0, 0), width=3)
            buf = io.BytesIO(); image.save(buf, "PNG")
            result["annotated_b64"] = base64.b64encode(buf.getvalue()).decode()
        except Exception as exc:  # noqa: BLE001
            result["annotated_error"] = str(exc)
    return result


def op_click_text(title_contains, target, candidate_index=0):
    return agent.window_text_click(title_contains, target, int(candidate_index or 0))


def op_click_window(title_contains, instruction, topk=3, idx=0):
    return agent.window_click(title_contains, str(instruction), topk=int(topk or 3), idx=int(idx or 0))


def op_activate(title_contains):
    return agent.activate_window(title_contains)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 精简访问日志
        sys.stderr.write("[debug] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- helpers ----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"_raw": raw.decode("utf-8", "replace")}

    def _do_action(self, fn):
        payload = _call(fn)
        if isinstance(payload, dict) and payload.get("error") and not payload.get("ok"):
            self._send_json({"ok": False, **payload}, status=500)
        else:
            if isinstance(payload, dict):
                payload.setdefault("ok", True)
            self._send_json(payload)

    # ---- 只读 ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = {}
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[k] = v
        from urllib.parse import unquote
        if path == "/health":
            self._do_action(op_health)
        elif path == "/backend":
            self._send_json({"ok": True, **op_health()})
        elif path == "/active":
            self._do_action(op_active)
        elif path == "/windows":
            self._do_action(lambda: op_windows(params.get("contains", "")))
        elif path == "/screenshot_desktop":
            try:
                self._send_png(_call(op_screenshot_desktop))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
        elif path == "/screenshot_window":
            try:
                self._send_png(_call(lambda: op_screenshot_window(unquote(params.get("title", "")))))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
        elif path == "/tools":
            self._send_json({"ok": True, "endpoints": [
                "GET /health", "GET /backend", "GET /active", "GET /windows?contains=",
                "GET /screenshot_desktop", "GET /screenshot_window?title=",
                "POST /read_window", "POST /ground", "POST /click_text",
                "POST /click_window", "POST /activate", "GET /tools",
            ]})
        else:
            self._send_json({"ok": False, "error": f"未知端点 {path}", "hint": "GET /tools"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        try:
            if path == "/read_window":
                self._do_action(lambda: op_read_window(
                    str(body.get("title_contains", "")),
                    str(body.get("keywords", "")),
                    int(body.get("max_items", 80)),
                ))
            elif path == "/ground":
                self._do_action(lambda: op_ground(
                    body.get("instruction", ""),
                    body.get("img", "desktop"),
                    int(body.get("topk", 3)),
                    bool(body.get("annotated", False)),
                ))
            elif path == "/click_text":
                self._do_action(lambda: op_click_text(
                    str(body.get("title_contains", "")),
                    str(body.get("target", "")),
                    int(body.get("candidate_index", 0)),
                ))
            elif path == "/click_window":
                self._do_action(lambda: op_click_window(
                    str(body.get("title_contains", "")),
                    str(body.get("instruction", "")),
                    int(body.get("topk", 3)),
                    int(body.get("idx", 0)),
                ))
            elif path == "/activate":
                self._do_action(lambda: op_activate(str(body.get("title_contains", ""))))
            else:
                self._send_json({"ok": False, "error": f"未知端点 {path}", "hint": "GET /tools"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def main():
    _ensure_worker()
    if PRELOAD:
        print("[debug] 预加载 GUI-Owl 模型 ...", flush=True)
        _call(agent.load_model)
        print("[debug] 模型已加载", flush=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[debug] Vision 调试服务已启动: http://127.0.0.1:{PORT}  (GET /tools 看端点)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
