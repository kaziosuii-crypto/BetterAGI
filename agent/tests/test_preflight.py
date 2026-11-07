# -*- coding: utf-8 -*-
'''开播前自检（agent/preflight.py）：结论、超时、异常都不许静默。'''
from __future__ import annotations

import time

import preflight


def test_all_pass(tmp_path) -> None:
    report = preflight.run_preflight([('a', lambda: True), ('b', lambda: {'ok': True, 'detail': 'fine'})])
    assert report.ok is True
    assert [r.name for r in report.results] == ['a', 'b']
    assert report.failures == []
    assert '通过' in report.markdown()


def test_failure_carries_hint() -> None:
    report = preflight.run_preflight([('omni', lambda: preflight.CheckResult('omni', False, '连不上', hint='看额度'))])
    assert report.ok is False
    assert report.failures[0].hint == '看额度'
    assert 'omni' in report.markdown()


def test_exception_becomes_failure() -> None:
    def boom():
        raise RuntimeError('端口不通')

    report = preflight.run_preflight([('gamescaling', boom)])
    assert report.ok is False
    assert 'RuntimeError' in report.failures[0].detail and '端口不通' in report.failures[0].detail
    assert report.failures[0].hint  # 一定要给下一步


def test_timeout_is_failure_not_hang() -> None:
    def slow():
        time.sleep(1.0)
        return True

    started = time.monotonic()
    report = preflight.run_preflight([('omni', slow)], timeout_ms=50)
    assert time.monotonic() - started < 0.9
    assert report.ok is False
    assert '超过' in report.failures[0].detail


def test_callable_name_inferred() -> None:
    def my_check():
        return True

    report = preflight.run_preflight([my_check])
    assert report.results[0].name == 'my_check'


def test_danmaku_down_reported(tmp_path) -> None:
    class FakeFeed:
        def stats(self):
            return {'ok': False, 'error': 'connection refused'}

    result = preflight.check_danmaku(FakeFeed())
    assert result.ok is False
    assert 'connection refused' in result.detail


def test_disk_check(tmp_path) -> None:
    assert preflight.check_disk(str(tmp_path), min_free_mb=0).ok is True
    assert preflight.check_disk(str(tmp_path), min_free_mb=10 ** 12).ok is False

class _Motor:
    def __init__(self, health):
        self._health = health

    def health(self):
        return self._health


def test_check_motor_available_false_is_failure() -> None:
    result = preflight.check_motor(_Motor({'available': False, 'endpoint': 'http://127.0.0.1:18041',
                                          'error': 'ConnectError: 目标计算机积极拒绝'}))
    assert result.ok is False
    assert 'ConnectError' in result.detail
    assert result.hint


def test_check_motor_available_true_passes() -> None:
    result = preflight.check_motor(_Motor({'available': True, 'model': 'gamescaling'}))
    assert result.ok is True


def test_check_motor_ok_false_is_failure() -> None:
    result = preflight.check_motor(_Motor({'ok': False, 'error': '没加载 checkpoint'}))
    assert result.ok is False and 'checkpoint' in result.detail
