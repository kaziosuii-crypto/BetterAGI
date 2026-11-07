# -*- coding: utf-8 -*-
'''弹幕取用单测（agent/danmaku_feed.py）：JSONL 解析、失败降级、since/limit 透传。'''
from __future__ import annotations

import httpx
import pytest

import danmaku_feed as df


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)


def test_fetch_parses_jsonl_and_passes_params() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['url'] = str(request.url)
        body = '{"uid":"1","nick":"夜车","text":"来了"}\n{"uid":"2","text":"草"}\n'
        return httpx.Response(200, text=body)

    feed = df.DanmakuFeed(client=_client(handler))
    status = feed.fetch(since_ms=1710000000000, limit=20)
    assert status.ok and len(status.items) == 2
    assert status.items[1]['text'] == '草'
    assert 'since=1710000000000' in seen['url']
    assert 'limit=20' in seen['url']


def test_bad_lines_are_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='not json\n{"text":"好"}\n{broken\n')

    status = df.DanmakuFeed(client=_client(handler)).fetch()
    assert status.ok and [i['text'] for i in status.items] == ['好']


def test_http_error_degrades_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text='down')

    status = df.DanmakuFeed(client=_client(handler)).fetch()
    assert status.ok is False and status.items == []
    assert '503' in status.error


def test_connect_error_degrades_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('refused')

    status = df.DanmakuFeed(client=_client(handler)).fetch()
    assert status.ok is False
    assert 'ConnectError' in status.error


def test_stats_and_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith('/stats'):
            return httpx.Response(200, json={'count': 12, 'connected': True})
        return httpx.Response(404)

    feed = df.DanmakuFeed(client=_client(handler))
    assert feed.health() is True
    assert feed.stats()['count'] == 12


def test_stats_failure_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('refused')

    stats = df.DanmakuFeed(client=_client(handler)).stats()
    assert stats['ok'] is False and 'ConnectError' in stats['error']
