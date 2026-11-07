#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立验证事件日志、事务、WAL、恢复、数据校验和并发。"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import player_event_log as log  # noqa: E402


def event(event_id="e1", session_id="s1", **kwargs):
    """构造一条合法的最小事件。"""
    result = {"event_id": event_id, "session_id": session_id, "timestamp": "2026-01-01T00:00:00Z",
              "frame_seq": 7, "type": "observe", "payload": {"text": "蒙德"}, "source": "vision",
              "caused_by": None, "intent_id": "i1"}
    result.update(kwargs)
    return result


def test_roundtrip_wal_and_reopen(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as store:
        assert store.append([event(), event("e2", caused_by="e1")], projections={"belief": {"place": "蒙德"}}) == [1, 2]
        assert store.get_projection("s1", "belief") == {"state": {"place": "蒙德"}, "revision": 2}
        assert [e["event_id"] for e in store.list_events("s1", after_seq=1)] == ["e2"]
        assert store.list_events("s2") == []
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA user_version").fetchone()[0] == log.SCHEMA_VERSION
    with log.PlayerEventLog(path) as store:
        assert store.list_events("s1")[0] == {"seq": 1, **event()}
        assert store.get_projection("s1", "belief")["state"] == {"place": "蒙德"}


def test_duplicate_rolls_back_full_batch_and_projection(tmp_path):
    with log.PlayerEventLog(tmp_path / "events.db") as store:
        store.append([event()], projections={"state": {"v": 1}})
        with pytest.raises(log.EventLogConflict):
            store.append([event("e2"), event()], projections={"state": {"v": 2}})
        assert [e["event_id"] for e in store.list_events("s1")] == ["e1"]
        assert store.get_projection("s1", "state") == {"state": {"v": 1}, "revision": 1}


def test_failure_after_event_insert_rolls_back(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as store:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_projection BEFORE INSERT ON projections BEGIN SELECT RAISE(ABORT, 'failed'); END")
        with pytest.raises(log.EventLogError):
            store.append([event()], projections={"state": {"v": 1}})
        assert store.list_events("s1") == []
        assert store.get_projection("s1", "state") is None


def test_reader_cannot_see_uncommitted_event_or_projection(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as reader:
        with sqlite3.connect(path, isolation_level=None) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("INSERT INTO events(event_id,session_id,timestamp,type,payload,source) VALUES(?,?,?,?,?,?)", ("e1", "s1", "t", "observe", "{}", "test"))
            writer.execute("INSERT INTO projections VALUES(?,?,?,?)", ("s1", "state", '{"v":1}', 1))
            assert reader.list_events("s1") == []
            assert reader.get_projection("s1", "state") is None
            writer.execute("COMMIT")
        assert reader.list_events("s1")[0]["event_id"] == "e1"
        assert reader.get_projection("s1", "state")["state"] == {"v": 1}


def test_revision_compare_and_session_isolation(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as a, log.PlayerEventLog(path) as b:
        a.append([event()], projections={"state": 1}, expected_revisions={"state": 0})
        old = b.get_projection("s1", "state")["revision"]
        a.append([event("e2")], projections={"state": 2}, expected_revisions={"state": old})
        with pytest.raises(log.EventLogConflict):
            b.append([event("e3")], projections={"state": 3}, expected_revisions={"state": old})
        b.append([event("e4", session_id="s2")], projections={"state": 4}, expected_revisions={"state": 0})
        assert a.get_projection("s1", "state")["state"] == 2
        assert a.get_projection("s2", "state")["state"] == 4
        assert [e["event_id"] for e in a.list_events("s1")] == ["e1", "e2"]


def test_concurrent_writers_serialized_and_unique(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as a, log.PlayerEventLog(path) as b:
        barrier = threading.Barrier(2)
        def write(store, prefix):
            barrier.wait()
            for i in range(20):
                store.append([event(f"{prefix}-{i}")])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, a, "a"), pool.submit(write, b, "b")]
            for future in futures:
                future.result()
        rows = a.list_events("s1")
        assert len(rows) == 40
        assert len({e["event_id"] for e in rows}) == 40
        assert [e["seq"] for e in rows] == sorted(e["seq"] for e in rows)


def test_concurrent_compare_and_swap_has_one_winner(tmp_path):
    """两个写者基于相同投影版本写入时，只有一个事务可以提交。"""
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as a, log.PlayerEventLog(path) as b:
        a.append([event()], projections={"state": 0})
        barrier = threading.Barrier(2)
        def write(store, event_id):
            barrier.wait()
            try:
                store.append([event(event_id)], projections={"state": event_id}, expected_revisions={"state": 1})
                return "committed"
            except log.EventLogConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, a, "e2"), pool.submit(write, b, "e3")]
            assert sorted(f.result() for f in futures) == ["committed", "conflict"]
        assert len(a.list_events("s1")) == 2
        assert a.get_projection("s1", "state")["state"] in {"e2", "e3"}


@pytest.mark.parametrize("payload", [float("nan"), float("inf"), {"x": -float("inf")}, {1: "value"}, {"bad": object()}])
def test_invalid_json_rejected_without_writes(tmp_path, payload):
    with log.PlayerEventLog(tmp_path / "events.db") as store:
        with pytest.raises(log.EventLogError):
            store.append([event(payload=payload)], projections={"state": {"ok": True}})
        assert store.list_events("s1") == []
        assert store.get_projection("s1", "state") is None


@pytest.mark.parametrize("payload", [{"nested": [{"accessToken": "danger"}]}, {"note": "Bearer abcdefghijklmnop"}, {"note": "sk-abcdefghijklmnop"}, {"note": "token=verysecret"}])
def test_secrets_rejected_without_writes(tmp_path, payload):
    with log.PlayerEventLog(tmp_path / "events.db") as store:
        with pytest.raises(log.EventLogError):
            store.append([event(payload=payload)])
        with pytest.raises(log.EventLogError):
            store.append([event()], projections={"state": payload})
        assert store.list_events("s1") == []


def test_input_validation_and_parameterized_sql(tmp_path):
    with log.PlayerEventLog(tmp_path / "events.db") as store:
        sid = "s' OR 1=1 --"
        store.append([event("id'", session_id=sid)], projections={"x'); DROP TABLE events; --": {"v": 1}})
        assert len(store.list_events(sid)) == 1
        assert store.list_events("s") == []
        for events in ([], [event(frame_seq=True)], [event(frame_seq=-1)], [event(), event("e2", session_id="other")], [event(extra="bad")]):
            with pytest.raises(log.EventLogError):
                store.append(events)
        with pytest.raises(log.EventLogError):
            store.append([event("next")], projections={"s": {}}, expected_revisions={"s": -1})


def test_database_versions_and_corruption_fail_closed(tmp_path):
    future = tmp_path / "future.db"
    with sqlite3.connect(future) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(log.EventLogSchemaError, match="高于"):
        log.PlayerEventLog(future)
    with sqlite3.connect(future) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999
    foreign = tmp_path / "foreign.db"
    with sqlite3.connect(foreign) as db:
        db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(log.EventLogSchemaError, match="未版本化"):
        log.PlayerEventLog(foreign)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(log.EventLogSchemaError):
        log.PlayerEventLog(corrupt)
    malformed = tmp_path / "malformed.db"
    with sqlite3.connect(malformed) as db:
        db.execute("PRAGMA user_version=1")
    with pytest.raises(log.EventLogSchemaError, match="schema"):
        log.PlayerEventLog(malformed)


def test_corrupt_stored_json_rejected_on_read(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as store:
        store.append([event()], projections={"state": {"v": 1}})
        with sqlite3.connect(path) as db:
            db.execute("UPDATE events SET payload=?", ("NaN",))
            db.execute("UPDATE projections SET state=?", (json.dumps({"x": float("inf")}),))
        with pytest.raises(log.EventLogSchemaError, match="payload"):
            store.list_events("s1")
        with pytest.raises(log.EventLogSchemaError, match="projection"):
            store.get_projection("s1", "state")


def test_bounded_cross_session_queries_and_cursor(tmp_path):
    """跨会话检索先按类型与内容过滤，再按全局提交序分页。"""
    with log.PlayerEventLog(tmp_path / "events.db") as store:
        store.append([event("a", session_id="p:a", type="experience.raw_observation",
                            payload={"visibility": "player", "channel": "dialogue", "text": "a"})])
        store.append([event("hidden", session_id="p:b", type="experience.raw_observation",
                            payload={"visibility": "hidden", "channel": "dialogue"})])
        store.append([event("b", session_id="p:b", type="experience.raw_observation",
                            payload={"visibility": "player", "channel": "dialogue", "text": "b"})])
        for i in range(5):
            store.append([event(f"visual-{i}", session_id="p:b", type="experience.raw_observation",
                                payload={"visibility": "player", "channel": "visual"})])
        filters = dict(session_ids=["p:a", "p:b"], event_types=["experience.raw_observation"],
                       visibility="player", channel="dialogue", limit=1)
        first = store.query_events(**filters)
        assert first[0]["event_id"] == "b"
        second = store.query_events(**filters, before_seq=first[0]["seq"])
        assert second[0]["event_id"] == "a"
        assert store.query_events(**filters, before_seq=second[0]["seq"]) == []
        oldest = store.query_events(**filters, after_seq=0)
        assert oldest[0]["event_id"] == "a"
        assert store.query_events(**filters, after_seq=oldest[0]["seq"])[0]["event_id"] == "b"
        with pytest.raises(log.EventLogError):
            store.query_events(**filters, before_seq=10, after_seq=0)
        assert store.get_event("p:b", "a") is None
        assert store.get_event("p:a", "a")["payload"]["text"] == "a"
        for kwargs in ({}, {"session_ids": []}, {"session_ids": ["p:a"], "session_prefix": "p:"},
                       {"session_ids": ["p:a", "p:a"]}, {"session_ids": ["p:a"], "limit": 201}):
            with pytest.raises(log.EventLogError):
                store.query_events(event_types=["experience.raw_observation"], **kwargs)


def test_existing_v1_database_gets_read_indexes_without_losing_events(tmp_path):
    """已存在的 v1 数据库可以安全补建索引并保留事件。"""
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as store:
        store.append([event()])
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX events_type_seq")
        db.execute("DROP INDEX events_session_type_seq")
    with log.PlayerEventLog(path) as store:
        assert store.get_event("s1", "e1")["event_id"] == "e1"
        names = {row[1] for row in store._db.execute("PRAGMA index_list(events)")}
        assert {"events_type_seq", "events_session_type_seq"} <= names
        assert store._db.execute("PRAGMA user_version").fetchone()[0] == log.SCHEMA_VERSION


def test_scoped_projections_are_separate_from_sessions_and_cas(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as store:
        store.append([event("local", session_id="alice")], projections={"x": "session"})
        first = store.append([event("a", session_id="s1")], projections={"x": "player"},
                             projection_scope="alice", expected_revisions={"x": 0})[0]
        assert store.get_projection("alice", "x")["state"] == "session"
        assert store.get_projection("s2", "x", projection_scope="alice")["state"] == "player"
        assert store.get_projection("s1", "x", projection_scope="bob") is None
        with pytest.raises(log.EventLogConflict):
            store.append([event("stale", session_id="s2")], projections={"x": "lost"},
                         projection_scope="alice", expected_revisions={"x": 0})
        assert store.get_event("s2", "stale") is None
        second = store.append([event("b", session_id="s2")], projections={"x": "updated"},
                              projection_scope="alice", expected_revisions={"x": first})[0]
        assert store.get_projection("s1", "x", projection_scope="alice")["revision"] == second
        assert store.get_event("s2", "b")["session_id"] == "s2"
        for scope in ("", "a/b", " alice", "a b", "x" * 129, 42):
            with pytest.raises(log.EventLogError):
                store.append([event("invalid")], projections={"x": 1}, projection_scope=scope)
        assert store.get_event("s1", "invalid") is None


def test_scoped_projection_migration_and_atomic_failure(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path) as store:
        store.append([event()], projections={"old": "kept"})
    # Simulate an existing v1 database created before the optional extension.
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE scoped_projections")
    with log.PlayerEventLog(path) as store:
        assert store.get_projection("s1", "old")["state"] == "kept"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_scoped BEFORE INSERT ON scoped_projections BEGIN SELECT RAISE(ABORT, 'failed'); END")
        with pytest.raises(log.EventLogError):
            store.append([event("scoped")], projections={"x": 1}, projection_scope="alice", expected_revisions={"x": 0})
        assert store.get_event("s1", "scoped") is None
        assert store.get_projection("s1", "x", projection_scope="alice") is None


def test_scoped_projection_rejects_bad_existing_schema(tmp_path):
    path = tmp_path / "events.db"
    with log.PlayerEventLog(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE scoped_projections")
        db.execute("CREATE TABLE scoped_projections(scope TEXT)")
    with pytest.raises(log.EventLogSchemaError, match="schema"):
        log.PlayerEventLog(path)


def test_close_and_memory_rejected(tmp_path):
    with pytest.raises(log.EventLogError):
        log.PlayerEventLog(":memory:")
    store = log.PlayerEventLog(tmp_path / "events.db")
    store.close()
    store.close()
    with pytest.raises(log.EventLogError, match="关闭"):
        store.list_events("s1")
