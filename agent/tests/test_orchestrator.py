# -*- coding: utf-8 -*-
'''编排层（agent/orchestrator.py）：状态机、冻结窗口、落账、熔断。'''
from __future__ import annotations

import json

import pytest

import mailbox as mailbox_mod
import orchestrator
import recorder
import slow_planner
import timeline as timeline_mod

CONCLUSION = '走到村口找到红衣NPC并说完话'
OBSERVATIONS = ('## 结论' + chr(10) + '到了村口，红衣NPC说完了话。' + chr(10) + '## 看到了什么' + chr(10) + '这次从篝火往东走，路上过了两个火堆，没遇到敌人；村口有个穿红衣的NPC站在井边，'
                '我跟她说了话，对话框弹出来了，她说了一句「你终于来了」；'
                '手上还剩三瓶药，血条八成，旁边有个商人没去搭话；现在天还没黑，往回走有条岔路。'
                + chr(10) + '## 我现在在哪' + chr(10) + '村口井边，往东是村内，往西回篝火。')


class FakeExecutor:
    def __init__(self, ended: str = 'goal_reached', **extra) -> None:
        self.ended = ended
        self.extra = extra
        self.calls = []
        self.context = None

    def run(self, intent, done_when='', **kwargs):
        self.calls.append({'intent': intent, 'done_when': done_when, 'kwargs': kwargs})
        # 编排层通过 context_factory 每个意图新建一份手嘴通道，这里留证。
        self.context = kwargs.get('context', self.context)
        payload = {'ended': self.ended, 'reached': self.ended == 'goal_reached',
                   'total_ms': 1234, 'action_count': 6, 'decide_ms': [120, 130],
                   'stats': {'decisions': 6}, 'errors': []}
        payload.update(self.extra)
        return payload


class FakePlanner:
    def __init__(self, goal: str = '从篝火往东走到村口找红衣NPC', success: str = '画面出现对话框') -> None:
        self.calls = []
        self.goal = goal
        self.success = success

    def plan(self, report_summary, **kwargs):
        self.calls.append({'summary': report_summary, 'kwargs': kwargs})
        return slow_planner.SlowPlan(goal=self.goal, success=self.success, raw='x')


def _build(tmp_path, *, executor=None, planner=None, timeline=True):
    tl = timeline_mod.open_timeline(tmp_path, 'sess-1') if timeline else None
    index = recorder.SessionIndex(tmp_path, session_id='sess-1', game='eldenring', save_id='run3')
    box = mailbox_mod.Mailbox()
    orch = orchestrator.Orchestrator(tmp_path, 'sess-1', 'eldenring', 'run3',
                                     timeline=tl, index=index, mailbox=box,
                                     executor=executor or FakeExecutor(), planner=planner or FakePlanner())
    return orch, tl, index, box


def test_plan_freezes_picture_and_records_window(tmp_path) -> None:
    orch, tl, _, box = _build(tmp_path)
    plan = orch.plan('刚刚在篝火前站了一会')
    assert plan.ok is True
    assert orch.state == orchestrator.STATE_PLANNING
    freezes = [row for row in tl.rows() if row['kind'] == 'freeze']
    assert [row['restored'] for row in freezes] == [False, True]
    assert freezes[-1]['ms'] >= 0
    assert all(row['reason'] == 'slow_think' for row in freezes)


def test_execute_success_lands_everywhere(tmp_path) -> None:
    orch, tl, index, _ = _build(tmp_path)
    orch.plan('开场')
    result = orch.execute(orch.last_plan)
    assert result['ended'] == 'goal_reached' and result['result'] == 'reached'
    assert result['intent_id'] == 'sess-1-001'
    assert orch.blocked_count == 0
    kinds = [row['kind'] for row in tl.rows()]
    assert kinds.count('intent_start') == 1 and kinds.count('intent_end') == 1
    intents = index.to_dict()['intents']
    assert intents[0]['intent_id'] == 'sess-1-001' and intents[0]['result'] == 'reached'
    assert intents[0]['started_ms'] is not None and intents[0]['ended_ms'] is not None


def test_blocked_counts_and_feeds_thinking(tmp_path) -> None:
    orch, _, _, _ = _build(tmp_path, executor=FakeExecutor(ended='budget'))
    for _ in range(3):
        orch.plan('又卡住了')
        result = orch.execute(orch.last_plan)
        orch.handoff(result, '这一轮又没走通，还在原地', OBSERVATIONS)
    assert orch.blocked_count == 3

    thinking_calls = []

    def client(messages, thinking=False):
        thinking_calls.append(thinking)
        return '## 这一轮做什么' + chr(10) + '换个方向走' + chr(10) + '## 就算成了' + chr(10) + '看到新场景'

    orch.planner = slow_planner.SlowPlanner(client, budget_ms=500, thinking_budget_ms=500)
    plan = orch.plan('还是那个路口')
    assert thinking_calls == [True]
    assert any('thinking' in w for w in plan.warnings)


