#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""严格精度验证:
1) 确定性: 同一输入同配置跑 3 次, 检查采样路径是否稳定
2) 合成图真值精度: 1080p 图上放已知坐标的带标签按钮, 量化 原生 vs 降采样 误差
"""
import os, sys, time, statistics

os.environ["VISION_BACKEND"] = "gui_owl"
os.environ["VISION_MAX_SIDE"] = "0"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import torch
from PIL import Image, ImageDraw, ImageFont
import agent

agent.load_model()

def make_font(size):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()

# ---- 合成 1080p 测试图: 4 个按钮, 位置/颜色/字号各不相同 ----
W, H = 1920, 1080
img = Image.new("RGB", (W, H), (240, 240, 245))
d = ImageDraw.Draw(img)
BUTTONS = [
    ("Submit",   0.25, 0.30, (200, 60, 60)),
    ("Settings", 0.70, 0.55, (60, 60, 200)),
    ("Search",   0.45, 0.80, (60, 140, 60)),
    ("Close",    0.85, 0.15, (120, 60, 160)),
]
font = make_font(30)
truth = {}
for label, nx, ny, color in BUTTONS:
    cx, cy = int(nx * W), int(ny * H)
    w, h = 220, 70
    d.rounded_rectangle([cx - w//2, cy - h//2, cx + w//2, cy + h//2],
                        radius=12, fill=color)
    tb = d.textbbox((0, 0), label, font=font)
    d.text((cx - (tb[2]-tb[0])//2, cy - (tb[3]-tb[1])//2), label,
           fill=(255, 255, 255), font=font)
    truth[label] = (nx, ny)
img.save(os.path.join(HERE, "test_artifacts", "bench_synth_1080p.png"))

def run(inst, img, n=1):
    outs = []
    for _ in range(n):
        pts = agent.ground_image(inst, img, topk=1)
        outs.append((round(pts[0][0], 3), round(pts[0][1], 3)) if pts else None)
    return outs

agent.ground_image("warmup", img, topk=1)

# ---- 1) 确定性: 同图同指令 x3 ----
print("[确定性] 原生 1080p, 'click the Submit button' x3:")
det = run("click the Submit button", img, n=3)
print("  ", det, "->", "稳定" if len(set(det)) == 1 else "不稳定!")

# ---- 2) 真值精度: 原生 vs 降采样 ----
print("\n[真值精度] 合成 1080p 图:")
for side, tag in ((0, "原生1080p"), (1280, "降采样720p")):
    agent.VISION_MAX_SIDE = side
    errs, found = [], 0
    for label, (nx, ny) in truth.items():
        r = run(f"click the {label} button", img, n=1)[0]
        if r:
            found += 1
            err = ((r[0]-nx)**2 + (r[1]-ny)**2) ** 0.5
            errs.append(err)
            print(f"  [{tag}] {label:9s}: 预测{r} 真值({nx},{ny}) 偏差{err*100:.2f}%")
        else:
            print(f"  [{tag}] {label:9s}: 未找到!")
    if errs:
        print(f"  [{tag}] 找到{found}/4 | 平均偏差 {statistics.mean(errs)*100:.2f}% | "
              f"最大 {max(errs)*100:.2f}%  (1080p 全屏下 1% ≈ 21px)")
