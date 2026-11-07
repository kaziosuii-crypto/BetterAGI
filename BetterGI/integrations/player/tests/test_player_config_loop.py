#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置解析与 deliberative 循环测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_config as config_module  # noqa: E402
import player_loop as loop_module  # noqa: E402


def test_defaults_without_env(tmp_path: Path) -> None:
    config = config_module.resolve_config(env={})
    assert config.enabled is True
    assert config.no_llm is False
    assert config.session_id == "vtuber"
    assert config.interval == config_module.DEFAULT_INTERVAL
    assert config.profile_is_default is True
    assert config.profile_path.name == "profile.json"
    assert config.state_path.name == "resume_state.json"
    assert config.shadow_dir.name == "shadow"
    assert config.token == ""


def test_env_overrides(tmp_path: Path) -> None:
    env = {
        "PLAYER_HOME": str(tmp_path / "home"),
        "PLAYER_PROFILE": str(tmp_path / "custom-profile.json"),
        "PLAYER_STATE": str(tmp_path / "custom-state.json"),
        "PLAYER_SHADOW_DIR": str(tmp_path / "custom-shadow"),
        "BETTERGI_LIVE_WS": "ws://example:1234/ws",
        "BETTERGI_AGENT_TOKEN": "secret-token",
        "BETTERGI_SESSION_ID": "env-session",
        "PLAYER_INTERVAL": "7.5",
        "PLAYER_AGENT_ENABLED": "0",
        "PLAYER_MAX_FRAME_AGE_MS": "1234",
    }
    config = config_module.resolve_config(env=env)
    assert config.home == (tmp_path / "home").resolve()
    assert config.profile_path == tmp_path / "custom-profile.json"
    assert config.profile_is_default is False
    assert config.state_path == tmp_path / "custom-state.json"
    assert config.shadow_dir == tmp_path / "custom-shadow"
    assert config.ws_url == "ws://example:1234/ws"
    assert config.token == "secret-token"
    assert config.session_id == "env-session"
    assert config.interval == 7.5
    assert config.enabled is False
    assert config.max_frame_age_ms == 1234


def test_config_file_relative_paths_and_camel_case(tmp_path: Path) -> None:
    config_path = tmp_path / "player.json"
    config_path.write_text(
        json.dumps(
            {
                "profile": "profiles/lumi.json",
                "state": "state/resume.json",
                "shadowDir": "state/shadow",
                "sessionId": "file-session",
                "interval": 30,
                "bettergiWs": "ws://file:30648/ws",
                "enabled": True,
                "token": "must-be-ignored",
            }
        ),
        encoding="utf-8",
    )
    config = config_module.resolve_config(config_path, env={})
    assert config.profile_path == (tmp_path / "profiles" / "lumi.json").resolve()
    assert config.state_path == (tmp_path / "state" / "resume.json").resolve()
    assert config.shadow_dir == (tmp_path / "state" / "shadow").resolve()
    assert config.session_id == "file-session"
    assert config.interval == 30
    assert config.ws_url == "ws://file:30648/ws"
    assert config.token == ""  # JSON 配置里的令牌永远不读


def test_profile_at_default_path_is_seedable(tmp_path: Path) -> None:
    config_path = tmp_path / "player.json"
    config_path.write_text(
        json.dumps({"profile": "profile/profile.json"}), encoding="utf-8"
    )
    config = config_module.resolve_config(
        config_path, env={"PLAYER_HOME": str(tmp_path)}
    )
    assert config.profile_path == tmp_path / "profile" / "profile.json"
    assert config.profile_is_default is True

    custom = config_module.resolve_config(
        env={"PLAYER_HOME": str(tmp_path), "PLAYER_PROFILE": str(tmp_path / "x.json")}
    )
    assert custom.profile_is_default is False


