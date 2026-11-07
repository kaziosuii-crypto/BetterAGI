# -*- coding: utf-8 -*-
"""便利贴 → 手模型三槽位 system（agent/motor_wire.py）。

口径（2026-11-04 用户拍板）：手模型永远只吃便利贴，两块空着也照发空槽位，
旧的 build_system_prompt 只在 BETTERAGI_MOTOR_LEGACY_PROMPT=1 或没有游戏名时兜底。
"""
from __future__ import annotations

import pytest

import motor_wire
import prompt_builder
import sticky


RULE = "- 走到发光的平台按 F 才算交互过"
KEYS = "- 移动 W A S D"


def _write(root, game="hollow", save="slot1", **kwargs):
    return sticky.write_sticky(game, save, root=root, **kwargs)


def test_sticky_goes_into_three_slots(tmp_path) -> None:
    _write(tmp_path, key_binding=KEYS, game_rules=RULE)
    out = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path)
    assert out["source"] == motor_wire.SOURCE_STICKY
    assert RULE in out["text"]
    assert KEYS in out["text"]
    # 三槽位必须真的被填掉：留花括号模型会当字面文本抄进动作。
    assert "{GAME_RULES}" not in out["text"]
    assert "{KEY_BINDINGS}" not in out["text"]
    assert "hollow" in out["text"]
    assert out["key_binding_chars"] == len(KEYS)
    assert out["game_rules_chars"] == len(RULE)
    assert out["blocks_empty"] is False


def test_mouse_line_is_not_duplicated(tmp_path) -> None:
    keys = KEYS + "\n" + prompt_builder.MOUSE_BINDING_LINE
    _write(tmp_path, key_binding=keys, game_rules=RULE)
    text = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path)["text"]
    assert text.count("Mouse X Y") == 1


def test_empty_blocks_still_use_sticky_and_never_legacy(tmp_path) -> None:
    """两块都空 = 还没学会：照样发模板骨架，绝不替它填旧提示词。"""
    _write(tmp_path, key_binding="", game_rules="")

    def legacy() -> str:
        raise AssertionError("便利贴路径可用时不准回退旧提示词")

    out = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path,
                                       legacy=legacy)
    assert out["source"] == motor_wire.SOURCE_STICKY
    assert out["blocks_empty"] is True
    assert "You are a gaming expert" in out["text"]
    assert "{GAME_RULES}" not in out["text"]
    assert any("两块都还空着" in item for item in out["warnings"])


def test_missing_file_is_sticky_with_empty_blocks(tmp_path) -> None:
    out = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path)
    assert out["source"] == motor_wire.SOURCE_STICKY
    assert out["blocks_empty"] is True
    assert out["key_binding_chars"] == 0


def test_missing_save_id_reads_default_save(tmp_path) -> None:
    _write(tmp_path, save="default", key_binding=KEYS, game_rules=RULE)
    out = motor_wire.build_motor_system(game="hollow", save_id="", root=tmp_path, legacy="OLD")
    assert out["source"] == motor_wire.SOURCE_STICKY
    assert RULE in out["text"]


def test_read_error_keeps_sticky_contract_with_warning(tmp_path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("文件被写坏了")

    monkeypatch.setattr(sticky, "read_sticky", boom)
    out = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path, legacy="OLD")
    assert out["source"] == motor_wire.SOURCE_STICKY
    assert out["blocks_empty"] is True
    assert any("便利贴读取失败" in item for item in out["warnings"])


def test_assemble_error_does_not_silently_use_legacy(tmp_path, monkeypatch) -> None:
    _write(tmp_path, key_binding=KEYS, game_rules=RULE)

    def boom(*args, **kwargs):
        raise ValueError("模板坏了")

    monkeypatch.setattr(prompt_builder, "assemble_motor_system", boom)
    monkeypatch.delenv(motor_wire.LEGACY_ENV, raising=False)
    out = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path, legacy="OLD")
    assert out["source"] == motor_wire.SOURCE_EMPTY
    assert out["text"] == ""
    assert any("三槽位拼装失败" in item for item in out["warnings"])


def test_assemble_error_with_env_uses_legacy(tmp_path, monkeypatch) -> None:
    _write(tmp_path, key_binding=KEYS, game_rules=RULE)

    def boom(*args, **kwargs):
        raise ValueError("模板坏了")

    monkeypatch.setattr(prompt_builder, "assemble_motor_system", boom)
    monkeypatch.setenv(motor_wire.LEGACY_ENV, "1")
    out = motor_wire.build_motor_system(game="hollow", save_id="slot1", root=tmp_path, legacy="OLD")
    assert out["source"] == motor_wire.SOURCE_LEGACY
    assert out["text"] == "OLD"


def test_no_game_name_uses_legacy(tmp_path) -> None:
    out = motor_wire.build_motor_system(game="", save_id="slot1", root=tmp_path, legacy="OLD")
    assert out["source"] == motor_wire.SOURCE_LEGACY
    assert out["text"] == "OLD"
