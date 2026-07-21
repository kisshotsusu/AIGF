import asyncio
import aiohttp
import yaml
from pathlib import Path
from modules.live.ai_live_assistant.bilibili import BilibiliLive

async def test():
    cfg = yaml.safe_load(Path('config.yaml').open('r', encoding='utf-8'))
    room_id = cfg['app']['room_id']
    print(f'Testing connection to room {room_id}...')
    
    async with aiohttp.ClientSession() as session:
        bili = BilibiliLive(session, room_id)
        real_id, token, ws_url = await bili._room_info()
        print(f'Connected to room {real_id}')
        print(f'WebSocket URL: {ws_url}')
        
        # 测试获取历史消息
        print('\nTesting history events...')
        count = 0
        async for event in bili.history_events(interval=1.0):
            cmd = event.get('cmd', '')
            if cmd == 'DANMU_MSG':
                info = event.get('info', [])
                if len(info) > 2:
                    user_info = info[2] if isinstance(info[2], (list, tuple)) else []
                    user = str(user_info[1]).strip() if len(user_info) > 1 else ""
                    text = str(info[1])
                    print(f'历史弹幕: {user}: {text}')
                    count += 1
            if count >= 5:  # 只测试5条
                break
        
        print('\nConnection test completed!')

if __name__ == '__main__':
    asyncio.run(test())