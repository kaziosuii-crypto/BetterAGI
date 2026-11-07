#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""resume 状态与 shadow 记录器的单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_io  # noqa: E402
import player_state as state_module  # noqa: E402
import shadow_recorder as recorder_module  # noqa: E402


class Clock:
    def __init__(self, start: int = 1_700_000_000_000):
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------


def test_missing_state_returns_default(tmp_path: Path) -> None:
    store = state_module.StateStore(tmp_path / "resume.json", "lumi")
    state = store.load()
    assert state["schema_version"] == 1
    assert state["player_id"] == "lumi"
    assert state["shadow_ticks"] == 0
    assert store.loaded_from_disk is False


def test_state_roundtrip_is_atomic_and_utf8(tmp_path: Path) -> None:
    path = tmp_path / "state" / "resume.json"
    clock = Clock()
    store = state_module.StateStore(path, "lumi", clock=clock)
    state = store.load()
    state["last_internal_thought"] = "前面好像有东西"
    state["current_intention"] = "绕过去看看"
    store.save(state)

    raw = path.read_text(encoding="utf-8")
    assert "前面好像有东西" in raw
    assert "\\u" not in raw
    assert json.loads(raw)["updated_at"] == player_io.utc_now_iso(clock.now)
    assert [item.name for item in path.parent.iterdir()] == ["resume.json"]

    loaded = state_module.StateStore(path, "lumi").load()
    assert loaded["last_internal_thought"] == "前面好像有东西"
    assert loaded["current_intention"] == "绕过去看看"


def test_corrupt_state_is_backed_up_and_recovered(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text("{broken", encoding="utf-8")
    clock = Clock()
    store = state_module.StateStore(path, "lumi", clock=clock)
    state = store.load()

    assert state["player_id"] == "lumi"
    assert store.loaded_from_disk is False
    assert store.recovered_corrupt is not None
    assert store.recovered_corrupt.exists()
    assert "损坏" in store.load_warning
    assert not path.exists()

    store.save(state)
    assert path.exists()
    assert state_module.StateStore(path, "lumi").load()["player_id"] == "lumi"


def test_newer_state_schema_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    payload = {"schema_version": 99, "player_id": "lumi", "mystery": "keep me"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(state_module.StateSchemaError, match="高于当前"):
        state_module.StateStore(path, "lumi").load()
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_state_of_another_player_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        json.dumps({"schema_version": 1, "player_id": "someone-else"}),
        encoding="utf-8",
    )
    with pytest.raises(state_module.StateError, match="属于玩家"):
        state_module.StateStore(path, "lumi").load()


def test_unknown_state_fields_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "player_id": "lumi",
                "future_field": {"nested": True},
            }
        ),
        encoding="utf-8",
    )
    store = state_module.StateStore(path, "lumi")
    state = store.load()
    assert state["future_field"] == {"nested": True}
    store.save(state)
    assert json.loads(path.read_text(encoding="utf-8"))["future_field"] == {
        "nested": True
    }


def test_state_normalizes_bad_types(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "player_id": "lumi",
                "unfinished_actions": "not-a-list",
                "shadow_ticks": "7",
                "current_emotion": "sad",
            }
        ),
        encoding="utf-8",
    )
    state = state_module.StateStore(path, "lumi").load()
    assert state["unfinished_actions"] == []
    assert state["shadow_ticks"] == 7
    assert state["current_emotion"] == {}


def test_state_secret_fields_are_dropped_and_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "player_id": "lumi",
                "api_key": "sk-should-not-survive",
                "nested": {"token": "secret-token", "keep": 1},
            }
        ),
        encoding="utf-8",
    )
    store = state_module.StateStore(path, "lumi")
    state = store.load()
    assert "api_key" not in state
    assert state["nested"] == {"keep": 1}
    assert "敏感字段" in store.load_warning

    store.save(state)
    text = path.read_text(encoding="utf-8")
    assert "sk-should-not-survive" not in text
    assert "secret-token" not in text


def test_v0_migration_backs_up_before_save(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    original = {"player_id": "lumi", "last_internal_thought": "旧的"}
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    store = state_module.StateStore(path, "lumi")
    state = store.load()
    assert store.migrated_from == 0
    assert "迁移" in store.load_warning
    store.save(state)

    backup = tmp_path / "resume.json.v0.bak"
    assert backup.exists()
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_state_camel_case_secret_fields_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "player_id": "lumi",
                "accessToken": "SECRET-CAMEL-123",
                "nested": {"refreshToken": "SECRET-REFRESH"},
            }
        ),
        encoding="utf-8",
    )
    store = state_module.StateStore(path, "lumi")
    state = store.load()
    assert "accessToken" not in state
    assert state["nested"] == {}
    assert "敏感字段" in store.load_warning
    store.save(state)
    text = path.read_text(encoding="utf-8")
    assert "SECRET-CAMEL-123" not in text
    assert "SECRET-REFRESH" not in text


def test_state_read_tolerates_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    path.write_text(
        "﻿" + json.dumps({"schema_version": 1, "player_id": "lumi"}),
        encoding="utf-8",
    )
    state = state_module.StateStore(path, "lumi").load()
    assert state["player_id"] == "lumi"


# ----------------------------------------------------------------------
# Shadow recorder
# ----------------------------------------------------------------------


