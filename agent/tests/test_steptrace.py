# -*- coding: utf-8 -*-
'''每步三段计时账本（agent/steptrace.py）：编帧 / 上传+推理 / 按键。

用户口径（2026-11-04）：一次决策的墙钟是三段之和，不许再拿「单次上传秒数」当带宽。
这里只测账本本身：落一行、中位数、缺段要诚实报警、写失败不许炸执行链路。
'''
from __future__ import annotations

import json
import os

import steptrace


def _rows(path):
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_record_appends_one_jsonl_row(tmp_path):
    trace = steptrace.build_trace(str(tmp_path / 'run' / 's1'))
    row = trace.record(t_encode=12.5, t_upload_infer=900.0, t_act=198.0,
                       intent_id='i-1', step=3, payload_bytes=804671)

    assert row['ok'] is True
    assert os.path.exists(os.path.join(str(tmp_path / 'run' / 's1'), 'steps.jsonl'))
    rows = _rows(row and trace.path)
    assert len(rows) == 1
    assert rows[0]['t_encode'] == 12.5
    assert rows[0]['t_upload_infer'] == 900.0
    assert rows[0]['t_act'] == 198.0
    assert rows[0]['total_ms'] == 1110.5
    assert rows[0]['intent_id'] == 'i-1'
    assert rows[0]['payload_bytes'] == 804671


def test_summary_reports_the_three_medians(tmp_path):
    trace = steptrace.build_trace(str(tmp_path))
    for encode, upload, act in ((10, 900, 200), (20, 1000, 200), (30, 1100, 220)):
        trace.record(t_encode=encode, t_upload_infer=upload, t_act=act)

    summary = trace.summary()
    assert summary['steps'] == 3
    assert summary['phases']['t_encode']['median_ms'] == 20.0
    assert summary['phases']['t_upload_infer']['median_ms'] == 1000.0
    assert summary['phases']['t_act']['median_ms'] == 200.0
    assert summary['total_median_ms'] == 1220.0
    assert summary['phases']['t_upload_infer']['p95_ms'] >= 1080.0


def test_missing_phase_is_reported_not_invented(tmp_path):
    trace = steptrace.build_trace(str(tmp_path))
    trace.record(t_encode=5, t_upload_infer=500, t_act=198)
    trace.record(t_encode=6, t_act=198)  # 少 t_upload_infer

    summary = trace.summary()
    assert summary['phases']['t_upload_infer']['n'] == 1
    assert any('t_upload_infer' in warning for warning in summary['warnings']), \
        '缺段必须显式警告，不能拿别的段顶替'


def test_write_failure_does_not_raise(tmp_path):
    blocker = tmp_path / 'not-a-dir'
    blocker.write_text('x', encoding='utf-8')
    trace = steptrace.build_trace(str(blocker))

    row = trace.record(t_encode=1, t_upload_infer=2, t_act=3)

    assert row['ok'] is False and row['error']
    assert trace.errors


def test_rows_skip_a_broken_line(tmp_path):
    trace = steptrace.build_trace(str(tmp_path))
    trace.record(t_encode=1, t_upload_infer=2, t_act=3)
    with open(trace.path, 'a', encoding='utf-8') as handle:
        handle.write('{"t_encode": 9, "half')
        handle.write(os.linesep)
    trace.record(t_encode=4, t_upload_infer=5, t_act=6)

    rows = trace.rows()
    assert len(rows) == 2


def test_clock_with_now_ms_is_used(tmp_path):
    class Clock:
        def now_ms(self):
            return 4242

    trace = steptrace.build_trace(str(tmp_path), clock=Clock())
    assert trace.record(t_encode=1)['t_ms'] == 4242


def test_median_of_nothing_is_zero():
    assert steptrace.median([]) == 0.0
    assert steptrace.median([5]) == 5.0
    assert steptrace.median([3, 1, 2]) == 2.0
    assert steptrace.median([1, 2, 3, 4]) == 2.5
