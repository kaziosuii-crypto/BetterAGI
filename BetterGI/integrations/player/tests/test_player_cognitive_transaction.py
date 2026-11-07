"""主动认知事务：可见来源、完整投影、CAS 与 SQLite 回滚。"""
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_cognitive_transaction import CognitiveTransactionError, PlayerCognitiveTransaction, PROJECTION
from player_event_log import EventLogConflict, EventLogError, PlayerEventLog
from player_experience import PlayerExperience


def observe(log, eid="obs", session="s", visibility="player", text="看见派蒙"):
    PlayerExperience(log).record_observation(session_id=session, event_id=eid, timestamp="2026-01-01T00:00:00Z",
                                             source_id="sensor-1", channel="visual", text=text, visibility=visibility)


def commit(tx, eid="turn", source="obs", session="s", updates=None, **kw):
    return tx.commit(session_id=session, source_event_id=source, event_id=eid, thought="也许是派蒙",
                     intention={"goal": "打招呼"}, speech={"text": "你好"}, updates=updates, now_ms=1000, **kw)


def test_speech_reservation_survives_restart_and_never_reauthorizes(tmp_path):
    path = tmp_path / "speech.db"
    binding = {"session_id": "recording", "instance": "instance-1"}
    speech = {"type": "speech", "source": "persistent_player", "text": "你好",
              "generated_at_ms": 1000, "thought_id": "turn"}
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="player-p", event_id="bind")
        observe(log)
        commit(tx, player_scope="player-p")
        reservation = tx.begin_speech_delivery(session_id="s", player_scope="player-p", thought_id="turn",
                                               decision_id="decision", binding=binding, speech=speech)
        assert reservation["status"] == "created" and reservation["already_recorded"] is False
        with pytest.raises(CognitiveTransactionError, match="其他内容"):
            tx.begin_speech_delivery(session_id="s", player_scope="player-p", thought_id="turn",
                                     decision_id="decision", binding={**binding, "instance": "other"}, speech=speech)
    with PlayerEventLog(path) as reopened:
        tx = PlayerCognitiveTransaction(reopened)
        args = dict(session_id="s", player_scope="player-p", thought_id="turn", decision_id="decision",
                    binding=binding, speech=speech)
        assert tx.begin_speech_delivery(**args) == {"already_recorded": True, "status": "unknown"}
        with pytest.raises(CognitiveTransactionError, match="不能重试"):
            tx.finish_speech_delivery(**args, status="timeline_written", receipt=speech)
        result = tx.finish_speech_delivery(**args, status="unknown")
        assert result["status"] == "unknown" and result["already_recorded"]
        with pytest.raises(CognitiveTransactionError, match="不能重试"):
            tx.finish_speech_delivery(**args, status="timeline_written", receipt=speech)
        assert [row["payload"]["status"] for row in reopened.list_events("s") if row["type"] == "cognitive.speech"] == ["created", "unknown"]


def test_speech_receipt_requires_original_source_and_is_idempotent(tmp_path):
    with PlayerEventLog(tmp_path / "speech.db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="player-p", event_id="bind")
        observe(log)
        commit(tx, player_scope="player-p")
        binding = {"session_id": "recording", "instance": "one"}
        speech = {"type": "speech", "source": "persistent_player", "text": "你好",
                  "generated_at_ms": 1000, "thought_id": "turn"}
        args = dict(session_id="s", player_scope="player-p", thought_id="turn", decision_id="decision",
                    binding=binding, speech=speech)
        with pytest.raises(CognitiveTransactionError, match="文本"):
            tx.begin_speech_delivery(**{**args, "speech": {**speech, "text": "被改写"}})
        tx.begin_speech_delivery(**args)
        observe(log, eid="obs-two")
        commit(tx, eid="turn-two", source="obs-two", player_scope="player-p")
        with pytest.raises(CognitiveTransactionError, match="decision"):
            tx.begin_speech_delivery(**{**args, "thought_id": "turn-two",
                                        "speech": {**speech, "thought_id": "turn-two"}})
        with pytest.raises(CognitiveTransactionError, match="回执"):
            tx.finish_speech_delivery(**args, status="timeline_written", receipt={**speech, "text": "被改写"})
        assert tx.begin_speech_delivery(**args)["status"] == "unknown"
        speech = {**speech, "thought_id": "turn-two"}
        args = {**args, "thought_id": "turn-two", "decision_id": "decision-two", "speech": speech}
        tx.begin_speech_delivery(**args)
        receipt = {**speech, "t": 1, "dur": 500}
        result = tx.finish_speech_delivery(**args, status="timeline_written", receipt=receipt)
        assert result["status"] == "timeline_written"
        event = log.get_event("s", result["event_id"])
        created = log.get_event("s", event["caused_by"])
        assert created["caused_by"] == "turn-two" and event["payload"]["receipt"]["text"] == "你好"
        assert tx.finish_speech_delivery(**args, status="timeline_written", receipt=receipt)["already_recorded"]


def test_speech_unknown_log_failure_revokes_live_success_claim(tmp_path, monkeypatch):
    with PlayerEventLog(tmp_path / "speech.db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="player-p", event_id="bind")
        observe(log)
        commit(tx, player_scope="player-p")
        speech = {"type": "speech", "source": "persistent_player", "text": "你好",
                  "generated_at_ms": 1000, "thought_id": "turn"}
        args = dict(session_id="s", player_scope="player-p", thought_id="turn", decision_id="decision",
                    binding={"session_id": "recording", "instance": "one"}, speech=speech)
        tx.begin_speech_delivery(**args)
        original = log.append
        monkeypatch.setattr(log, "append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")))
        with pytest.raises(OSError):
            tx.finish_speech_delivery(**args, status="unknown")
        monkeypatch.setattr(log, "append", original)
        with pytest.raises(CognitiveTransactionError, match="跨进程"):
            tx.finish_speech_delivery(**args, status="timeline_written", receipt={**speech, "t": 0, "dur": 500})
        assert tx.begin_speech_delivery(**args)["status"] == "unknown"


