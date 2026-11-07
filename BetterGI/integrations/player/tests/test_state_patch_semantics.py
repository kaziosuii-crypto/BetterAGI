#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关系、信念与线索的更新和持久化回归测试。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_beliefs import BeliefStore
from player_relationships import RelationshipStore
from player_threads import ThreadStore


class Clock:
    """可控毫秒时钟。"""

    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


def test_relationship_unicode_patch_initial_cap_and_legacy_ids(tmp_path):
    path = tmp_path / "relationships.json"
    clock = Clock()
    # 旧数据中的中文 ID 曾经都为 unknown；加载时按名字修复。
    path.write_text(json.dumps({"schema_version": 1, "relationships": [
        {"character": "安柏", "character_id": "unknown", "trust": 0.7,
         "affection": 0.6, "last_updated_ms": 100},
        {"character": "凯亚", "character_id": "unknown", "trust": 0.2,
         "last_updated_ms": 200},
    ]}, ensure_ascii=False), encoding="utf-8")
    store = RelationshipStore(path, clock=clock)
    store.apply([{"character": "安柏", "trust": 1.0},
                 {"character": "琴", "trust": 1.0, "affection": 0.8}])
    records = {x["character"]: x for x in store.list()}
    assert len({x["character_id"] for x in records.values()}) == 3
    assert records["安柏"]["trust"] == pytest.approx(0.85)
    assert records["安柏"]["affection"] == pytest.approx(0.6)
    assert records["凯亚"]["trust"] == pytest.approx(0.2)
    assert records["琴"]["trust"] == pytest.approx(0.15)
    assert records["琴"]["affection"] == pytest.approx(0.15)
    assert records["安柏"]["last_updated_ms"] == 1000
    assert records["凯亚"]["last_updated_ms"] == 200


def test_relationship_collisions_unicode_normalization_and_reopen(tmp_path):
    path = tmp_path / "relationships.json"
    clock = Clock()
    names = ["安 柏", "安-柏", "😀", "😃", "甲" * 60 + "一", "甲" * 60 + "二"]
    store = RelationshipStore(path, clock=clock)
    store.apply([{"character": name, "trust": 0.1} for name in names])
    ids = {x["character"]: x["character_id"] for x in store.list()}
    assert len(ids) == len(names)
    assert len(set(ids.values())) == len(names)
    assert all(len(value) <= 60 for value in ids.values())
    reopened = RelationshipStore(path, clock=clock)
    reopened.apply([{"character": "安 柏", "trust": 0.8}])
    records = {x["character"]: x for x in reopened.list()}
    assert {name: item["character_id"] for name, item in records.items()} == ids
    assert records["安 柏"]["trust"] == pytest.approx(0.25)
    assert records["安-柏"]["trust"] == pytest.approx(0.1)
    # NFKC 等价的名称应指向同一角色，而不是生成新记录。
    reopened.apply([{"character": "Ａlice", "trust": 0.1}])
    alice_id = next(x["character_id"] for x in reopened.list() if x["character"] == "Ａlice")
    reopened.apply([{"character": "Alice", "trust": 0.3}])
    alice = [x for x in reopened.list() if x["character_id"] == alice_id]
    assert len(alice) == 1
    assert alice[0]["trust"] == pytest.approx(0.25)


def test_relationship_legacy_ids_and_unknown_migration_survive_reopen(tmp_path):
    path = tmp_path / "relationships.json"
    path.write_text(json.dumps({"schema_version": 1, "relationships": [
        {"character": "安 柏", "character_id": "unknown", "trust": 0.2},
        {"character": "凯亚", "character_id": "unknown", "trust": 0.3},
        {"character": "Kaeya 2", "character_id": "Kaeya-2", "trust": 0.4},
        {"character": "Kaeya-2", "character_id": "Kaeya-2", "trust": 0.5},
    ]}, ensure_ascii=False), encoding="utf-8")
    store = RelationshipStore(path, clock=Clock())
    records = {x["character"]: x for x in store.list()}
    ids = {name: item["character_id"] for name, item in records.items()}
    assert len(set(ids.values())) == 4
    assert ids["Kaeya 2"] == "Kaeya-2"
    assert ids["安 柏"] != "unknown" and ids["凯亚"] != "unknown"
    store.apply([{"character": "安 柏", "trust": 0.3}])
    reopened = RelationshipStore(path, clock=Clock())
    records = {x["character"]: x for x in reopened.list()}
    assert {name: item["character_id"] for name, item in records.items()} == ids
    assert records["Kaeya 2"]["trust"] == pytest.approx(0.4)
    assert records["Kaeya-2"]["trust"] == pytest.approx(0.5)
    reopened.apply([{"character": "Kaeya-2", "trust": 0.7}])
    records = {x["character"]: x for x in reopened.list()}
    assert records["Kaeya 2"]["trust"] == pytest.approx(0.4)
    assert records["Kaeya-2"]["trust"] == pytest.approx(0.65)
    assert records["Kaeya-2"]["character_id"] == ids["Kaeya-2"]


