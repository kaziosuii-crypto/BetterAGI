#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地联调用的假 BetterGI Live Hub + 假 OpenAI 服务。

只用于验证流水线协议，不产生真实游戏输入。启动后：

* ``ws://127.0.0.1:<port>/ws/v1/agent/live`` 持续推送测试画面；
* 接收 guidance / commentary / actions，并把解说与动作回显给所有客户端；
* ``http://127.0.0.1:<llm-port>/v1/chat/completions`` 按轮次返回
  导演所需的 JSON，让 ``live_director.py`` 不依赖外部大模型也能跑通。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Set

from aiohttp import WSMsgType, web

CLIENTS: Set[web.WebSocketResponse] = set()
SEQ = {"frame": 0, "commentary": 0}
SCRIPT = [
    {
        "thought": "先沿着路走，看看前面有没有东西。",
        "guidance": "沿路向前",
        "commentary": [
            {"kind": "plan", "text": "我们先沿着这条路走一段。", "emotion": "calm"}
        ],
        "memory_patch": {},
        "tool_calls": [],
    },
    {
        "thought": "左边草丛里有反光，可能是宝箱。",
        "guidance": "向左前方靠近",
        "commentary": [
            {
                "kind": "reaction",
                "text": "哎？那边好像有个宝箱！",
                "emotion": "surprised",
            }
        ],
        "memory_patch": {},
        "tool_calls": [],
    },
    {
        "thought": "拿到了，继续往山上走。",
        "guidance": "继续前进",
        "commentary": [
            {"kind": "result", "text": "拿到了，今天运气不错。", "emotion": "happy"}
        ],
        "memory_patch": {},
        "tool_calls": [],
    },
]
LLM_CALLS = {"count": 0}


def iso_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def broadcast(payload: dict[str, Any]) -> None:
    dead = []
    for client in CLIENTS:
        try:
            asyncio.create_task(client.send_json(payload))
        except Exception:  # noqa: BLE001
            dead.append(client)
    for client in dead:
        CLIENTS.discard(client)


async def frame_loop(frame_bytes: bytes, fps: float) -> None:
    """按固定帧率广播测试画面和动作状态。"""
    interval = 1.0 / max(0.5, fps)
    while True:
        SEQ["frame"] += 1
        broadcast(
            {
                "type": "frame",
                "seq": SEQ["frame"],
                "timestampUtc": iso_now(),
                "jpeg": base64.b64encode(frame_bytes).decode("ascii"),
                "width": 1280,
                "height": 720,
            }
        )
        broadcast(
            {
                "type": "action_state",
                "keys": [],
                "queueDepth": 0,
                "starved": False,
                "applied": 0,
                "dropped": 0,
            }
        )
        await asyncio.sleep(interval)


async def live_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    CLIENTS.add(ws)
    await ws.send_json(
        {
            "type": "hello",
            "sessionId": "smoke",
            "fps": 10,
            "width": 1280,
            "height": 720,
            "actionIntervalMs": 33,
            "chunksPerAction": 6,
        }
    )
    try:
        async for message in ws:
            if message.type is not WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except ValueError:
                continue
            kind = str(payload.get("type") or "")
            if kind == "guidance":
                broadcast(
                    {
                        "type": "guidance",
                        "text": payload.get("text", ""),
                        "source": payload.get("source") or "dsh",
                        "timestampUtc": iso_now(),
                    }
                )
            elif kind == "commentary":
                SEQ["commentary"] += 1
                broadcast(
                    {
                        "type": "commentary",
                        "seq": SEQ["commentary"],
                        "timestampUtc": iso_now(),
                        "kind": payload.get("kind") or "reaction",
                        "text": payload.get("text", ""),
                        "emotion": payload.get("emotion"),
                        "frameSeq": payload.get("frameSeq"),
                        "meta": payload.get("meta"),
                    }
                )
            elif kind == "actions":
                block = payload.get("block") or {
                    "seq": payload.get("seq"),
                    "chunks": payload.get("chunks") or [],
                    "absolute": payload.get("absolute"),
                    "thought": payload.get("thought"),
                }
                # 回显动作块，模拟真实 Hub 让录制器拿到鼠标位移与思考。
                broadcast(
                    {
                        "type": "action",
                        "seq": block.get("seq"),
                        "timestampUtc": iso_now(),
                        "block": block,
                        "text": block.get("thought"),
                    }
                )
    finally:
        CLIENTS.discard(ws)
    return ws


async def live_start(request: web.Request) -> web.Response:
    return web.json_response({"running": True, "sessionId": "smoke", "fps": 10})


async def live_stop(request: web.Request) -> web.Response:
    return web.json_response({"running": False})


async def live_status(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "running": True,
            "sessionId": "smoke",
            "fps": 10,
            "frameSeq": SEQ["frame"],
            "clients": len(CLIENTS),
            "pressedKeys": [],
            "actionQueueDepth": 0,
            "starved": False,
            "appliedActions": 0,
            "droppedActions": 0,
            "timelineSeq": SEQ["commentary"],
        }
    )


async def memory(request: web.Request) -> web.Response:
    """导演的会话记忆接口；测试里保持空对象。"""
    return web.json_response({})


async def checkpoints(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def llm_chat(request: web.Request) -> web.Response:
    """假 OpenAI：按轮次循环返回导演 JSON。"""
    await request.json()
    index = LLM_CALLS["count"] % len(SCRIPT)
    LLM_CALLS["count"] += 1
    content = json.dumps(SCRIPT[index], ensure_ascii=False)
    return web.json_response(
        {
            "id": f"fake-{LLM_CALLS['count']}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fake-director",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )


def build_live_app(frame_bytes: bytes, fps: float) -> web.Application:
    app = web.Application()
    app.router.add_get("/ws/v1/agent/live", live_ws)
    app.router.add_post("/api/v1/agent/live/start", live_start)
    app.router.add_post("/api/v1/agent/live/stop", live_stop)
    app.router.add_get("/api/v1/agent/live/status", live_status)
    app.router.add_get("/api/v1/agent/sessions/{sid}/memory", memory)
    app.router.add_post("/api/v1/agent/sessions/{sid}/memory", memory)
    app.router.add_post("/api/v1/agent/sessions/{sid}/checkpoints", checkpoints)
    app["frame_task"] = None
    return app


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=30648)
    parser.add_argument("--llm-port", type=int, default=18080)
    parser.add_argument("--frame", required=True, help="1280x720 JPEG 测试图")
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args()

    frame_bytes = Path(args.frame).read_bytes()

    live_app = build_live_app(frame_bytes, args.fps)
    live_runner = web.AppRunner(live_app)
    await live_runner.setup()
    await web.TCPSite(live_runner, "127.0.0.1", args.port).start()

    llm_app = web.Application()
    llm_app.router.add_post("/v1/chat/completions", llm_chat)
    llm_runner = web.AppRunner(llm_app)
    await llm_runner.setup()
    await web.TCPSite(llm_runner, "127.0.0.1", args.llm_port).start()

    frame_task = asyncio.create_task(frame_loop(frame_bytes, args.fps))
    print(
        f"fake hub ws://127.0.0.1:{args.port}/ws/v1/agent/live "
        f"llm http://127.0.0.1:{args.llm_port}/v1",
        flush=True,
    )
    try:
        await asyncio.Event().wait()
    finally:
        frame_task.cancel()
        await live_runner.cleanup()
        await llm_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