def test_memory_paths_and_context_builder(tmp_path: Path) -> None:
    config = config_module.resolve_config(
        env={
            "PLAYER_HOME": str(tmp_path / "home"),
            "CONTEXT_BUILDER": "legacy",
            "PLAYER_MEMORY_DIR": str(tmp_path / "mem"),
        }
    )
    assert config.memory_dir == tmp_path / "mem"
    assert config.episodes_dir == tmp_path / "mem" / "episodes"
    assert config.semantic_path == tmp_path / "mem" / "semantic" / "facts.json"
    assert config.working_path == tmp_path / "home" / "state" / "working.json"
    assert config.recap_path == tmp_path / "home" / "state" / "session_recap.json"
    assert config.context_builder == "legacy"

    default = config_module.resolve_config(env={"PLAYER_HOME": str(tmp_path / "h2")})
    assert default.context_builder == "v2"
    assert default.memory_dir == tmp_path / "h2" / "memory"
    assert default.episodes_dir == tmp_path / "h2" / "memory" / "episodes"
    assert default.semantic_path == (
        tmp_path / "h2" / "memory" / "semantic" / "facts.json"
    )


def test_config_file_memory_keys_and_invalid_context_builder(tmp_path: Path) -> None:
    config_path = tmp_path / "player.json"
    config_path.write_text(
        json.dumps(
            {
                "memoryDir": "memory-root",
                "episodesDir": "memory-root/eps",
                "semanticPath": "memory-root/semantic.json",
                "workingPath": "state/w.json",
                "contextBuilder": "nonsense",
            }
        ),
        encoding="utf-8",
    )
    config = config_module.resolve_config(config_path, env={})
    assert config.memory_dir == (tmp_path / "memory-root").resolve()
    assert config.episodes_dir == (tmp_path / "memory-root" / "eps").resolve()
    assert config.semantic_path == (tmp_path / "memory-root" / "semantic.json").resolve()
    assert config.working_path == (tmp_path / "state" / "w.json").resolve()
    assert config.context_builder == "v2"


