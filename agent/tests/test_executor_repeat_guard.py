# -*- coding: utf-8 -*-
'''重复按键守卫（用户 2026-11-06：不许不过脑子地狂按）。

背景：实测手模型在画面毫无变化时连按 C 16 次、Z 15 次。卡住检测只按「逐帧差」判，
某些场景（小幅动画/光标闪烁）逐帧差会清零，守卫因此漏判。这里补一条更硬的判据：
同一套动作连着重复 6 次、画面相对本次意图起点几乎没动 -> 收手。
'''
from __future__ import annotations

import io

from PIL import Image

import executor


def solid(color: int) -> bytes:
    img = Image.new('RGB', (64, 36), (color, color, color))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


class StaticAdapter:
    '''每帧都一样：模拟画面真的没动。'''

    def __init__(self, jpeg: bytes = None):
        self.jpeg = jpeg or solid(30)
        self.played = 0

    def grab(self):
        return {'data': self.jpeg}

    def play_steps(self, steps, step_ms=33):
        self.played += 1
        return {'pressed': ['C'], 'skipped_keys': []}


class ChangingAdapter(StaticAdapter):
    '''每帧换一个颜色：模拟走对路、画面在变。'''

    def __init__(self):
        super().__init__()
        self.n = 0

    def grab(self):
        self.n += 1
        return {'data': solid((self.n * 37) % 200)}


class RepeatMotor:
    '''永远吐同一套动作。'''

    def __init__(self, steps=None):
        self.steps = [list(step) for step in (steps or [['C'], [], [], [], [], []])]

    def reset(self):
        return None

    def decide(self, frame_url, instruction='', game='', system_prompt=None, deadline_ms=None):
        return {'steps': [list(step) for step in self.steps], 'mouse': {}}

    def record(self, frame_url, action_text, **kwargs):
        return None


def _run(adapter, max_decisions=20):
    ex = executor.FastExecutor(adapter, motor=RepeatMotor(), narrator=None, step_ms=1)
    return ex.run('推进对话', '', budget_s=10.0, max_decisions=max_decisions,
                  intent_id='i-1')


def test_repeated_steps_on_static_screen_stop_the_intent():
    result = _run(StaticAdapter())
    assert result['ended'] == 'stuck'
    assert result['reached'] is False
    assert result['action_count'] <= executor.REPEAT_STEPS_LIMIT
    assert any('卡住' in text for text in result['errors'])


def test_repeated_steps_while_screen_changes_keep_going():
    result = _run(ChangingAdapter(), max_decisions=8)
    assert result['ended'] != 'stuck'
    assert result['action_count'] == 8
