#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载 SenseVoiceSmall 权重到 ./models/SenseVoiceSmall (走 ModelScope)。"""
import os
from modelscope import snapshot_download

HERE = os.path.dirname(os.path.abspath(__file__))
target = os.path.join(HERE, "models", "SenseVoiceSmall")
print(f"[download] iic/SenseVoiceSmall -> {target}", flush=True)
snapshot_download("iic/SenseVoiceSmall", local_dir=target)
print("[download] DONE", flush=True)