def test_example_config_respects_player_home(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "player_config.example.json"
    config = config_module.resolve_config(
        example, env={"PLAYER_HOME": str(tmp_path / "home")}
    )
    assert config.profile_path == tmp_path / "home" / "profile" / "profile.json"
    assert config.state_path == tmp_path / "home" / "state" / "resume_state.json"
    assert config.shadow_dir == tmp_path / "home" / "state" / "shadow"
    assert config.profile_is_default is True


def test_blank_env_does_not_override_explicit_config(tmp_path: Path) -> None:
    config_path = tmp_path / "player.json"
    config_path.write_text(
        json.dumps({"enabled": False, "shadow": False}), encoding="utf-8"
    )
    config = config_module.resolve_config(
        config_path,
        env={"PLAYER_AGENT_ENABLED": "", "PLAYER_AGENT_SHADOW": "  "},
    )
    assert config.enabled is False
    assert config.shadow_mode is False


def test_shadow_mode_switch(tmp_path: Path) -> None:
    assert config_module.resolve_config(env={}).shadow_mode is True
    disabled = config_module.resolve_config(env={"PLAYER_AGENT_SHADOW": "0"})
    assert disabled.shadow_mode is False
    assert (
        config_module.resolve_config(env={"PLAYER_AGENT_SHADOW": "1"}).shadow_mode
        is True
    )


def test_precedence_cli_env_config(tmp_path: Path) -> None:
    config_path = tmp_path / "player.json"
    config_path.write_text(json.dumps({"interval": 30, "sessionId": "file"}), encoding="utf-8")
    env = {"PLAYER_INTERVAL": "20", "BETTERGI_SESSION_ID": "env"}
    config = config_module.resolve_config(
        config_path, env=env, interval=10, session_id="cli"
    )
    assert config.interval == 10
    assert config.session_id == "cli"

    env_only = config_module.resolve_config(config_path, env=env)
    assert env_only.interval == 20
    assert env_only.session_id == "env"


def test_interval_is_clamped(tmp_path: Path) -> None:
    assert config_module.resolve_config(env={"PLAYER_INTERVAL": "0.1"}).interval == 1.0
    assert (
        config_module.resolve_config(env={"PLAYER_INTERVAL": "99999"}).interval == 600.0
    )


def test_token_file_and_missing_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text("  file-token\n", encoding="utf-8")
    config = config_module.resolve_config(env={}, token_file=token_file)
    assert config.token == "file-token"

    assert config_module.resolve_config(env={}, token="  abc  ").token == "abc"

    with pytest.raises(ValueError, match="令牌文件失败"):
        config_module.resolve_config(env={}, token_file=tmp_path / "missing.txt")


def test_missing_config_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        config_module.resolve_config(tmp_path / "missing.json", env={})


def test_ws_url_token_and_redaction() -> None:
    url = config_module.build_ws_url("ws://127.0.0.1:30648/ws", "abc")
    assert "access_token=abc" in url
    assert "abc" not in config_module.redact_url(url)
    assert "access_token" not in config_module.redact_url(url)

    rotated = config_module.build_ws_url(
        "ws://127.0.0.1:30648/ws?access_token=OLD&keep=1", "NEW"
    )
    assert "access_token=NEW" in rotated
    assert "OLD" not in rotated
    assert "keep=1" in rotated
    assert "access_token" not in config_module.build_ws_url(
        "ws://127.0.0.1:30648/ws?token=OLD", ""
    )

    redacted = config_module.redact_url("ws://user:pass@127.0.0.1:30648/ws?token=x")
    assert "user" not in redacted
    assert "pass" not in redacted


def test_ensure_directories_creates_but_does_not_seed_profile(tmp_path: Path) -> None:
    config = config_module.resolve_config(
        env={"PLAYER_HOME": str(tmp_path / "home")}
    )
    config_module.ensure_directories(config)
    assert config.profile_path.parent.exists()
    assert config.state_path.parent.exists()
    assert config.shadow_dir.exists()
    assert not config.profile_path.exists()


# ----------------------------------------------------------------------
# Loop
# ----------------------------------------------------------------------


class FakeAgent:
    def __init__(self, calls_before_stop=None, fail_first=False):
        self.calls = 0
        self.force_flags = []
        self.stop_event = None
        self.calls_before_stop = calls_before_stop
        self.fail_first = fail_first

    async def tick(self, force=False):
        self.calls += 1
        self.force_flags.append(force)
        if self.fail_first and self.calls == 1:
            raise RuntimeError("boom")
        if self.calls_before_stop and self.calls >= self.calls_before_stop:
            if self.stop_event is not None:
                self.stop_event.set()
        return self.calls


def test_wait_seconds() -> None:
    clock = lambda: 12.0  # noqa: E731
    loop = loop_module.DeliberativeLoop(FakeAgent(), 2.0, clock=clock)
    assert loop.wait_seconds(10.0, now=10.0) == 2.0
    assert loop.wait_seconds(10.0, now=11.5) == 0.5
    assert loop.wait_seconds(10.0, now=20.0) == loop_module.MIN_WAIT_S


def test_run_once_forces_tick() -> None:
    fake = FakeAgent()
    loop = loop_module.DeliberativeLoop(fake, 5.0)
    assert asyncio.run(loop.run_once()) == 1
    assert fake.force_flags == [True]


def test_run_stops_on_event() -> None:
    fake = FakeAgent(calls_before_stop=2)
    event = asyncio.Event()
    fake.stop_event = event
    loop = loop_module.DeliberativeLoop(fake, 0.1)

    async def scenario():
        await asyncio.wait_for(loop.run(event), timeout=2.0)

    asyncio.run(scenario())
    assert fake.calls == 2


def test_loop_survives_tick_exception() -> None:
    fake = FakeAgent(calls_before_stop=1, fail_first=True)
    event = asyncio.Event()
    fake.stop_event = event
    loop = loop_module.DeliberativeLoop(fake, 0.1)

    async def scenario():
        await asyncio.wait_for(loop.run(event), timeout=2.0)

    asyncio.run(scenario())
    # 第一次抛异常后循环继续，第二次成功并在 tick 内设置停止事件。
    assert fake.calls == 2
