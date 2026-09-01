#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
GUI-Owl-1.5-2B-Instruct 端到端冒烟测试
=====================================
统一运行于项目共享 .venv（已升级 transformers>=4.57 以支持 GUI-Owl）。

用法:
  .venv\Scripts\python.exe Vision\test_gui_owl.py

测试内容:
  1. 加载 GUI-Owl 模型 (计时 + 显存)
  2. 合成图 grounding: 多个带标签控件, 验证定位精度
  3. 真实桌面 grounding: 截取当前桌面, 询问常见控件 (只读, 不点击)

仅做 grounding (坐标预测), 不执行任何鼠标/键盘动作, 安全只读。
"""
import os
import sys
import time

# 必须在 import agent 之前选定后端
os.environ["VISION_BACKEND"] = "gui_owl"
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # 让 agent 能 import modules.live...
sys.path.insert(0, HERE)

import torch
from PIL import Image, ImageDraw, ImageFont

print("=" * 70)
print("GUI-Owl-1.5-2B-Instruct 冒烟测试")
print("=" * 70)
print(f"torch {torch.__version__} | cuda {torch.cuda.is_available()} | "
      f"device {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'cpu'}")
print(f"VISION_BACKEND = {os.environ['VISION_BACKEND']}")

import agent  # noqa: E402


def make_font(size=26):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def report(instruction: str, img: Image.Image, truth=None):
    """运行一次 grounding 并打印结果。"""
    print(f"\n   指令: {instruction}")
    t0 = time.time()
    pts = agent.ground_image(instruction, img, topk=3)
    dt = time.time() - t0
    w, h = img.size
    print(f"   耗时 {dt*1000:.0f}ms | 原始输出: {agent._last_raw_output[:260]!r}")
    if not pts:
        print("   坐标: (未找到 / terminate)")
        return
    for i, (nx, ny) in enumerate(pts):
        px, py = int(nx * w), int(ny * h)
        tag = ""
        if truth:
            err = ((nx - truth[0]) ** 2 + (ny - truth[1]) ** 2) ** 0.5
            tag = f" | 与真值偏差={err*100:.1f}%"
        print(f"   #{i} 归一化=({nx:.3f},{ny:.3f}) 像素=({px},{py}){tag}")


# ---- 1. 加载模型 ----
t0 = time.time()
print("\n[1/3] 加载 GUI-Owl 模型 ...")
print("     加载前 backend_info:", agent.backend_info())
agent.load_model()
dt = time.time() - t0
print(f"     加载完成, 耗时 {dt:.1f}s, 设备 {agent._model.device}")
if torch.cuda.is_available():
    print(f"     显存 已分配={torch.cuda.memory_allocated()/1024**3:.2f}GB "
          f"峰值={torch.cuda.max_memory_allocated()/1024**3:.2f}GB")
print("     backend_info:", agent.backend_info())


# ---- 2. 合成图 grounding (确定性, 多控件) ----
print("\n[2/3] 合成图 grounding: 多个带标签控件, 验证定位精度")
W, H = 1000, 700
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)
font = make_font(26)
controls = {
    "Submit": ((760, 600), (900, 660), "#2e7d32", "Submit"),
    "Search": ((60, 60), (320, 120), "#eeeeee", "Search"),
    "Close":  ((900, 40), (950, 90), "#c62828", "X"),
}
for label, (a, b, color, text) in controls.items():
    d.rectangle([a, b], fill=color, outline="#333333", width=2)
    d.text((a[0] + 12, a[1] + 14), text, fill="white" if color != "#eeeeee" else "black", font=font)

report("click the submit button", img, truth=(830 / W, 630 / H))
report("click the search box", img, truth=(190 / W, 90 / H))
report("click the close button", img, truth=(925 / W, 65 / H))
report("click the nonexistent foobar control", img)  # 期望 terminate / 空


# ---- 3. 真实桌面 grounding (只读) ----
print("\n[3/3] 真实桌面 grounding (只读, 不点击)")
try:
    desk = agent.desktop_screenshot_pil()
    print(f"   桌面截图尺寸: {desk.size}")
    report("click the start button", desk)
    report("click the search box", desk)
except Exception as e:
    print(f"   桌面截图失败 (可能锁屏/无会话): {e}")

print("\n" + "=" * 70)
print("测试结束")
print("=" * 70)