def test_concurrent_speech_reservation_has_only_one_sender(tmp_path):
    path = tmp_path / "speech.db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="player-p", event_id="bind")
        observe(log)
        commit(tx, player_scope="player-p")
    args = {"session_id": "s", "player_scope": "player-p", "thought_id": "turn", "decision_id": "decision",
            "binding": {"session_id": "recording", "instance": "one"},
            "speech": {"type": "speech", "source": "persistent_player", "text": "你好",
                       "generated_at_ms": 1000, "thought_id": "turn"}}
    barrier = Barrier(2)
    def reserve():
        with PlayerEventLog(path) as log:
            tx = PlayerCognitiveTransaction(log)
            barrier.wait()
            try:
                return tx.begin_speech_delivery(**args)
            except EventLogConflict:
                return {"already_recorded": True, "status": "unknown"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(result["already_recorded"] is False for result in results) == 1
    with PlayerEventLog(path) as log:
        assert len([row for row in log.list_events("s") if row["type"] == "cognitive.speech"]) == 1
        assert PlayerCognitiveTransaction(log).begin_speech_delivery(**args)["status"] == "unknown"


def test_live_writer_cannot_be_terminated_by_recovery_or_competing_transaction(tmp_path):
    path = tmp_path / "speech.db"
    speech = {"type": "speech", "source": "persistent_player", "text": "你好",
              "generated_at_ms": 1000, "thought_id": "turn"}
    args = dict(session_id="s", player_scope="player-p", thought_id="turn", decision_id="decision",
                binding={"session_id": "recording", "instance": "one"}, speech=speech)
    with PlayerEventLog(path) as owner_log, PlayerEventLog(path) as other_log:
        owner, other = PlayerCognitiveTransaction(owner_log), PlayerCognitiveTransaction(other_log)
        owner.bind_session(session_id="s", player_scope="player-p", event_id="bind")
        observe(owner_log)
        commit(owner, player_scope="player-p")
        owner.begin_speech_delivery(**args)
        assert other.recover_speech_deliveries(session_id="s", player_scope="player-p") == {"unknown": 0, "active": 1}
        assert other.begin_speech_delivery(**args) == {"already_recorded": True, "status": "unknown"}
        with pytest.raises(CognitiveTransactionError, match="非持有者"):
            other.finish_speech_delivery(**args, status="unknown")
        with pytest.raises(CognitiveTransactionError, match="绑定不匹配"):
            owner.finish_speech_delivery(**{**args, "binding": {"session_id": "other", "instance": "one"}},
                                         status="unknown")
        assert other.recover_speech_deliveries(session_id="s", player_scope="player-p")["active"] == 1
        receipt = {**speech, "t": 0, "dur": 500}
        owner.finish_speech_delivery(**args, status="timeline_written", receipt=receipt)
        assert other.begin_speech_delivery(**args) == {"already_recorded": True, "status": "timeline_written"}
        rows = [row for row in owner_log.list_events("s") if row["type"] == "cognitive.speech"]
        assert [row["payload"]["status"] for row in rows] == ["created", "timeline_written"]


def test_process_exit_allows_persistent_unknown_recovery(tmp_path):
    path = tmp_path / "speech.db"
    speech = {"type": "speech", "source": "persistent_player", "text": "你好",
              "generated_at_ms": 1000, "thought_id": "turn"}
    args = dict(session_id="p:s", player_scope="player-p", thought_id="turn", decision_id="decision",
                binding={"session_id": "recording", "instance": "one"}, speech=speech)
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="p:s", player_scope="player-p", event_id="bind")
        observe(log, session="p:s")
        commit(tx, session="p:s", player_scope="player-p")
    code = ("import json,sys; from player_event_log import PlayerEventLog; "
            "from player_cognitive_transaction import PlayerCognitiveTransaction; "
            "log=PlayerEventLog(sys.argv[1]); tx=PlayerCognitiveTransaction(log); "
            "assert not tx.begin_speech_delivery(**json.loads(sys.argv[2]))['already_recorded']; "
            "import os; os._exit(0)")
    subprocess.run([sys.executable, "-c", code, str(path), json.dumps(args, ensure_ascii=True)],
                   cwd=str(Path(__file__).resolve().parents[1]), check=True, timeout=15)
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        assert tx.recover_player_speech_deliveries(session_prefix="p:", player_scope="player-p") == {"unknown": 1, "active": 0}
        assert tx.begin_speech_delivery(**args) == {"already_recorded": True, "status": "unknown"}
        assert [row["payload"]["status"] for row in log.list_events("p:s") if row["type"] == "cognitive.speech"] == ["created", "unknown"]
    with PlayerEventLog(path) as log:
        assert PlayerCognitiveTransaction(log).begin_speech_delivery(**args)["status"] == "unknown"


