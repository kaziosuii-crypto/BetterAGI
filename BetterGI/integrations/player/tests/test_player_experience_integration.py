#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于真实事件日志的经历检索、线索、反思及恢复测试。"""
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_event_log import PlayerEventLog  # noqa: E402
from player_experience import ExperienceError, HYPOTHESIS, PlayerExperience  # noqa: E402


@pytest.fixture
def stores(tmp_path):
    """每个测试拥有独立可重开的 WAL 日志。"""
    path = tmp_path / "events.db"
    with PlayerEventLog(path) as log:
        yield path, log, PlayerExperience(log)


def observe(memory, event_id, text, *, session="p:a", channel="visual", visibility="player", **extra):
    """写入合成的观察而不依赖游戏。"""
    return memory.record_observation(session_id=session, event_id=event_id, timestamp="t",
                                     source_id="source-" + event_id, channel=channel,
                                     text=text, visibility=visibility, **extra)


def test_retrieval_filters_before_limit_and_keeps_sources(stores):
    _, _, memory = stores
    observe(memory, "dialogue-a", "遗迹线索", channel="dialogue", speaker="候选甲", conversation_id="c")
    observe(memory, "dialogue-b", "遗迹附近", session="p:b", channel="dialogue")
    for index in range(8):
        observe(memory, f"visual-{index}", "其他画面", session="p:b")
        observe(memory, f"secret-{index}", "遗迹秘密", session="p:b", channel="dialogue", visibility="hidden")
    observe(memory, "other-player", "遗迹其他玩家", session="p2:a", channel="dialogue")
    recent = memory.recent_dialogue("p", limit=2)
    assert [entry["event_id"] for entry in recent] == ["dialogue-a", "dialogue-b"]
    assert recent[0]["speaker_status"] == "candidate"
    first = memory.retrieve(["p:a", "p:b"], query="遗迹", limit=1, scan_limit=100)
    assert first["events"][0]["event_id"] == "dialogue-b"
    second = memory.retrieve(["p:a", "p:b"], query="遗迹", limit=1, scan_limit=100,
                             before_seq=first["next_before_seq"])
    event = second["events"][0]
    assert event["event_id"] == "dialogue-a"
    assert event["payload"]["source_id"] == "source-dialogue-a"
    assert event["payload"]["speaker_status"] == "candidate"
    with pytest.raises(ExperienceError):
        memory.recent_dialogue("p:a")


def test_experience_recall_respects_verified_player_scope(stores):
    from player_cognitive_transaction import PlayerCognitiveTransaction
    _, log, memory = stores
    tx = PlayerCognitiveTransaction(log)
    tx.bind_session(session_id="p:own", player_scope="player-p", event_id="bind-own")
    tx.bind_session(session_id="p:foreign", player_scope="player-other", event_id="bind-other")
    for sid, suffix in (("p:own", "own"), ("p:foreign", "foreign"), ("p:unbound", "unbound")):
        observe(memory, "line-" + suffix, "遗迹 " + suffix, session=sid, channel="dialogue")
        observe(memory, "quest-" + suffix, "任务 " + suffix, session=sid, region="quest")
        memory.record_reflection(session_id=sid, event_id="reflection-" + suffix, timestamp="t",
            text="猜测 " + suffix, uncertainty="未知", evidence=[{"session_id": sid, "event_id": "line-" + suffix}])
        memory.record_observation(session_id=sid, event_id="conversation-line-" + suffix, timestamp="t",
            source_id="dialogue-frame", channel="dialogue", text="对话", visibility="player", conversation_id="c")
        memory.end_conversation(session_id=sid, event_id="conv-" + suffix, timestamp="t", conversation_id="c",
                                source_id="end", incomplete=True)
    assert [item["event_id"] for item in memory.recent_dialogue("p", player_scope="player-p")] == ["line-own", "conversation-line-own"]
    assert [item["event_id"] for item in memory.recent_region_observations("p", player_scope="player-p")] == ["quest-own"]
    assert [item["event_id"] for item in memory.recent_reflections("p", player_scope="player-p")] == ["reflection-own"]
    assert memory.recent_dialogue("p", limit=1, player_scope="player-p")[0]["event_id"] == "conversation-line-own"
    assert memory.recent_region_observations("p", limit=1, player_scope="player-p")[0]["event_id"] == "quest-own"
    assert memory.recent_reflections("p", limit=1, player_scope="player-p")[0]["event_id"] == "reflection-own"
    assert len(memory.recent_dialogue("p")) == 6
    assert memory.pending_conversations("p", player_scope="player-p") == [
        {"session_id": "p:own", "conversation_event_id": "conv-own"}]


