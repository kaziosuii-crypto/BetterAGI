# -*- coding: utf-8 -*-
'''冻结泵（agent/freeze_pump.py）：冻结窗口里继续喂帧、超时贴图、坏下游不炸主链路。'''
from __future__ import annotations

import time

import freeze_pump


class Paint:
    def __init__(self, tag: bytes = b'PAINTED') -> None:
        self.tag = tag
        self.calls = 0
        self.fail = False

    def draw(self, jpeg: bytes) -> bytes:
        self.calls += 1
        if self.fail:
            raise RuntimeError('字体挂了')
        return self.tag


def test_hold_once_writes_raw_frame_without_painter() -> None:
    frames = []
    pump = freeze_pump.FreezePump(tap=frames.append, warn_ms=0)
    pump.set_frame(b'JPEG')
    assert pump.hold_once(500) is True
    assert frames == [b'JPEG']
    assert pump.stats['frames'] == 1 and pump.stats['painted'] == 0


def test_paints_only_after_warn_ms() -> None:
    frames = []
    painter = Paint()
    pump = freeze_pump.FreezePump(tap=frames.append, painter=painter, warn_ms=900, refresh_ms=0)
    pump.set_frame(b'JPEG')
    pump.hold_once(100)
    assert painter.calls == 0 and frames[-1] == b'JPEG'
    pump.hold_once(950)
    assert painter.calls == 1 and frames[-1] == b'PAINTED'
    assert pump.stats['painted'] == 1


def test_painted_frame_is_cached_within_refresh_window() -> None:
    frames = []
    painter = Paint()
    pump = freeze_pump.FreezePump(tap=frames.append, painter=painter, warn_ms=0, refresh_ms=10000)
    pump.set_frame(b'JPEG')
    for _ in range(3):
        pump.hold_once(100)
    assert painter.calls == 1  # 贴图很贵：窗口内复用同一张
    assert [f for f in frames] == [b'PAINTED'] * 3


def test_painter_failure_keeps_raw_frame() -> None:
    frames = []
    painter = Paint()
    painter.fail = True
    pump = freeze_pump.FreezePump(tap=frames.append, painter=painter, warn_ms=0)
    pump.set_frame(b'JPEG')
    assert pump.hold_once(10) is True
    assert frames[-1] == b'JPEG'
    assert pump.stats['errors'] == 1


def test_tap_failure_is_recorded_not_raised() -> None:
    def bad_tap(_jpeg):
        raise IOError('ffmpeg 断了')

    pump = freeze_pump.FreezePump(tap=bad_tap)
    pump.set_frame(b'JPEG')
    assert pump.hold_once(10) is False
    assert 'ffmpeg 断了' in pump.stats['last_error']


def test_holding_pumps_frames_while_blocked() -> None:
    frames = []
    pump = freeze_pump.FreezePump(tap=frames.append, fps=20.0)
    pump.set_frame(b'JPEG')
    with pump.holding() as hold:
        time.sleep(0.15)
    assert hold.elapsed_ms >= 100
    assert len(frames) >= 2
    assert pump.stats['holds'] == 1


def test_holding_without_frame_does_not_start_thread() -> None:
    pump = freeze_pump.FreezePump(tap=lambda _f: None)
    with pump.holding() as hold:
        pass
    assert hold.summary()['pumped'] is False
    assert pump.stats['frames'] == 0


def test_frame_tee_fans_out_and_remembers_last() -> None:
    a, b = [], []
    tee = freeze_pump.FrameTee(a.append, b.append)
    assert tee(b'ONE') is True
    assert tee(b'TWO') is True
    assert a == [b'ONE', b'TWO'] and b == [b'ONE', b'TWO']
    assert tee.last_frame == b'TWO'


def test_frame_tee_survives_broken_sink() -> None:
    good = []

    def bad(_f):
        raise RuntimeError('录播挂了')

    tee = freeze_pump.FrameTee(bad, good.append)
    assert tee(b'JPEG') is True
    assert good == [b'JPEG']
    assert tee.errors and '录播挂了' in tee.errors[0]


def test_real_freeze_painter_actually_changes_the_frame() -> None:
    '''真贴图件的方法叫 paint（不是 draw）：泵必须调得动它，否则画面永远不贴字。'''

    try:
        import freeze_overlay
        painter = freeze_overlay.FreezePainter(text='大肥鱼CPU烧了')
    except Exception as skip:  # 没有中文字体时跳过（贴图件会显式报错）
        import pytest
        pytest.skip('没有可用中文字体：%s' % skip)

    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGB', (320, 180), (30, 40, 50)).save(buffer, format='JPEG', quality=85)
    jpeg = buffer.getvalue()

    frames = []
    pump = freeze_pump.FreezePump(tap=frames.append, painter=painter, warn_ms=0, refresh_ms=0)
    pump.set_frame(jpeg)
    pump.hold_once(1000)

    assert pump.stats['painted'] == 1
    assert frames and frames[0] != jpeg, '贴过字的帧不可能和原帧逐字节相同'