"""Harness shell entrypoint: list tools, or call using a JSON stdin request."""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def main():
    payload = None if sys.argv[1:] == ["list"] else sys.stdin.buffer.read(2_000_001)
    if payload is not None:
        if len(payload) > 2_000_000:
            raise ValueError("Tool request too large")
        request = json.loads(payload)
        if not isinstance(request, dict):
            raise ValueError("Expected a JSON object")
    url = os.environ["AIGF_TOOL_BRIDGE_URL"] + ("/tools" if payload is None else "/call")
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": "Bearer " + os.environ["AIGF_TOOL_BRIDGE_TOKEN"],
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=360) as response:
        sys.stdout.buffer.write(response.read())


if __name__ == "__main__":
    main()
