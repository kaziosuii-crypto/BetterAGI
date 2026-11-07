#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Belief / Relationship / Emotion / Thread 状态测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_beliefs as beliefs  # noqa: E402
import player_emotion as emotion  # noqa: E402
import player_memory_consolidator as consolidator_module  # noqa: E402
import player_relationships as relationships  # noqa: E402
import player_episodic_memory as episodic  # noqa: E402
import player_semantic_memory as semantic  # noqa: E402
import player_threads as threads  # noqa: E402
import player_working_memory as working  # noqa: E402


class Clock:
    def __init__(self, start=1_700_000_000_000):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms


def test_belief_apply_disprove_preserves_history_and_rejects_ground_truth(tmp_path: Path) -> None:
    store = beliefs.BeliefStore(tmp_path / "beliefs.json", clock=Clock())
    store.apply([{"proposition": "凯亚在隐瞒", "status": "suspicion", "confidence": 0.5}])
    store.apply([{"proposition": "凯亚在隐瞒", "status": "disproven", "evidence": ["琴解释了"]}])
    record = store.list()[0]
    assert record["status"] == "disproven"
    assert any("suspicion" in item for item in record["history"])
    assert "琴解释了" in record["contradictions"]
    text = "\n".join(store.summary())
    assert "猜错了" in text
    assert "0.5" not in text
    with pytest.raises(beliefs.BeliefError):
        beliefs.normalize_belief({"proposition": "真相", "status": "ground_truth"})


def test_relationship_delta_is_capped(tmp_path: Path) -> None:
    store = relationships.RelationshipStore(tmp_path / "r.json", clock=Clock())
    store.apply([{"character": "安柏", "trust": 0.2, "memorable_events": ["她救了我"]}])
    store.apply([{"character": "安柏", "trust": 1.0}])
    record = store.list()[0]
    assert record["trust"] == pytest.approx(0.30)
    assert "她救了我" in record["memorable_events"]
    summary = store.summary()[0]
    assert "安柏" in summary
    assert "0.35" not in summary


def test_emotion_decays_and_event_delta_capped(tmp_path: Path) -> None:
    clock = Clock()
    store = emotion.EmotionStore(tmp_path / "e.json", clock=clock)
    store.apply({"sadness": 1.0, "joy": 0.9})
    assert store.view(now_ms=clock.now)["sadness"] == pytest.approx(emotion.MAX_DELTA)
    clock.advance(emotion.HALF_LIFE_MS)
    half = store.view(now_ms=clock.now)["sadness"]
    assert 0.15 <= half <= 0.19
    store.apply({"sadness": 0.0, "fear": 1.0})
    view = store.view(now_ms=clock.now)
    assert view["sadness"] <= half + 0.01
    assert view["fear"] >= 0.3
    assert "害怕" in store.summary(now_ms=clock.now)


def test_threads_activate_and_resolve(tmp_path: Path) -> None:
    store = threads.ThreadStore(tmp_path / "t.json", clock=Clock())
    store.apply([{"title": "深渊教团在哪", "status": "open", "current_theory": "可能在城外"}])
    thread = store.list()[0]
    store.apply([{"title": "深渊教团在哪", "status": "resolved"}])
    assert store.list()[0]["status"] == "resolved"
    store.apply([{"title": "新的线索", "status": "dormant"}])
    dormant = [item for item in store.list() if item["title"] == "新的线索"][0]
    store.activate([dormant["thread_id"]])
    assert [item for item in store.list() if item["title"] == "新的线索"][0]["status"] == "open"
    assert thread["thread_id"].startswith("th-")


def _event(store, event_id, now, *, happened="陌生人在港口停下", characters=()):
    """创建并返回可检索的玩家 episode。"""
    return store.add({
        "event_id": event_id, "time_ms": now, "visibility": "player_knowledge",
        "what_happened": happened, "characters": list(characters),
        "what_i_knew_before": "我知道有港口", "what_i_thought": "我想继续观察",
        "what_i_felt": {"curiosity": 0.5},
    }, dedupe=False)[0]


def test_event_activation_persists_reason_history_and_is_idempotent(tmp_path: Path) -> None:
    clock = Clock()
    store = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    episodes = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    store.apply([{"title": "凯亚的来历", "status": "dormant", "characters": ["凯亚"],
                  "evidence": ["之前的线索"], "current_theory": "尚不清楚",
                  "extension": {"keep": True}}])
    clock.advance(100)
    _event(episodes, "event-1", clock.now, characters=["凯亚"])
    changed = store.activate_from_event("event-1", episodic_store=episodes)
    assert len(changed) == 1
    reopened = threads.ThreadStore(store.path, clock=clock)
    record = reopened.list()[0]
    assert record["status"] == "open"
    assert record["last_activation"] == {"event_id": "event-1", "reason": "entity:凯亚", "time_ms": clock.now}
    assert record["status_history"][-1]["event_id"] == "event-1"
    assert record["status_history"][-1]["from_status"] == "dormant"
    assert record["evidence"] == ["之前的线索"]
    assert record["current_theory"] == "尚不清楚"
    assert record["extension"] == {"keep": True}
    assert reopened.activate_from_event("event-1", episodic_store=episodes) == []
    reopened.apply([{"title": "凯亚的来历", "status": "dormant"}])
    assert reopened.activate_from_event("event-1", episodic_store=episodes) == []
    assert reopened.list()[0]["status"] == "dormant"
    assert [x["to_status"] for x in reopened.list()[0]["status_history"]] == ["open", "dormant"]
    clock.advance(100)
    _event(episodes, "event-2", clock.now, characters=["凯亚"])
    assert len(reopened.activate_from_event("event-2", episodic_store=episodes)) == 1
    history = reopened.list()[0]["status_history"]
    assert [entry.get("event_id") for entry in history] == ["event-1", None, "event-2"]


