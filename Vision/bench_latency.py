#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""1080p 端到端延迟基准: 桌面截图 -> GUI-Owl 识别返回"""
import os, sys, time, statistics

os.environ["VISION_BACKEND"] = "gui_owl"
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
from PIL import Image
print("torch", torch.__version__, "| cuda:", torch.cuda.is_available(),
      "|", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu")

import agent  # noqa: E402

# ---- 1. 加载模型 ----
t0 = time.time()
agent.load_model()
load_s = time.time() - t0
print(f"[load] 模型加载 {load_s:.1f}s, 设备 {agent._model.device}")
if torch.cuda.is_available():
    print(f"[load] 显存 {torch.cuda.memory_allocated()/1024**3:.2f}GB")

# ---- 2. 取真实桌面截图, 统一缩放到 1080p ----
raw = agent.desktop_screenshot_pil()
print(f"[screen] 当前桌面原始分辨率: {raw.size}")
img1080 = raw.convert("RGB").resize((1920, 1080), Image.LANCZOS)

# ---- 3. 预热 2 次 (CUDA kernel / KV cache 首次开销) ----
for i in range(2):
    t0 = time.time()
    agent.ground_image("click the search box", img1080, topk=1)
    print(f"[warmup {i+1}] {time.time()-t0:.2f}s")

# ---- 4. 正式计时: 截图 / 推理 / 端到端, 各 N 次 ----
N = 6
INSTRUCTIONS = ["click the search box", "find the close button",
                "click the settings icon", "find the play button",
                "click the menu button", "find the user avatar"]
cap_ts, inf_ts, e2e_ts = [], [], []
for i in range(N):
    t0 = time.time()
    # 截图 (真实链路, 不用预置图)
    shot = agent.desktop_screenshot_pil().convert("RGB")
    if shot.size != (1920, 1080):
        shot = shot.resize((1920, 1080), Image.LANCZOS)
    t1 = time.time()
    agent.ground_image(INSTRUCTIONS[i % len(INSTRUCTIONS)], shot, topk=1)
    t2 = time.time()
    cap_ts.append(t1 - t0); inf_ts.append(t2 - t1); e2e_ts.append(t2 - t0)
    print(f"[run {i+1}] 截图+缩放 {cap_ts[-1]*1000:.0f}ms | 推理 {inf_ts[-1]*1000:.0f}ms | 端到端 {e2e_ts[-1]*1000:.0f}ms")

def stat(name, xs):
    print(f"[{name}] mean={statistics.mean(xs)*1000:.0f}ms "
          f"median={statistics.median(xs)*1000:.0f}ms "
          f"min={min(xs)*1000:.0f}ms max={max(xs)*1000:.0f}ms")

stat("截图+缩放", cap_ts)
stat("推理", inf_ts)
stat("端到端", e2e_ts)
if torch.cuda.is_available():
    print(f"[vram] 显存峰值 {torch.cuda.max_memory_allocated()/1024**3:.2f}GB")
print("RAW_LAST:", agent._last_raw_output[:200])
