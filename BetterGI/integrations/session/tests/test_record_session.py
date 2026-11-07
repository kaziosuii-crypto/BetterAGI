#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录制器端到端测试：假实时服务 → 视频 + 会话文件。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import record_session  # noqa: E402
import run_episode  # noqa: E402
import session_format  # noqa: E402
from websockets.sync.server import serve  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="需要 ffmpeg")


def make_jpeg(path: Path, color: str = "blue") -> bytes:
    subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=64x36:d=0.1",
            "-frames:v",
            "1",
            "-q:v",
            "5",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def iso_now() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def probe_duration(path: Path) -> float:
    output = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(output)


class FakeLiveServer:
    """按真实协议推送帧、解说、指导与动作状态，然后停止会话。"""

    def __init__(self, jpeg: bytes) -> None:
        self.jpeg = base64.b64encode(jpeg).decode("ascii")
        self.server = serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeLiveServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()

    def _handler(self, websocket) -> None:
        websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "sessionId": "fake-live",
                    "fps": 10,
                    "width": 64,
                    "height": 36,
                    "chunksPerAction": 6,
                    "actionIntervalMs": 33,
                }
            )
        )
        for seq in range(1, 6):
            websocket.send(
                json.dumps(
                    {
                        "type": "frame",
                        "seq": seq,
                        "timestampUtc": iso_now(),
                        "jpeg": self.jpeg,
                        "width": 64,
                        "height": 36,
                    }
                )
            )
            time.sleep(0.05)
        websocket.send(
            json.dumps(
                {
                    "type": "commentary",
                    "kind": "thought",
                    "text": "先观察一下周围",
                    "emotion": "thinking",
                    "timestampUtc": iso_now(),
                    "meta": {"internal": True},
                }
            )
        )
        websocket.send(
            json.dumps(
                {
                    "type": "commentary",
                    "kind": "reaction",
                    "text": "前面好像有个宝箱！",
                    "emotion": "excited",
                    "timestampUtc": iso_now(),
                }
            )
        )
        websocket.send(
            json.dumps(
                {
                    "type": "guidance",
                    "text": "往左前方走",
                    "source": "director",
                    "timestampUtc": iso_now(),
                }
            )
        )
        websocket.send(
            json.dumps({"type": "action_state", "keys": ["W"], "queueDepth": 0})
        )
        websocket.send(json.dumps({"type": "action_state", "keys": ["W", "Shift"]}))
        time.sleep(0.25)
        websocket.send(json.dumps({"type": "live.stopped"}))


def build_args(url: str, out: Path, name: str = "fake-session"):
    parser = record_session.build_parser()
    args = parser.parse_args(
        [
            "--url",
            url,
            "--out",
            str(out),
            "--capture",
            "frames",
            "--fps",
            "10",
            "--name",
            name,
            "--reconnect-ms",
            "200",
        ]
    )
    args.token = "test-token"
    return args


def player_recorder(tmp_path: Path, monkeypatch, *, t0: int = 1000, exclusive: bool = False):
    """不启动服务或采集设备，构造已有写入器的录制会话。"""
    args = build_args("ws://127.0.0.1:1/live", tmp_path, "player")
    args.capture = "none"
    if exclusive:
        args.speech_mode = "player-exclusive"
        args.exclusive_producer_required = True
        args.formal_chain_id = "a" * 32
        args.formal_chain_guard = lambda _pid: True
        args.player_speech_endpoint = str(tmp_path / "endpoint.json")
        args.player_speech_secret = "secret-" + "a" * 40
    recorder = record_session.SessionRecorder(args)
    recorder.t0_ms = t0
    recorder.session_dir = tmp_path / "player"
    recorder.writer = session_format.SessionWriter(
        recorder.session_dir, session_id="current", t0_unix_ms=t0
    )
    monkeypatch.setattr(record_session.time, "time", lambda: 10.0)
    return recorder


