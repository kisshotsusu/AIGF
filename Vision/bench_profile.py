#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""分段剖析: 定位 1.7s 到底花在哪 (CPU预处理 / prefill / decode), 并测优化手段"""
import os, sys, time, statistics

os.environ["VISION_BACKEND"] = "gui_owl"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
from PIL import Image
import agent

agent.load_model()
proc, model, tok = agent._processor, agent._model, agent._tokenizer

raw = agent.desktop_screenshot_pil().convert("RGB")
img1080 = raw if raw.size == (1920, 1080) else raw.resize((1920, 1080), Image.LANCZOS)

from qwen_vl_utils import process_vision_info

SYS = agent._GUI_OWL_SYSTEM_PROMPT + agent._GUI_OWL_INFEASIBLE_SUFFIX

def build_inputs(img):
    messages = [
        {"role": "system", "content": [{"type": "text", "text": SYS}]},
        {"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text", "text": "click the search box"},
        ]},
    ]
    t0 = time.time()
    text = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs = process_vision_info(messages)
    inputs = proc(text=[text], images=image_inputs, videos=video_inputs,
                  padding=True, return_tensors="pt").to(model.device)
    return inputs, time.time() - t0

def run_gen(inputs, max_new=256):
    t0 = time.time()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new, do_sample=True,
                             temperature=0.01, top_p=0.01, top_k=1, repetition_penalty=1.0)
    return time.time() - t0, out.shape[1] - inputs.input_ids.shape[1]

def profile(tag, img, n=4, max_new=256):
    prep, gen, nvis = [], [], 0
    for i in range(n):
        inputs, pt = build_inputs(img)
        if i == 0:
            nvis = (inputs.input_ids != tok.pad_token_id).sum().item()
            nvis_img = inputs.image_grid_thw.prod(dim=1).sum().item() if hasattr(inputs, "image_grid_thw") else -1
        gt, nout = run_gen(inputs, max_new)
        prep.append(pt); gen.append(gt)
    print(f"[{tag}] 预处理(CPU) mean={statistics.mean(prep)*1000:.0f}ms | "
          f"生成(GPU) mean={statistics.mean(gen)*1000:.0f}ms | "
          f"视觉token≈{nvis_img} | 输出token={nout}")

# 预热
inputs, _ = build_inputs(img1080); run_gen(inputs, 32)

print("=" * 70)
profile("1080p  1920x1080", img1080)
profile("720p   1280x720 ", img1080.resize((1280, 720), Image.LANCZOS))
profile("540p   960x540  ", img1080.resize((960, 540), Image.LANCZOS))
profile("1080p max_new=64", img1080, max_new=64)

# 半屏裁剪 (窗口场景模拟: 960x1080)
crop = img1080.crop((0, 0, 960, 1080))
profile("半屏裁剪 960x1080", crop, n=4)

# 纯 prefill 计时 (n 都相同则只需一次): forward 一次不生成
inputs, pt = build_inputs(img1080)
t0 = time.time()
with torch.inference_mode():
    model(**inputs)
print(f"[1080p 纯prefill forward] {(time.time()-t0)*1000:.0f}ms (排除 generate 开销)")
