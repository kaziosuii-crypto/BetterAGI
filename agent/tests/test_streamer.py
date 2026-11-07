# -*- coding: utf-8 -*-
'''总装层的验收：回合真的走完、会话真的每意图一开一关、失败不被吞掉。'''

from __future__ import annotations

import json
from pathlib import Path

import pytest

import streamer as st


class FakePlan:
    def __init__(self, goal='走到存档点', success='看到存档图标', avoid='别打怪'):
        self.goal = goal
        self.success = success
        self.avoid = avoid
        self.needs_rewrite = False
        self.warnings = []


class FakeOrch:
    def __init__(self):
        self.calls = []
        self.state = 'IDLE'
        self.session_id = 'run-1'
        self.game = 'mc'
        self.save_id = 'save-1'
        self.root = Path('run/run-1')
        self.plan_error = None
        self.exec_error = None
        self.handoff_error = None
        self.memory = object()
        self.index = FakeIndex()

    def plan(self, summary, **kwargs):
        self.calls.append(('plan', summary, kwargs))
        if self.plan_error:
            raise self.plan_error
        self.state = 'PLANNING'
        return FakePlan()

    def execute(self, plan, **kwargs):
        self.calls.append(('execute', plan.goal, kwargs))
        if self.exec_error:
            raise self.exec_error
        self.state = 'EXECUTING'
        return {
            'intent_id': 'i-001',
            'result': 'reached',
            'ended': 'goal_reached',
            'summary': '走到存档点并成功存档',
            'commentary': [{'t_ms': 120, 'text': '左边有个箱子，绕过去'}],
            'action_count': 3,
            'total_ms': 900,
            'decide_ms': [180, 190, 175],
            'slow_steps': 0,
            'errors': [],
        }

    def handoff(self, result, conclusion, observations, stats=None):
        self.calls.append(('handoff', conclusion, observations, stats))
        if self.handoff_error:
            raise self.handoff_error
        return {'ok': True, 'file': 'handoffs/i-001.json'}

    def remember(self, **kwargs):
        self.calls.append(('remember', kwargs))
        return {'stored': True, 'id': 'e1'}


class FakeIndex:
    def __init__(self):
        self.saved = 0

    def save(self):
        self.saved += 1

    def orphans(self):
        return []


class FakeSession:
    def __init__(self):
        self.starts = 0
        self.closes = 0
        self.fail_start = False
        self.stats_override = {}

    def start(self):
        if self.fail_start:
            raise RuntimeError('连不上')
        self.starts += 1

    def close_intent(self):
        self.closes += 1

    def stats(self):
        base = {'session_opens': self.starts, 'reconnects': 0, 'degraded': False}
        base.update(self.stats_override)
        return base


class FakeFeed:
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.since = []
        self.fail = False

    def fetch(self, since_ms, limit=50):
        self.since.append(since_ms)
        if self.fail:
            raise RuntimeError('弹幕服务 500')
        items = self.batches.pop(0) if self.batches else []
        return type('S', (), {'items': items})()


def test_happy_round_wires_everything():
    orch = FakeOrch()
    session = FakeSession()
    node = st.Streamer(orchestrator=orch, session=session, constraints=['别剧透'])

    outcome = node.round()

    assert outcome['ok'] is True
    assert outcome['stage'] == 'handoff'
    assert session.starts == 1 and session.closes == 1
    kinds = [c[0] for c in orch.calls]
    assert kinds == ['plan', 'execute', 'handoff']
    plan_call = orch.calls[0]
    assert plan_call[2]['constraints'] == ['别剧透']
    assert plan_call[2]['history'] is None
    assert 8 <= len(outcome['conclusion']) <= 60
    assert outcome['observations_chars'] >= 120
    assert len(node.history) == 1
    assert node.last_report
    assert json.loads(node.last_report)['结论'] == outcome['conclusion']


def test_danmaku_cursor_only_new_items():
    feed = FakeFeed([[
        {'t_ms': 1000, 'text': '主播走走看'},
        {'t_ms': 1200, 'text': '左边左边'},
    ], [{'t_ms': 1500, 'text': '你死了'}]])

    node = st.Streamer(orchestrator=FakeOrch(), feed=feed)
    first = node.round()
    second = node.round()

    assert first['danmaku'] == ['主播走走看', '左边左边']
    assert second['danmaku'] == ['你死了']
    assert feed.since == [0, 1200]


def test_feed_failure_does_not_block_round():
    feed = FakeFeed()
    feed.fail = True
    node = st.Streamer(orchestrator=FakeOrch(), feed=feed)
    outcome = node.round()
    assert outcome['ok'] is True