def test_handoff_writes_file_and_mailbox(tmp_path) -> None:
    orch, tl, _, box = _build(tmp_path)
    orch.plan('开场')
    result = orch.execute(orch.last_plan)
    payload = orch.handoff(result, CONCLUSION, OBSERVATIONS)
    assert payload['ok'] is True
    path = payload['file']
    data = json.loads(open(path, 'r', encoding='utf-8').read())
    assert data['intent_id'] == result['intent_id']
    assert data['result'] == 'reached'
    assert data['timing']['intent_ms'] == result['intent_ms']
    assert [row['kind'] for row in tl.rows()].count('handoff') == 1
    assert box.depth('intent.end') == 1


def test_handoff_rejects_bad_conclusion(tmp_path) -> None:
    orch, _, _, _ = _build(tmp_path)
    orch.plan('开场')
    result = orch.execute(orch.last_plan)
    with pytest.raises(Exception) as info:
        orch.handoff(result, '太短', OBSERVATIONS)
    assert 'conclusion' in str(info.value)


def test_illegal_transition_raises(tmp_path) -> None:
    orch, _, _, _ = _build(tmp_path)
    with pytest.raises(orchestrator.OrchestratorError):
        orch.execute(slow_planner.SlowPlan(goal='随便'))  # IDLE → EXECUTING 不允许


def test_overlong_task_refused(tmp_path) -> None:
    orch, _, _, _ = _build(tmp_path, planner=FakePlanner(goal='往前走' * 200))
    plan = orch.plan('卡住了')
    assert plan.needs_rewrite is True
    with pytest.raises(orchestrator.OrchestratorError) as info:
        orch.execute(plan)
    assert '退回重写' in str(info.value)


def test_motor_parse_error_trips_and_aborts(tmp_path) -> None:
    orch, tl, _, _ = _build(tmp_path,
                            executor=FakeExecutor(ended='motor_parse_error',
                                                  error_detail={'message': '解析不到动作'}))
    orch.plan('开场')
    with pytest.raises(orchestrator.OrchestratorAborted) as info:
        orch.execute(orch.last_plan)
    assert info.value.kind == 'motor_parse_error'
    assert orch.state == orchestrator.STATE_ABORTED
    last = tl.rows()[-1]
    assert last['kind'] == 'incident' and last['incident_kind'] == 'motor_parse_error'
    bundle = info.value.bundle_dir
    import os

    assert os.path.isdir(bundle)
    assert os.path.isfile(os.path.join(bundle, 'README.md'))
    assert os.path.isfile(os.path.join(bundle, 'proc.json'))
    # 熔断之后不许再有任何动作事件（断言器 A8）
    after = [row for row in tl.rows() if row['kind'] == 'game_action']
    assert after == []


def test_trip_is_idempotent_on_state(tmp_path) -> None:
    orch, tl, _, _ = _build(tmp_path)
    first = orch.trip('invariant', '不变量违反')
    assert orch.state == orchestrator.STATE_ABORTED
    assert [row['kind'] for row in tl.rows()].count('incident') == 1
    with pytest.raises(orchestrator.OrchestratorError):
        orch.trip('invariant', '再来一次')  # ABORTED 之后不许再跳
    assert first

def test_each_intent_gets_a_fresh_context(tmp_path) -> None:
    '''手嘴通道每意图一份：上一轮的「上一动作」不能带进下一轮。'''

    made = []

    def factory():
        made.append(1)
        return {'round': len(made)}

    orch, _, _, _ = _build(tmp_path)
    orch.context_factory = factory
    plan = slow_planner.SlowPlan(goal='从篝火往东走到村口', success='画面出现对话框', raw='x')
    # execute 只能从 PLANNING 进（状态机 ALLOWED 表），这里手动摆位，不测 plan 本身。
    orch._to(orchestrator.STATE_PLANNING, 'test')
    first_result = orch.execute(plan)
    first = orch.executor.context
    orch.handoff(first_result, CONCLUSION, OBSERVATIONS)
    orch._to(orchestrator.STATE_PLANNING, 'test')
    orch.execute(plan)
    assert made == [1, 1]
    assert first == {'round': 1}
    assert orch.executor.context == {'round': 2}
    assert orch.executor.context is not first


def test_stuck_is_blocked_not_failed(tmp_path) -> None:
    '''卡住 = 没进展（blocked），不是故障：不熔断、不写 incident、照常交棒。'''
    stuck_info = {'ok': True, 'stuck': True, 'why': '画面连续 12 帧几乎没变'}
    orch, tl, index, _ = _build(tmp_path, executor=FakeExecutor(ended='stuck', stuck=stuck_info))
    orch.plan('走到存档点')
    result = orch.execute(orch.last_plan)
    assert result['ended'] == 'stuck'
    assert result['result'] == 'blocked'
    assert orch.blocked_count == 1
    assert orch.state == orchestrator.STATE_EXECUTING  # 交棒由 handoff() 推进
    assert index.to_dict()['intents'][0]['result'] == 'blocked'
    assert not any(row['kind'] == 'incident' for row in tl.rows())
