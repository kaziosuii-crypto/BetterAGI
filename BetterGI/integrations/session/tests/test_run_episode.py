#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_episode 启动 shadow 玩家的参数接线测试（不启动真实进程）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_episode  # noqa: E402


class FakeProcess:
    def poll(self):
        return None


class GracefulProcess(FakeProcess):
    def __init__(self, exits: bool = True):
        self.exits = exits
        self.terminated = False
        self.waited = False

    def wait(self, timeout=None):
        self.waited = True
        if not self.exits:
            raise run_episode.subprocess.TimeoutExpired("shadow", timeout)
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def poll(self):
        return 0 if (self.exits and self.waited) else None


def test_stop_processes_gives_shadow_a_graceful_window() -> None:
    shadow = GracefulProcess(exits=True)
    run_episode.stop_processes([shadow], shadow, grace_seconds=0.01)
    assert shadow.waited is True
    assert shadow.terminated is False

    stubborn = GracefulProcess(exits=False)
    run_episode.stop_processes([stubborn], stubborn, grace_seconds=0.01)
    assert stubborn.waited is True
    assert stubborn.terminated is True


def test_stop_file_signals_shadow_before_graceful_wait(tmp_path: Path) -> None:
    shadow = GracefulProcess(exits=True)
    stop_file = tmp_path / "shadow.stop"
    run_episode.stop_processes([shadow], shadow, grace_seconds=0.01, shadow_stop_file=stop_file)
    assert stop_file.exists()
    assert shadow.waited is True
    assert shadow.terminated is False


def test_shadow_config_starts_readonly_process(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_popen(command, env=None):
        calls.append((command, env))
        return FakeProcess()

    monkeypatch.setattr(run_episode.subprocess, "Popen", fake_popen)
    options = run_episode.build_parser().parse_args(
        [
            "--player-shadow-config",
            str(tmp_path / "player.json"),
            "--ws-url",
            "ws://remote:30648/ws/v1/agent/live",
        ]
    )
    options.token = "token-value"
    processes = run_episode.start_agent_processes(options)

    assert len(processes) == 1
    command, env = calls[0]
    assert any("run_shadow.py" in part for part in command)
    assert "--config" in command
    assert env["BETTERGI_AGENT_TOKEN"] == "token-value"
    assert env["BETTERGI_LIVE_WS"] == "ws://remote:30648/ws/v1/agent/live"
    assert options.player_shadow_process is processes[0]
    # 旧链路没有配置时不应启动导演/Lumine。
    assert len(calls) == 1


def test_child_processes_receive_only_needed_agent_secrets(monkeypatch):
    calls = []
    monkeypatch.setattr(run_episode.subprocess, "Popen", lambda command, env=None: calls.append((command, env)) or FakeProcess())
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY",
                "BRIDGE_INTENT_TOKEN", "LUMINE_SERVER_TOKEN"):
        monkeypatch.setenv(key, "test-" + key)
    options = run_episode.build_parser().parse_args(["--director-config", "director.json", "--lumine-config", "bridge.json",
                                                      "--player-shadow-config", "player.json"])
    options.token = "bgi-token"
    run_episode.start_agent_processes(options)
    director, motor, player = [env for _, env in calls]
    assert "BRIDGE_INTENT_TOKEN" not in director and "LUMINE_SERVER_TOKEN" not in director
    assert all(key not in motor for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "DEEPSEEK_API_KEY"))
    assert motor["BRIDGE_INTENT_TOKEN"] == "test-BRIDGE_INTENT_TOKEN"
    assert motor["LUMINE_SERVER_TOKEN"] == "test-LUMINE_SERVER_TOKEN"
    assert "LUMINE_SERVER_TOKEN" not in player and player["OPENAI_API_KEY"] == "test-OPENAI_API_KEY"
    assert all(env["BETTERGI_AGENT_TOKEN"] == "bgi-token" for env in (director, motor, player))


