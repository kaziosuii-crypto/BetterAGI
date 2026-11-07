# -*- coding: utf-8 -*-
'''「慢」和「坏」分开：单次超时不毁整轮，连续两次才认输。

真机依据（2026-11-04，暴露端点 http://i-2.gpushare.com:47960）：单帧 p50 904ms、
六帧 p50 1200ms、尾延迟见过 2282ms。执行器原先一遇 MotorTimeout 就结束整轮，
结果模型明明答对了却一个动作都没执行（0 步）。'''
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import motor as motor_mod  # noqa: E402
from executor import FastExecutor  # noqa: E402


class _Adapter:
    def __init__(self):
        self.plays = 0

    def grab(self):
        return {'data': b'\xff\xd8fake', 'w': 320, 'h': 180}

    def play_steps(self, steps, step_ms=33, **_kw):
        self.plays += 1
        return {'pressed': True}


class _Narrator:
    def __init__(self, done_at=99):
        self.n = 0
        self.done_at = done_at

    def observe(self, image, instructions=''):
        self.n += 1
        if self.n >= self.done_at:
            return '[DONE] 已经看到存档点了，够了吧'
        return '左边有箱子'

    ask = observe


class _Motor:
    '''fail_times 是「前几次调用抛超时」，0 表示成功。'''

    def __init__(self, fail_times=()):
        self.fail_times = list(fail_times)
        self.calls = 0
        self.resets = 0
        self.reset_raises = False

    def reset(self):
        self.resets += 1
        if self.reset_raises:
            raise RuntimeError('reset 坏了')
        return None

    def record(self, *args, **kwargs):
        return {'ok': True}

    def decide(self, frame_url, instruction='', **kwargs):
        self.calls += 1
        if self.fail_times and self.fail_times[0] > 0:
            self.fail_times[0] -= 1
            raise motor_mod.MotorTimeout('慢', elapsed_ms=2400, deadline_ms=3740, frame_url=frame_url)
        return {'steps': [['W'], [], [], [], [], []], 'keys': ['W'], 'parsed': True,
                'history_text': '<|action_start|>0 0 0 ; W<|action_end|>'}


def _executor(motor, narrator=None, **kw):
    return FastExecutor(_Adapter(), motor, narrator or _Narrator(done_at=99),
                        motor_prompt='p', game='g', step_ms=33, done_confirm=1,
                        narrate_every_s=0.01, **kw)


def test_single_timeout_does_not_kill_the_intent():
    '''第一次超时等价重试一次：后面还在走，不算失败。'''

    motor = _Motor(fail_times=[1])
    result = _executor(motor).run('走到存档点', budget_s=5, max_decisions=3)

    assert result['ended'] != 'motor_timeout'
    assert result['action_count'] >= 1, result
    assert motor.calls >= 2
    assert any('MotorTimeout' in e for e in result['errors']), result['errors']


def test_two_consecutive_timeouts_end_the_intent():
    '''连续两次超时才是真的跟不上：明确收手，并把两次都留证。'''

    result = _executor(_Motor(fail_times=[2])).run('走到存档点', budget_s=5, max_decisions=3)

    assert result['ended'] == 'motor_timeout'
    assert result['action_count'] == 0
    assert sum(1 for e in result['errors'] if 'MotorTimeout' in e) >= 2, result['errors']


def test_done_during_a_timeout_still_counts_as_reached():
    '''超时等待期间嘴已经判定完成：这一轮是成功，不是失败。'''

    motor = _Motor(fail_times=[1])
    result = _executor(motor, narrator=_Narrator(done_at=1)).run(
        '走到存档点', budget_s=5, max_decisions=3)

    assert result['ended'] == 'goal_reached'
    assert result['reached'] is True

def test_retry_after_timeout_drops_the_history_frames():
    '''超时重试只带当前帧：5 张历史帧 1.5MB 的上传是真机超时的主要成本。'''

    motor = _Motor(fail_times=[1])
    result = _executor(motor).run('走到存档点', budget_s=5, max_decisions=1)

    assert motor.resets >= 1, '超时后应当清空手历史再重试'
    assert any('清空历史帧' in e for e in result['errors']), result['errors']


def test_reset_failure_does_not_break_the_retry():
    '''reset 自己抛错只记账，重试照样发生。'''

    motor = _Motor(fail_times=[1])
    motor.reset_raises = True
    result = _executor(motor).run('走到存档点', budget_s=5, max_decisions=1)

    assert motor.calls >= 2
    assert any('清历史失败' in e for e in result['errors']), result['errors']