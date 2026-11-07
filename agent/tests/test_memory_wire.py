"""记忆接线纯函数测试：检索注入、写回、格式化。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import Episode, LocalJsonMemory  # noqa: E402
from memory_wire import (  # noqa: E402
    episode_from_tool,
    format_recall,
    query_terms,
    recall_for,
    writeback,
)


@pytest.fixture()
def mem(tmp_path: Path) -> LocalJsonMemory:
    return LocalJsonMemory(tmp_path / "mem", "undertale", expected_group="undertale")


def test_query_terms_splits_chinese_without_spaces() -> None:
    terms = query_terms("从清档开始跑进游戏：先按确认键")
    assert terms, "中文必须切出词，否则关键词检索全落空"
    assert all(len(t) >= 1 for t in terms)
    assert len(terms) <= 6


def test_query_terms_empty() -> None:
    assert query_terms("") == []
    assert query_terms("   ") == []


def test_recall_returns_recent_when_query_has_no_hit(mem: LocalJsonMemory) -> None:
    mem.add_episode(Episode(text="按 [confirm] 进入了标题画面", kind="lesson", group_id="undertale"))
    assert recall_for(mem, goal="完全无关的词组", limit=3)


def test_recall_matches_chinese_note(mem: LocalJsonMemory) -> None:
    mem.add_episode(Episode(text="按 [confirm] 进入了命名界面", kind="lesson", group_id="undertale"))
    hits = recall_for(mem, goal="命名", limit=3)
    assert any("命名" in h.text for h in hits)


def test_recall_dedupes_and_limits(mem: LocalJsonMemory) -> None:
    for i in range(6):
        mem.add_episode(Episode(text=f"经验 {i}", kind="lesson", group_id="undertale"))
    hits = recall_for(mem, goal="经验", limit=3)
    assert len(hits) <= 3
    assert len({h.text for h in hits}) == len(hits)


def test_recall_none_memory_is_empty() -> None:
    assert recall_for(None) == []


def test_format_recall_empty() -> None:
    assert format_recall([]) == ""


def test_format_recall_respects_budget() -> None:
    eps = [Episode(text="很长的笔记" * 40, kind="lesson", group_id="undertale") for _ in range(5)]
    text = format_recall(eps, max_chars=200)
    assert "笔记" in text
    assert len(text) <= 260  # 头部说明 + 预算内的行


def test_episode_from_tool_ignores_look(mem: LocalJsonMemory) -> None:
    assert episode_from_tool("look", {}, "ok", True, group_id="undertale") is None


def test_episode_from_tool_act_records_keys() -> None:
    ep = episode_from_tool("act", {"keys": ["confirm"]}, "pressed confirm->z", True, group_id="undertale")
    assert ep is not None
    assert ep.kind == "lesson"
    assert "confirm" in ep.text
    assert "成功" in ep.text
    assert ep.group_id == "undertale"


def test_episode_from_tool_failure_lowers_confidence() -> None:
    ok_ep = episode_from_tool("act", {"keys": ["x"]}, "ok", True, group_id="undertale", confidence=0.8)
    bad_ep = episode_from_tool("act", {"keys": ["x"]}, "boom", False, group_id="undertale", confidence=0.8)
    assert ok_ep and bad_ep
    assert bad_ep.confidence < ok_ep.confidence
    assert "失败" in bad_ep.text


def test_episode_from_tool_learn_key_is_keymap() -> None:
    ep = episode_from_tool(
        "learn_key", {"name": "confirm", "physical": "z"}, "learned", True, group_id="undertale"
    )
    assert ep is not None and ep.kind == "keymap"


def test_episode_from_tool_requires_group() -> None:
    assert episode_from_tool("act", {"keys": ["z"]}, "ok", True, group_id="") is None


def test_writeback_roundtrip(mem: LocalJsonMemory) -> None:
    ep = episode_from_tool("act", {"keys": ["confirm"]}, "pressed", True, group_id=mem.group_id)
    assert writeback(mem, ep) is True
    assert mem.count("lesson") == 1


def test_writeback_swallows_errors() -> None:
    class Broken:
        group_id = "undertale"

        def add_episode(self, episode):
            raise RuntimeError("neo4j down")

    ep = Episode(text="x", kind="lesson", group_id="undertale")
    assert writeback(Broken(), ep) is False
    assert writeback(None, ep) is False
