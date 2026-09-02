#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""GUI-Owl torch.compile / CUDA Graphs 加速稳态基准 (走生产路径 agent.ground_image)。

用固定合成图重复 grounding, 隔离 torch.compile 的稳态收益: 首次调用含编译开销, 不计入统计。
切换方式:
  GUI_OWL_TORCH_COMPILE=0       关闭编译 (eager 基线)
  GUI_OWL_TORCH_COMPILE=1 GUI_OWL_COMPILE_MODE=default       (动态 shape, 推荐)
  GUI_OWL_TORCH_COMPILE=1 GUI_OWL_COMPILE_MODE=reduce-overhead   (内部 CUDA Graphs)
  GUI_OWL_STATIC_SHAPE=1         固定 1280x720 让 reduce-overhead 的 CUDA Graphs 生效
"""
import os
import sys
import time
import statistics

os.environ["VISION_BACKEND"] = "gui_owl"
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
from PIL import Image, ImageDraw
import agent

W, H = 1000, 700
img = Image.new("RGB", (W, H), "white")
d = ImageDraw.Draw(img)
d.rectangle([(760, 600), (900, 660)], fill="#2e7d32", outline="#333333")
d.rectangle([(60, 60), (320, 120)], fill="#eeeeee", outline="#333333")

compile_on = os.environ.get("GUI_OWL_TORCH_COMPILE", "1").strip().lower() not in ("0", "false", "no")
mode = os.environ.get("GUI_OWL_COMPILE_MODE", "default")
static = os.environ.get("GUI_OWL_STATIC_SHAPE", "0")
print(f"===== bench: compile={'on' if compile_on else 'off'} mode={mode} static_shape={static} =====")
print(f"torch {torch.__version__} | cuda {torch.cuda.is_available()}")

t0 = time.time()
agent.load_model()
print(f"[load] {time.time()-t0:.1f}s")

# warmup: 触发编译, 不计时 (用稳定出坐标的指令, 反映真实 grounding 延迟)
_ = agent.ground_image("click the search box", img, topk=1)
if torch.cuda.is_available():
    print(f"[warmup done] VRAM peak={torch.cuda.max_memory_allocated()/1024**3:.2f}GB")

N = 6
times = []
for i in range(N):
    t = time.time()
    pts = agent.ground_image("click the search box", img, topk=1)
    times.append(time.time() - t)
print(f"[per-call ms] {[round(x*1000, 1) for x in times]}")
print(f"[median ms ] {round(statistics.median(times)*1000, 1)}")
print(f"[first  ms ] {round(times[0]*1000, 1)}  (warmup 已排除编译开销)")
print(f"[last   ms ] {round(times[-1]*1000, 1)}")
print(f"[result] {pts}")
print("===== bench done =====")
