"""BetterGI 实时解说接入：类型白名单、去重、情绪映射、有界队列与本地端到端。

端到端用例起一个真实的本地 WebSocket 服务（127.0.0.1 随机端口），要求 Bearer 头，
配合真实 EventBus 与发言管线、替身语音引擎，验证"解说直接播出、画面帧不进下游、
客户端不回写"三条边界。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List, Tuple

import pytest
from websockets.asyncio.server import serve

from src.agents.streamer.bettergi_live import TOKEN_ENV_VAR, BetterGILiveBridge, validate_live_url
from src.agents.streamer.config import BetterGILiveConfig
from src.agents.streamer.speech_dispatcher import SpeechDispatcher
from src.modules.events.event_bus import EventBus
from src.modules.events.names import CoreEvents
from src.modules.events.payloads.speech import StreamerSpeechPayload
from tests.agents.streamer.helpers import FakeBetterGIServer, make_streamer_agent, stub_llm

_TTS_ON = {"enabled": True, "max_queue": 5, "render_timeout_ms": 1000}


class _Engine:
    """TTS 引擎替身：只记录被要求播出的台词。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Any]] = []

    async def handle_speech(self, text: str, utterance_id: Any = None) -> None:
        self.calls.append((text, utterance_id))


async def _until(condition: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


def _bridge(
    queue_max: int = 20, url: str = "ws://127.0.0.1:30648/ws/v1/agent/live", reconnect_ms: int = 2000
) -> Tuple[BetterGILiveBridge, SpeechDispatcher, EventBus, _Engine, List[StreamerSpeechPayload]]:
    """构造桥接：真实 EventBus + 真实发言管线 + 替身语音引擎，捕获 streamer.speech。"""
    bus = EventBus(enable_stats=False)
    engine = _Engine()
    speech = SpeechDispatcher(event_bus=bus, subtitle_service=None, tts_engine=engine, speech_config=_TTS_ON)
    captured: List[StreamerSpeechPayload] = []

    async def _capture(event_name: str, payload: StreamerSpeechPayload, source: str = "") -> None:
        captured.append(payload)

    bus.on(CoreEvents.STREAMER_SPEECH, _capture, model_class=StreamerSpeechPayload)
    config = BetterGILiveConfig(enabled=True, url=url, queue_max=queue_max, reconnect_ms=reconnect_ms)
    return BetterGILiveBridge(config, speech), speech, bus, engine, captured


def _commentary(seq: int, text: str, emotion: Any = None, ts: str = "2026-10-27T05:00:00.0000000+00:00") -> str:
    return json.dumps(
        {"type": "commentary", "seq": seq, "timestampUtc": ts, "kind": "reaction", "text": text, "emotion": emotion}
    )


# ===== 地址与凭证 =====


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.1:30648/ws/v1/agent/live",
        "ws://localhost:30648/ws/v1/agent/live",
        "wss://[::1]:30648/ws/v1/agent/live",
    ],
)
def test_validate_live_url_accepts_loopback(url: str) -> None:
    validate_live_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:30648/ws/v1/agent/live",
        "ws://192.168.1.10:30648/ws/v1/agent/live",
        "ws://user:pass@127.0.0.1:30648/ws/v1/agent/live",
        "ws://127.0.0.1:30648/ws/v1/agent/live?token=secret",
        "ws://127.0.0.1:30648/ws/v1/agent/live#frag",
        "ws://example.com/ws/v1/agent/live",
    ],
)
def test_validate_live_url_rejects_unsafe_addresses(url: str) -> None:
    with pytest.raises(ValueError):
        validate_live_url(url)


async def test_start_without_token_does_not_connect(monkeypatch: pytest.MonkeyPatch, loguru_capture) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    bridge, speech, bus, _engine, _captured = _bridge()
    await speech.start()
    try:
        await bridge.start()
        assert bridge._reader is None and bridge._consumer is None  # 未启动：不开网络任务
        assert any("BETTERGI_LIVE_TOKEN" in record["message"] for record in loguru_capture.records)
    finally:
        await bridge.stop()
        await speech.stop()
        await bus.cleanup()


# ===== 类型白名单与字段校验 =====


def test_only_commentary_type_is_spoken() -> None:
    bridge, speech, bus, _engine, captured = _bridge()
    bridge._accept(json.dumps({"type": "hello", "sessionId": "s1", "fps": 10, "width": 1280, "height": 720}))
    bridge._accept(json.dumps({"type": "frame", "seq": 7, "jpeg": "A" * 500_000, "width": 1280, "height": 720}))
    bridge._accept(json.dumps({"type": "guidance", "seq": 8, "text": "去左边的门"}))
    bridge._accept(json.dumps({"type": "action_state", "keys": ["W"]}))
    bridge._accept(json.dumps({"type": "error", "code": "X", "message": "bad"}))
    bridge._accept(json.dumps({"type": "pong"}))
    bridge._accept("not-json")
    bridge._accept({"type": "commentary"})
    bridge._accept(_commentary(9, "  "))  # 空白台词
    bridge._accept(_commentary(10, "太长了" * 2000))  # 超长台词
    bridge._accept(json.dumps({"type": "commentary", "seq": "9", "timestampUtc": "t", "text": "坏序号"}))
    bridge._accept(
        json.dumps(
            {
                "type": "commentary",
                "seq": 12,
                "timestampUtc": "2026-10-27T05:00:01.0000000+00:00",
                "kind": "plan",
                "text": "内部思考不该朗读",
                "meta": {"internal": True},
            }
        )
    )
    assert bridge._queue.empty()

    bridge._accept(_commentary(11, "这一段先别冲"))
    assert bridge._queue.qsize() == 1
    assert bridge._queue.get_nowait()["speech"] == "这一段先别冲"


