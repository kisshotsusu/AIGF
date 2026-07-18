#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证 SenseVoiceSmall 本地加载 + 真实转写 (CPU, 避开被占用的 GPU)。"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

t0 = time.time()
import asr
print(f"[verify] asr 导入完成 {time.time()-t0:.1f}s", flush=True)

sample = os.path.join(HERE, "models", "SenseVoiceSmall", "example", "zh.mp3")
print(f"[verify] 样本: {sample}  存在={os.path.isfile(sample)}", flush=True)

t1 = time.time()
r = asr.transcribe_file(sample, language="zh")
print(f"[verify] 转写完成 {time.time()-t1:.1f}s", flush=True)
print("=== 结果 ===")
print("language:", r.get("language"))
print("raw  :", r.get("raw"))
print("text :", r.get("text"))
print("[verify] ALL_DONE")
