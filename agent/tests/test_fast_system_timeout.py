# -*- coding: utf-8 -*-
'''快系统门面超时语义（agent/fast_system.py）：超时要「取消 + 断连」，不许只丢异常。'''
from __future__ import annotations

import asyncio
import time

import pytest

import fast_system as fs
import omni


class _Reply:
    def __init__(self, text: str, latency_ms: float) -> None:
        self.text = text
        self.latency_ms = latency_ms


class _SlowClient:
    '''永远不回包的客户端：模拟 WS 挂着不出 token。'''

    def __init__(self) -> None:
        self.closed = 0

    async def ask(self, *args, **kwargs):
        await asyncio.sleep(30)
        raise AssertionError('不该走到这里')

    async def close(self) -> None:
        self.closed += 1


class _OkClient:
    async def ask(self, *args, **kwargs):
        return _Reply('画面是命名界面', 12.5)

    async def close(self) -> None:
        return None


def _observer(client) -> fs.FastObserver:
    item = fs.FastObserver(omni.OmniConfig(api_key='test-key', model='fake'))
    item._client = client
    return item


def test_observe_timeout_raises_and_drops_client() -> None:
    client = _SlowClient()
    item = _observer(client)
    started = time.monotonic()
    with pytest.raises(fs.FastSystemError_):
        item.observe(b'\xff\xd8\xff', deadline_ms=80)
    assert time.monotonic() - started < 3.0
    assert item._client is None
    assert item.drop_count == 1
    assert 'timeout' in item.last_drop_reason
    item.close()


def test_observe_ok_keeps_client() -> None:
    client = _OkClient()
    item = _observer(client)
    text = item.observe(b'\xff\xd8\xff')
    assert text == '画面是命名界面'
    assert item.last_latency_ms == pytest.approx(12.5)
    assert item._client is client
    assert item.drop_count == 0
    item.close()


def test_deadline_env_override(monkeypatch) -> None:
    monkeypatch.setenv('BETTERAGI_OBSERVE_DEADLINE_MS', '3456')
    assert fs.observe_deadline_ms() == 3456
    monkeypatch.delenv('BETTERAGI_OBSERVE_DEADLINE_MS')
    assert fs.observe_deadline_ms() == fs.DEFAULT_OBSERVE_DEADLINE_MS


def test_empty_frame_still_rejected() -> None:
    item = _observer(_OkClient())
    with pytest.raises(fs.FastSystemError_):
        item.observe(b'')
    item.close()
