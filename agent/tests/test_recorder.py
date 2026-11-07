# -*- coding: utf-8 -*-
'''场次索引（agent/recorder.py）：意图/分片索引、孤儿分片、原子落盘。'''
from __future__ import annotations

import json
from pathlib import Path

import recorder


def _index(tmp_path: Path) -> recorder.SessionIndex:
    return recorder.SessionIndex(tmp_path, session_id='20261104-1930-01', game='eldenring', save_id='run3', t0_epoch_ms=1793000000000)


def test_intent_lifecycle(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.intent_start('int-1', '去村口找红衣NPC', started_ms=184320)
    index.intent_end('int-1', 'done', ended_ms=203980)
    index.intent_start('int-2', '打大树守卫', started_ms=210000)
    payload = index.to_dict()
    assert payload['schema_version'] == recorder.SCHEMA_VERSION
    assert payload['intents'][0] == {
        'intent_id': 'int-1',
        'goal_short': '去村口找红衣NPC',
        'started_ms': 184320,
        'ended_ms': 203980,
        'result': 'done',
    }
    assert payload['intents'][1]['ended_ms'] is None  # 没结束就是 None，不编 0


def test_intent_end_clamps_and_warns(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.intent_start('int-1', started_ms=5000)
    index.intent_end('int-1', ended_ms=1000)
    assert index.intents[0].ended_ms == 5000
    assert any('早于开始' in w for w in index.warnings)
    assert index.intent_end('nope') is None
    assert any('找不到' in w for w in index.warnings)


def test_segment_anchor_and_orphans(tmp_path: Path) -> None:
    index = _index(tmp_path)
    assert index.segment_name(3) == 'seg_003.mp4'
    index.segment_header(index.segment_name(0), start_ms=0, frame_no=0)
    index.segment_end(index.segment_name(0), end_ms=600000, frames=15000)
    index.segment_end(index.segment_name(1), end_ms=1200000)  # 没 header → orphan
    payload = index.to_dict()
    first, second = payload['segments']
    assert first['orphan'] is False and first['frames'] == 15000
    assert first['start_ms'] == 0 and first['end_ms'] == 600000
    assert second['orphan'] is True
    assert index.orphans() == ['seg_001.mp4']


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    index = _index(tmp_path)
    index.intent_start('int-1', '看看地图', started_ms=10)
    index.intent_end('int-1', 'done', ended_ms=20)
    index.segment_header('seg_000.mp4', start_ms=0, frame_no=0)
    index.segment_end('seg_000.mp4', end_ms=600000, frames=15000)
    path = index.save()
    assert path == tmp_path / 'session_index.json'
    assert not (tmp_path / 'session_index.json.tmp').exists()
    raw = json.loads(path.read_text(encoding='utf-8'))
    assert raw['session_id'] == '20261104-1930-01'

    loaded = recorder.SessionIndex.load(tmp_path)
    assert loaded.game == 'eldenring' and loaded.save_id == 'run3'
    assert loaded.intents[0].result == 'done'
    assert loaded.segments[0]['anchored'] is True
    assert loaded.to_dict() == raw


def test_no_second_clock_read(tmp_path: Path) -> None:
    '''所有毫秒都由调用方传入：模块内部不许自己取时钟。'''

    index = _index(tmp_path)
    index.intent_start('int-1', started_ms=1234)
    assert index.intents[0].started_ms == 1234
    index.segment_header('seg_000.mp4', start_ms=1234, frame_no=7)
    assert index.segments[0]['start_ms'] == 1234
