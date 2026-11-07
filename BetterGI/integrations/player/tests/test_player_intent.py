#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MotorIntent / MotorFeedback 状态机测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_intent as intent_module  # noqa: E402


def test_intent_normalization_and_limits() -> None:
    intent = intent_module.normalize_intent(
        {
            "goal": "  跟 安柏 去 骑士团  ",
            "reason": "继续主线",
            "constraints": ["不要离开任务区", "剧情点停下"],
            "motor_mode": "navigate",
            "deadline_ms": 999999,
        },
        now_ms=1_700_000_000_000,
    )
    assert intent["goal"] == "跟 安柏 去 骑士团"
    assert intent["deadline_ms"] == 60000
    assert intent["version"] == 2
    assert intent["intent_id"].startswith("i-")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"goal": ""},
        {"goal": "x", "intent_id": "bad/id"},
        {"goal": "x", "version": True},
        {"goal": "x", "motor_mode": "unsupported"},
        {"goal": "x", "motor_mode": ""},
    ],
)
def test_invalid_intent_is_rejected(payload) -> None:
    with pytest.raises(intent_module.IntentError):
        intent_module.normalize_intent(payload)


def test_tracker_single_primary_and_replacement() -> None:
    tracker = intent_module.IntentTracker()
    first = tracker.create({"intent_id": "i-1", "goal": "A"})
    assert first["status"] == "created"
    second = tracker.create({"intent_id": "i-2", "goal": "B"})
    assert second["status"] == "created"
    assert tracker.history[-1]["intent_id"] == "i-1"
    assert tracker.history[-1]["status"] == "cancelled"
    assert tracker.current["intent_id"] == "i-2"


def test_tracker_feedback_done_and_replan_threshold() -> None:
    tracker = intent_module.IntentTracker(max_failures=3)
    tracker.create({"intent_id": "i-1", "goal": "A"})
    tracker.mark_sent("i-1")
    assert tracker.apply_feedback(
        {"intent_id": "i-1", "status": "progress", "reason": "walking"}
    )
    assert tracker.current["status"] == "running"
    for _ in range(3):
        tracker.apply_feedback({"intent_id": "i-1", "status": "blocked", "reason": "wall"})
    assert tracker.needs_replan is True
    tracker.create({"intent_id": "i-2", "goal": "B"})
    assert tracker.needs_replan is False
    tracker.apply_feedback({"intent_id": "i-2", "status": "done"})
    assert tracker.current["status"] == "awaiting_observation"
    assert tracker.current["game_response_confirmed"] is None
    assert tracker.needs_replan is True
    assert tracker.consecutive_failures == 3


def test_tracker_ignores_foreign_feedback_and_expires() -> None:
    tracker = intent_module.IntentTracker(max_failures=3)
    tracker.create({"intent_id": "i-1", "goal": "A"}, now_ms=1000)
    assert tracker.apply_feedback({"intent_id": "other", "status": "done"}) is False
    assert tracker.mark_sent("other") is False
    assert tracker.expire(now_ms=2000) is False
    assert tracker.expire(now_ms=1000 + 15001) is True
    assert tracker.current["status"] == "expired"


def test_feedback_normalization() -> None:
    feedback = intent_module.normalize_feedback(
        {"intent_id": "i-1", "status": "blocked", "reason": "path_blocked"}
    )
    assert feedback["status"] == "blocked"
    with pytest.raises(intent_module.IntentError):
        intent_module.normalize_feedback({"intent_id": "i-1", "status": "running"})


def test_terminal_feedback_cannot_resurrect_and_zero_clock_expires() -> None:
    tracker = intent_module.IntentTracker()
    tracker.create({"intent_id": "i-zero", "goal": "A", "deadline_ms": 1000}, now_ms=0)
    assert tracker.expire(now_ms=1000)
    assert tracker.needs_replan
    assert not tracker.apply_feedback({"intent_id": "i-zero", "status": "progress"})
    assert not tracker.mark_sent("i-zero")
    assert tracker.current["status"] == "expired"


def test_cancel_requires_bridge_acceptance(monkeypatch) -> None:
    import asyncio

    class Response:
        status = 200
        data = {"accepted": False}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self.data

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            assert url.endswith("/intent") and kwargs["json"] == {"cancel": True}
            return Response()

    monkeypatch.setattr(intent_module.aiohttp, "ClientSession", Session)
    publisher = intent_module.IntentPublisher("http://127.0.0.1:8000")
    assert asyncio.run(publisher.cancel()) is False
    Response.data = {"accepted": True, "cancelled": True, "remoteStopSent": False, "inputReleaseSent": True}
    assert asyncio.run(publisher.cancel()) is False
    assert publisher.last_cancel_result["remoteStopSent"] is False
    Response.data = {"accepted": True, "cancelled": True}
    assert asyncio.run(publisher.cancel()) is False
    Response.data = {"accepted": True, "cancelled": True, "remoteStopSent": True, "inputReleaseSent": True}
    assert asyncio.run(publisher.cancel()) is True


