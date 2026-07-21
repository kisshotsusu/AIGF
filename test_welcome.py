import asyncio
import aiohttp
import json
from modules.live.ai_live_assistant.bilibili import BilibiliLive

async def test_events():
    async with aiohttp.ClientSession() as session:
        bili = BilibiliLive(session, 21248701, '')
        events = []
        async for event in bili.events():
            events.append(event)
            if len(events) >= 20:
                break
        print(f"共收到 {len(events)} 个事件")
        for i, event in enumerate(events):
            cmd = event.get("cmd", "unknown")
            print(f"事件 {i+1}: {cmd}")
            if cmd in ["INTERACT_WORD", "INTERACT_WORD_V2", "ENTRY_EFFECT", "ENTRY_EFFECT_MUST_RECEIVE"]:
                print(f"  欢迎事件详情: {json.dumps(event, ensure_ascii=False, indent=2)}")

asyncio.run(test_events())