# -*- coding: utf-8 -*-
"""执行器 × IntentContext：手做的每一步都要让嘴看见（设计 03 §C.6 / D.2）。

三条硬要求：
- 注入 context 时每一步记一条，条数与 actions 一致（第 1 步 = actions[0]）；
- 嘴收到的 instructions 末尾带「上一动作：第 k 步按键 X,Y，耗时 N ms」；
- 不注入 context 时 instructions 与返回结构与改造前逐字一致（老测试不受影响）。

另外覆盖两个容易漏的分支：解析失败但异常里带了部分 steps；context 有但手还没走。
全部离线（假帧 / 假手 / 假嘴），不联网、不碰 GPU、不起真实等待。
"""
from __future__ import annotations

import threading

import executor
import intent_context

JPEG = b"\xff\xd8\xff\xd9"

#: 纯加法的新键（第四轮：按键时序账 + 历史写回回执）。老测试断言形状时
#: 允许它们存在，但必须显式列出——多出任何一个新键都算破坏形状契约。
#: 2026-11-04 用户口径「不操作就是不操作」：空动作兜底已整体删除（executor 不再替模型按键）。
ADDED_KEYS = {"late_steps", "timing", "history", "dropped_steps"}

#: 改造前 run() 的返回键集合：不注入 context 时必须一模一样。
OLD_RESULT_KEYS = {
    "ended", "reached", "done_reason", "total_ms", "commentary", "action_count",
    "actions", "summary", "errors", "slow_steps", "decide_ms", "error_detail",
}


class FakeAdapter:
    """最小画面/按键替身：固定帧；按键只记账。"""

    def __init__(self, skipped=None) -> None:
        self.played = []
        self.skipped = list(skipped or [])

    def grab(self):
        return {"data": JPEG}

    def play_steps(self, steps, step_ms=33):
        self.played.append([list(step) for step in steps])
        return {"pressed": [key for step in steps for key in step],
                "skipped_keys": list(self.skipped)}


class ScriptedMotor:
    """固定出手的假手；reset 计数，decide 返回给定 steps。"""

    def __init__(self, steps=None) -> None:
        self.steps = [["W"]] if steps is None else [list(step) for step in steps]
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def decide(self, frame_url, instruction="", game="", system_prompt=None, deadline_ms=None):
        return {"steps": [list(step) for step in self.steps], "mouse": {}}


class MotorParseError(Exception):
    """与 motor.MotorParseError 同名同形（执行器按类名分流）。"""

    def __init__(self, message: str, *, steps=None, raw_text: str = "") -> None:
        super().__init__(message)
        self.steps = [list(step) for step in (steps or [])]
        self.raw_text = raw_text


class PartialParseMotor:
    """解析失败，但异常里带着已经解析出来的 steps（部分解析）。"""

    def __init__(self) -> None:
        self.steps = [["W"], ["A"]]

    def reset(self) -> None:
        pass

    def decide(self, *args, **kwargs):
        raise MotorParseError("解析到一半就断了", steps=self.steps, raw_text="<garbage>")


class SavingNarrator:
    """按脚本说话；ask 一次就置 stop，让 _narrate_loop 同步跑一轮就收。"""

    def __init__(self, text: str = "画面基本没变") -> None:
        self.text = text
        self.instructions = []
        self.stop = threading.Event()

    def push(self, data) -> None:
        pass

    def ask(self, instructions) -> str:
        self.instructions.append(instructions)
        self.stop.set()
        return self.text


def _narrate_once(ex, narrator, intent="走到存档点", done_when="存档点出现"):
    """同步跑一轮嘴循环（不起线程，避免时序 flake）。"""
    ex.started = ex.clock()
    ex._narrate_loop(
        intent, done_when, narrator.stop, threading.Event(),
        {"done": 0, "empty": 0, "reason": ""}, [],
    )


