#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2 ContextBuilder：稳定、预算、legacy 回滚与防泄漏测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_context as legacy  # noqa: E402
import player_context_builder as builder_module  # noqa: E402
import player_profile as profile_module  # noqa: E402
from player_retrieval import RetrievedMemory  # noqa: E402

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "lumi.json"


def inputs():
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    state = {
        "last_internal_thought": "刚才那个箱子旁边好像有动静",
        "current_intention": "先绕到坡上看一眼",
        "last_location": "蒙德城外",
        "current_quest": "看看附近有什么",
        "things_i_want_to_check_next": ["坡上的声音"],
    }
    snapshot = {
        "frame": {"seq": 12, "width": 1280, "height": 720, "ui_category": "world"},
        "guidance": "往坡上走",
        "guidance_source": "director",
        "commentary": [{"kind": "reaction", "text": "起风了"}],
        "action_state": {"keys": ["W"], "queueDepth": 1, "starved": False},
        "recent_actions": [{"thought": "向前", "keys": ["W"], "status": "accepted"}],
        "live_running": True,
    }
    working = {
        "location": "蒙德城外",
        "characters": ["安柏"],
        "recent_events": [{"text": "看到一条斜坡", "kind": "observation"}],
        "current_activity": "沿斜坡向上",
        "why": "声音从坡上传来",
        "open_questions": ["坡顶有什么"],
        "emotion": {"curious": 0.73},
        "short_plan": ["上坡看看"],
    }
    memories = [
        RetrievedMemory(
            memory_id="ep-1",
            kind="episode",
            text="蒙德城外：安柏带我们见了琴。；我当时想：她很信任琴。",
            source="episode:ep-1",
            time="2023-11-14T22:13:20Z",
            time_ms=1_700_000_000_000,
            score=0.82,
            visibility="player_knowledge",
            importance=0.7,
            meta={"characters": ["安柏", "琴"]},
        ),
        RetrievedMemory(
            memory_id="k-1",
            kind="semantic",
            text="琴是代理团长",
            source="fact:k-1",
            time="2023-11-14T22:13:20Z",
            time_ms=1_700_000_000_000,
            score=0.6,
            visibility="player_knowledge",
            importance=0.8,
            meta={"tags": ["蒙德"]},
        ),
        RetrievedMemory(
            memory_id="ep-world",
            kind="episode",
            text="世界真相：某人是反派",
            source="episode:ep-world",
            time="2023-11-14T22:13:20Z",
            time_ms=1_700_000_000_000,
            score=0.99,
            visibility="world_only",
            meta={},
        ),
    ]
    return profile, state, snapshot, working, memories


def test_v2_bundle_has_blocks_and_is_stable() -> None:
    profile, state, snapshot, working, memories = inputs()
    builder = builder_module.ContextBuilder(profile)
    first = builder.build(
        state, snapshot, working=working, memories=memories,
        retrieval_meta={"selected": 2, "episodes": 1, "semantic": 1},
    )
    second = builder.build(
        state, snapshot, working=working, memories=memories,
        retrieval_meta={"selected": 2, "episodes": 1, "semantic": 1},
    )
    assert first.system == second.system
    assert first.user == second.user
    assert first.record() == second.record()
    assert first.mode == "v2"
    assert "小光 Lumi" in first.system
    assert "shadow" in first.system
    for text in (
        "沿斜坡向上",
        "往坡上走",
        "起风了",
        "上次留下的想法",
        "刚才那个箱子旁边好像有动静",
        "上次想做的事",
        "先绕到坡上看一眼",
        "安柏带我们见了琴",
        "琴是代理团长",
    ):
        assert text in first.user
    assert "世界真相" not in first.user
    assert "world_only" not in first.user
    assert "E:\\" not in first.user
    names = [block.name for block in first.blocks]
    assert names == [
        "identity",
        "perception",
        "working",
        "intention",
        "resume",
        "episodic",
        "semantic",
        "commentary",
        "output",
    ]
    assert first.retrieval["selected"] == 2


def test_no_raw_scores_or_numeric_emotion_in_prompt() -> None:
    profile, state, snapshot, working, memories = inputs()
    bundle = builder_module.ContextBuilder(profile).build(
        state, snapshot, working=working, memories=memories
    )
    assert "0.82" not in bundle.user
    assert "score" not in bundle.user.lower()
    assert "curious" in bundle.user
    assert "0.73" not in bundle.user


def test_block_budgets_are_enforced() -> None:
    profile, state, snapshot, working, memories = inputs()
    builder = builder_module.ContextBuilder(profile, budgets={"episodic": 20, "output": 80})
    bundle = builder.build(state, snapshot, working=working, memories=memories)
    episodic = next(block for block in bundle.blocks if block.name == "episodic")
    assert len(episodic.text) <= episodic.budget
    assert episodic.truncated is True
    assert "还有" in episodic.text or "…" in episodic.text

    output = next(block for block in bundle.blocks if block.name == "output")
    # required 块永不截断：宁可 over_budget，也不能让模型收到半截 schema。
    assert output.truncated is False
    assert output.over_budget is True
    assert "proposed_actions" in output.text


def test_total_budget_drops_optional_blocks() -> None:
    profile, state, snapshot, working, memories = inputs()
    full = builder_module.ContextBuilder(profile).build(
        state, snapshot, working=working, memories=memories
    )
    builder = builder_module.ContextBuilder(profile, max_total_chars=500)
    bundle = builder.build(state, snapshot, working=working, memories=memories)
    assert bundle.total_chars < full.total_chars
    assert bundle.dropped
    assert "output" in [block.name for block in bundle.blocks]
    assert "identity" in [block.name for block in bundle.blocks]


def test_legacy_mode_matches_phase1_prompt() -> None:
    profile, state, snapshot, _working, _memories = inputs()
    expected = legacy.build_shadow_prompt(profile, state, snapshot)
    bundle = builder_module.ContextBuilder(profile, mode="legacy").build(
        state, snapshot, working={}, memories=[]
    )
    assert bundle.mode == "legacy"
    assert bundle.system == expected["system"]
    assert bundle.user == expected["user"]
    assert bundle.blocks == ()


def test_empty_optional_blocks_are_omitted() -> None:
    profile, state, snapshot, _working, _memories = inputs()
    bundle = builder_module.ContextBuilder(profile).build(state, snapshot)
    names = [block.name for block in bundle.blocks]
    assert "working" not in names
    assert "episodic" not in names
    assert "output" in names
