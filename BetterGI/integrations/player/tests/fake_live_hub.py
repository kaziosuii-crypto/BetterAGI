#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的假 BetterGI Live Hub：只广播，不执行任何操作。

字段形状对齐真实 ``LiveServerMessage``（camelCase、WhenWritingNull）：
frame 带 capture/runtime、不带 uiCategory；action 回显在 ``block.chunks`` 里；
另有显式 guidance、commentary、action_state 与 live.stopped。
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import WSMsgType, web

PORT = 30999
# 记录客户端发来的应用层消息；只读观察者应始终为空。
RECEIVED: list[str] = []

# 1x1 的 FFD8FFD9 不是合法 JPEG，真实模型会拒绝；优先读取生成好的小图。
_FRAME_FILE = Path(__file__).with_name("_smoke_frame.b64")
if _FRAME_FILE.exists():
    JPEG = _FRAME_FILE.read_text(encoding="ascii").strip()
else:
    JPEG = base64.b64encode(b"\xff\xd8\xff\xd9").decode("ascii")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hello_payload(session_id: str = "smoke") -> dict:
    return {
        "type": "hello",
        "sessionId": session_id,
        "fps": 5,
        "width": 640,
        "height": 360,
        "actionIntervalMs": 33,
        "chunksPerAction": 6,
    }


def frame_payload(seq: int, guidance: str | None = None) -> dict:
    payload = {
        "type": "frame",
        "seq": seq,
        "timestampUtc": _timestamp(),
        "jpeg": JPEG,
        "width": 640,
        "height": 360,
        "uiCategory": "Unknown",
        "isInTalkUi": None,
        "capture": {"x": 0, "y": 0, "width": 640, "height": 360, "dpiScale": 1.0},
        "runtime": {
            "isAlive": True,
            "isForeground": True,
            "isMinimized": False,
            "requiresForeground": False,
        },
    }
    if guidance is not None:
        payload["guidance"] = guidance
    return payload


def action_state_payload() -> dict:
    return {
        "type": "action_state",
        "keys": ["W"],
        "queueDepth": 0,
        "starved": False,
        "applied": 6,
        "dropped": 0,
    }


def action_payload(seq: int) -> dict:
    return {
        "type": "action",
        "seq": seq,
        "timestampUtc": _timestamp(),
        "text": "向前走",
        "block": {
            "seq": seq,
            "thought": "向前走",
            "chunks": [
                {"keys": ["W"], "dx": 4, "dy": -1, "scroll": 0},
                {"keys": ["W"], "dx": 0, "dy": 0, "scroll": 0},
            ],
            "absolute": None,
        },
    }


def commentary_payload() -> dict:
    return {
        "type": "commentary",
        "kind": "reaction",
        "text": "起风了",
        "emotion": "calm",
        "frameSeq": 4,
        "timestampUtc": _timestamp(),
    }


def guidance_payload(text: str, source: str = "director") -> dict:
    return {
        "type": "guidance",
        "text": text,
        "source": source,
        "timestampUtc": _timestamp(),
    }


async def live(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)

    async def reader() -> None:
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                RECEIVED.append(message.data)

    reader_task = asyncio.create_task(reader())
    try:
        await ws.send_json(hello_payload())
        for seq in range(1, 16):
            inline = "往左看看" if seq == 3 else None
            await ws.send_json(frame_payload(seq, guidance=inline))
            await ws.send_json(action_state_payload())
            if seq == 2:
                await ws.send_json(action_payload(seq))
            if seq == 4:
                await ws.send_json(commentary_payload())
            if seq == 5:
                await ws.send_json(guidance_payload("坡上有动静"))
            await asyncio.sleep(0.4)
        await ws.send_json({"type": "live.stopped", "sessionId": "smoke"})
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
    await ws.close()
    return ws


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ws/v1/agent/live", live)
    return app


def main(port: int = PORT) -> None:
    print("fake live hub on 127.0.0.1:{0}".format(port), flush=True)
    web.run_app(create_app(), host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
