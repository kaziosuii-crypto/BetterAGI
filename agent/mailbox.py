# -*- coding: utf-8 -*-
'''进程内事件总线（设计文档 02_schemas.md §5）：统一信封 + 每类队列上限 + 等待点 deadline。

为什么需要它：现有实现的串台根因是「没有信封、没有超时」——Omni 的 recv 无 deadline，
一条迟到的 response.done 会被下一轮读到；模块之间靠几个 threading.Event 传状态，
谁丢了消息、谁卡住了都查不出来。这里把三件事定死：

1. 信封：{v, ts_ms, type, id, trace:{intent_id, session_id}, payload}；
2. 每类事件一个队列上限与丢弃策略——**丢键位等于动作错乱**，所以 action.block 不许丢；
3. 每个等待点必须给 deadline，超时走明确分支（抛 MailboxTimeout），不许无限等。

对外接口：

    box = Mailbox()
    box.put('speech.text', {'text': '好'}, trace={'intent_id': 'i1'})
    env = box.get('speech.text', deadline_ms=100)
    env = box.wait_for(('intent.end', 'incident'), deadline_ms=2000)

信封校验失败抛 MailboxError；队列满且策略是 reject 时抛 MailboxFull；等待超时抛 MailboxTimeout。
'''
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1


class MailboxError(Exception):
    '''信封不合法。'''


class MailboxFull(MailboxError):
    '''队列满且该类型不允许丢弃。'''


class MailboxTimeout(MailboxError):
    '''等待超时——调用方必须走明确分支，不许无限等。'''


@dataclass(frozen=True)
class EventSpec:
    '''一类事件的队列容量与丢弃策略。'''

    capacity: int
    policy: str  # reject | drop_oldest | latest_only | merge


#: 事件表（02_schemas.md §5.2）。
EVENT_SPECS: Dict[str, EventSpec] = {
    'intent.dispatch': EventSpec(1, 'reject'),
    'intent.end': EventSpec(8, 'reject'),
    'action.block': EventSpec(64, 'reject'),
    'speech.text': EventSpec(256, 'reject'),
    'tts.audio': EventSpec(64, 'drop_oldest'),
    'frame.sample': EventSpec(8, 'latest_only'),
    'danmaku.batch': EventSpec(8, 'merge'),
    'tool.call': EventSpec(32, 'reject'),
    'tool.result': EventSpec(32, 'reject'),
    'state.change': EventSpec(32, 'reject'),
    'timeline.event': EventSpec(4096, 'reject'),
    'incident': EventSpec(4, 'reject'),
}

#: 等待点上限（毫秒，02_schemas.md §5.3）。超时必须走明确分支。
DEADLINES: Dict[str, int] = {
    'omni_first_token': 30000,
    'omni_step_token': 5000,
    'motor_step': 300,
    'tts_first_frame': 2500,
    'slow_round': 2000,
    'tool_call': 800,
    'search_memory': 2000,
}


def deadline_ms(point: str) -> int:
    '''取某个等待点的上限；未登记的点直接报错，避免「忘了给 deadline」。'''
    if point not in DEADLINES:
        raise KeyError('未登记的等待点: ' + str(point))
    return DEADLINES[point]


@dataclass
class Envelope:
    '''一条消息：type + trace + payload，自带 ts_ms 与 id。'''

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    ts_ms: int = 0
    id: str = ''
    v: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            'v': self.v,
            'ts_ms': self.ts_ms,
            'type': self.type,
            'id': self.id,
            'trace': dict(self.trace),
            'payload': dict(self.payload),
        }

    @classmethod
    def build(
        cls,
        type: str,
        payload: Optional[Dict[str, Any]] = None,
        trace: Optional[Dict[str, Any]] = None,
        ts_ms: Optional[int] = None,
    ) -> 'Envelope':
        if type not in EVENT_SPECS:
            raise MailboxError('未知事件类型: ' + str(type))
        if payload is not None and not isinstance(payload, dict):
            raise MailboxError('payload 必须是 dict: ' + type)
        return cls(
            type=type,
            payload=dict(payload or {}),
            trace=dict(trace or {}),
            ts_ms=int(ts_ms if ts_ms is not None else time.monotonic() * 1000),
            id=uuid.uuid4().hex[:16],
        )


