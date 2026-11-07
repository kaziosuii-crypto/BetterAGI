# -*- coding: utf-8 -*-
'''集成：编排层 + 慢系统 + 快系统 + 时间轴 + 落账，跑一条完整离线闭环。

对应 04_acceptance.md §2 的不变量骨架：时间轴单调、意图成对、交棒一份、冻结成对、
熔断留证。全部离线（假手 / 假嘴 / 假慢系统），不碰网络、不碰游戏。

这个文件的价值不在覆盖率，而在**把各模块的接口真接一遍**：并行开发期每个模块
自己测都绿，接起来却常常对不上（例：orchestrator 曾把整包 intent dict 当字符串
传给 executor.run）。所以这里只断言「接得上」，不断言内部实现。
'''
from __future__ import annotations

import json
import time

import pytest

import mailbox as mailbox_mod
import orchestrator as orchestrator_mod
import recorder
import slow_planner
import timeline as timeline_mod
from executor import FastExecutor

N = chr(10)

OBS = (
    '## 结论' + N + '到了村口，红衣NPC说完了话。' + N
    + '## 看到了什么' + N
    + '从篝火往东走，路上过了两个火堆，没遇到敌人；村口有个穿红衣的 NPC 站在井边，'
      '我跟她说了话，对话框弹了出来，她说了一句你终于来了；手上还剩三瓶药，血条八成，'
      '旁边有个商人没去搭话；现在天还没黑，往回走有条岔路。' + N
    + '## 我现在在哪' + N + '村口井边，往东是村内，往西回篝火。'
)

PLAN_TEXT = (
    '## 这一轮做什么' + N + '往东走到村口，跟井边的红衣 NPC 说完话' + N
    + '## 就算成了' + N + '对话框里的那句话说完' + N
    + '## 别做' + N + '不要出村，不要去惹那个商人'
)


class MotorParseError(Exception):
    '''与 agent/motor.py 的 MotorParseError 同名同形（executor 按类名分流）。'''

    def __init__(self, message: str, raw_text: str = '') -> None:
        super().__init__(message)
        self.raw_text = raw_text


class FakeAdapter:
    def __init__(self) -> None:
        self.played = []

    def grab(self):
        data = b'JPEGDATA'
        # 跟真 adapter 一样走录播旁路：一次抓帧同时喂给 tap（录播）。
        tap = getattr(self, 'frame_tap', None)
        if callable(tap):
            tap(data)
        return {'data': data, 'w': 1280, 'h': 736}

    def play_steps(self, steps, step_ms=33):
        self.played.append(steps)
        return {'pressed': [k for step in steps for k in step], 'skipped_keys': []}


class FakeMotor:
    def __init__(self, steps=None, error=None) -> None:
        self.steps = steps if steps is not None else [['Z'], [], [], [], [], []]
        self.error = error
        self.calls = 0
        self.resets = 0
        self.deadlines = []

    def reset(self) -> None:
        self.resets += 1

    def decide(self, frame_url, instruction, game=None, system_prompt=None,
               deadline_ms=None, new_intent=False):
        self.calls += 1
        self.deadlines.append(deadline_ms)
        # 真手一步 ~200ms：这里睡 10ms 让解说线程有机会先喊收手，否则主循环
        # 会在一次 narrate_every_s 内把 max_decisions 刷满，测出来是 budget 而不是 reached。
        time.sleep(0.01)
        if self.error is not None:
            raise self.error
        return {'steps': [list(step) for step in self.steps], 'mouse': {},
                'raw': 'Z', 'parsed': True}


class FakeNarrator:
    '''第 done_after 次起就喊收手（带理由）；总结那一问单独回一句。'''

    def __init__(self, done_after: int = 2) -> None:
        self.calls = 0
        self.done_after = done_after
        self.pushed = 0
        self.summaries = 0

    def push(self, jpeg) -> None:
        self.pushed += 1

    def ask(self, instructions) -> str:
        if '总结' in instructions:
            self.summaries += 1
            return '这一段从篝火往东走到村口，跟红衣NPC说完了话。'
        self.calls += 1
        if self.calls >= self.done_after:
            return '[DONE] 对话已经说完，任务达成'
        return '还在往东走，画面基本没变'


class FakeSlowClient:
    def __init__(self, text: str = PLAN_TEXT) -> None:
        self.text = text
        self.calls = []

    def __call__(self, messages, thinking=False):
        self.calls.append({'messages': messages, 'thinking': thinking})
        return self.text


def _build(tmp_path, motor=None, narrator=None, slow_text: str = PLAN_TEXT):
    root = tmp_path / 'run' / 's1'      # 一场直播一个目录（02 §2 布局）
    tl = timeline_mod.Timeline(root, 's1')
    idx = recorder.SessionIndex(root, 's1', 'minecraft', 'w1', int(time.time() * 1000))
    mb = mailbox_mod.Mailbox()
    client = FakeSlowClient(slow_text)
    executor = FastExecutor(
        FakeAdapter(), motor or FakeMotor(), narrator or FakeNarrator(),
        game='minecraft', motion_instruction='用 W 往前走，用 Z 交互',
        narrate_every_s=0.005, done_confirm=2, step_ms=1,
    )
    planner = slow_planner.SlowPlanner(client, budget_ms=2000)
    orch = orchestrator_mod.Orchestrator(
        root=root, session_id='s1', game='minecraft', save_id='w1',
        timeline=tl, index=idx, mailbox=mb, executor=executor, planner=planner,
        memory=None, incident_root=tmp_path / 'run' / 'incidents',
    )
    return orch, tl, idx, mb, client