def test_action_attribution_and_attempt_are_recorded(tmp_path: Path, monkeypatch) -> None:
    recorder = player_recorder(tmp_path, monkeypatch)
    recorder._handle({"type": "action", "seq": 7, "intentId": "intent-1", "motorGeneration": 2,
                      "attemptId": "attempt-1", "block": {"seq": 7, "chunks": [{"keys": ["W"]}]}})
    recorder._handle({"type": "action_attempt", "executionAttempt": {"seq": 7, "intentId": "intent-1",
                      "motorGeneration": 2, "attemptId": "attempt-1", "status": "unknown"}})
    recorder._handle({"type": "input_evidence", "seq": 7, "intentId": "intent-1", "motorGeneration": 2,
                      "attemptId": "attempt-1", "inputEvidence": {"status": "accepted", "operation": "KeyDown"}})
    recorder._finish()
    events = session_format.read_session(recorder.session_dir).of_type("action")
    assert events[0]["intentId"] == "intent-1"
    assert events[0]["status"] == "accepted"
    assert events[1]["executionAttempt"]["status"] == "unknown"
    assert events[1]["executionAttempt"]["attemptId"] == "attempt-1"
    assert events[2]["inputEvidence"]["status"] == "accepted"
    assert events[2]["intentId"] == "intent-1"


def test_player_speech_roundtrip_without_network(tmp_path: Path, monkeypatch) -> None:
    recorder = player_recorder(tmp_path, monkeypatch)
    text = "  原样\n" + "字" * 2100 + "！  "
    event = {
        "type": "speech", "source": "persistent_player", "text": text,
        "generated_at_ms": 1456, "frame_seq": 0, "thought_id": "t-1",
        "intent_id": "i-1", "emotion": "worried", "intensity": 0.7,
        "pace": "slow", "volume": "soft", "pause_before_ms": 300,
        "interruptible": False, "thought_source": "dialogue",
    }
    written = recorder.record_player_speech(event, expected_session_id="current")
    assert written["t"] == 456
    assert recorder.speeches == 1
    assert recorder.last_event_t == 456 + written["dur"]
    recorder._finish()
    session = session_format.read_session(recorder.session_dir)
    session.validate()
    stored = session.speeches[0]
    assert stored == written
    for key in ("source", "text", "generated_at_ms", "frame_seq", "thought_id",
                "intent_id", "emotion", "intensity", "pace", "volume",
                "pause_before_ms", "interruptible", "thought_source"):
        assert stored[key] == event[key]
    assert stored["frame"] == 0  # 旧 renderer 仍使用 frame
    assert stored["avatar"]["mouth_open"]["values"]


@pytest.mark.parametrize("changes", [
    {"type": "thought"}, {"source": "director"}, {"source": None},
    {"generated_at_ms": None}, {"generated_at_ms": True},
    {"generated_at_ms": 999}, {"generated_at_ms": 10001},
    {"generated_at_ms": 1000.5}, {"text": "  "}, {"text": 12},
    {"frame_seq": True}, {"frame_seq": -1}, {"thought_id": ""},
    {"intent_id": 1}, {"intensity": float("nan")},
    {"pace": "instant"}, {"volume": "shout"},
    {"pause_before_ms": -1}, {"interruptible": "false"},
    {"prosody": "slow"}, {"prosody": {"pace": "instant"}},
    {"pace": "fast", "prosody": {"pace": "slow"}},
])
def test_player_speech_rejects_invalid_events(tmp_path: Path, monkeypatch, changes) -> None:
    recorder = player_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "你好",
             "generated_at_ms": 1000}
    event.update(changes)
    with pytest.raises(ValueError):
        recorder.record_player_speech(event, expected_session_id="current")
    assert recorder.speeches == 0
    assert recorder.writer.event_count == 0
    recorder._finish()
    assert not session_format.read_session(recorder.session_dir).speeches


