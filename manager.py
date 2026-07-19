"""Compatibility entry point for the relocated livestream console."""
from modules.live.manager import *  # noqa: F401,F403
from modules.live.manager import main


if __name__ == "__main__":
    main()
