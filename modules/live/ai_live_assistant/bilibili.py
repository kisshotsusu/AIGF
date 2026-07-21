from __future__ import annotations

import asyncio
import brotli
import json
import logging
import random
import struct
import time
import zlib
from collections.abc import AsyncIterator
from typing import Any
from http.cookies import SimpleCookie

import aiohttp


HEADER = struct.Struct(">IHHII")
LOG = logging.getLogger("ai_live.bilibili")


def packet(operation: int, body: bytes = b"", version: int = 1) -> bytes:
    return HEADER.pack(16 + len(body), 16, version, operation, 1) + body


def unpack_packets(data: bytes) -> list[dict[str, Any]]:
    out = []
    offset = 0
    while offset + 16 <= len(data):
        length, header_len, version, operation, _ = HEADER.unpack_from(data, offset)
        if length < header_len or offset + length > len(data): break
        body = data[offset + header_len:offset + length]
        if version == 2: out.extend(unpack_packets(zlib.decompress(body)))
        elif version == 3: out.extend(unpack_packets(brotli.decompress(body)))
        elif operation == 5:
            try: out.append(json.loads(body.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError): pass
        offset += length
    return out


class BilibiliLive:
    def __init__(self, session: aiohttp.ClientSession, room_id: int, cookie: str = ""):
        self.session, self.room_id, self.cookie = session, room_id, cookie

    def _cookies(self) -> dict[str, str]:
        parsed = SimpleCookie()
        try:
            parsed.load(self.cookie)
        except Exception:
            return {}
        return {key: morsel.value for key, morsel in parsed.items()}

    def _headers(self) -> dict[str, str]:
        return {
            "Cookie": self.cookie,
            "Referer": f"https://live.bilibili.com/{self.room_id}",
            "Origin": "https://live.bilibili.com",
        }

    def _auth_payload(self, real_id: int, token: str) -> dict[str, Any]:
        cookies = self._cookies()
        try:
            uid = int(cookies.get("DedeUserID", "0"))
        except ValueError:
            uid = 0
        return {"uid": uid, "roomid": real_id, "protover": 3,
                "buvid": cookies.get("buvid3") or cookies.get("buvid4", ""),
                "platform": "web", "type": 2, "key": token}

    async def _room_info(self) -> tuple[int, str, str]:
        async with self.session.get("https://api.live.bilibili.com/room/v1/Room/room_init", params={"id": self.room_id}, headers=self._headers()) as r:
            data = await r.json()
        real_id = int(data["data"]["room_id"])
        async with self.session.get("https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo", params={"id": real_id, "type": 0}, headers={"Cookie": self.cookie}) as r:
            info = await r.json()
        # 新接口可能对匿名请求返回 -352；旧版直播接口仍提供同一握手令牌。
        if info.get("code") != 0:
            async with self.session.get(
                "https://api.live.bilibili.com/room/v1/Danmu/getConf",
                params={"room_id": real_id, "platform": "pc", "player": "web"},
            ) as r:
                info = await r.json()
            if info.get("code") != 0: raise RuntimeError(f"获取弹幕服务器失败: {info}")
            hosts = info["data"]["host_server_list"]
        else:
            hosts = info["data"]["host_list"]
        host = random.choice(hosts)
        return real_id, info["data"]["token"], f"wss://{host['host']}:{host['wss_port']}/sub"

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                real_id, token, ws_url = await self._room_info()
                async with self.session.ws_connect(ws_url, heartbeat=None) as ws:
                    auth = json.dumps(self._auth_payload(real_id, token)).encode()
                    await ws.send_bytes(packet(7, auth))
                    heartbeat = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                for event in unpack_packets(msg.data): yield event
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED): break
                    finally: heartbeat.cancel()
            except asyncio.CancelledError: raise
            except Exception:
                LOG.warning("B站直播事件连接失败，5 秒后重试", exc_info=True)
                await asyncio.sleep(5)

    async def history_events(self, interval: float = 2.5) -> AsyncIterator[dict[str, Any]]:
        """Poll recent danmaku as a fallback for silent/stalled WebSocket connections."""
        seen: set[str] = set()
        initialized = False
        while True:
            try:
                async with self.session.get(
                    "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory",
                    params={"roomid": self.room_id},
                    headers={"Cookie": self.cookie, "Referer": f"https://live.bilibili.com/{self.room_id}"},
                ) as r:
                    data = await r.json()
                rows = data.get("data", {}).get("room", []) if data.get("code") == 0 else []
                current_ids = {str(row.get("id_str") or f"{row.get('uid')}:{row.get('timeline')}:{row.get('text')}") for row in rows}
                if not initialized:
                    seen.update(current_ids)
                    initialized = True
                else:
                    for row in rows:
                        message_id = str(row.get("id_str") or f"{row.get('uid')}:{row.get('timeline')}:{row.get('text')}")
                        if message_id in seen: continue
                        seen.add(message_id)
                        yield {
                            "cmd": "DANMU_MSG",
                            "info": [[], str(row.get("text", "")), [row.get("uid", 0), str(row.get("nickname", "观众"))]],
                            "_source": "history",
                            "_message_id": message_id,
                        }
                    seen.intersection_update(current_ids)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("B站历史弹幕轮询失败", exc_info=True)
            await asyncio.sleep(interval)

    @staticmethod
    async def _heartbeat(ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await ws.send_bytes(packet(2, b"[object Object]"))
            await asyncio.sleep(30)

    async def send_danmaku(self, text: str) -> None:
        cookies = {}
        for part in self.cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1); cookies[k] = v
        csrf = cookies.get("bili_jct")
        if not csrf or not cookies.get("SESSDATA"): raise RuntimeError("发送弹幕需要 Cookie 中的 SESSDATA 和 bili_jct")
        form = {"bubble": "0", "msg": text, "color": "16777215", "mode": "1", "fontsize": "25", "rnd": str(int(time.time())), "roomid": str(self.room_id), "csrf": csrf, "csrf_token": csrf}
        async with self.session.post("https://api.live.bilibili.com/msg/send", data=form, headers={"Cookie": self.cookie, "Referer": f"https://live.bilibili.com/{self.room_id}"}) as r:
            result = await r.json(content_type=None)
        if result.get("code") != 0: raise RuntimeError(f"发送弹幕失败: {result}")
