# -*- coding: utf-8 -*-
'''执行器 × 三段计时：每步必须落 t_encode / t_upload_infer / t_act。

为什么要它：以前只有 decide_ms 一个数，谁都不知道「慢」是编帧、上传+推理，还是按键。
这里钉住：三步都记、payload 字节数真、history_len 反映手模型带了几帧、没挂账本时
返回结构与改造前完全一致（不多键）。
'''
from __future__ import annotations

import os

import executor
import steptrace

JPEG = b'\xff\xd8\xff\xd9'


class FakeAdapter:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.played = []
        self.delay_s = delay_s

    def grab(self):
        return {'data': JPEG}

    def play_steps(self, steps, step_ms=33):
        self.played.append([list(step) for step in steps])
        return {'pressed': [key for step in steps for key in step],
                'skipped_keys': [],
                'step_results': [{'step': i, 'plan_t': i * 33, 'actual_t': i * 33,
                                  'lag_ms': 0, 'pressed': True}
                                 for i in range(len(steps))]}


class ScriptedMotor:
    def __init__(self, steps=None, history=None) -> None:
        self.steps = [['W'], ['A']] if steps is None else [list(s) for s in steps]
        self.history = list(history or [])
        self.calls = 0

    def reset(self) -> None:
        self.history = []

    def record(self, frame_url, text, pressed=None, step=None, skipped_reason=''):
        self.history.append({'frame': frame_url, 'text': text,
                             'pressed': pressed, 'step': step})
        return {'ok': True}

    def decide(self, frame_url, instruction='', game='', system_prompt=None, deadline_ms=None):
        self.calls += 1
        return {'steps': [list(step) for step in self.steps], 'mouse': {},
                'raw': '<|action_start|>0 0 0 ; W<|action_end|>',
                'history_text': '<|action_start|>0 0 0 ; W ; W ; W ; W ; W ; W<|action_end|>'}


def _run(tmp_path, trace, steps=2):
    adapter = FakeAdapter()
    motor = ScriptedMotor()
    ex = executor.FastExecutor(adapter, motor, None, step_ms=1,
                               narrate_every_s=10.0, trace=trace)
    return ex.run('go', '', budget_s=5.0, max_decisions=steps)


def test_each_step_writes_three_phases(tmp_path):
    trace = steptrace.build_trace(str(tmp_path / 'run' / 's1'))
    result = _run(tmp_path, trace)

    rows = trace.rows()
    assert len(rows) == result['action_count'] == 2
    for row in rows:
        assert row['t_encode'] is not None and row['t_encode'] >= 0
        assert row['t_upload_infer'] is not None and row['t_upload_infer'] >= 0
        assert row['t_act'] is not None and row['t_act'] >= 0
        assert row['payload_bytes'] == len(JPEG), 'payload 必须是真帧字节数'
        assert row['intent_id'] == result.get('intent_id', row['intent_id'])


def test_summary_lands_in_the_result(tmp_path):
    trace = steptrace.build_trace(str(tmp_path))
    result = _run(tmp_path, trace, steps=2)

    summary = result['steps_summary']
    assert summary['steps'] == 2
    assert summary['file'].endswith('steps.jsonl')
    assert summary['phases']['t_upload_infer']['n'] == 2


def test_history_len_is_recorded(tmp_path):
    trace = steptrace.build_trace(str(tmp_path))
    result = _run(tmp_path, trace, steps=2)

    lens = [row['history_len'] for row in trace.rows()]
    assert lens == sorted(lens), '手历史只会随步数增长（除非超时 reset）'
    assert lens[-1] >= 1, '每步都要写回历史，否则 A/B 的「5 张 vs 2 张」根本没流量'


def test_without_trace_the_result_shape_is_unchanged(tmp_path):
    result = _run(tmp_path, None, steps=1)

    assert 'steps_summary' not in result