def test_every_step_is_recorded_and_count_matches_actions():
    ctx = intent_context.IntentContext()
    ex = executor.FastExecutor(FakeAdapter(), motor=ScriptedMotor([["W"]]), context=ctx)
    result = ex.run("走到存档点", "", budget_s=5.0, max_decisions=3)

    snap = ctx.snapshot()
    assert result["action_count"] == 3
    assert snap["action_count"] == result["action_count"]
    # 第 1 步 = actions[0]：step_index/t_ms/decide_ms/keys 必须和动作条目对得上。
    recorded = [(item["step_index"], item["keys"], item["decide_ms"], item["t_ms"])
                for item in snap["recent_actions"]]
    expected = [(index + 1, item["keys"], item["decide_ms"], item["t_ms"])
                for index, item in enumerate(result["actions"])]
    assert recorded == expected
    assert [item["step_index"] for item in snap["recent_actions"]] == [1, 2, 3]
    assert result["intent_context"]["action_count"] == 3
    assert set(result) == OLD_RESULT_KEYS | {"intent_context"} | ADDED_KEYS


def test_narrator_instructions_include_last_action():
    ctx = intent_context.IntentContext()
    ctx.record_action(3, ["W", "A"], 41, 120)
    narrator = SavingNarrator()
    ex = executor.FastExecutor(FakeAdapter(), motor=ScriptedMotor(), narrator=narrator,
                               context=ctx, narrate_every_s=0.2)
    _narrate_once(ex, narrator)

    assert len(narrator.instructions) == 1
    text = narrator.instructions[0]
    # 追加不是替换：完成条件还在，上一动作在末尾。
    assert "完成条件：存档点出现" in text
    assert text.rstrip().endswith("上一动作：第 3 步按键 W,A，耗时 41ms")
    # 嘴说的话也写回了信箱（手下一次 decide 能读）。
    assert ctx.commentary(limit=1)[-1]["text"] == "画面基本没变"
    assert ctx.snapshot()["last_commentary"]["text"] == "画面基本没变"
    assert ctx.snapshot()["commentary_count"] == 1


def test_without_context_instructions_are_unchanged():
    narrator = SavingNarrator()
    ex = executor.FastExecutor(FakeAdapter(), motor=ScriptedMotor(), narrator=narrator,
                               narrate_every_s=0.2)
    assert ex.context is None
    _narrate_once(ex, narrator)
    expected = executor.NARRATE_INSTRUCTIONS.format(
        intent="走到存档点", mark=executor.DONE_MARK, done_when="存档点出现",
    )
    assert narrator.instructions[0] == expected
    assert "上一动作" not in narrator.instructions[0]


def test_context_without_actions_leaves_instructions_unchanged():
    ctx = intent_context.IntentContext()
    narrator = SavingNarrator()
    ex = executor.FastExecutor(FakeAdapter(), motor=ScriptedMotor(), narrator=narrator,
                               context=ctx, narrate_every_s=0.2)
    _narrate_once(ex, narrator)
    expected = executor.NARRATE_INSTRUCTIONS.format(
        intent="走到存档点", mark=executor.DONE_MARK, done_when="存档点出现",
    )
    assert narrator.instructions[0] == expected
    assert ctx.snapshot()["action_count"] == 0


def test_parse_failure_with_partial_steps_is_recorded():
    ctx = intent_context.IntentContext()
    ex = executor.FastExecutor(FakeAdapter(), motor=PartialParseMotor(), context=ctx)
    result = ex.run("走到存档点", "", budget_s=5.0, max_decisions=5)

    assert result["ended"] == "motor_parse_error"
    assert result["action_count"] == 0  # 没按下去，动作列表不能假装成功
    snap = ctx.snapshot()
    assert snap["action_count"] == 1
    last = snap["last_action"]
    assert last["step_index"] == 1
    assert last["keys"] == ["W", "A"]
    assert "解析失败" in last["note"]
    assert result["error_detail"]["kind"] == "motor_parse_error"


def test_run_without_context_keeps_old_result_shape():
    ex = executor.FastExecutor(FakeAdapter(), motor=ScriptedMotor([["W"]]))
    result = ex.run("走到存档点", "", budget_s=5.0, max_decisions=2)
    assert "intent_context" not in result
    assert set(result) == OLD_RESULT_KEYS | ADDED_KEYS