def test_commentary_dedupes_within_session_and_resets_on_new_session() -> None:
    bridge, _speech, _bus, _engine, _captured = _bridge()
    bridge._accept(json.dumps({"type": "hello", "sessionId": "session-a"}))
    bridge._accept(_commentary(1, "第一句"))
    bridge._accept(_commentary(1, "第一句"))  # 同一 (会话, 序号, 时间戳) 重复
    assert bridge._queue.qsize() == 1

    bridge._accept(json.dumps({"type": "live.stopped", "sessionId": "session-a"}))
    bridge._accept(json.dumps({"type": "hello", "sessionId": "session-b"}))
    bridge._accept(_commentary(1, "第一句"))
    assert bridge._queue.qsize() == 2


def test_commentary_emotion_is_coerced() -> None:
    bridge, _speech, _bus, _engine, _captured = _bridge()
    for seq, raw in enumerate(["calm", "worried", "banana", "excited"], start=1):
        bridge._accept(_commentary(seq, f"第{seq}句", raw))
    bridge._accept(_commentary(5, "没有情绪"))
    names = [bridge._queue.get_nowait()["emotion"]["name"] for _ in range(5)]
    assert names == ["neutral", "scared", "neutral", "excited", "neutral"]


def test_queue_is_bounded_and_drops_oldest() -> None:
    bridge, _speech, _bus, _engine, _captured = _bridge(queue_max=2)
    bridge._accept(_commentary(1, "第一句"))
    bridge._accept(_commentary(2, "第二句"))
    bridge._accept(_commentary(3, "第三句"))
    assert bridge._queue.qsize() == 2
    assert bridge._queue.get_nowait()["speech"] == "第二句"  # 满队丢最旧，新解说仍能进来


# ===== 本地 WebSocket 端到端 =====


async def test_bridge_end_to_end_speaks_commentary_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    async with FakeBetterGIServer() as server:
        bridge, speech, bus, engine, captured = _bridge(url=server.url)
        await speech.start()
        try:
            await bridge.start()
            await _until(lambda: bool(engine.calls) and bool(captured))
        finally:
            await bridge.stop()
            await speech.stop()
            await bus.cleanup()

    assert server.headers[0] == "Bearer test-token"  # 凭证只出现在鉴权头
    assert server.received == []  # 只读一方向：客户端不发任何应用消息
    assert [call[0] for call in engine.calls] == ["先贴着左边走"]
    assert captured[0].text == "先贴着左边走" and captured[0].emotion == "neutral"
    assert captured[0].utterance_id.startswith("utt_")


async def test_bridge_reconnects_after_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    connections = 0

    async def handler(websocket: Any) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"type": "hello", "sessionId": "session-1"}))
        await websocket.send(_commentary(connections, f"第{connections}段"))
        if connections == 1:
            await websocket.close()  # 第一次连接主动断开，逼出重连
        else:
            await asyncio.sleep(0.5)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        bridge, speech, bus, engine, _captured = _bridge(url=f"ws://127.0.0.1:{port}/x", reconnect_ms=100)
        await speech.start()
        try:
            await bridge.start()
            await _until(lambda: len(engine.calls) >= 2)
        finally:
            await bridge.stop()
            await speech.stop()
            await bus.cleanup()

    assert [call[0] for call in engine.calls] == ["第1段", "第2段"]


async def test_stop_cancels_receive_and_clears_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    async with FakeBetterGIServer() as server:
        bridge, speech, bus, _engine, _captured = _bridge(url=server.url, reconnect_ms=100)
        await speech.start()
        await bridge.start()
        await _until(lambda: server.connections == 1)
        await bridge.stop()
        assert bridge._reader is None and bridge._consumer is None and bridge._queue.empty()
        await speech.stop()
        await bus.cleanup()


# ===== 主播接线 =====


def test_streamer_constructs_bridge_only_when_enabled() -> None:
    assert make_streamer_agent()._bettergi_live is None
    agent = make_streamer_agent({"bettergi_live": {"enabled": True}})
    assert agent._bettergi_live is not None


async def test_streamer_speaks_bettergi_commentary_without_calling_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端到端：主播启用接入后，解说经同一条发言管线播出，语言模型不被调用。"""
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")
    async with FakeBetterGIServer() as server:
        bus = EventBus(enable_stats=False)
        engine = _Engine()
        llm = stub_llm()
        captured: List[StreamerSpeechPayload] = []

        async def _capture(event_name: str, payload: StreamerSpeechPayload, source: str = "") -> None:
            captured.append(payload)

        bus.on(CoreEvents.STREAMER_SPEECH, _capture, model_class=StreamerSpeechPayload)
        agent = make_streamer_agent(
            {"bettergi_live": {"enabled": True, "url": server.url, "reconnect_ms": 100}},
            event_bus=bus,
            tts_engine=engine,
            speech_config=dict(_TTS_ON),
            llm=llm,
        )
        await agent.start()
        try:
            await _until(lambda: bool(engine.calls) and bool(captured))
        finally:
            await agent.stop()
            await bus.cleanup()

    assert [call[0] for call in engine.calls] == ["先贴着左边走"]
    assert captured[0].emotion == "neutral"
    assert llm.generate.await_count == 0  # 解说已是成品台词，不再进语言模型
