#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI-Owl 实机测试：打开网易云音乐桌面端 -> 搜"焚蝶" -> 播放。
自包含：脚本内启动网易云（作为本进程子进程，随脚本存活），轮询窗口出现后驱动。
全程走已在 8765 的 GUI-Owl Vision MCP（窗口级截图+grounding），每步存图留证。
避开 desktop_hotkey（该环境桌面截图失败），改用"点击搜索按钮"提交搜索。
"""
import asyncio
import base64
import os
import subprocess
import sys
import time
from datetime import datetime

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

MCP_URL = "http://127.0.0.1:8765/mcp"
APP_EXE = r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe"
APP_TITLE_HINTS = ["网易云音乐", "网易云", "音乐", "NetEase", "cloudmusic"]
ART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_artifacts", "netease")
os.makedirs(ART_DIR, exist_ok=True)
_step = 0


def _ts():
    return datetime.now().strftime("%H%M%S")


def _save_images(result, label):
    paths = []
    for i, c in enumerate(getattr(result, "content", []) or []):
        if getattr(c, "type", None) == "image":
            raw = getattr(c, "data", None) or getattr(c, "blob", None)
            if not raw:
                continue
            if isinstance(raw, str):
                raw = base64.b64decode(raw)
            p = os.path.join(ART_DIR, f"{label}_{_ts()}_{i}.png")
            open(p, "wb").write(raw)
            paths.append(p)
    return paths


def _text(result):
    return "".join(getattr(c, "text", "") for c in getattr(result, "content", []) or []
                   if getattr(c, "type", None) == "text")


async def call(session, tool, args, label=None, shot=True):
    global _step
    _step += 1
    tag = label or tool
    print(f"\n=== [{_step}] {tag} ===")
    try:
        res = await session.call_tool(tool, args)
    except Exception as e:
        print(f"  !! 调用异常: {e}")
        return None
    txt = _text(res)
    if txt:
        print(f"  < {txt[:700]}")
    for p in (_save_images(res, tag.replace(' ', '_')) if shot else []):
        print(f"  [截图] {p}")
    return res


async def find_netease_window(session, timeout=30):
    """轮询直到出现网易云进程窗口，返回进程级引用 'cloudmusic.exe'（标题会变，用进程名最稳）。"""
    print(f"[*] 轮询网易云窗口（最多 {timeout}s）...")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            res = await session.call_tool("list_windows", {"title_contains": ""})
            txt = _text(res)
            # 解析 process_name 字段，找 cloudmusic
            import re
            procs = re.findall(r"'process_name':\s*'([^']*)'", txt)
            for p in procs:
                if "cloudmusic" in p.lower():
                    title = re.findall(r"'title':\s*'([^']*)'", txt)
                    print(f"  -> 找到网易云进程 (process_name={p!r})")
                    return "cloudmusic.exe"
        except Exception:
            pass
        await asyncio.sleep(2)
    print("  !! 超时未找到网易云窗口。当前窗口：")
    try:
        res = await session.call_tool("list_windows", {"title_contains": ""})
        print("   ", _text(res)[:800])
    except Exception as e:
        print("   list_windows err:", e)
    return None


async def main():
    print(f"启动网易云音乐: {APP_EXE}")
    proc = subprocess.Popen([APP_EXE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  pid={proc.pid}")

    print(f"连接 GUI-Owl Vision MCP: {MCP_URL}")
    async with streamablehttp_client(MCP_URL) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("MCP 会话已初始化")

            r = await call(session, "vision_backend", {}, "vision_backend")
            title = await find_netease_window(session, timeout=35)
            if not title:
                print("\n[!] 无法从本上下文拉起可见的网易云窗口（会话/桌面边界限制）。")
                print("    请手动打开网易云音乐后，再运行本脚本（它会自动检测到窗口并继续驱动）。")
                return

            await call(session, "window_screenshot", {"title_contains": title}, "shot_initial")
            time.sleep(1.5)

            # 点击搜索框
            r = await call(session, "window_click",
                           {"title_contains": title, "instruction": "搜索框", "topk": 3}, "click_searchbox")
            if r and '"clicked": false' in _text(r).replace(" ", ""):
                await call(session, "window_click",
                           {"title_contains": title, "instruction": "search box", "topk": 3}, "click_searchbox_en")

            # 输入 焚蝶
            await call(session, "window_type_text",
                       {"title_contains": title, "instruction": "搜索框", "text": "焚蝶"}, "type_焚蝶")
            time.sleep(1.0)

            # 输入后稍等下拉建议，再点第一项建议（避免点中麦克风）
            time.sleep(1.5)
            r = await call(session, "window_click",
                           {"title_contains": title, "instruction": "搜索建议里的第一项", "topk": 3}, "click_first_suggestion")
            if r and '"clicked": false' in _text(r).replace(" ", ""):
                await call(session, "window_click",
                           {"title_contains": title, "instruction": "搜索下拉里的 焚蝶", "topk": 3}, "click_suggestion_fandie")
            time.sleep(2.5)
            await call(session, "window_screenshot", {"title_contains": title}, "shot_search_results")

            # 双击歌曲列表第一行播放
            r = await call(session, "window_double_click",
                           {"title_contains": title, "instruction": "歌曲列表第一行", "topk": 5},
                           "dblclick_first_row")
            if r and '"double_clicked": false' in _text(r).replace(" ", ""):
                # fallback：先点第一首歌选中，再点底部播放栏播放
                await call(session, "window_click",
                           {"title_contains": title, "instruction": "第一首歌", "topk": 5}, "click_first_song")
                await call(session, "window_click",
                           {"title_contains": title, "instruction": "底部播放栏的播放按钮", "topk": 3}, "click_playbar_play")
            time.sleep(3.0)
            await call(session, "window_screenshot", {"title_contains": title}, "shot_playing")
            await call(session, "vision_memory_status", {}, "vision_memory_status", shot=False)

            print(f"\n=== 测试结束 | 网易云 title={title!r} | 截图目录: {ART_DIR} ===")


if __name__ == "__main__":
    asyncio.run(main())
