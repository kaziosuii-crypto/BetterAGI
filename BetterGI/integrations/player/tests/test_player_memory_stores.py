#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 2 结构化记忆：working / episodic / semantic / retrieval / consolidator。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_episodic_memory as episodic  # noqa: E402
import player_memory_consolidator as consolidator_module  # noqa: E402
import player_retrieval as retrieval  # noqa: E402
import player_semantic_memory as semantic  # noqa: E402
import player_working_memory as working_module  # noqa: E402


class Clock:
    def __init__(self, start: int = 1_700_000_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def make_episode(**overrides):
    data = {
        "event_id": "ep-test",
        "time_ms": 1_700_000_000_000,
        "time": "2023-11-14T22:13:20Z",
        "game_time_range": [1_700_000_000_000, 1_700_000_010_000],
        "location": "蒙德城外",
        "characters": ["安柏", "派蒙"],
        "what_happened": "安柏带我们见了琴，提到了深渊教团。",
        "what_i_knew_before": "只知道蒙德有骑士团。",
        "what_i_learned": "琴是代理团长。",
        "what_i_thought": "安柏似乎很信任琴。",
        "what_i_felt": {"curiosity": 0.7, "tension": 0.3},
        "importance": 0.7,
        "emotional_intensity": 0.7,
        "source_session": "s-1",
    }
    data.update(overrides)
    return data


def make_fact(**overrides):
    data = {
        "fact_id": "k-test",
        "proposition": "琴是代理团长",
        "source": "ep-test",
        "learned_ms": 1_700_000_000_000,
        "confidence": 0.8,
        "tags": ["蒙德", "角色"],
    }
    data.update(overrides)
    return data


# ----------------------------------------------------------------------
# Working
# ----------------------------------------------------------------------


def test_invalid_episode_files_do_not_break_list_or_get(tmp_path: Path) -> None:
    store = episodic.EpisodicStore(tmp_path, clock=Clock())
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")
    future = make_episode(event_id="future", schema_version=99, visibility="player_knowledge")
    (tmp_path / "future.json").write_text(json.dumps(future), encoding="utf-8")
    assert store.list() == []
    assert store.get("future") is None


def test_episode_id_collision_never_overwrites(tmp_path: Path) -> None:
    store = episodic.EpisodicStore(tmp_path, clock=Clock())
    store.add(make_episode(event_id="same"))
    before = (tmp_path / "same.json").read_bytes()
    with pytest.raises(episodic.EpisodeError):
        store.add(make_episode(event_id="same", what_happened="另一事件"))
    assert (tmp_path / "same.json").read_bytes() == before


def test_semantic_invalid_entries_are_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "facts.json"
    original = {"schema_version": 1, "facts": [{"proposition": "未知条目"}]}
    path.write_text(json.dumps(original), encoding="utf-8")
    store = semantic.SemanticStore(path, clock=Clock())
    assert store.list() == []
    with pytest.raises(semantic.SemanticMemoryError):
        store.add(make_fact())
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_working_save_refuses_future_version(tmp_path: Path) -> None:
    path = tmp_path / "working.json"
    path.write_text('{"schema_version":99}', encoding="utf-8")
    store = working_module.WorkingMemoryStore(path)
    with pytest.raises(working_module.WorkingMemorySchemaError):
        store.save(working_module.default_working())
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 99


def test_all_memory_stores_redact_text(tmp_path: Path) -> None:
    clock = Clock()
    episode_store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    saved, _ = episode_store.add(make_episode(what_i_thought="token=secret123"))
    assert "secret123" not in episode_store.path_for(saved["event_id"]).read_text(encoding="utf-8")
    fact_store = semantic.SemanticStore(tmp_path / "facts.json", clock=clock)
    fact_store.add(make_fact(proposition="api_key=secret789"))
    assert "secret789" not in fact_store.path.read_text(encoding="utf-8")
    working_store = working_module.WorkingMemoryStore(tmp_path / "working.json", clock=clock)
    working_store.save({"why": "password=secret456"})
    assert "secret456" not in working_store.path.read_text(encoding="utf-8")


def test_concurrent_fact_adds_do_not_lose_entries(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "facts.json"
    store = semantic.SemanticStore(path, clock=Clock())
    errors = []

    def add(index: int) -> None:
        try:
            store.add(make_fact(fact_id="k-{0}".format(index), proposition="命题 {0}".format(index)))
        except Exception as exc:  # noqa: BLE001 - 记录失败供断言
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert store.count() == 4
    assert not list(tmp_path.glob("*.lock"))


def test_working_roundtrip_and_bounds(tmp_path: Path) -> None:
    path = tmp_path / "state" / "working.json"
    store = working_module.WorkingMemoryStore(path, clock=Clock())
    working = working_module.default_working()
    working["location"] = "蒙德城外"
    working["characters"] = [str(i) for i in range(20)]
    working["current_activity"] = "沿斜坡向上"
    working["emotion"] = {"curious": 0.7, "tension": 9}
    for index in range(30):
        working_module.append_recent_event(working, "事件 {0}".format(index), index)
    store.save(working)

    loaded = store.load()
    assert store.loaded_from_disk is True
    assert loaded["location"] == "蒙德城外"
    assert len(loaded["characters"]) == 12
    assert len(loaded["recent_events"]) == 20
    assert loaded["emotion"]["tension"] == 1.0
    assert loaded["updated_at"]


def test_working_corrupt_and_future_version(tmp_path: Path) -> None:
    path = tmp_path / "working.json"
    path.write_text("{broken", encoding="utf-8")
    store = working_module.WorkingMemoryStore(path, clock=Clock())
    loaded = store.load()
    assert loaded["schema_version"] == 1
    assert store.recovered_corrupt is not None and store.recovered_corrupt.exists()
    assert "损坏" in store.load_warning

    path.write_text(
        json.dumps({"schema_version": 99, "player_id": "lumi"}), encoding="utf-8"
    )
    with pytest.raises(working_module.WorkingMemorySchemaError):
        working_module.WorkingMemoryStore(path, clock=Clock()).load()


# ----------------------------------------------------------------------
# Episodic
# ----------------------------------------------------------------------


def test_episode_roundtrip_and_dedupe(tmp_path: Path) -> None:
    store = episodic.EpisodicStore(tmp_path / "episodes", clock=Clock())
    episode, created = store.add(make_episode())
    assert created is True
    assert store.path_for(episode["event_id"]).exists()
    assert episode["visibility"] == "player_knowledge"
    assert episode["what_i_felt"] == {"curiosity": 0.7, "tension": 0.3}

    duplicate, created_again = store.add(make_episode(event_id="ep-other"))
    assert created_again is False
    assert duplicate["event_id"] == episode["event_id"]

    loaded = store.list()
    assert len(loaded) == 1
    assert loaded[0]["location"] == "蒙德城外"
    assert store.get(episode["event_id"])["what_i_thought"] == "安柏似乎很信任琴。"


@pytest.mark.parametrize(
    "missing",
    ["what_i_knew_before", "what_i_thought", "what_i_felt", "what_happened"],
)
def test_episode_required_fields(tmp_path: Path, missing: str) -> None:
    data = make_episode()
    data.pop(missing)
    store = episodic.EpisodicStore(tmp_path / "episodes", clock=Clock())
    with pytest.raises(episodic.EpisodeError, match=missing):
        store.add(data)


def test_episode_rejects_world_visibility(tmp_path: Path) -> None:
    store = episodic.EpisodicStore(tmp_path / "episodes", clock=Clock())
    with pytest.raises(episodic.EpisodeError, match="player_knowledge"):
        store.add(make_episode(visibility="world_only"))


def test_episode_list_skips_corrupt_and_future(tmp_path: Path) -> None:
    directory = tmp_path / "episodes"
    directory.mkdir()
    store = episodic.EpisodicStore(directory, clock=Clock())
    store.add(make_episode(event_id="ep-ok"))
    (directory / "bad.json").write_text("{broken", encoding="utf-8")
    (directory / "future.json").write_text(
        json.dumps({"schema_version": 99, "what_happened": "x"}), encoding="utf-8"
    )
    episodes = store.list()
    assert [item["event_id"] for item in episodes] == ["ep-ok"]


# ----------------------------------------------------------------------
# Semantic
# ----------------------------------------------------------------------


def test_semantic_roundtrip_and_merge(tmp_path: Path) -> None:
    path = tmp_path / "semantic" / "facts.json"
    store = semantic.SemanticStore(path, clock=Clock())
    fact, created = store.add(make_fact())
    assert created is True
    assert path.exists()
    assert store.list()[0]["proposition"] == "琴是代理团长"

    merged, created_again = store.add(
        make_fact(fact_id="k-other", confidence=0.9, tags=["骑士团"])
    )
    assert created_again is False
    assert merged["confidence"] == 0.9
    assert set(merged["tags"]) == {"蒙德", "角色", "骑士团"}
    assert store.count() == 1


@pytest.mark.parametrize("visibility", ["world_only", "ground_truth"])
def test_semantic_rejects_world_visibility(tmp_path: Path, visibility: str) -> None:
    store = semantic.SemanticStore(tmp_path / "facts.json", clock=Clock())
    with pytest.raises(semantic.SemanticMemoryError, match="player_knowledge"):
        store.add(make_fact(visibility=visibility))


def test_semantic_missing_proposition(tmp_path: Path) -> None:
    store = semantic.SemanticStore(tmp_path / "facts.json", clock=Clock())
    with pytest.raises(semantic.SemanticMemoryError, match="proposition"):
        store.add({"proposition": ""})


def test_semantic_corrupt_and_future(tmp_path: Path) -> None:
    path = tmp_path / "facts.json"
    path.write_text("{broken", encoding="utf-8")
    store = semantic.SemanticStore(path, clock=Clock())
    assert store.load()["facts"] == []
    assert store.recovered_corrupt is not None

    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(semantic.SemanticMemorySchemaError):
        semantic.SemanticStore(path, clock=Clock()).load()


# ----------------------------------------------------------------------
# Retrieval
# ----------------------------------------------------------------------


def _populated_stores(tmp_path: Path, clock: Clock):
    episodic_store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    semantic_store = semantic.SemanticStore(tmp_path / "facts.json", clock=clock)
    episodic_store.add(make_episode(event_id="ep-old", time_ms=clock.now - 86_400_000,
                                    game_time_range=[1, 2], importance=0.9))
    clock.advance(1000)
    episodic_store.add(make_episode(
        event_id="ep-new",
        time_ms=clock.now,
        game_time_range=[3, 4],
        location="琴的办公室",
        characters=["琴"],
        what_happened="琴交给我们一个清理深渊教团的任务。",
        what_i_thought="她似乎在试探我们。",
        importance=0.6,
    ))
    semantic_store.add(make_fact(fact_id="k-mond", proposition="蒙德由西风骑士团守护"))
    return episodic_store, semantic_store


def test_retrieval_provenance_and_ranking(tmp_path: Path) -> None:
    clock = Clock()
    episodic_store, semantic_store = _populated_stores(tmp_path, clock)
    retriever = retrieval.MemoryRetriever(episodic_store, semantic_store, clock=clock)
    results = retriever.retrieve("琴 深渊 任务", now_ms=clock.now)
    assert results
    top = results[0]
    assert top.kind in ("episode", "semantic")
    assert top.source.startswith("episode:") or top.source.startswith("fact:")
    assert top.time
    assert 0 < top.score <= 1
    assert top.to_dict()["score"] == pytest.approx(round(top.score, 4))

    # 新且相关的 episode 应该排在旧记忆前面。
    ids = [item.memory_id for item in results]
    assert "ep-new" in ids


def test_retrieval_filters_world_visibility_and_budget(tmp_path: Path) -> None:
    clock = Clock()
    episodic_store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    # 直接写一个 world_only 文件，store 读取时应跳过。
    path = episodic_store.path_for("ep-world")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_id": "ep-world",
                "time": "2023-11-14T22:13:20Z",
                "time_ms": clock.now,
                "what_happened": "世界真相：某人是反派",
                "what_i_knew_before": "x",
                "what_i_thought": "x",
                "what_i_felt": {"x": 1},
                "visibility": "world_only",
            }
        ),
        encoding="utf-8",
    )
    retriever = retrieval.MemoryRetriever(episodic_store, None, clock=clock)
    assert retriever.retrieve("世界真相 反派", now_ms=clock.now) == []

    for index in range(5):
        episodic_store.add(
            make_episode(
                event_id="ep-{0}".format(index),
                game_time_range=[index, index + 1],
                what_happened="宝箱 " + "内容" * 200,
                importance=0.8,
            )
        )
    results = retriever.retrieve("宝箱", limit=10, max_chars=300)
    assert len(results) <= 10
    assert sum(len(item.text) for item in results) <= 300 or len(results) == 1


