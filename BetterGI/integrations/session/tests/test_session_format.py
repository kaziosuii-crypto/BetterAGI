#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话文件格式的单元测试。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_format as sf  # noqa: E402


def test_writer_reader_roundtrip(tmp_path: Path) -> None:
    with sf.SessionWriter(
        tmp_path / "s", session_id="abc", t0_unix_ms=1_700_000_000_000
    ) as writer:
        writer.emit_thought(120, "前面有丘丘人")
        speech = writer.emit_speech(
            800,
            "哇，这边居然藏了个宝箱！",
            kind="reaction",
            emotion="surprised",
            frame_seq=7,
        )
        writer.emit_action(
            900, keys=["W", "Shift"], mouse={"dx": 12, "dy": -4}, scroll=1
        )
        writer.emit_avatar(1000, "blink", dur=120)
        writer.close(duration_ms=3000, counts={"speeches": 1})

    session = sf.read_session(tmp_path / "s")
    session.validate()
    assert session.session_id == "abc"
    assert session.duration_ms == 3000
    assert [event["type"] for event in session.events] == [
        "thought",
        "speech",
        "action",
        "avatar",
        "end",
    ]
    stored = session.events[1]
    assert stored["text"] == "哇，这边居然藏了个宝箱！"
    assert stored["frame"] == 7
    assert stored["avatar"]["expression"] == "surprised"
    assert stored["avatar"]["mouth_open"]["step_ms"] == 50
    assert len(stored["avatar"]["mouth_open"]["values"]) == stored["dur"] // 50 + 1
    assert speech["dur"] >= 700

    raw = (tmp_path / "s" / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(raw[0])["type"] == "session"
    assert "\\u" not in raw[1], "中文应原样写入，不转义"


def test_estimate_speech_ms_is_bounded_and_monotonic() -> None:
    short = sf.estimate_speech_ms("好")
    long = sf.estimate_speech_ms(
        "这是一段明显更长的中文解说，用来验证时长随文本增长。" * 4
    )
    assert 700 <= short <= 20_000
    assert short < long <= 20_000
    assert sf.estimate_speech_ms("") == 700


def test_mouth_envelope_is_deterministic_and_in_range() -> None:
    first = sf.mouth_envelope("测试口型", 2000, seed=42)
    second = sf.mouth_envelope("测试口型", 2000, seed=42)
    other = sf.mouth_envelope("测试口型", 2000, seed=43)
    assert first == second
    assert first != other
    assert len(first) == 2000 // 50 + 1
    assert all(0.0 <= value <= 1.0 for value in first)
    assert first[0] == pytest.approx(0.04, abs=0.01)
    assert max(first) > 0.5


def test_avatar_for_speech_maps_emotion() -> None:
    happy = sf.avatar_for_speech("好耶", "happy", 1200)
    sad = sf.avatar_for_speech("唉", "sad", 1200)
    assert happy["expression"] == "happy"
    assert happy["params"]["ParamMouthForm"] > 0
    assert sad["expression"] == "sad"
    assert sad["params"]["ParamMouthForm"] < 0
    assert len(happy["mouth_open"]["values"]) == 1200 // 50 + 1


def test_reader_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "session", "version": 99, "session_id": "x"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="版本"):
        sf.read_session(path)


def test_validate_requires_end(tmp_path: Path) -> None:
    with sf.SessionWriter(tmp_path / "s") as writer:
        writer.emit_thought(0, "未收尾")
        writer.close(duration_ms=100)
    lines = (tmp_path / "s" / "session.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "s" / "session.jsonl").write_text(
        "\n".join(lines[:-1]) + "\n", encoding="utf-8"
    )
    session = sf.read_session(tmp_path / "s")
    with pytest.raises(ValueError, match="end"):
        session.validate()


def test_writer_rejects_unknown_event() -> None:
    with tempfile.TemporaryDirectory() as directory:
        with sf.SessionWriter(directory) as writer:
            with pytest.raises(ValueError, match="未知事件类型"):
                writer.emit(0, "bogus", text="x")


@pytest.mark.parametrize("kwargs", [
    {"source": "director"}, {"generated_at_ms": True},
    {"generated_at_ms": 1.5}, {"generated_at_ms": 0},
    {"thought_id": " "}, {"intent_id": 1},
    {"source": "persistent_player", "frame_seq": True},
    {"source": "persistent_player", "frame_seq": -1},
])
def test_speech_metadata_rejects_invalid_values(tmp_path: Path, kwargs) -> None:
    with sf.SessionWriter(tmp_path / "s", t0_unix_ms=1000) as writer:
        with pytest.raises(ValueError):
            writer.emit_speech(0, "内容", **kwargs)
        assert writer.event_count == 0


def test_player_speech_timestamp_provenance_cannot_drift(tmp_path: Path) -> None:
    with sf.SessionWriter(tmp_path / "s", t0_unix_ms=1000) as writer:
        with pytest.raises(ValueError, match="生成时间"):
            writer.emit_speech(0, "内容", source="persistent_player")
        with pytest.raises(ValueError, match="生成时间"):
            writer.emit_speech(99, "内容", source="persistent_player", generated_at_ms=1100)
        event = writer.emit_speech(100, "  原样\n文本  ", source="persistent_player",
                                   generated_at_ms=1100, thought_id="thought")
        assert event["text"] == "  原样\n文本  "
        assert event["thought_id"] == "thought"
    assert sf.read_session(tmp_path / "s").speeches == [event]


def test_legacy_speech_fields_stay_compatible(tmp_path: Path) -> None:
    with sf.SessionWriter(tmp_path / "s", t0_unix_ms=1000) as writer:
        event = writer.emit_speech(10, " 旧文本 ", frame_seq=7)
        assert event["text"] == "旧文本"
        assert event["frame"] == 7
        assert not ({"source", "generated_at_ms", "thought_id", "intent_id", "frame_seq"} & event.keys())
    assert sf.read_session(tmp_path / "s").speeches[0] == event


def test_speech_prosody_fields_roundtrip(tmp_path: Path) -> None:
    with sf.SessionWriter(tmp_path / "s", session_id="p", t0_unix_ms=1000) as writer:
        writer.emit_speech(
            100, "先等一下。", emotion="worried", intensity=0.7, pace="slow",
            volume="soft", pause_before_ms=300, interruptible=False,
            thought_source="dialogue",
        )
        writer.close(duration_ms=1000)
    event = sf.read_session(tmp_path / "s").speeches[0]
    assert event["intensity"] == 0.7
    assert event["pace"] == "slow"
    assert event["volume"] == "soft"
    assert event["pause_before_ms"] == 300
    assert event["interruptible"] is False
    assert event["thought_source"] == "dialogue"