def test_other_process_recovery_cannot_override_live_writer(tmp_path):
    path = tmp_path / "speech.db"
    speech = {"type": "speech", "source": "persistent_player", "text": "你好",
              "generated_at_ms": 1000, "thought_id": "turn"}
    args = dict(session_id="p:s", player_scope="player-p", thought_id="turn", decision_id="decision",
                binding={"session_id": "recording", "instance": "one"}, speech=speech)
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="p:s", player_scope="player-p", event_id="bind")
        observe(log, session="p:s")
        commit(tx, session="p:s", player_scope="player-p")
    code = ("import json,sys; from player_event_log import PlayerEventLog; "
            "from player_cognitive_transaction import PlayerCognitiveTransaction; "
            "log=PlayerEventLog(sys.argv[1]); tx=PlayerCognitiveTransaction(log); "
            "args=json.loads(sys.argv[2]); tx.begin_speech_delivery(**args); "
            "print('ready',flush=True); input(); "
            "tx.finish_speech_delivery(**args,status='timeline_written',receipt={**args['speech'],'t':0,'dur':500})")
    proc = subprocess.Popen([sys.executable, "-c", code, str(path), json.dumps(args, ensure_ascii=True)],
                            cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        with PlayerEventLog(path) as log:
            other = PlayerCognitiveTransaction(log)
            assert other.recover_player_speech_deliveries(session_prefix="p:", player_scope="player-p") == {"unknown": 0, "active": 1}
            with pytest.raises(CognitiveTransactionError, match="非持有者"):
                other.finish_speech_delivery(**args, status="unknown")
        proc.stdin.write("continue\n")
        proc.stdin.flush()
        assert proc.wait(timeout=15) == 0
        with PlayerEventLog(path) as log:
            assert PlayerCognitiveTransaction(log).begin_speech_delivery(**args)["status"] == "timeline_written"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def test_recent_narrative_requires_matching_player_binding(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        for session, player_id in (("p:good", "p"), ("p:foreign", "other")):
            scope = "player-" + hashlib.sha256(player_id.encode("utf-8")).hexdigest()
            tx.bind_session(session_id=session, player_scope=scope, event_id="bind-" + player_id)
            observe(log, "obs-" + player_id, session)
            commit(tx, "turn-" + player_id, "obs-" + player_id, session, player_scope=scope)
        events = PlayerExperience(log).recent_narrative("p")
        assert [(row["session_id"], row["event_id"]) for row in events] == [("p:good", "turn-p")]


def test_dialogue_decision_requires_same_frame_registered_choice(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        memory = PlayerExperience(log)
        tx = PlayerCognitiveTransaction(log)
        scope = "player-p"
        tx.bind_session(session_id="p:s", player_scope=scope, event_id="bind-dialogue")
        frame_id, text = "frame-1", "一起去"
        bounds = {"x": 0.8, "y": 0.2, "width": 0.2, "height": 0.05}
        raw = json.dumps(["p:s", frame_id, 0, text, bounds], ensure_ascii=True,
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
        option_id = "choice-" + hashlib.sha256(raw.encode("ascii")).hexdigest()
        memory.record_observation(session_id="p:s", event_id=option_id, timestamp="t",
                                  source_id="frame:" + frame_id + ":sha256:" + "a" * 64, channel="choice", text=text,
                                  visibility="player", choice_index=0, choice_bounds=bounds)
        visual_seq = memory.record_observation(session_id="p:s", event_id="visual", timestamp="t",
                                               source_id="observe:" + frame_id + ":sha256:" + "a" * 64,
                                               channel="visual", text="一帧画面", visibility="player")
        for bad in ({"action": "select", "frame_id": "other", "option_id": option_id},
                    {"action": "select", "frame_id": frame_id, "option_id": "missing"}):
            with pytest.raises(CognitiveTransactionError):
                commit(tx, "bad-" + bad["frame_id"], "visual", "p:s", player_scope=scope,
                       dialogue_decision=bad)
        result = commit(tx, "choice-turn", "visual", "p:s", player_scope=scope,
                        dialogue_decision={"action": "select", "frame_id": frame_id, "option_id": option_id})
        decision = log.get_event("p:s", result["event_id"])["payload"]["dialogue_decision"]
        assert decision == {"action": "select", "frame_id": frame_id, "option_id": option_id,
                            "status": "proposed_not_executed"}
        assert memory.retrieve(["p:s"], kinds=["experienced_fact"])["events"] == []
        observe(log, "later", "p:s")
        with pytest.raises(EventLogConflict):
            commit(tx, "stale-choice", "visual", "p:s", player_scope=scope,
                   dialogue_decision={"action": "select", "frame_id": frame_id, "option_id": option_id},
                   expected_dialogue_experience_revision=visual_seq)
        assert log.get_event("p:s", "stale-choice") is None


def test_three_sessions_survive_separate_process_restarts(tmp_path):
    path = tmp_path / "journey.sqlite"
    player_dir = str(Path(__file__).resolve().parents[1])
    scope = "player-" + hashlib.sha256(b"lumi").hexdigest()
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from player_cognitive_transaction import PlayerCognitiveTransaction
from player_event_log import PlayerEventLog
from player_experience import PlayerExperience
path, scope, n = sys.argv[2], sys.argv[3], sys.argv[4]
session = 'lumi:session-' + n
with PlayerEventLog(path) as log:
    tx = PlayerCognitiveTransaction(log)
    tx.bind_session(session_id=session, player_scope=scope, event_id='binding-' + n)
    PlayerExperience(log).record_observation(session_id=session, event_id='source-' + n,
        timestamp='2026-01-01T00:00:00Z', source_id='frame-' + n, channel='dialogue',
        text='安柏说遗迹线索 ' + n, visibility='player', speaker='安柏')
    tx.commit(session_id=session, player_scope=scope, source_event_id='source-' + n,
        event_id='turn-' + n, thought='继续寻找遗迹线索 ' + n, intention={'goal':'寻找遗迹'},
        speech=None, now_ms=1700000000000 + int(n) * 1000,
        updates={'relationships':[{'character':'安柏','trust':1.0}], 'emotion':{'curiosity':1.0}})
"""
    for number in range(1, 4):
        result = subprocess.run([sys.executable, "-c", script, player_dir, str(path), scope, str(number)],
                                capture_output=True, text=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        state = tx.read("lumi:session-3", player_scope=scope)["state"]
        assert state["relationships"][0]["trust"] == 0.45
        assert state["emotion"]["curiosity"] > 0.9
        memory = PlayerExperience(log)
        assert [row["text"] for row in memory.recent_dialogue("lumi")] == [
            "安柏说遗迹线索 1", "安柏说遗迹线索 2", "安柏说遗迹线索 3"]
        assert len(memory.recent_narrative("lumi")) == 3


def test_roundtrip_patch_clamps_history_and_reactivation(tmp_path):
    path = tmp_path / "db.sqlite"
    with PlayerEventLog(path) as log:
        observe(log)
        tx = PlayerCognitiveTransaction(log)
        first = commit(tx, updates={
            "working": {"location": "蒙德", "characters": ["派蒙"], "last_motor_feedback": {"x": 1, "y": 2}},
            "beliefs": [{"proposition": "派蒙在附近", "status": "known_fact", "confidence": 0.9}],
            "relationships": [{"character": "派蒙", "trust": 1, "affection": 1}],
            "emotion": {"joy": 1, "fear": 0.5},
            "threads": [{"title": "寻找派蒙", "status": "dormant", "importance": 0.8}],
            "facts": [{"proposition": "派蒙在附近", "confidence": 0.9}],
        })
        state = first["state"]
        assert state["beliefs"][0]["status"] == "belief"
        assert state["facts"][0]["verification"] == "subjective_unverified"
        assert state["relationships"][0]["trust"] == 0.15
        assert state["emotion"]["joy"] == 0.35
        observe(log, "obs2")
        second = commit(tx, "turn2", "obs2", expected_revision=first["revision"], updates={
            "working": {"last_motor_feedback": {"x": 3}},
            "beliefs": [{"proposition": "派蒙在附近", "confidence": 0.3}],
            "relationships": [{"character": "派蒙", "trust": 1}],
            "emotion": {"joy": 1},
            "threads": [{"title": "寻找派蒙", "status": "open"}],
        })
        s = second["state"]
        assert s["working"]["location"] == "蒙德"
        assert s["working"]["last_motor_feedback"] == {"x": 3, "y": 2}
        assert s["beliefs"][0]["status"] == "belief"
        assert s["relationships"][0]["trust"] == 0.3
        assert s["relationships"][0]["affection"] == 0.15
        assert s["emotion"]["joy"] == 0.7 and s["emotion"]["fear"] == 0.35
        assert s["threads"][0]["importance"] == 0.8
        assert s["threads"][0]["status_history"][-1]["event_id"] == "obs2"
        assert s["threads"][0]["last_activation"]["event_id"] == "obs2"
        assert "history" not in s
        assert log.get_event("s", "turn2")["payload"]["changes"]["relationships"]["before"][0]["trust"] == 0.15
        assert log.get_event("s", "turn2")["caused_by"] == "obs2"
        second["state"]["working"]["location"] = "tamper"
        assert tx.read("s")["state"]["working"]["location"] == "蒙德"
    with PlayerEventLog(path) as log:
        loaded = PlayerCognitiveTransaction(log).read("s")
        assert loaded["revision"] == second["seq"]
        assert loaded["state"]["threads"][0]["status"] == "open"


def test_belief_and_thread_transition_history_is_preserved(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        observe(log)
        tx = PlayerCognitiveTransaction(log)
        commit(tx, updates={"beliefs": [{"proposition": "x", "status": "belief"}],
                            "threads": [{"title": "q", "status": "open"}]})
        commit(tx, "t2", updates={"beliefs": [{"proposition": "x", "status": "disproven", "evidence": ["矛盾"], "reason": "新线索"}],
                                   "threads": [{"title": "q", "status": "dormant"}]})
        commit(tx, "t3", updates={"beliefs": [{"proposition": "x", "status": "belief"}],
                                   "threads": [{"title": "q", "status": "open"}]})
        state = tx.read("s")["state"]
        assert state["beliefs"][0]["history"] == ["disproven -> belief"]
        assert state["beliefs"][0]["contradictions"] == ["矛盾"]
        transition = log.get_event("s", "t2")["payload"]["changes"]["beliefs"]["after"][0]["transitions"][0]
        assert (transition["from"], transition["to"], transition["reason"]) == ("belief", "disproven", "新线索")
        assert transition["evidence"] == ["矛盾"] and transition["source_event_id"] == "obs"
        assert type(transition["timestamp_ms"]) is int
        assert len(state["threads"][0]["status_history"]) == 1
        for eid in ("t2", "t3"):
            change = log.get_event("s", eid)["payload"]["changes"]["threads"]["after"][0]
            assert change["status_history"][-1]["event_id"] == "obs"


@pytest.mark.parametrize("source", ["missing", "hidden", "other", "derived", "forged"])
def test_reject_missing_hidden_cross_session_or_nonraw_source(tmp_path, source):
    with PlayerEventLog(tmp_path / "db") as log:
        observe(log)
        observe(log, "hidden", visibility="hidden")
        observe(log, "other", session="other-session")
        log.append([{"event_id": "derived", "session_id": "s", "timestamp": "t", "type": "cognitive.turn",
                     "source": "player_cognitive_transaction", "payload": {"visibility": "player"}}])
        log.append([{"event_id": "forged", "session_id": "s", "timestamp": "t", "type": "experience.raw_observation",
                     "source": "player_experience", "payload": {"kind": "raw_observation", "visibility": "player", "session_id": "s", "event_id": "other"}}])
        tx = PlayerCognitiveTransaction(log)
        with pytest.raises(CognitiveTransactionError):
            commit(tx, source=source)
        assert tx.read("s")["revision"] == 0
        assert log.get_event("s", "turn") is None


@pytest.mark.parametrize("updates", [
    {"facts": [{"proposition": "x", "visibility": "canonical"}]},
    {"beliefs": [{"proposition": "x", "status": "ground_truth"}]},
    {"threads": [{"title": "x", "world_only": True}]},
    {"emotion": {"joy": float("nan")}},
    {"emotion": {"joy": "nan"}},
    {"beliefs": [{"proposition": "x", "status": "made_up"}]},
    {"threads": [{"title": "x", "status": "typo"}]},
    {"beliefs": [{"proposition": "x"}], "relationships": [{"character": "x", "trust": True}]},
    {"relationships": [{"character": "x", "trust": 1}, {"character": "x", "trust": 1}]},
    {"threads": [{"title": "x"}, {"title": "x"}]},
])
def test_invalid_updates_have_no_partial_write(tmp_path, updates):
    with PlayerEventLog(tmp_path / "db") as log:
        observe(log)
        tx = PlayerCognitiveTransaction(log)
        with pytest.raises((CognitiveTransactionError, ValueError)):
            commit(tx, updates=updates)
        assert tx.read("s")["revision"] == 0
        assert log.get_event("s", "turn") is None


def test_initial_insert_failure_and_single_append(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        observe(log)
        tx = PlayerCognitiveTransaction(log)
        original = log.append
        calls = []
        def counted(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        log.append = counted
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON projections WHEN NEW.name = 'cognitive.v1' BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            commit(tx, updates={"facts": ["主观推测"], "working": {"location": "蒙德"}})
        assert len(calls) == 1
        assert calls[0][1]["expected_revisions"][PROJECTION] == 0
        assert len(calls[0][1]["expected_revisions"]) == 2
        assert log.get_event("s", "turn") is None
        assert tx.read("s")["revision"] == 0


def test_duplicate_id_and_database_failure_roll_back(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        observe(log)
        tx = PlayerCognitiveTransaction(log)
        first = commit(tx)
        with pytest.raises(EventLogConflict):
            commit(tx, updates={"emotion": {"joy": 1}})
        assert tx.read("s")["revision"] == first["revision"]
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON projections WHEN NEW.name = 'cognitive.v1' BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            commit(tx, "turn2", updates={"emotion": {"joy": 1}})
        assert log.get_event("s", "turn2") is None
        assert tx.read("s")["state"] == first["state"]


def test_cas_concurrent_writers_and_stale_client(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        observe(log)
        barrier = Barrier(2)
        def run(n):
            with PlayerEventLog(path) as other:
                tx = PlayerCognitiveTransaction(other)
                original = other.append
                def synchronized(*a, **kw):
                    barrier.wait(timeout=5)
                    return original(*a, **kw)
                other.append = synchronized
                try:
                    return commit(tx, f"t{n}", updates={"emotion": {"joy": 1}})
                except EventLogConflict:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [1, 2]))
        assert sum(result is not None for result in results) == 1
        tx = PlayerCognitiveTransaction(log)
        assert tx.read("s")["state"]["emotion"]["joy"] == 0.35
        with pytest.raises(EventLogConflict):
            commit(tx, "stale", expected_revision=0)
        assert log.get_event("s", "stale") is None
        assert len([x for x in log.list_events("s") if x["type"] == "cognitive.turn"]) == 1


def test_cross_session_player_scope_restart_and_isolation(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s1", player_scope="alice", event_id="bind1")
        observe(log, "obs1", "s1")
        first = commit(tx, "t1", "obs1", "s1", player_scope="alice", updates={
            "relationships": [{"character": "派蒙", "trust": 1}],
            "threads": [{"title": "寻找派蒙", "status": "dormant"}],
            "facts": ["看见派蒙"]})
        tx.bind_session(session_id="s-other", player_scope="bob", event_id="bind-other")
        observe(log, "obs-other", "s-other")
        other = commit(tx, "other-turn", "obs-other", "s-other", player_scope="bob", updates={"facts": ["独立事实"]})
        assert other["state"]["relationships"] == []
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s2", player_scope="alice", event_id="bind2")
        observe(log, "obs2", "s2")
        restored = tx.read("s2", player_scope="alice")
        assert restored["revision"] == first["revision"]
        assert restored["state"]["relationships"][0]["trust"] == 0.15
        second = commit(tx, "t2", "obs2", "s2", player_scope="alice", expected_revision=first["revision"], updates={
            "relationships": [{"character": "派蒙", "trust": 1}],
            "threads": [{"title": "寻找派蒙", "status": "open"}]})
        assert second["state"]["relationships"][0]["trust"] == 0.3
        assert second["state"]["threads"][0]["last_activation"]["event_id"] == "obs2"
        assert log.get_event("s1", "t1")["session_id"] == "s1"
        assert log.get_event("s2", "t2")["session_id"] == "s2"
        assert log.get_event("s2", "t2")["caused_by"] == "obs2"
        assert log.get_event("s2", "t2")["payload"]["player_scope"] == "alice"
        assert tx.read("s-other", player_scope="bob")["state"] == other["state"]
        with pytest.raises(CognitiveTransactionError):
            commit(tx, "cross-player", "obs2", "s2", player_scope="bob")
        with pytest.raises(CognitiveTransactionError):
            commit(tx, "cross-session", "obs1", "s2", player_scope="alice")
        with pytest.raises(CognitiveTransactionError):
            tx.bind_session(session_id="s2", player_scope="bob", event_id="rebind")
        with pytest.raises(CognitiveTransactionError):
            commit(tx, "no-scope", "obs2", "s2")
        assert log.get_event("s2", "cross-player") is None
        assert log.get_event("s2", "cross-session") is None
        assert log.get_event("s2", "rebind") is None
        assert tx.read("s2", player_scope="alice")["revision"] == second["revision"]


def test_unbound_session_and_wrong_player_cannot_read_or_write(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        observe(log)
        with pytest.raises(CognitiveTransactionError):
            commit(tx, player_scope="alice")
        with pytest.raises(CognitiveTransactionError):
            tx.read("s", player_scope="alice")
        tx.bind_session(session_id="s", player_scope="alice", event_id="binding")
        with pytest.raises(CognitiveTransactionError):
            tx.read("s", player_scope="bob")
        assert log.get_event("s", "turn") is None


def test_cross_session_concurrent_writers_conflict(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        for n in (1, 2):
            tx.bind_session(session_id=f"s{n}", player_scope="alice", event_id=f"bind{n}")
            observe(log, f"obs{n}", f"s{n}")
        barrier = Barrier(2)
        def run(n):
            with PlayerEventLog(path) as writer:
                current = PlayerCognitiveTransaction(writer)
                original = writer.append
                def synchronized(*args, **kwargs):
                    barrier.wait(timeout=5)
                    return original(*args, **kwargs)
                writer.append = synchronized
                try:
                    return commit(current, f"t{n}", f"obs{n}", f"s{n}", player_scope="alice",
                                  updates={"emotion": {"joy": 1}})
                except EventLogConflict:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [1, 2]))
        assert sum(x is not None for x in results) == 1
        assert tx.read("s1", player_scope="alice")["state"]["emotion"]["joy"] == 0.35
        assert sum(log.get_event(f"s{n}", f"t{n}") is not None for n in (1, 2)) == 1


def test_scoped_update_failure_rolls_back_event_and_state(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="binding")
        observe(log)
        first = commit(tx, player_scope="alice", updates={"emotion": {"joy": 1}})
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_scoped_update BEFORE UPDATE ON scoped_projections BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            commit(tx, "turn2", player_scope="alice", updates={"emotion": {"joy": 1}})
        assert log.get_event("s", "turn2") is None
        assert tx.read("s", player_scope="alice")["state"] == first["state"]


def test_existing_legacy_session_not_silently_migrated(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        observe(log)
        tx = PlayerCognitiveTransaction(log)
        before = commit(tx, updates={"facts": ["旧状态"]})
        with pytest.raises(CognitiveTransactionError, match="旧会话"):
            tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        assert tx.read("s")["state"] == before["state"]
        assert log.get_event("s", "bind") is None
        assert log.get_projection("s", PROJECTION, projection_scope="alice") is None


def test_binding_change_during_commit_conflicts_atomically(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log, PlayerEventLog(path) as attacker:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="binding")
        observe(log)
        original = log.append
        def invalidate(*args, **kwargs):
            attacker.append([{"event_id": "external", "session_id": "s", "timestamp": "t", "type": "external", "source": "test", "payload": {}}],
                            projections={"cognitive.session_owner.v1": {"version": 1, "session_id": "s", "player_scope": "bob", "event_id": "external"}})
            return original(*args, **kwargs)
        log.append = invalidate
        with pytest.raises(EventLogConflict):
            commit(tx, player_scope="alice", updates={"facts": ["不可落盘"]})
        assert log.get_event("s", "turn") is None
        assert log.get_projection("s", PROJECTION, projection_scope="alice") is None


def test_automatic_activation_across_sessions_and_idempotence(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s1", player_scope="alice", event_id="bind1")
        observe(log, "seed", "s1")
        commit(tx, "seed-turn", "seed", "s1", player_scope="alice", updates={"threads": [
            {"title": "寻找遗迹", "status": "dormant", "characters": ["派蒙"]},
            {"title": "风龙废墟", "status": "dormant"},
            {"title": "未解决", "status": "resolved", "characters": ["派蒙"]},
        ]})
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s2", player_scope="alice", event_id="bind2")
        observe(log, "ocr", "s2", text="派蒙在风龙废墟附近")
        result = tx.activate_dormant_threads(session_id="s2", player_scope="alice", source_event_id="ocr", now_ms=2000)
        assert [x["reason"] for x in result["activated"]] == ["entity:派蒙", "topic:风龙废墟"]
        assert not result["already_processed"]
        thread = result["state"]["threads"][0]
        assert thread["status"] == "open"
        assert thread["last_activation"]["event_id"] == "ocr"
        assert thread["status_history"][-1] == {"from_status": "dormant", "to_status": "open", "time_ms": 2000,
                                                  "reason": "entity:派蒙", "event_id": "ocr"}
        assert result["state"]["threads"][2]["status"] == "resolved"
        event = log.get_event("s2", result["event_id"])
        assert event["caused_by"] == "ocr" and event["payload"]["player_scope"] == "alice"
        replay = tx.activate_dormant_threads(session_id="s2", player_scope="alice", source_event_id="ocr", now_ms=3000)
        assert replay["already_processed"] and replay["seq"] == result["seq"]
        assert replay["revision"] == result["revision"]
        commit(tx, "dormant", "ocr", "s2", player_scope="alice", updates={"threads": [{"title": "寻找遗迹", "status": "dormant"}]})
        replay = tx.activate_dormant_threads(session_id="s2", player_scope="alice", source_event_id="ocr", now_ms=4000)
        assert replay["already_processed"] and replay["state"]["threads"][0]["status"] == "dormant"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        assert tx.activate_dormant_threads(session_id="s2", player_scope="alice", source_event_id="ocr")["already_processed"]


def test_automatic_activation_strict_matching_stale_and_zero_match_dedup(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log, "old", text="派蒙和 the abyss order")
        commit(tx, "create", "old", player_scope="alice", updates={"threads": [
            {"title": "神秘的任务", "status": "dormant"},
            {"title": "Abyss Order", "status": "dormant"},
            {"title": "寻找遗迹", "status": "dormant", "characters": ["派蒙"]},
        ]})
        old = tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="old")
        assert old["activated"] == []  # 观察早于线索创建。
        observe(log, "generic", text="新的神秘任务，abyss ordering 发生了")
        no_match = tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="generic")
        assert no_match["activated"] == []
        assert tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="generic")["already_processed"]
        observe(log, "match", text="发现 Abyss Order 的踪迹")
        result = tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="match")
        assert [x["reason"] for x in result["activated"]] == ["topic:abyss order"]
        assert result["state"]["threads"][0]["status"] == "dormant"


def test_automatic_activation_rejects_invalid_sources_and_scope(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log, "hidden", visibility="hidden")
        observe(log, "foreign", session="other")
        PlayerExperience(log).record_observation(session_id="s", event_id="attempt", timestamp="t", source_id="motor",
                                                 channel="action_attempt", text="派蒙", visibility="player")
        for source in ("missing", "hidden", "foreign", "attempt"):
            with pytest.raises(CognitiveTransactionError):
                tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id=source)
        with pytest.raises(CognitiveTransactionError):
            tx.activate_dormant_threads(session_id="s", player_scope="bob", source_event_id="attempt")
        assert tx.read("s", player_scope="alice")["revision"] == 0


def test_automatic_activation_sql_failure_and_revision_conflict(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log, "seed")
        first = commit(tx, "create", "seed", player_scope="alice", updates={"threads": [{"title": "寻找派蒙", "status": "dormant"}]})
        observe(log, "ocr", text="寻找派蒙")
        with pytest.raises(EventLogConflict):
            tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="ocr", expected_revision=0)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_activation BEFORE UPDATE ON scoped_projections BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="ocr")
        assert tx.read("s", player_scope="alice")["state"] == first["state"]
        assert not [e for e in log.list_events("s") if e["type"] == "cognitive.thread_activation"]
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_activation")
        assert tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="ocr")["activated"]


def test_automatic_activation_concurrent_same_source(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log, "seed")
        commit(tx, "create", "seed", player_scope="alice", updates={"threads": [{"title": "寻找派蒙", "status": "dormant"}]})
        observe(log, "ocr", text="寻找派蒙")
        barrier = Barrier(2)
        def run(_):
            with PlayerEventLog(path) as writer:
                worker = PlayerCognitiveTransaction(writer)
                original = writer.append
                def synchronized(*args, **kwargs):
                    barrier.wait(timeout=5)
                    return original(*args, **kwargs)
                writer.append = synchronized
                try:
                    return worker.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="ocr")
                except EventLogConflict:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        assert sum(x is not None for x in results) == 1
        retry = tx.activate_dormant_threads(session_id="s", player_scope="alice", source_event_id="ocr")
        assert retry["already_processed"]
        assert len([e for e in log.list_events("s") if e["type"] == "cognitive.thread_activation"]) == 1
        assert len(retry["state"]["threads"][0]["status_history"]) == 1


def _legacy_player(log):
    """构建包含旧版 turn 与只有投影记录 changes 的激活事件。"""
    import player_threads
    tx = PlayerCognitiveTransaction(log)
    tx.bind_session(session_id="old", player_scope="alice", event_id="old-bind")
    observe(log, "old-obs1", "old", text="寻找派蒙")
    original = player_threads.normalize_thread({"title": "寻找派蒙", "status": "dormant"}, now_ms=1000)
    original.update(visibility="player_knowledge", verification="subjective_unverified", source_event_ids=["old-obs1"])
    first_change = {"threads": {"before": [], "after": [copy.deepcopy(original)]}}
    src1 = log.get_event("old", "old-obs1")
    first_payload = {"visibility": "player_knowledge", "verification": "subjective_unverified", "player_scope": "alice",
                     "source_event_id": "old-obs1", "source_seq": src1["seq"], "thought": "寻找派蒙", "intention": None,
                     "speech": None, "changes": first_change}
    log.append([{"event_id": "old-turn", "session_id": "old", "timestamp": "t", "type": "cognitive.turn",
                 "source": "player_cognitive_transaction", "caused_by": "old-obs1", "payload": first_payload}])
    observe(log, "old-obs2", "old", text="寻找派蒙")
    activated = copy.deepcopy(original)
    activated["status"] = "open"
    activated["status_history"] = [{"from_status": "dormant", "to_status": "open", "time_ms": 2000,
                                    "reason": "topic:寻找派蒙", "event_id": "old-obs2"}]
    activated["last_referenced_ms"] = 2000
    activated["source_event_ids"].append("old-obs2")
    activate_change = {"threads": {"before": [original], "after": [copy.deepcopy(activated)]}}
    import hashlib
    key = json.dumps(["alice", "old", "old-obs2"], ensure_ascii=True, separators=(",", ":"))
    activation_id = "cognitive-activation-" + hashlib.sha256(key.encode()).hexdigest()
    src2 = log.get_event("old", "old-obs2")
    activation_payload = {"visibility": "player_knowledge", "verification": "subjective_unverified", "player_scope": "alice",
                          "source_event_id": "old-obs2", "source_seq": src2["seq"],
                          "activated": [{"thread_id": original["thread_id"], "reason": "topic:寻找派蒙"}]}
    old = {"version": 1, "working": tx.read("old", player_scope="alice")["state"]["working"],
           "beliefs": [], "relationships": [], "emotion": tx.read("old", player_scope="alice")["state"]["emotion"],
           "threads": [activated], "facts": [], "history": [
               {"event_id": "old-turn", "session_id": "old", "source_event_id": "old-obs1", "time_ms": 1000, "changes": first_change},
               {"event_id": activation_id, "session_id": "old", "source_event_id": "old-obs2", "time_ms": 2000, "changes": activate_change}]}
    seq = log.append([{"event_id": activation_id, "session_id": "old", "timestamp": "t", "type": "cognitive.thread_activation",
                       "source": "player_cognitive_transaction", "caused_by": "old-obs2", "payload": activation_payload}],
                     projections={PROJECTION: old}, projection_scope="alice", expected_revisions={PROJECTION: 0})[0]
    return old, seq, activation_id


def test_v1_migration_archives_full_old_state_and_preserves_causality(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        old, revision, activation_id = _legacy_player(log)
        tx = PlayerCognitiveTransaction(log)
        assert tx.read("old", player_scope="alice")["state"] == old
        tx.bind_session(session_id="new", player_scope="alice", event_id="new-bind")
        observe(log, "new-obs", "new", text="寻找派蒙")
        result = commit(tx, "new-turn", "new-obs", "new", player_scope="alice", expected_revision=revision)
        state = result["state"]
        assert state["version"] == 2 and "history" not in state
        assert state["threads"][0]["status"] == old["threads"][0]["status"]
        archive = log.get_event("new", state["legacy_archive"]["event_id"])
        assert archive["type"] == "cognitive.migration_archive"
        assert archive["payload"]["legacy_state"] == old
        legacy_name = "cognitive.legacy." + archive["event_id"]
        assert log.get_projection("new", legacy_name, projection_scope="alice")["state"] == old
        assert archive["payload"]["previous_revision"] == revision
        assert archive["payload"]["legacy_state"]["history"][1]["changes"]["threads"]["after"][0]["status"] == "open"
        assert state["thread_last_events"][old["threads"][0]["thread_id"]]["event_id"] == activation_id
        intent = tx.append_intent(session_id="old", player_scope="alice", thought_id="old-turn", intent_id="old-intent", attribution={}, status="created")
        assert intent["state"]["thought_id"] == "old-turn"
        replay = tx.activate_dormant_threads(session_id="old", player_scope="alice", source_event_id="old-obs2")
        assert replay["already_processed"] and replay["event_id"] == activation_id
    with PlayerEventLog(path) as log:
        assert PlayerCognitiveTransaction(log).read("new", player_scope="alice")["state"] == state


def test_v1_migration_failure_rolls_back_archive_and_retains_projection(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        old, revision, _ = _legacy_player(log)
        tx = PlayerCognitiveTransaction(log)
        observe(log, "fresh", "old")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_migration BEFORE UPDATE ON scoped_projections BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            commit(tx, "new-turn", "fresh", "old", player_scope="alice")
        assert tx.read("old", player_scope="alice") == {"state": old, "revision": revision}
        assert not [x for x in log.list_events("old") if x["type"] == "cognitive.migration_archive"]
        assert log.get_event("old", "new-turn") is None


def test_v1_migration_concurrent_writers_one_wins(tmp_path):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        old, _, _ = _legacy_player(log)
        observe(log, "fresh", "old")
        barrier = Barrier(2)
        def run(n):
            with PlayerEventLog(path) as writer:
                tx = PlayerCognitiveTransaction(writer)
                original = writer.append
                def synchronized(*args, **kwargs):
                    barrier.wait(timeout=5)
                    return original(*args, **kwargs)
                writer.append = synchronized
                try:
                    return commit(tx, f"migrated{n}", "fresh", "old", player_scope="alice")
                except EventLogConflict:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [1, 2]))
        assert sum(x is not None for x in results) == 1
        events = log.list_events("old")
        assert len([event for event in events if event["type"] == "cognitive.migration_archive"]) == 1
        assert [event for event in events if event["type"] == "cognitive.migration_archive"][0]["payload"]["legacy_state"] == old


def test_v1_migration_rejects_inconsistent_history_without_writes(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        old, _, _ = _legacy_player(log)
        old["history"][0]["changes"] = {"forged": {}}
        forged = {"event_id": "tamper", "session_id": "old", "timestamp": "t", "type": "test", "source": "test", "payload": {}}
        log.append([forged], projections={PROJECTION: old}, projection_scope="alice")
        tx = PlayerCognitiveTransaction(log)
        observe(log, "fresh", "old")
        with pytest.raises(CognitiveTransactionError, match="不一致"):
            commit(tx, "new-turn", "fresh", "old", player_scope="alice")
        assert tx.read("old", player_scope="alice")["state"] == old
        assert not [x for x in log.list_events("old") if x["type"] == "cognitive.migration_archive"]


@pytest.mark.parametrize("field", ["title", "reason", "timestamp", "source"])
def test_v1_activation_migration_rejects_forged_projection(tmp_path, field):
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        old, revision, _ = _legacy_player(log)
        changed = old["history"][1]["changes"]["threads"]["after"][0]
        if field == "title":
            changed["title"] = "伪造线索"
        elif field == "reason":
            changed["status_history"][-1]["reason"] = "topic:伪造"
        elif field == "timestamp":
            changed["last_referenced_ms"] = 3000
        else:
            changed["source_event_ids"][-1] = "other-source"
        old["threads"][0] = copy.deepcopy(changed)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE scoped_projections SET state=? WHERE scope=? AND name=?",
                       (json.dumps(old, ensure_ascii=False), "alice", PROJECTION))
        tx = PlayerCognitiveTransaction(log)
        observe(log, "fresh", "old")
        with pytest.raises(CognitiveTransactionError):
            commit(tx, "new-turn", "fresh", "old", player_scope="alice")
        with pytest.raises(CognitiveTransactionError):
            tx.activate_dormant_threads(session_id="old", player_scope="alice", source_event_id="old-obs2")
        assert tx.read("old", player_scope="alice")["revision"] == revision
        assert log.get_event("old", "new-turn") is None
        assert not [e for e in log.list_events("old") if e["type"] == "cognitive.migration_archive"]


def test_repeated_turns_do_not_grow_projection_history_and_preserve_audit(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log)
        sizes = []
        for n in range(120):
            result = commit(tx, f"t{n}", player_scope="alice", updates={"beliefs": [{"proposition": "p", "status": "belief" if n % 2 else "suspicion"}],
                             "facts": [{"proposition": "同一事实", "confidence": 0.5}]})
            sizes.append(len(json.dumps(result["state"], ensure_ascii=False)))
        assert max(sizes[-50:]) - min(sizes[-50:]) < 50
        assert len(result["state"]["beliefs"][0]["history"]) == 1
        assert len(result["state"]["facts"]) == 1
        assert len([e for e in log.list_events("s") if e["type"] == "cognitive.turn"]) == 120
        assert log.get_event("s", "t10")["payload"]["changes"]["beliefs"]["after"][0]["status"] == "suspicion"
        assert tx.append_intent(session_id="s", player_scope="alice", thought_id="t0", intent_id="first-intent", attribution={}, status="created")


def test_corrupt_projection_refused(tmp_path):
    with PlayerEventLog(tmp_path / "db") as log:
        observe(log)
        log.append([{"event_id": "bad", "session_id": "s", "timestamp": "t", "type": "bad", "source": "test", "payload": {}}],
                   projections={PROJECTION: {"version": 99}})
        with pytest.raises(CognitiveTransactionError):
            commit(PlayerCognitiveTransaction(log))
        assert log.get_event("s", "turn") is None


def test_intent_chain_replay_restart_and_status(tmp_path):
    """状态重放保留最新事实；反馈不是游戏执行证明。"""
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="binding")
        observe(log)
        commit(tx, player_scope="alice")
        args = dict(session_id="s", player_scope="alice", thought_id="turn", intent_id="intent-1")
        created = tx.append_intent(**args, attribution={"actionGroupId": "g"}, status="created")
        sent = tx.append_intent(**args, attribution={"actionGroupId": "g", "motorGeneration": 4}, status="sent")
        feedback = {"intent_id": "intent-1", "status": "done", "motorGeneration": 4, "actionGroupId": "g"}
        fb_args = dict(session_id="s", player_scope="alice", intent_id="intent-1", feedback=feedback, event_id="fb")
        result = tx.append_motor_feedback(**fb_args)
        assert log.get_event("s", "fb")["caused_by"] == sent["event_id"]
        assert log.get_event("s", sent["event_id"])["caused_by"] == "turn"
        assert log.get_event("s", "turn")["caused_by"] == "obs"
        assert log.get_event("s", "fb")["payload"]["game_execution"] == "unknown"
        assert log.get_event("s", sent["event_id"])["payload"]["game_execution"] == "unknown"
        assert tx.append_motor_feedback(**fb_args)["seq"] == result["seq"]
        cancelled = tx.append_intent(**args, attribution={"actionGroupId": "g", "motorGeneration": 4}, status="cancelled")
        replay = tx.append_intent(**args, attribution={"actionGroupId": "g"}, status="created")
        assert replay["seq"] == created["seq"] and replay["state"]["status"] == "cancelled"
        assert replay["revision"] == cancelled["seq"]
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        assert tx.append_intent(**args, attribution={"actionGroupId": "g"}, status="created")["state"]["status"] == "cancelled"
        assert tx.append_motor_feedback(**fb_args)["already_recorded"]
        with pytest.raises(CognitiveTransactionError):
            tx.append_motor_feedback(**{**fb_args, "event_id": "new-fb"})


def test_intent_rejects_foreign_missing_and_reused_thought(tmp_path):
    """同 ID 不能更换 thought，也不能引用其他会话或伪造来源。"""
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        for session in ("s", "s2"):
            tx.bind_session(session_id=session, player_scope="alice", event_id="bind-" + session)
            observe(log, "obs-" + session, session)
            commit(tx, "turn-" + session, "obs-" + session, session, player_scope="alice")
        def record(s, thought, intent="i", scope="alice"):
            return tx.append_intent(session_id=s, player_scope=scope, thought_id=thought, intent_id=intent, attribution={}, status="created")
        for thought in ("missing", "turn-s2", "obs-s"):
            with pytest.raises(CognitiveTransactionError):
                record("s", thought)
        with pytest.raises(CognitiveTransactionError):
            record("s", "turn-s", scope="bob")
        record("s", "turn-s")
        commit(tx, "second", "obs-s", "s", player_scope="alice")
        with pytest.raises(CognitiveTransactionError):
            record("s", "second")
        with pytest.raises(EventLogConflict):
            record("s2", "turn-s2")
        assert not [e for e in log.list_events("s2") if e["type"] == "cognitive.intent"]


def test_feedback_attribution_and_generation_are_strict(tmp_path):
    """缺代际、不同动作组及旧代际反馈均拒绝且不写入。"""
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log)
        commit(tx, player_scope="alice")
        def send(intent, generation):
            args = dict(session_id="s", player_scope="alice", thought_id="turn", intent_id=intent)
            tx.append_intent(**args, attribution={"actionGroupId": "g"}, status="created")
            tx.append_intent(**args, attribution={"actionGroupId": "g", "motorGeneration": generation}, status="sent")
        send("old", 2)
        old_fb = dict(session_id="s", player_scope="alice", intent_id="old", event_id="old-fb",
                      feedback={"intent_id": "old", "status": "done", "motorGeneration": 2, "actionGroupId": "g"})
        tx.append_motor_feedback(**old_fb)
        send("new", 3)
        with pytest.raises(CognitiveTransactionError):
            tx.append_motor_feedback(**old_fb)
        with pytest.raises(CognitiveTransactionError):
            tx.append_motor_feedback(**{**old_fb, "session_id": "other"})
        feedbacks = [
            {"intent_id": "old", "status": "done", "motorGeneration": 2, "actionGroupId": "g"},
            {"intent_id": "new", "status": "done", "actionGroupId": "g"},
            {"intent_id": "new", "status": "done", "motorGeneration": 2, "actionGroupId": "g"},
            {"intent_id": "new", "status": "done", "motorGeneration": 3},
            {"intent_id": "new", "status": "done", "motorGeneration": True, "actionGroupId": "g"},
        ]
        for index, fb in enumerate(feedbacks):
            with pytest.raises(CognitiveTransactionError):
                tx.append_motor_feedback(session_id="s", player_scope="alice", intent_id=fb["intent_id"], feedback=fb, event_id=f"bad-{index}")
        assert [e["event_id"] for e in log.list_events("s") if e["type"] == "cognitive.motor_feedback"] == ["old-fb"]
        with pytest.raises(CognitiveTransactionError):
            tx.append_intent(session_id="s", player_scope="alice", thought_id="turn", intent_id="new", attribution={}, status="sent")


def test_intent_atomic_failure_and_concurrent_cas(tmp_path):
    """投影失败时事件回滚，并发对同一版本只有一个胜者。"""
    path = tmp_path / "db"
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log)
        commit(tx, player_scope="alice")
        args = dict(session_id="s", player_scope="alice", thought_id="turn", intent_id="i", attribution={}, status="created")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON projections WHEN NEW.name LIKE 'cognitive.intent.%' BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            tx.append_intent(**args)
        assert not [e for e in log.list_events("s") if e["type"] == "cognitive.intent"]
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_intent")
        tx.append_intent(**args)
        barrier = Barrier(2)
        def run(_):
            with PlayerEventLog(path) as other:
                worker = PlayerCognitiveTransaction(other)
                original = other.append
                def synchronized(*a, **kw):
                    barrier.wait(timeout=5)
                    return original(*a, **kw)
                other.append = synchronized
                try:
                    return worker.append_intent(**{**args, "status": "sent", "attribution": {"motorGeneration": 1}})
                except EventLogConflict:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, range(2)))
        assert sum(x is not None for x in outcomes) == 1
        assert tx.append_intent(**{**args, "status": "sent", "attribution": {"motorGeneration": 1}})["already_recorded"]


def test_forged_turn_and_unverified_observation_rejected(tmp_path):
    """仅有形似认知事件不足以证明其来自已提交的认知事务。"""
    with PlayerEventLog(tmp_path / "db") as log:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log)
        observe(log, "hidden", visibility="hidden")
        commit(tx, player_scope="alice")
        genuine = log.get_event("s", "turn")
        for eid, source in (("forged", "obs"), ("hidden-turn", "hidden")):
            payload = dict(genuine["payload"], source_event_id=source,
                           source_seq=log.get_event("s", source)["seq"])
            log.append([{**{k: v for k, v in genuine.items() if k != "seq"},
                         "event_id": eid, "caused_by": source, "payload": payload}])
            with pytest.raises(CognitiveTransactionError):
                tx.append_intent(session_id="s", player_scope="alice", thought_id=eid,
                                 intent_id=eid, attribution={}, status="created")
        assert not [e for e in log.list_events("s") if e["type"] == "cognitive.intent"]


def test_feedback_rollback_conflicts_and_failed_intent(tmp_path):
    """反馈事务失败时无半成品；并发修改投影时 CAS 拒绝写入。"""
    path = tmp_path / "db"
    with PlayerEventLog(path) as log, PlayerEventLog(path) as other:
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="s", player_scope="alice", event_id="bind")
        observe(log)
        commit(tx, player_scope="alice")
        args = dict(session_id="s", player_scope="alice", thought_id="turn", intent_id="i")
        tx.append_intent(**args, attribution={}, status="created")
        tx.append_intent(**args, attribution={"motorGeneration": 5}, status="sent")
        fb = dict(session_id="s", player_scope="alice", intent_id="i", event_id="fb",
                  feedback={"intent_id": "i", "motorGeneration": 5, "status": "progress"})
        with sqlite3.connect(path) as db:
            db.execute("CREATE TRIGGER fail_fb BEFORE INSERT ON events WHEN NEW.type = 'cognitive.motor_feedback' BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(EventLogError):
            tx.append_motor_feedback(**fb)
        assert log.get_event("s", "fb") is None
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_fb")
        original = log.append
        def competing(*a, **kw):
            PlayerCognitiveTransaction(other).append_intent(**args, attribution={"motorGeneration": 5}, status="failed")
            return original(*a, **kw)
        log.append = competing
        with pytest.raises(EventLogConflict):
            tx.append_motor_feedback(**fb)
        log.append = original
        assert log.get_event("s", "fb") is None
        replay = tx.append_intent(**args, attribution={}, status="created")
        assert replay["state"]["status"] == "failed"
        with pytest.raises(CognitiveTransactionError):
            tx.append_motor_feedback(**fb)