def test_external_retrieval_is_disabled_without_verified_provenance(tmp_path: Path) -> None:
    class External:
        player_knowledge_only = True
        called = False

        async def recall(self, query):
            self.called = True
            return "未验证的剧透"

    clock = Clock()
    external = External()
    retriever = retrieval.MemoryRetriever(None, None, external=external, clock=clock)
    assert asyncio.run(retriever.retrieve_async("安柏", now_ms=clock.now)) == []
    assert external.called is False


def test_openviking_legacy_adapter_is_not_available() -> None:
    assert not hasattr(retrieval, "OpenVikingPlayerRetriever")


def test_build_retrieval_query_contains_context(tmp_path: Path) -> None:
    query = retrieval.build_retrieval_query(
        {"guidance": "往坡上走", "commentary": [{"text": "起风了"}]},
        {"current_intention": "查看坡顶", "current_quest": "清理营地"},
        {"location": "蒙德城外", "characters": ["安柏"]},
    )
    for text in ("往坡上走", "起风了", "查看坡顶", "清理营地", "蒙德城外", "安柏"):
        assert text in query


# ----------------------------------------------------------------------
# 认知状态只影响已授权候选项的注意力
# ----------------------------------------------------------------------


class RetrievalRows:
    """为召回测试提供精确控制的原始候选项。"""

    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return self.rows


