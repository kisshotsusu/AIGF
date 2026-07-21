from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .ai_live_assistant.app import LiveAssistant
from .ai_live_assistant.config import load_config
from .ai_live_assistant.instance_lock import InstanceLock


def main() -> None:
    parser = argparse.ArgumentParser(description="B站 AI 直播弹幕助手")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--check", action="store_true", help="只检查配置")
    args = parser.parse_args()
    cfg = load_config(Path(args.config).resolve())
    logging.basicConfig(level=getattr(logging, cfg["app"].get("log_level", "INFO")), format="%(asctime)s | %(levelname)s | %(message)s")
    if args.check:
        print("配置检查通过")
        return
    lock = InstanceLock(Path(cfg["_root"]) / "state" / "live-assistant.lock")
    if not lock.acquire():
        raise SystemExit("直播助手已在运行，拒绝启动重复实例")
    try:
        asyncio.run(LiveAssistant(cfg).run())
    finally:
        lock.release()


if __name__ == "__main__": main()
