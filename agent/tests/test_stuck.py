# -*- coding: utf-8 -*-
'''卡住检测（agent/stuck.py）：看不清时不判卡住、重复动作+画面不动才交棒。'''
import io

import pytest
from PIL import Image

import stuck


def frame(color=0, noise=False, size=(64, 36)) -> bytes:
    img = Image.new('RGB', size, (color, color, color))
    if noise:
        pixels = img.load()
        for x in range(size[0]):
            for y in range(size[1]):
                pixels[x, y] = ((x * 37 + y * 11) % 256, (x * 17) % 256, (y * 29) % 256)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=90)
    return buf.getvalue()


def test_static_frames_eventually_count_as_stuck():
    detector = stuck.StuckDetector(window=4, min_change=1.5)
    jpeg = frame(30)
    results = [detector.observe(jpeg, 'W') for _ in range(6)]
    assert results[0]['stuck'] is False
    assert results[-1]['stuck'] is True
    assert '画面连续' in results[-1]['why']
    assert results[-1]['static_streak'] >= 4
    assert detector.stats['stuck'] >= 1


def test_changing_frames_are_not_stuck():
    detector = stuck.StuckDetector(window=3, min_change=1.0)
    for i in range(8):
        out = detector.observe(frame(10 + i * 20))
        assert out['stuck'] is False
    assert detector.stats['static_frames'] == 0


def test_unreadable_frame_is_never_stuck():
    detector = stuck.StuckDetector(window=2)
    out = detector.observe(b'not a jpeg at all')
    assert out['ok'] is False
    assert out['stuck'] is False
    assert detector.stats['unknown'] == 1
    # 半截真 JPEG 也一样：看不清就不判断。
    half = frame(40)[:40]
    assert detector.observe(half)['stuck'] is False


def test_repeated_action_plus_static_uses_repeat_reason():
    detector = stuck.StuckDetector(window=12, min_change=1.5, min_repeats=2)
    jpeg = frame(60)
    out = {}
    for _ in range(6):
        out = detector.observe(jpeg, 'W ; W ; W')
    assert out['stuck'] is True
    assert '同一套动作重复了' in out['why']


def test_repeated_action_with_moving_camera_is_not_stuck():
    detector = stuck.StuckDetector(window=3, min_change=1.0, min_repeats=2)
    for i in range(8):
        out = detector.observe(frame(10 + i * 25), 'W ; W ; W')
        assert out['stuck'] is False


def test_reset_clears_streaks():
    detector = stuck.StuckDetector(window=4)
    jpeg = frame(0)
    for _ in range(5):
        detector.observe(jpeg)
    assert detector.last['stuck'] is True
    detector.reset()
    assert detector.observe(jpeg)['static_streak'] == 0
    assert detector.last['stuck'] is False


def test_signature_and_distance():
    assert stuck.signature(b'') is None
    a = stuck.signature(frame(0))
    b = stuck.signature(frame(0))
    c = stuck.signature(frame(200))
    assert a and b and c
    assert stuck.distance(a, b) == 0
    assert stuck.distance(a, c) > 50
    assert stuck.distance(a, None) is None
    assert stuck.distance(a, a[:-1]) is None


def test_build_stuck_without_pillow_returns_none(monkeypatch):
    monkeypatch.setattr(stuck, '_pil', lambda: None)
    assert stuck.build_stuck() is None


def test_observe_swallows_signature_errors(monkeypatch):
    detector = stuck.StuckDetector(window=2)

    def boom(*_args, **_kwargs):
        raise RuntimeError('decode exploded')

    monkeypatch.setattr(stuck, 'signature', boom)
    out = detector.observe(frame(0))
    assert out['stuck'] is False
    assert detector.stats['errors'] == 1
    assert 'decode exploded' in detector.stats['last_error']