def test_pending_conversation_page_preserves_cursor_and_player_scope(stores):
    _, log, memory = stores
    from player_cognitive_transaction import PlayerCognitiveTransaction
    tx = PlayerCognitiveTransaction(log)
    tx.bind_session(session_id="p:own", player_scope="player-p", event_id="bind-paging")
    tx.bind_session(session_id="p:other", player_scope="other", event_id="bind-other-paging")
    for index in range(11):
        session = "p:other" if index == 3 else "p:own"
        observe(memory, f"line-{index}", "台词", session=session, channel="dialogue", conversation_id=f"c{index}")
        memory.end_conversation(session_id=session, event_id=f"conv-{index}", timestamp="t",
                                conversation_id=f"c{index}", source_id="frame:end", incomplete=True)
    seen = []
    cursor = 0
    while True:
        page = memory.pending_conversation_page("p", player_scope="player-p", after_seq=cursor, limit=3)
        seen.extend(item["conversation_event_id"] for item in page["items"])
        if page["next_after_seq"] is None:
            break
        assert page["next_after_seq"] > cursor
        cursor = page["next_after_seq"]
    assert seen == [f"conv-{index}" for index in range(11) if index != 3]


def test_thread_reflection_uses_existing_events_and_reopens(stores):
    path, log, memory = stores
    observe(memory, "line", "前往遗迹", channel="dialogue", speaker="甲", conversation_id="c")
    memory.end_conversation(session_id="p:a", event_id="conv", timestamp="t",
                            conversation_id="c", source_id="frame:end", incomplete=True)
    refs = [{"session_id": "p:a", "event_id": "conv"}]
    memory.record_thread_update(session_id="p:b", event_id="thread1", timestamp="t",
                                thread_id="ruin", title="遗迹线索", status="open",
                                uncertainty="OCR 和人物身份未核实", evidence=refs,
                                current_theory="也许需要前往遗迹")
    memory.record_reflection(session_id="p:b", event_id="reflect", timestamp="t",
                             text="我还不确定遗迹的意义", uncertainty="对话可能不完整",
                             evidence=refs, thread_id="ruin")
    log.close()
    with PlayerEventLog(path) as reopened:
        memory = PlayerExperience(reopened)
        page = memory.retrieve(["p:a", "p:b"], query="遗迹", scan_limit=100)
        assert [event["event_id"] for event in page["events"]] == ["reflect", "thread1", "conv", "line"]
        reflection = page["events"][0]["payload"]
        assert reflection["verification"] == "subjective_unverified"
        source = memory.resolve_evidence(reflection["evidence"], session_ids=["p:a"])[0]
        assert source["payload"]["incomplete"] is True
        assert source["payload"]["turns"][0]["speaker_status"] == "candidate"
        assert source["payload"]["turns"][0]["source_id"] == "source-line"
        assert memory.recent_threads(["p:a", "p:b"])["events"][0]["event_id"] == "thread1"
        assert memory.retrieve(["p:b"])["events"] == []
        with pytest.raises(ExperienceError):
            memory.resolve_evidence(reflection["evidence"], session_ids=["p:b"])


