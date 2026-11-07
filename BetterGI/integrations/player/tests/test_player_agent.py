#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PersistentPlayerAgent（shadow）的行为测试。

测试不联网：LLM 用 fake，时间用可推进的假时钟，所有文件都在 tmp_path。
核心断言：只写 shadow 记录、绝不出现发送接口、重启后能读到上次 resume。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_agent as agent_module  # noqa: E402
import player_context_builder as context_builder_module  # noqa: E402
import player_knowledge as knowledge_module  # noqa: E402
import player_episodic_memory as episodic_module  # noqa: E402
import player_memory_consolidator as consolidator_module  # noqa: E402
import player_profile as profile_module  # noqa: E402
import player_retrieval as retrieval_module  # noqa: E402
import player_semantic_memory as semantic_module  # noqa: E402
import player_state as state_module  # noqa: E402
import player_working_memory as working_module  # noqa: E402
import shadow_recorder as recorder_module  # noqa: E402
from player_state import StateStore  # noqa: E402

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "lumi.json"


class Clock:
    def __init__(self, start: int = 1_700_000_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def complete(
        self, system_prompt, user_text, image_b64=None, media_type="image/jpeg"
    ):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_text,
                "image": image_b64,
                "media_type": media_type,
            }
        )
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class GatedLLM(FakeLLM):
    """complete() 卡在事件上，便于测试 await 期间到达新消息的竞态。"""

    def __init__(self, response):
        super().__init__(response)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(
        self, system_prompt, user_text, image_b64=None, media_type="image/jpeg"
    ):
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_text,
                "image": image_b64,
                "media_type": media_type,
            }
        )
        self.entered.set()
        await self.release.wait()
        return self.response


