# -*- coding: utf-8 -*-
'''传输层：连接必须复用（2026-11-04）。

实测（暴露端点，253KB 请求）：新建连接 0.433s、复用 0.170s；1.5MB：0.733s → 0.264s。
每步新建连接等于把「一次决策」的时间预算浪费在握手上，所以 httpx.Client 改成
模块级单例 + 连接池，并在开播前 warmup()。'''
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import motor  # noqa: E402

#: 必须先抓住真类：monkeypatch 打的是 httpx 模块属性，工厂里再写 httpx.Client 就是自己
#: 调自己（RecursionError）。
REAL_CLIENT = httpx.Client
DATA_URL = 'data:image/png;base64,AAAA'


@pytest.fixture(autouse=True)
def _isolated_pool():
    motor.close_shared_client()
    yield
    motor.close_shared_client()


def _reply(content: str = '<think>\n\n</think>\n\n<|action_start|>0 0 0 ; Z ; Z ; Z ; Z ; Z ; Z<|action_end|>') -> dict:
    return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1}}


class _Factory:
    '''记录被调用次数的 httpx.Client 工厂（真 MockTransport，不联网）。'''

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0
        self.kwargs = []

    def __call__(self, *args, **kw):
        self.calls += 1
        self.kwargs.append(dict(kw))
        kw['transport'] = httpx.MockTransport(self.handler)
        return REAL_CLIENT(*args, **kw)


def _factory(monkeypatch, handler) -> _Factory:
    factory = _Factory(handler)
    monkeypatch.setattr(motor.httpx, 'Client', factory)
    monkeypatch.setattr(motor, '_shared_client', None)
    monkeypatch.setattr(motor, '_shared_factory', None)
    return factory


def test_shared_client_is_a_singleton(monkeypatch):
    factory = _factory(monkeypatch, lambda request: httpx.Response(200, json=_reply()))

    first = motor.shared_client()
    second = motor.shared_client()

    assert first is second, '同一进程里必须复用同一个连接池'
    assert factory.calls == 1
    motor.close_shared_client()


def test_shared_client_rebuilds_when_the_factory_changes(monkeypatch):
    first_factory = _factory(monkeypatch, lambda request: httpx.Response(200, json=_reply()))
    first = motor.shared_client()

    second_factory = _factory(monkeypatch, lambda request: httpx.Response(200, json=_reply()))
    second = motor.shared_client()

    assert second is not first, '换了工厂（测试替换/换端点）必须重建，不能沿用旧 MockTransport'
    assert first_factory.calls == 1 and second_factory.calls == 1
    motor.close_shared_client()


def test_client_is_built_with_the_connection_pool_limits(monkeypatch):
    factory = _factory(monkeypatch, lambda request: httpx.Response(200, json=_reply()))
    motor.shared_client()

    limits = factory.kwargs[0].get('limits')
    assert limits is not None, '必须显式设置连接池上限'
    assert limits.max_connections == 4
    assert limits.max_keepalive_connections == 4
    assert limits.keepalive_expiry == 300.0
    motor.close_shared_client()


def test_two_decisions_reuse_one_connection_pool(monkeypatch):
    factory = _factory(monkeypatch, lambda request: httpx.Response(200, json=_reply()))
    client = motor.GamescalingClient(endpoint='http://motor.test', timeout=5)

    client.decide(DATA_URL, deadline_ms=5000, grace_ms=0)
    client.decide(DATA_URL, deadline_ms=5000, grace_ms=0)

    assert factory.calls == 1, '第二步不许再建连接'
    motor.close_shared_client()


def test_warmup_hits_models_twice(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={'data': [{'id': 'gamescaling'}]})

    _factory(monkeypatch, handler)
    client = motor.GamescalingClient(endpoint='http://motor.test')
    out = client.warmup(attempts=2)

    assert out['ok'] is True
    assert out['attempts'] == 2
    assert seen == ['/v1/models', '/v1/models']
    assert len(out['elapsed_ms']) == 2 and 'reused' in out
    motor.close_shared_client()


def test_warmup_reports_a_dead_endpoint(monkeypatch):
    def boom(request):
        raise httpx.ConnectError('nope', request=request)

    _factory(monkeypatch, boom)
    client = motor.GamescalingClient(endpoint='http://motor.test')
    out = client.warmup(attempts=2)

    assert out['ok'] is False
    assert 'ConnectError' in out['error']
    motor.close_shared_client()