def test_tracker_requires_exact_generation_and_group_on_reused_id() -> None:
    tracker = intent_module.IntentTracker()
    tracker.create({"intent_id": "same", "goal": "old", "motorGeneration": 1, "actionGroupId": "g"})
    tracker.create({"intent_id": "same", "goal": "new", "motorGeneration": 2, "actionGroupId": "g"})
    for row in (
        {"intent_id": "same", "motorGeneration": 1, "actionGroupId": "g", "status": "failed"},
        {"intent_id": "same", "motorGeneration": 1, "actionGroupId": "g", "status": "cancelled"},
        {"intent_id": "same", "motorGeneration": 2, "status": "failed"},
        {"intent_id": "same", "status": "blocked"},
    ):
        assert tracker.apply_feedback(row) is False
    assert tracker.current["status"] == "created"
    assert tracker.consecutive_failures == 0
    assert tracker.apply_feedback({"intent_id": "same", "motorGeneration": 2, "actionGroupId": "g", "status": "cancelled", "reason": "player_cancel"})
    assert tracker.current["status"] == "cancelled"
    assert tracker.consecutive_failures == 0


def test_legacy_reused_id_fails_closed() -> None:
    tracker = intent_module.IntentTracker()
    tracker.create({"intent_id": "same", "goal": "old"})
    tracker.create({"intent_id": "same", "goal": "new"})
    assert not tracker.apply_feedback({"intent_id": "same", "status": "failed"})
    assert tracker.current["status"] == "created"


@pytest.mark.parametrize("field,value", [("motorGeneration", True), ("motorGeneration", -1), ("motorGeneration", "1"), ("actionGroupId", " "), ("actionGroupId", 1)])
def test_invalid_attribution_rejected(field, value) -> None:
    with pytest.raises(intent_module.IntentError):
        intent_module.normalize_feedback({"intent_id": "i", "status": "progress", field: value})


def test_publish_binds_bridge_generation_and_preserves_group(monkeypatch) -> None:
    import asyncio

    class Response:
        status = 200
        data = {"accepted": True, "intent_id": "same", "motorGeneration": 2, "actionGroupId": "g"}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return self.data

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs):
            assert kwargs["json"]["intent"]["actionGroupId"] == "g"
            return Response()

    monkeypatch.setattr(intent_module.aiohttp, "ClientSession", Session)
    publisher = intent_module.IntentPublisher("http://127.0.0.1:8000")
    intent = {"intent_id": "same", "goal": "go", "actionGroupId": "g"}
    assert asyncio.run(publisher.publish(intent))
    assert intent["motorGeneration"] == 2
    assert intent_module.normalize_intent(intent)["actionGroupId"] == "g"
    for bad in ({"accepted": True, "intent_id": "same"},
                {"accepted": True, "intent_id": "same", "motorGeneration": True, "actionGroupId": "g"},
                {"accepted": True, "intent_id": "other", "motorGeneration": 2, "actionGroupId": "g"},
                {"accepted": True, "intent_id": "same", "motorGeneration": 2}):
        Response.data = bad
        assert not asyncio.run(publisher.publish(dict(intent)))


def test_mock_bus_cancel_ack_does_not_prove_input_release(monkeypatch) -> None:
    """Motor 确认取消不能证明 BetterGI 输入已释放。"""
    import asyncio
    import json

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lumine"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lumine" / "tests"))
    import lumine_bridge as bridge
    from test_lumine_bridge import _bind_active_v2

    class Socket:
        closed = False
        def __init__(self): self.sent = []
        async def send_str(self, value): self.sent.append(value)

    instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
    instance._lumine_ws = Socket()
    instance._bettergi_ws = Socket()

    class Response:
        status = 200
        def __init__(self, request): self.request = request
        async def __aenter__(self):
            result = await instance.set_intent(self.request)
            if result is None:
                self.status = 503
                self.data = {"accepted": False}
            else:
                self.data = {"accepted": True, "intent_id": result["intent_id"],
                             "motorGeneration": result["motorGeneration"],
                             "actionGroupId": result["actionGroupId"]}
            return self
        async def __aexit__(self, *args): pass
        async def json(self): return self.data

    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, **kwargs): return Response(kwargs["json"])

    publisher = intent_module.IntentPublisher("http://127.0.0.1:8000")
    tracker = intent_module.IntentTracker()

    async def scenario():
        await _bind_active_v2(instance)
        monkeypatch.setattr(intent_module.aiohttp, "ClientSession", Session)
        old = {"intent_id": "same", "goal": "old", "actionGroupId": "g"}
        assert await publisher.publish(old)
        tracker.create(old)
        await instance.clear_intent("cancelled_by_player")
        new = {"intent_id": "same", "goal": "new", "actionGroupId": "g"}
        assert not await publisher.publish(new)
        await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": 2}),
                                               source_ws=instance._lumine_ws)
        assert instance._pending_cancel_generation is None
        assert instance.intent_snapshot()["inputReleaseUnconfirmed"] is True
        assert not await publisher.publish(new)
        for feedback in instance.intent_feedback:
            tracker.apply_feedback(feedback)
        assert tracker.current["motorGeneration"] == 1
        assert tracker.current["status"] == "cancelled"
        assert tracker.consecutive_failures == 0
        await instance._handle_lumine_message(json.dumps({"type": "error", "intentId": "same", "motorGeneration": 1, "actionGroupId": "g", "code": "stale"}))
        assert instance.latest_intent is None

    asyncio.run(scenario())
