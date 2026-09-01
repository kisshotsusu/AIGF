#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""实验2: 静态 KV cache + torch.compile(reduce-overhead) 能否压解码调度开销
成功标准: 解码 ms/token 相比 36ms(720p) 明显下降; 失败则回退报告
"""
import os, sys, time, statistics

os.environ["VISION_BACKEND"] = "gui_owl"
os.environ["VISION_MAX_SIDE"] = "1280"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
import agent
from qwen_vl_utils import process_vision_info

agent.load_model()
proc, model = agent._processor, agent._model
SYS = agent._OWL_COMPACT_SYSTEM_PROMPT

try:
    img = agent.desktop_screenshot_pil().convert("RGB")
except Exception as exc:
    from PIL import Image, ImageDraw
    print(f"[warn] 桌面截图失败, 改用合成 1080p 图")
    img = Image.new("RGB", (1920, 1080), (240, 240, 245))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([380, 260, 600, 330], radius=12, fill=(200, 60, 60))
    d.rounded_rectangle([1240, 540, 1460, 610], radius=12, fill=(60, 60, 200))
def build(inst):
    messages = [
        {"role": "system", "content": [{"type": "text", "text": SYS}]},
        {"role": "user", "content": [{"type": "image", "image": img},
                                     {"type": "text", "text": inst}]},
    ]
    text = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    ii, vi = process_vision_info(messages)
    return proc(text=[text], images=ii, videos=vi, padding=True,
                return_tensors="pt").to(model.device)

def gen(inputs):
    t0 = time.time()
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=64, do_sample=True,
                             temperature=0.01, top_p=0.01, top_k=1, repetition_penalty=1.0)
    dt = time.time() - t0
    n = out.shape[1] - inputs.input_ids.shape[1]
    return dt, n

# ---- 基线: eager ----
inputs = build("click the search box")
gen(inputs); gen(inputs)
ts = [gen(build(i)) for i in ["click the search box", "find the close button",
                              "click the settings icon", "find the play button"]]
eager_ms = statistics.mean(dt / max(n, 1) * 1000 for dt, n in ts)
print(f"[eager 基线] 解码 {eager_ms:.1f}ms/token "
      f"(生成均值 {statistics.mean(dt*1000 for dt, n in ts):.0f}ms)")

# ---- 实验: static cache + compile ----
print("[实验] 启用 static cache + torch.compile(reduce-overhead) ...")
model.generation_config.cache_implementation = "static"
t0 = time.time()
try:
    model.forward = torch.compile(model.forward, mode="reduce-overhead", fullgraph=False)
    # 首次触发编译 (可能耗时数分钟)
    gen(inputs)
    compile_s = time.time() - t0
    print(f"[实验] 编译+首次运行 {compile_s:.0f}s")
    ts = [gen(build(i)) for i in ["click the search box", "find the close button",
                                  "click the settings icon", "find the play button",
                                  "click the search box"]]
    compiled_ms = statistics.mean(dt / max(n, 1) * 1000 for dt, n in ts)
    print(f"[实验结果] 解码 {compiled_ms:.1f}ms/token "
          f"(生成均值 {statistics.mean(dt*1000 for dt, n in ts):.0f}ms) "
          f"vs eager {eager_ms:.1f}ms/token -> "
          f"{'有效, 提速 ' + format(eager_ms/compiled_ms, '.2f') + 'x' if compiled_ms < eager_ms * 0.9 else '收益不明显'}")
except Exception as exc:
    print(f"[实验失败] torch.compile 不可用: {type(exc).__name__}: {str(exc)[:300]}")
