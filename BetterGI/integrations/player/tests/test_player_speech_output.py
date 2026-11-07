#!/usr/bin/env python3
"""Speech output capability binding, provenance, and ambiguous delivery tests."""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_speech_output import PlayerSpeechOutput  # noqa: E402


SECRET = "s" * 40
BINDING = {"session_id": "session", "instance": "instance"}


def output(read, send, **kwargs):
    """Create an explicitly configured capability without network or game access."""
    return PlayerSpeechOutput("endpoint", SECRET, read_endpoint=read, send_speech=send, **kwargs)


def test_environment_factory_is_opt_in_and_rejects_partial_config():
    def read(*_):
        return BINDING
    def send(*_a, **_k):
        return None
    assert PlayerSpeechOutput.from_environment(read_endpoint=read, send_speech=send, environ={}) is None
    with pytest.raises(ValueError):
        PlayerSpeechOutput.from_environment(read_endpoint=read, send_speech=send,
                                            environ={"BETTERGI_PLAYER_SPEECH_ENDPOINT": "endpoint"})
    adapter = PlayerSpeechOutput.from_environment(
        read_endpoint=read, send_speech=send,
        environ={"BETTERGI_PLAYER_SPEECH_ENDPOINT": "endpoint", "BETTERGI_PLAYER_SPEECH_SECRET": SECRET})
    assert asyncio.run(adapter.prepare()) == BINDING


def test_output_preserves_exact_binding_text_and_provenance():
    calls = []
    def read(endpoint, secret):
        calls.append(("prepare", endpoint, secret))
        return BINDING.copy()
    def send(endpoint, secret, event, **kwargs):
        calls.append(("send", event, kwargs))
        return {**event, "t": 0, "dur": 500}
    adapter = output(read, send)
    event = {"type": "speech", "source": "persistent_player", "text": "  原样\n文本  ",
             "generated_at_ms": 1000, "thought_id": "thought-1", "intent_id": "intent-1"}
    async def scenario():
        binding = await adapter.prepare()
        return await adapter.deliver(binding, event)
    assert asyncio.run(scenario()) == {**event, "t": 0, "dur": 500}
    assert calls[0][0] == "prepare"
    assert calls[1][1] == event
    assert calls[1][2]["expected_session_id"] == "session"
    assert calls[1][2]["expected_instance"] == "instance"


def test_unknown_timeout_cannot_resubmit_same_thought():
    calls = []
    def send(*_args, **_kwargs):
        calls.append(1)
        raise TimeoutError("unknown")
    adapter = output(lambda *_: BINDING, send)
    async def scenario():
        binding = await adapter.prepare()
        event = {"thought_id": "thought-1"}
        with pytest.raises(TimeoutError):
            await adapter.deliver(binding, event)
        with pytest.raises(ValueError, match="do not retry"):
            await adapter.deliver(binding, event)
    asyncio.run(scenario())
    assert calls == [1]


def test_cancelled_worker_cannot_be_retried():
    started, release = threading.Event(), threading.Event()
    calls = []
    def send(*_args, **_kwargs):
        calls.append(1)
        started.set()
        release.wait(2)
        return {"type": "speech"}
    adapter = output(lambda *_: BINDING, send)
    async def scenario():
        binding = await adapter.prepare()
        task = asyncio.create_task(adapter.deliver(binding, {"thought_id": "thought-1"}))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ValueError, match="do not retry"):
            await adapter.deliver(binding, {"thought_id": "thought-1"})
        release.set()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert calls == [1]


def test_binding_thought_and_timeout_fail_closed():
    adapter = output(lambda *_: {}, lambda *_a, **_k: None)
    with pytest.raises(ValueError, match="incomplete"):
        asyncio.run(adapter.prepare())
    with pytest.raises(ValueError, match="binding"):
        asyncio.run(adapter.deliver({}, {"thought_id": "t"}))
    with pytest.raises(ValueError, match="thought_id"):
        asyncio.run(adapter.deliver(BINDING, {}))
    with pytest.raises(ValueError, match="not prepared"):
        asyncio.run(adapter.deliver(BINDING, {"thought_id": "t"}))
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            output(lambda *_: BINDING, lambda *_a, **_k: None, timeout=bad)
