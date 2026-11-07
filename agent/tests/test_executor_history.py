# -*- coding: utf-8 -*-
"""执行器写回手历史 + 按键时序账（设计 03 §C.4/§C.13 的消费方）。

改造后很长一段时间里 `motor.record()` 全仓零调用：gamescaling 的输入是
「当前帧 + 前 5 帧 + 当时动作」，history 空着等于每步都在无历史条件下决策。
这里把三件事钉住：

1. 每次决策都真的写一条历史（帧 + 实际动作串），不管有没有按键；
2. 按键被吞 / 窗口抢不到时，历史里必须带 pressed=False 与原因；
3. `step_results` 的 lag 必须被算出来并进结果，晚一整拍才算 late；
   旧 adapter 没有 step_results 时绝不许伪造 0 延迟。
"""
from __future__ import annotations

import executor

JPEG = b'\xff\xd8\xff\xd9'


class FakeAdapter:
    """截图/按键替身：step_results 可脚本化（默认全部按计划落地）。"""

    def __init__(self, step_results=None, pressed=None):
        self.step_results = step_results
        self.pressed = pressed
        self.played = 0

    def grab(self):
        return {"data": JPEG}

    def play_steps(self, steps, step_ms=33):
        self.played += 1
        out = {"pressed": ["Z"], "skipped_keys": []}
        if self.step_results is not None:
            out["step_results"] = [dict(row) for row in self.step_results]
        return out


class RecordingMotor:
    """最小 motor：回固定动作、把每次 record 记下来。"""

    def __init__(self, steps=None, history_text='<|action_start|>0 0 0 ; Z<|action_end|>',
                 record_kwargs=True):
        self.steps = [list(step) for step in (steps or [['Z'], [], [], [], [], []])]
        self.history_text = history_text
        self.record_kwargs = record_kwargs
        self.records = []
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def decide(self, frame_url, instruction='', game='', system_prompt=None, deadline_ms=None):
        out = {'steps': [list(step) for step in self.steps], 'mouse': {}}
        if self.history_text is not None:
            out["history_text"] = self.history_text
        return out

    def record(self, frame_url, action_text, **kwargs):
        if not self.record_kwargs and kwargs:
            raise TypeError("record() takes 2 positional arguments but 4 were given")
        self.records.append({"frame_url": frame_url, "action": action_text, "kwargs": dict(kwargs)})


def _run(motor, adapter=None, **kwargs):
    step_ms = int(kwargs.pop("step_ms", 33))
    ex = executor.FastExecutor(adapter or FakeAdapter(), motor=motor, narrator=None,
                               step_ms=step_ms)
    params = {"budget_s": 5.0, "max_decisions": 2}
    params.update(kwargs)
    return ex.run("走到存档点", "", **params)


def test_every_decision_writes_one_history_turn() -> None:
    motor = RecordingMotor()
    result = _run(motor, max_decisions=3)
    assert result['action_count'] == 3
    assert len(motor.records) == 3
    assert all(row['frame_url'].startswith('data:image/jpeg;base64,') for row in motor.records)
    assert all(row['action'] == motor.history_text for row in motor.records)
    assert all(row['kwargs'].get('pressed') is None for row in motor.records)  # 没 step_results 就不编状态
    assert all(item['ok'] for item in result['history'])


def test_skipped_keys_mark_history_as_not_pressed() -> None:
    rows = [
        {'index': 0, 'plan_t': 0, 'actual_t': 0, 'lag_ms': 0, 'pressed': True, 'skipped_reason': ''},
        {'index': 1, 'plan_t': 33, 'actual_t': 41, 'lag_ms': 8, 'pressed': False,
         'skipped_reason': 'no_window_focus'},
    ]
    motor = RecordingMotor()
    _run(motor, adapter=FakeAdapter(step_results=rows), max_decisions=1)
    kwargs = motor.records[0]['kwargs']
    assert kwargs['pressed'] is False
    assert kwargs['step'] == 2
    assert kwargs['skipped_reason'] == 'no_window_focus'


def test_all_steps_pressed_marks_history_ok() -> None:
    rows = [
        {'index': i, 'plan_t': i * 33, 'actual_t': i * 33, 'lag_ms': 0, 'pressed': True,
         'skipped_reason': ''} for i in range(6)
    ]
    motor = RecordingMotor()
    _run(motor, adapter=FakeAdapter(step_results=rows), max_decisions=1)
    kwargs = motor.records[0]['kwargs']
    assert kwargs['pressed'] is True and kwargs['step'] == 6 and kwargs['skipped_reason'] == ''


def test_legacy_record_signature_still_gets_history() -> None:
    motor = RecordingMotor(record_kwargs=False)
    rows = [{'index': 0, 'lag_ms': 0, 'pressed': False, 'skipped_reason': 'no_window_focus'}]
    result = _run(motor, adapter=FakeAdapter(step_results=rows), max_decisions=1)
    assert len(motor.records) == 1 and motor.records[0]["kwargs"] == {}
    assert result['history'][0]['ok'] is True


def test_missing_history_text_does_not_record() -> None:
    motor = RecordingMotor(history_text=None)
    result = _run(motor, max_decisions=1)
    assert motor.records == []
    assert result['history'][0]['ok'] is False


def test_play_failure_is_written_as_not_pressed() -> None:
    class BoomAdapter(FakeAdapter):
        def play_steps(self, steps, step_ms=33):
            raise RuntimeError("SendInput 失败")

    motor = RecordingMotor()
    _run(motor, adapter=BoomAdapter(), max_decisions=1)
    kwargs = motor.records[0]['kwargs']
    assert kwargs['pressed'] is False and kwargs['skipped_reason'] == 'play_failed'


def test_timing_counts_steps_late_by_a_full_beat() -> None:
    rows = [
        {'index': 0, 'plan_t': 0, 'actual_t': 0, 'lag_ms': 0, 'pressed': True, 'skipped_reason': ''},
        {'index': 1, 'plan_t': 33, 'actual_t': 80, 'lag_ms': 47, 'pressed': True, 'skipped_reason': ''},
        {'index': 2, 'plan_t': 66, 'actual_t': 172, 'lag_ms': 106, 'pressed': True, 'skipped_reason': ''},
    ]
    motor = RecordingMotor()
    result = _run(motor, adapter=FakeAdapter(step_results=rows), max_decisions=2)
    assert result['late_steps'] == 4  # 每步：47 与 106 都 > 33，两个决策共 4 步 late
    assert result['timing']['decisions'] == 2
    assert result['timing']['max_lag_ms'] == 106
    assert result['actions'][0]['timing']['steps'] == 3
    assert result['actions'][0]['timing']['late_steps'] == 2


def test_no_step_results_means_no_timing_claims() -> None:
    motor = RecordingMotor()
    result = _run(motor, max_decisions=2)
    assert result['late_steps'] == 0
    assert result['timing'] == {}
    assert all(entry['timing'] == {} for entry in result['actions'])


def test_aggregate_prefers_window_reason_over_unknown_key() -> None:
    assert executor._aggregate_step_status(
        [{'pressed': False, 'skipped_reason': 'unknown_key:Z'},
         {'pressed': False, 'skipped_reason': 'no_window_focus'}])['reason'] == 'unknown_key:Z'
    assert executor._aggregate_step_status([]) == {}