def test_recorder_websocket_uses_header_not_url(tmp_path: Path, monkeypatch) -> None:
    args = build_args("ws://127.0.0.1:1/live?access_token=old&x=1", tmp_path)
    recorder = record_session.SessionRecorder(args)
    seen = {}
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def connect(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Socket()
    monkeypatch.setattr(record_session, "ws_connect", connect)
    monkeypatch.setattr(recorder, "_session_loop", lambda socket: None)
    recorder._connect_loop()
    assert seen["url"] == "ws://127.0.0.1:1/live?x=1"
    assert seen["additional_headers"] == {"Authorization": "Bearer test-token"}
    assert "test-token" not in recorder.url


def test_strict_live_start_is_required_and_fails_closed(tmp_path: Path, monkeypatch) -> None:
    args = build_args("ws://127.0.0.1:1/live", tmp_path)
    args.strict_safety = True
    recorder = record_session.SessionRecorder(args)
    requests = []
    monkeypatch.setattr(recorder, "_http", lambda method, path, payload=None: requests.append((method, path, payload)))
    recorder.start_live()
    assert requests[0][2]["requireStrictSafety"] is True
    assert requests[0][2]["requireControlLease"] is True
    def fail(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(recorder, "_http", fail)
    with pytest.raises(RuntimeError, match="严格安全模式启动未确认"):
        recorder.start_live()


def test_player_speech_does_not_start_session(tmp_path: Path) -> None:
    args = build_args("ws://127.0.0.1:1/live", tmp_path)
    recorder = record_session.SessionRecorder(args)
    event = {"type": "speech", "source": "persistent_player", "text": "x",
             "generated_at_ms": 1000}
    with pytest.raises(RuntimeError, match="未开始"):
        recorder.record_player_speech(event, expected_session_id="old")
    assert recorder.writer is None
    assert recorder.session_dir is None
    assert not list(tmp_path.iterdir())


def test_player_speech_requires_current_open_session(tmp_path: Path, monkeypatch) -> None:
    recorder = player_recorder(tmp_path, monkeypatch, t0=0)
    event = {"type": "speech", "source": "persistent_player", "text": "x",
             "generated_at_ms": 123}
    with pytest.raises(ValueError, match="当前录制会话"):
        recorder.record_player_speech(event, expected_session_id="previous")
    assert recorder.record_player_speech(event, expected_session_id="current")["t"] == 123
    recorder._finish()
    with pytest.raises(RuntimeError):
        recorder.record_player_speech(event, expected_session_id="current")
    assert recorder.speeches == 1


def test_player_speech_write_failure_is_not_success(tmp_path: Path, monkeypatch) -> None:
    recorder = player_recorder(tmp_path, monkeypatch)
    def fail_write(payload):
        raise OSError("disk error")
    monkeypatch.setattr(recorder.writer, "_write", fail_write)
    with pytest.raises(OSError, match="disk error"):
        recorder.record_player_speech({"type": "speech", "source": "persistent_player",
                                      "text": "你好", "generated_at_ms": 1000},
                                     expected_session_id="current")
    assert recorder.speeches == 0
    assert recorder.last_event_t == 0
    monkeypatch.undo()
    recorder._finish()


def ipc_recorder(tmp_path, monkeypatch):
    """使用真实临时文件模拟生产者和单写者录制线程。"""
    recorder = player_recorder(tmp_path, monkeypatch)
    recorder._speech_endpoint = tmp_path / "endpoint.json"
    recorder._speech_secret = "secret-" + "a" * 40
    recorder._handle({"type": "hello", "sessionId": "live-session"})
    descriptor = record_session.read_player_speech_endpoint(recorder._speech_endpoint, recorder._speech_secret)
    return recorder, descriptor


def ipc_send(recorder, descriptor, event, *, timeout=2):
    """异步投递并让调用线程执行录制器的 drain。"""
    outcome = {}
    def sender():
        try:
            outcome["event"] = record_session.send_player_speech(
                recorder._speech_endpoint, recorder._speech_secret, event,
                expected_session_id=descriptor["session_id"],
                expected_instance=descriptor["instance"], timeout=timeout)
        except Exception as exc:
            outcome["error"] = exc
    thread = threading.Thread(target=sender)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not list((recorder._speech_dir / "requests").glob("*.request")):
        time.sleep(0.005)
    recorder._drain_player_speech()
    thread.join(timeout=3)
    assert not thread.is_alive()
    return outcome


def test_player_speech_file_ipc_roundtrip(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "  原样\n文本  ",
             "generated_at_ms": 1500, "frame_seq": 8, "thought_id": "thought-1",
             "intent_id": "intent-1", "prosody": {"pace": "slow", "volume": "soft",
             "intensity": 0.3, "pause_before_ms": 123, "interruptible": False}}
    result = ipc_send(recorder, descriptor, event)
    assert "error" not in result
    assert result["event"]["text"] == event["text"]
    assert result["event"]["prosody"] == event["prosody"]
    assert result["event"]["pace"] == "slow"
    recorder._finish()
    assert not recorder._speech_endpoint.exists()
    assert session_format.read_session(recorder.session_dir).speeches == [result["event"]]


def test_ipc_auth_session_and_restart_checks(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="身份校验"):
        record_session.read_player_speech_endpoint(recorder._speech_endpoint, "bad-" + "b" * 40)
    event = {"type": "speech", "source": "persistent_player", "text": "x", "generated_at_ms": 1000}
    with pytest.raises(ValueError, match="当前录制会话"):
        record_session.send_player_speech(recorder._speech_endpoint, recorder._speech_secret, event,
                                         expected_session_id=descriptor["session_id"], expected_instance="old")
    body = {"id": "a" * 32, "instance": descriptor["instance"],
            "session_id": descriptor["session_id"], "event": event}
    (recorder._speech_dir / "requests" / (body["id"] + ".request")).write_bytes(
        record_session._ipc_json({"body": body, "signature": "invalid"}))
    recorder._drain_player_speech()
    assert recorder.speeches == 0
    assert not list(recorder._speech_dir.glob("*.response"))
    recorder._finish()


def test_ipc_rejects_writer_failure_and_stopped_session(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "x", "generated_at_ms": 1000,
             "thought_id": "failed-thought"}
    original = recorder.writer._write
    def fail_write(payload):
        raise OSError("disk error")
    monkeypatch.setattr(recorder.writer, "_write", fail_write)
    result = ipc_send(recorder, descriptor, event)
    assert isinstance(result.get("error"), RuntimeError)
    assert "disk error" in str(result["error"])
    assert recorder.speeches == 0
    monkeypatch.setattr(recorder.writer, "_write", original)
    recorder.stopped = True
    result = ipc_send(recorder, descriptor, event)
    assert isinstance(result.get("error"), RuntimeError)
    assert recorder.speeches == 0
    recorder._finish()


def test_session_loop_consumes_ipc_without_extra_writer_thread(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    class QuietSocket:
        def __init__(self):
            self.hello_sent = False

        def recv(self, timeout):
            if not self.hello_sent:
                self.hello_sent = True
                return json.dumps({"type": "hello", "sessionId": "live-session"})
            time.sleep(0.01)
            raise TimeoutError()
    worker = threading.Thread(target=recorder._session_loop, args=(QuietSocket(),))
    worker.start()
    try:
        event = {"type": "speech", "source": "persistent_player", "text": "loop",
                 "generated_at_ms": 1000, "thought_id": "loop-thought"}
        receipt = record_session.send_player_speech(
            recorder._speech_endpoint, recorder._speech_secret, event,
            expected_session_id=descriptor["session_id"], expected_instance=descriptor["instance"])
        assert receipt["text"] == "loop"
    finally:
        recorder.stopped = True
        worker.join(timeout=2)
        assert not worker.is_alive()
        recorder._finish()
    assert session_format.read_session(recorder.session_dir).speeches[0] == receipt


def test_ipc_rejects_authenticated_wrong_session_and_replay(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "x", "generated_at_ms": 1000}
    body = {"id": "b" * 32, "instance": descriptor["instance"], "session_id": "old", "event": event,
            "write_deadline_ns": time.monotonic_ns() + 10_000_000_000}
    path = recorder._speech_dir / "requests" / (body["id"] + ".request")
    def submit():
        path.write_bytes(record_session._ipc_json({"body": body, "signature": record_session._ipc_signature(recorder._speech_secret, body)}))
        recorder._drain_player_speech()
        return json.loads((recorder._speech_dir / (body["id"] + ".response")).read_text(encoding="utf-8"))["body"]
    assert submit()["status"] == "error"
    body["session_id"] = descriptor["session_id"]
    assert submit()["status"] == "error"
    assert recorder.speeches == 0
    recorder._finish()


def test_ipc_timeout_is_unknown_not_delivery(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "x", "generated_at_ms": 1000,
             "thought_id": "timed-out-thought"}
    with pytest.raises(TimeoutError, match="结果未知"):
        record_session.send_player_speech(recorder._speech_endpoint, recorder._speech_secret, event,
                                         expected_session_id=descriptor["session_id"],
                                         expected_instance=descriptor["instance"], timeout=0.05)
    assert recorder.speeches == 0
    # 客户端仍须视为未知；此处单写者尚未处理，过期请求不会写入。
    recorder._drain_player_speech()
    assert recorder.speeches == 0
    recorder._finish()


def test_ipc_refuses_forged_success_receipt(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    real_write = record_session._ipc_atomic_write
    def forge(path, payload, **kwargs):
        real_write(path, payload, **kwargs)
        if path.suffix == ".request":
            request = json.loads(payload)["body"]
            fake = {"id": request["id"], "instance": request["instance"],
                    "session_id": request["session_id"], "request_hash": "wrong",
                    "status": "written", "event": {"text": "forged"}}
            real_write(path.parent.parent / (path.stem + ".response"), record_session._ipc_json({"body": fake, "signature": "bad"}))
    monkeypatch.setattr(record_session, "_ipc_atomic_write", forge)
    with pytest.raises(RuntimeError, match="回执验证失败"):
        record_session.send_player_speech(recorder._speech_endpoint, recorder._speech_secret,
            {"type": "speech", "source": "persistent_player", "text": "x", "generated_at_ms": 1000},
            expected_session_id=descriptor["session_id"], expected_instance=descriptor["instance"])
    recorder.stopped = True
    recorder._finish()
    assert recorder.speeches == 0


def test_ipc_refuses_signed_receipt_for_different_content(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    real_write = record_session._ipc_atomic_write
    def wrong_content(path, payload, **kwargs):
        real_write(path, payload, **kwargs)
        if path.suffix == ".request":
            body = json.loads(payload)["body"]
            receipt = {"id": body["id"], "instance": body["instance"],
                       "session_id": body["session_id"],
                       "request_hash": record_session.hashlib.sha256(record_session._ipc_json(body)).hexdigest(),
                       "status": "written", "event": {**body["event"], "text": "different"}}
            real_write(path.parent.parent / (path.stem + ".response"), record_session._ipc_json({
                "body": receipt, "signature": record_session._ipc_signature(recorder._speech_secret, receipt)}))
    monkeypatch.setattr(record_session, "_ipc_atomic_write", wrong_content)
    event = {"type": "speech", "source": "persistent_player", "text": "original",
             "generated_at_ms": 1000, "thought_id": "receipt-thought"}
    with pytest.raises(RuntimeError, match="回执内容不匹配"):
        record_session.send_player_speech(recorder._speech_endpoint, recorder._speech_secret, event,
                                         expected_session_id=descriptor["session_id"],
                                         expected_instance=descriptor["instance"])
    recorder.stopped = True
    recorder._finish()


def test_ipc_requires_thought_and_deduplicates_new_request_ids(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "same thought",
             "generated_at_ms": 1000}
    assert isinstance(ipc_send(recorder, descriptor, event).get("error"), RuntimeError)
    event["thought_id"] = "persisted-thought"
    first = ipc_send(recorder, descriptor, event)
    assert first["event"]["text"] == event["text"]
    event["text"] = "changed replay"
    second = ipc_send(recorder, descriptor, event)
    assert isinstance(second.get("error"), RuntimeError)
    assert recorder.speeches == 1
    recorder._finish()
    assert [s["text"] for s in session_format.read_session(recorder.session_dir).speeches] == ["same thought"]


def test_ipc_failed_flush_never_retries_same_thought(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    event = {"type": "speech", "source": "persistent_player", "text": "maybe written",
             "generated_at_ms": 1000, "thought_id": "flush-thought"}
    original = recorder.writer._write
    def append_then_fail(payload):
        original(payload)
        raise OSError("flush result unknown")
    monkeypatch.setattr(recorder.writer, "_write", append_then_fail)
    first = ipc_send(recorder, descriptor, event)
    assert "结果未知" in str(first["error"])
    monkeypatch.setattr(recorder.writer, "_write", original)
    second = ipc_send(recorder, descriptor, event)
    assert isinstance(second.get("error"), RuntimeError)
    recorder._finish()
    assert len(session_format.read_session(recorder.session_dir).speeches) == 1


def test_player_speech_writer_thread_ownership(tmp_path):
    args = build_args("ws://127.0.0.1:1/live", tmp_path)
    args.capture = "none"
    recorder = record_session.SessionRecorder(args)
    recorder._ensure_writer(1000)
    failures = []
    def submit():
        try:
            recorder.record_player_speech(
                {"type": "speech", "source": "persistent_player", "text": "x",
                 "generated_at_ms": 1000}, expected_session_id=recorder.writer.session_id)
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=submit)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(failures) == 1 and "写入线程" in str(failures[0])
    assert recorder.speeches == 0
    recorder._finish()


def test_standalone_recorder_rejects_unavailable_exclusive_mode(tmp_path):
    assert record_session.main(["--speech-mode", "player-exclusive"]) == 2
    args = build_args("ws://127.0.0.1:1/live", tmp_path)
    args.speech_mode = "player-exclusive"
    with pytest.raises(ValueError, match="player-exclusive"):
        record_session.SessionRecorder(args)


def test_endpoint_restart_rejects_old_instance(tmp_path, monkeypatch):
    recorder, descriptor = ipc_recorder(tmp_path, monkeypatch)
    recorder._finish()
    other = player_recorder(tmp_path / "new", monkeypatch)
    other._speech_endpoint = recorder._speech_endpoint
    other._speech_secret = recorder._speech_secret
    other._handle({"type": "hello", "sessionId": "live-session"})
    current = record_session.read_player_speech_endpoint(other._speech_endpoint, other._speech_secret)
    assert current["session_id"] == descriptor["session_id"]
    assert current["instance"] != descriptor["instance"]
    with pytest.raises(ValueError, match="当前录制会话"):
        record_session.send_player_speech(other._speech_endpoint, other._speech_secret,
            {"type": "speech", "source": "persistent_player", "text": "old", "generated_at_ms": 1000},
            expected_session_id=descriptor["session_id"], expected_instance=descriptor["instance"])
    other._finish()


def test_endpoint_refuses_to_overwrite_existing_file(tmp_path, monkeypatch):
    recorder = player_recorder(tmp_path, monkeypatch)
    recorder._speech_endpoint = tmp_path / "endpoint.json"
    recorder._speech_endpoint.write_text("existing", encoding="utf-8")
    recorder._speech_secret = "secret-" + "a" * 40
    with pytest.raises(FileExistsError):
        recorder._handle({"type": "hello", "sessionId": "live-session"})
    assert recorder._speech_endpoint.read_text(encoding="utf-8") == "existing"
    recorder._finish()


def test_exclusive_producer_ready_requires_matching_signed_session(tmp_path, monkeypatch):
    recorder = player_recorder(tmp_path, monkeypatch, exclusive=True)
    recorder.bind_expected_speech_producer(os.getpid())
    recorder._handle({"type": "hello", "sessionId": "live-session"})
    descriptor = record_session.read_player_speech_endpoint(recorder._speech_endpoint, recorder._speech_secret)
    ready_path = recorder._speech_dir / "producer.ready"
    body = {"session_id": descriptor["session_id"], "instance": descriptor["instance"],
            "challenge": "wrong", "pid": os.getpid(), "deadline_ns": time.monotonic_ns() + 5_000_000_000}
    ready_path.write_bytes(record_session._ipc_json({"body": body,
                           "signature": record_session._ipc_signature(recorder._speech_secret, body)}))
    recorder._drain_player_speech()
    assert not recorder.speech_producer_ready.is_set()

    result = {}
    def announce():
        try:
            result["ready"] = record_session.announce_player_speech_producer(
                recorder._speech_endpoint, recorder._speech_secret, timeout=2)
        except Exception as exc:
            result["error"] = exc
    thread = threading.Thread(target=announce)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not recorder.speech_producer_ready.is_set():
        recorder._drain_player_speech()
        time.sleep(0.005)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert "error" not in result
    assert recorder.speech_producer_ready.is_set()
    assert recorder.speech_producer_pid == os.getpid()
    heartbeat = record_session.read_player_speech_heartbeat(recorder._speech_endpoint, recorder._speech_secret)
    assert heartbeat["live_session_id"] == "live-session"
    assert heartbeat["pid"] == os.getpid()
    assert heartbeat["legacy_formal_speech_excluded"] is True
    recorder._finish()


def test_player_exclusive_excludes_old_speech_but_keeps_thought(tmp_path, monkeypatch):
    recorder = player_recorder(tmp_path, monkeypatch, exclusive=True)
    recorder._on_commentary({"text": "旧解说", "kind": "reaction"})
    recorder._on_commentary({"text": "内部思考", "kind": "thought"})
    assert recorder.speeches == 0
    assert recorder.thoughts == 1
    recorder._finish()
    session = session_format.read_session(recorder.session_dir)
    assert not session.speeches
    assert session.thoughts[0]["text"] == "内部思考"


def test_audio_event_uses_measured_launch_not_last_event(tmp_path, monkeypatch):
    """启动区间独立于其他事件（包括延伸到将来的 speech）。"""
    recorder = player_recorder(tmp_path, monkeypatch)
    recorder.args.audio_device = "synthetic"
    recorder.last_event_t = 9000

    class FakeAudio:
        def __init__(self, path, device):
            self.start_unix_ms = 2250
            self.start_window_ms = (2200, 2300)
            self.error = None
        def start(self):
            pass
        def stop(self):
            return 5000

    monkeypatch.setattr(record_session, "AudioSink", FakeAudio)
    recorder._start_audio()
    recorder._finish()
    session = session_format.read_session(recorder.session_dir)
    event = session.of_type("audio")[0]
    assert event["t"] == event["start_offset_ms"] == 1250
    assert event["process_start_window_unix_ms"] == [2200, 2300]
    assert event["process_start_uncertainty_ms"] == 51
    assert event["timing_basis"] == "popen_call_midpoint"
    assert event["first_sample_offset_ms"] is None
    assert session.duration_ms == 9000


def test_audio_launch_measurement_reaches_session_event(tmp_path, monkeypatch):
    """真实 AudioSink 启动路径通过录制器落盘，而不是只测试替身字段。"""
    import io
    recorder = player_recorder(tmp_path, monkeypatch)
    recorder.args.audio_device = "fake"
    recorder.last_event_t = 9000
    times = iter([2.2, 2.3])
    class Process:
        stdin = io.BytesIO()
        stderr = io.BytesIO()
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
    monkeypatch.setattr(record_session.time, "time", lambda: next(times))
    monkeypatch.setattr(record_session.time, "sleep", lambda _: None)
    monkeypatch.setattr(record_session.shutil, "which", lambda _: "fake")
    monkeypatch.setattr(record_session.subprocess, "Popen", lambda *a, **k: Process())
    recorder._start_audio()
    event = session_format.read_session(recorder.session_dir).of_type("audio")[0]
    assert event["t"] == 1250
    assert event["process_start_window_unix_ms"] == [2200, 2300]
    assert event["first_sample_offset_ms"] is None
    recorder._finish()


def test_audio_length_contributes_to_session_end(tmp_path, monkeypatch):
    recorder = player_recorder(tmp_path, monkeypatch)
    class FakeAudio:
        start_unix_ms = 2250
        error = None
        def stop(self):
            return 5000
    recorder.audio = FakeAudio()
    recorder._finish()
    session = session_format.read_session(recorder.session_dir)
    assert session.duration_ms == 6250
    assert session.of_type("end")[0]["counts"]["game_audio_ms"] == 5000


def test_audio_sink_measures_popen_and_reports_early_exit(tmp_path, monkeypatch):
    """检测退出、探测真实文件时长，不用等待一秒后的当前时钟。"""
    import io
    class FakeProcess:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stderr = io.BytesIO(b"device failed")
            self.returncode = None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            return self.returncode

    proc = FakeProcess()
    times = iter([2.2, 2.3])
    monkeypatch.setattr(record_session.time, "time", lambda: next(times))
    monkeypatch.setattr(record_session.time, "sleep", lambda _: None)
    monkeypatch.setattr(record_session.shutil, "which", lambda _: "fake")
    monkeypatch.setattr(record_session.subprocess, "Popen", lambda *a, **k: proc)
    sink = record_session.AudioSink(tmp_path / "audio.m4a", "device")
    sink.start()
    assert sink.start_window_ms == (2200, 2300)
    assert sink.start_unix_ms == 2250
    proc.returncode = 1
    assert sink.stop() == 0
    assert "意外退出" in sink.error and "device failed" in sink.error
    assert "无法探测" in sink.error
    assert sink.stop() == 0


@requires_ffmpeg
def test_audio_sink_probes_file_duration_not_process_lifetime(tmp_path, monkeypatch):
    """实际媒体仅 0.4 秒，stop 读取 ffprobe，而非估计进程运行时长。"""
    import io
    audio = tmp_path / "capture.m4a"
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.4", "-c:a", "aac", str(audio)],
                   check=True, timeout=60)
    class FakeProcess:
        stdin = io.BytesIO()
        stderr = io.BytesIO()
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
    sink = record_session.AudioSink(audio, "device")
    sink._proc = FakeProcess()
    assert 380 <= sink.stop() <= 450
    assert sink.error is None


def test_audio_sink_start_failure_is_not_published(tmp_path, monkeypatch):
    import io
    class FailedProcess:
        stdin = io.BytesIO()
        stderr = io.BytesIO(b"device unavailable")
        def poll(self):
            return 1
    monkeypatch.setattr(record_session.shutil, "which", lambda _: "fake")
    monkeypatch.setattr(record_session.subprocess, "Popen", lambda *a, **k: FailedProcess())
    monkeypatch.setattr(record_session.time, "sleep", lambda _: None)
    sink = record_session.AudioSink(tmp_path / "audio.m4a", "device")
    with pytest.raises(RuntimeError, match="device unavailable"):
        sink.start()
    assert sink.stop() == 0


@requires_ffmpeg
def test_recorder_end_to_end(tmp_path: Path) -> None:
    jpeg = make_jpeg(tmp_path / "frame.jpg")
    out = tmp_path / "sessions"
    with FakeLiveServer(jpeg) as server:
        args = build_args(f"ws://127.0.0.1:{server.port}/ws/v1/agent/live", out)
        recorder = record_session.SessionRecorder(args)
        thread = threading.Thread(target=recorder.run)
        thread.start()
        thread.join(timeout=20)
        assert not thread.is_alive(), "录制器没有在会话停止后退出"

    session_dir = out / "fake-session"
    assert session_dir.is_dir()
    session = session_format.read_session(session_dir)
    session.validate()
    assert session.meta["session_id"] == session_dir.name
    assert session.meta["source"]["capture"] == "frames"
    assert session.of_type("speech"), "应记录解说"
    assert session.of_type("thought"), "应记录思考"
    assert session.of_type("guidance"), "应记录指导"
    assert session.of_type("action"), "应记录动作"

    speech = session.speeches[0]
    assert speech["text"] == "前面好像有个宝箱！"
    assert speech["emotion"] == "excited"
    assert speech["dur"] > 0
    assert speech["avatar"]["mouth_open"]["values"]

    video = session_dir / "gameplay.mp4"
    assert video.is_file() and video.stat().st_size > 0
    duration = probe_duration(video)
    assert 0.2 <= duration <= 2.0, f"视频时长异常：{duration}"

    # 基础姿态与眨眼是录制器生成的，渲染器不需要再猜。
    avatar_ops = {event.get("op") for event in session.of_type("avatar")}
    assert "pose" in avatar_ops


@requires_ffmpeg
def test_recorder_capture_none_writes_events_only(tmp_path: Path) -> None:
    jpeg = make_jpeg(tmp_path / "frame.jpg")
    out = tmp_path / "events"
    with FakeLiveServer(jpeg) as server:
        args = build_args(
            f"ws://127.0.0.1:{server.port}/ws/v1/agent/live", out, "events-only"
        )
        args.capture = "none"
        recorder = record_session.SessionRecorder(args)
        thread = threading.Thread(target=recorder.run)
        thread.start()
        thread.join(timeout=20)
        assert not thread.is_alive()

    session = session_format.read_session(out / "events-only")
    session.validate()
    assert session.speeches
    assert not (out / "events-only" / "gameplay.mp4").exists()


@requires_ffmpeg
def test_run_episode_records_without_agents(tmp_path: Path) -> None:
    """一条命令的胶水层：只录制、不启动导演/操作桥，也要正常收尾。"""
    jpeg = make_jpeg(tmp_path / "frame.jpg")
    out = tmp_path / "episodes"
    with FakeLiveServer(jpeg) as server:
        code = run_episode.main(
            [
                "--no-start",
                "--no-agents",
                "--keep-live",
                "--token",
                "test-token",
                "--ws-url",
                f"ws://127.0.0.1:{server.port}/ws/v1/agent/live",
                "--out",
                str(out),
                "--name",
                "episode",
                "--capture",
                "frames",
                "--fps",
                "10",
            ]
        )
    assert code == 0
    session = session_format.read_session(out / "episode")
    session.validate()
    assert session.speeches
    assert (out / "episode" / "gameplay.mp4").is_file()