def test_thread_updates_are_append_only_and_reject_hidden_sources(stores):
    path, log, memory = stores
    observe(memory, "visible", "文字", channel="dialogue")
    observe(memory, "hidden", "秘密", visibility="hidden")
    memory.record_claim(session_id="p:a", event_id="guess", timestamp="t", kind=HYPOTHESIS,
                        text="也许", evidence=[{"session_id": "p:a", "event_id": "visible"}], uncertainty="未知")
    memory.record_world_truth(session_id="p:a", event_id="truth", timestamp="t", source_id="world", text="秘密")
    for eid in ("hidden", "truth", "guess", "missing"):
        with pytest.raises(ExperienceError):
            memory.record_reflection(session_id="p:a", event_id="bad-" + eid, timestamp="t",
                                     text="推断", uncertainty="未知",
                                     evidence=[{"session_id": "p:a", "event_id": eid}])
    refs = [{"session_id": "p:a", "event_id": "visible"}]
    with PlayerEventLog(path) as other_log:
        for store, eid, status in ((memory, "first", "open"),
                                   (PlayerExperience(other_log), "second", "resolved")):
            store.record_thread_update(session_id="p:a", event_id=eid, timestamp="t", thread_id="same",
                                       title="线索", status=status, uncertainty="仍需验证", evidence=refs)
        assert memory.recent_threads(["p:a"])["events"][0]["event_id"] == "second"
        assert [item["event_id"] for item in log.list_events("p:a")][-2:] == ["first", "second"]


def test_retrieval_scan_budget_and_evidence_tampering(stores):
    _, _, memory = stores
    observe(memory, "old", "需要找的")
    for i in range(5):
        observe(memory, f"new{i}", "无关")
    page = memory.retrieve(["p:a"], query="需要", scan_limit=2)
    assert page["events"] == [] and page["scanned"] == 2
    page = memory.retrieve(["p:a"], query="需要", scan_limit=2, before_seq=page["next_before_seq"])
    page = memory.retrieve(["p:a"], query="需要", scan_limit=2, before_seq=page["next_before_seq"])
    assert page["events"][0]["event_id"] == "old"
    with pytest.raises(ExperienceError):
        memory.resolve_evidence([{"session_id": "p:a", "event_id": "old", "source_id": "forged"}],
                                session_ids=["p:a"])


def test_concurrent_thread_writes_keep_both_versions(stores):
    """两个连接并发追加同一线索，按数据库序号选择最新版本。"""
    path, log, memory = stores
    observe(memory, "source", "文字")
    barrier = threading.Barrier(2)
    refs = [{"session_id": "p:a", "event_id": "source"}]
    with PlayerEventLog(path) as other_log:
        def write(store, event_id):
            barrier.wait()
            return store.record_thread_update(session_id="p:a", event_id=event_id, timestamp="t",
                                              thread_id="same", title="线索", status="open",
                                              uncertainty="未知", evidence=refs)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, memory, "thread-a"),
                       pool.submit(write, PlayerExperience(other_log), "thread-b")]
            seqs = [future.result() for future in futures]
        rows = memory.retrieve(["p:a"], kinds=["thread_update"])["events"]
        assert {row["event_id"] for row in rows} == {"thread-a", "thread-b"}
        assert rows[0]["seq"] == max(seqs)
        assert memory.recent_threads(["p:a"])["events"][0]["seq"] == max(seqs)


def test_reflection_event_failure_leaves_no_partial_write(stores):
    path, log, memory = stores
    observe(memory, "source", "文本")
    log._db.execute("""CREATE TRIGGER fail_reflection BEFORE INSERT ON events
        WHEN NEW.type = 'experience.reflection' BEGIN SELECT RAISE(ABORT, 'failure'); END""")
    with pytest.raises(ValueError):
        memory.record_reflection(session_id="p:a", event_id="failed", timestamp="t", text="思考",
                                 uncertainty="未知", evidence=[{"session_id": "p:a", "event_id": "source"}])
    log.close()
    with PlayerEventLog(path) as reopened:
        assert [item["event_id"] for item in reopened.list_events("p:a")] == ["source"]