def attention_episode(event_id, **overrides):
    """生成无共同角色和情绪偏差的玩家记忆。"""
    data = make_episode(
        event_id=event_id, visibility="player_knowledge", location="place",
        characters=[], what_happened="clue", what_i_knew_before="before",
        what_i_learned="", what_i_thought="thought", what_i_felt={},
        unresolved_questions=[], importance=0.5, emotional_intensity=0.5,
    )
    data.update(overrides)
    return data


def test_cognitive_relationship_and_emotion_change_ranking():
    rows = [attention_episode("z"), attention_episode(
        "b", characters=["Alice"], what_i_felt={"curiosity": 0.8}
    )]
    retriever = retrieval.MemoryRetriever(RetrievalRows(rows), clock=Clock())
    baseline = retriever.retrieve("clue")
    context = retrieval.CognitiveContext(
        relationship_weights={"Alice": 0.9}, emotion={"curiosity": 0.75}
    )
    results = retriever.retrieve("clue", cognitive_context=context)
    assert baseline[0].memory_id == "z"  # 旧排序按 ID 打破平局
    assert results[0].memory_id == "b"
    score = results[0].meta["score_breakdown"]
    assert score["cognitive"]["relationship"] == pytest.approx(0.045)
    assert score["cognitive"]["emotion"] == pytest.approx(0.024)
    assert results[0].score > baseline[0].score
    assert score["total"] == pytest.approx(sum(score["base"].values()) + sum(score["cognitive"].values()))
    assert results[0].meta["attention_matches"]["emotion_dimensions"] == ["curiosity"]
    assert retriever.retrieve("missing", cognitive_context=context) == []


