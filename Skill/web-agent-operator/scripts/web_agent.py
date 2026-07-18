from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def emit(event: str, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


async def call(session, name: str, arguments=None):
    emit("step_started", tool=name)
    result = await session.call_tool(name, arguments or {})
    texts = [getattr(item, "text", "") for item in result.content if getattr(item, "text", "")]
    if getattr(result, "isError", False): raise RuntimeError("\n".join(texts) or f"{name} failed")
    emit("step_completed", tool=name)
    return "\n".join(texts)


def score(label: str, query: str) -> int:
    label = label.lower()
    tokens = [x.lower() for x in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", query)]
    return sum(3 if token in label else 0 for token in tokens) + (2 if all(token in label for token in tokens) else 0)


async def run(url: str, site: str, query: str, action: str):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if site != "bilibili": raise RuntimeError(f"unsupported site: {site}")
            emit("stage", state="NAVIGATED", detail="opening search results directly")
            await call(session, "navigate", {"url": f"https://search.bilibili.com/all?keyword={quote(query)}"})
            search_url = await call(session, "get_url")
            if "search.bilibili.com" not in search_url or "keyword=" not in search_url:
                raise RuntimeError("SEARCH_SUBMITTED: search URL was not verified")
            emit("stage", state="SEARCH_SUBMITTED", url=search_url, query=query)
            page = ast.literal_eval(await call(session, "web_read", {"max_chars": 18000}))
            candidates = []
            for link in page.get("links", []):
                href, label = str(link.get("href", "")), str(link.get("text", ""))
                if "bilibili.com/video/" in href: candidates.append((score(label, query), label, href))
            if not candidates: raise RuntimeError("no video links found in search results")
            if max(item[0] for item in candidates) <= 0:
                raise RuntimeError("RESULTS_VERIFIED: results did not match the query")
            emit("stage", state="RESULTS_VERIFIED", candidates=len(candidates))
            best_score, label, href = max(candidates, key=lambda item: item[0])
            emit("result_selected", title=label, url=href, score=best_score)
            await call(session, "navigate", {"url": href})
            verified_url = await call(session, "get_url")
            if "/video/" not in verified_url: raise RuntimeError("video page navigation was not verified")
            emit("stage", state="RESULT_SELECTED", title=label, url=verified_url)
            if action == "play":
                played = ast.literal_eval(await call(session, "web_play_media"))
                if not played.get("played"): raise RuntimeError(played.get("reason", "media did not start"))
                emit("stage", state="ACTION_EXECUTED", action="play", evidence=played)
            else:
                emit("stage", state="ACTION_EXECUTED", action="open", evidence={"url": verified_url})
            final = ast.literal_eval(await call(session, "web_read", {"max_chars": 3000}))
            final_url = str(final.get("url") or verified_url)
            if "/video/" not in final_url:
                raise RuntimeError("FINAL_VERIFIED: final page is not a video page")
            emit("stage", state="FINAL_VERIFIED", title=final.get("title") or label, url=final_url)
            emit("completed", title=final.get("title") or label, url=final.get("url") or href, played=action == "play")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--action", default="open", choices=("open", "play"))
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    try: await asyncio.wait_for(run(args.url, args.site, args.query, args.action), timeout=args.timeout)
    except Exception as exc:
        emit("failed", error=str(exc)); raise SystemExit(1)


if __name__ == "__main__": asyncio.run(main())
