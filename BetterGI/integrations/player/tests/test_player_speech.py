#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Speech Policy 测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_speech_policy as policy_module  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_000_000

    def __call__(self):
        return self.now


def proposal(text="好，我跟你去。", emotion="curious", intensity=0.6, should_speak=True):
    return {
        "speech": {
            "text": text,
            "emotion": emotion,
            "intensity": intensity,
            "should_speak": should_speak,
        }
    }


def test_policy_speaks_then_cools_down_and_important_bypasses() -> None:
    clock = Clock()
    policy = policy_module.SpeechPolicy(clock=clock)
    first = policy.decide(proposal(), now_ms=clock.now)
    assert first["speak"] is True
    assert first["pace"] == "normal"
    assert policy.acknowledge_delivery(first["decision_id"], now_ms=clock.now)
    clock.now += 1000
    second = policy.decide(proposal(), now_ms=clock.now)
    assert second["speak"] is False
    assert second["reason"] == "cooldown"
    important = policy.decide(
        proposal(emotion="surprised", intensity=0.9), now_ms=clock.now
    )
    assert important["speak"] is True


def test_delivery_receipt_separates_proposed_delivered_and_suppressed() -> None:
    clock = Clock()
    policy = policy_module.SpeechPolicy(clock=clock)
    first = policy.decide(proposal(text="先看这里"))
    assert policy.last_speech_proposed["text"] == "先看这里"
    assert policy.last_speech_delivered is None
    assert not policy.acknowledge_delivery("wrong-id")
    assert policy.reject_delivery(first["decision_id"], "timeline_unavailable")
    assert policy.last_speech_suppressed["reason"] == "timeline_unavailable"
    assert policy.last_speech_delivered is None
    second = policy.decide(proposal(text="再看看"))
    assert second["speak"]
    assert policy.acknowledge_delivery(second["decision_id"])
    assert policy.last_speech_delivered["text"] == "再看看"
    assert not policy.acknowledge_delivery(second["decision_id"])
    assert policy.decide(proposal())["reason"] == "cooldown"
    policy.reset()
    assert policy.last_speech_delivered is None
    assert policy.last_speech_suppressed is None


def test_unknown_delivery_is_not_reported_as_suppressed_or_delivered() -> None:
    policy = policy_module.SpeechPolicy(clock=Clock())
    decision = policy.decide(proposal())
    assert policy.mark_delivery_unknown(decision["decision_id"])
    assert policy.last_speech_unknown["text"] == "好，我跟你去。"
    assert policy.last_speech_suppressed is None
    assert policy.last_speech_delivered is None
    assert not policy.acknowledge_delivery(decision["decision_id"])


def test_dialogue_lock_and_silence_bias_block_speech() -> None:
    clock = Clock()
    policy = policy_module.SpeechPolicy(clock=clock)
    assert policy.decide(proposal(), dialogue_locked=True, now_ms=clock.now)["reason"] == "dialogue_lock"
    assert policy.decide(proposal(intensity=0.3), now_ms=clock.now, silence_bias="high")["reason"] == "silence_bias"
    assert policy.decide(proposal(should_speak=False), now_ms=clock.now)["reason"] == "model_silent"
    assert policy.decide(proposal(intensity=0), now_ms=clock.now)["intensity"] == 0.0
    assert policy.decide({"speech": {"text": ""}}, now_ms=clock.now)["reason"] == "empty"


def test_policy_outputs_prosody_without_raw_emotion_numbers() -> None:
    policy = policy_module.SpeechPolicy(clock=Clock())
    result = policy.decide(proposal(emotion="sad", intensity=0.2), now_ms=1_000_000)
    assert result["speak"] is True
    assert result["pace"] == "slow"
    assert result["volume"] == "soft"
    assert result["pause_before_ms"] == 300
    assert "intensity" in result
