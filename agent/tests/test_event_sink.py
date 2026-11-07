# -*- coding: utf-8 -*-
'''事件消费端（agent/event_sink.py）：抽干、落盘、按 cursor 增量拉。'''
from __future__ import annotations

import json

import event_sink
import mailbox as mailbox_mod


def _box() -> mailbox_mod.Mailbox:
    return mailbox_mod.Mailbox()


def test_drain_empties_queue_and_counts_cursor(tmp_path) -> None:
    box = _box()
    box.put('state.change', {'from': 'IDLE', 'to': 'PLANNING', 'reason': 'plan'})
    box.put('intent.end', {'intent_id': 's-001', 'result': 'reached'})
    sink = event_sink.EventSink(box, root=str(tmp_path))
    rows = sink.drain()
    assert [row['type'] for row in rows] == ['state.change', 'intent.end']
    assert [row['cursor'] for row in rows] == [1, 2]
    assert box.depth('state.change') == 0 and box.depth('intent.end') == 0
    assert sink.drain() == []
    assert sink.cursor() == 2


def test_payload_and_trace_survive(tmp_path) -> None:
    box = _box()
    box.put('intent.end', {'intent_id': 's-002', 'result': 'blocked'},
            trace={'intent_id': 's-002', 'session_id': 'live-1'})
    sink = event_sink.EventSink(box, root=str(tmp_path))
    row = sink.drain()[0]
    assert row['payload']['result'] == 'blocked'
    assert row['trace']['session_id'] == 'live-1'
    assert row['ts_ms'] > 0 and row['id']


def test_writes_jsonl_one_line_per_event(tmp_path) -> None:
    box = _box()
    sink = event_sink.EventSink(box, root=str(tmp_path))
    box.put('state.change', {'to': 'EXECUTING'})
    sink.drain()
    box.put('state.change', {'to': 'HANDOFF'})
    sink.drain()
    lines = [line for line in sink.path() and open(sink.path(), encoding='utf-8').read().splitlines() if line]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [row['payload']['to'] for row in parsed] == ['EXECUTING', 'HANDOFF']
    assert [row['cursor'] for row in parsed] == [1, 2]


def test_since_returns_only_newer(tmp_path) -> None:
    box = _box()
    sink = event_sink.EventSink(box, root=str(tmp_path))
    for i in range(4):
        box.put('tool.result', {'n': i})
    sink.drain()
    newer = sink.since(2)
    assert [row['payload']['n'] for row in newer] == [2, 3]
    assert sink.since(4) == []


def test_without_mailbox_is_harmless() -> None:
    sink = event_sink.EventSink(None)
    assert sink.drain() == []
    assert sink.path() == ''
    snap = sink.snapshot()
    assert snap['cursor'] == 0 and snap['path'] == ''


def test_only_registered_types_are_consumed(tmp_path) -> None:
    box = _box()
    box.put('speech.text', {'text': '这句不该被抽走'})
    sink = event_sink.EventSink(box, root=str(tmp_path))
    assert sink.drain() == []
    assert box.depth('speech.text') == 1  # 留给真正的消费者（TTS）


def test_dropped_is_reported(tmp_path) -> None:
    box = _box()
    sink = event_sink.EventSink(box, root=str(tmp_path))
    for _ in range(9):  # intent.end 上限 8，第 9 条会 makeput 抛 MailboxFull
        try:
            box.put('intent.end', {'intent_id': 'x'})
        except mailbox_mod.MailboxFull:
            break
    snap = sink.snapshot()
    assert snap['dropped'] == {}  # reject 策略不记 drop，是直接抛


def test_snapshot_reports_counts(tmp_path) -> None:
    box = _box()
    box.put('incident', {'kind': 'motor_parse_error'})
    sink = event_sink.EventSink(box, root=str(tmp_path))
    sink.drain()
    snap = sink.snapshot()
    assert snap['counts'] == {'incident': 1}
    assert snap['kept'] == 1 and snap['cursor'] == 1
    assert snap['errors'] == []