def test_shadow_ocr_flag_reaches_only_shadow(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(run_episode.subprocess, "Popen", lambda command, env=None: calls.append(command) or FakeProcess())
    options = run_episode.build_parser().parse_args([
        "--player-shadow-config", str(tmp_path / "player.json"),
        "--player-observe-ocr", "--http-base", "http://127.0.0.1:30648",
    ])
    options.token = "test"
    run_episode.start_agent_processes(options)
    assert "--observe-ocr" in calls[0]
    assert calls[0][calls[0].index("--observe-http") + 1] == "http://127.0.0.1:30648"


def test_player_region_and_embedding_options_reach_player(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(run_episode.subprocess, "Popen", lambda command, env=None: calls.append(command) or FakeProcess())
    options = run_episode.build_parser().parse_args([
        "--player-shadow-config", "player.json", "--player-observe-ocr",
        "--player-named-ocr", "dialogue_text", "10", "20", "200", "50",
        "--player-named-ocr", "quest", "20", "100", "150", "70",
        "--player-dialogue-roi", "0.1", "0.6", "0.9", "0.9",
        "--player-quest-roi", "0.1", "0.1", "0.4", "0.3",
        "--player-embedding-model", str(tmp_path)])
    options.token = "test"
    run_episode.start_agent_processes(options)
    command = calls[0]
    assert command.count("--named-ocr") == 2
    assert command[command.index("--named-ocr") + 1:command.index("--named-ocr") + 6] == ["dialogue_text", "10", "20", "200", "50"]
    assert command[command.index("--dialogue-roi") + 1:command.index("--dialogue-roi") + 5] == ["0.1", "0.6", "0.9", "0.9"]
    assert command[command.index("--embedding-model") + 1] == str(tmp_path)


@pytest.mark.parametrize("args", [
    ["--player-named-ocr", "quest", "1", "2", "3", "4"],
    ["--player-shadow-config", "p.json", "--player-named-ocr", "quest", "1", "2", "3", "4"],
    ["--player-shadow-config", "p.json", "--player-observe-ocr", "--player-ocr-roi", "1", "2", "3", "4",
     "--player-named-ocr", "quest", "1", "2", "3", "4"],
])
def test_invalid_player_observation_options_fail_before_start(args, monkeypatch) -> None:
    monkeypatch.setattr(run_episode.record_session, "SessionRecorder", lambda *_: pytest.fail("must not start"))
    assert run_episode.main(args) == 2


def test_player_speech_options_never_expose_capability_to_observer(monkeypatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(run_episode.subprocess, "Popen", lambda command, env=None: calls.append((command, env)) or FakeProcess())
    monkeypatch.setenv("BETTERGI_PLAYER_SPEECH_SECRET", "should-not-leak")
    options = run_episode.build_parser().parse_args([
        "--director-config", "director.json", "--player-shadow-config", "player.json",
        "--player-speech-endpoint", str(tmp_path / "endpoint.json"), "--speech-mode", "player-exclusive"])
    options.token = "test"
    options.player_speech_secret = "secret-" + "x" * 40
    recorder_args = run_episode.build_recorder_args(options)
    assert recorder_args.player_speech_endpoint == options.player_speech_endpoint
    assert recorder_args.player_speech_secret == options.player_speech_secret
    assert recorder_args.speech_mode == "player-exclusive"
    run_episode.start_agent_processes(options)
    for _command, env in calls:
        assert "BETTERGI_PLAYER_SPEECH_SECRET" not in env
        assert "BETTERGI_PLAYER_SPEECH_ENDPOINT" not in env


@pytest.mark.parametrize("extra", [[], ["--no-agents"], ["--player-shadow-config", "player.json"],
                                  ["--player-speech-endpoint", "endpoint.json"]])
def test_unavailable_player_cannot_silently_replace_formal_speech(extra) -> None:
    assert run_episode.main(["--speech-mode", "player-exclusive", *extra]) == 2


def test_active_recorder_requests_strict_safety() -> None:
    options = run_episode.build_parser().parse_args(["--player-mode", "motor_active"])
    options.token = "test"
    assert run_episode.build_recorder_args(options).strict_safety is True
    observer = run_episode.build_parser().parse_args([])
    observer.token = "test"
    assert run_episode.build_recorder_args(observer).strict_safety is False


def test_audio_device_is_forwarded_to_recorder() -> None:
    options = run_episode.build_parser().parse_args(["--audio-device", "Test Loopback"])
    options.token = "test"
    assert run_episode.build_recorder_args(options).audio_device == "Test Loopback"


def test_no_shadow_config_starts_nothing(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        run_episode.subprocess,
        "Popen",
        lambda command, env=None: calls.append(command) or FakeProcess(),
    )
    options = run_episode.build_parser().parse_args([])
    options.token = "token-value"
    assert run_episode.start_agent_processes(options) == []
    assert calls == []


def test_spawn_failure_cleans_up_started_children(monkeypatch) -> None:
    started = []

    def fake_popen(command, env=None):
        if started:
            raise OSError("cannot spawn")
        process = FakeProcess()
        started.append(process)
        return process

    stopped = []
    monkeypatch.setattr(run_episode.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        run_episode, "stop_processes", lambda processes: stopped.extend(processes)
    )
    options = run_episode.build_parser().parse_args(
        [
            "--director-config",
            "director.json",
            "--lumine-config",
            "lumine.json",
            "--player-shadow-config",
            "player.json",
        ]
    )
    options.token = "token-value"
    with pytest.raises(OSError):
        run_episode.start_agent_processes(options)
    assert stopped == started
    assert len(started) == 1


def test_no_agents_clears_shadow_config(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        run_episode.subprocess,
        "Popen",
        lambda command, env=None: calls.append(command) or FakeProcess(),
    )
    options = run_episode.build_parser().parse_args(
        ["--no-agents", "--player-shadow-config", "x.json"]
    )
    options.token = "token-value"
    run_episode.apply_no_agents(options)
    assert run_episode.start_agent_processes(options) == []
    assert calls == []
