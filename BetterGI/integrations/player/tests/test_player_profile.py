#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家档案与提示词构造的单元测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_context as context  # noqa: E402
import player_profile as profile_module  # noqa: E402

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "lumi.json"


def minimal_profile() -> dict:
    return {
        "schema_version": 1,
        "player_id": "test-player",
        "identity": {"name": "测试玩家"},
        "personality": {"traits": ["好奇"]},
        "values": ["先观察"],
        "speech_style": {"tone": "短句"},
    }


def test_default_profile_loads_and_summary_is_stable() -> None:
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    assert profile.player_id == "lumi"
    assert profile.display_name == "小光 Lumi"
    assert profile.boundaries["no_spoilers"] is True

    first = profile.stable_summary()
    second = profile.stable_summary()
    assert first == second
    assert "小光 Lumi" in first
    assert "不剧透" in first
    # 风险值等内部数值不直接进入提示词。
    assert "0.45" not in first
    # 摘要不应包含时间戳等每轮变化的内容。
    assert "2026-" not in first


def test_minimal_profile_gets_defaults() -> None:
    profile = profile_module.profile_from_dict(minimal_profile())
    assert profile.identity["pronouns"] == "她"
    assert profile.play_style["pace"] == "exploratory"
    assert profile.speech_style["language"] == "简体中文"
    assert profile.speech_style["silence_bias"] == "medium"
    assert profile.boundaries == {
        "no_spoilers": True,
        "no_meta_ai_talk": True,
        "no_game_memory_access": True,
    }


def test_spoiler_ban_cannot_be_disabled() -> None:
    data = minimal_profile()
    data["boundaries"] = {"no_spoilers": False}
    with pytest.raises(profile_module.ProfileError, match="no_spoilers"):
        profile_module.profile_from_dict(data)


def test_secret_fields_are_rejected_recursively() -> None:
    data = minimal_profile()
    data["preferences"] = {"api_key": "sk-should-not-be-here"}
    with pytest.raises(profile_module.ProfileError, match="令牌"):
        profile_module.profile_from_dict(data)

    data = minimal_profile()
    data["identity"]["access_token"] = "abc"
    with pytest.raises(profile_module.ProfileError, match="令牌"):
        profile_module.profile_from_dict(data)


@pytest.mark.parametrize(
    "key",
    [
        "openai_api_key",
        "aws_secret_access_key",
        "access_key_id",
        "client_secret_key",
        "private_key_id",
    ],
)
def test_secret_key_suffix_variants_are_rejected(key: str) -> None:
    data = minimal_profile()
    data["preferences"] = {key: "sk-should-not-be-here"}
    with pytest.raises(profile_module.ProfileError, match="令牌"):
        profile_module.profile_from_dict(data)


@pytest.mark.parametrize(
    "key",
    ["accessToken", "refreshToken", "clientSecret", "openaiApiKey"],
)
def test_camel_case_secret_keys_are_rejected(key: str) -> None:
    data = minimal_profile()
    data["preferences"] = {key: "sk-should-not-be-here"}
    with pytest.raises(profile_module.ProfileError, match="令牌"):
        profile_module.profile_from_dict(data)


def test_camel_case_world_truth_key_is_rejected() -> None:
    data = minimal_profile()
    data["self_history"] = {"summary": "ok", "groundTruth": "某某是反派"}
    with pytest.raises(profile_module.ProfileError, match="世界真相"):
        profile_module.profile_from_dict(data)


def test_world_truth_fields_are_rejected() -> None:
    data = minimal_profile()
    data["self_history"] = {"summary": "ok", "ground_truth": "某某是反派"}
    with pytest.raises(profile_module.ProfileError, match="世界真相"):
        profile_module.profile_from_dict(data)


@pytest.mark.parametrize(
    "mutation, match",
    [
        (lambda data: data.pop("schema_version"), "schema_version"),
        (lambda data: data.update(schema_version=99), "高于当前"),
        (lambda data: data.pop("player_id"), "player_id"),
        (lambda data: data.update(player_id="bad id!"), "player_id"),
        (lambda data: data.update(identity={}), "identity.name"),
        (lambda data: data.update(personality={"traits": []}), "traits"),
        (lambda data: data.update(values=[]), "values"),
        (lambda data: data.update(speech_style={}), "tone"),
    ],
)
def test_profile_validation_errors(mutation, match) -> None:
    data = minimal_profile()
    mutation(data)
    with pytest.raises(profile_module.ProfileError, match=match):
        profile_module.profile_from_dict(data)


def test_load_profile_reports_missing_and_bad_json(tmp_path: Path) -> None:
    with pytest.raises(profile_module.ProfileError, match="不存在"):
        profile_module.load_profile(tmp_path / "missing.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(profile_module.ProfileError, match="合法 JSON"):
        profile_module.load_profile(broken)


def test_profile_read_tolerates_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text("﻿" + json.dumps(minimal_profile()), encoding="utf-8")
    profile = profile_module.load_profile(path)
    assert profile.player_id == "test-player"


def test_seed_default_profile_only_creates_once(tmp_path: Path) -> None:
    target = tmp_path / "profile" / "profile.json"
    assert profile_module.seed_default_profile(target) is True
    assert target.exists()
    assert profile_module.load_profile(target).player_id == "lumi"

    text_before = target.read_text(encoding="utf-8")
    assert profile_module.seed_default_profile(target) is False
    assert target.read_text(encoding="utf-8") == text_before


def test_seed_rejects_broken_template(tmp_path: Path) -> None:
    template = tmp_path / "template.json"
    template.write_text("{broken", encoding="utf-8")
    with pytest.raises(profile_module.ProfileError, match="合法 JSON"):
        profile_module.seed_default_profile(tmp_path / "out.json", template=template)


def test_build_shadow_prompt_is_deterministic_and_safe() -> None:
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    state = {
        "last_internal_thought": "刚才那个箱子旁边好像有动静",
        "current_intention": "先绕到坡上看一眼",
        "last_location": "蒙德城外",
        "current_quest": "看看附近有什么",
        "things_i_want_to_check_next": ["坡上的声音"],
    }
    snapshot = {
        "frame": {
            "seq": 12,
            "width": 1280,
            "height": 720,
            "ui_category": "world",
        },
        "guidance": "往坡上走",
        "guidance_source": "director",
        "commentary": [{"kind": "reaction", "text": "起风了"}],
        "action_state": {"keys": ["W"], "queueDepth": 1, "starved": False},
        "recent_actions": [{"thought": "向前", "keys": ["W"], "status": "applied"}],
        "live_running": True,
    }

    first = context.build_shadow_prompt(profile, state, snapshot)
    second = context.build_shadow_prompt(profile, state, snapshot)
    assert first == second
    assert "小光 Lumi" in first["system"]
    assert "shadow" in first["system"]
    assert "往坡上走" in first["user"]
    assert "刚才那个箱子旁边好像有动静" in first["user"]
    assert "坡上的声音" in first["user"]
    assert "起风了" in first["user"]
    assert "proposed_actions" in first["user"]
    # 不注入图片、路径或环境变量。
    assert "jpeg" not in first["user"].lower()
    assert "E:\\" not in first["user"]
    assert "sk-" not in first["system"]


def test_prompt_omits_empty_resume_block() -> None:
    profile = profile_module.load_profile(DEFAULT_PROFILE)
    text = context.build_shadow_prompt(profile, {}, {"frame": {"seq": 1}})
    assert "你上一次留下的状态" not in text["user"]