def test_two_rounds_closed_loop(tmp_path):
    orch, tl, idx, mb, client = _build(tmp_path)
    results = []
    for round_no in range(2):
        plan = orch.plan('第 %d 轮：上一轮到了村口' % round_no)
        assert plan.goal
        result = orch.execute(plan)
        results.append(result)
        orch.handoff(result, '走到了村口，跟红衣NPC说完话', OBS)

    # ---- 慢系统：两轮都被真的问了一遍 ----
    assert len(client.calls) == 2
    assert '第 0 轮' in str(client.calls[0]['messages'])

    # ---- 执行器：意图是「一句话」，不是 dict ----
    for result in results:
        assert result['result'] == 'reached'
        assert isinstance(result['plan_goal'], str)
        assert result['action_count'] >= 1
        assert len(result['decide_ms']) == result['action_count']
        assert result['slow_steps'] == 0
        assert result['intent_id'].startswith('s1-')

    # ---- 时间轴：单调、事件类型合法、意图成对、冻结成对 ----
    rows = tl.rows()
    assert rows
    stamps = [row['t_ms'] for row in rows]
    assert stamps == sorted(stamps)
    assert all(row['kind'] in timeline_mod.KINDS for row in rows)
    starts = [row for row in rows if row['kind'] == 'intent_start']
    ends = [row for row in rows if row['kind'] == 'intent_end']
    assert len(starts) == 2 and len(ends) == 2
    assert [row['intent_id'] for row in starts] == [row['intent_id'] for row in ends]
    actions = [row for row in rows if row['kind'] == 'game_action']
    assert actions
    assert all(row['intent_id'] in [s['intent_id'] for s in starts] for row in actions)
    freezes = [row for row in rows if row['kind'] == 'freeze']
    assert len([row for row in freezes if row.get('restored') is False]) == len(
        [row for row in freezes if row.get('restored') is True])

    # ---- 交棒：每意图一份，落盘可查 ----
    handoffs = [row for row in rows if row['kind'] == 'handoff']
    assert len(handoffs) == 2
    folder = tmp_path / 'run' / 's1' / 'handoffs'
    files = sorted(folder.glob('*.json'))
    assert len(files) == 2
    payload = json.loads(files[0].read_text(encoding='utf-8'))
    assert payload['result'] == 'reached'
    assert payload['conclusion']
    assert '## 看到了什么' in payload['observations']

    # ---- 索引：意图列表两行、结果都是 reached ----
    index = json.loads((tmp_path / 'run' / 's1' / 'session_index.json').read_text(encoding='utf-8'))
    assert len(index['intents']) == 2
    assert [row['result'] for row in index['intents']] == ['reached', 'reached']
    assert all(row['started_ms'] is not None for row in index['intents'])

    # ---- mailbox：两个 intent.end 事件都投进去了 ----
    assert mb.depth('intent.end') == 2
    assert orch.state == orchestrator_mod.STATE_HANDOFF


def test_fatal_parse_error_trips_and_leaves_evidence(tmp_path):
    motor = FakeMotor(error=MotorParseError('模型输出没有动作块', raw_text='<garbage>'))
    orch, tl, idx, mb, _client = _build(tmp_path, motor=motor)
    plan = orch.plan('开播第一轮')

    with pytest.raises(orchestrator_mod.OrchestratorAborted) as info:
        orch.execute(plan)

    assert info.value.kind == 'motor_parse_error'
    assert orch.state == orchestrator_mod.STATE_ABORTED

    # 留证目录必须真的在，而且不是空壳
    bundle = tmp_path / 'run' / 'incidents'
    folders = sorted(p for p in bundle.glob('*') if p.is_dir())
    assert len(folders) == 1
    names = sorted(p.name for p in folders[0].iterdir())
    assert 'README.md' in names
    assert 'parsed.json' in names
    parsed = json.loads((folders[0] / 'parsed.json').read_text(encoding='utf-8'))
    assert parsed['kind'] == 'motor_parse_error'

    # 熔断之后不许再有动作：incident 必须是这条时间轴上最后一个「操作类」事件
    rows = tl.rows()
    incident_at = [i for i, row in enumerate(rows) if row['kind'] == 'incident']
    assert len(incident_at) == 1
    after = [row['kind'] for row in rows[incident_at[0] + 1:]]
    assert 'game_action' not in after
    assert 'intent_start' not in after

    # 索引被保存（下播/重启后还能按 intent_id 查到这次事故）
    index = json.loads((tmp_path / 'run' / 's1' / 'session_index.json').read_text(encoding='utf-8'))
    assert index['intents']
    assert index['intents'][0]['result'] == 'aborted'


def test_slow_timeout_does_not_kill_the_run(tmp_path):
    '''慢系统超时是「这一轮没想出来」，不是熔断：状态退回 IDLE，可以重试。'''

    orch, tl, idx, mb, _client = _build(tmp_path)

    def _boom(messages, thinking=False):
        time.sleep(0.3)
        return PLAN_TEXT

    orch.planner = slow_planner.SlowPlanner(_boom, budget_ms=50)
    with pytest.raises(slow_planner.SlowPlannerTimeout):
        orch.plan('慢系统卡住')
    assert orch.state == orchestrator_mod.STATE_PLANNING

    orch.planner = slow_planner.SlowPlanner(FakeSlowClient(), budget_ms=2000)
    plan = orch.plan('重试')
    result = orch.execute(plan)
    orch.handoff(result, '重试之后走到了村口', OBS)
    assert orch.state == orchestrator_mod.STATE_HANDOFF
    assert [row['kind'] for row in tl.rows()].count('freeze') >= 2

