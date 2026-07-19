from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .ai_live_assistant.app import LiveAssistant
from .ai_live_assistant.config import load_config


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
    asyncio.run(LiveAssistant(cfg).run())


if __name__ == "__main__": main()
