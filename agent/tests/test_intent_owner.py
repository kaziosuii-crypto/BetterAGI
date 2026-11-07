# -*- coding: utf-8 -*-
'''意图总线的归属与互斥（03 §C.7）：谁拥有意图、交棒期谁能改主意、时钟只有一个。'''
from __future__ import annotations

import pytest

import intent as intent_mod
from intent import Intent, IntentBus


def _intent(type='navigate', owner='fast', **kwargs):
    return Intent(type=type, target='村口', confidence=0.9, urgency=0.6, owner=owner, **kwargs)


def test_unknown_owner_is_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        Intent(type='navigate', owner='robot')
    assert "'robot'" in str(exc.value)


def test_unknown_handoff_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        Intent(type='navigate', handoff_state='flying')


def test_handoff_blocks_fast_intents_but_allows_slow() -> None:
    bus = IntentBus(min_dwell=0.0)
    bus.begin_handoff('慢系统在规划')

    ok, why = bus.accept(_intent(owner='fast'))
    assert ok is False and '交棒中' in why

    ok, why = bus.accept(_intent(type='interact', owner='slow'))
    assert ok is True, why


def test_no_op_still_passes_during_handoff() -> None:
    bus = IntentBus(min_dwell=10.0)
    bus.accept(_intent())
    bus.begin_handoff()

    ok, _why = bus.accept(Intent(type='NO_OP', ttl=0.0))
    assert ok is True, '喊停永远要放行（安全优先）'


def test_end_handoff_restores_fast_control() -> None:
    bus = IntentBus(min_dwell=0.0)
    bus.begin_handoff()
    assert bus.accept(_intent())[0] is False
    bus.end_handoff()
    ok, why = bus.accept(_intent())
    assert ok is True, why
    assert bus.handoff_state == 'idle'


def test_clock_ms_drives_ttl_not_wall_clock() -> None:
    now_ms = [1000]
    bus = IntentBus(min_dwell=0.0, clock_ms=lambda: now_ms[0])
    bus.accept(Intent(type='wait', ttl=0.5, confidence=0.9))

    now_ms[0] += 300
    assert bus.current().type == 'wait', 'TTL 500ms 还没到'

    now_ms[0] += 400
    assert bus.current().type == 'NO_OP', 'TTL 过了必须回落 NO_OP，不能抱陈旧命令跑'


def test_history_records_intent_id_and_handoff_marks() -> None:
    bus = IntentBus(min_dwell=0.0)
    bus.accept(Intent(type='interact', intent_id='i-007', confidence=0.9))
    bus.begin_handoff('卡住了')
    bus.end_handoff('想好了')

    rows = bus.history(10)
    ids = [row.get('intent_id') for row in rows if row.get('intent_id')]
    kinds = [row.get('kind') for row in rows if row.get('kind')]
    assert 'i-007' in ids
    assert kinds == ['handoff_begin', 'handoff_end']