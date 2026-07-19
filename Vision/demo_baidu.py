#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""演示：用 GUI-Actor-2B + Playwright 打开百度、识别搜索框、输入并搜索。
截图保存到 logs/ 下，供用户查看。"""
import os
import sys
from pathlib import Path

HERE = str(Path(__file__).resolve().parent)
sys.path.insert(0, HERE)
import agent

LOGDIR = os.path.join(HERE, "logs")
os.makedirs(LOGDIR, exist_ok=True)

print("[demo] 打开 https://www.baidu.com ...", flush=True)
agent.navigate("https://www.baidu.com")

# 截图1：百度首页
home = agent.screenshot_pil()
home.save(os.path.join(LOGDIR, "baidu_home.png"))
print("[demo] 已保存首页截图 baidu_home.png", flush=True)

# 识别并点击搜索框，输入"百度"
print("[demo] GUI-Actor 识别搜索框并输入 ...", flush=True)
r = agent.type_text("click the search input box to type", "百度")
print("[demo] type_text ->", r, flush=True)

# 回车搜索
page = agent.ensure_browser()
page.keyboard.press("Enter")
agent.wait(2500)

# 截图2：搜索结果
res = agent.screenshot_pil()
res.save(os.path.join(LOGDIR, "baidu_result.png"))
print("[demo] 已保存结果截图 baidu_result.png", flush=True)
print("[demo] 搜索后 URL:", agent.get_url(), flush=True)

agent.close()
print("[demo] DONE")
