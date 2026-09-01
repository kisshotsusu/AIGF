#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""实验1: 紧凑输出格式 vs 官方 <tool_call> 格式
对比: 输出 token 数 / 解码耗时 / 合成图真值精度
"""
import os, sys, time, statistics

os.environ["VISION_BACKEND"] = "gui_owl"
os.environ["VISION_MAX_SIDE"] = "1280"  # 用落地后的默认配置测
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
from PIL import Image, ImageDraw, ImageFont
import agent
from qwen_vl_utils import process_vision_info

agent.load_model()
proc, model, tok = agent._processor, agent._model, agent._tokenizer

BASE_SYS = agent._GUI_OWL_SYSTEM_PROMPT + agent._GUI_OWL_INFEASIBLE_SUFFIX
COMPACT_SYS = (
    "# Task\n\n"
    "You control a computer mouse. The screen is 1000x1000 normalized.\n"
    "Locate the UI element described by the user and respond with ONLY one line:\n"
    '{"action": "left_click", "coordinate": [x, y]}\n'
    "where x, y are integers in 0~1000 marking the element center.\n"
    'If the element does not exist in the image, respond with ONLY: '
    '{"action": "terminate", "status": "failure"}\n'
    "No explanation, no markdown, no XML tags."
)

# ---- 合成 1080p 测试图 (与 bench_precision.py 相同布局) ----
W, H = 1920, 1080
img = Image.new("RGB", (W, H), (240, 240, 245))
d = ImageDraw.Draw(img)
BUTTONS = [("Submit", 0.25, 0.30, (200, 60, 60)), ("Settings", 0.70, 0.55, (60, 60, 200)),
           ("Search", 0.45, 0.80, (60, 140, 60)), ("Close", 0.85, 0.15, (120, 60, 160))]
try:
    font = ImageFont.truetype("arial.ttf", 30)
except Exception:
    font = ImageFont.load_default()
truth = {}
for label, nx, ny, color in BUTTONS:
    cx, cy = int(nx * W), int(ny * H)
    d.rounded_rectangle([cx-110, cy-35, cx+110, cy+35], radius=12, fill=color)
    tb = d.textbbox((0, 0), label, font=font)
    d.text((cx-(tb[2]-tb[0])//2, cy-(tb[3]-tb[1])//2), label, fill=(255,255,255), font=font)
    truth[label] = (nx, ny)

def run(sys_prompt, inst, img):
    messages = [
        {"role": "system", "content": [{"type": "text", "text": sys_prompt}]},
        {"role": "user", "content": [{"type": "image", "image": img},
                                     {"type": "text", "text": inst}]},
    ]
    text = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    ii, vi = process_vision_info(messages)
    inputs = proc(text=[text], images=ii, videos=vi, padding=True, return_tensors="pt").to(model.device)
    t0 = time.time()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=64, do_sample=True,
                             temperature=0.01, top_p=0.01, top_k=1, repetition_penalty=1.0)
    dt = time.time() - t0
    n = out.shape[1] - inputs.input_ids.shape[1]
    txt = proc.batch_decode([out[0][inputs.input_ids.shape[1]:]], skip_special_tokens=True)[0]
    pts = agent._parse_gui_owl_points(txt, 1)
    return dt, n, txt.strip()[:120], ((round(pts[0][0],3), round(pts[0][1],3)) if pts else None)

# 预热 (两种提示词各一次)
run(BASE_SYS, "click the Submit button", img)
run(COMPACT_SYS, "click the Submit button", img)

print("=" * 76)
for tag, sysp in (("官方tool_call", BASE_SYS), ("紧凑格式", COMPACT_SYS)):
    errs, toks, dts, raws = [], [], [], []
    for label, (nx, ny) in truth.items():
        dt, n, raw, got = run(sysp, f"click the {label} button", img)
        toks.append(n); dts.append(dt); raws.append(raw)
        if got:
            errs.append(((got[0]-nx)**2 + (got[1]-ny)**2) ** 0.5)
    print(f"[{tag}] 输出token mean={statistics.mean(toks):.0f} | "
          f"生成 mean={statistics.mean(dts)*1000:.0f}ms | "
          f"精度 mean偏差={statistics.mean(errs)*100:.2f}% max={max(errs)*100:.2f}%")
    for raw in raws[:2]:
        print(f"    raw: {raw!r}")
print("=" * 76)
