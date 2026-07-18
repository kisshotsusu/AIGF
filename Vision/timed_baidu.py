#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
带详细计时的百度搜索演示。
重点测量: 模型加载 / 单次 grounding 推理 / 总耗时。
"""
import os
import time
import json

import agent

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "timed_demo.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    open(LOG, "w", encoding="utf-8").close()  # 清空
    t0 = time.perf_counter()

    # ---- 1. 模型加载 ----
    t_load0 = time.perf_counter()
    log("步骤1: 加载 GUI-Actor-2B 模型 ...")
    agent.load_model()
    device = agent._model.device
    t_load1 = time.perf_counter()
    log(f"  模型已加载到 {device} | 耗时 {t_load1 - t_load0:.2f}s")

    # ---- 2. 打开百度 ----
    t_nav0 = time.perf_counter()
    log("步骤2: 打开 https://www.baidu.com ...")
    url = agent.navigate("https://www.baidu.com")
    t_nav1 = time.perf_counter()
    log(f"  已打开 | 当前URL={url} | 耗时 {t_nav1 - t_nav0:.2f}s")

    # ---- 3. 截图 ----
    t_shot0 = time.perf_counter()
    img = agent.screenshot_pil()
    t_shot1 = time.perf_counter()
    log(f"  首页截图完成 {img.size} | 耗时 {t_shot1 - t_shot0:.2f}s")

    # ---- 4. grounding 识别搜索框 (核心 GPU 指标) ----
    t_g0 = time.perf_counter()
    log("步骤3: grounding 识别搜索框 ...")
    pts = agent.ground("locate the search input box", topk=3)
    t_g1 = time.perf_counter()
    if not pts:
        log("  !! 模型未返回坐标")
    else:
        x, y = pts[0]
        px, py = int(x * agent.VIEWPORT["width"]), int(y * agent.VIEWPORT["height"])
        log(f"  识别到坐标 norm=({x:.3f},{y:.3f}) pixel=({px},{py}) | "
            f"耗时 {t_g1 - t_g0:.2f}s | 全部候选={pts}")
        # ---- 5. 点击搜索框并输入 ----
        t_type0 = time.perf_counter()
        agent._page.mouse.click(px, py)
        agent._page.wait_for_timeout(300)
        agent._page.keyboard.type("百度", delay=20)
        agent._page.wait_for_timeout(200)
        agent._page.keyboard.press("Enter")
        t_type1 = time.perf_counter()
        log(f"  已输入'百度'并回车 | 耗时 {t_type1 - t_type0:.2f}s")

    # ---- 6. 等待结果 + 截图 ----
    t_wait0 = time.perf_counter()
    agent._page.wait_for_timeout(2500)
    final_url = agent._page.url
    res = agent.screenshot_pil()
    out = os.path.join(os.path.dirname(LOG), "baidu_result.png")
    res.save(out)
    t_wait1 = time.perf_counter()
    log(f"  结果页已截图 -> {out} | 最终URL={final_url} | 耗时 {t_wait1 - t_wait0:.2f}s")

    # ---- 汇总 ----
    t_total = time.perf_counter()
    summary = {
        "device": str(device),
        "model_load_s": round(t_load1 - t_load0, 2),
        "navigate_s": round(t_nav1 - t_nav0, 2),
        "screenshot_s": round(t_shot1 - t_shot0, 2),
        "grounding_infer_s": round(t_g1 - t_g0, 2),
        "type_enter_s": round(t_type1 - t_type0, 2) if pts else 0,
        "wait_result_s": round(t_wait1 - t_wait0, 2),
        "total_s": round(t_total - t0, 2),
    }
    log("=" * 40)
    log("计时汇总:")
    for k, v in summary.items():
        log(f"  {k:20s}: {v}s")
    log("=" * 40)

    agent.close()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("\nSUMMARY_JSON:" + json.dumps(summary, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
