# -*- coding: utf-8 -*-
'''入口装配的验收：不联网、不开游戏窗口，也能把「profile -> 零件 -> 一回合」接起来。'''

from __future__ import annotations

import json
from pathlib import Path

import pytest

import adapter as adapter_mod
import danmaku_feed
import fast_session
import memory as memory_mod
import motor as motor_mod
import run_stream
import slow_planner

from test_integration_loop import FakeAdapter, FakeMotor, FakeNarrator, FakeSlowClient, PLAN_TEXT


class FakeProfile:
    def __init__(self, root: Path):
        self.dir = root
        self.game_id = 'minecraft'
        self.display_name = '我的世界'
        self.group_id = 'minecraft'

    def load_config(self):
        return {}


class FakeSession(FakeNarrator):
    def __init__(self):
        super().__init__(done_after=2)
        self.starts = 0
        self.closes = 0

    def start(self):
        self.starts += 1

    def close_intent(self):
        self.closes += 1

    def stats(self):
        return {'session_opens': self.starts, 'reconnects': 0, 'degraded': False}


class MotorStub(FakeMotor):
    def health(self):
        return {'ok': True, 'model': 'gamescaling-stub'}


class MemoryStub:
    def __init__(self):
        self.episodes = []

    def sanitize_group_id(self, value):
        return str(value or '')

    def search(self, *args, **kwargs):
        return []

    def add_episode(self, episode):
        self.episodes.append(episode)
        return 'e%d' % len(self.episodes)


class FeedStub:
    def __init__(self):
        self.calls = []

    def fetch(self, since_ms, limit=50):
        self.calls.append(since_ms)
        return type('S', (), {'items': [{'t_ms': 500, 'text': '主播走走看'}]})()

    def health(self):
        return {'ok': True, 'items': 1}


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    profile = FakeProfile(tmp_path / 'profiles' / 'minecraft')
    profile.dir.mkdir(parents=True)
    session = FakeSession()
    memory = MemoryStub()
    feed = FeedStub()

    monkeypatch.setattr(adapter_mod, 'ProfileAdapter', lambda profile: FakeAdapter())
    monkeypatch.setattr(motor_mod, 'build_motor', lambda: MotorStub())
    monkeypatch.setattr(fast_session, 'build_intent_session', lambda **kwargs: session)
    monkeypatch.setattr(memory_mod, 'build_memory', lambda profile, **kwargs: memory)
    monkeypatch.setattr(slow_planner, 'build_slow_planner',
                        lambda client=None, **kwargs: slow_planner.SlowPlanner(FakeSlowClient(PLAN_TEXT), budget_ms=2000))
    monkeypatch.setattr(danmaku_feed, 'build_feed', lambda **kwargs: feed)
    return profile, session, memory, feed


def test_assemble_wires_all_parts(tmp_path, wired):
    profile, session, memory, feed = wired
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s1')

    assert parts['session_id'] == 's1'
    assert Path(parts['root']).name == 's1'
    assert parts['session'] is session
    assert parts['memory'] is memory
    assert parts['game'] == 'minecraft'
    assert parts['orchestrator'].executor is parts['executor']
    assert parts['orchestrator'].planner is parts['planner']
    assert parts['streamer'].feed is feed
    assert 'prompt_source' in parts


def test_round_runs_through_assembled_stack(tmp_path, wired):
    profile, session, memory, feed = wired
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s1')

    outcome = parts['streamer'].round()

    assert outcome['ok'] is True
    assert outcome['danmaku'] == ['主播走走看']
    assert session.starts == 1 and session.closes == 1
    # 落账：执行器完成一轮 -> handoff 写记忆
    assert len(memory.episodes) == 1
    payload = json.loads((tmp_path / 'runs' / 's1' / 'timeline.jsonl')
                         .read_text(encoding='utf-8').splitlines()[0])
    assert payload['kind'] in ('freeze', 'intent_start')
    files = list((tmp_path / 'runs' / 's1' / 'handoffs').glob('*.json'))
    assert len(files) == 1


def test_no_session_still_plays(tmp_path, wired):
    profile, session, memory, feed = wired
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s2',
                                with_session=False)

    assert parts['session'] is None
    outcome = parts['streamer'].round()
    assert outcome['ok'] is True
    assert session.starts == 0




class RecordingStub:
    def __init__(self, root, **kwargs):
        self.root = Path(root)
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.frames = []

    def start(self):
        self.started += 1
        return {'ok': True}

    def write(self, data):
        self.frames.append(len(data))
        return True

    def stop(self, timeout=10.0):
        self.stopped += 1
        return {'ok': True, 'frames': len(self.frames), 'segments': 1}


