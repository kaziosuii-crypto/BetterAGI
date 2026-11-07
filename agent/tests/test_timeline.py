# -*- coding: utf-8 -*-
'''时间轴模块单测（agent/timeline.py）：单调性、jitter 留痕、播出视图导出、分片索引。'''
from __future__ import annotations

import json
from pathlib import Path

import pytest

import timeline


class FakeClock:
    '''按脚本推进的毫秒时钟。'''

    def __init__(self, values=None) -> None:
        self.values = list(values or [])
        self.now = 0

    def now_ms(self) -> int:
        if self.values:
            self.now = self.values.pop(0)
        return self.now


def _tl(tmp_path: Path, clock=None) -> timeline.Timeline:
    return timeline.Timeline(root=tmp_path, session_id='run-test', clock=clock)


def test_event_written_with_seq_and_schema(tmp_path: Path) -> None:
    tl = _tl(tmp_path, FakeClock([100]))
    assert tl.event('intent_start', intent_id='i1') == 100
    rows = tl.rows()
    assert len(rows) == 1
    assert rows[0]['kind'] == 'intent_start'
    assert rows[0]['v'] == timeline.SCHEMA_VERSION
    assert rows[0]['seq'] == 1
    assert rows[0]['intent_id'] == 'i1'


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    tl = _tl(tmp_path)
    with pytest.raises(ValueError):
        tl.event('not_a_kind')


def test_clock_regression_is_clamped_and_recorded(tmp_path: Path) -> None:
    tl = _tl(tmp_path, FakeClock([300, 150]))
    tl.event('speech', text='a')
    t2 = tl.event('speech', text='b')
    assert t2 == 300  # clamp 到上一刻
    assert tl.jitter and tl.jitter[0]['wanted'] == 150
    assert [r['t_ms'] for r in tl.rows()] == [300, 300]


def test_export_viewer_keeps_only_broadcast_events(tmp_path: Path) -> None:
    tl = _tl(tmp_path, FakeClock([10, 20, 30, 40]))
    tl.event('intent_start')
    tl.speech('你好', dur_ms=800)
    tl.game_action(steps=[[1, 2]])
    tl.event('l2d', motion='smile')
    path = tl.export_viewer()
    kinds = [json.loads(line)['kind'] for line in path.read_text(encoding='utf-8').splitlines()]
    assert kinds == ['speech', 'l2d']


def test_segment_index_follows_ten_minutes(tmp_path: Path) -> None:
    tl = _tl(tmp_path)
    assert tl.segment_index(0) == 0
    assert tl.segment_index(timeline.SEGMENT_MS - 1) == 0
    assert tl.segment_index(timeline.SEGMENT_MS) == 1


def test_open_timeline_writes_segment_header(tmp_path: Path) -> None:
    tl = timeline.open_timeline(tmp_path, 'run-open')
    rows = tl.rows()
    assert rows[0]['kind'] == 'segment_header'
    assert rows[0]['segment'] == 0


def test_closed_timeline_refuses_writes(tmp_path: Path) -> None:
    tl = _tl(tmp_path)
    tl.event('speech', text='x')
    tl.close()
    with pytest.raises(RuntimeError):
        tl.event('speech', text='y')
