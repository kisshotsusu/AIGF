#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GUI-Actor-2B 冒烟测试：验证模型加载 + 推理 + 浏览器全链路。"""
import os
import sys
import time
from PIL import Image, ImageDraw

HERE = r"E:/Doc/AI直播/Vision"
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

# ---- 核心2：真实浏览器截图 -> grounding（验证端到端）----
try:
    print("[smoke] 3) opening browser -> bing ...", flush=True)
    agent.navigate("https://www.bing.com")
    bpts = agent.ground("click the search box", topk=3)
    print(f"[smoke]    bing search-box grounding: {bpts}")
    agent.close()
except Exception as e:
    print(f"[smoke]    browser test error (non-fatal): {e!r}")

print("[smoke] DONE. model OK =", bool(pts), flush=True)