def test_event_activation_uses_specific_observed_topics_and_entities_only(tmp_path: Path) -> None:
    clock = Clock()
    store = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    episodes = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    store.apply([
        {"title": "教团的动向", "status": "dormant", "topics": ["深渊教团"]},
        {"title": "旧任务", "status": "dormant", "topics": ["任务"], "characters": ["琴"]},
        {"title": "海港的人", "status": "dormant", "characters": ["Alice"]},
        {"title": "另一条线索", "status": "resolved", "topics": ["深渊教团"]},
        {"title": "新的线索", "status": "dormant", "topics": ["当前任务", "new mission"]},
    ])
    clock.advance(10)
    _event(episodes, "event-topic", clock.now, happened="听说深渊教团在附近。新的线索是当前任务，new mission。", characters=["琴", "Alicia"])
    changed = store.activate_from_event("event-topic", episodic_store=episodes)
    assert [x["title"] for x in changed] == ["教团的动向"]
    assert changed[0]["last_activation"]["reason"] == "topic:深渊教团"
    assert {x["title"]: x["status"] for x in store.list()} == {
        "教团的动向": "open", "旧任务": "dormant", "海港的人": "dormant", "另一条线索": "resolved", "新的线索": "dormant"
    }


def test_event_activation_rejects_unsafe_missing_old_or_unobserved_events(tmp_path: Path) -> None:
    clock = Clock()
    store = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    episodes = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    _event(episodes, "old", clock.now - 1, happened="深渊教团出现")
    store.apply([{"title": "深渊教团", "status": "dormant"}])
    assert store.activate_from_event("old", episodic_store=episodes) == []
    assert store.activate_from_event("../old", episodic_store=episodes) == []
    assert store.activate_from_event("missing", episodic_store=episodes) == []
    clock.advance(10)

    class UntrustedStore:
        def __init__(self, row):
            self.row = row
        def get(self, event_id):
            return self.row

    for row in (
        {"event_id": "bad", "time_ms": clock.now, "what_happened": "深渊教团"},
        {"event_id": "bad", "time_ms": clock.now, "what_happened": "深渊教团", "visibility": "world_only"},
        {"event_id": "other", "time_ms": clock.now, "what_happened": "深渊教团", "visibility": "player_knowledge"},
        {"event_id": "bad", "time_ms": clock.now, "what_happened": "天气晴朗", "what_i_thought": "深渊教团", "visibility": "player_knowledge"},
        {"event_id": "bad", "time_ms": clock.now + 1, "what_happened": "深渊教团", "visibility": "player_knowledge"},
    ):
        assert store.activate_from_event("bad", episodic_store=UntrustedStore(row)) == []
    assert store.list()[0]["status"] == "dormant"


def test_event_activation_has_hard_limit_and_does_not_replay(tmp_path: Path) -> None:
    clock = Clock()
    store = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    episodes = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    for i in range(12):
        store.apply([{"title": f"线索 {i}", "status": "dormant", "characters": ["凯亚"]}])
    clock.advance(10)
    _event(episodes, "event-limited", clock.now, characters=["凯亚"])
    assert len(store.activate_from_event("event-limited", episodic_store=episodes)) == 8
    assert store.activate_from_event("event-limited", episodic_store=episodes) == []
    assert len(store.list(status="dormant")) == 4


def test_consolidator_applies_all_state_updates(tmp_path: Path) -> None:
    clock = Clock()
    working_store = working.WorkingMemoryStore(tmp_path / "w.json", clock=clock)
    episodic_store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    semantic_store = semantic.SemanticStore(tmp_path / "facts.json", clock=clock)
    belief_store = beliefs.BeliefStore(tmp_path / "beliefs.json", clock=clock)
    relationship_store = relationships.RelationshipStore(tmp_path / "rel.json", clock=clock)
    emotion_store = emotion.EmotionStore(tmp_path / "emo.json", clock=clock)
    thread_store = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    consolidator = consolidator_module.MemoryConsolidator(
        working_store, episodic_store, semantic_store,
        clock=clock, episode_threshold=0.0, min_episode_interval_ms=0,
        belief_store=belief_store, relationship_store=relationship_store,
        emotion_store=emotion_store, thread_store=thread_store,
    )
    result = consolidator.consolidate(
        {
            "thought": "她似乎很信任琴。",
            "observation": "安柏带我们见了琴",
            "memory_note": {"what_happened": "安柏引荐我们见了琴", "what_i_felt": {"curiosity": 0.6}},
            "beliefs": [{"proposition": "安柏信任琴", "status": "belief", "confidence": 0.7}],
            "relationships": [{"character": "安柏", "trust": 0.4}],
            "emotion": {"curiosity": 0.6},
            "threads": [{"title": "深渊教团在哪", "status": "open"}],
        },
        {}, {}, now_ms=clock.now,
    )
    assert result.state_updates["beliefs"]
    assert result.state_updates["relationships"] == ["安柏"]
    assert result.state_updates["emotion"] is True
    assert result.state_updates["threads"]
