# -*- coding: utf-8 -*-
'''卡住检测接进执行器（01 §7「卡关」的机器判据）。

为什么：之前只有快系统自述「我卡住了」这一条路，模型不自知时（对着墙按 W）慢系统
永远等不到交棒。这里钉住四件事：

1. 判到卡住 -> ended=stuck、动作立即收手、时间轴留一条 stuck 事件；
2. 检测器自己抛错 -> 只记错误，游玩继续（绝不能因为检测坏了打断直播）；
3. 没挂检测器 -> 结果里不多任何键（老调用方结构不变）；
4. 检测器真的收到了这一轮的动作串（重复动作判据要靠它）。
'''
from __future__ import annotations

import io

from PIL import Image

import executor
import stuck
import timeline as timeline_mod

JPEG = b'\xff\xd8\xff\xd9'


def real_jpeg(color: int = 30, size=(64, 36)) -> bytes:
    img = Image.new('RGB', size, (color, color, color))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


class FakeAdapter:
    def __init__(self, jpeg: bytes = JPEG):
        self.jpeg = jpeg
        self.played = 0

    def grab(self):
        return {'data': self.jpeg}

    def play_steps(self, steps, step_ms=33):
        self.played += 1
        return {'pressed': ['Z'], 'skipped_keys': []}


class FakeMotor:
    def __init__(self, steps=None):
        self.steps = [list(step) for step in (steps or [['W'], [], [], [], [], []])]

    def reset(self):
        return None

    def decide(self, frame_url, instruction='', game='', system_prompt=None, deadline_ms=None):
        return {'steps': [list(step) for step in self.steps], 'mouse': {}}

    def record(self, frame_url, action_text, **kwargs):
        return None


class ScriptedDetector:
    '''第 stuck_at 次观察开始返回卡住；boom=True 时每次都抛。'''

    def __init__(self, stuck_at=2, boom=False):
        self.stuck_at = stuck_at
        self.boom = boom
        self.calls = []
        self.stats = {'frames': 0}

    def observe(self, jpeg, action=None):
        if self.boom:
            raise RuntimeError('detector exploded')
        self.calls.append(action)
        n = len(self.calls)
        if n >= self.stuck_at:
            return {'ok': True, 'stuck': True, 'static_streak': 12, 'repeat': 4,
                    'why': '画面连续 12 帧几乎没变', 'frames_seen': n}
        return {'ok': True, 'stuck': False, 'static_streak': 0, 'repeat': 0,
                'why': '', 'frames_seen': n}


def _run(detector, *, jpeg=JPEG, max_decisions=5, timeline=None, adapter=None):
    ex = executor.FastExecutor(adapter or FakeAdapter(jpeg), motor=FakeMotor(),
                               narrator=None, step_ms=1, stuck=detector)
    return ex.run('走到存档点', '', budget_s=5.0, max_decisions=max_decisions,
                  intent_id='i-1', timeline=timeline)


def test_stuck_ends_the_intent_and_writes_timeline(tmp_path):
    tl = timeline_mod.Timeline(root=tmp_path / 'run' / 's1', session_id='s1')
    detector = ScriptedDetector(stuck_at=2)
    result = _run(detector, timeline=tl)
    tl.close()
    assert result['ended'] == 'stuck'
    assert result['reached'] is False
    assert result['action_count'] == 2
    assert result['stuck']['why'] == '画面连续 12 帧几乎没变'
    assert any('卡住' in text for text in result['errors'])
    rows = [r for r in tl.rows() if r.get('kind') == 'stuck']
    assert len(rows) == 1
    assert rows[0]['static_streak'] == 12
    assert rows[0]['intent_id'] == 'i-1'


def test_detector_error_does_not_stop_play() -> None:
    result = _run(ScriptedDetector(boom=True), max_decisions=3)
    assert result['ended'] == 'max_decisions'
    assert result['action_count'] == 3
    assert any('卡住检测异常' in text for text in result['errors'])
    assert result['stuck']['stuck'] is False


def test_no_detector_keeps_old_shape() -> None:
    result = _run(None, max_decisions=2)
    assert result['ended'] == 'max_decisions'
    assert 'stuck' not in result
    assert 'stuck_stats' not in result


def test_detector_receives_this_rounds_action() -> None:
    detector = ScriptedDetector(stuck_at=99)
    _run(detector, max_decisions=2)
    assert detector.calls[0] == 'W'
    assert len(detector.calls) == 2


def test_real_detector_catches_a_wall() -> None:
    '''真检测器 + 真静态 JPEG：对着墙一直按 W，应该在 window 帧内收手。'''
    jpeg = real_jpeg(20)
    detector = stuck.StuckDetector(window=4, min_change=1.5)
    result = _run(detector, jpeg=jpeg, max_decisions=10)
    assert result['ended'] == 'stuck'
    assert result['action_count'] <= 6
    assert result['stuck_stats']['static_frames'] >= 3
    assert '画面连续' in result['stuck']['why']


def test_real_detector_lets_a_moving_scene_play_on() -> None:
    '''画面每次都在变（换色）就不许判卡住，哪怕动作一模一样。'''

    class ColorAdapter(FakeAdapter):
        def __init__(self):
            self.i = 0

        def grab(self):
            self.i += 1
            return {'data': real_jpeg(10 + self.i * 30)}

    detector = stuck.StuckDetector(window=3, min_change=1.0)
    result = _run(detector, adapter=ColorAdapter(), max_decisions=5)
    assert result['ended'] == 'max_decisions'
    assert result['action_count'] == 5