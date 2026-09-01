#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""实验3: 手写最小贪心解码循环 vs HF generate()
对比: 解码 ms/token + 输出文本一致性
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
proc, model, tok = agent._processor, agent._model, agent._tokenizer
SYS = agent._OWL_COMPACT_SYSTEM_PROMPT

img = agent.desktop_screenshot_pil().convert("RGB")
EOS_ID = tok.eos_token_id
MAX_NEW = 48

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

@torch.inference_mode()
def manual_decode(inputs):
    """最小贪心循环: prefill -> argmax 逐步, 无 logits processors / sampler 调度。"""
    from transformers.cache_utils import DynamicCache
    cache = DynamicCache()
    out = model(**inputs, use_cache=True, past_key_values=cache)
    cur = out.logits[0, -1].argmax().view(1, 1)
    tokens = []
    n_in = inputs.input_ids.shape[1]
    for step in range(MAX_NEW):
        tokens.append(cur.item())
        if cur.item() == EOS_ID:
            break
        pos = torch.tensor([n_in + step], device=model.device)
        out = model(input_ids=cur, use_cache=True, past_key_values=cache,
                    cache_position=pos)
        cur = out.logits[0, -1].argmax().view(1, 1)
    return tokens

def hf_decode(inputs):
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=MAX_NEW, do_sample=True,
                             temperature=0.01, top_p=0.01, top_k=1, repetition_penalty=1.0)
    return out[0, inputs.input_ids.shape[1]:].tolist()

INSTR = ["click the search box", "find the close button",
         "click the settings icon", "find the play button"]

# 预热
manual_decode(build("warmup")); hf_decode(build("warmup"))

mt, ht, same = [], [], 0
for inst in INSTR:
    inputs = build(inst)
    t0 = time.time(); toks_m = manual_decode(inputs); tm = time.time() - t0
    t0 = time.time(); toks_h = hf_decode(inputs); th = time.time() - t0
    mt.append(tm / max(len(toks_m), 1)); ht.append(th / max(len(toks_h), 1))
    txt_m = tok.decode(toks_m, skip_special_tokens=True).strip()
    txt_h = tok.decode(toks_h, skip_special_tokens=True).strip()
    ok = txt_m == txt_h
    same += ok
    print(f"{inst!r}: manual {tm*1000:.0f}ms/{len(toks_m)}tok | HF {th*1000:.0f}ms/{len(toks_h)}tok | "
          f"输出{'一致' if ok else '不同!'}")
    if not ok:
        print(f"   manual: {txt_m[:100]!r}\n   HF    : {txt_h[:100]!r}")

pm = statistics.mean(mt); ph = statistics.mean(ht)
print(f"\n[summary] manual {pm*1000:.1f}ms/token vs HF {ph*1000:.1f}ms/token "
      f"-> {ph/pm:.2f}x | 输出一致 {same}/4")