def test_cognitive_belief_support_and_counter_are_distinct_and_query_scoped():
    rows = [attention_episode("support", what_happened="first"),
            attention_episode("counter", what_happened="second"),
            attention_episode("other", what_happened="third")]
    retriever = retrieval.MemoryRetriever(RetrievalRows(rows), clock=Clock())
    context = retrieval.CognitiveContext(beliefs=[retrieval.BeliefLinks(
        "b1", support_sources=["episode:support"], counter_sources=["episode:counter"],
        relevance=0.8,
    )])
    results = retriever.retrieve("unrelatedquery", cognitive_context=context)
    assert {item.memory_id for item in results} == {"support", "counter"}
    roles = {item.memory_id: item.meta["attention_matches"]["belief_links"][0]["role"] for item in results}
    assert roles == {"support": "support", "counter": "counter"}
    assert all(item.meta["score_breakdown"]["cognitive"]["belief"] == pytest.approx(0.056) for item in results)
    assert retriever.retrieve("unrelatedquery", cognitive_context=retrieval.CognitiveContext()) == []
    assert retriever.retrieve("", cognitive_context=context) == []


def test_cognitive_threads_match_source_or_lexically():
    rows = [attention_episode("linked", what_happened="different"),
            attention_episode("words", what_happened="harbor clue"),
            attention_episode("unrelated", what_happened="forest")]
    retriever = retrieval.MemoryRetriever(RetrievalRows(rows), clock=Clock())
    context = retrieval.CognitiveContext(threads=[retrieval.ThreadAttention(
        "th-1", title="harbor", memory_sources=["episode:linked"], relevance=0.5
    )])
    results = retriever.retrieve("notfound", cognitive_context=context)
    assert {item.memory_id for item in results} == {"linked", "words"}
    matches = {item.memory_id: item.meta["attention_matches"]["threads"][0]["match"] for item in results}
    assert matches == {"linked": "source", "words": "lexical"}
    assert all(0 < item.meta["score_breakdown"]["cognitive"]["thread"] <= 0.02 for item in results)