def test_relationship_event_capacity_survives_load(tmp_path):
    store = RelationshipStore(tmp_path / "relationships.json", clock=Clock())
    events = ["事件" + str(i) for i in range(12)]
    impressions = ["印象" + str(i) for i in range(10)]
    store.apply([{"character": "安柏", "memorable_events": events, "impressions": impressions}])
    store.apply([{"character": "凯亚", "trust": 0.1}])
    record = next(x for x in store.list() if x["character"] == "安柏")
    assert record["memorable_events"] == events
    assert record["impressions"] == impressions


def test_belief_full_transition_history_and_stable_timestamps(tmp_path):
    clock = Clock()
    store = BeliefStore(tmp_path / "beliefs.json", clock=clock)
    store.apply([{"proposition": "线索成立", "status": "suspicion", "confidence": 0.8}])
    clock.now = 2000
    store.apply([{"proposition": "另一条线索", "status": "belief"}])
    clock.now = 3000
    assert [x["proposition"] for x in store.list()] == ["另一条线索", "线索成立"]
    assert store.list()[1]["last_updated_ms"] == 1000
    # 不提供状态和置信度的补充证据不会制造状态转换。
    store.apply([{"proposition": "线索成立", "evidence": ["证据"]}])
    record = next(x for x in store.list() if x["proposition"] == "线索成立")
    assert record["status"] == "suspicion"
    assert record["confidence"] == pytest.approx(0.8)
    assert record["history"] == []
    for i in range(16):
        clock.now += 1
        store.apply([{"proposition": "线索成立", "status": "belief" if i % 2 == 0 else "suspicion"}])
    record = next(x for x in store.list() if x["proposition"] == "线索成立")
    assert record["history"] == ["suspicion -> belief", "belief -> suspicion"] * 8
    assert record["first_formed_ms"] == 1000
    assert record["last_updated_ms"] == clock.now
    other = next(x for x in store.list() if x["proposition"] == "另一条线索")
    assert other["last_updated_ms"] == 2000


def test_thread_patch_preserves_omitted_values_and_accepts_zero(tmp_path):
    clock = Clock()
    store = ThreadStore(tmp_path / "threads.json", clock=clock)
    store.apply([{"title": "线索", "status": "resolved", "importance": 0.9,
                  "current_theory": "理论", "characters": ["琴"], "evidence": ["证据甲"]}])
    clock.now = 2000
    store.apply([{"title": "其他线索", "status": "open"}])
    clock.now = 3000
    assert [x["title"] for x in store.list()] == ["其他线索", "线索"]
    store.apply([{"title": "线索", "evidence": ["证据乙"]}])
    record = store.list()[0]
    assert (record["status"], record["importance"], record["current_theory"]) == ("resolved", 0.9, "理论")
    assert record["characters"] == ["琴"]
    assert record["evidence"] == ["证据甲", "证据乙"]
    assert record["first_seen_ms"] == 1000
    assert record["last_referenced_ms"] == 3000
    assert next(x for x in store.list() if x["title"] == "其他线索")["last_referenced_ms"] == 2000
    store.apply([{"title": "线索", "importance": 0, "current_theory": ""}])
    record = store.list()[0]
    assert record["importance"] == 0
    assert record["current_theory"] == ""
    assert record["status"] == "resolved"
