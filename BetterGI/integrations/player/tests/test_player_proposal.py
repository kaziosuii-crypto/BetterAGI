#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow 提案解析的单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_proposal as proposal_module  # noqa: E402


@pytest.mark.parametrize("decision", [
    {"action": "wait", "frame_id": "帧-01"},
    {"action": "advance", "frame_id": "frame-01"},
    {"action": "select", "frame_id": "帧-01", "option_id": "选项-02"},
    {"action": "select", "frame_id": "a" * 128, "option_id": "b" * 128},
])
def test_dialogue_decision_preserves_exact_identifiers(decision) -> None:
    parsed = proposal_module.normalize_proposal({"thought": "想一想", "dialogue_decision": decision})
    assert parsed["dialogue_decision"] == decision
    assert parsed["dialogue_decision"] is not decision
    assert "executed" not in parsed["dialogue_decision"]
    assert "dialogue_decision" not in proposal_module.normalize_proposal({"thought": "想一想"})


@pytest.mark.parametrize("decision", [
    None, "wait", [], {},
    {"action": "skip", "frame_id": "frame"},
    {"action": "WAIT", "frame_id": "frame"},
    {"action": True, "frame_id": "frame"},
    {"action": "wait"},
    {"action": "wait", "frame_id": ""},
    {"action": "wait", "frame_id": " frame"},
    {"action": "wait", "frame_id": "frame\nother"},
    {"action": "wait", "frame_id": "frame\x00"},
    {"action": "wait", "frame_id": "\ud800"},
    {"action": "wait", "frame_id": "a" * 129},
    {"action": "advance", "frame_id": "frame", "option_id": None},
    {"action": "select", "frame_id": "frame"},
    {"action": "select", "frame_id": "frame", "option_id": ""},
    {"action": "select", "frame_id": "frame", "option_id": "b" * 129},
    {"action": "select", "frame_id": "frame", "option_id": 1},
    {"action": "select", "frame_id": "frame", "option_id": "option", "x": 10},
])
def test_invalid_dialogue_decision_rejects_entire_proposal(decision) -> None:
    assert proposal_module.normalize_proposal({"thought": "想一想", "dialogue_decision": decision}) is None


def test_dialogue_decision_works_without_other_content_and_in_wrapper() -> None:
    decision = {"action": "wait", "frame_id": "f"}
    assert proposal_module.normalize_proposal({"dialogue_decision": decision})["dialogue_decision"] == decision
    assert proposal_module.normalize_proposal({"proposal": {"dialogue_decision": decision}})["dialogue_decision"] == decision


def test_duplicate_dialogue_fields_are_rejected() -> None:
    assert proposal_module.parse_proposal(
        '{"thought":"想一想","dialogue_decision":{"action":"wait","action":"select","frame_id":"f"}}'
    ) is None
    assert proposal_module.parse_proposal(
        '{"thought":"想一想","dialogue_decision":{"action":"wait","frame_id":"f"},'
        '"dialogue_decision":{"action":"advance","frame_id":"f"}}'
    ) is None


def test_dialogue_decision_schema_is_non_executable() -> None:
    from player_context import PROPOSAL_SCHEMA
    assert '"dialogue_decision"' in PROPOSAL_SCHEMA
    assert "wait、advance 或 select" in PROPOSAL_SCHEMA
    assert "不得添加坐标" in PROPOSAL_SCHEMA
    assert "不代表执行或选项已选中" in PROPOSAL_SCHEMA


def test_parse_fenced_json_with_prose() -> None:
    text = """好的，这是我的想法：
```json
{"observation": "前面有箱子", "thought": "先看看有没有埋伏", "intention": {"goal": "绕到箱子侧面"}}
```
希望有帮助。"""
    proposal = proposal_module.parse_proposal(text)
    assert proposal["thought"] == "先看看有没有埋伏"
    assert proposal["intention"]["goal"] == "绕到箱子侧面"
    assert proposal["intention"]["horizon"] == "soon"