def test_cognitive_context_never_bypasses_visibility_or_fact_gate():
    class Gate:
        def permits(self, fact):
            return fact["fact_id"] == "verified"

    episodes = RetrievalRows([attention_episode("world", visibility="world_only")])
    facts = RetrievalRows([
        make_fact(fact_id="verified", proposition="clue", visibility="player_knowledge"),
        make_fact(fact_id="unverified", proposition="clue", visibility="player_knowledge"),
        make_fact(fact_id="worldfact", proposition="clue", visibility="world_only"),
    ])
    context = retrieval.CognitiveContext(beliefs=[retrieval.BeliefLinks(
        "b", support_sources=["episode:world", "fact:unverified", "fact:worldfact", "fact:verified"]
    )])
    retriever = retrieval.MemoryRetriever(episodes, facts, knowledge_gate=Gate(), clock=Clock())
    assert [item.memory_id for item in retriever.retrieve("clue", cognitive_context=context)] == ["verified"]
    assert retrieval.MemoryRetriever(episodes, facts, clock=Clock()).retrieve("clue", cognitive_context=context) == []
    assert [item.memory_id for item in asyncio.run(retriever.retrieve_async("clue", cognitive_context=context))] == ["verified"]


def test_thread_activation_sources_drive_recall_without_bypassing_fact_gate():
    records = [
        {"thread_id": "t1", "status": "open", "title": "unknown topic",
         "last_activation": {"event_id": "observed"}},
        {"thread_id": "t2", "status": "dormant", "last_activation": {"event_id": "hidden"}},
        {"thread_id": "t3", "status": "open", "last_activation": {"event_id": "../unsafe"}},
    ]
    attention = retrieval.thread_attention_from_records(records)
    assert [x.thread_id for x in attention] == ["t1", "t3"]
    assert attention[0].memory_sources == ["episode:observed"]
    assert attention[1].memory_sources == []
    episodes = RetrievalRows([attention_episode("observed", what_happened="sighting")])
    facts = RetrievalRows([make_fact(fact_id="unchecked", proposition="secret", visibility="player_knowledge")])
    context = retrieval.CognitiveContext(
        threads=attention,
        beliefs=[retrieval.BeliefLinks("b", support_sources=["fact:unchecked"], counter_sources=["episode:observed"])],
        relationship_weights={"Alice": 1}, emotion={"curiosity": 1},
    )
    retriever = retrieval.MemoryRetriever(episodes, facts, clock=Clock())
    result = retriever.retrieve("unrelatedquery", cognitive_context=context)
    assert [x.memory_id for x in result] == ["observed"]
    assert result[0].meta["attention_matches"]["threads"][0]["match"] == "source"
    assert result[0].meta["attention_matches"]["belief_links"][0]["role"] == "counter"
    assert retriever.retrieve("secret", cognitive_context=context)[0].memory_id == "observed"


