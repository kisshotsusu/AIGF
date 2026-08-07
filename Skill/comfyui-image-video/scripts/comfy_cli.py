"""CLI wrapper for HomeAgent's ComfyUI image/video generation module.

Usage examples:
  python comfy_cli.py status
  python comfy_cli.py models
  python comfy_cli.py generate-image --prompt "..." [--model anima] [--steps 8] [--width 1024] [--height 1024]
  python comfy_cli.py edit-image --image <path> --prompt "..."
  python comfy_cli.py generate-video --prompt "..." [--frames 24] [--steps 8] [--first-frame <path>]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HOME_AGENT = ROOT / "HomeAgent"
if str(HOME_AGENT) not in sys.path:
    sys.path.insert(0, str(HOME_AGENT))

import yaml  # noqa: E402

from home_modules.comfyui_client import ComfyUIClient  # noqa: E402


def _client() -> ComfyUIClient:
    project = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    config = dict(project.get("comfyui", {}) or {})
    config.setdefault("project_root", str(ROOT))
    return ComfyUIClient(config)


async def _main(args: argparse.Namespace) -> int:
    client = _client()
    try:
        if args.command == "status":
            result = await client.ensure_running()
        elif args.command == "models":
            result = await client.list_models()
        elif args.command == "generate-image":
            result = await client.generate_image(
                args.prompt, args.negative_prompt or "", args.model,
                width=args.width, height=args.height, steps=args.steps, cfg=args.cfg,
                seed=args.seed, use_lora=not args.no_lora,
                status=lambda text: print(f"[status] {text}", flush=True),
            )
        elif args.command == "edit-image":
            result = await client.edit_image(
                args.image, args.prompt, args.negative_prompt or "", args.model,
                steps=args.steps, cfg=args.cfg, seed=args.seed,
                status=lambda text: print(f"[status] {text}", flush=True),
            )
        elif args.command == "generate-video":
            result = await client.generate_video(
                args.prompt, args.model, width=args.width, height=args.height,
                frames=args.frames, steps=args.steps, seed=args.seed, fps=args.fps,
                first_frame=args.first_frame, use_int8=args.use_int8,
                status=lambda text: print(f"[status] {text}", flush=True),
            )
        else:
            print("未知命令")
            return 2
    except Exception as exc:
        print(f"错误：{exc}")
        return 1
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HomeAgent ComfyUI 图像/视频生成 CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="检查并启动 ComfyUI")
    sub.add_parser("models", help="列出模型预设")
    image = sub.add_parser("generate-image", help="生成图像")
    image.add_argument("--prompt", required=True)
    image.add_argument("--negative-prompt", default="")
    image.add_argument("--model", default="qwen-image-2512", choices=["qwen-image-2512", "anima"])
    image.add_argument("--width", type=int, default=1024)
    image.add_argument("--height", type=int, default=1024)
    image.add_argument("--steps", type=int, default=None)
    image.add_argument("--cfg", type=float, default=None)
    image.add_argument("--seed", type=int, default=None)
    image.add_argument("--no-lora", action="store_true")
    edit = sub.add_parser("edit-image", help="编辑图像")
    edit.add_argument("--image", required=True)
    edit.add_argument("--prompt", required=True)
    edit.add_argument("--negative-prompt", default="")
    edit.add_argument("--model", default="qwen-image-edit-2511", choices=["qwen-image-edit-2511"])
    edit.add_argument("--steps", type=int, default=None)
    edit.add_argument("--cfg", type=float, default=None)
    edit.add_argument("--seed", type=int, default=None)
    video = sub.add_parser("generate-video", help="生成视频")
    video.add_argument("--prompt", required=True)
    video.add_argument("--model", default="minimax-h3", choices=["minimax-h3"])
    video.add_argument("--width", type=int, default=1344)
    video.add_argument("--height", type=int, default=768)
    video.add_argument("--frames", type=int, default=49)
    video.add_argument("--steps", type=int, default=None)
    video.add_argument("--fps", type=int, default=24)
    video.add_argument("--seed", type=int, default=None)
    video.add_argument("--first-frame", default=None)
    video.add_argument("--use-int8", action="store_true")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
