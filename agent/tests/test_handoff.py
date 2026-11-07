#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HandoffReport（交棒载荷）与 11 个慢系统工具路由的离线单测。

覆盖 2026-11-04 改造的两件事：
1. 交棒不再是 lumine 的自由文本，而是一条要过校验、能落盘、能进事件流的数据；
2. dispatch() 认全 11 个慢系统工具名，tool_specs() 给出标准 JSON Schema。
失败必须变成 ok=False + 可读原因，不许抛栈、不许编内容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent as A  # noqa: E402
import handoff as H  # noqa: E402

GOOD_CONCLUSION = "拿到钥匙了，NPC 说晚上八点去塔楼见面"
GOOD_OBSERVATIONS = (
    "## 结论\n拿到钥匙后塔楼的门开了，守卫说晚上八点在塔楼见。\n\n"
    "## 看到了什么\n画面右下角出现钥匙图标，对话框里 NPC 的原话是「塔楼见」，"
    "背景音乐从紧张变成舒缓，说明这一段任务已经推进；道具栏第二格多了一把铜钥匙。\n\n"
    "## 我现在在哪\n还在教堂门口，人物可自由移动，面前是通往塔楼的石阶，存档点就在旁边。"
)
SLOW_NAMES = (
    "run_script",
    "read_sticky",
    "write_sticky",
    "search_memory",
    "write_memory",
    "read_danmaku",
    "get_last_episode",
    "set_hook",
    "set_plan",
)


def make_report(**overrides):
    """构造一份默认合法的交棒报告，便于单点覆盖某个字段。"""

    data = {
        "intent_id": "01JTEST0001",
        "result": "reached",
        "conclusion": GOOD_CONCLUSION,
        "observations": GOOD_OBSERVATIONS,
    }
    data.update(overrides)
    return H.HandoffReport(**data)


def make_tools(tmp_path):
    """不读 token、不联网的工具替身：只给测试真正用到的属性。"""

    tools = A.BetterGITools.__new__(A.BetterGITools)
    tools.session = A.Session(root=tmp_path / "run", session_id="s1")
    tools.profile = SimpleNamespace(game_id="undertale", display_name="Undertale")
    tools.keymap = None
    tools.known_keys = lambda: {}
    tools.stuck_count = 0
    tools.dispatch_hook = None
    tools.mailbox = None
    tools.current_intent_id = ""
    tools._intent_started_ms = None
    tools.current_save_id = ""
    tools._memory_backend = None
    return tools


# ---- 校验：长度越界必须抛 ValueError，且说清实际长度 --------------------
def test_validate_rejects_short_conclusion():
    report = make_report(conclusion="太短了")
    with pytest.raises(ValueError) as info:
        report.validate()
    message = str(info.value)
    assert "conclusion" in message
    assert "实际 3 字" in message, message


def test_validate_rejects_long_conclusion():
    report = make_report(conclusion="字" * 61)
    with pytest.raises(ValueError) as info:
        report.validate()
    message = str(info.value)
    assert "conclusion" in message
    assert "实际 61 字" in message, message


def test_validate_rejects_short_observations():
    report = make_report(observations="看到钥匙了")
    with pytest.raises(ValueError) as info:
        report.validate()
    message = str(info.value)
    assert "observations" in message
    assert "实际 5 字" in message, message


def test_validate_rejects_unknown_result():
    with pytest.raises(ValueError) as info:
        make_report(result="done").validate()
    assert "result" in str(info.value)


# ---- 真交棒：落盘 + 事件流 ---------------------------------------------
def test_dispatch_read_sticky_without_args_is_explicit_error(tmp_path):
    tools = make_tools(tmp_path)
    result = tools.dispatch("read_sticky", {})
    assert result.ok is False
    assert "save_id" in result.text


def test_dispatch_read_sticky_reports_missing_module(tmp_path, monkeypatch):
    tools = make_tools(tmp_path)
    # 让 import sticky 失败：None 占位会让 __import__ 抛 ImportError。
    # agent 用脚本式导入时相对分支查的是 ".sticky"，两个键都要堵住。
    monkeypatch.setitem(sys.modules, "sticky", None)
    monkeypatch.setitem(sys.modules, ".sticky", None)
    result = tools.dispatch("read_sticky", {"save_id": "slot1", "game": "undertale"})
    assert result.ok is False
    assert "sticky" in result.text
    assert "不可用" in result.text


def test_dispatch_rejects_typo_tool_name(tmp_path):
    tools = make_tools(tmp_path)
    with pytest.raises(ValueError) as info:
        tools.dispatch("request_handof", {})
    assert "未知工具" in str(info.value)


# ---- 工具面：11 个名字齐全，parameters 是标准 JSON Schema ---------------
def test_tool_specs_expose_ten_slow_tools(tmp_path):
    tools = make_tools(tmp_path)
    specs = tools.tool_specs()
    names = [spec["function"]["name"] for spec in specs]

    # 原有 6 个顺序不变，后面追加 10 个慢系统工具（交棒工具已删，加了 run_script）
    assert names[:6] == [
        "look",
        "lumine",
        "learn_key",
        "learn_rule",
        "restart_fast",
        "end_test",
    ]
    assert len(names) == 16, names
    assert set(names[6:]) == set(SLOW_NAMES)

    for spec in specs:
        function = spec["function"]
        if function["name"] not in SLOW_NAMES:
            continue
        assert function["description"].strip(), function["name"]
        params = function["parameters"]
        assert params["type"] == "object", function["name"]
        assert isinstance(params["properties"], dict), function["name"]
        assert params["additionalProperties"] is False, function["name"]
        assert isinstance(params["required"], list), function["name"]
        for key in params["required"]:
            assert key in params["properties"], (function["name"], key)
        for key, prop in params["properties"].items():
            assert prop.get("type") in ("string", "integer", "number", "boolean", "array", "object"), (
                function["name"],
                key,
            )
            assert prop.get("description"), (function["name"], key)
