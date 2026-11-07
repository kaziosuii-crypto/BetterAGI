# -*- coding: utf-8 -*-
'''TUI 的慢系统规划接线：规划走 complete、超时回退、三段目标进任务、卡关才开 thinking。'''
from __future__ import annotations

from types import SimpleNamespace

import slow_planner
import tui


GOOD_PLAN = """## 这一轮做什么
走到村口，和守卫说话。

## 就算成了
画面出现对话框。

## 别做
别打大树守卫。
"""


class FakeLithos:
    def __init__(self, text=GOOD_PLAN):
        self.text = text
        self.complete_calls = []
        self.stream_calls = []

    def complete(self, messages, **kwargs):
        self.complete_calls.append((messages, kwargs))
        return SimpleNamespace(text=self.text)

    def stream_complete(self, *args, **kwargs):
        self.stream_calls.append((args, kwargs))
        raise AssertionError("慢系统规划不应该走 stream_complete")


class FakeConsole:
    def __init__(self):
        self.notes = []
        self.status = {}

    def note(self, text):
        self.notes.append(text)

    def set_status(self, **fields):
        self.status.update(fields)


class StubPlanner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_bridge(fake, console=None):
    console = console or FakeConsole()
    return tui.SlowTurnPlanner(tui.build_tui_slow_planner(fake), console), console


def test_planning_uses_slow_planner_not_stream_complete():
    fake = FakeLithos()
    bridge, _ = make_bridge(fake)

    plan = bridge.plan(report_summary="上一轮被石头挡住了", danmaku=["往左走"])

    assert plan.goal == "走到村口，和守卫说话。"
    assert fake.complete_calls, "慢系统规划应该调用 lithos.complete"
    assert fake.stream_calls == []
    messages, kwargs = fake.complete_calls[0]
    assert kwargs["thinking"] is False
    assert messages[0]["role"] == "system"
    assert "上一轮被石头挡住了" in messages[1]["content"]
    assert "往左走" in messages[1]["content"]
    assert bridge.degraded is False
    assert bridge.planner.budget_ms == 30000


def test_timeout_keeps_previous_plan_and_notes():
    console = FakeConsole()
    stub = StubPlanner([
        slow_planner.SlowPlan(goal="先走到村口", success="看到门", avoid="别跑"),
        slow_planner.SlowPlannerTimeout(2100, 2000),
    ])
    bridge = tui.SlowTurnPlanner(stub, console)

    first = bridge.plan(report_summary="刚出门")
    second = bridge.plan(report_summary="还在原地")

    assert first.goal == "先走到村口"
    assert second.goal == "先走到村口"
    assert bridge.degraded is True
    assert bridge.degraded_kind == "timeout"
    assert "慢系统超时，沿用上一轮" in console.notes[-1]
    assert "沿用上一轮" in bridge.task_text(second)
    assert stub.calls[1]["last_plan"] == "先走到村口"


def test_error_without_previous_plan_uses_fallback_task():
    console = FakeConsole()
    bridge = tui.SlowTurnPlanner(StubPlanner([slow_planner.SlowPlannerError("boom")]), console)

    plan = bridge.plan()

    assert plan.goal == tui.SLOW_TASK_FALLBACK
    assert "降级" in bridge.status_text()
    assert "兜底" in bridge.status_text()
    assert "慢系统报错" in console.notes[-1]


def test_three_section_goal_is_injected_as_round_task():
    bridge, _ = make_bridge(FakeLithos())
    messages = [{"role": "system", "content": "原 system"}]

    plan = bridge.prepare(messages, report_summary="门前没人", danmaku=["去左边"])

    task = messages[-1]
    assert task["role"] == "user"
    assert task["content"].startswith("【慢系统规划｜这一轮任务】")
    assert plan.goal in task["content"]
    assert "## 就算成了" in task["content"]
    assert "画面出现对话框。" in task["content"]
    assert "## 别做" in task["content"]
    assert "别打大树守卫。" in task["content"]


def test_thinking_only_at_blocked_threshold():
    fake = FakeLithos()
    bridge, _ = make_bridge(fake)

    bridge.plan(blocked_count=0)
    bridge.plan(blocked_count=2)
    bridge.plan(blocked_count=3)

    flags = [kwargs["thinking"] for _, kwargs in fake.complete_calls]
    assert flags == [False, False, True]
    assert "同一个地方已经卡了 3 次" in fake.complete_calls[2][0][1]["content"]
    assert tui.update_blocked_count(0, []) == 0
    assert tui.update_blocked_count(2, [{"ok": True}]) == 0
    assert tui.update_blocked_count(1, [{"ok": False}]) == 2


def test_receipt_and_status_line_surface_degraded_note():
    receipt = tui.slow_receipt("我按了 W", [{"name": "look", "ok": True, "text": "看到村口"}])
    assert "上一轮我说：我按了 W" in receipt
    assert "look 成功：看到村口" in receipt

    console = tui.Console()
    console.plain = True
    console.set_status(slow="超时·沿用上一轮")
    line = console.status_line()
    assert "慢超时·沿用上一轮" in line
    assert "车道慢" in line
