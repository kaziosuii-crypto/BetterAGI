# -*- coding: utf-8 -*-
"""记忆闭环（agent/orchestrator.py）：每轮检索注入 + 每轮回执落账。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import orchestrator as orch_mod
from memory import Episode

#: 回执的见闻段有硬下限（120 字，02 §3.3）：测试也照真实口径写，别用缩写蒙过去。
OBS = (
    "我站在电梯口，右手边墙上有一排按钮，最上面那个亮着，按下去以后门开了，"
    "走廊尽头有两只怪在来回走，地面是湿的、反光很强，看不清门后是什么，"
    "我退回楼梯间，那个台阶上去有一扇关着的门，门边写着 B2。楼梯间的灯泡在闪，数了数一共七级台阶，最后一级踩上去会响。"
)


class FakeMemory:
    def __init__(self) -> None:
        self.added = []
        self.queries = []

    def search(self, query, limit=5, kinds=(), layers=None, include_spoiler=False):
        self.queries.append(query)
        return [Episode(text="上次在电梯口被三只怪围住，退回去绕楼梯", kind="story", layer="episodic")]

    def add_episode(self, episode):
        self.added.append(episode)
        return episode


class FakePlanner:
    def __init__(self) -> None:
        self.calls = []

    def plan(self, report_summary, memory_hits=None, danmaku=None, history=None, constraints=None,
             blocked_count=0, last_plan=None):
        self.calls.append({"summary": report_summary, "memory_hits": memory_hits})
        return type("Plan", (), {"goal": "走到电梯口并按下按钮", "success": "电梯门开了",
                                 "avoid": "别再被围住", "warnings": [], "needs_rewrite": False})()


def _build(tmp_path, *, memory=None, planner=None):
    root = tmp_path / "run" / "s1"
    root.mkdir(parents=True, exist_ok=True)
    return orch_mod.Orchestrator(
        root, "s1", game="hollow", save_id="slot1",
        planner=planner or FakePlanner(), memory=memory,
        incident_root=tmp_path / "run" / "incidents",
    ), root


def test_plan_injects_recalled_memory(tmp_path) -> None:
    memory = FakeMemory()
    planner = FakePlanner()
    node, _ = _build(tmp_path, memory=memory, planner=planner)
    node.plan("上一轮我在电梯口被围住了")
    assert memory.queries, "检索必须真的发生"
    hits = planner.calls[0]["memory_hits"]
    assert hits and "绕楼梯" in hits[0]


def test_explicit_memory_hits_are_not_overridden(tmp_path) -> None:
    memory = FakeMemory()
    planner = FakePlanner()
    node, _ = _build(tmp_path, memory=memory, planner=planner)
    node.plan("回执", memory_hits=["调用方给的那条"])
    assert planner.calls[0]["memory_hits"] == ["调用方给的那条"]
    assert memory.queries == []


def test_no_memory_still_plans(tmp_path) -> None:
    planner = FakePlanner()
    node, _ = _build(tmp_path, memory=None, planner=planner)
    node.plan("回执")
    assert planner.calls[0]["memory_hits"] == []


def test_recall_reports_error_instead_of_raising(tmp_path) -> None:
    class BadMemory(FakeMemory):
        def search(self, *args, **kwargs):
            raise RuntimeError("后端连不上")

    node, _ = _build(tmp_path, memory=BadMemory())
    block = node.recall("随便")
    assert block["text"] == ""
    assert any("记忆检索失败" in item for item in block["warnings"])


def test_handoff_writes_episode_into_memory(tmp_path) -> None:
    memory = FakeMemory()
    node, root = _build(tmp_path, memory=memory)
    node.state = orch_mod.STATE_EXECUTING  # 单元测试直接摆好状态：handoff 只允许从执行中来
    result = {"intent_id": "s1-001", "result": "reached", "ended": "goal_reached", "decide_ms": [180]}
    payload = node.handoff(
        result,
        conclusion="电梯门开了，我进了下一层",
        observations=OBS,
    )
    assert payload["memory"]["stored"] is True
    assert len(memory.added) == 1
    episode = memory.added[0]
    assert "电梯门开了" in episode.text
    assert episode.layer == "episodic"
    assert episode.meta["intent_id"] == "s1-001"
    assert episode.meta["result"] == "reached"
    assert str(episode.group_id) and episode.group_ids == [episode.group_id]


def test_handoff_without_memory_says_so_instead_of_pretending(tmp_path) -> None:
    node, _ = _build(tmp_path, memory=None)
    node.state = orch_mod.STATE_EXECUTING  # 单元测试直接摆好状态：handoff 只允许从执行中来
    result = {"intent_id": "s1-002", "result": "blocked", "ended": "budget"}
    payload = node.handoff(result, conclusion="我什么也没做成，卡在门口了",
                           observations=OBS)
    assert payload["memory"]["stored"] is False
    assert "没接记忆库" in payload["memory"]["reason"]
    assert any("这一轮没写进记忆" in item for item in payload.get("warnings") or [])


def test_remember_rejects_empty_text(tmp_path) -> None:
    node, _ = _build(tmp_path, memory=FakeMemory())
    out = node.remember(conclusion="", observations="")
    assert out["stored"] is False
    assert "空" in out["reason"]
