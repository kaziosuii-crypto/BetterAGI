# -*- coding: utf-8 -*-
"""执行循环改造单测（agent/executor.py）：

- 每步执行期 deadline（deadline_ms）与 slow_steps 统计；
- 失败分类：MotorParseError / MotorTimeout 分开，且失败绝不允许伪装成功；
- motor.reset() 每意图一次；
- timeline 打点（intent_start / game_action / intent_end，t_ms 单调不降）；
- 线程卫生：narrate 线程在 finally 里被 stop + join，不泄漏。
"""
from __future__ import annotations

import threading
import time

import executor
import timeline

JPEG = b"\xff\xd8\xff\xd9"


class MotorParseError(Exception):
    """测试替身：与 motor.MotorParseError 同名（执行器按类名兜底识别）。"""


class MotorTimeout(Exception):
    """测试替身：与 motor.MotorTimeout 同名。"""


class FakeAdapter:
    """最小截图/按键替身：回固定帧，动作原样记账。"""

    def __init__(self) -> None:
        self.played = []

    def grab(self):
        return {"data": JPEG}

    def play_steps(self, steps, step_ms=33):
        self.played.append([list(step) for step in steps])
        return {"pressed": ["Z"], "skipped_keys": []}


class ScriptedMotor:
    """固定出手的假 motor：可计数 reset、记录每次 decide 收到的参数。"""

    def __init__(self, steps=None) -> None:
        self.steps = [["Z"]] if steps is None else [list(step) for step in steps]
        self.reset_calls = 0
        self.decide_calls = []

    def reset(self) -> None:
        self.reset_calls += 1

    def decide(self, frame_url, instruction="", game="", system_prompt=None, deadline_ms=None):
        self.decide_calls.append({"instruction": instruction, "deadline_ms": deadline_ms})
        return {"steps": [list(step) for step in self.steps], "mouse": {}}


class OldMotor:
    """改造前的 motor 签名：没有 deadline_ms，硬传就会 TypeError；也没有 reset。"""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, frame_url, instruction="", game="", system_prompt=None):
        self.calls += 1
        return {"steps": [["Z"]], "mouse": {}}


class ParseBoomMotor:
    """一出手就抛 MotorParseError（模型输出坏）。"""

    def __init__(self, raw_text="I think you should press A", delay_s=0.0) -> None:
        self.raw_text = raw_text
        self.delay_s = delay_s
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def decide(self, *args, **kwargs):
        if self.delay_s:
            time.sleep(self.delay_s)
        error = MotorParseError("<parse_failed> 模型没吐动作标签")
        error.raw_text = self.raw_text
        raise error


class TimeoutBoomMotor:
    """一出手就抛 MotorTimeout（网络/服务超时）。"""

    def reset(self) -> None:
        pass

    def decide(self, *args, **kwargs):
        raise MotorTimeout("gamescaling 超过 300ms 没回")


class SlowMotor:
    """每步都超预算的 motor（睡 20ms）。"""

    def __init__(self) -> None:
        self.reset_calls = 0
        self.last_deadline = None

    def reset(self) -> None:
        self.reset_calls += 1

    def decide(self, frame_url, instruction="", game="", system_prompt=None, deadline_ms=None):
        self.last_deadline = deadline_ms
        time.sleep(0.02)
        return {"steps": [["Z"]], "mouse": {}}


class FakeNarrator:
    """假 Omni：按脚本说话，之后一直说"画面基本没变"。"""

    def __init__(self, lines=None) -> None:
        self.lines = list(lines or [])
        self.asked = 0
        self.pushed = 0

    def push(self, data) -> None:
        self.pushed += 1

    def ask(self, instructions) -> str:
        self.asked += 1
        if self.lines:
            return self.lines.pop(0)
        return "画面基本没变"


def _run(motor, narrator=None, ctor=None, **kwargs):
    ex = executor.FastExecutor(FakeAdapter(), motor=motor, narrator=narrator, **(ctor or {}))
    params = {"budget_s": 5.0, "max_decisions": 3}
    params.update(kwargs)
    return ex.run("走到存档点", "", **params)


def test_motor_parse_error_is_classified_and_never_success() -> None:
    motor = ParseBoomMotor()
    result = _run(motor)
    assert result["ended"] == "motor_parse_error"
    assert result["reached"] is False
    assert result["action_count"] == 0
    detail = result["error_detail"]
    assert detail and detail["kind"] == "motor_parse_error"
    assert detail["type"] == "MotorParseError"
    assert detail["raw_text"] == "I think you should press A"
    # 默认执行期预算 = 200ms（06 设计 MOTOR_STEP_BUDGET_MS；03 C.4）
    assert detail["deadline_ms"] == 200
    assert result["errors"] and "MotorParseError" in result["errors"][0]


