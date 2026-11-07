#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Session Recap 生成与存储测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_recap as recap_module  # noqa: E402


def test_build_recap_is_deterministic_and_has_next_session_fields() -> None:
    episodes = [
        {
            "time_ms": 3000,
            "what_happened": "安柏带我们见了琴。",
            "characters": ["安柏", "琴"],
            "unresolved_questions": ["深渊教团在哪？"],
        },
        {
            "time_ms": 2000,
            "what_happened": "在城门口看到一张告示。",
            "characters": ["派蒙"],
            "unresolved_questions": [],
        },
    ]
    facts = [{"learned_ms": 3100, "proposition": "屏幕OCR（未校对）：骑士团正在开会"}]
    working = {
        "location": "骑士团总部",
        "current_activity": "跟安柏去见琴",
        "open_questions": ["深渊教团在哪？"],
        "emotion": {"curious": 0.7},
        "short_plan": ["问琴任务"],
    }
    state = {
        "last_location": "骑士团总部",
        "current_quest": "见琴",
        "current_intention": "跟安柏去见琴",
        "last_internal_thought": "她好像很信任琴。",
    }
    first = recap_module.build_recap(
        session_id="s-1", state=state, working=working,
        episodes=episodes, facts=facts, now_ms=5000,
    )
    second = recap_module.build_recap(
        session_id="s-1", state=state, working=working,
        episodes=episodes, facts=facts, now_ms=5000,
    )
    assert first == second
    assert first["what_happened"][0] == "安柏带我们见了琴。"
    assert first["people"] == ["安柏", "琴", "派蒙"]
    assert first["unresolved"] == ["深渊教团在哪？"]
    assert first["episode_count"] == 2
    text = recap_module.recap_prompt_text(first)
    assert "上次停在：骑士团总部" in text
    assert "安柏带我们见了琴" in text
    assert "上次最后在想" in text
    assert "0.7" not in text


def test_recap_store_corrupt_and_future(tmp_path: Path) -> None:
    store = recap_module.RecapStore(tmp_path / "recap.json", clock=lambda: 1000)
    store.save({"session_id": "s", "created_ms": 1})
    assert store.load()["session_id"] == "s"

    path = tmp_path / "recap.json"
    path.write_text("{broken", encoding="utf-8")
    assert store.load() is None
    assert store.recovered_corrupt is not None

    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(recap_module.RecapSchemaError):
        recap_module.RecapStore(path).load()
