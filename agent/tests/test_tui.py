#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TUI 底栏与输入框的单元测试。

只测纯逻辑（缓冲、提交、抹掉、提示符），不依赖真实终端：``Console`` 在非 tty 下
会退化成纯文本，``_draw_bottom`` 直接 no-op，因此可以在 pytest 里安全构造。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tui  # noqa: E402


def test_history_uses_resolved_game_system_prompt() -> None:
    history = tui.History()
    history.system_prompt = "undertale specific prompt"
    assert history.messages("")[0]["content"] == "undertale specific prompt"


def make_console() -> tui.Console:
    console = tui.Console()
    console.plain = True  # 不往 pytest 的 stdout 写 ANSI 控制序列
    return console


def test_typing_accumulates_and_enter_submits() -> None:
    console = make_console()
    assert console.key("h") is None
    assert console.key("i") is None
    assert console._buffer == "hi"
    assert console.key("\r") == "hi"
    assert console._buffer == ""


def test_backspace_removes_last_char() -> None:
    console = make_console()
    console.key("a")
    console.key("b")
    console.key("\b")
    assert console._buffer == "a"
    console.key("\b")
    console.key("\b")  # 退格到空也不该炸
    assert console._buffer == ""


def test_escape_clears_the_line() -> None:
    console = make_console()
    console.key("x")
    console.key("y")
    console.key("\x1b")
    assert console._buffer == ""


def test_blank_enter_submits_nothing() -> None:
    console = make_console()
    assert console.key(" ") is None
    assert console.key("\r") is None


def test_bottom_line_shows_prompt_and_buffer() -> None:
    console = make_console()
    console.key("走")
    console.key("吧")
    line = console._bottom_text()
    assert "▸" in line
    assert line.rstrip().endswith("走吧")
    # 状态与输入框在同一行：状态在前面，输入在后面。
    assert line.index("上下文") < line.index("▸")


def test_viewer_line_is_labelled_as_danmaku() -> None:
    console = make_console()
    written: list[str] = []
    console._block = lambda text: written.append(text)  # type: ignore[assignment]
    console.viewer("往左走")
    assert written and "弹幕" in written[0] and "往左走" in written[0]


def test_tool_call_assistant_always_carries_reasoning_content() -> None:
    """带 tool_calls 的 assistant 必须带 reasoning_content；没思考也要发空串。

    实测（deepseek-flash thinking 模式，2026-11-03）：缺这个字段 → HTTP 400
    错误原文 "The reasoning_content in the thinking mode must be passed back to the API."，
    空串 "" 则 200。一次空思考会把整段历史钉死成永久 400，所以这里必须补字段。
    """
    history = tui.History()
    call = {"id": "call_1", "type": "function", "function": {"name": "scan", "arguments": "{}"}}
    history.add(tui.Entry(role="assistant", content="", ts="10:00:00", tool_calls=[call], reasoning=""))
    assistant = [m for m in history.messages("现在 10:00:01") if m["role"] == "assistant"][0]
    assert assistant["tool_calls"] == [call]
    assert assistant["reasoning_content"] == ""


def test_tool_call_assistant_keeps_real_reasoning() -> None:
    history = tui.History()
    call = {"id": "call_1", "type": "function", "function": {"name": "scan", "arguments": "{}"}}
    history.add(tui.Entry(role="assistant", content="", ts="10:00:00", tool_calls=[call], reasoning="先看一眼"))
    assistant = [m for m in history.messages("") if m["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "先看一眼"


def test_text_assistant_without_reasoning_omits_field() -> None:
    history = tui.History()
    history.add(tui.Entry(role="assistant", content="随便走走", ts="10:00:00"))
    assistant = [m for m in history.messages("") if m["role"] == "assistant"][0]
    assert "reasoning_content" not in assistant
    assert assistant["content"] == "随便走走"


def test_is_reasoning_400() -> None:
    error = tui.lithos.LithosError(
        "LithosAI 返回 HTTP 400",
        status=400,
        body='{"error":{"message":"The reasoning_content in the thinking mode must be passed back to the API."}}',
    )
    assert tui.is_reasoning_400(error)
    assert not tui.is_reasoning_400(tui.lithos.LithosError("boom", status=500))
    assert not tui.is_reasoning_400(tui.lithos.LithosError("bad", status=400))

class FakeConsole:
    """只记下调用，不碰真终端：车道映射是纯逻辑。"""

    def __init__(self) -> None:
        self.fast_lines = []
        self.statuses = []

    def fast(self, text: str) -> None:
        self.fast_lines.append(text)

    def set_status(self, **fields) -> None:
        self.statuses.append(fields)


def test_lane_trace_marks_fast_lane_and_returns_to_slow() -> None:
    console = FakeConsole()
    handler = tui.lane_trace(console)
    handler("operator_start", {"intent": "把名字确认掉", "text": "Frisk", "done_when": "进入游戏",
                              "narrator": True})
    handler("operator_action", {"t_ms": 40, "keys": ["z"], "decide_ms": 90, "pressed": ["z"]})
    handler("operator_note", {"t_ms": 1430, "text": "画面从标题进入了命名界面", "done": False})
    handler("operator_note", {"t_ms": 2900, "text": "名字已经填好并确认", "done": True})
    handler("operator_action", {"t_ms": 3000, "keys": [], "decide_ms": 88, "pressed": []})
    handler("operator_end", {"ended": "goal_reached", "steps": 2, "total_ms": 3100, "reached": True})

    assert console.statuses[0] == {"lane": "快"}
    assert console.statuses[-1] == {"lane": "慢"}
    assert "操作器接手" in console.fast_lines[0] and "Frisk" in console.fast_lines[0]
    assert "进入游戏" in console.fast_lines[0] and "嘴在线" in console.fast_lines[0]
    # 快车道画的是"手的一次决策"和"嘴的一句解说"，不是"选中了哪个语义键"。
    assert "手 #1" in console.fast_lines[1] and "键 z" in console.fast_lines[1]
    assert "[1.4s]" in console.fast_lines[2] and "命名界面" in console.fast_lines[2]
    assert "[2.9s]" in console.fast_lines[3] and "判定达成" in console.fast_lines[3]
    # 空动作不刷屏：第 2 次决策没按键就不画，只在 1 次/每 5 次/真按键时出头。
    assert len(console.fast_lines) == 5
    assert "收手：goal_reached" in console.fast_lines[4]
    assert "达成条件成立" in console.fast_lines[4]


def test_lane_trace_ignores_unknown_events() -> None:
    console = FakeConsole()
    tui.lane_trace(console)("noise", {"x": 1})
    assert console.fast_lines == [] and console.statuses == []


def test_console_fast_writes_a_marked_line(capsys) -> None:
    console = make_console()
    console.fast("手 #1 · 决策 90ms · 键 z")
    out = capsys.readouterr().out
    assert "快 │" in out and "键 z" in out


def test_status_line_carries_the_lane() -> None:
    console = make_console()
    console.set_status(lane="快", turn=3, phase="调用工具")
    assert "车道快" in console.status_line()
