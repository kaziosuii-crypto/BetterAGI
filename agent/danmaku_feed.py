# -*- coding: utf-8 -*-
'''弹幕取用（设计文档 02_schemas.md §12）：慢系统是唯一的消费者。

用户口语：弹幕本地服务由别的代理做，接入方式按最省事的定——

    GET /danmaku?since=<epoch_ms>&limit=50   -> JSONL，每行一条弹幕
    GET /stats                              -> {uptime_s, count, last_ts_ms, connected}

「since 用 epoch 毫秒」是硬约束：本场时间轴用的是单调毫秒，两者不能混。
这一层只负责取用与降级，不负责去重排序（服务端是环形 500 条）。

重要：弹幕永远不阻塞主循环——服务不通就返回空列表 + 记一条状态，
让执行继续（设计里「执行优先、记下等意图结束再议」）。
'''
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = 'http://127.0.0.1:18080'
DEFAULT_LIMIT = 50
DEFAULT_TIMEOUT_MS = 800


@dataclass
class DanmakuStatus:
    '''一次取用的结果与降级说明（落进 incident / session 用）。'''

    ok: bool
    items: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ''
    elapsed_ms: int = 0
    since_ms: Optional[int] = None


class DanmakuFeed:
    '''弹幕服务的只读客户端：取一段、看统计、什么都不改。'''

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        client: Any = None,
    ) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip('/')
        self.timeout_s = max(0.05, float(timeout_ms) / 1000.0)
        self._client = client

    # ---- 内部 ----------------------------------------------------------

    def _http(self):
        if self._client is not None:
            return self._client
        import httpx

        # trust_env=False：本机服务，别走系统代理。
        self._client = httpx.Client(timeout=self.timeout_s, trust_env=False)
        return self._client

    # ---- 对外 ----------------------------------------------------------

    def fetch(self, since_ms: Optional[int] = None, limit: int = DEFAULT_LIMIT) -> DanmakuStatus:
        '''取 since 之后的最多 limit 条；失败不抛，返回 ok=False 的空结果。'''
        started = time.monotonic()
        params: Dict[str, Any] = {'limit': int(limit)}
        if since_ms is not None:
            params['since'] = int(since_ms)
        try:
            response = self._http().get(self.base_url + '/danmaku', params=params)
            elapsed = int((time.monotonic() - started) * 1000)
            if response.status_code != 200:
                return DanmakuStatus(False, [], 'HTTP %s' % response.status_code, elapsed, since_ms)
            items = _parse_jsonl(response.text)
            return DanmakuStatus(True, items, '', elapsed, since_ms)
        except Exception as error:  # noqa: BLE001 - 弹幕挂了不许影响执行
            elapsed = int((time.monotonic() - started) * 1000)
            return DanmakuStatus(False, [], '%s: %s' % (type(error).__name__, error), elapsed, since_ms)

    def stats(self) -> Dict[str, Any]:
        '''服务端统计；失败返回 {'ok': False, 'error': ...}。'''
        try:
            response = self._http().get(self.base_url + '/stats')
            if response.status_code != 200:
                return {'ok': False, 'error': 'HTTP %s' % response.status_code}
            data = response.json()
            if not isinstance(data, dict):
                return {'ok': False, 'error': 'stats 不是对象'}
            return {'ok': True, **data}
        except Exception as error:  # noqa: BLE001
            return {'ok': False, 'error': '%s: %s' % (type(error).__name__, error)}

    def health(self) -> bool:
        return bool(self.stats().get('ok'))

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None and hasattr(client, 'close'):
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 收尾失败不重要
                pass


def _parse_jsonl(text: str) -> List[Dict[str, Any]]:
    '''一行一条；坏行跳过但保留计数（坏行说明服务端在写半个包）。'''
    out: List[Dict[str, Any]] = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def build_feed(base_url: Optional[str] = None) -> DanmakuFeed:
    '''按环境变量 BETTERAGI_DANMAKU_URL 覆盖默认地址。'''
    import os

    return DanmakuFeed(base_url or os.environ.get('BETTERAGI_DANMAKU_URL') or DEFAULT_BASE_URL)
