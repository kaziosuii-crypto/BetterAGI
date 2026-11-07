# -*- coding: utf-8 -*-
'''慢系统（agent/slow_planner.py）：三段解析、400 字退回、预算硬截断、thinking 分级。'''
from __future__ import annotations

import time

import pytest

import slow_planner


GOOD = '''## 这一轮做什么
从篝火往东走到村口，找到穿红衣的 NPC 跟她说话。
## 就算成了
画面出现对话框，NPC 开始说话。
## 别做
别去打路边的大树守卫。'''


def test_parse_three_sections() -> None:
    plan = slow_planner.parse_slow_plan(GOOD)
    assert plan.ok is True
    assert plan.goal.startswith('从篝火往东走到村口')
    assert '对话框' in plan.success
    assert '大树守卫' in plan.avoid
    assert plan.needs_rewrite is False
    assert plan.warnings == []


def test_missing_done_section_warns_not_fabricated() -> None:
    plan = slow_planner.parse_slow_plan('## 这一轮做什么' + chr(10) + '往前走走')
    assert plan.success == ''  # 不编判据
    assert any('就算成了' in w for w in plan.warnings)


def test_over_400_chars_needs_rewrite() -> None:
    long_goal = '一直往前走' * 100
    plan = slow_planner.parse_slow_plan('## 这一轮做什么' + chr(10) + long_goal)
    assert plan.needs_rewrite is True
    assert any('退回重写' in w for w in plan.warnings)


def test_empty_text_is_not_ok() -> None:
    plan = slow_planner.parse_slow_plan('   ')
    assert plan.ok is False
    assert plan.warnings


def test_untitled_text_treated_as_goal_with_warning() -> None:
    plan = slow_planner.parse_slow_plan('去村口找红衣 NPC')
    assert plan.goal == '去村口找红衣 NPC'
    assert any('缺少标题' in w for w in plan.warnings)


def test_build_messages_has_system_and_report() -> None:
    messages = slow_planner.build_slow_messages('走到门口被挡住了', memory_hits=['上次在这里卡过'],
                                                danmaku=['主播往前'], blocked_count=2)
    assert messages[0]['role'] == 'system'
    assert '这一轮做什么' in messages[0]['content']
    body = messages[1]['content']
    assert '走到门口被挡住了' in body
    assert '上次在这里卡过' in body
    assert '主播往前' in body
    assert '卡了 2 次' in body
    assert '（无）' in body  # 空的历史项不许留白让模型脑补


def test_thinking_only_after_threshold() -> None:
    assert slow_planner.thinking_for(0) is False
    assert slow_planner.thinking_for(2) is False
    assert slow_planner.thinking_for(3) is True


def test_budget_timeout_raises() -> None:
    def slow_client(messages, thinking=False):
        time.sleep(1.0)
        return GOOD

    planner = slow_planner.SlowPlanner(slow_client, budget_ms=60)
    started = time.monotonic()
    with pytest.raises(slow_planner.SlowPlannerTimeout) as info:
        planner.plan('刚刚走到村口')
    assert time.monotonic() - started < 0.9
    assert '预算 60ms' in str(info.value)
    assert planner.last_error == 'slow_timeout'


def test_client_exception_wrapped() -> None:
    def broken(messages, thinking=False):
        raise RuntimeError('额度用完了')

    planner = slow_planner.SlowPlanner(broken)
    with pytest.raises(slow_planner.SlowPlannerError) as info:
        planner.plan('报告')
    assert '额度用完了' in str(info.value)


def test_object_client_and_thinking_budget() -> None:
    calls = []

    class FakeClient:
        def complete(self, messages, thinking=False):
            calls.append(thinking)
            return GOOD

    planner = slow_planner.SlowPlanner(FakeClient(), budget_ms=200, thinking_budget_ms=5000)
    plan = planner.plan('报告', blocked_count=5)
    assert plan.ok is True
    assert calls == [True]
    assert any('thinking' in w for w in plan.warnings)


def test_build_without_client_is_honest() -> None:
    try:
        planner = slow_planner.build_slow_planner()
    except slow_planner.SlowPlannerError as error:
        assert '慢系统' in str(error)
    else:
        assert planner.budget_ms == slow_planner.DEFAULT_BUDGET_MS

class _FakeLithos:
    class config:
        @staticmethod
        def available():
            return True

    def describe(self):
        return {'available': True, 'models': {'fast': 'deepseek-flash'}}

    def complete(self, messages, *, thinking=None):
        text = chr(10).join([
            slow_planner.SECTION_GOAL, '往东走', '',
            slow_planner.SECTION_DONE, '走到村口', '',
            slow_planner.SECTION_AVOID, '别碰怪',
        ])
        return type('R', (), {'text': text})()


def test_lithos_slow_client_wraps_reply_text() -> None:
    client = slow_planner.LithosSlowClient(_FakeLithos())
    text = client([{'role': 'user', 'content': 'x'}], thinking=True)
    assert slow_planner.SECTION_GOAL in text
    assert client.available() is True
    assert client.describe()['models']['fast'] == 'deepseek-flash'


def test_build_client_reports_missing_key(monkeypatch) -> None:
    import lithos

    class NoKey:
        @staticmethod
        def available():
            return False

    monkeypatch.setattr(lithos.LithosConfig, 'from_env', classmethod(lambda cls, env=None: NoKey()))
    with pytest.raises(slow_planner.SlowPlannerError) as excinfo:
        slow_planner.build_client()
    assert 'key' in str(excinfo.value).lower()


def test_build_slow_planner_uses_build_client(monkeypatch) -> None:
    fake = slow_planner.LithosSlowClient(_FakeLithos())
    monkeypatch.setattr(slow_planner, 'build_client', lambda client=None, config=None: fake)
    planner = slow_planner.build_slow_planner()
    assert planner.client is fake