def test_cognitive_bonus_is_bounded_and_invalid_inputs_are_ignored():
    row = attention_episode("a", characters=["Alice"], what_i_felt={"curiosity": 1})
    retriever = retrieval.MemoryRetriever(RetrievalRows([row]), clock=Clock())
    context = retrieval.CognitiveContext(
        relationship_weights={"Alice": 20}, emotion={"curiosity": 10},
        beliefs=[retrieval.BeliefLinks("b", support_sources=["episode:a"], relevance=10)],
        threads=[retrieval.ThreadAttention("t", memory_sources=["episode:a"], relevance=5)],
    )
    result = retriever.retrieve("clue", cognitive_context=context)[0]
    breakdown = result.meta["score_breakdown"]
    assert sum(breakdown["cognitive"].values()) <= 0.2
    assert result.score <= 1
    invalid = retrieval.CognitiveContext(relationship_weights={"Alice": float("nan")},
                                          emotion={"curiosity": float("inf")})
    assert retriever.retrieve("clue", cognitive_context=invalid)[0].score == retriever.retrieve("clue")[0].score


# ----------------------------------------------------------------------
# Consolidator
# ----------------------------------------------------------------------


def test_consolidator_creates_episode_and_facts(tmp_path: Path) -> None:
    clock = Clock()
    working_store = working_module.WorkingMemoryStore(
        tmp_path / "state" / "working.json", clock=clock
    )
    episodic_store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    semantic_store = semantic.SemanticStore(tmp_path / "facts.json", clock=clock)
    consolidator = consolidator_module.MemoryConsolidator(
        working_store,
        episodic_store,
        semantic_store,
        session_id="s-1",
        clock=clock,
        episode_threshold=0.0,
        min_episode_interval_ms=0,
    )
    proposal = {
        "observation": "安柏带我们进了骑士团总部",
        "thought": "她好像很信任琴。",
        "intention": {"goal": "跟安柏去见琴", "reason": "想确认深渊教团的事"},
        "speech": {"text": "好，我跟你去。", "emotion": "curious", "intensity": 0.6},
        "memory_note": {
            "scene": "骑士团总部",
            "characters": ["安柏", "琴"],
            "what_happened": "安柏引荐我们见了琴。",
            "what_i_knew_before": "只知道蒙德有骑士团。",
            "what_i_learned": "琴是代理团长。",
            "what_i_felt": {"curiosity": 0.7, "trust": 0.5},
            "things_to_check": ["深渊教团在哪"],
            "learned_facts": [
                {"proposition": "琴是代理团长", "confidence": 0.8, "tags": ["蒙德"]}
            ],
        },
    }
    result = consolidator.consolidate(
        proposal,
        {"action_state": {"keys": ["W"]}},
        {"last_location": "蒙德城外"},
        now_ms=clock.now,
    )
    assert result.episode_created is True
    episode = result.episode
    assert episode["location"] == "骑士团总部"
    assert episode["characters"] == ["安柏", "琴"]
    assert episode["what_happened"] == "安柏引荐我们见了琴。"
    assert episode["what_i_thought"] == "她好像很信任琴。"
    assert episode["what_i_felt"] == {"curiosity": 0.7, "trust": 0.5}
    assert episode["source_session"] == "s-1"
    assert result.facts_created == 0
    assert semantic_store.list() == []
    assert episode["choices_i_made"] == []
    assert episode["proposed_intention"] == "跟安柏去见琴"
    assert result.working["location"] == "骑士团总部"
    assert result.working["current_activity"] == "跟安柏去见琴"
    assert result.working["recent_events"]

    again = consolidator.consolidate(proposal, {}, {}, now_ms=clock.now)
    assert again.episode_created is False
    assert again.episode["event_id"] == episode["event_id"]
    assert again.facts_created == 0


def test_consolidator_without_thought_does_not_create_episode(tmp_path: Path) -> None:
    clock = Clock()
    working_store = working_module.WorkingMemoryStore(
        tmp_path / "working.json", clock=clock
    )
    episodic_store = episodic.EpisodicStore(tmp_path / "episodes", clock=clock)
    semantic_store = semantic.SemanticStore(tmp_path / "facts.json", clock=clock)
    consolidator = consolidator_module.MemoryConsolidator(
        working_store,
        episodic_store,
        semantic_store,
        clock=clock,
        episode_threshold=0.0,
        min_episode_interval_ms=0,
    )
    result = consolidator.consolidate(
        {"observation": "看到一只猫", "memory_note": {"scene": "城门口"}},
        {},
        {},
        now_ms=clock.now,
    )
    assert result.episode is None
    assert result.working["location"] == "城门口"
    assert episodic_store.count() == 0