def test_motor_timeout_is_classified_separately() -> None:
    result = _run(TimeoutBoomMotor())
    assert result["ended"] == "motor_timeout"
    assert result["reached"] is False
    assert result["error_detail"]["kind"] == "motor_timeout"
    assert result["error_detail"]["type"] == "MotorTimeout"


def test_run_resets_motor_history_once() -> None:
    motor = ScriptedMotor()
    _run(motor, max_decisions=2)
    assert motor.reset_calls == 1


def test_old_motor_without_deadline_kwarg_does_not_typeerror() -> None:
    motor = OldMotor()  # 没有 reset、decide 不接 deadline_ms
    result = _run(motor, max_decisions=2)
    assert motor.calls == 2
    assert result["action_count"] == 2
    assert result["ended"] == "max_decisions"


def test_deadline_forwarded_to_motor() -> None:
    motor = ScriptedMotor()
    _run(motor, max_decisions=2, deadline_ms=250)
    assert [call["deadline_ms"] for call in motor.decide_calls] == [250, 250]


def test_slow_steps_are_counted_but_not_failures() -> None:
    motor = SlowMotor()
    result = _run(motor, max_decisions=1, deadline_ms=1)
    assert result["slow_steps"] == 1
    assert result["decide_ms"] and result["decide_ms"][0] > 1
    # 慢不等于错：没有任何手侧失败，也不是解析/超时结束。
    assert result["error_detail"] is None
    assert result["ended"] == "max_decisions"
    assert motor.last_deadline == 1


def test_fast_path_decide_ms_matches_actions() -> None:
    motor = ScriptedMotor()
    result = _run(motor, max_decisions=3)
    assert result["action_count"] == 3
    assert len(result["decide_ms"]) == result["action_count"]
    assert [item["decide_ms"] for item in result["actions"]] == result["decide_ms"]
    assert result["slow_steps"] == 0
    assert result["error_detail"] is None
    assert result["reached"] is False


def test_timeline_records_start_action_end_in_order(tmp_path) -> None:
    tl = timeline.Timeline(root=tmp_path, session_id="run-exec")
    motor = ScriptedMotor()
    result = _run(motor, max_decisions=2, intent_id="i-001", timeline=tl)
    assert (tmp_path / "timeline.jsonl").exists()
    rows = tl.rows()
    kinds = [row["kind"] for row in rows]
    assert kinds[0] == "intent_start"
    assert kinds[-1] == "intent_end"
    assert kinds.count("game_action") == 2
    assert rows[0]["intent_id"] == "i-001"
    assert rows[0]["goal"] == "走到存档点"
    assert all(row["intent_id"] == "i-001" for row in rows)
    stamps = [row["t_ms"] for row in rows]
    assert stamps == sorted(stamps), stamps
    assert all(row["decide_ms"] >= 0 for row in rows if row["kind"] == "game_action")
    last = rows[-1]
    assert last["ended"] == result["ended"] == "max_decisions"
    assert last["reached"] is False
    assert last["total_ms"] >= 0


def test_timeline_end_marks_failure_not_success(tmp_path) -> None:
    tl = timeline.Timeline(root=tmp_path, session_id="run-exec-fail")
    _run(ParseBoomMotor(), intent_id="i-err", timeline=tl)
    rows = tl.rows()
    assert [row["kind"] for row in rows] == ["intent_start", "intent_end"]
    assert rows[-1]["ended"] == "motor_parse_error"
    assert rows[-1]["reached"] is False


def test_narrator_done_cannot_mask_motor_failure() -> None:
    # 手正在 decide（睡 0.5s）时嘴置位 done，随后手抛解析错：绝不允许
    # 因为 done 已置位就把这次失败翻成 goal_reached / reached=True。
    narrator = FakeNarrator(["画面基本没变", "[DONE]理由：画面里存档点已经出现"])
    motor = ParseBoomMotor(delay_s=0.5)
    ex = executor.FastExecutor(FakeAdapter(), motor=motor, narrator=narrator,
                               done_confirm=1, narrate_every_s=0.2)
    result = ex.run("走到存档点", "存档点出现", budget_s=5.0, max_decisions=5)
    assert result["ended"] == "motor_parse_error"
    assert result["ended"] != "goal_reached"
    assert result["reached"] is False
    assert result["error_detail"]["kind"] == "motor_parse_error"


def test_narrator_thread_is_joined_and_not_leaked() -> None:
    baseline = threading.active_count()
    narrator = FakeNarrator()
    motor = ScriptedMotor()
    result = _run(motor, narrator=narrator, ctor={"narrate_every_s": 0.2}, max_decisions=3)
    assert result["action_count"] == 3
    assert narrator.asked >= 1
    assert threading.active_count() == baseline
    assert not [item for item in threading.enumerate() if item.name == "fast-narrator"]