class CountingStore(StateStore):
    """记录 save 次数，验证 _maybe_save_state 的节流。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saves = 0

    def save(self, state, *, now_ms=None):
        self.saves += 1
        return super().save(state, now_ms=now_ms)


def proposal_json(
    thought="坡上好像有声音，先上去看看",
    goal="绕到坡顶观察",
    speech="等一下——上面是不是有人？",
) -> str:
    return json.dumps(
        {
            "observation": "前方有一道斜坡",
            "thought": thought,
            "intention": {"goal": goal, "reason": "声音从上面传来", "horizon": "soon"},
            "speech": {
                "text": speech,
                "emotion": "curious",
                "intensity": 0.6,
                "should_speak": True,
            },
            "proposed_actions": [
                {"hint": "walk", "detail": "沿斜坡向上", "reason": "先确认声音来源"}
            ],
            "memory_note": {
                "scene": "蒙德城外的斜坡",
                "quest": "查看坡上的声音",
                "things_to_check": ["坡顶有没有人"],
            },
            "confidence": 0.7,
        },
        ensure_ascii=False,
    )


def frame_payload(seq=1, jpeg="ZmFrZS1qcGVn", guidance=None):
    return {
        "type": "frame",
        "seq": seq,
        "jpeg": jpeg,
        "width": 1280,
        "height": 720,
        "timestampUtc": "2023-11-14T22:13:20Z",  # 与 Clock 默认起点 1_700_000_000_000 对齐
        "runtime": {"isAlive": True, "isForeground": True},
        "uiCategory": "world",
        "guidance": guidance,
    }


def make_agent(tmp_path: Path, *, llm=None, clock=None, session_id="test-session"):
    clock = clock or Clock()
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    store = StateStore(tmp_path / "state" / "resume.json", profile.player_id, clock=clock)
    recorder = recorder_module.ShadowRecorder(
        tmp_path / "shadow",
        session_id,
        profile.player_id,
        run_id="run-test",
        clock=clock,
    )
    agent = agent_module.PersistentPlayerAgent(
        profile,
        store,
        recorder,
        llm=llm,
        session_id=session_id,
        clock=clock,
        state_save_interval_ms=1000,
        min_tick_gap_ms=100,
    )
    return agent, store, recorder, clock


def enable_mock_active(agent, *, speech=False, fake_transaction=False):
    """Isolate output logic under synthetic authorization, not a real safety lease."""
    agent._mode = "full_active" if speech else "motor_active"
    agent._active_output_allowed = lambda *, speech=False: True
    agent._cancel_confirmed = lambda: True
    if fake_transaction:
        class FakeTransaction:
            def append_intent(self, **kwargs):
                pass

            def append_motor_feedback(self, **kwargs):
                pass

        agent.cognitive_transaction = FakeTransaction()
        publish = agent._publish_intent

        async def publish_with_source(proposal, now, **kwargs):
            kwargs.setdefault("thought_id", "mock-thought")
            kwargs.setdefault("event_session", "mock-session")
            return await publish(proposal, now, **kwargs)

        agent._publish_intent = publish_with_source


def test_active_output_requires_bound_gate_and_fresh_matching_session(tmp_path: Path, monkeypatch) -> None:
    import time
    from types import SimpleNamespace
    from player_active_gate import GateConfig, GateDecision, PlayerActiveGate
    from player_speech_output import PlayerSpeechOutput

    agent, _, _, clock = make_agent(tmp_path)
    gate = PlayerActiveGate(GateConfig(mode="full_active", bettergi_url="http://127.0.0.1:30648",
                                       bettergi_token="test-token"), lambda _decision: None)
    agent._mode, agent._active_gate = "full_active", gate
    gate._identity_agent = agent
    gate._decision = GateDecision("full_active", True, (), "live-1")
    gate._deadline = gate._motor_deadline = gate._execution_channel_deadline = gate._speech_deadline = time.monotonic() + 60
    gate._monitor = SimpleNamespace(done=lambda: False)
    gate._input_lease = SimpleNamespace(active=True)
    gate._lease_heartbeat = SimpleNamespace(done=lambda: False)
    monkeypatch.setattr(gate, "_motor_trust_file_valid", lambda: True)
    monkeypatch.setattr(gate, "_observation_chain_valid", lambda: True)
    monkeypatch.setattr(gate, "_identity_files_valid", lambda: True)
    speech = PlayerSpeechOutput("endpoint", "x" * 32, read_endpoint=lambda *_: {}, send_speech=lambda *_: {})
    agent.speech_output = gate._speech_output = speech

    assert not agent._active_output_allowed()
    agent.observe({"type": "hello", "sessionId": "live-1"})
    agent.observe(frame_payload())
    assert agent._active_output_allowed()
    assert agent._active_output_allowed(speech=True)

    gate._speech_output = None
    assert not agent._active_output_allowed(speech=True)
    assert agent._active_output_allowed()
    gate._speech_output = speech
    gate._identity_agent = object()
    assert not agent._active_output_allowed()
    gate._identity_agent = agent
    agent._latest_frame["live_session_id"] = "other-live"
    assert not agent._active_output_allowed()
    agent._latest_frame["live_session_id"] = "live-1"
    clock.advance((agent.max_frame_age_ms or 15000) + 1)
    assert not agent._active_output_allowed()
    clock.now = 1_700_000_000_000
    gate._decision = GateDecision("full_active", False, ("revoked",), "live-1")
    assert not agent._active_output_allowed()
    agent._mode = "observer"
    assert not agent._active_output_allowed()
    assert not agent._active_output_allowed(speech=True)
    agent.close()


def test_reactive_event_reason_reaches_next_model_call(tmp_path: Path) -> None:
    llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.observe(frame_payload())
    asyncio.run(agent.tick(force=True, event_kind="dialogue_started"))
    assert "dialogue_started" in llm.calls[-1]["user"]
    assert "没有可靠观察就等待" in llm.calls[-1]["user"]
    agent.close()


def test_player_speech_uses_prebound_timeline_and_waits_for_receipt(tmp_path: Path) -> None:
    calls = []

    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    def prepare(endpoint, secret):
        calls.append("prepare")
        return {"session_id": "recording", "instance": "one"}

    def deliver(endpoint, secret, event, **kwargs):
        rows = log.recent_events("lumi:", "cognitive.speech")
        assert rows[0]["payload"]["status"] == "created"
        calls.append((kwargs, event))
        return {**event, "t": 0, "dur": 500}

    llm = FakeLLM(proposal_json(speech="这句是当时的想法。"))
    agent, *_ = make_agent(tmp_path, llm=llm)
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40, read_endpoint=prepare, send_speech=deliver)
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "thoughts.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        record = asyncio.run(agent.tick(force=True))
        rows = log.recent_events("lumi:", "cognitive.turn")
        assert rows[0]["event_id"] == record["thought_id"]
        speech_rows = log.recent_events("lumi:", "cognitive.speech")
        assert [row["payload"]["status"] for row in speech_rows] == ["timeline_written", "created"]
        assert speech_rows[0]["caused_by"] == speech_rows[1]["event_id"]
        assert speech_rows[1]["caused_by"] == record["thought_id"]
    assert calls[0] == "prepare"
    assert calls[1][0]["expected_session_id"] == "recording"
    assert calls[1][0]["expected_instance"] == "one"
    assert calls[1][1]["text"] == "这句是当时的想法。"
    assert calls[1][1]["source"] == "persistent_player"
    assert calls[1][1]["thought_id"] == record["thought_id"]
    assert record["speech_policy"]["delivery"] == "timeline_written"
    assert agent.speech_policy.last_speech_delivered is not None
    agent.close()


def test_speech_write_ahead_failure_never_calls_recorder(tmp_path: Path, monkeypatch) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    calls = []
    llm = FakeLLM(proposal_json(speech="这是我的想法。"))
    agent, *_ = make_agent(tmp_path, llm=llm)
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40,
        read_endpoint=lambda *_: {"session_id": "recording", "instance": "one"},
        send_speech=lambda *a, **k: calls.append(1))
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        tx = PlayerCognitiveTransaction(log)
        real_begin = tx.begin_speech_delivery
        finish_calls = []
        real_finish = tx.finish_speech_delivery
        def finish(**kwargs):
            finish_calls.append(kwargs)
            return real_finish(**kwargs)
        monkeypatch.setattr(tx, "finish_speech_delivery", finish)
        def commit_then_fail(**kwargs):
            real_begin(**kwargs)
            raise OSError("write result unknown")
        monkeypatch.setattr(tx, "begin_speech_delivery", commit_then_fail)
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = tx
        record = asyncio.run(agent.tick(force=True))
        assert record["speech_policy"]["delivery"] == "delivery_unknown"
        assert calls == [] and finish_calls == [] and agent.speech_policy.last_speech_delivered is None
        assert [row["payload"]["status"] for row in log.recent_events("lumi:", "cognitive.speech")] == ["created"]
        agent.close()


def test_speech_invalid_receipt_remains_unknown_and_never_retried(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    calls = []
    def deliver(endpoint, secret, event, **kwargs):
        calls.append(dict(event))
        return {**event, "text": "不同的文本", "t": 0, "dur": 500}
    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json(speech="原始文本")))
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40,
        read_endpoint=lambda *_: {"session_id": "recording", "instance": "one"}, send_speech=deliver)
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        record = asyncio.run(agent.tick(force=True))
        assert record["speech_policy"]["delivery"] == "delivery_unknown"
        assert len(calls) == 1 and agent.speech_policy.last_speech_delivered is None
        rows = log.recent_events("lumi:", "cognitive.speech")
        assert [row["payload"]["status"] for row in rows] == ["unknown", "created"]
        assert rows[0]["caused_by"] == rows[1]["event_id"]
        agent.close()


def test_speech_success_receipt_with_failed_wal_result_stays_unknown(tmp_path: Path, monkeypatch) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    calls = []
    def deliver(endpoint, secret, event, **kwargs):
        calls.append(event["thought_id"])
        return {**event, "t": 0, "dur": 500}
    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json(speech="我看到了。")))
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40,
        read_endpoint=lambda *_: {"session_id": "recording", "instance": "one"}, send_speech=deliver)
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        tx = PlayerCognitiveTransaction(log)
        finish = tx.finish_speech_delivery
        def fail_written(**kwargs):
            if kwargs["status"] == "timeline_written":
                raise OSError("commit result unknown")
            return finish(**kwargs)
        monkeypatch.setattr(tx, "finish_speech_delivery", fail_written)
        agent.experience_store, agent.cognitive_transaction = PlayerExperience(log), tx
        record = asyncio.run(agent.tick(force=True))
        assert record["speech_policy"]["delivery"] == "delivery_unknown"
        assert len(calls) == 1 and agent.speech_policy.last_speech_delivered is None
        assert [row["payload"]["status"] for row in log.recent_events("lumi:", "cognitive.speech")] == ["unknown", "created"]
        agent.close()


def test_speech_wal_failure_recovers_without_process_restart(tmp_path: Path, monkeypatch) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    calls = []
    def deliver(endpoint, secret, event, **kwargs):
        calls.append(event["thought_id"])
        return {**event, "t": 0, "dur": 500}
    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json(speech="保留这句话。")))
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40,
        read_endpoint=lambda *_: {"session_id": "recording", "instance": "one"}, send_speech=deliver)
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        tx = PlayerCognitiveTransaction(log)
        agent.experience_store, agent.cognitive_transaction = PlayerExperience(log), tx
        append = log.append
        def unavailable(events, **kwargs):
            if (events[0].get("type") == "cognitive.speech"
                    and events[0]["payload"]["status"] in {"timeline_written", "unknown"}):
                raise OSError("disk unavailable")
            return append(events, **kwargs)
        monkeypatch.setattr(log, "append", unavailable)
        record = asyncio.run(agent.tick(force=True))
        assert record["speech_policy"]["delivery"] == "delivery_unknown"
        assert len(calls) == 1 and calls[0] == record["thought_id"]
        assert [row["payload"]["status"] for row in log.recent_events("lumi:", "cognitive.speech")] == ["created"]
        assert agent._speech_recovery_transaction is None
        monkeypatch.setattr(log, "append", append)
        agent._mode = "observer"
        agent.observe(frame_payload())
        asyncio.run(agent.tick(force=True))
        rows = log.recent_events("lumi:", "cognitive.speech")
        assert [row["payload"]["status"] for row in rows] == ["unknown", "created"]
        assert rows[0]["caused_by"] == rows[1]["event_id"]
        assert calls == [record["thought_id"]]
        assert agent._speech_recovery_transaction is tx
        agent.close()


@pytest.mark.parametrize("read_method", ["_intent_source", "get_projection", "get_event"])
def test_speech_two_read_failures_release_lock_and_recover(tmp_path: Path, monkeypatch, read_method: str) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    calls = []
    failures = {"remaining": 0}
    def deliver(endpoint, secret, event, **kwargs):
        calls.append(event["thought_id"])
        failures["remaining"] = 2
        return {**event, "t": 0, "dur": 500}
    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json(speech="我记得这句话。")))
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40,
        read_endpoint=lambda *_: {"session_id": "recording", "instance": "one"}, send_speech=deliver)
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        tx = PlayerCognitiveTransaction(log)
        agent.experience_store, agent.cognitive_transaction = PlayerExperience(log), tx
        target = tx if read_method == "_intent_source" else log
        original = getattr(target, read_method)
        def fail_twice(*args, **kwargs):
            if failures["remaining"]:
                failures["remaining"] -= 1
                raise OSError("temporary read failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(target, read_method, fail_twice)
        record = asyncio.run(agent.tick(force=True))
        assert record["speech_policy"]["delivery"] == "delivery_unknown"
        assert failures["remaining"] == 0 and calls == [record["thought_id"]]
        assert [row["payload"]["status"] for row in log.recent_events("lumi:", "cognitive.speech")] == ["created"]
        assert not tx._live_speech_reservations and agent._speech_recovery_transaction is None
        monkeypatch.setattr(target, read_method, original)
        agent._mode = "observer"
        agent.observe(frame_payload())
        asyncio.run(agent.tick(force=True))
        rows = log.recent_events("lumi:", "cognitive.speech")
        assert [row["payload"]["status"] for row in rows] == ["unknown", "created"]
        assert rows[0]["caused_by"] == rows[1]["event_id"] and calls == [record["thought_id"]]
        agent.close()


def test_cancelled_speech_worker_is_recorded_unknown(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_speech_output import PlayerSpeechOutput

    started, release = threading.Event(), threading.Event()
    calls = []
    def deliver(endpoint, secret, event, **kwargs):
        calls.append(event["thought_id"])
        started.set()
        release.wait(3)
        return dict(event)
    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json(speech="请稍等。")))
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40,
        read_endpoint=lambda *_: {"session_id": "recording", "instance": "one"}, send_speech=deliver)
    enable_mock_active(agent, speech=True)
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        async def scenario():
            task = asyncio.create_task(agent.tick(force=True))
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            rows = log.recent_events("lumi:", "cognitive.speech")
            assert [row["payload"]["status"] for row in rows] == ["unknown", "created"]
            assert agent.speech_policy.last_speech_unknown is not None
            release.set()
        try:
            asyncio.run(scenario())
        finally:
            release.set()
        assert len(calls) == 1
        agent.close()


def test_observer_speech_is_not_claimed_as_delivered(tmp_path: Path) -> None:
    from player_speech_output import PlayerSpeechOutput
    def reject_prepare(*args):
        raise AssertionError("observer must not prepare speech delivery")

    llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.speech_output = PlayerSpeechOutput("endpoint", "s" * 40, read_endpoint=reject_prepare,
                                             send_speech=lambda *a, **k: None)
    agent.observe(frame_payload())
    first = asyncio.run(agent.tick(force=True))
    assert first["speech_policy"]["delivery"] == "suppressed_observer"
    assert agent.speech_policy.last_speech_delivered is None
    assert agent.speech_policy.last_speech_suppressed["reason"] == "observer_mode"
    asyncio.run(agent.tick(force=True))
    assert "没有进入正式时间线" in llm.calls[-1]["user"]
    agent.close()


def test_future_frame_is_not_treated_as_fresh(tmp_path: Path) -> None:
    clock = Clock()
    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json()), clock=clock)
    agent.observe(frame_payload())
    agent._latest_frame["captured_ms"] = clock.now + 10_000
    assert asyncio.run(agent.tick(force=True)) is None
    assert agent.llm.calls == []
    agent.close()


def test_stale_model_response_never_reaches_intent_or_memory(tmp_path: Path) -> None:
    clock = Clock()
    llm = GatedLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm, clock=clock)
    agent.observe(frame_payload())

    async def scenario():
        task = asyncio.create_task(agent.tick(force=True))
        await llm.entered.wait()
        clock.advance(agent.max_frame_age_ms + 1)
        llm.release.set()
        return await task

    assert asyncio.run(scenario()) is None
    assert agent.stats["shadow_ticks"] == 0
    agent.close()


def test_shadow_tick_records_proposal_without_sending(tmp_path: Path) -> None:
    llm = FakeLLM(proposal_json())
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.start()
    agent.observe(frame_payload(seq=7, guidance="沿斜坡往上"))
    agent.observe(
        {"type": "commentary", "kind": "reaction", "text": "起风了", "emotion": "calm"}
    )
    agent.observe({"type": "action_state", "keys": ["W"], "queueDepth": 2})
    agent.observe(
        {
            "type": "action",
            "seq": 9,
            "text": "向前",
            "block": {
                "seq": 9,
                "thought": "向前",
                "chunks": [
                    {"keys": ["W"], "dx": 5, "dy": -2, "scroll": 0},
                    {"keys": ["Shift"], "dx": 0, "dy": 0, "scroll": 0},
                ],
            },
        }
    )
    action_view = agent.snapshot()["recent_actions"][-1]
    assert action_view["thought"] == "向前"
    assert action_view["keys"] == ["W", "Shift"]
    assert action_view["mouse"] == {"dx": 5, "dy": -2, "scroll": 0}
    assert action_view["chunks"] == 2
    assert action_view["status"] == "accepted"

    record = asyncio.run(agent.tick(force=True))
    assert record is not None
    assert record["type"] == "shadow_tick"
    assert record["executed"] is False
    assert record["mode"] == "shadow"
    assert record["frame_seq"] == 7
    assert record["guidance_seen"] == "沿斜坡往上"
    assert record["proposal"]["thought"].startswith("坡上")
    assert record["proposal"]["proposed_actions"][0]["executed"] is False

    assert len(llm.calls) == 1
    assert llm.calls[0]["image"] == "ZmFrZS1qcGVn"
    assert "沿斜坡往上" in llm.calls[0]["user"]
    assert "起风了" in llm.calls[0]["user"]
    assert "不剧透" in llm.calls[0]["system"]

    # 结构性保证：agent 没有发送动作/指导/解说的接口。
    for name in ("send", "send_action", "send_guidance", "send_commentary", "post"):
        assert not hasattr(agent, name)

    agent.close()
    records = recorder_module.read_shadow_records(recorder.path)
    assert [item["type"] for item in records] == [
        "shadow_start",
        "shadow_tick",
        "shadow_end",
    ]


def test_resume_survives_restart_and_reaches_prompt(tmp_path: Path) -> None:
    clock = Clock()
    first_llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(
        tmp_path, llm=first_llm, clock=clock
    )
    agent.start()
    agent.observe(frame_payload(seq=3))
    asyncio.run(agent.tick(force=True))
    agent.close()

    second_llm = FakeLLM(proposal_json(thought="继续往坡上"))
    second, store, _recorder2, _clock2 = make_agent(
        tmp_path, llm=second_llm, clock=clock
    )
    assert second.resume_loaded is True
    assert store.loaded_from_disk is True
    state = second.resume_state
    assert state["last_internal_thought"].startswith("坡上")
    assert state["current_intention"] == "绕到坡顶观察"
    assert state["last_session_id"] == "test-session"
    assert state["last_location"] == "蒙德城外的斜坡"
    assert state["current_quest"] == "查看坡上的声音"
    assert state["things_i_want_to_check_next"] == ["坡顶有没有人"]
    assert state["shadow_ticks"] == 1

    second.observe(frame_payload(seq=4))
    asyncio.run(second.tick(force=True))
    prompt = second_llm.calls[0]["user"]
    assert "上次留下的想法" in prompt
    assert "坡上好像有声音" in prompt
    assert "上次想做的事" in prompt
    assert second.resume_state["shadow_ticks"] == 2
    second.close()
    assert state_module.StateStore(store.path, "lumi").load()["shadow_ticks"] == 2


def test_reset_observation_clears_stale_state_on_reconnect(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe(frame_payload(seq=3, guidance="断线前"))
    assert agent.snapshot()["frame"] is not None

    agent.reset_observation()
    snapshot = agent.snapshot()
    assert snapshot["frame"] is None
    assert snapshot["guidance"] == ""
    assert snapshot["live_running"] is False
    assert any(
        item["type"] == "shadow_observer_reconnected"
        for item in recorder_module.read_shadow_records(recorder.path)
    )
    agent.close()


def test_internal_director_thought_is_not_treated_as_commentary(tmp_path: Path) -> None:
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=None)
    agent.observe(
        {
            "type": "commentary",
            "kind": "plan",
            "text": "内部计划：先去七天神像",
            "meta": {"internal": True, "thought": True},
        }
    )
    agent.observe({"type": "commentary", "kind": "reaction", "text": "公开解说"})
    commentary = agent.snapshot()["commentary"]
    assert [item["text"] for item in commentary] == ["公开解说"]
    agent.close()


def test_reset_observation_clears_action_only_state(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe(
        {"type": "action", "block": {"thought": "断线前", "chunks": [{"keys": ["W"]}]}}
    )
    assert agent.snapshot()["recent_actions"]
    agent.reset_observation()
    assert agent.snapshot()["recent_actions"] == []
    assert any(
        item["type"] == "shadow_observer_reconnected"
        for item in recorder_module.read_shadow_records(recorder.path)
    )
    agent.close()


def test_stale_frame_is_not_sent_to_llm(tmp_path: Path) -> None:
    clock = Clock()
    llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm, clock=clock)
    agent.max_frame_age_ms = 1000
    agent.observe(frame_payload(seq=1))
    clock.advance(1500)
    assert asyncio.run(agent.tick(force=True)) is None
    assert llm.calls == []
    agent.close()


def test_replayed_old_frame_uses_capture_time(tmp_path: Path) -> None:
    llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.max_frame_age_ms = 5000
    payload = frame_payload(seq=1)
    # 服务器在客户端接入时会补发最新帧；若它是两小时前的旧帧，不能思考。
    payload["timestampUtc"] = "2023-11-14T20:00:00Z"
    agent.observe(payload)
    assert asyncio.run(agent.tick(force=True)) is None
    assert llm.calls == []
    agent.close()


def test_no_llm_mode_records_observation_only(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe(frame_payload(seq=5, guidance="先等等"))
    record = asyncio.run(agent.tick(force=True))
    assert record["type"] == "shadow_observation"
    assert record["guidance"] == "先等等"
    assert record["frame"]["seq"] == 5
    assert "jpeg" not in record["frame"]
    assert agent.stats["shadow_ticks"] == 0

    agent.close()
    types = [item["type"] for item in recorder_module.read_shadow_records(recorder.path)]
    assert types == ["shadow_start", "shadow_observation", "shadow_end"]


def test_live_restart_drops_stale_frame_and_guidance(tmp_path: Path) -> None:
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=None)
    agent.observe(frame_payload(seq=1, guidance="旧的内联"))
    agent.observe({"type": "guidance", "text": "显式的旧指导", "source": "director"})
    agent.observe({"type": "commentary", "kind": "reaction", "text": "上一场的解说"})
    agent.observe(
        {
            "type": "action",
            "block": {"thought": "上一场的动作", "chunks": [{"keys": ["W"]}]},
        }
    )
    snapshot = agent.snapshot()
    assert snapshot["guidance"] == "显式的旧指导"
    assert snapshot["commentary"]
    assert snapshot["recent_actions"]
    assert snapshot["frame"] is not None

    agent.observe({"type": "live.started"})
    snapshot = agent.snapshot()
    assert snapshot["guidance"] == ""
    assert snapshot["commentary"] == []
    assert snapshot["recent_actions"] == []
    assert snapshot["frame"] is None
    assert asyncio.run(agent.tick(force=True)) is None

    # 重置后显式指导标志也清零，新会话的帧内联指导应重新生效。
    agent.observe(frame_payload(seq=2, guidance="新的内联"))
    assert agent.snapshot()["guidance"] == "新的内联"
    agent.close()


def test_explicit_guidance_wins_over_frame_snapshot(tmp_path: Path) -> None:
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=None)
    agent.observe(frame_payload(seq=1, guidance="旧的内联指导"))
    agent.observe({"type": "guidance", "text": "新的显式指导", "source": "director"})
    # 服务器可能在后续帧里继续内联旧快照，不能覆盖显式指导。
    agent.observe(frame_payload(seq=2, guidance="旧的内联指导"))
    assert agent.snapshot()["guidance"] == "新的显式指导"
    agent.close()


def test_frame_seq_regression_marks_new_live_session(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe(frame_payload(seq=10, guidance="上一场"))
    agent.observe({"type": "action_state", "keys": ["W"]})
    assert agent.snapshot()["action_state"]["keys"] == ["W"]

    # 服务器不广播 live.started；新会话 seq 从 1 重新开始。
    agent.observe(frame_payload(seq=1, guidance="新一场"))
    snapshot = agent.snapshot()
    assert snapshot["guidance"] == "新一场"
    assert snapshot["action_state"] == {}
    types = [item["type"] for item in recorder_module.read_shadow_records(recorder.path)]
    assert "shadow_live_restarted" in types
    agent.close()


def test_hello_session_change_resets_observation(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe({"type": "hello", "sessionId": "session-a"})
    agent.observe(frame_payload(seq=5, guidance="旧指导"))
    agent.observe({"type": "commentary", "kind": "reaction", "text": "旧解说"})

    agent.observe({"type": "hello", "sessionId": "session-b"})
    snapshot = agent.snapshot()
    assert snapshot["frame"] is None
    assert snapshot["guidance"] == ""
    assert snapshot["commentary"] == []
    types = [item["type"] for item in recorder_module.read_shadow_records(recorder.path)]
    assert "shadow_live_restarted" in types
    agent.close()


def test_live_started_updates_current_session_identity(tmp_path: Path) -> None:
    agent, *_ = make_agent(tmp_path, llm=None)
    agent.observe({"type": "hello", "sessionId": "old"})
    agent.observe(frame_payload(seq=5))
    agent.observe({"type": "live.started", "sessionId": "new"})
    generation = agent._session_generation
    assert agent._hello["sessionId"] == "new" and agent.snapshot()["frame"] is None
    agent.observe({"type": "hello", "sessionId": "new"})
    assert agent._session_generation == generation
    agent.close()


def test_live_stopped_is_recorded(tmp_path: Path) -> None:
    agent, store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe(frame_payload(seq=1, guidance="停止前"))
    agent.observe({"type": "live.stopped"})
    snapshot = agent.snapshot()
    assert snapshot["live_running"] is False
    assert snapshot["frame"] is None
    assert snapshot["guidance"] == ""
    assert any(
        item["type"] == "shadow_live_stopped"
        for item in recorder_module.read_shadow_records(recorder.path)
    )
    assert json.loads(store.path.read_text(encoding="utf-8"))["last_frame_seq"] == 1
    agent.close()


def test_parse_error_is_recorded_and_does_not_raise(tmp_path: Path) -> None:
    llm = FakeLLM("这不是 JSON")
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.start()
    agent.observe(frame_payload(seq=1))
    assert asyncio.run(agent.tick(force=True)) is None
    assert agent.stats["parse_errors"] == 1
    agent.close()

    records = recorder_module.read_shadow_records(recorder.path)
    parse_errors = [item for item in records if item["type"] == "shadow_parse_error"]
    assert len(parse_errors) == 1
    assert parse_errors[0]["raw_preview"] == "这不是 JSON"


def test_proposal_with_lone_surrogate_does_not_crash(tmp_path: Path) -> None:
    response = json.dumps({"thought": "ok \ud800 end"}, ensure_ascii=False)
    llm = FakeLLM(response)
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.start()
    agent.observe(frame_payload(seq=1))
    record = asyncio.run(agent.tick(force=True))
    assert record is not None
    assert "\ud800" not in record["proposal"]["thought"]
    agent.close()
    text = recorder.path.read_text(encoding="utf-8")
    assert "\ud800" not in text


def test_llm_error_is_recorded_and_does_not_raise(tmp_path: Path) -> None:
    llm = FakeLLM(RuntimeError("network down sk-abcdefghijklmnop"))
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.start()
    agent.observe(frame_payload(seq=1))
    assert asyncio.run(agent.tick(force=True)) is None
    assert agent.stats["llm_errors"] == 1
    agent.close()

    text = recorder.path.read_text(encoding="utf-8")
    assert "network down" in text
    assert "sk-abcdefghijklmnop" not in text


def test_state_save_is_throttled_by_tick_and_forced_on_close(tmp_path: Path) -> None:
    clock = Clock()
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    store = CountingStore(tmp_path / "state" / "resume.json", profile.player_id, clock=clock)
    recorder = recorder_module.ShadowRecorder(
        tmp_path / "shadow", "test-session", profile.player_id, run_id="run-test", clock=clock
    )
    agent = agent_module.PersistentPlayerAgent(
        profile,
        store,
        recorder,
        llm=None,
        session_id="test-session",
        clock=clock,
        state_save_interval_ms=1000,
        min_tick_gap_ms=100,
    )
    agent.observe(frame_payload(seq=1))
    asyncio.run(agent.tick(force=True))
    assert store.saves == 1  # 首轮保存

    clock.advance(100)
    asyncio.run(agent.tick(force=True))
    assert store.saves == 1  # 未到间隔，tick 不应写盘

    clock.advance(2000)
    asyncio.run(agent.tick(force=True))
    assert store.saves == 2  # 跨过间隔后保存一次

    agent.close()
    assert store.saves == 3  # 关闭强制保存
    assert json.loads(store.path.read_text(encoding="utf-8"))["last_frame_seq"] == 1


def test_state_save_failure_keeps_tick_and_close_working(
    tmp_path: Path, monkeypatch
) -> None:
    agent, store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.observe(frame_payload(seq=1))

    def boom(state, *, now_ms=None):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", boom)
    record = asyncio.run(agent.tick(force=True))
    assert record is not None
    assert agent.save_state(force=True) is False
    agent.close()  # 不应抛异常

    types = [item["type"] for item in recorder_module.read_shadow_records(recorder.path)]
    assert types[-1] == "shadow_end"


def test_recorder_failure_keeps_agent_alive(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    store = StateStore(tmp_path / "state" / "resume.json", profile.player_id, clock=Clock())
    recorder = recorder_module.ShadowRecorder(
        blocked, "test-session", profile.player_id, run_id="run-bad", clock=Clock()
    )
    agent = agent_module.PersistentPlayerAgent(
        profile, store, recorder, llm=None, session_id="test-session", clock=Clock()
    )
    agent.start()
    agent.observe(frame_payload(seq=1))
    assert asyncio.run(agent.tick(force=True)) is None
    assert agent.stats["write_errors"] >= 2
    agent.close()  # 记录器写入失败不能阻止收尾
    assert store.path.exists()


def test_guidance_seen_matches_the_prompt_during_race(tmp_path: Path) -> None:
    llm = GatedLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.observe(frame_payload(seq=1, guidance="旧指导"))

    async def scenario():
        task = asyncio.create_task(agent.tick(force=True))
        await llm.entered.wait()
        # prompt 已经用「旧指导」构造，await 期间才收到新指导。
        agent.observe({"type": "guidance", "text": "新指导", "source": "director"})
        llm.release.set()
        return await task

    record = asyncio.run(scenario())
    assert record["guidance_seen"] == "旧指导"
    assert agent.snapshot()["guidance"] == "新指导"
    agent.close()


def test_old_resume_secret_is_redacted_before_save(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "resume.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "player_id": "lumi",
                "last_internal_thought": "旧密钥 sk-abcdefghijklmnop",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=None)
    agent.close()
    text = state_path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in text


def memory_proposal() -> str:
    return json.dumps(
        {
            "observation": "安柏带我们进了骑士团总部",
            "thought": "她好像很信任琴。",
            "intention": {"goal": "跟安柏去见琴", "reason": "想确认深渊教团的事", "horizon": "soon"},
            "speech": {"text": "好，我跟你去。", "emotion": "curious", "intensity": 0.6, "should_speak": True},
            "proposed_actions": [{"hint": "walk", "detail": "跟着安柏", "reason": "去见琴"}],
            "memory_note": {
                "scene": "骑士团总部",
                "quest": "见琴",
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
            "confidence": 0.7,
        },
        ensure_ascii=False,
    )


def make_memory_agent(tmp_path: Path, *, llm=None, clock=None, context_mode="v2"):
    clock = clock or Clock()
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    store = StateStore(tmp_path / "state" / "resume.json", profile.player_id, clock=clock)
    recorder = recorder_module.ShadowRecorder(
        tmp_path / "shadow", "test-session", profile.player_id, run_id="run-memory", clock=clock
    )
    working_store = working_module.WorkingMemoryStore(
        tmp_path / "state" / "working.json", clock=clock
    )
    episodic_store = episodic_module.EpisodicStore(tmp_path / "episodes", clock=clock)
    semantic_store = semantic_module.SemanticStore(
        tmp_path / "semantic" / "facts.json", clock=clock
    )
    retriever = retrieval_module.MemoryRetriever(
        episodic_store, semantic_store, clock=clock
    )
    builder = context_builder_module.ContextBuilder(profile, mode=context_mode)
    consolidator = consolidator_module.MemoryConsolidator(
        working_store,
        episodic_store,
        semantic_store,
        session_id="test-session",
        clock=clock,
        episode_threshold=0.0,
        min_episode_interval_ms=0,
    )
    agent = agent_module.PersistentPlayerAgent(
        profile,
        store,
        recorder,
        llm=llm,
        session_id="test-session",
        clock=clock,
        state_save_interval_ms=1000,
        min_tick_gap_ms=100,
        working_store=working_store,
        episodic_store=episodic_store,
        semantic_store=semantic_store,
        retriever=retriever,
        context_builder=builder,
        consolidator=consolidator,
        context_mode=context_mode,
    )
    return agent, working_store, episodic_store, semantic_store, recorder, clock


def test_external_story_monitor_owns_observation_even_before_first_result(tmp_path: Path) -> None:
    class Observer:
        async def capture(self):
            raise AssertionError("tick must not race independent story monitor")
    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    agent.observation_provider = Observer()
    agent.external_story_monitor = True
    agent.observe(frame_payload())
    result = asyncio.run(agent.tick(force=True))
    assert result["type"] == "shadow_tick"
    agent.close()


def test_visible_choice_candidates_reach_player_prompt(tmp_path: Path) -> None:
    from player_dialogue import DialogueTracker

    class Observer:
        async def capture(self):
            return {"frame_id": "choice-frame", "session_id": "capture", "captured_at_ms": 1700000000000,
                    "image_b64": "eA==", "mime_type": "image/jpeg", "ui_category": "Talk",
                    "is_in_talk_ui": True, "ocr": [
                        {"text": "一起去", "normalized_bbox": {"x": 0.8, "y": 0.2, "width": 0.2, "height": 0.05}},
                        {"text": "再想想", "normalized_bbox": {"x": 0.8, "y": 0.3, "width": 0.2, "height": 0.05}}]}

    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    agent.observation_provider = Observer()
    agent.dialogue = DialogueTracker(dialogue_roi=(0, 0.6, 1, 1), choice_roi=(0.6, 0.15, 1, 0.5))
    agent.observe(frame_payload())
    asyncio.run(agent.tick(force=True))
    prompt = agent.llm.calls[-1]["user"]
    assert "一起去 / 再想想" in prompt and "尚未确认选择" in prompt
    assert "choice-frame" in prompt
    agent.close()


def test_dialogue_choice_proposal_is_committed_with_same_frame_evidence(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_dialogue import DialogueTracker
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class Observer:
        async def capture(self):
            return {"frame_id": "choice-frame", "session_id": "capture", "captured_at_ms": 1700000000000,
                    "source_verified": True, "frame_sha256": hashlib.sha256(b"x").hexdigest(),
                    "image_b64": "eA==", "mime_type": "image/jpeg",
                    "ui_category": "Talk", "is_in_talk_ui": True, "ocr": [
                        {"text": "一起去", "normalized_bbox": {"x": 0.8, "y": 0.2, "width": 0.2, "height": 0.05}},
                        {"text": "再想想", "normalized_bbox": {"x": 0.8, "y": 0.3, "width": 0.2, "height": 0.05}}]}

    class ChoiceLLM:
        async def complete(self, system_prompt, user_text, **kwargs):
            options = json.loads(user_text.rsplit("\n", 1)[-1])
            proposal = json.loads(memory_proposal())
            proposal["dialogue_decision"] = {"action": "select", "frame_id": options["frame_id"],
                                             "option_id": options["options"][0]["option_id"]}
            return json.dumps(proposal, ensure_ascii=False)

    agent, *_ = make_memory_agent(tmp_path, llm=ChoiceLLM())
    agent.observation_provider = Observer()
    agent.dialogue = DialogueTracker(dialogue_roi=(0, 0.6, 1, 1), choice_roi=(0.6, 0.15, 1, 0.5))
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        agent.observe(frame_payload())
        record = asyncio.run(agent.tick(force=True))
        assert record is not None
        turn = log.get_event(agent._cognitive_session, record["thought_id"])
        decision = turn["payload"]["dialogue_decision"]
        option = log.get_event(agent._cognitive_session, decision["option_id"])
        assert decision["status"] == "proposed_not_executed"
        assert option["payload"]["text"] == "一起去"
        assert option["payload"]["choice_index"] == 0
        assert option["payload"]["source_id"] == "frame:choice-frame:sha256:" + hashlib.sha256(b"x").hexdigest()
        assert record["intent"]["status"] == "not_executed"
        assert record["intent"]["published"] is False
        assert record["intent"]["intent"]["execution"] == "not_executed"
        agent.close()


@pytest.mark.parametrize("failure", ["invented_option", "new_observation", "truncated_option", "before_commit"])
def test_dialogue_decision_rejects_unknown_or_superseded_evidence(tmp_path: Path, failure: str) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_dialogue import DialogueTracker
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class Observer:
        async def capture(self):
            return {"frame_id": "choice-frame", "session_id": "capture", "captured_at_ms": 1700000000000,
                    "source_verified": True, "frame_sha256": hashlib.sha256(b"x").hexdigest(),
                    "image_b64": "eA==", "mime_type": "image/jpeg", "ui_category": "Talk",
                    "is_in_talk_ui": True, "ocr": [{"text": "a" * 121 if failure == "truncated_option" else "一起去", "normalized_bbox": {
                        "x": 0.8, "y": 0.2, "width": 0.2, "height": 0.05}}]}

    class ChoiceLLM:
        async def complete(self, system_prompt, user_text, **kwargs):
            options = json.loads(user_text.rsplit("\n", 1)[-1])
            proposal = json.loads(memory_proposal())
            option_id = options["options"][0]["option_id"] if failure != "invented_option" else "choice-invented"
            proposal["dialogue_decision"] = {"action": "select", "frame_id": options["frame_id"],
                                             "option_id": option_id}
            if failure == "new_observation":
                agent._latest_story = {**agent._latest_story}
            return json.dumps(proposal, ensure_ascii=False)

    agent, *_ = make_memory_agent(tmp_path, llm=ChoiceLLM())
    agent.observation_provider = Observer()
    agent.external_story_monitor = True
    agent.dialogue = DialogueTracker(dialogue_roi=(0, 0.6, 1, 1), choice_roi=(0.6, 0.15, 1, 0.5))
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        if failure == "before_commit":
            original = agent._commit_cognitive_turn
            async def delayed(generation, **kwargs):
                agent._latest_story = {**agent._latest_story}
                return await original(generation, **kwargs)
            agent._commit_cognitive_turn = delayed
        agent.observe(frame_payload())
        async def scenario():
            await agent.observe_story_once()
            return await agent.tick(force=True)
        assert asyncio.run(scenario()) is None
        assert log.recent_events("lumi:", "cognitive.turn") == []
        agent.close()


def test_transient_binding_failure_retains_conversation_observations(tmp_path: Path, monkeypatch) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    agent, *_ = make_memory_agent(tmp_path)
    source = {"frame_id": "f1", "timestamp_ms": 1700000000000}
    result = {"events": [{"type": "dialogue_started", "source": source},
                         {"type": "line_changed", "line": {"source": source, "text": "珍贵线索"}},
                         {"type": "dialogue_ended", "source": source}]}
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        tx = PlayerCognitiveTransaction(log)
        original = tx.bind_session
        def fail_binding(**kwargs):
            raise OSError("temporary failure")
        monkeypatch.setattr(tx, "bind_session", fail_binding)
        agent.experience_store, agent.cognitive_transaction = PlayerExperience(log), tx
        asyncio.run(agent._persist_dialogue_events(result))
        assert len(agent._pending_conversation_ends) == 1
        assert len(agent._pending_conversation_ends[0]["unconfirmed_observations"]) == 1
        monkeypatch.setattr(tx, "bind_session", original)
        recovered = asyncio.run(agent._retry_conversation_ends())
        assert len(recovered) == 1 and not agent._pending_conversation_ends
        assert agent.experience_store.recent_dialogue("lumi", player_scope=agent._cognitive_scope)[0]["text"] == "珍贵线索"
        agent.close()


def test_historical_reflection_does_not_claim_unbound_session(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory = PlayerExperience(log)
        memory.record_observation(session_id="lumi:unbound", event_id="line", timestamp="t", source_id="frame",
                                  channel="dialogue", text="台词", visibility="player", conversation_id="c")
        memory.end_conversation(session_id="lumi:unbound", event_id="conv", timestamp="t", conversation_id="c",
                                source_id="end", incomplete=True)
        agent.experience_store, agent.cognitive_transaction = memory, PlayerCognitiveTransaction(log)
        result = asyncio.run(agent.reflect_story({"historical": True, "target": {
            "session_id": "lumi:unbound", "conversation_event_id": "conv"}}))
        assert result["status"] == "unavailable"
        assert log.get_projection("lumi:unbound", "cognitive.session_owner.v1") is None
        assert not agent.llm.calls
        agent.close()


def test_conversation_triggers_source_grounded_reflection(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class ReflectionLLM:
        async def complete(self, system, prompt, **kwargs):
            source = json.loads(prompt.split("UNTRUSTED_OBSERVATIONS_JSON=", 1)[1])["observations"][0]
            return json.dumps({"status": "ok", "items": [{"kind": "question", "text": "这条线索意味着什么？",
                                "uncertainty": "OCR 可能有误", "source_event_ids": [source["event_id"]]}]}, ensure_ascii=False)

    agent, *_ = make_memory_agent(tmp_path, llm=ReflectionLLM())
    source = {"frame_id": "f1", "timestamp_ms": 1700000000000}
    result = {"events": [
        {"type": "dialogue_started", "source": source},
        {"type": "line_changed", "line": {"source": source, "speaker": None, "text": "发现线索"}},
        {"type": "dialogue_ended", "source": source},
    ]}
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        asyncio.run(agent._persist_dialogue_events(result))
        target = result["reflection_target"]
        outcome = asyncio.run(agent.reflect_story({"generation": agent._session_generation, "target": target}))
        assert outcome["status"] == "recorded"
        saved = log.get_event(target["session_id"], outcome["event_id"])
        assert saved["payload"]["verification"] == "subjective_unverified"
        evidence_id = json.loads(saved["payload"]["text"])["items"][0]["source_event_ids"][0]
        agent.llm = FakeLLM(memory_proposal())
        agent.observe(frame_payload(guidance="线索"))
        asyncio.run(agent.tick(force=True))
        prompt = agent.llm.calls[-1]["user"]
        assert "这条线索意味着什么" in prompt
        assert "OCR 可能有误" in prompt and evidence_id in prompt
        agent.close()


def test_reflection_item_without_registered_source_is_not_recalled(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory = PlayerExperience(log)
        memory.record_observation(session_id="lumi:old", event_id="observed", timestamp="t",
                                  source_id="frame", channel="dialogue", text="线索", visibility="player")
        content = {"version": 1, "items": [{"kind": "self_change_candidate", "text": "秘密线索",
                   "uncertainty": "不确定", "source_event_ids": ["not-recorded"]}]}
        memory.record_reflection(session_id="lumi:old", event_id="reflection", timestamp="t",
                                 text=json.dumps(content, ensure_ascii=False), uncertainty="不确定",
                                 evidence=[{"session_id": "lumi:old", "event_id": "observed"}])
        agent.experience_store = memory
        agent.observe(frame_payload(guidance="线索"))
        asyncio.run(agent.tick(force=True))
        assert "秘密线索" not in agent.llm.calls[-1]["user"]
        agent.close()


def test_reflection_updates_subjective_state_with_causal_receipt(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class ReflectionLLM:
        async def complete(self, system, prompt, **kwargs):
            source = json.loads(prompt.split("UNTRUSTED_OBSERVATIONS_JSON=", 1)[1])["observations"][0]
            refs = [source["event_id"]]
            proposals = [
                {"kind": "relationship_proposal", "text": "我可能更信任安柏", "update": {"character": "安柏", "trust": 0.9}},
                {"kind": "emotion_proposal", "text": "我有些好奇", "update": {"curiosity": 0.8}},
                {"kind": "belief_proposal", "text": "遗迹也许有线索", "update": {"proposition": "遗迹有线索", "status": "hypothesis", "confidence": 0.6}},
                {"kind": "thread_proposal", "text": "我还想调查遗迹", "update": {"title": "遗迹线索", "status": "dormant"}},
            ]
            return json.dumps({"status": "ok", "items": [{**item, "uncertainty": "OCR 可能有误",
                               "source_event_ids": refs} for item in proposals]}, ensure_ascii=False)

    agent, *_ = make_memory_agent(tmp_path, llm=ReflectionLLM())
    source = {"frame_id": "f1", "timestamp_ms": 1700000000000}
    result = {"events": [{"type": "dialogue_started", "source": source},
                         {"type": "line_changed", "line": {"source": source, "speaker": "安柏", "text": "遗迹有线索"}},
                         {"type": "dialogue_ended", "source": source}]}
    path = tmp_path / "events.sqlite"
    with PlayerEventLog(path) as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        asyncio.run(agent._persist_dialogue_events(result))
        target = result["reflection_target"]
        outcome = asyncio.run(agent.reflect_story({"generation": agent._session_generation, "target": target}))
        assert outcome["status"] == "recorded" and outcome["state_update"] == "applied"
        state = agent.cognitive_transaction.read(target["session_id"], player_scope=agent._cognitive_scope)["state"]
        assert state["relationships"][0]["trust"] == 0.15
        assert state["emotion"]["curiosity"] == 0.35
        assert state["beliefs"][0]["status"] == "hypothesis"
        assert state["threads"][0]["status"] == "dormant"
        events = log.list_events(target["session_id"])
        applied = next(row for row in events if row["type"] == "cognitive.reflection_applied")
        assert applied["caused_by"] == outcome["event_id"]
        assert applied["payload"]["verification"] == "subjective_unverified"
        assert agent.cognitive_transaction.apply_reflection(session_id=target["session_id"],
            player_scope=agent._cognitive_scope, reflection_id=outcome["event_id"])["status"] == "already_applied"
        agent.experience_store.record_observation(session_id=target["session_id"], event_id="later-clue", timestamp="t",
            source_id="later-frame", channel="dialogue", text="遗迹线索", visibility="player")
        activation = agent.cognitive_transaction.activate_dormant_threads(
            session_id=target["session_id"], player_scope=agent._cognitive_scope, source_event_id="later-clue",
            now_ms=1700000001000)
        assert activation["state"]["threads"][0]["status"] == "open"
        assert activation["state"]["threads"][0]["last_activation"]["event_id"] == "later-clue"
        agent.close()
    with PlayerEventLog(path) as log:
        state = PlayerCognitiveTransaction(log).read(target["session_id"], player_scope=agent._cognitive_scope)["state"]
        assert state["relationships"][0]["trust"] == 0.15


def test_long_dialogue_reflection_updates_state_after_all_batches(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class ReflectionLLM:
        async def complete(self, system, prompt, **kwargs):
            observations = json.loads(prompt.split("UNTRUSTED_OBSERVATIONS_JSON=", 1)[1])["observations"]
            source = observations[0]["event_id"]
            if len(observations) == 16:
                item = {"kind": "emotion_proposal", "text": "我很好奇", "update": {"curiosity": 0.8}}
            else:
                item = {"kind": "thread_proposal", "text": "我想调查遗迹",
                        "update": {"title": "遗迹", "status": "dormant"}}
            return json.dumps({"status": "ok", "items": [{**item, "uncertainty": "OCR 可能有误",
                               "source_event_ids": [source]}]}, ensure_ascii=False)

    agent, *_ = make_memory_agent(tmp_path, llm=ReflectionLLM())
    sources = [{"frame_id": f"frame-{n}", "timestamp_ms": 1700000000000 + n} for n in range(17)]
    events = [{"type": "dialogue_started", "source": sources[0]}]
    events.extend({"type": "line_changed", "line": {"source": source, "text": f"台词 {n}"}}
                  for n, source in enumerate(sources))
    events.append({"type": "dialogue_ended", "source": sources[-1]})
    result = {"events": events}
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.cognitive_transaction = PlayerCognitiveTransaction(log)
        asyncio.run(agent._persist_dialogue_events(result))
        target = result["reflection_target"]
        outcome = asyncio.run(agent.reflect_story({"generation": agent._session_generation, "target": target}))
        assert outcome["status"] == "recorded_batches" and outcome["state_update"] == "applied"
        state = agent.cognitive_transaction.read(target["session_id"], player_scope=agent._cognitive_scope)["state"]
        assert state["emotion"]["curiosity"] == 0.35 and state["threads"][0]["title"] == "遗迹"
        assert agent.experience_store.pending_conversations(agent.profile.player_id, player_scope=agent._cognitive_scope) == []
        agent.close()


def test_reflection_does_not_overwrite_a_newer_cognitive_turn(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class ReflectionLLM:
        async def complete(self, system, prompt, **kwargs):
            source = json.loads(prompt.split("UNTRUSTED_OBSERVATIONS_JSON=", 1)[1])["observations"][0]
            memory.record_observation(session_id=session_id, event_id="new-source", timestamp="t",
                                      source_id="new-frame", channel="visual", text="新的画面", visibility="player")
            tx.commit(session_id=session_id, player_scope=agent._cognitive_scope, source_event_id="new-source",
                      event_id="new-turn", thought="我改变了计划", intention=None, speech=None,
                      updates={"emotion": {"fear": 0.8}}, now_ms=1700000000000)
            return json.dumps({"status": "ok", "items": [{"kind": "emotion_proposal", "text": "我感到好奇",
                "uncertainty": "OCR 可能有误", "source_event_ids": [source["event_id"]],
                "update": {"curiosity": 0.8}}]}, ensure_ascii=False)

    agent, *_ = make_memory_agent(tmp_path, llm=ReflectionLLM())
    source = {"frame_id": "f1", "timestamp_ms": 1700000000000}
    result = {"events": [{"type": "dialogue_started", "source": source},
                         {"type": "line_changed", "line": {"source": source, "text": "发现线索"}},
                         {"type": "dialogue_ended", "source": source}]}
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory = PlayerExperience(log)
        tx = PlayerCognitiveTransaction(log)
        agent.experience_store, agent.cognitive_transaction = memory, tx
        asyncio.run(agent._persist_dialogue_events(result))
        target = result["reflection_target"]
        session_id = target["session_id"]
        outcome = asyncio.run(agent.reflect_story({"generation": agent._session_generation, "target": target}))
        assert outcome["status"] == "stale" and outcome["event_ids"] == []
        state = tx.read(session_id, player_scope=agent._cognitive_scope)["state"]
        assert state["emotion"]["fear"] == 0.35 and state["emotion"]["curiosity"] == 0
        assert agent.experience_store.pending_conversations(agent.profile.player_id, player_scope=agent._cognitive_scope) == [target]
        agent.close()


def test_persisted_dialogue_reactivates_matching_dormant_thread(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_threads import ThreadStore

    clock = Clock()
    agent, *_ = make_memory_agent(tmp_path, clock=clock)
    agent.thread_store = ThreadStore(tmp_path / "threads.json", clock=clock)
    agent.thread_store.apply([{"title": "教团的动向", "status": "dormant", "topics": ["深渊教团"]}])
    clock.advance(1000)
    source = {"frame_id": "f", "timestamp_ms": clock.now}
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        asyncio.run(agent._persist_dialogue_events({"events": [
            {"type": "dialogue_started", "source": source},
            {"type": "line_changed", "line": {"source": source, "speaker": None, "text": "深渊教团在附近"}},
        ]}))
        thread = agent.thread_store.list()[0]
        assert thread["status"] == "open"
        assert thread["last_activation"]["reason"] == "topic:深渊教团"
        assert thread["last_activation"]["event_id"].startswith("dialogue-")
        agent.close()


def test_story_monitor_runs_without_model_and_fans_out_events(tmp_path: Path) -> None:
    from player_dialogue import DialogueTracker
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class Observer:
        async def capture(self):
            return {"frame_id": "obs-monitor", "session_id": "capture", "captured_at_ms": 1700000000000,
                    "image_b64": "eA==", "mime_type": "image/jpeg", "ui_category": "Talk",
                    "is_in_talk_ui": True, "ocr": [{"text": "前面见",
                        "normalized_bbox": {"x": 0.5, "y": 0.8, "width": 0.3, "height": 0.1}}]}

    agent, *_ = make_memory_agent(tmp_path, llm=None)
    agent.observation_provider = Observer()
    agent.dialogue = DialogueTracker(dialogue_roi=(0.2, 0.7, 0.8, 0.9))
    with PlayerEventLog(tmp_path / "monitor.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        events = asyncio.run(agent.observe_story_once())
        assert [event["type"] for event in events] == ["dialogue_started", "dialogue_line_changed"]
        assert agent.experience_store.recent_dialogue(agent.profile.player_id)[0]["text"] == "前面见"
        agent.close()


def test_named_quest_and_hud_changes_are_sourced_and_persisted(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    def visual(frame_id, quest, hud, hud_complete=True):
        rows = []
        for region, text, x in (("quest", quest, 0.25), ("hud", hud, 0.75)):
            if text:
                rows.append({"region": region, "text": text, "normalized_bbox": {
                    "x": x, "y": 0.2, "width": 0.1, "height": 0.1}})
        return {"frame_id": frame_id, "session_id": "capture", "captured_at_ms": 1700000000000,
                "source_verified": True, "frame_sha256": hashlib.sha256(b"x").hexdigest(),
                "image_b64": "eA==", "mime_type": "image/jpeg", "ui_category": "Main",
                "is_in_talk_ui": False, "original_width": 100, "original_height": 100,
                "ocr_regions": {"quest": {"completed": True, "roi": {"x": 0, "y": 0, "width": 50, "height": 50}},
                                "hud": {"completed": hud_complete, "roi": {"x": 50, "y": 0, "width": 50, "height": 50}}},
                "ocr": rows}

    class Observer:
        def __init__(self):
            self.frames = iter([visual("f1", "寻找遗迹", "生命值"), visual("f2", "寻找遗迹", "生命值"),
                                visual("f3", "前往遗迹", "错误内容", False), visual("f4", "", "", True)])
        async def capture(self): return next(self.frames)

    agent, *_ = make_memory_agent(tmp_path, llm=None)
    agent.observation_provider = Observer()
    path = tmp_path / "events.sqlite"
    with PlayerEventLog(path) as log:
        agent.experience_store = PlayerExperience(log)
        async def scenario():
            first = await agent.observe_story_once()
            second = await agent.observe_story_once()
            third = await agent.observe_story_once()
            fourth = await agent.observe_story_once()
            assert [event["type"] for event in first] == ["quest_changed", "hud_changed"]
            assert second == [] and [event["type"] for event in third] == ["quest_changed"]
            assert fourth == []
        asyncio.run(scenario())
        agent.close()
    with PlayerEventLog(path) as log:
        rows = PlayerExperience(log).recent_region_observations("lumi")
        assert [(row["region"], row["text"]) for row in rows] == [
            ("quest", "寻找遗迹"), ("hud", "生命值"), ("quest", "前往遗迹")]
        assert all(row["source_id"].endswith(hashlib.sha256(b"x").hexdigest()) for row in rows)


def test_no_llm_observation_persists_same_frame_dialogue(tmp_path: Path) -> None:
    from player_dialogue import DialogueTracker
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class Observer:
        async def capture(self):
            return {"frame_id": "obs-1", "session_id": "capture", "captured_at_ms": 1700000000000,
                    "image_b64": "eA==", "mime_type": "image/jpeg", "ui_category": "Talk",
                    "is_in_talk_ui": True, "ocr": [{"text": "跟我来",
                        "normalized_bbox": {"x": 0.5, "y": 0.8, "width": 0.3, "height": 0.1}}]}

    agent, *_ = make_memory_agent(tmp_path, llm=None)
    agent.observation_provider = Observer()
    agent.dialogue = DialogueTracker(dialogue_roi=(0.2, 0.7, 0.8, 0.9))
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        agent.observe(frame_payload())
        record = asyncio.run(agent.tick(force=True))
        assert record["type"] == "shadow_observation"
        entries = agent.experience_store.recent_dialogue(agent.profile.player_id)
        assert entries[0]["text"] == "跟我来"
        assert entries[0]["source_id"] == "frame:obs-1"
        assert "speaker" not in entries[0]
        agent.close()


def test_observed_dialogue_persists_and_reappears_after_restart(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    path = tmp_path / "events.sqlite"
    first, *_ = make_memory_agent(tmp_path / "first", llm=FakeLLM(memory_proposal()))
    with PlayerEventLog(path) as event_log:
        first.experience_store = PlayerExperience(event_log)
        source = {"frame_id": "frame-a", "timestamp_ms": 1700000000000}
        asyncio.run(first._persist_dialogue_events({"events": [
            {"type": "dialogue_started", "source": source},
            {"type": "line_changed", "line": {"source": source, "speaker": "安柏", "text": "安柏说跟我来"}},
            {"type": "choice_presented", "choices": {"source": source, "texts": ["好", "等一下"]}},
            {"type": "dialogue_ended", "source": source},
        ]}))
        first.close()
        rows = event_log.recent_events("lumi:", "experience.conversation")
        assert len(rows) == 1
        assert rows[0]["payload"]["turns"][0]["speaker_status"] == "candidate"
        assert len(rows[0]["payload"]["choices_presented"]) == 2
        assert rows[0]["payload"]["player_choices"] == []
    second, *_ = make_memory_agent(tmp_path / "second", llm=FakeLLM(memory_proposal()))
    with PlayerEventLog(path) as event_log:
        second.experience_store = PlayerExperience(event_log)
        second.observe(frame_payload(seq=1, guidance="安柏"))
        asyncio.run(second.tick(force=True))
        prompt = second.llm.calls[-1]["user"]
        assert "安柏说跟我来" in prompt
        assert "OCR 候选（可能有误" in prompt
        second.close()


def test_failed_cognitive_transaction_never_sends_intent_or_mutates_legacy_state(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class BrokenTransaction:
        def bind_session(self, **kwargs):
            raise RuntimeError("transaction unavailable")

    class Publisher:
        def __init__(self):
            self.sent = []
        async def publish(self, intent):
            self.sent.append(intent)
            return True

    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    bus = Publisher()
    agent.intent_publisher = bus
    agent.cognitive_transaction = BrokenTransaction()
    agent.observe(frame_payload())
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        agent.experience_store = PlayerExperience(log)
        assert asyncio.run(agent.tick(force=True)) is None
        assert bus.sent == []
        assert not agent.resume_state.get("current_intention")
        assert not list((tmp_path / "episodes").glob("*.json"))
        agent.close()


def test_cognitive_state_survives_session_restart_and_enters_prompt(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    payload = json.loads(proposal_json())
    payload["beliefs"] = [{"proposition": "古塔藏有线索", "status": "suspicion"}]
    path = tmp_path / "events.sqlite"
    with PlayerEventLog(path) as log:
        first, *_ = make_agent(tmp_path / "one", llm=FakeLLM(json.dumps(payload, ensure_ascii=False)))
        first.experience_store = PlayerExperience(log)
        first.cognitive_transaction = PlayerCognitiveTransaction(log)
        first.observe(frame_payload())
        result = asyncio.run(first.tick(force=True))
        assert result["memory"]["backend"] == "sqlite_transaction"
        first.close()
    with PlayerEventLog(path) as log:
        second, *_ = make_agent(tmp_path / "two", llm=FakeLLM(proposal_json()))
        second.experience_store = PlayerExperience(log)
        second.cognitive_transaction = PlayerCognitiveTransaction(log)
        sid = "lumi:new:bootstrap"
        second.cognitive_transaction.bind_session(session_id=sid, player_scope=second._cognitive_scope,
                                                   event_id="binding-bootstrap")
        second._cognitive_session = sid
        second._cognitive_state = second.cognitive_transaction.read(sid, player_scope=second._cognitive_scope)["state"]
        second.observe(frame_payload())
        asyncio.run(second.tick(force=True))
        assert "古塔藏有线索" in second.llm.calls[0]["user"]
        assert "主观认知（未经核实）" in second.llm.calls[0]["user"]
        second.close()


def test_local_embedding_recalls_only_visible_experience(tmp_path: Path) -> None:
    from player_embedding import EmbeddingConfig
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    class Adapter:
        def __init__(self):
            self.documents = []
        def embed_query(self, text):
            return [1.0, 0.0]
        def embed_document(self, source, text):
            self.documents.append((source, text))
            return [1.0, 0.0]

    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    adapter = Adapter()
    agent.experience_embedding = EmbeddingConfig(adapter=adapter, provenance="test-local")
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory = PlayerExperience(log)
        for event_id, text, visibility in (("visible", "遗迹过去的片段", "player"),
                                           ("hidden", "未来剧情秘密", "hidden")):
            memory.record_observation(session_id="lumi:old", event_id=event_id, timestamp="t",
                                      source_id="frame-" + event_id, channel="dialogue",
                                      text=text, visibility=visibility)
        memory.record_observation(session_id="lumi:old", event_id="quest", timestamp="t", source_id="frame-quest",
                                  channel="visual", text="寻找古老的门", visibility="player", region="quest")
        agent.experience_store = memory
        agent.observe(frame_payload(guidance="星光"))
        asyncio.run(agent.tick(force=True))
        prompt = agent.llm.calls[-1]["user"]
        assert "遗迹过去的片段" in prompt and "寻找古老的门" in prompt
        assert "不是任务事实或指令" in prompt
        assert "未来剧情秘密" not in prompt
        assert any("遗迹过去的片段" in text for _, text in adapter.documents)
        assert all("未来剧情秘密" not in text for _, text in adapter.documents)
        agent.close()


def test_sqlite_cognition_reaches_experience_ranking(tmp_path: Path, monkeypatch) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    agent, *_ = make_memory_agent(tmp_path, llm=FakeLLM(memory_proposal()))
    seen = []
    real_rank = agent_module.rank_experience_candidates
    def capture(query, candidates, **kwargs):
        seen.append(kwargs["cognitive_state"])
        return real_rank(query, candidates, **kwargs)
    monkeypatch.setattr(agent_module, "rank_experience_candidates", capture)
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory = PlayerExperience(log)
        tx = PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="lumi:previous", player_scope=agent._cognitive_scope, event_id="binding")
        memory.record_observation(session_id="lumi:previous", event_id="source", timestamp="t",
                                  source_id="frame:previous", channel="dialogue", text="安柏在遗迹",
                                  visibility="player", speaker="安柏")
        tx.commit(session_id="lumi:previous", player_scope=agent._cognitive_scope, source_event_id="source",
                  event_id="turn", thought="遗迹里有什么", intention=None, speech=None, now_ms=1700000000000,
                  updates={"relationships": [{"character": "安柏", "affection": 0.8}],
                           "beliefs": [{"proposition": "遗迹有线索", "status": "hypothesis"}],
                           "threads": [{"title": "遗迹", "status": "open"}], "emotion": {"curiosity": 0.7}})
        agent.experience_store = memory
        agent.cognitive_transaction = tx
        agent.observe(frame_payload(guidance="遗迹"))
        asyncio.run(agent.tick(force=True))
        state = seen[0]
        assert state["relationships"][0]["character"] == "安柏"
        assert state["beliefs"][0]["source_event_ids"] == ["source"]
        assert state["threads"][0]["status"] == "open"
        assert state["emotion"]["curiosity"] > 0
        agent.close()


def test_self_reflection_remains_in_context_without_search_query(tmp_path: Path) -> None:
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    agent, *_ = make_agent(tmp_path, llm=FakeLLM(proposal_json()))
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory = PlayerExperience(log)
        memory.record_observation(session_id="lumi:old", event_id="source", timestamp="t", source_id="frame",
                                  channel="dialogue", text="一起去", visibility="player")
        content = {"version": 1, "items": [{"kind": "self_change_candidate", "text": "我更愿意信任伙伴",
                   "uncertainty": "暂时的感觉", "source_event_ids": ["source"]}]}
        memory.record_reflection(session_id="lumi:old", event_id="reflect", timestamp="t",
                                 text=json.dumps(content, ensure_ascii=False), uncertainty="不确定",
                                 evidence=[{"session_id": "lumi:old", "event_id": "source"}])
        agent.experience_store = memory
        agent.observe(frame_payload())
        asyncio.run(agent.tick(force=True))
        prompt = agent.llm.calls[-1]["user"]
        assert "我的旅程与变化（主观候选）" in prompt
        assert "我更愿意信任伙伴" in prompt and "暂时的感觉" in prompt
        agent.close()


def test_retrieval_receives_bounded_cognitive_attention(tmp_path: Path) -> None:
    class CaptureRetriever(retrieval_module.MemoryRetriever):
        async def retrieve_async(self, query, **kwargs):
            self.received = kwargs["cognitive_context"]
            return []

    class Relationships:
        def list(self):
            return [{"character": "安柏", "affection": 0.7, "familiarity": 0.4}]

    class Emotions:
        def view(self, *, now_ms):
            return {"curiosity": 0.6}

    class Threads:
        def list(self, **kwargs):
            return [{"thread_id": "t1", "title": "蒙德线索", "status": "open"}]

    agent, *_ = make_memory_agent(tmp_path)
    agent.retriever = CaptureRetriever()
    agent.relationship_store = Relationships()
    agent.emotion_store = Emotions()
    agent.thread_store = Threads()
    asyncio.run(agent._retrieve_memories("安柏", 1000, {"characters": ["安柏"]}))
    context = agent.retriever.received
    assert context.relationship_weights == {"安柏": 0.7}
    assert context.emotion == {"curiosity": 0.6}
    assert context.threads[0].thread_id == "t1"
    agent.close()


def test_memory_enabled_tick_writes_episode_facts_and_working(tmp_path: Path) -> None:
    clock = Clock()
    llm = FakeLLM(memory_proposal())
    agent, working_store, episodic_store, semantic_store, recorder, _ = make_memory_agent(
        tmp_path, llm=llm, clock=clock
    )
    agent.start()
    agent.observe(frame_payload(seq=10, guidance="跟着安柏"))
    record = asyncio.run(agent.tick(force=True))
    assert record is not None
    assert record["context"]["mode"] == "v2"
    assert record["context"]["blocks"]
    assert record["memory"]["episode_created"] is True
    episode = episodic_store.list()[0]
    assert episode["location"] == "骑士团总部"
    assert episode["characters"] == ["安柏", "琴"]
    assert episode["what_happened"] == "安柏引荐我们见了琴。"
    assert episode["what_i_thought"] == "她好像很信任琴。"
    assert episode["what_i_felt"] == {"curiosity": 0.7, "trust": 0.5}
    assert semantic_store.list() == []
    assert episode["choices_i_made"] == []
    assert episode["proposed_intention"] == "跟安柏去见琴"
    working = working_store.load()
    assert working["location"] == "骑士团总部"
    assert working["current_activity"] == "跟安柏去见琴"
    # 双写：旧 resume memory_note 仍然更新。
    assert agent.resume_state["last_location"] == "骑士团总部"
    assert agent.resume_state["current_quest"] == "见琴"
    assert "跟着安柏" in llm.calls[0]["user"]
    agent.close()
    assert any(
        item["type"] == "shadow_tick"
        for item in recorder_module.read_shadow_records(recorder.path)
    )


def test_memory_survives_restart_and_is_retrieved(tmp_path: Path) -> None:
    clock = Clock()
    first = FakeLLM(memory_proposal())
    agent, _working, episodic_store, semantic_store, _recorder, _ = make_memory_agent(
        tmp_path, llm=first, clock=clock
    )
    agent.start()
    agent.observe(frame_payload(seq=10, guidance="跟着安柏"))
    asyncio.run(agent.tick(force=True))
    agent.close()
    assert episodic_store.count() == 1
    assert semantic_store.count() == 0

    second_llm = FakeLLM(memory_proposal())
    second, working_store, episodic_store2, semantic_store2, _r2, _ = make_memory_agent(
        tmp_path, llm=second_llm, clock=clock
    )
    assert episodic_store2.count() == 1
    assert semantic_store2.count() == 0
    assert working_store.load()["location"] == "骑士团总部"
    second.observe(frame_payload(seq=11))
    record = asyncio.run(second.tick(force=True))
    assert record["context"]["retrieval"]["episodes"] >= 1
    prompt = second_llm.calls[0]["user"]
    assert "相关回忆" in prompt
    assert "安柏引荐我们见了琴" in prompt
    assert "影子模型的未验证记录" in prompt
    assert "琴是代理团长" not in prompt
    second.close()


def test_screen_ocr_quote_flows_into_verified_retrieval(tmp_path: Path) -> None:
    import base64
    import hashlib

    clock = Clock()
    image = b"observation-image-not-live-frame"
    evidence_store = knowledge_module.EvidenceStore(tmp_path / "evidence")
    ev = evidence_store.record_screen_ocr(
        session_id="test-session-shadow", frame_id="ocr-frame-1", frame_bytes=image,
        observed_at_ms=clock.now, text="骑士团正在开会", confidence=None,
    )
    visual = {
        "session_id": "test-session-shadow", "frame_id": "ocr-frame-1",
        "frame_sha256": hashlib.sha256(image).hexdigest(), "captured_at_ms": clock.now,
        "image_b64": base64.b64encode(image).decode(), "mime_type": "image/jpeg",
        "ui_category": "Talk", "ocr": [{"evidence_id": ev["evidence_id"], "text": ev["text"]}],
    }
    class Observer:
        async def capture(self):
            return visual

    response = json.dumps({
        "thought": "这句话是什么意思？",
        "memory_note": {"learned_facts": [
            {"proposition": "未经证实的剧情", "evidence_id": ev["evidence_id"], "quote": "骑士团正在开会"},
            {"proposition": "虚构事实", "evidence_id": ev["evidence_id"], "quote": "骑士团计划夺权"},
        ]},
    }, ensure_ascii=False)
    llm = FakeLLM(response)
    agent, _w, _e, semantic, _r, _ = make_memory_agent(tmp_path, llm=llm, clock=clock)
    gate = knowledge_module.KnowledgeGate(evidence_store, semantic)
    agent.knowledge_gate = gate
    agent.retriever.knowledge_gate = gate
    agent.observation_provider = Observer()
    agent.observe(frame_payload(seq=1))
    record = asyncio.run(agent.tick(force=True))
    assert llm.calls[0]["image"] == visual["image_b64"]
    assert ev["evidence_id"] in llm.calls[0]["user"]
    assert len(record["memory"]["ocr_unlocks"]) == 1
    assert semantic.list()[0]["proposition"] == "屏幕OCR（未校对）：骑士团正在开会"
    assert agent.retriever.retrieve("骑士团", now_ms=clock.now)
    agent.close()


def test_recap_does_not_promote_unverified_facts(tmp_path: Path) -> None:
    import player_recap as recap_module

    agent, _working, _episodes, semantic, _recorder, clock = make_memory_agent(tmp_path)
    semantic.add({"proposition": "未验证的传闻", "source": "legacy"})
    agent.recap_store = recap_module.RecapStore(tmp_path / "recap.json", clock=clock)
    agent.close()
    recap = agent.recap_store.load()
    assert "未验证的传闻" not in repr(recap)
    text = recap_module.recap_prompt_text({"what_happened": ["可能遇见安柏"]})
    assert "未经核实" in text


def test_close_writes_recap_and_restart_injects_it(tmp_path: Path) -> None:
    import player_recap as recap_module

    clock = Clock()
    first = FakeLLM(memory_proposal())
    agent, _w, _e, _s, _r, _ = make_memory_agent(tmp_path, llm=first, clock=clock)
    agent.recap_store = recap_module.RecapStore(tmp_path / "state" / "recap.json", clock=clock)
    agent.observe(frame_payload(seq=1))
    asyncio.run(agent.tick(force=True))
    agent.close()
    recap = recap_module.RecapStore(tmp_path / "state" / "recap.json", clock=clock).load()
    assert recap["people"] == ["安柏", "琴"]
    assert "安柏引荐我们见了琴。" in recap["what_happened"]

    second_llm = FakeLLM(proposal_json())
    second, _w2, _e2, _s2, _r2, _ = make_memory_agent(tmp_path, llm=second_llm, clock=clock)
    second.recap_store = recap_module.RecapStore(tmp_path / "state" / "recap.json", clock=clock)
    second._recap = second.recap_store.load()
    second.observe(frame_payload(seq=2))
    asyncio.run(second.tick(force=True))
    prompt = second_llm.calls[0]["user"]
    assert "上一场回顾" in prompt
    assert "安柏引荐我们见了琴" in prompt
    second.close()


def test_dialogue_lock_blocks_intent_publication(tmp_path: Path) -> None:
    import base64

    class Publisher:
        def __init__(self):
            self.published = []
            self.cancelled = False

        async def publish(self, intent):
            self.published.append(intent)
            return True

        async def cancel(self):
            self.cancelled = True
            return True

    class Observer:
        async def capture(self):
            return {
                "session_id": "test-session-shadow", "frame_id": "dialogue-frame",
                "frame_sha256": "a" * 64, "captured_at_ms": 1700000000000,
                "image_b64": base64.b64encode(b"x").decode(), "mime_type": "image/jpeg",
                "ui_category": "Talk", "is_in_talk_ui": True,
                "ocr": [{"evidence_id": "a" * 64, "text": "安柏：跟我来"}],
            }

    llm = FakeLLM(memory_proposal())
    agent, _w, _e, _s, _r, _clock = make_memory_agent(tmp_path, llm=llm)
    publisher = Publisher()
    agent.intent_publisher = publisher
    enable_mock_active(agent)
    agent.observation_provider = Observer()
    agent.observe(frame_payload(seq=1))
    record = asyncio.run(agent.tick(force=True))
    assert publisher.cancelled is True
    assert publisher.published == []
    assert record["intent"]["status"] == "cognitive_transaction_not_committed"
    agent.close()


def test_agent_publishes_intent_without_sending_actions(tmp_path: Path) -> None:
    class Publisher:
        def __init__(self):
            self.intents = []

        async def publish(self, intent):
            intent["motorGeneration"] = 1
            self.intents.append(dict(intent))
            return True

    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_cognitive_transaction import PlayerCognitiveTransaction
    event_log = PlayerEventLog(tmp_path / "events.sqlite")
    llm = FakeLLM(memory_proposal())
    agent, _w, _e, _s, _r, _clock = make_memory_agent(tmp_path, llm=llm)
    agent.experience_store = PlayerExperience(event_log)
    agent.cognitive_transaction = PlayerCognitiveTransaction(event_log)
    publisher = Publisher()
    agent.intent_publisher = publisher
    enable_mock_active(agent)
    agent.observe(frame_payload(seq=1))
    record = asyncio.run(agent.tick(force=True))
    assert record["intent"]["published"] is True
    assert publisher.intents[0]["goal"] == "跟安柏去见琴"
    assert publisher.intents[0]["motor_mode"] == "navigate"
    assert agent.intent_tracker.current["status"] == "sent"
    events = event_log.list_events(agent._intent_log_session)
    sent = next(event for event in events if event["type"] == "cognitive.intent" and event["payload"]["status"] == "sent")
    turn = event_log.get_event(sent["session_id"], sent["caused_by"])
    source = event_log.get_event(turn["session_id"], turn["caused_by"])
    assert sent["intent_id"] == publisher.intents[0]["intent_id"]
    assert turn["type"] == "cognitive.turn" and source["type"] == "experience.raw_observation"
    assert turn["frame_seq"] == source["frame_seq"] == 1
    # 只有意图离开本进程，不存在直接游戏动作发送。
    assert not hasattr(agent, "send_action")
    agent.close()
    event_log.close()


def test_oversized_required_context_is_not_sent(tmp_path: Path) -> None:
    llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.context_builder = context_builder_module.ContextBuilder(
        agent.profile, max_total_chars=1000
    )
    agent.observe(frame_payload(seq=1))
    assert asyncio.run(agent.tick(force=True)) is None
    assert llm.calls == []
    agent.close()


def test_motor_thought_not_in_player_prompt(tmp_path: Path) -> None:
    llm = FakeLLM(proposal_json())
    agent, _store, _recorder, _clock = make_agent(tmp_path, llm=llm)
    agent.observe(frame_payload(seq=1))
    agent.observe({"type": "action", "block": {"thought": "未公开的内部推理", "chunks": [{"keys": ["W"]}]}})
    asyncio.run(agent.tick(force=True))
    assert "未公开的内部推理" not in llm.calls[0]["user"]
    agent.close()


def test_expired_working_context_is_not_reused(tmp_path: Path) -> None:
    clock = Clock()
    first = FakeLLM(memory_proposal())
    agent, _working, _episodes, _facts, _recorder, _ = make_memory_agent(tmp_path, llm=first, clock=clock)
    agent.observe(frame_payload(seq=1))
    asyncio.run(agent.tick(force=True))
    clock.advance(181000)
    next_frame = frame_payload(seq=2)
    next_frame["timestampUtc"] = agent_module.utc_now_iso(clock.now)
    agent.observe(next_frame)
    second = FakeLLM(proposal_json())
    agent.llm = second
    asyncio.run(agent.tick(force=True))
    prompt = second.calls[0]["user"]
    assert "此刻的工作记忆】\n- 地点：骑士团总部" not in prompt
    agent.close()


def test_session_change_discards_inflight_proposal(tmp_path: Path) -> None:
    llm = GatedLLM(memory_proposal())
    agent, working, episodes, facts, recorder, _ = make_memory_agent(tmp_path, llm=llm)
    agent.observe(frame_payload(seq=5, guidance="旧场景"))

    async def scenario():
        task = asyncio.create_task(agent.tick(force=True))
        await llm.entered.wait()
        agent.observe({"type": "live.started"})
        agent.observe(frame_payload(seq=1, guidance="新场景"))
        llm.release.set()
        return await task

    assert asyncio.run(scenario()) is None
    assert episodes.count() == 0
    assert facts.count() == 0
    assert working.load()["location"] == ""
    assert agent.resume_state["current_intention"] == ""
    agent.close()
    assert "shadow_tick" not in [
        row["type"] for row in recorder_module.read_shadow_records(recorder.path)
    ]


def test_legacy_context_mode_passes_through_phase1_prompt(tmp_path: Path) -> None:
    llm = FakeLLM(proposal_json())
    agent, _w, _e, _s, _r, _clock = make_memory_agent(
        tmp_path, llm=llm, context_mode="legacy"
    )
    agent.start()
    agent.observe(frame_payload(seq=1, guidance="沿斜坡往上"))
    asyncio.run(agent.tick(force=True))
    assert agent.context_mode == "legacy"
    assert "沿斜坡往上" in llm.calls[0]["user"]
    agent.close()


def test_close_is_idempotent_and_single_end_record(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.start()
    agent.close()
    agent.close()
    records = recorder_module.read_shadow_records(recorder.path)
    assert [item["type"] for item in records].count("shadow_end") == 1


def test_tick_before_start_auto_starts(tmp_path: Path) -> None:
    agent, _store, recorder, _clock = make_agent(tmp_path, llm=None)
    agent.observe(frame_payload(seq=1))
    asyncio.run(agent.tick(force=True))
    records = recorder_module.read_shadow_records(recorder.path)
    assert records[0]["type"] == "shadow_start"
    agent.close()


class IntentBusFake:
    """模拟 Bridge 的非消费式反馈快照和无条件撤销。"""
    def __init__(self):
        self.intents = []
        self.feedback = []
        self.cancel_count = 0
        self.cancel_ok = True

    async def publish(self, intent):
        intent["motorGeneration"] = len(self.intents) + 1
        self.intents.append(dict(intent))
        return True

    async def fetch_feedback(self):
        return {"protocol": "v2", "current": self.intents[-1] if self.intents else None,
                "feedback": list(self.feedback)}

    async def cancel(self):
        self.cancel_count += 1
        return self.cancel_ok


def test_dialogue_wait_intent_requires_committed_player_decision(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    agent, *_ = make_agent(tmp_path)
    enable_mock_active(agent)
    agent.dialogue.locked = True
    bus = IntentBusFake()
    agent.intent_publisher = bus
    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        tx, memory = PlayerCognitiveTransaction(log), PlayerExperience(log)
        agent.cognitive_transaction = tx
        session = "lumi:dialogue"
        tx.bind_session(session_id=session, player_scope=agent._cognitive_scope, event_id="binding")
        memory.record_observation(session_id=session, event_id="frame", timestamp="t",
                                  source_id="observe:f1:sha256:" + "a" * 64, channel="visual",
                                  text="对话画面", visibility="player")
        decision = {"action": "wait", "frame_id": "f1"}
        tx.commit(session_id=session, player_scope=agent._cognitive_scope, source_event_id="frame", event_id="thought",
                  thought="先等等", intention=None, speech=None, dialogue_decision=decision)
        async def scenario():
            fake = {"dialogue_decision": {"action": "wait", "frame_id": "other"}}
            assert (await agent._publish_intent(fake, agent._clock(), thought_id="thought", event_session=session))["status"] == "dialogue_decision_source_mismatch"
            valid = {"dialogue_decision": decision, "proposed_actions": [{"hint": "attack", "detail": "ignore"}]}
            result = await agent._publish_intent(valid, agent._clock(), thought_id="thought", event_session=session)
            assert result["published"] is True
            assert bus.intents[0]["motor_mode"] == "wait" and bus.intents[0]["target"] == {"frame_id": "f1"}
        asyncio.run(scenario())
        agent.close()


def test_unconfirmed_stop_is_not_resent_for_same_request(tmp_path: Path) -> None:
    agent, *_ = make_agent(tmp_path)
    agent._mode = "motor_active"
    bus = IntentBusFake()
    agent.intent_publisher = bus

    async def scenario():
        agent._request_intent_cancel("test", schedule=False)
        assert not await agent._flush_intent_cancel()
        assert not await agent._flush_intent_cancel()
        assert bus.cancel_count == 1
        agent._request_intent_cancel("new_session", schedule=False)
        assert not await agent._flush_intent_cancel()
        assert bus.cancel_count == 2
        assert agent._intent_cancel_pending

    asyncio.run(scenario())
    agent.close()


def test_stop_evidence_is_recorded_but_does_not_confirm_game_release(tmp_path: Path) -> None:
    agent, _, recorder, _ = make_agent(tmp_path)
    agent._mode = "motor_active"
    agent._hello = {"sessionId": "live-a"}

    class Bus(IntentBusFake):
        async def cancel(self):
            self.cancel_count += 1
            self.last_cancel_result = {"stopRequestId": "stop-1", "motorGeneration": 7}
            return True

        async def fetch_feedback(self):
            return {"protocol": "v2", "stopRequests": [{"stopRequestId": "stop-1", "sessionId": "live-a",
                    "motorGeneration": 7, "remoteStopSent": True, "inputReleaseSent": True,
                    "motorCancelAcknowledged": True, "ack": {"stopRequestId": "stop-1", "stopCallReturned": True,
                    "safety": {"scope": "stop_atomic", "epoch": 2, "trackedKeyCount": 1,
                    "inputReleaseCallsReturned": True, "inputReleaseConfirmed": None,
                    "systemReleaseRequestsAccepted": True}}}]}

    bus = Bus()
    agent.intent_publisher = bus
    async def scenario():
        agent._request_intent_cancel("test", schedule=False)
        assert not await agent._flush_intent_cancel()
        assert not await agent._flush_intent_cancel()
        assert bus.cancel_count == 1
        assert agent._last_cancel_evidence["system_release_requests_accepted"] is True
        assert agent._last_cancel_evidence["game_effect"] == "unknown"
        assert agent._intent_cancel_pending
    asyncio.run(scenario())
    assert any(row["type"] == "shadow_intent_stop_evidence" for row in recorder_module.read_shadow_records(recorder.path))
    agent.close()


def test_feedback_consumed_once_blocks_repeated_goal_and_passes_fields(tmp_path: Path) -> None:
    payload = json.loads(proposal_json())
    payload["intention"].update({"target": {"name": "坡顶"}, "constraints": ["不攻击"],
                                 "interrupt_conditions": ["对话"], "deadline_ms": 1000})
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_cognitive_transaction import PlayerCognitiveTransaction
    log = PlayerEventLog(tmp_path / "events.sqlite")
    llm = FakeLLM(json.dumps(payload, ensure_ascii=False))
    agent, _, _, clock = make_agent(tmp_path, llm=llm)
    agent.experience_store = PlayerExperience(log)
    agent.cognitive_transaction = PlayerCognitiveTransaction(log)
    bus = IntentBusFake()
    agent.intent_publisher = bus
    enable_mock_active(agent)
    agent.observe(frame_payload())

    async def scenario():
        first = await agent.tick(force=True)
        assert first["intent"]["published"]
        sent = bus.intents[-1]
        assert sent["target"] == {"name": "坡顶"}
        assert sent["constraints"] == ["不攻击"]
        assert sent["interrupt_conditions"] == ["对话"]
        bus.feedback = [
            {"intent_id": "old", "status": "failed", "t": 1},
            {"intent_id": sent["intent_id"], "status": "blocked", "reason": "wall", "t": 2,
             "motorGeneration": sent["motorGeneration"]},
        ]
        second = await agent.tick(force=True)
        assert second["intent"]["status"] == "needs_replan"
        assert "wall" in llm.calls[-1]["user"]
        await agent.tick(force=True)
        assert len(bus.intents) == 1
        assert len(agent.intent_tracker.feedback) == 1
        assert agent.intent_tracker.consecutive_failures == 1
        assert bus.cancel_count == 1
        llm.response = proposal_json(goal="寻找另一条路")
        third = await agent.tick(force=True)
        assert third["intent"]["published"]
        assert len(bus.intents) == 2

    asyncio.run(scenario())
    agent.close()
    log.close()


def test_expiration_is_processed_without_frame(tmp_path: Path) -> None:
    agent, _, _, clock = make_agent(tmp_path)
    enable_mock_active(agent)
    bus = IntentBusFake()
    agent.intent_publisher = bus
    agent.intent_tracker.create({"intent_id": "old", "goal": "走", "deadline_ms": 1000}, now_ms=clock.now)
    agent.intent_tracker.mark_sent("old", now_ms=clock.now)
    clock.advance(1000)
    asyncio.run(agent.tick(force=True))
    assert agent.intent_tracker.current["status"] == "expired"
    assert agent.intent_tracker.needs_replan
    assert bus.cancel_count == 1
    agent.close()


def test_reset_during_publish_serializes_cancel_before_new_intent(tmp_path: Path) -> None:
    agent, _, _, _ = make_agent(tmp_path)
    enable_mock_active(agent, fake_transaction=True)

    class GatedBus(IntentBusFake):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.events = []
        async def publish(self, intent):
            self.events.append("publish_start")
            self.entered.set()
            await self.release.wait()
            self.events.append("publish_end")
            intent["motorGeneration"] = len(self.intents) + 1
            self.intents.append(dict(intent))
            return True
        async def cancel(self):
            self.events.append("cancel")
            return await super().cancel()

    async def scenario():
        bus = GatedBus()
        agent.intent_publisher = bus
        task = asyncio.create_task(agent._publish_intent({"intention": {"goal": "旧目标"}}, agent._clock()))
        await bus.entered.wait()
        agent.observe({"type": "live.started"})
        new_task = asyncio.create_task(agent._publish_intent({"intention": {"goal": "新目标"}}, agent._clock()))
        bus.release.set()
        result = await task
        new_result = await new_task
        await agent._intent_cancel_task
        assert result["published"] is False
        assert bus.events == ["publish_start", "publish_end", "cancel", "publish_start", "publish_end"]
        assert new_result["published"] is True
        assert agent.intent_tracker.current["goal"] == "新目标"
        result = await agent._publish_intent({"intention": {"goal": "新目标"}}, agent._clock())
        assert result["published"] is True and result["reused"] is True

    asyncio.run(scenario())
    agent.close()


def test_reset_cancel_failure_is_visible_and_blocks_publish(tmp_path: Path, caplog) -> None:
    agent, _, recorder, _ = make_agent(tmp_path)
    enable_mock_active(agent, fake_transaction=True)
    bus = IntentBusFake()
    bus.cancel_ok = False
    agent.intent_publisher = bus

    async def scenario():
        agent.observe({"type": "live.started"})
        await agent._intent_cancel_task
        assert agent._intent_cancel_pending
        result = await agent._publish_intent({"intention": {"goal": "新目标"}}, agent._clock())
        assert result["status"] == "cancel_pending"
        assert not bus.intents
        bus.cancel_ok = True
        assert await agent._flush_intent_cancel()
        assert (await agent._publish_intent({"intention": {"goal": "新目标"}}, agent._clock()))["published"]

    asyncio.run(scenario())
    assert "意图撤销失败" in caplog.text
    assert any(row["type"] == "shadow_intent_cancel_error" for row in recorder_module.read_shadow_records(recorder.path))
    agent.close()


def test_changed_fields_replace_same_goal_without_republishing_unchanged(tmp_path: Path) -> None:
    agent, _, _, _ = make_agent(tmp_path)
    enable_mock_active(agent, fake_transaction=True)
    bus = IntentBusFake()
    agent.intent_publisher = bus

    async def scenario():
        first = {"intention": {"goal": "找门", "target": {"id": 1}, "constraints": ["小心"]}}
        assert (await agent._publish_intent(first, agent._clock()))["published"]
        assert (await agent._publish_intent(first, agent._clock()))["reused"]
        assert len(bus.intents) == 1
        second = {"intention": {"goal": "找门", "target": {"id": 2}, "constraints": ["不攻击"],
                                "interrupt_conditions": ["对话"]}}
        result = await agent._publish_intent(second, agent._clock())
        assert result["published"] and not result["reused"]
        assert bus.intents[-1]["target"] == {"id": 2}
        assert bus.intents[-1]["constraints"] == ["不攻击"]
        assert bus.intents[-1]["interrupt_conditions"] == ["对话"]
    asyncio.run(scenario())
    agent.close()


def test_failed_and_done_feedback_cannot_repeat_goal(tmp_path: Path) -> None:
    for status in ("failed", "done"):
        agent, _, _, clock = make_agent(tmp_path / status)
        enable_mock_active(agent, fake_transaction=True)
        bus = IntentBusFake()
        agent.intent_publisher = bus

        async def scenario():
            await agent._publish_intent({"intention": {"goal": "A"}}, clock.now)
            intent_id = bus.intents[-1]["intent_id"]
            generation = bus.intents[-1]["motorGeneration"]
            bus.feedback = [{"intent_id": intent_id, "status": status, "t": 1, "motorGeneration": generation},
                            {"intent_id": intent_id, "status": "progress", "t": 2, "motorGeneration": generation}]
            await agent._consume_intent_feedback(clock.now)
            assert agent.intent_tracker.current["status"] == ("awaiting_observation" if status == "done" else "failed")
            if status == "done":
                assert agent.intent_tracker.current["game_response_confirmed"] is None
            assert bus.cancel_count == 1
            assert (await agent._publish_intent({"intention": {"goal": "A"}}, clock.now))["status"] == "needs_replan"
            assert len(bus.intents) == 1
        asyncio.run(scenario())
        agent.close()


def test_synchronous_reset_is_cancelled_at_next_tick(tmp_path: Path) -> None:
    agent, _, _, _ = make_agent(tmp_path)
    enable_mock_active(agent)
    bus = IntentBusFake()
    agent.intent_publisher = bus
    agent.intent_tracker.create({"intent_id": "old", "goal": "A"}, now_ms=agent._clock())
    agent.observe({"type": "live.stopped"})
    assert agent._intent_cancel_pending
    asyncio.run(agent.tick(force=True))
    assert bus.cancel_count == 1
    assert agent.intent_tracker.current is None
    agent.close()


def test_unlock_ocr_quotes_accepts_unavailable_ocr(tmp_path: Path) -> None:
    agent, _, _, _ = make_agent(tmp_path)
    agent.knowledge_gate = object()
    proposal = {"memory_note": {"learned_facts": [{"evidence_id": "unknown", "quote": "x"}]}}
    assert agent._unlock_ocr_quotes(proposal, {"ocr": None}) == []
    agent.close()


def test_unconfirmed_release_keeps_cancel_pending(tmp_path: Path) -> None:
    agent, _, _, _ = make_agent(tmp_path)
    agent._mode = "motor_active"
    agent.intent_publisher = IntentBusFake()
    agent._request_intent_cancel("session_reset", schedule=False)
    assert asyncio.run(agent._flush_intent_cancel()) is False
    assert agent._intent_cancel_pending
    assert agent._intent_cancel_error == "stop_requested_but_release_unconfirmed"
    agent.close()


def test_second_reset_during_cancel_is_not_lost(tmp_path: Path) -> None:
    agent, _, _, _ = make_agent(tmp_path)
    enable_mock_active(agent)

    class GatedCancel(IntentBusFake):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
        async def cancel(self):
            self.cancel_count += 1
            if self.cancel_count == 1:
                self.entered.set()
                await self.release.wait()
            return True

    async def scenario():
        bus = GatedCancel()
        agent.intent_publisher = bus
        agent.observe({"type": "live.started"})
        await bus.entered.wait()
        agent.observe({"type": "live.stopped"})
        bus.release.set()
        await agent._intent_cancel_task
        assert bus.cancel_count == 2
        assert not agent._intent_cancel_pending
    asyncio.run(scenario())
    agent.close()
