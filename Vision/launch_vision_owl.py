#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以 DETACHED_PROCESS 脱离当前 shell 启动 GUI-Owl Vision MCP 服务（端口 8765）。"""
import os
import subprocess

HERE = r"E:\Doc\AIAgent\Vision"
PY = r"E:\Doc\AIAgent\.venv-owl\Scripts\python.exe"
LOG = os.path.join(HERE, "vision_mcp_owl.log")

env = dict(os.environ)
env["VISION_BACKEND"] = "gui_owl"
env["GUI_OWL_MODEL"] = os.path.join(HERE, "models", "GUI-Owl-1.5-2B-Instruct")
env["VISION_MCP_TRANSPORT"] = "http"
env["VISION_MCP_HOST"] = "127.0.0.1"
env["VISION_MCP_PORT"] = "8765"
env["VISION_PRELOAD_MODEL"] = "1"
env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

CREATE_BREAKAWAY_FROM_JOB = 0x01000000
with open(LOG, "w", encoding="utf-8") as log:
    p = subprocess.Popen(
        [PY, os.path.join(HERE, "mcp_server.py")],
        env=env,
        creationflags=0x00000008 | 0x00000200 | CREATE_BREAKAWAY_FROM_JOB,  # DETACHED | NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
print(f"spawned GUI-Owl Vision MCP, pid={p.pid}, log={LOG}")
