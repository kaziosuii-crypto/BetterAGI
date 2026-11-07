#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连续性 A–E 的缩小版回归（假数据，不联网、不启动游戏）。

这些测试验证状态机与上下文是否真的跨进程/跨场保留，而不是只测字段。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_beliefs as beliefs  # noqa: E402
import player_context_builder as context_builder  # noqa: E402
import player_emotion as emotion  # noqa: E402
import player_episodic_memory as episodic  # noqa: E402
import player_profile as profile_module  # noqa: E402
import player_recap as recap_module  # noqa: E402
import player_retrieval as retrieval  # noqa: E402
import player_semantic_memory as semantic  # noqa: E402
import player_speech_policy as speech  # noqa: E402
import player_threads as threads  # noqa: E402

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "lumi.json"


class Clock:
    def __init__(self, start=1_700_000_000_000):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms


def episode(event_id, text, characters, time_ms, question=""):
    return {
        "event_id": event_id, "time_ms": time_ms, "time": "2023-11-14T22:13:20Z",
        "what_happened": text, "what_i_knew_before": "之前不知道", "what_i_thought": "要记住",
        "what_i_felt": {"curiosity": 0.6}, "characters": characters,
        "unresolved_questions": [question] if question else [], "importance": 0.7,
        "source_session": "s", "visibility": "player_knowledge",
    }


def test_a_cross_session_memory_reaches_next_session_prompt(tmp_path: Path) -> None:
    clock = Clock()
    store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    store.add(episode("ep-a", "安柏帮我们解了围。", ["安柏"], clock.now, "她为什么帮我们？"))
    state = {"last_location": "蒙德城外", "last_internal_thought": "安柏很可靠。"}
    working = {"location": "蒙德城外", "characters": ["安柏"], "emotion": {"curiosity": 0.6}}
    recap = recap_module.build_recap(
        session_id="s1", state=state, working=working, episodes=store.list(), now_ms=clock.now
    )
    recap_store = recap_module.RecapStore(tmp_path / "recap.json", clock=clock)
    recap_store.save(recap)
    recap_store = recap_module.RecapStore(tmp_path / "recap.json", clock=clock)
    loaded_recap = recap_store.load()
    retriever = retrieval.MemoryRetriever(store, None, clock=clock)
    results = retriever.retrieve("安柏 帮我们", now_ms=clock.now)
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    bundle = context_builder.ContextBuilder(profile).build(
        state, {"frame": {"seq": 1}}, working=working, memories=results, recap=loaded_recap
    )
    assert "安柏帮我们解了围" in bundle.user
    assert "上一场回顾" in bundle.user
    assert "安柏" in bundle.user


def test_b_belief_formed_then_disproven_keeps_history(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "beliefs.json"
    store = beliefs.BeliefStore(path, clock=clock)
    store.apply([{"proposition": "凯亚在撒谎", "status": "suspicion", "confidence": 0.6}])
    store.apply([{"proposition": "凯亚在撒谎", "status": "disproven", "evidence": ["他救了我们"]}])
    reopened = beliefs.BeliefStore(path, clock=clock)
    record = reopened.list()[0]
    assert record["status"] == "disproven"
    assert any("suspicion" in item for item in record["history"])
    assert "猜错了" in "\n".join(reopened.summary())


def test_c_dormant_thread_can_be_reactivated(tmp_path: Path) -> None:
    clock = Clock()
    store = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    store.apply([{"title": "深渊教团的线索", "status": "open"}])
    thread_id = store.list()[0]["thread_id"]
    store.apply([{"title": "深渊教团的线索", "status": "dormant"}])
    assert store.list(status="open") == []
    store.activate([thread_id], now_ms=clock.now)
    reopened = threads.ThreadStore(tmp_path / "threads.json", clock=clock)
    assert reopened.list(status="open")[0]["title"] == "深渊教团的线索"


def test_d_sadness_afterglow_damps_instant_excitement(tmp_path: Path) -> None:
    clock = Clock()
    store = emotion.EmotionStore(tmp_path / "emotion.json", clock=clock)
    for _ in range(3):
        store.apply({"sadness": 0.95}, now_ms=clock.now)
    clock.advance(30_000)
    policy = speech.SpeechPolicy(clock=clock)
    decision = policy.decide(
        {"speech": {"text": "哇！宝箱！", "emotion": "excited", "intensity": 0.9}},
        now_ms=clock.now,
        emotion_view=store.view(now_ms=clock.now),
    )
    assert decision["speak"] is True
    assert decision["mood_damped"] is True
    assert decision["pace"] != "fast"


def test_e_world_only_never_enters_prompt(tmp_path: Path) -> None:
    path = tmp_path / "facts.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "facts": [{
            "fact_id": "k-world", "proposition": "最终反派是某角色",
            "source": "world", "visibility": "world_only", "confidence": 1.0,
        }],
    }, ensure_ascii=False), encoding="utf-8")
    store = semantic.SemanticStore(path, clock=Clock())
    assert store.list() == []
    retriever = retrieval.MemoryRetriever(None, store, clock=Clock())
    assert retriever.retrieve("最终反派") == []
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    bundle = context_builder.ContextBuilder(profile).build(
        {}, {"frame": {"seq": 1}}, memories=[]
    )
    assert "最终反派" not in bundle.user
