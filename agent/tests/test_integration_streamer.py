# -*- coding: utf-8 -*-
'''集成：总装层 Streamer + 编排 + 慢系统 + 手嘴 + 每意图一个快系统会话 + 落账。

与 test_integration_loop.py 的分工：那个测「编排层接得上执行器」，这个测
「一场直播的回合循环接得上快系统会话与记忆」——尤其是：

* 一个意图 = 一个 Omni 会话（start/close 成对，第二个意图不会复用第一个的连接）；
* 上一轮的交棒结论真的进了下一轮慢系统的输入（report_summary 链式传递）；
* 落账真的写进记忆（每轮一条 episode，带 intent_id 与 result）；
* 下播四件事里，钩子文件与记忆都真的落盘。
'''
from __future__ import annotations

import json
import types

import pytest

import fast_session
import omni
import orchestrator as orchestrator_mod
import slow_planner
import timeline as timeline_mod
import recorder
import mailbox as mailbox_mod
import streamer as streamer_mod
from executor import FastExecutor

from test_integration_loop import (
    FakeAdapter,
    FakeMotor,
    FakeNarrator,
    FakeSlowClient,
    OBS,
    PLAN_TEXT,
)


class FakeOmniClient:
    instances: list = []

    def __init__(self, config):
        self.config = config
        self.closed = False
        self.seeded = 0
        FakeOmniClient.instances.append(self)

    async def connect(self):
        return None

    async def close(self):
        self.closed = True

    async def cancel_inflight(self):
        return None

    async def seed_audio(self):
        self.seeded += 1

    async def send_frame(self, frame):
        return None

    async def commit(self):
        return None

    async def create_response(self, instructions):
        return None

    async def collect(self, *args, **kwargs):
        return types.SimpleNamespace(text='画面里还是那条往东的土路')


class MemoryStub:
    '''只实现编排层用到的那几个方法：检索返回空、写回真的留下 episode。'''

    def __init__(self):
        self.episodes = []

    def sanitize_group_id(self, value):
        return str(value or '')

    def search(self, *args, **kwargs):
        return []

    def add_episode(self, episode):
        self.episodes.append(episode)
        return 'e%d' % len(self.episodes)


def _build(tmp_path, monkeypatch):
    FakeOmniClient.instances = []
    monkeypatch.setattr(omni, 'OmniClient', FakeOmniClient)

    root = tmp_path / 'run' / 's1'
    tl = timeline_mod.Timeline(root, 's1')
    idx = recorder.SessionIndex(root, 's1', 'minecraft', 'w1', 0)
    mb = mailbox_mod.Mailbox()
    client = FakeSlowClient(PLAN_TEXT)
    memory = MemoryStub()
    executor = FastExecutor(
        FakeAdapter(), FakeMotor(), FakeNarrator(),
        game='minecraft', motion_instruction='用 W 往前走，用 Z 交互',
        narrate_every_s=0.005, done_confirm=2, step_ms=1,
    )
    planner = slow_planner.SlowPlanner(client, budget_ms=2000)
    orch = orchestrator_mod.Orchestrator(
        root=root, session_id='s1', game='minecraft', save_id='w1',
        timeline=tl, index=idx, mailbox=mb, executor=executor, planner=planner,
        memory=memory, incident_root=tmp_path / 'run' / 'incidents',
    )
    session = fast_session.IntentFastSession(fps=4.0, pump=False)
    node = streamer_mod.Streamer(orchestrator=orch, session=session, root=root,
                                 constraints=['别剧透：只能依据记忆里已知的内容'])
    return node, orch, tl, idx, client, memory, session


def test_two_rounds_one_session_each(tmp_path, monkeypatch):
    node, orch, tl, idx, client, memory, session = _build(tmp_path, monkeypatch)

    first = node.round()
    second = node.round()

    assert first['ok'] and second['ok']
    # 每意图一个新会话：连了两次、且第一个会话真的被关掉了
    assert session.session_opens == 2
    assert len(FakeOmniClient.instances) == 2
    assert FakeOmniClient.instances[0].closed is True
    assert first['session']['session_opens'] == 1

    # 上一轮的结论真的进了下一轮慢系统的输入
    assert len(client.calls) == 2
    assert first['conclusion'] in str(client.calls[1]['messages'])

    # 落账：每轮一条 episode，带 intent_id 与 result
    assert len(memory.episodes) == 2
    metas = [dict(getattr(e, 'meta', {}) or {}) for e in memory.episodes]
    assert [m.get('result') for m in metas] == ['reached', 'reached']
    assert all(m.get('intent_id') for m in metas)

    # 时间轴：意图成对、t_ms 单调
    rows = tl.rows()
    assert [r['t_ms'] for r in rows] == sorted(r['t_ms'] for r in rows)
    assert len([r for r in rows if r['kind'] == 'intent_start']) == 2
    assert len([r for r in rows if r['kind'] == 'intent_end']) == 2

    # 交棒文件两份，都是合法 JSON
    files = sorted((tmp_path / 'run' / 's1' / 'handoffs').glob('*.json'))
    assert len(files) == 2
    for path in files:
        payload = json.loads(path.read_text(encoding='utf-8'))
        assert payload['result'] == 'reached'
        assert 8 <= len(payload['conclusion']) <= 60


def test_signoff_writes_hook_and_memory(tmp_path, monkeypatch):
    node, orch, tl, idx, client, memory, session = _build(tmp_path, monkeypatch)
    node.round()

    out = node.signoff(summary='今天在村口跟红衣NPC说完了话，明天进村', hook='明天进村找商人')

    assert out['hook_file'].endswith('next_hook.json')
    hook = json.loads((tmp_path / 'run' / 's1' / 'next_hook.json').read_text(encoding='utf-8'))
    assert hook['hook'] == '明天进村找商人'
    assert out['index']['saved'] is True
    # 下播记忆：一轮的执行落账 + 一场的总结 = 两条
    assert len(memory.episodes) == 2


def test_abort_stops_the_loop(tmp_path, monkeypatch):
    from test_integration_loop import MotorParseError

    node, orch, tl, idx, client, memory, session = _build(tmp_path, monkeypatch)
    orch.executor.motor = FakeMotor(error=MotorParseError('模型输出没有动作块', raw_text='<x>'))

    summary = node.run(rounds=3)

    assert summary['aborted'] is True
    assert summary['round_count'] == 1
    assert orch.state == orchestrator_mod.STATE_ABORTED
    # 嘴的会话必须被关掉：熔断不等于可以漏连接
    assert all(c.closed for c in FakeOmniClient.instances)