def test_plan_failure_keeps_session_closed():
    orch = FakeOrch()
    orch.plan_error = RuntimeError('慢系统 2s 预算用完')
    session = FakeSession()
    node = st.Streamer(orchestrator=orch, session=session)

    outcome = node.round()

    assert outcome['ok'] is False
    assert outcome['stage'] == 'plan'
    assert '预算' in outcome['error']
    assert session.starts == 0


def test_execute_failure_reports_abort_and_closes_session():
    orch = FakeOrch()
    error = RuntimeError('手模型解析失败')
    error.bundle_dir = 'incidents/20261104_i-001'
    orch.exec_error = error
    session = FakeSession()
    node = st.Streamer(orchestrator=orch, session=session)

    outcome = node.round()

    assert outcome['ok'] is False
    assert outcome['stage'] == 'execute'
    assert outcome['aborted'] is True
    assert outcome['bundle_dir'].endswith('i-001')
    assert session.closes == 1


def test_session_start_failure_is_a_warning_not_a_stop():
    orch = FakeOrch()
    session = FakeSession()
    session.fail_start = True
    node = st.Streamer(orchestrator=orch, session=session)

    outcome = node.round()

    assert outcome['ok'] is True
    assert any('快系统会话没开起来' in w for w in node.warnings)


def test_conclusion_falls_back_to_human_phrase():
    text = st.build_conclusion({'summary': '短', 'ended': 'budget'}, FakePlan(goal='走到存档点'))
    assert 8 <= len(text) <= 60
    assert '时间用完' in text
    assert '存档点' in text


def test_observations_padded_to_minimum():
    body = st.build_observations({})
    assert len(body) >= st.OBSERVATIONS_MIN
    assert '本轮没留下更多解说' in body


def test_observations_trimmed_to_maximum():
    result = {'commentary': [{'t_ms': i, 'text': 'x' * 60} for i in range(80)]}
    body = st.build_observations(result)
    assert len(body) <= st.OBSERVATIONS_MAX


def test_fast_system_handoff_reason_becomes_conclusion():
    orch = FakeOrch()
    session = FakeSession()
    session.stats_override = {
        'handoff_reason': '箱子已经打开了，接下来该看主线',
        'handoff_requested': True,
        'events': 2,
    }
    node = st.Streamer(orchestrator=orch, session=session)

    outcome = node.round()

    assert outcome['conclusion'] == '箱子已经打开了，接下来该看主线'
    handoff_call = [c for c in orch.calls if c[0] == 'handoff'][0]
    assert '快系统交棒时说的理由' in handoff_call[2]
    tools = handoff_call[3]['tools']
    assert tools['handoff_requested'] is True and tools['events'] == 2


def test_conclusion_hint_ignored_when_summary_is_good():
    short = '这条理由太短'
    assert st.build_conclusion({'summary': '走到存档点并成功存档'}, None,
                               hint=short) == '走到存档点并成功存档'
    assert st.build_conclusion({}, None, hint='') .startswith('这一轮')

def test_run_stops_on_abort():
    orch = FakeOrch()
    error = RuntimeError('解析失败')
    error.bundle_dir = 'incidents/x'
    orch.exec_error = error
    node = st.Streamer(orchestrator=orch)

    summary = node.run(rounds=3)

    assert summary['aborted'] is True
    assert summary['stopped_at'] == 1
    assert summary['round_count'] == 1
    assert summary['ok'] is False


def test_run_multiple_rounds(tmp_path):
    orch = FakeOrch()
    orch.root = tmp_path / 'run-1'
    node = st.Streamer(orchestrator=orch, root=orch.root)

    summary = node.run(rounds=2)

    assert summary['round_count'] == 2
    assert summary['ok'] is True
    assert len(node.history) == 2


def test_signoff_four_things(tmp_path):
    orch = FakeOrch()
    orch.root = tmp_path / 'run-1'
    node = st.Streamer(orchestrator=orch, root=orch.root)
    node.round()

    out = node.signoff(summary='今天在存档点前面摔了三次，最后走通了', hook='下一场去地牢', lines=['大家晚安'])

    assert out['memory']['stored'] is True
    assert out['index']['saved'] is True
    assert orch.index.saved == 1
    assert out['lines'] == ['大家晚安']
    hook = json.loads(Path(out['hook_file']).read_text(encoding='utf-8'))
    assert hook['hook'] == '下一场去地牢'
    assert not list(Path(tmp_path / 'run-1').glob('*.tmp'))


def test_signoff_without_prepared_lines():
    node = st.Streamer(orchestrator=FakeOrch())
    out = node.signoff()
    assert out['lines'] == ['（没有准备下播话术）']