def test_v1_snapshot_migrates_once_and_old_writer_fails_closed(tmp_path):
    """模拟真实 v1 事件加完整投影，迁移不复制、不删除并阻止旧版覆盖。"""
    path = tmp_path / "legacy.db"
    entries = [
        {"kind": "raw_observation", "session_id": "p:a", "event_id": "old-line",
         "source_id": "frame:1", "timestamp": "t", "channel": "dialogue", "text": "旧对话",
         "visibility": "player", "conversation_id": "c"},
        {"kind": "raw_observation", "session_id": "p:a", "event_id": "world-only",
         "source_id": "private", "timestamp": "t", "channel": "dialogue", "text": "秘密",
         "visibility": "world_only", "conversation_id": "c"},
    ]
    with PlayerEventLog(path) as log:
        for index, entry in enumerate(entries):
            event = {"event_id": entry["event_id"], "session_id": "p:a", "timestamp": "t",
                     "type": "experience.raw_observation", "source": "player_experience", "payload": entry}
            log.append([event], projections={"experience.v1": {"version": 1, "entries": entries[:index + 1]}})
    # 还原为没有 v2 结构的真实 v1 形态。
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT state,revision FROM projections WHERE session_id='p:a'").fetchone()
        for trigger in ("experience_projection_insert_guard", "experience_projection_update_guard",
                        "experience_projection_delete_guard"):
            db.execute(f"DROP TRIGGER {trigger}")
        db.execute("DROP TABLE experience_entries")
        db.execute("DROP TABLE experience_sessions")
        db.execute("PRAGMA user_version=1")
    with PlayerEventLog(path) as log:
        memory = PlayerExperience(log)
        assert log.get_projection("p:a", "experience.v1")["state"]["entries"] == entries
        observe(memory, "new-line", "新对话", channel="dialogue", conversation_id="c")
        assert [row["event_id"] for row in memory.recall(["p:a"])] == ["old-line", "new-line"]
        assert [row["text"] for row in memory.conversations(["p:a"])[0]["turns"]] == ["旧对话", "新对话"]
        memory.end_conversation(session_id="p:a", event_id="end", timestamp="t", source_id="end",
                                conversation_id="c")
        assert [row["event_id"] for row in log.get_event("p:a", "end")["payload"]["turns"]] == ["old-line", "new-line"]
        assert log.get_projection("p:a", "experience.v1")["state"]["entries"][:3] == entries + [log.get_event("p:a", "new-line")["payload"]]
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT state,revision FROM projections WHERE session_id='p:a'").fetchone() == original
            assert db.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0] == 4
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE projections SET state='{}' WHERE session_id='p:a' AND name='experience.v1'")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("DELETE FROM projections WHERE session_id='p:a' AND name='experience.v1'")
        with pytest.raises(ValueError):
            log.append([{"event_id": "stale", "session_id": "p:a", "timestamp": "t", "type": "other",
                         "source": "old", "payload": {}}], projections={"experience.v1": {"version": 1, "entries": []}})
        assert log.get_event("p:a", "stale") is None
    with PlayerEventLog(path) as log:
        assert len(log.get_projection("p:a", "experience.v1")["state"]["entries"]) == 4
        assert log._db.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0] == 4
        assert log._db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_migration_failure_rolls_back_and_retries(tmp_path):
    """索引创建失败不留下半迁移状态，重启后可以完整重试。"""
    path = tmp_path / "retry.db"
    entry = {"kind": "raw_observation", "session_id": "s", "event_id": "old", "source_id": "source",
             "timestamp": "t", "channel": "dialogue", "text": "旧观察", "visibility": "player",
             "conversation_id": "c"}
    with PlayerEventLog(path) as log:
        log.append([{"event_id": "old", "session_id": "s", "timestamp": "t",
                     "type": "experience.raw_observation", "source": "player_experience", "payload": entry}],
                   projections={"experience.v1": {"version": 1, "entries": [entry]}})
        log._db.execute("""CREATE TRIGGER fail_migration BEFORE INSERT ON experience_entries
                           BEGIN SELECT RAISE(ABORT, 'test'); END""")
        with pytest.raises(ValueError):
            observe(PlayerExperience(log), "new", "新观察", session="s")
        assert log._db.execute("SELECT COUNT(*) FROM experience_sessions").fetchone()[0] == 0
        assert log._db.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0] == 0
        assert [row["event_id"] for row in log.list_events("s")] == ["old"]
        assert log.get_projection("s", "experience.v1")["state"]["entries"] == [entry]
        log._db.execute("DROP TRIGGER fail_migration")
    with PlayerEventLog(path) as log:
        observe(PlayerExperience(log), "new", "新观察", session="s")
        assert [row["event_id"] for row in PlayerExperience(log).recall(["s"])] == ["old", "new"]
        assert log._db.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0] == 2