def test_record_taps_adapter_and_stops_at_signoff(tmp_path, wired, monkeypatch):
    import record as record_mod

    profile, session, memory, feed = wired
    monkeypatch.setattr(record_mod, 'SegmentWriter', RecordingStub)

    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s3', record=True)
    writer = parts['recorder']
    assert writer.started == 1
    assert callable(parts['adapter'].frame_tap)

    outcome = parts['streamer'].round()
    assert outcome['ok'] is True
    assert writer.frames, '执行器的每一帧都应该喂给录播'

    result = run_stream.run(profile, rounds=1, root=tmp_path / 'runs', record=True)
    assert result['record']['ok'] is True


def test_record_not_started_by_default(tmp_path, wired):
    profile, session, memory, feed = wired
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s4')
    assert parts['recorder'] is None
    # 不开录播也要抓帧：冻结泵靠最后一帧撑住慢系统思考期，没有它冻结时无帧可喂。
    assert parts['pump'].tap is None
    assert parts['adapter'].frame_tap == parts['pump'].set_frame

def test_resolve_profile_from_directory(tmp_path):
    import profiles

    root = tmp_path / 'profiles'
    (root / 'undertale').mkdir(parents=True)
    (root / 'registry.json').write_text(json.dumps({
        'games': {'undertale': {'display_name': '传说之下', 'aliases': ['ut']}},
    }), encoding='utf-8')

    profile = run_stream._resolve_profile(str(root / 'undertale'))

    assert isinstance(profile, profiles.Profile)
    assert profile.game_id == 'undertale'
    assert profile.display_name == '传说之下'
    assert profile.dir == root / 'undertale'


def test_dry_run_reports_preflight(tmp_path, wired):
    profile, session, memory, feed = wired
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s3')
    report = parts['streamer'].preflight()
    data = report.to_dict()
    assert 'checks' in data
    names = [row['name'] for row in data['checks']]
    assert any('便利贴' in name for name in names)
    assert any('弹幕' in name for name in names)
    assert any('记忆' in name for name in names)
    assert any('编码器' in name for name in names)
    assert any('游戏窗口' in name for name in names)
    assert any('快系统' in name for name in names)


def test_preflight_covers_design_six_items(tmp_path, wired):
    '''设计写的六项必须都在清单里；「没查」只能标跳过，不能伪装通过。'''

    profile, session, memory, feed = wired
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s4')
    data = parts['streamer'].preflight().to_dict()
    names = [row['name'] for row in data['checks']]
    for expected in ('游戏窗口', '快系统', '弹幕服务', '便利贴', '记忆', '磁盘', '编码器'):
        assert expected in names, (expected, names)
    window = [row for row in data['checks'] if row['name'] == '游戏窗口'][0]
    assert window['elapsed_ms'] >= 0

def test_pump_takes_frames_and_covers_plan_window(tmp_path, wired, monkeypatch):
    import record as record_mod

    profile, session, memory, feed = wired
    monkeypatch.setattr(record_mod, 'SegmentWriter', RecordingStub)
    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s5',
                                record=True, freeze_text='大肥鱼CPU烧了')
    pump = parts['pump']
    assert pump is parts['streamer'].pump
    assert callable(parts['adapter'].frame_tap)
    # 直播里采集是连续跑的：规划前手上已经有最后一帧，冻结期才有帧可喂。
    parts['adapter'].grab()

    outcome = parts['streamer'].round()

    assert outcome['ok'] is True
    # 采集旁路的每一帧都过 pump（冻结时才有最后一帧可喂）
    assert pump.last_frame == b'JPEGDATA'
    # plan 窗口里真的喂过帧（不是空转的装饰）
    assert outcome['freeze']['frames'] >= 1
    assert 'elapsed_ms' in outcome['freeze']


def test_pump_and_recorder_share_one_frame_tap(tmp_path, wired, monkeypatch):
    import record as record_mod

    profile, session, memory, feed = wired
    monkeypatch.setattr(record_mod, 'SegmentWriter', RecordingStub)

    parts = run_stream.assemble(profile, root=tmp_path / 'runs', session_id='s6',
                                record=True)
    parts['streamer'].round()

    # 一次抓帧同时喂录播和冻结泵：录播拿到帧，泵也拿到同一张
    assert parts['recorder'].frames
    assert parts['pump'].last_frame == b'JPEGDATA'