def test_recorder_appends_records_and_end(tmp_path: Path) -> None:
    clock = Clock()
    recorder = recorder_module.ShadowRecorder(
        tmp_path / "shadow", "vtuber", "lumi", run_id="run-1", clock=clock
    )
    first = recorder.write("shadow_tick", proposal={"thought": "嗯"})
    assert first["mode"] == "shadow"
    assert first["executed"] is False
    assert first["sent"] is False
    assert first["seq"] == 1
    assert first["run_t_ms"] == 0
    assert first["run_id"] == "run-1"
    assert first["schema_version"] == 1

    clock.advance(1000)
    recorder.close(shadow_ticks=1)
    with pytest.raises(RuntimeError):
        recorder.write("shadow_tick")

    records = recorder_module.read_shadow_records(recorder.path)
    assert [record["type"] for record in records] == ["shadow_tick", "shadow_end"]
    assert records[-1]["shadow_ticks"] == 1
    assert json.loads(recorder.path.read_text(encoding="utf-8").splitlines()[0])[
        "proposal"
    ] == {"thought": "嗯"}


def test_recorder_close_is_idempotent(tmp_path: Path) -> None:
    recorder = recorder_module.ShadowRecorder(
        tmp_path, "vtuber", "lumi", run_id="run-2", clock=Clock()
    )
    assert recorder.close()["type"] == "shadow_end"
    assert recorder.close() is None
    assert recorder.closed is True


def test_recorder_redacts_secrets(tmp_path: Path) -> None:
    recorder = recorder_module.ShadowRecorder(
        tmp_path, "vtuber", "lumi", run_id="run-3", clock=Clock()
    )
    recorder.write(
        "shadow_error",
        message="request failed with sk-abcdefghijklmnop and Bearer abcdefghijkl",
        url="ws://x/?access_token=secret-token",
        nested={"api_key": "sk-zzzzzzzzzzzzzzzz"},
        camel={"accessToken": "SECRET-CAMEL"},
    )
    text = recorder.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in text
    assert "Bearer abcdefghijkl" not in text
    assert "secret-token" not in text
    assert "sk-zzzzzzzzzzzzzzzz" not in text
    assert "SECRET-CAMEL" not in text


def test_recorder_sanitizes_session_filename(tmp_path: Path) -> None:
    recorder = recorder_module.ShadowRecorder(
        tmp_path, "../../evil/name", "lumi", run_id="run-4", clock=Clock()
    )
    assert recorder.path.parent == tmp_path
    assert ".." not in recorder.path.name
    assert "/" not in recorder.path.name
    assert "\\" not in recorder.path.name


def test_read_shadow_records_reports_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"type":"ok"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 行"):
        recorder_module.read_shadow_records(path)


def test_read_shadow_records_can_tolerate_truncated_tail(tmp_path: Path) -> None:
    path = tmp_path / "truncated.jsonl"
    path.write_text('{"type":"ok"}\n{"type":"tru', encoding="utf-8")
    assert [r["type"] for r in recorder_module.read_shadow_records(path, strict=False)] == [
        "ok"
    ]
    with pytest.raises(ValueError):
        recorder_module.read_shadow_records(path)


def test_recorder_redacts_secrets_adjacent_to_chinese(tmp_path: Path) -> None:
    clock = Clock()
    recorder = recorder_module.ShadowRecorder(
        tmp_path, "vtuber", "lumi", run_id="run-cn", clock=clock
    )
    recorder.write(
        "shadow_error",
        message="密钥sk-abcdefghijklmnop已失效，令牌Bearer abcdefghijkl，token=abc123",
    )
    text = recorder.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in text
    assert "Bearer abcdefghijkl" not in text
    assert "abc123" not in text
    assert "已失效" in text


def test_recorder_survives_lone_surrogates(tmp_path: Path) -> None:
    recorder = recorder_module.ShadowRecorder(
        tmp_path, "vtuber", "lumi", run_id="run-surrogate", clock=Clock()
    )
    record = recorder.write("shadow_tick", proposal={"thought": "ok \ud800 end"})
    assert "\ud800" not in record["proposal"]["thought"]
    text = recorder.path.read_text(encoding="utf-8")
    assert "\ud800" not in text
    assert "ok" in text


def test_state_save_cleans_surrogates(tmp_path: Path) -> None:
    path = tmp_path / "resume.json"
    store = state_module.StateStore(path, "lumi", clock=Clock())
    state = store.load()
    state["unknown_future"] = "ok \ud800 end"
    store.save(state)
    text = path.read_text(encoding="utf-8")
    assert "\ud800" not in text
    assert "ok" in text


def test_atomic_write_rejects_unencodable_text_without_temp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw.txt"
    with pytest.raises(UnicodeError):
        player_io.atomic_write_text(path, "ok \ud800 end")
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_json_write_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "data.json"
    player_io.atomic_write_json(path, {"a": "中文"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "中文"}
    assert list(path.parent.iterdir()) == [path]


def test_atomic_write_retries_locked_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "data.json"
    real_replace = player_io.os.replace
    attempts = {"count": 0}

    def flaky_replace(source, target):
        if attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError("file is locked")
        return real_replace(source, target)

    monkeypatch.setattr(player_io.os, "replace", flaky_replace)
    monkeypatch.setattr(player_io.time, "sleep", lambda _seconds: None)
    player_io.atomic_write_json(path, {"ok": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert attempts["count"] == 1
    assert [item.name for item in path.parent.iterdir()] == ["data.json"]