def test_parse_wrapped_proposal() -> None:
    text = json.dumps(
        {"proposal": {"thought": "先等等", "speech": {"text": "咦？"}}},
        ensure_ascii=False,
    )
    proposal = proposal_module.parse_proposal(text)
    assert proposal["thought"] == "先等等"
    assert proposal["speech"]["text"] == "咦？"
    assert proposal["speech"]["emotion"] == "calm"


def test_balanced_object_ignores_braces_in_strings() -> None:
    text = '说明 {"thought": "他说的是 {别过去}", "confidence": 0.4} 结束'
    proposal = proposal_module.parse_proposal(text)
    assert proposal["thought"] == "他说的是 {别过去}"
    assert proposal["confidence"] == 0.4


def test_empty_or_malformed_returns_none() -> None:
    assert proposal_module.parse_proposal("") is None
    assert proposal_module.parse_proposal("not json") is None
    assert proposal_module.parse_proposal("[1, 2, 3]") is None
    assert proposal_module.parse_proposal('{"unrelated": true}') is None


def test_fields_are_clamped_and_actions_limited() -> None:
    actions = [
        {"hint": "walk", "detail": "走" * 500, "reason": "因为" * 500}
        for _ in range(10)
    ]
    actions.append({"hint": "teleport", "detail": "未知动作"})
    text = json.dumps(
        {
            "thought": "想" * 2000,
            "speech": {
                "text": "说" * 500,
                "emotion": "furious",
                "intensity": 9,
                "should_speak": True,
            },
            "proposed_actions": actions,
            "confidence": -3,
        },
        ensure_ascii=False,
    )
    proposal = proposal_module.parse_proposal(text)
    assert len(proposal["thought"]) == 800
    assert len(proposal["speech"]["text"]) == 160
    assert proposal["speech"]["emotion"] == "calm"
    assert proposal["speech"]["intensity"] == 1.0
    assert proposal["confidence"] == 0.0
    assert len(proposal["proposed_actions"]) == 6
    for action in proposal["proposed_actions"]:
        assert action["executed"] is False
    # 第 7 个动作（未知 hint）被截断，不在结果里；安全检查仍覆盖顶层 hint。
    single = proposal_module.normalize_proposal(
        {"proposed_actions": [{"hint": "teleport", "detail": "传送"}]}
    )
    assert single["proposed_actions"][0]["hint"] == "other"


def test_should_speak_false_is_preserved() -> None:
    proposal = proposal_module.normalize_proposal(
        {"speech": {"text": "算了不说了", "should_speak": False}}
    )
    assert proposal["speech"]["should_speak"] is False
    assert proposal["speech"]["text"] == "算了不说了"


def test_memory_note_is_limited() -> None:
    proposal = proposal_module.normalize_proposal(
        {
            "thought": "嗯",
            "memory_note": {
                "scene": "城门口",
                "quest": "找凯亚",
                "things_to_check": ["a", "b", "c", "d", "e", "f", "g"],
            },
        }
    )
    assert proposal["memory_note"]["scene"] == "城门口"
    assert proposal["memory_note"]["things_to_check"] == ["a", "b", "c", "d", "e"]


def test_intention_preserves_motor_fields_without_changing_legacy_shape() -> None:
    old = proposal_module.normalize_proposal({"intention": "继续走"})
    assert old["intention"] == {"goal": "继续走", "reason": "", "horizon": "soon"}
    proposal = proposal_module.normalize_proposal({"intention": {
        "goal": "找门", "target": {"name": "大门", "x": 12},
        "constraints": ["不要攻击"], "interrupt_conditions": ["进入对话"],
        "motor_mode": "explore", "deadline_ms": 9000,
    }})
    assert proposal["intention"]["target"] == {"name": "大门", "x": 12}
    assert proposal["intention"]["constraints"] == ["不要攻击"]
    assert proposal["intention"]["interrupt_conditions"] == ["进入对话"]
    assert proposal["intention"]["motor_mode"] == "explore"
    assert proposal["intention"]["deadline_ms"] == 9000
