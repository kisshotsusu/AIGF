#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""验证: 贪心解码 do_sample=False vs 采样路径 的解码速度 + 精度复核"""
import os, sys, time, statistics

os.environ["VISION_BACKEND"] = "gui_owl"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
from PIL import Image
import agent
from qwen_vl_utils import process_vision_info

agent.load_model()
proc, model, tok = agent._processor, agent._model, agent._tokenizer
SYS = agent._GUI_OWL_SYSTEM_PROMPT + agent._GUI_OWL_INFEASIBLE_SUFFIX

raw = agent.desktop_screenshot_pil().convert("RGB")
img1080 = raw if raw.size == (1920, 1080) else raw.resize((1920, 1080), Image.LANCZOS)

def build(img, inst):
    messages = [
        {"role": "system", "content": [{"type": "text", "text": SYS}]},
        {"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text", "text": inst},
        ]},
    ]
    text = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    ii, vi = process_vision_info(messages)
    return proc(text=[text], images=ii, videos=vi, padding=True, return_tensors="pt").to(model.device)

def gen(inputs, greedy, max_new=64):
    t0 = time.time()
    kw = dict(max_new_tokens=max_new, repetition_penalty=1.0)
    if greedy:
        kw.update(do_sample=False)
    else:
        kw.update(do_sample=True, temperature=0.01, top_p=0.01, top_k=1)
    with torch.inference_mode():
        out = model.generate(**inputs, **kw)
    dt = time.time() - t0
    n = out.shape[1] - inputs.input_ids.shape[1]
    txt = proc.batch_decode([out[0][inputs.input_ids.shape[1]:]],
                            skip_special_tokens=True)[0]
    return dt, n, txt

# 预热
inputs = build(img1080, "click the search box")
gen(inputs, greedy=True, max_new=32)

INSTR = ["click the search box", "find the close button",
         "click the settings icon", "find the play button"]

for greedy in (True, False):
    ts, outs = [], []
    for inst in INSTR:
        inputs = build(img1080, inst)
        dt, n, txt = gen(inputs, greedy)
        ts.append(dt / max(n, 1)); outs.append(agent._parse_gui_owl_points(txt, 1))
    print(f"[{'贪心' if greedy else '采样'}] 1080p 解码 {statistics.mean(ts)*1000:.1f}ms/token "
          f"| 结果: {[(p[0] if p else None) for p in outs]}")

# 720p + 贪心 组合拳 + 精度对照 (与 1080p 结果比对)
img720 = img1080.resize((1280, 720), Image.LANCZOS)
ts = []
r720 = []
for inst in INSTR:
    inputs = build(img720, inst)
    dt, n, txt = gen(inputs, greedy=True)
    ts.append(dt / max(n, 1))
    pts = agent._parse_gui_owl_points(txt, 1)
    r720.append((int(pts[0][0] * 1280), int(pts[0][1] * 720)) if pts else None)
print(f"[720p+贪心] 解码 {statistics.mean(ts)*1000:.1f}ms/token | 像素坐标: {r720}")
print("(1080p 原生分辨率下同一指令的坐标应与上面 720p 结果按 1.5x 比例接近)")