def test_incremental_writes_and_indexed_conversation_query(stores):
    """长局观察写入不读取或重写完整投影，对话查询走复合索引。"""
    _, log, memory = stores
    observe(memory, "first", "第一条", channel="dialogue", conversation_id="target")
    statements = []
    log._db.set_trace_callback(statements.append)
    for index in range(200):
        observe(memory, f"v{index}", "画面")
    log._db.set_trace_callback(None)
    assert not any("projections" in sql.lower() for sql in statements)
    plan = log._db.execute("EXPLAIN QUERY PLAN SELECT seq FROM experience_entries "
                           "WHERE session_id=? AND conversation_id=? ORDER BY seq", ("p:a", "target")).fetchall()
    assert any("experience_conversation" in row[3] for row in plan)
    memory.end_conversation(session_id="p:a", event_id="end", timestamp="t",
                            source_id="end", conversation_id="target")
    assert [turn["event_id"] for turn in log.get_event("p:a", "end")["payload"]["turns"]] == ["first"]
    assert len(log.get_projection("p:a", "experience.v1")["state"]["entries"]) == 202
    revision = log.get_projection("p:a", "experience.v1")["revision"]
    auxiliary = {"event_id": "aux", "session_id": "p:a", "timestamp": "t", "type": "other",
                 "source": "test", "payload": {}}
    with pytest.raises(ValueError):
        log.append([auxiliary], expected_session_revisions={"experience.v1": 0})
    log.append([auxiliary], expected_session_revisions={"experience.v1": revision})


def test_unregistered_forged_source_never_becomes_evidence(stores):
    """直接伪造的事件不因同名类型或 source 字段获得经历登记资格。"""
    _, log, memory = stores
    observe(memory, "real", "真正观察")
    forged = {"kind": "raw_observation", "session_id": "p:a", "event_id": "forged",
              "source_id": "fake", "timestamp": "t", "channel": "dialogue", "text": "伪造",
              "visibility": "player", "conversation_id": "c"}
    log.append([{"event_id": "forged", "session_id": "p:a", "timestamp": "t",
                 "type": "experience.raw_observation", "source": "player_experience", "payload": forged}])
    with pytest.raises(ExperienceError):
        memory.record_claim(session_id="p:a", event_id="bad", timestamp="t", kind=HYPOTHESIS,
                            text="猜测", uncertainty="未知", evidence=[{"session_id": "p:a", "event_id": "forged"}])
    with pytest.raises(ExperienceError):
        memory.resolve_evidence([{"session_id": "p:a", "event_id": "forged"}], session_ids=["p:a"])
    assert [row["event_id"] for row in memory.retrieve(["p:a"])["events"]] == ["real"]
    assert memory.recent_dialogue("p") == []
    assert memory.conversations(["p:a"]) == []


def test_process_writers_share_incremental_index(tmp_path):
    """独立 Python 进程并发写入同一 SQLite 时均保留且无需快照冲突重试。"""
    path = tmp_path / "process.db"
    with PlayerEventLog(path):
        pass
    script = """import sys
sys.path.insert(0, sys.argv[2])
from player_event_log import PlayerEventLog
from player_experience import PlayerExperience
with PlayerEventLog(sys.argv[1], timeout=30) as log:
    memory = PlayerExperience(log)
    for i in range(40):
        eid = sys.argv[3] + str(i)
        memory.record_observation(session_id='s', event_id=eid, timestamp='t', source_id=eid,
                                  channel='visual', text=eid, visibility='player')
"""
    player_dir = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(path), player_dir, prefix],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                 for prefix in ("a", "b")]
    for process in processes:
        _, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr.decode(errors="replace")
    with PlayerEventLog(path) as log:
        rows = PlayerExperience(log).recall(["s"])
        assert len(rows) == 80
        assert {row["event_id"] for row in rows} == {f"{prefix}{i}" for prefix in ("a", "b") for i in range(40)}
        assert len(log.get_projection("s", "experience.v1")["state"]["entries"]) == 80
