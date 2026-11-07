#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话感知与 dialogue_lock 测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_dialogue as dialogue  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_700_000_000_000

    def __call__(self):
        return self.now


def test_same_frame_observations_remain_distinct_across_conversations(tmp_path: Path) -> None:
    import asyncio
    import logging
    from player_agent import PersistentPlayerAgent
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    async def scenario(log):
        agent = object.__new__(PersistentPlayerAgent)
        agent.experience_store = PlayerExperience(log)
        agent._dialogue_persist_lock = asyncio.Lock()
        agent._pending_conversation_ends = []
        agent._session_generation = 0
        agent._closed = False
        agent.cognitive_transaction = None
        agent.thread_store = None
        agent.log = logging.getLogger(__name__)
        event = {"type": "dialogue_line_changed", "line": {"text": "跟我来", "source": {
            "frame_id": "same-frame", "timestamp_ms": 1700000000000}}}
        for conversation_id in ("first", "second"):
            entry = {"session_id": "player:session", "generation": 0, "conversation_id": conversation_id,
                     "inflight": 1, "closing": False, "anchor_seq": 0, "anchor_event_id": None,
                     "unconfirmed_observations": []}
            await agent._write_dialogue_events(entry, [event])
            assert not entry["unconfirmed_observations"]
            assert entry["anchor_event_id"]
        events = log.list_events("player:session")
        assert len(events) == 2
        assert len({item["event_id"] for item in events}) == 2
        assert {item["payload"]["conversation_id"] for item in events} == {"first", "second"}

    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        asyncio.run(scenario(log))


def test_context_includes_presented_choices_without_claiming_selection() -> None:
    tracker = dialogue.DialogueTracker(dialogue_roi=(0, 0.6, 1, 1), choice_roi=(0.6, 0.15, 1, 0.5))
    result = tracker.observe({"frame_id": "choice-frame", "captured_at_ms": 1700000000000,
                              "ui_category": "Talk", "is_in_talk_ui": True, "ocr": [
                                  {"text": "一起去", "normalized_bbox": {"x": 0.8, "y": 0.2, "width": 0.2, "height": 0.05}},
                                  {"text": "再想想", "normalized_bbox": {"x": 0.8, "y": 0.3, "width": 0.2, "height": 0.05}},
                              ]})
    assert any(event["type"] == "choice_presented" for event in result["events"])
    assert result["choice_snapshot"]["source"]["frame_id"] == "choice-frame"
    second = tracker.observe({"frame_id": "choice-frame-2", "captured_at_ms": 1700000000001,
                              "ui_category": "Talk", "is_in_talk_ui": True,
                              "ocr": result["choice_snapshot"]["rows"]})
    assert second["choice_snapshot"]["source"]["frame_id"] == "choice-frame-2"
    assert not any(event["type"] == "choice_presented" for event in second["events"])
    context = "\n".join(tracker.context_lines())
    assert "一起去 / 再想想" in context and "choice-frame" in context
    assert "可能已变化" in context and "尚未确认选择" in context
    tracker.reset()
    assert tracker.context_lines() == []


def test_dialogue_lock_starts_holds_and_ends() -> None:
    clock = Clock()
    tracker = dialogue.DialogueTracker(clock=clock, dialogue_roi=(0, 0.6, 1, 1), choice_roi=(0.7, 0.2, 1, 0.5))
    row = {"text": "安柏：跟我来", "normalized_bbox": {"x": 0.5, "y": 0.8, "width": 0.4, "height": 0.1}}
    started = tracker.observe(
        {"frame_id": "1", "captured_at_ms": clock.now, "ui_category": "Talk", "is_in_talk_ui": True, "ocr": [row]}
    )
    assert started["started"] is True
    assert tracker.is_locked(now_ms=clock.now) is True
    assert "跟我来" in tracker.context_lines()[0]

    again = tracker.observe(
        {"frame_id": "2", "captured_at_ms": clock.now, "ui_category": "Talk", "is_in_talk_ui": True, "ocr": [row]}
    )
    assert again["started"] is False
    assert again["ended"] is False

    clock.now += 5000  # 计时本身不证明对话结束
    assert tracker.is_locked(now_ms=clock.now) is True
    # 即使类别明确，没有帧标识也不能证明收到了不同观察。
    assert not tracker.observe({"ui_category": "Main"})["ended"]
    for frame_id in ("3", "4", "5"):
        # 未执行 OCR 时，明确的 Main 仍不能单独释放锁。
        assert not tracker.observe({"frame_id": frame_id, "ui_category": "Main"})["ended"]
    for frame_id in ("6", "7", "8"):
        ended = tracker.observe({"frame_id": frame_id, "captured_at_ms": clock.now,
                                 "ui_category": "Main", "is_in_talk_ui": False,
                                 "ocr_requested": True, "ocr_complete": True, "ocr": [],
                                 "ocr_roi": {"x": 0, "y": 0, "width": 100, "height": 100},
                                 "original_width": 100, "original_height": 100})
    assert ended["ended"] is True
    assert tracker.is_locked(now_ms=clock.now) is False


def test_dialogue_reset_and_no_talk_text() -> None:
    tracker = dialogue.DialogueTracker()
    result = tracker.observe({"frame_id": "1", "captured_at_ms": 1, "ui_category": "Talk",
                              "ocr": [{"text": "台词", "normalized_bbox": {
                                  "x": 0.5, "y": 0.8, "width": 0.2, "height": 0.05}}]})
    # 未配置对话区域时不能把全屏 OCR 当作台词。
    assert result["started"] is True
    assert result["conversation"]["lines"] == []
    assert tracker.context_lines() == []
    tracker.reset()
    assert tracker.locked is False
    assert tracker.context_lines() == []


def test_dialogue_lock_blocks_intent_publication(tmp_path: Path, monkeypatch) -> None:
    # 直接测试发布门控，不启动网络。
    from player_intent import IntentPublisher

    published = []

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self):
            return {"accepted": True, "cancelled": True, "remoteStopSent": True, "inputReleaseSent": True}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            published.append(kwargs.get("json"))
            return FakeResponse()

    import player_intent as pi

    class FakeAiohttp:
        @staticmethod
        def ClientTimeout(*args, **kwargs):
            return None

        @staticmethod
        def ClientSession(*args, **kwargs):
            return FakeSession()

    monkeypatch.setattr(pi, "aiohttp", FakeAiohttp)
    publisher = IntentPublisher("http://127.0.0.1:9", token="t")
    import asyncio

    assert asyncio.run(publisher.cancel()) is True
    assert published == [{"cancel": True}]
