#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
下载 GUI-Actor-2B (Qwen2-VL) 权重到 ./models/GUI-Actor-2B-Qwen2-VL

用 curl 逐文件断点续传下载, 规避 huggingface_hub 大文件单连接被代理掐断的问题。
用法: python download_model.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "models", "GUI-Actor-2B-Qwen2-VL")
REPO = "microsoft/GUI-Actor-2B-Qwen2-VL"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/main"

# 推理所需文件(跳过 .gitattributes 与训练产物)
FILES = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "preprocessor_config.json",
    "generation_config.json",
    "added_tokens.json",
    "chat_template.json",
    "merges.txt",
    "vocab.json",
    "args.json",
    "README.md",
]

# 可选: 设置 HF_TOKEN 环境变量可提升匿名限速下的稳定性
TOKEN = os.environ.get("HF_TOKEN")


def main():
    os.makedirs(TARGET, exist_ok=True)
    headers = ["-H", f"Authorization: Bearer {TOKEN}"] if TOKEN else []
    for f in FILES:
        out = os.path.join(TARGET, f)
        url = f"{BASE_URL}/{f}"
        print(f"[download] {f} -> {out}", flush=True)
        # -C - : 断点续传; --retry-all-errors : 网络抖动重试
        cmd = [
            "curl", "-L", "--retry", "30", "--retry-delay", "3",
            "--retry-all-errors", "-C", "-", "-o", out, url,
        ] + headers
        # 大文件可能需要更久; 不限制最大时间
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f"[WARN] curl exited {rc} for {f}; will retry on next run (resume).", flush=True)
        else:
            print(f"[ok] {f}", flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
