#!/usr/bin/env python3
"""Synthetic player → authenticated file IPC → session timeline integration."""
from __future__ import annotations

import asyncio
import multiprocessing
import queue
import sys
import time
from pathlib import Path

PLAYER = Path(__file__).resolve().parents[1]
SESSION = PLAYER.parent / "session"
sys.path.insert(0, str(PLAYER))
sys.path.insert(0, str(SESSION))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import record_session  # noqa: E402
import session_format  # noqa: E402
from player_event_log import PlayerEventLog  # noqa: E402
from player_cognitive_transaction import PlayerCognitiveTransaction  # noqa: E402
from player_experience import PlayerExperience  # noqa: E402
from player_speech_output import PlayerSpeechOutput  # noqa: E402
from test_player_agent import FakeLLM, frame_payload, make_agent, proposal_json  # noqa: E402


def _speech_producer_process(endpoint: str, secret: str, result_queue) -> None:
    async def submit() -> dict:
        output = PlayerSpeechOutput(
            endpoint, secret, read_endpoint=record_session.read_player_speech_endpoint,
            send_speech=record_session.send_player_speech,
            announce_producer=record_session.announce_player_speech_producer,
            read_heartbeat=record_session.read_player_speech_heartbeat,
        )
        await output.announce_ready()
        binding = await output.prepare()
        return await output.deliver(binding, {
            "type": "speech", "source": "persistent_player", "text": "这是同一条发言。",
            "generated_at_ms": int(time.time() * 1000), "thought_id": "thought-child",
        })

    try:
        result_queue.put(("ok", asyncio.run(submit())))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))


def test_exclusive_speech_producer_handshake_and_delivery(tmp_path: Path) -> None:
    args = record_session.build_parser().parse_args(["--out", str(tmp_path), "--capture", "none"])
    args.token = "test"
    args.speech_mode = "player-exclusive"
    args.exclusive_producer_required = True
    args.formal_chain_id = "a" * 32
    args.player_speech_endpoint = str(tmp_path / "speech-endpoint.json")
    args.player_speech_secret = "temporary-secret-" + "x" * 40
    process = None
    args.formal_chain_guard = lambda pid: process is not None and pid == process.pid and process.is_alive()
    recorder = record_session.SessionRecorder(args)
    recorder.t0_ms = int(time.time() * 1000) - 1000
    recorder.session_dir = tmp_path / "session"
    recorder.writer = session_format.SessionWriter(
        recorder.session_dir, session_id="recording", t0_unix_ms=recorder.t0_ms
    )
    recorder._handle({"type": "hello", "sessionId": "live-session"})
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    process = ctx.Process(target=_speech_producer_process,
                          args=(args.player_speech_endpoint, args.player_speech_secret, results))
    try:
        process.start()
        recorder.bind_expected_speech_producer(process.pid)
        deadline = time.monotonic() + 15
        outcome = None
        while time.monotonic() < deadline:
            recorder._drain_player_speech()
            try:
                outcome = results.get_nowait()
                break
            except queue.Empty:
                if not process.is_alive():
                    break
                time.sleep(0.01)
        process.join(timeout=2)
        assert not process.is_alive(), "speech producer did not finish"
        assert outcome is not None, f"speech producer exited without a result: {process.exitcode}"
        assert outcome[0] == "ok", outcome[1]
        assert recorder.speech_producer_ready.is_set()
        assert recorder.speech_producer_pid == process.pid
        recorder._finish()
        speech = session_format.read_session(recorder.session_dir).speeches[0]
        assert speech == outcome[1]
        assert speech["source"] == "persistent_player"
        assert speech["text"] == "这是同一条发言。"
        assert speech["thought_id"] == "thought-child"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        results.close()
        recorder._close_speech_endpoint()


def test_player_speech_reaches_real_session_writer(tmp_path: Path, monkeypatch) -> None:
    """Exercise cognition and IPC with synthetic active authorization, not the production gate."""
    agent, *_ = make_agent(tmp_path / "player", llm=FakeLLM(proposal_json(speech="我们去看看。")))
    agent._mode = "full_active"
    monkeypatch.setattr(agent, "_active_output_allowed", lambda **kwargs: True)
    args = record_session.build_parser().parse_args(["--out", str(tmp_path), "--capture", "none"])
    args.token = "test"
    recorder = record_session.SessionRecorder(args)
    recorder.t0_ms = 1699999999000
    recorder.session_dir = tmp_path / "session"
    recorder.writer = session_format.SessionWriter(
        recorder.session_dir, session_id="recording", t0_unix_ms=recorder.t0_ms
    )
    recorder._speech_endpoint = tmp_path / "speech-endpoint.json"
    recorder._speech_secret = "temporary-secret-" + "x" * 40
    recorder._handle({"type": "hello", "sessionId": "live-session"})
    log = PlayerEventLog(tmp_path / "events.sqlite")
    agent.experience_store = PlayerExperience(log)
    agent.cognitive_transaction = PlayerCognitiveTransaction(log)
    agent.speech_output = PlayerSpeechOutput(
        str(recorder._speech_endpoint), recorder._speech_secret,
        read_endpoint=record_session.read_player_speech_endpoint,
        send_speech=record_session.send_player_speech,
    )
    agent.observe(frame_payload())

    async def scenario():
        task = asyncio.create_task(agent.tick(force=True))
        while not task.done():
            recorder._drain_player_speech()
            await asyncio.sleep(0.01)
        return await task

    try:
        result = asyncio.run(scenario())
        assert result["speech_policy"]["delivery"] == "timeline_written"
        assert agent.speech_policy.last_speech_delivered is not None
        recorder._finish()
        speech = session_format.read_session(recorder.session_dir).speeches[0]
        assert speech["source"] == "persistent_player"
        assert speech["text"] == "我们去看看。"
        assert speech["thought_id"] == result["thought_id"]
        thought = log.recent_events("lumi:", "cognitive.turn")[0]
        assert thought["event_id"] == speech["thought_id"]
        observed = log.get_event(thought["session_id"], thought["caused_by"])
        assert observed["type"] == "experience.raw_observation"
        assert observed["payload"]["channel"] == "visual"
        assert speech["t"] == 1000
    finally:
        agent.close()
        log.close()
        recorder._close_speech_endpoint()
