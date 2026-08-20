#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
下载 Vision 模型权重到 ./models/。

用法:
  python download_model.py --model gui-owl    # 默认: GUI-Owl-1.5-2B-Instruct (~5GB)
  python download_model.py --model gui-actor  # 旧版 GUI-Actor-2B-Qwen2-VL (~4.5GB)
  python download_model.py --proxy http://127.0.0.1:7897   # 手动指定代理

用 curl 逐文件断点续传下载, 规避 huggingface_hub 大文件单连接被代理掐断的问题。
未显式指定代理时, 自动读取 HTTPS_PROXY/HTTP_PROXY 或 Windows 系统代理。
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

MODELS = {
    "gui-owl": {
        "repo": "mPLUG/GUI-Owl-1.5-2B-Instruct",
        "target_dir": "GUI-Owl-1.5-2B-Instruct",
        "files": [
            "config.json",
            "model.safetensors",
            "tokenizer.json",
            "tokenizer_config.json",
            "preprocessor_config.json",
            "video_preprocessor_config.json",
            "generation_config.json",
            "chat_template.json",
            "merges.txt",
            "vocab.json",
            "README.md",
        ],
    },
    "gui-actor": {
        "repo": "microsoft/GUI-Actor-2B-Qwen2-VL",
        "target_dir": "GUI-Actor-2B-Qwen2-VL",
        "files": [
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
        ],
    },
}


def _system_proxy_windows() -> str:
    """从 Windows 注册表读取系统代理 (如 Clash 的 127.0.0.1:7897)。"""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if enabled and server:
            server = str(server).strip()
            if not server.startswith(("http://", "https://", "socks")):
                server = "http://" + server
            return server
    except OSError:
        pass
    return ""


def _resolve_proxy(explicit: str) -> str:
    if explicit:
        return explicit
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return _system_proxy_windows()


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 Vision 模型权重")
    parser.add_argument(
        "--model",
        choices=list(MODELS),
        default="gui-owl",
        help="要下载的模型 (默认 gui-owl)",
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="HTTP 代理地址, 如 http://127.0.0.1:7897 (默认自动读取环境/系统代理)",
    )
    args = parser.parse_args()

    spec = MODELS[args.model]
    target = os.path.join(HERE, "models", spec["target_dir"])
    base_url = f"https://huggingface.co/{spec['repo']}/resolve/main"

    print(f"[download] {spec['repo']} -> {target}", flush=True)
    os.makedirs(target, exist_ok=True)

    # 可选: 设置 HF_TOKEN 环境变量可提升匿名限速下的稳定性
    token = os.environ.get("HF_TOKEN")
    headers = ["-H", f"Authorization: Bearer {token}"] if token else []
    proxy = _resolve_proxy(args.proxy)
    proxy_args = ["-x", proxy] if proxy else []
    if proxy:
        print(f"[proxy] 使用代理 {proxy}", flush=True)

    for name in spec["files"]:
        out = os.path.join(target, name)
        url = f"{base_url}/{name}"
        print(f"[download] {name} -> {out}", flush=True)
        # -C - : 断点续传; --retry-all-errors : 网络抖动重试
        cmd = [
            "curl", "-L", "--retry", "30", "--retry-delay", "3",
            "--retry-all-errors", "-C", "-", "-o", out, url,
        ] + proxy_args + headers
        # 大文件可能需要更久; 不限制最大时间
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f"[WARN] curl exited {rc} for {name}; will retry on next run (resume).", flush=True)
        else:
            print(f"[ok] {name}", flush=True)
    print("ALL_DONE", flush=True)


if __name__ == "__main__":
    main()
