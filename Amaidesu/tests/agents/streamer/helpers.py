"""主播 Agent 接线测试共用构造器。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from websockets.asyncio.server import serve

from src.agents.streamer.config import StreamerConfig
from src.agents.streamer.streamer_agent import StreamerAgent
from src.modules.llm.payload import Response


def streamer_config(sections: Optional[Dict[str, Any]] = None) -> StreamerConfig:
    """测试缺省：关闭主动发言与词过滤，攒批窗口缩短；sections 按子段浅合并覆盖。"""
    merged: Dict[str, Any] = {
        "proactive": {"enabled": False},
        "word_filter": {"enabled": False},
        "batch": {"batch_window_ms": 100, "tick_interval_ms": 50},
    }
    for key, value in (sections or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return StreamerConfig.from_dict(merged)


def stub_llm() -> MagicMock:
    """不应被真正调用的 LLM：调用即得到失败响应，而不是悬挂。"""
    llm = MagicMock()
    llm.generate = AsyncMock(return_value=Response(success=False, error="not used"))
    return llm


def stub_prompt() -> MagicMock:
    prompt = MagicMock()
    prompt.render = MagicMock(return_value="PROMPT")
    return prompt


def make_streamer_agent(
    sections: Optional[Dict[str, Any]] = None,
    *,
    llm: Any = None,
    prompt: Any = None,
    **kwargs: Any,
) -> StreamerAgent:
    """最小 StreamerAgent；其余构造参数（event_bus / tts_engine / chat_repo 等）原样透传。"""
    return StreamerAgent(
        config=streamer_config(sections),
        llm_manager=llm or stub_llm(),
        prompt_manager=prompt or stub_prompt(),
        **kwargs,
    )


class FakeBetterGIServer:
    """本地 BetterGI 实时会话替身（127.0.0.1 随机端口）。

    校验并记录 Bearer 头，推送固定消息序列（hello / frame / commentary /
    guidance），同时记录客户端发来的任何应用消息——用于断言"只读一方向"。
    """

    def __init__(self, *, hold_seconds: float = 0.5) -> None:
        self.headers: List[str] = []
        self.received: List[str] = []
        self.connections = 0
        self._hold_seconds = hold_seconds
        self._server: Any = None
        self._port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/ws/v1/agent/live"

    @staticmethod
    def commentary(seq: int, text: str, emotion: Any = None, ts: str = "2026-10-27T05:00:00.0000000+00:00") -> str:
        return json.dumps(
            {"type": "commentary", "seq": seq, "timestampUtc": ts, "kind": "reaction", "text": text, "emotion": emotion}
        )

    async def __aenter__(self) -> "FakeBetterGIServer":
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self._port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, websocket: Any) -> None:
        self.connections += 1
        self.headers.append(websocket.request.headers.get("Authorization", ""))
        collector = asyncio.create_task(self._collect(websocket))
        try:
            await websocket.send(json.dumps({"type": "hello", "sessionId": "session-1", "fps": 10}))
            await websocket.send(json.dumps({"type": "frame", "seq": 1, "jpeg": "A" * 200_000}))
            await websocket.send(self.commentary(1, "先贴着左边走", "calm"))
            await websocket.send(json.dumps({"type": "guidance", "seq": 2, "text": "不要冲"}))
            await asyncio.sleep(self._hold_seconds)
        finally:
            collector.cancel()

    async def _collect(self, websocket: Any) -> None:
        async for message in websocket:
            self.received.append(str(message))
