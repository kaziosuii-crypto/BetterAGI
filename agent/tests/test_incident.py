# -*- coding: utf-8 -*-
'''事故留证（agent/incident.py）：8 个文件齐、切片准、清理策略不误删。'''
from __future__ import annotations

import json
import time
from pathlib import Path

import incident


def _bundle(tmp_path: Path) -> incident.IncidentBundle:
    return incident.IncidentBundle(tmp_path, kind='motor_parse_error', intent_id='int-7', reason='解析失败')


def test_bundle_writes_eight_artifacts(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle.write_json('request.json', {'model': 'gamescaling', 'system': 'S'})
    bundle.write_text('raw_reply.txt', 'current game screen')
    bundle.capture_error(ValueError('parse fail'), summary='手模型吐了个非动作')
    bundle.proc_snapshot({'active_sessions': 1, 'queue': {'omni_first_token': 0}})
    bundle.env_snapshot({'checkpoint': '2B-S3'})
    rows = [{'t_ms': 1000}, {'t_ms': 10500}, {'t_ms': 2000}]
    bundle.timeline_slice(rows, anchor_ms=11000, radius_ms=10000)  # 1000..21000
    source = tmp_path / 'frame_a.jpg'
    source.write_bytes(b'jpg')
    bundle.copy_frames([source, tmp_path / 'missing.jpg'])

    for name in ('README.md', 'request.json', 'raw_reply.txt', 'parsed.json', 'timeline_slice.jsonl', 'proc.json', 'env.json'):
        assert (bundle.path / name).is_file(), name
    assert (bundle.path / 'frames').is_dir()
    assert (bundle.path / 'frames' / 'frame_a.jpg').is_file()
    assert 'missing.jpg' in (bundle.path / 'frames' / 'MISSING.txt').read_text(encoding='utf-8')
    assert json.loads((bundle.path / 'parsed.json').read_text(encoding='utf-8'))['error_type'] == 'ValueError'
    assert json.loads((bundle.path / 'proc.json').read_text(encoding='utf-8'))['queue'] == {'omni_first_token': 0}
    readme = (bundle.path / 'README.md').read_text(encoding='utf-8')
    assert 'motor_parse_error' in readme and '解析失败' in readme


def test_timeline_slice_window(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    rows = [{'t_ms': 0}, {'t_ms': 500}, {'t_ms': 20000}, {'t_ms': 20001}]
    bundle.timeline_slice(rows, anchor_ms=10000, radius_ms=10000)
    kept = [json.loads(line)['t_ms'] for line in (bundle.path / 'timeline_slice.jsonl').read_text(encoding='utf-8').splitlines()]
    assert kept == [0, 500, 20000]


def test_prune_keeps_newest_and_keep_marker(tmp_path: Path) -> None:
    root = tmp_path / 'incidents'
    root.mkdir()
    for i in range(25):
        item = root / ('2026010%d-%02d00_int' % (i // 10, i % 60))
        item.mkdir()
        (item / 'README.md').write_text('x' * 100, encoding='utf-8')
    oldest = sorted(p.name for p in root.iterdir())[0]
    (root / oldest / incident.KEEP_MARKER).write_text('', encoding='utf-8')
    deleted = incident.prune(root, keep_count=20)
    remaining = {p.name for p in root.iterdir()}
    assert len(remaining) == 21  # 20 个最新的 + 1 个被 keep 的最旧
    assert oldest in remaining
    assert oldest not in deleted
    assert len(deleted) == 4  # 25 个里 20 个最新 + 被 keep 的最旧 = 保留 21


def test_prune_by_size(tmp_path: Path) -> None:
    root = tmp_path / 'incidents'
    root.mkdir()
    for i in range(4):
        item = root / ('2026010%d-000000_int' % i)
        item.mkdir()
        (item / 'raw_reply.txt').write_bytes(b'x' * 1000)
    deleted = incident.prune(root, keep_count=10, max_bytes=2500)
    remaining = {p.name for p in root.iterdir()}
    assert len(deleted) == 2
    assert len(remaining) == 2

def test_slow_platform_probe_does_not_block_capture(tmp_path: Path, monkeypatch) -> None:
    '''平台探测卡住时，留证必须照常写完，只标 slow_probes。'''

    started = time.monotonic()

    def _hang() -> str:
        time.sleep(30)
        return 'never'

    monkeypatch.setattr(incident.platform, 'platform', _hang)
    monkeypatch.setattr(incident, 'SNAPSHOT_TIMEOUT_S', 0.2)
    bundle = _bundle(tmp_path)
    bundle.env_snapshot({'checkpoint': '2B-S3'})
    elapsed = time.monotonic() - started
    data = json.loads((bundle.path / 'env.json').read_text(encoding='utf-8'))
    assert elapsed < 5.0
    assert data['platform'] == incident.UNKNOWN
    assert 'platform' in data['slow_probes']
    assert data['checkpoint'] == '2B-S3'


def test_torch_probe_failure_is_marked_not_fatal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(incident, '_torch_info', lambda: {})
    bundle = _bundle(tmp_path)
    bundle.env_snapshot()
    data = json.loads((bundle.path / 'env.json').read_text(encoding='utf-8'))
    assert 'torch' not in data
    assert 'torch' in data['slow_probes']
