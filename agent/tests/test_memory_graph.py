"""长期记忆的四条真实约束：去重、冲突、关联、隔离（FR-4 / FR-7）。

这些行为不是"锦上添花"——少了任何一条，记忆就会退化成越写越乱的流水账：
重复学习把文件撑爆、冲突被静默覆盖成错事实、跨游戏串味。
"""
from __future__ import annotations

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import pytest  # noqa: E402

import memory as M  # noqa: E402


def _mem(tmp_path, group="undertale"):
    return M.LocalJsonMemory(tmp_path / "mem", group)


def test_repeated_episode_is_merged_not_appended(tmp_path):
    mem = _mem(tmp_path)
    for _ in range(3):
        mem.add_episode(M.Episode(text="Z 确认", kind="rule", confidence=0.5))
    assert mem.count() == 1
    stored = mem.load()[0]
    assert stored.meta["supports"] == 3
    assert stored.meta["deduped"] is True


def test_dedupe_keeps_highest_confidence_and_best_source(tmp_path):
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="Z 确认", kind="rule", confidence=0.4, source="slow"))
    mem.add_episode(M.Episode(text="z  确认", kind="rule", confidence=0.9, source="manual"))
    stored = mem.load()[0]
    assert stored.confidence == pytest.approx(0.9)
    assert stored.source == "manual"


def test_keymap_conflict_is_marked_not_overwritten(tmp_path):
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="confirm -> z", kind="keymap", confidence=0.8,
                              meta={"key": "confirm", "effect": "z"}))
    fresh = mem.add_episode(M.Episode(text="confirm -> enter", kind="keymap", confidence=0.5,
                                      meta={"key": "confirm", "effect": "enter"}))
    # 两条都在：谁对由慢系统复核，不能靠"后写的赢"
    assert mem.count() == 2
    assert fresh.meta["conflict"]["existing_effect"] == "z"
    conflicts = mem.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["key"] == "confirm"
    # 旧条目也被打上标记，避免它继续以高置信度混进提示词
    old = [e for e in mem.load() if e.meta.get("effect") == "z"][0]
    assert old.meta.get("conflict")


def test_conflict_lowers_search_rank(tmp_path):
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="confirm -> z", kind="keymap", meta={"key": "confirm", "effect": "z"}, confidence=1.0))
    mem.add_episode(M.Episode(text="confirm -> enter", kind="keymap", meta={"key": "confirm", "effect": "enter"}, confidence=1.0))
    hits = mem.search("confirm", limit=5)
    assert len(hits) == 2  # 冲突不隐藏，慢系统必须看得见矛盾
    assert all(e.meta.get("conflict") for e in hits)


def test_neighbours_share_entities(tmp_path):
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="Toriel 在废墟入口等你", kind="place"))
    mem.add_episode(M.Episode(text="Toriel 会给你 pie", kind="story"))
    mem.add_episode(M.Episode(text="Flowey 出现在花田", kind="story"))
    base = [e for e in mem.load() if "Toriel" in e.text][0]
    near = mem.neighbours(base)
    assert [e.text for e in near] == ["Toriel 会给你 pie"]


def test_group_isolation_between_two_games(tmp_path):
    ut = M.LocalJsonMemory(tmp_path / "mem", "undertale")
    hk = M.LocalJsonMemory(tmp_path / "mem", "hollow_knight")
    ut.add_episode(M.Episode(text="Z 确认", kind="rule"))
    hk.add_episode(M.Episode(text="A 跳跃", kind="rule"))
    assert [e.text for e in ut.load()] == ["Z 确认"]
    assert [e.text for e in hk.load()] == ["A 跳跃"]
    # 拿 A 游戏的身份写 B 游戏的分区必须直接报错，不能"顺手"串味
    with pytest.raises(M.MemoryError_):
        ut.add_episode(M.Episode(text="串味", group_id="hollow_knight"))


def test_unknown_kind_rejected(tmp_path):
    mem = _mem(tmp_path)
    with pytest.raises(M.MemoryError_):
        mem.add_episode(M.Episode(text="x", kind="nonsense"))


def test_search_prefers_high_confidence_and_fresh(tmp_path):
    mem = _mem(tmp_path)
    mem.add_episode(M.Episode(text="boss 弱点 火焰", kind="rule", confidence=0.2, ts=0.0))
    mem.add_episode(M.Episode(text="boss 弱点 冰霜", kind="rule", confidence=0.95))
    top = mem.search("boss 弱点", limit=2)
    assert "冰霜" in top[0].text