# -*- coding: utf-8 -*-
'''慢系统「直接看图」的接线测试：真帧 data URL 必须原样进慢系统请求。

覆盖三件事：
1. build_slow_messages(frames=[...]) 产出的 user content 是 image_url 段，不再有文字版画面；
2. SlowPlanner.plan 不收「客户端说自己能看图」的门控——有帧就发；
3. Streamer.latest_frames 从执行器缓存取帧并转成 data URL，编排层接口不收 frames 时不硬传。
'''

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import slow_planner  # noqa: E402
import streamer as streamer_mod  # noqa: E402


def _frame(tag: bytes) -> str:
    return 'data:image/jpeg;base64,' + base64.b64encode(tag).decode('ascii')


def test_build_messages_puts_real_frames_in_user_content():
    msgs = slow_planner.build_slow_messages('刚才在门口晃了一圈', frames=[_frame(b'A'), _frame(b'B')])
    user = msgs[1]
    assert user['role'] == 'user'
    parts = user['content']
    assert isinstance(parts, list)
    assert parts[0]['type'] == 'text'
    imgs = [p for p in parts if p['type'] == 'image_url']
    assert len(imgs) == 2
    assert imgs[0]['image_url']['url'].startswith('data:image/jpeg;base64,')
    # 文字版画面必须彻底消失：慢系统看的是图，不是快系统的转述
    assert '你现在看到的画面' not in parts[0]['text']
    assert '转述' in (slow_planner.build_slow_messages.__doc__ or '')


def test_messages_cap_frames_at_max():
    many = [_frame(bytes([i])) for i in range(8)]
    msgs = slow_planner.build_slow_messages('x', frames=many)
    imgs = [p for p in msgs[1]['content'] if p['type'] == 'image_url']
    assert len(imgs) == slow_planner.MAX_SLOW_FRAMES


def test_messages_without_frames_stay_plain_string():
    msgs = slow_planner.build_slow_messages('x')
    assert isinstance(msgs[1]['content'], str)


def test_plan_sends_frames_even_when_client_has_no_vision_flag():
    seen = {}

    def client(messages, thinking=False):
        seen['messages'] = messages
        return '## 这一轮做什么' + chr(10) + '往前走' + chr(10) + '## 就算成了' + chr(10) + '到门口' + chr(10) + '## 别做' + chr(10) + '别乱按'

    planner = slow_planner.SlowPlanner(client)
    plan = planner.plan('刚走到门口', frames=[_frame(b'A')])
    assert plan.goal == '往前走'
    imgs = [p for p in seen['messages'][1]['content'] if p['type'] == 'image_url']
    assert len(imgs) == 1


class _Exec:
    def __init__(self, frames):
        self.last_frames = list(frames)
        self.last_frame = frames[-1] if frames else None
        self.adapter = None


class _OrchNoFrames:
    '''旧版编排层：plan() 里根本没有 frames 参数，也不收 **kw。'''

    def __init__(self, executor):
        self.executor = executor
        self.state = 'PLANNING'
        self.calls = []

    def plan(self, summary, danmaku=None, history=None, constraints=None):
        self.calls.append({'danmaku': danmaku, 'history': history, 'constraints': constraints})
        return _Plan()


class _Orch:
    def __init__(self, executor, accepts_frames=True):
        self.executor = executor
        self.state = 'PLANNING'
        self.calls = []
        self._accepts = bool(accepts_frames)

    def plan(self, summary, frames=None, **kw):
        self.calls.append(dict(kw, frames=frames))
        return _Plan()


class _Plan:
    goal = '走一步'


def test_latest_frames_returns_data_urls():
    orch = _Orch(_Exec([b'aaa', b'bbb']))
    st = streamer_mod.Streamer(orchestrator=orch)
    frames = st.latest_frames()
    assert len(frames) == 2
    assert all(f.startswith('data:image/jpeg;base64,') for f in frames)
    assert frames[-1].endswith(base64.b64encode(b'bbb').decode('ascii'))


def test_latest_frames_falls_back_to_last_frame():
    orch = _Orch(_Exec([]))
    exec_ = orch.executor
    exec_.last_frame = b'only'
    st = streamer_mod.Streamer(orchestrator=orch)
    assert st.latest_frames() == [_frame(b'only')]


def test_latest_frames_empty_when_nothing_available():
    orch = _Orch(_Exec([]))
    st = streamer_mod.Streamer(orchestrator=orch)
    assert st.latest_frames() == []


def test_plan_call_passes_frames_when_supported():
    orch = _Orch(_Exec([b'A']), accepts_frames=True)
    st = streamer_mod.Streamer(orchestrator=orch)
    st._plan_call('summary', [], [_frame(b'A')])
    assert orch.calls[0]['frames'] == [_frame(b'A')]


def test_plan_call_warns_and_drops_frames_when_unsupported():
    orch = _OrchNoFrames(_Exec([b'A']))
    st = streamer_mod.Streamer(orchestrator=orch)
    st._plan_call('summary', [], [_frame(b'A')])
    assert 'frames' not in orch.calls[0]
    assert any('看不到画面' in w for w in st.warnings)