def _sink(tmp_path, box):
    import event_sink

    return event_sink.EventSink(box, root=str(tmp_path))


def test_round_drains_event_bus(tmp_path):
    '''事件只写不读等于没有总线：每轮必须把 mailbox 抽干并留证。'''

    import mailbox as mailbox_mod

    box = mailbox_mod.Mailbox()
    orch = FakeOrch()
    node = st.Streamer(orchestrator=orch, session=FakeSession(), sink=_sink(tmp_path, box))
    box.put('state.change', {'from': 'IDLE', 'to': 'PLANNING'})
    box.put('intent.end', {'intent_id': 'i-001', 'result': 'reached'})

    outcome = node.round()

    assert outcome['events'] == 2
    assert outcome['event_types'] == ['intent.end', 'state.change']
    assert box.depth('state.change') == 0 and box.depth('intent.end') == 0
    lines = (tmp_path / 'events.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])['payload']['result'] == 'reached'


def test_incident_event_lands_in_warnings(tmp_path):
    import mailbox as mailbox_mod

    box = mailbox_mod.Mailbox()
    node = st.Streamer(orchestrator=FakeOrch(), sink=_sink(tmp_path, box))
    box.put('incident', {'kind': 'motor_parse_error', 'reason': '回复被截断'})

    outcome = node.round()

    assert outcome['event_types'] == ['incident']
    assert any('motor_parse_error' in w for w in node.warnings)


def test_run_totals_events_and_sink_snapshot(tmp_path):
    import mailbox as mailbox_mod

    box = mailbox_mod.Mailbox()
    sink = _sink(tmp_path, box)
    node = st.Streamer(orchestrator=FakeOrch(), session=FakeSession(), sink=sink)
    for _ in range(2):
        box.put('state.change', {'to': 'EXECUTING'})

    summary = node.run(rounds=2)

    assert summary['round_count'] == 2
    assert summary['events'] == 2
    assert summary['sink']['cursor'] == 2
    assert summary['sink']['counts'] == {'state.change': 2}


def test_without_sink_events_are_zero(tmp_path):
    node = st.Streamer(orchestrator=FakeOrch())
    outcome = node.round()
    assert outcome['events'] == 0
    assert node.sink is None


# --------------------------------------------------------------- 冻结泵接线

class FakeHold:
    def __init__(self, pump: 'FakePump') -> None:
        self.pump = pump

    def __enter__(self) -> 'FakeHold':
        # 真泵的计数是在窗口内喂帧时累加的，这里在进入时就记，模拟同样语义。
        self.pump.stats['holds'] += 1
        self.pump.stats['frames'] += self.pump.frames_per_hold
        self.pump.stats['painted'] += self.pump.painted_per_hold
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def summary(self):
        return {'elapsed_ms': self.pump.elapsed_ms, 'pumped': True}


class FakePump:
    '''只回答 available()/holding()/stats，真泵的行为在 test_freeze_pump.py 里测。'''

    def __init__(self, elapsed_ms=1500, frames_per_hold=6, painted_per_hold=0):
        self.elapsed_ms = elapsed_ms
        self.frames_per_hold = frames_per_hold
        self.painted_per_hold = painted_per_hold
        self.stats = {'holds': 0, 'frames': 0, 'painted': 0, 'errors': 0}

    def available(self):
        return True

    def holding(self):
        return FakeHold(self)


def test_plan_window_is_covered_by_freeze_pump():
    pump = FakePump(elapsed_ms=1500, frames_per_hold=6, painted_per_hold=2)
    orch = FakeOrch()
    node = st.Streamer(orchestrator=orch, pump=pump)

    outcome = node.round()

    assert outcome['ok'] is True
    assert pump.stats['holds'] == 1, '规划期必须正好开一次冻结窗口'
    assert outcome['freeze']['elapsed_ms'] == 1500
    assert outcome['freeze']['frames'] == 6
    assert outcome['freeze']['painted'] == 2
    # 冻结只包住 plan，不包 execute（执行期画面是活的）
    assert [call[0] for call in orch.calls] == ['plan', 'execute', 'handoff']


def test_freeze_longer_than_warn_without_paint_warns():
    pump = FakePump(elapsed_ms=1200, painted_per_hold=0)
    node = st.Streamer(orchestrator=FakeOrch(), pump=pump)

    node.round()

    assert any('没贴图' in w for w in node.warnings), node.warnings


def test_freeze_stats_do_not_leak_between_rounds():
    pump = FakePump(elapsed_ms=1500, painted_per_hold=1)
    node = st.Streamer(orchestrator=FakeOrch(), pump=pump)

    first = node.round()
    second = node.round()

    assert first['freeze']['painted'] == 1
    assert second['freeze']['painted'] == 1, '第二轮要报增量，不是累计'
    assert pump.stats['holds'] == 2


def test_plan_failure_still_reports_freeze_window():
    pump = FakePump(elapsed_ms=800)
    orch = FakeOrch()
    orch.plan_error = slow_planner_timeout()
    node = st.Streamer(orchestrator=orch, pump=pump)

    outcome = node.round()

    assert outcome['ok'] is False and outcome['stage'] == 'plan'
    assert outcome['freeze']['elapsed_ms'] == 800
    assert pump.stats['holds'] == 1


def slow_planner_timeout():
    import slow_planner

    return slow_planner.SlowPlannerTimeout(waited_ms=2001, budget_ms=2000)


def test_without_pump_freeze_is_empty():
    node = st.Streamer(orchestrator=FakeOrch())
    outcome = node.round()
    assert outcome['freeze'] == {}

def test_observations_report_late_and_dropped_steps() -> None:
    """慢系统必须看见「手被吞了键 / 按晚了」，不然下一轮还会照原样再来一遍。"""

    result = {
        "commentary": [{"t_ms": 1200, "text": "前面有个篝火"}],
        "action_count": 3,
        "late_steps": 2,
        "dropped_steps": 1,
        "timing": {"decisions": 3, "max_lag_ms": 180, "mean_lag_ms": 70, "step_ms": 33},
        "actions": [
            {"step_status": {"pressed": True, "step": 6, "reason": ""}},
            {"step_status": {"pressed": False, "step": 6, "reason": "no_window_focus"}},
            {"step_status": {"pressed": True, "step": 6, "reason": ""}},
        ],
    }
    text = st.build_observations(result)
    assert "晚了一拍以上" in text and "180ms" in text
    assert "没真的生效" in text and "no_window_focus" in text


def test_observations_stay_silent_without_timing_data() -> None:
    text = st.build_observations({"action_count": 1})
    assert "晚了一拍" not in text and "没真的生效" not in text


def test_observations_say_why_the_handoff_happened() -> None:
    '''被判卡住时见闻必须写明原因：否则慢系统会照同一套打法再来一遍。'''
    result = {'action_count': 12, 'total_ms': 8000,
              'stuck': {'ok': True, 'stuck': True, 'why': '画面连续 12 帧几乎没变'}}
    body = st.build_observations(result)
    assert '被判卡住' in body
    assert '画面连续 12 帧几乎没变' in body


def test_observations_stay_silent_when_not_stuck() -> None:
    body = st.build_observations({'action_count': 3, 'total_ms': 900,
                                       'stuck': {'ok': True, 'stuck': False, 'why': ''}})
    assert '被判卡住' not in body


class FakeAdapterWithGrab:
    '''最小采集口：只回一帧假 JPEG，并记下被谁抓过。'''

    def __init__(self, data=b'\xff\xd8JPEGDATA'):
        self.data = data
        self.grabs = 0

    def grab(self):
        self.grabs += 1
        return {'data': self.data, 'w': 1280, 'h': 736}


class PumpWithoutFrame(FakePump):
    '''真实泵的复刻：一开始没有底片（available 为假），set_frame 之后才有。'''

    def __init__(self, **kw):
        FakePump.__init__(self, **kw)
        self.frame = b''
        self.primes = []

    def available(self):
        return bool(self.frame)

    def set_frame(self, jpeg):
        self.primes.append(len(jpeg))
        self.frame = jpeg


def test_first_round_primes_the_pump_with_a_real_frame():
    '''第一轮思考期不能因为「还没有帧」而整段停住：抓一张真画面垫底片。'''

    pump = PumpWithoutFrame(elapsed_ms=1500, frames_per_hold=6, painted_per_hold=2)
    orch = FakeOrch()
    orch.executor = type('E', (), {'adapter': FakeAdapterWithGrab()})()
    node = st.Streamer(orchestrator=orch, pump=pump)

    outcome = node.round()

    assert pump.primes, '第一轮必须补一张底片'
    assert pump.stats['holds'] == 1, '补完底片这一轮就该真的开冻结窗口'
    assert outcome['freeze']['frames'] == 6
    assert outcome['freeze']['painted'] == 2


def test_without_grab_the_pump_warns_instead_of_crashing():
    '''没有采集口时不能假装冻结成功：要显式提醒（画面会停住）。'''

    pump = PumpWithoutFrame(elapsed_ms=1500)
    node = st.Streamer(orchestrator=FakeOrch(), pump=pump)

    outcome = node.round()

    assert outcome['ok'] is True
    assert outcome['freeze'] == {}
    assert any('没有底片' in w for w in node.warnings), node.warnings