class Mailbox:
    '''线程安全的事件总线：按类型分队列，容量与丢弃策略写死在 EVENT_SPECS。'''

    def __init__(self, clock=None) -> None:
        self._clock = clock or (lambda: int(time.monotonic() * 1000))
        self._queues: Dict[str, Deque[Envelope]] = {name: deque() for name in EVENT_SPECS}
        self._cond = threading.Condition()
        self._dropped: Dict[str, int] = {}
        self._closed = False

    # ---- 写 ------------------------------------------------------------

    def put(
        self,
        type: str,
        payload: Optional[Dict[str, Any]] = None,
        trace: Optional[Dict[str, Any]] = None,
        ts_ms: Optional[int] = None,
    ) -> Envelope:
        '''投递一条消息，返回信封（调用方要用 id 配对）。'''
        env = Envelope.build(type, payload, trace, ts_ms)
        return self.put_envelope(env)

    def put_envelope(self, env: Envelope) -> Envelope:
        spec = EVENT_SPECS.get(env.type)
        if spec is None:
            raise MailboxError('未知事件类型: ' + str(env.type))
        with self._cond:
            if self._closed:
                raise MailboxError('Mailbox 已关闭')
            queue = self._queues[env.type]
            if spec.policy == 'latest_only':
                # 「只保留最新」：采样帧慢了就丢，队列里永远最多一条。
                if queue:
                    queue.clear()
                    self._dropped[env.type] = self._dropped.get(env.type, 0) + 1
                if not env.ts_ms:
                    env.ts_ms = int(self._clock())
                queue.append(env)
                self._cond.notify_all()
                return env
            if len(queue) >= spec.capacity:
                if spec.policy == 'reject':
                    # 丢键位 = 动作错乱，宁可直接炸给调用方。
                    raise MailboxFull('队列已满且不允许丢弃: ' + env.type)
                if spec.policy == 'drop_oldest':
                    queue.popleft()
                    self._dropped[env.type] = self._dropped.get(env.type, 0) + 1
                elif spec.policy == 'latest_only':
                    queue.clear()
                    self._dropped[env.type] = self._dropped.get(env.type, 0) + 1
                elif spec.policy == 'merge':
                    items = list(env.payload.get('items') or [])
                    merged = queue.pop()
                    merged.payload['items'] = list(merged.payload.get('items') or []) + items
                    queue.append(merged)
                    self._dropped[env.type] = self._dropped.get(env.type, 0) + 1
                    self._cond.notify_all()
                    return env
            if not env.ts_ms:
                env.ts_ms = int(self._clock())
            queue.append(env)
            self._cond.notify_all()
            return env

    # ---- 读 ------------------------------------------------------------

    def get(self, type: str, deadline_ms: Optional[int] = None) -> Envelope:
        '''取一条；没有就等到 deadline（None = 立刻返回，抛 MailboxTimeout）。'''
        with self._cond:
            if type not in self._queues:
                raise MailboxError('未知事件类型: ' + str(type))
            end = None if deadline_ms is None else time.monotonic() + max(0, deadline_ms) / 1000.0
            while not self._queues[type]:
                if deadline_ms is None:
                    raise MailboxTimeout('队列为空: ' + type)
                remain = end - time.monotonic() if end is not None else 0
                if remain <= 0:
                    raise MailboxTimeout('等待超时: ' + type)
                self._cond.wait(remain)
            return self._queues[type].popleft()

    def wait_for(
        self,
        types: Iterable[str],
        deadline_ms: int,
        ) -> Tuple[str, Envelope]:
        '''等任意一类事件到达；超时抛 MailboxTimeout。返回 (type, envelope)。'''
        wanted = [t for t in types]
        for name in wanted:
            if name not in self._queues:
                raise MailboxError('未知事件类型: ' + str(name))
        end = time.monotonic() + max(0, deadline_ms) / 1000.0
        with self._cond:
            while True:
                for name in wanted:
                    if self._queues[name]:
                        return name, self._queues[name].popleft()
                remain = end - time.monotonic()
                if remain <= 0:
                    raise MailboxTimeout('等待超时: ' + ','.join(wanted))
                self._cond.wait(remain)

    # ---- 观测 ----------------------------------------------------------

    def depth(self, type: str) -> int:
        with self._cond:
            return len(self._queues.get(type) or ())

    def depths(self) -> Dict[str, int]:
        with self._cond:
            return {name: len(queue) for name, queue in self._queues.items()}

    def dropped(self) -> Dict[str, int]:
        with self._cond:
            return dict(self._dropped)

    def drain(self, type: str) -> List[Envelope]:
        '''取空某类队列（收尾/孤儿清理用）。'''
        with self._cond:
            queue = self._queues.get(type)
            if not queue:
                return []
            out = list(queue)
            queue.clear()
            return out

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

