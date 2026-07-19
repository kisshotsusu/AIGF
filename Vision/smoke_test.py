#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GUI-Actor-2B 冒烟测试：验证模型加载 + 推理 + 浏览器全链路。"""
import os
import sys
import time
from pathlib import Path
from PIL import Image, ImageDraw

HERE = str(Path(__file__).resolve().parent)
sys.path.insert(0, HERE)
import agent  # 复用真实加载/推理路径

t0 = time.time()
print("[smoke] 1) loading model to GPU ...", flush=True)
agent.load_model()
print(f"[smoke]    model loaded on {agent._model.device} "
      f"in {time.time()-t0:.1f}s", flush=True)

# ---- 核心1：用 PIL 生成的测试图做 grounding（验证推理链路）----
img = Image.new("RGB", (1280, 800), (235, 235, 235))
d = ImageDraw.Draw(img)
d.rectangle([580, 360, 700, 440], fill=(0, 120, 215))          # 蓝色按钮
d.polygon([(615, 378), (615, 422), (658, 400)], fill=(255, 255, 255))  # 播放三角
d.text((605, 470), "PLAY", fill=(0, 0, 0))
conv = [
    {"role": "system", "content": [{"type": "text",
     "text": agent.grounding_system_message}]},
    {"role": "user", "content": [
        {"type": "image", "image": img},
        {"type": "text", "text": "click the blue play button"},
    ]},
]
pred = agent.inference(conv, agent._model, agent._tokenizer,
                       agent._processor, use_placeholder=True, topk=3)
pts = pred.get("topk_points") or []
print(f"[smoke] 2) PIL图 grounding topk_points: {pts}")
if not pts or not all(len(p) == 2 and 0 <= p[0] <= 1 and 0 <= p[1] <= 1 for p in pts):
    raise RuntimeError(f"合成图 grounding 失败: {pts!r}")

# ---- 核心2：真实浏览器截图 -> grounding（验证端到端）----
print("[smoke] 3) opening browser -> bing ...", flush=True)
try:
    url = agent.navigate("https://www.bing.com")
    if "bing.com" not in url: raise RuntimeError(f"导航地址异常: {url}")
    bpts = agent.ground("click the search box", topk=3)
    if not bpts or not all(len(p) == 2 and 0 <= p[0] <= 1 and 0 <= p[1] <= 1 for p in bpts):
        raise RuntimeError(f"真实页面 grounding 失败: {bpts!r}")
    print(f"[smoke]    bing search-box grounding: {bpts}")
finally:
    agent.close()

print("[smoke] DONE. all checks passed", flush=True)
