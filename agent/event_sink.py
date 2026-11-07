# -*- coding: utf-8 -*-
'''事件总线的消费端（设计 02 §5 的「反向 mailbox」）。

为什么需要：mailbox 从落地起只有写端（orchestrator.put / agent._publish），
没有任何读者——事件堆到队列上限就被丢，事后无法回答「那一刻到底发生了什么」。
这里把消费端补齐：

1. 每轮把各类队列抽干，落成 run/<sid>/events.jsonl（一行一个信封，带单调 cursor）；
2. 内存里留一个环形缓冲，供 TUI / 状态接口按 cursor 增量拉（GET /events?since=）；
3. 计数与丢弃量可查——「谁丢了消息」必须能回答，不能靠猜。

不做的事：不改事件定义、不做重放调度、不阻塞任何调用方（drain 永远非阻塞）。
'''
from __future__ import annotations

import json
import os
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

#: 默认消费的事件类型（写端会出现的那几类）。
DEFAULT_TYPES: Tuple[str, ...] = (
    'state.change',
    'intent.end',
    'incident',
    'tool.call',
    'tool.result',
    'danmaku.batch',
)

EVENTS_FILE = 'events.jsonl'
DEFAULT_CAPACITY = 2000


class EventSinkError(Exception):
    '''消费端自己的错误（写盘失败等）。'''


class EventSink:
    '''把 mailbox 里的事件抽出来，落盘 + 留环形缓冲。'''

    def __init__(self, mailbox: Any = None, root: Optional[str] = None,
                 types: Iterable[str] = DEFAULT_TYPES, capacity: int = DEFAULT_CAPACITY,
                 filename: str = EVENTS_FILE) -> None:
        self.mailbox = mailbox
        self.root = root
        self.types = tuple(types)
        self.capacity = max(1, int(capacity))
        self.filename = filename
        self._kept: Deque[Dict[str, Any]] = deque(maxlen=self.capacity)
        self._cursor = 0
        self._counts: Dict[str, int] = {}
        self.errors: List[str] = []

    # ---- 路径 ----------------------------------------------------------

    def path(self) -> str:
        if not self.root:
            return ''
        return os.path.join(str(self.root), self.filename)

    # ---- 消费 ----------------------------------------------------------

    def drain(self) -> List[Dict[str, Any]]:
        '''抽干已登记的各类队列，返回新事件（非阻塞，没有就是空表）。'''

        rows: List[Dict[str, Any]] = []
        box = self.mailbox
        if box is None:
            return rows
        for name in self.types:
            try:
                envs = box.drain(name)
            except Exception as error:  # noqa: BLE001 - 消费端不该把主链路带崩
                self.errors.append('drain %s 失败：%s: %s' % (name, type(error).__name__, error))
                continue
            for env in envs or ():
                self._cursor += 1
                row = {
                    'cursor': self._cursor,
                    'type': getattr(env, 'type', name),
                    'ts_ms': int(getattr(env, 'ts_ms', 0) or 0),
                    'id': str(getattr(env, 'id', '') or ''),
                    'trace': dict(getattr(env, 'trace', {}) or {}),
                    'payload': dict(getattr(env, 'payload', {}) or {}),
                }
                self._kept.append(row)
                self._counts[row['type']] = self._counts.get(row['type'], 0) + 1
                rows.append(row)
        if rows:
            self._append(rows)
        return rows

    def _append(self, rows: List[Dict[str, Any]]) -> None:
        path = self.path()
        if not path:
            return
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + chr(10))
        except Exception as error:  # noqa: BLE001
            self.errors.append('写 %s 失败：%s: %s' % (path, type(error).__name__, error))

    # ---- 查询 ----------------------------------------------------------

    def since(self, cursor: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        '''取 cursor 之后的事件（供状态接口增量拉）。'''

        start = max(0, int(cursor or 0))
        out = [row for row in self._kept if row['cursor'] > start]
        if limit is not None and limit > 0:
            out = out[-int(limit):]
        return out

    def cursor(self) -> int:
        return self._cursor

    def counts(self) -> Dict[str, int]:
        return dict(self._counts)

    def dropped(self) -> Dict[str, int]:
        box = self.mailbox
        if box is None or not hasattr(box, 'dropped'):
            return {}
        try:
            return dict(box.dropped())
        except Exception:  # noqa: BLE001
            return {}

    def snapshot(self) -> Dict[str, Any]:
        return {
            'cursor': self._cursor,
            'kept': len(self._kept),
            'capacity': self.capacity,
            'counts': self.counts(),
            'dropped': self.dropped(),
            'path': self.path(),
            'errors': list(self.errors),
        }


def build_sink(mailbox: Any = None, root: Optional[str] = None) -> EventSink:
    return EventSink(mailbox=mailbox, root=root)
