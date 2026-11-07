# -*- coding: utf-8 -*-
'''事件总线单测（agent/mailbox.py）：信封校验、队列上限与丢弃策略、等待超时。'''
from __future__ import annotations

import time

import pytest

import mailbox as mb


def test_envelope_has_id_and_ts() -> None:
    box = mb.Mailbox()
    env = box.put('speech.text', {'text': '你好'}, trace={'intent_id': 'i1'})
    assert env.id and len(env.id) == 16
    assert env.ts_ms > 0
    assert env.payload['text'] == '你好'
    assert env.to_dict()['trace'] == {'intent_id': 'i1'}


def test_unknown_type_rejected() -> None:
    box = mb.Mailbox()
    with pytest.raises(mb.MailboxError):
        box.put('nope.event', {})


def test_action_block_never_dropped() -> None:
    box = mb.Mailbox()
    cap = mb.EVENT_SPECS['action.block'].capacity
    for i in range(cap):
        box.put('action.block', {'intent_id': 'i1', 'step': i})
    with pytest.raises(mb.MailboxFull):
        box.put('action.block', {'intent_id': 'i1', 'step': cap})
    assert box.depth('action.block') == cap


def test_latest_only_keeps_newest_frame() -> None:
    box = mb.Mailbox()
    cap = mb.EVENT_SPECS['frame.sample'].capacity
    for i in range(cap + 3):
        box.put('frame.sample', {'path': 'f%d.jpg' % i})
    assert box.depth('frame.sample') == 1
    env = box.get('frame.sample')
    assert env.payload['path'] == 'f%d.jpg' % (cap + 2)
    assert box.dropped()['frame.sample'] == cap + 2  # 除第一条外每次投递都丢上一条


def test_merge_combines_danmaku_items() -> None:
    box = mb.Mailbox()
    cap = mb.EVENT_SPECS['danmaku.batch'].capacity
    for i in range(cap):
        box.put('danmaku.batch', {'items': [i]})
    box.put('danmaku.batch', {'items': [99]})
    assert box.depth('danmaku.batch') == cap
    last = box.drain('danmaku.batch')[-1]
    assert last.payload['items'] == [cap - 1, 99]  # 合并：旧批 items + 新批 items


def test_drop_oldest_tts() -> None:
    box = mb.Mailbox()
    cap = mb.EVENT_SPECS['tts.audio'].capacity
    for i in range(cap + 1):
        box.put('tts.audio', {'speech_id': i})
    assert box.depth('tts.audio') == cap
    assert box.get('tts.audio').payload['speech_id'] == 1


def test_get_without_deadline_fails_fast() -> None:
    box = mb.Mailbox()
    started = time.monotonic()
    with pytest.raises(mb.MailboxTimeout):
        box.get('intent.end')
    assert time.monotonic() - started < 0.2


def test_get_waits_until_deadline() -> None:
    box = mb.Mailbox()
    started = time.monotonic()
    with pytest.raises(mb.MailboxTimeout):
        box.get('intent.end', deadline_ms=60)
    assert 0.05 <= time.monotonic() - started < 1.0


def test_wait_for_any_type() -> None:
    box = mb.Mailbox()
    box.put('incident', {'kind': 'motor_parse_error'})
    kind, env = box.wait_for(('intent.end', 'incident'), deadline_ms=100)
    assert kind == 'incident'
    assert env.payload['kind'] == 'motor_parse_error'


def test_deadline_table_has_every_wait_point() -> None:
    for point in ('omni_first_token', 'omni_step_token', 'motor_step', 'tts_first_frame', 'slow_round', 'tool_call'):
        assert mb.deadline_ms(point) > 0
    with pytest.raises(KeyError):
        mb.deadline_ms('unknown_point')


def test_close_blocks_put() -> None:
    box = mb.Mailbox()
    box.close()
    with pytest.raises(mb.MailboxError):
        box.put('speech.text', {'text': 'x'})
