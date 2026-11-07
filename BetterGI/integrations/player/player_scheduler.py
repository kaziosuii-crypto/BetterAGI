#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单事件循环内的有界认知调度器，不依赖具体的 Agent 或 LLM 实现。

接线：创建 PlayerScheduler(handler)，在运行中的事件循环调用 start()；观察者
用 submit(CognitiveEvent(...)) 投递，并检查 Submission.status。handler 是接收
事件的异步函数，例如根据事件类型调用 agent.tick()。关闭时 await stop()。
所有主 LLM 调用必须经由同一个 handler 且在它返回前结束；其他调用路径、本地
或远端服务的内部并发不受本类约束。

队列按数量而非字节有界（最多 max_pending 个等待事件及一个运行事件），调用方
须限制 payload 大小及自己持有的回执数量。相同 (kind, key) 的等待事件合并为
最新 payload，合并者共用回执；运行中事件不合并，而是最多有一个后续事件。
已接收的关键事件不会因队满被淘汰：全是关键事件时显式拒绝新事件，生产者
必须处理拒绝/重试或另行持久化。关闭、任务失败和抢占也通过回执报告，不
承诺进程崩溃后的持久性或实时响应。

P0 绕过冷却并请求取消正在运行的非 P0；明确的 story_preempt 也可中断其他 P0。取消是协作式的，必须等 handler
真正退出后才执行 P0。若 handler 忽略取消，P0 和 stop 都会等待，不保证时限。
调度器不是线程安全的：所有 API 和回执均属于 start() 所在的事件循环。
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple


class Priority(IntEnum):
    """数字越小优先级越高；同级遵循首次入队顺序。"""

    P0_INTERRUPT = 0
    P1_REACTIVE = 1
    P2_PLAN = 2
    P3_REFLECTION = 3


@dataclass(frozen=True)
class CognitiveEvent:
    """投递事件；key 只在同一 kind 下参与等待事件合并。"""

    kind: str
    key: str
    priority: Priority
    payload: Any = None
    critical: bool = False
    not_before: float = 0.0  # 单调时钟上的绝对秒数


class SubmissionStatus(str, Enum):
    """投递结果；拒绝时没有回执，调用方必须自行处理。"""

    ACCEPTED = "accepted"
    COALESCED = "coalesced"
    REJECTED_FULL = "rejected_full"
    REJECTED_STOPPED = "rejected_stopped"


class OutcomeStatus(str, Enum):
    """已接收事件的最终状态，包含有损终止原因。"""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    EVICTED = "evicted"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Outcome:
    """执行结果或可观察的失败/取消；异常由调用方决定如何记录。"""

    status: OutcomeStatus
    value: Any = None
    error: Optional[BaseException] = None


class Receipt:
    """多个合并投递共享的回执；取消一个等待者不会取消共享回执。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._future: asyncio.Future[Outcome] = loop.create_future()

    async def wait(self) -> Outcome:
        """等待最终结果；等待者取消不会影响调度或其他等待者。"""
        return await asyncio.shield(self._future)

    @property
    def done(self) -> bool:
        """是否已经得到最终结果。"""
        return self._future.done()

    def _finish(self, outcome: Outcome) -> None:
        """至多发布一次结果。"""
        if not self._future.done():
            self._future.set_result(outcome)


@dataclass(frozen=True)
class Submission:
    """投递状态与可选回执；被淘汰的事件通过其自己的回执获知。"""

    status: SubmissionStatus
    receipt: Optional[Receipt] = None


@dataclass
class _Entry:
    """队列内部记录；保留首次到达次序和共享回执。"""

    event: CognitiveEvent
    order: int
    receipt: Receipt


async def _default_wait(wakeup: asyncio.Event, delay: Optional[float]) -> None:
    """等待新投递/停止或冷却到期，允许由测试替换。"""
    if delay is None:
        await wakeup.wait()
    else:
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


class PlayerScheduler:
    """有界优先队列和单一串行 handler；生命周期为单次 start/stop。"""

    def __init__(
        self,
        handler: Callable[[CognitiveEvent], Awaitable[Any]],
        *,
        max_pending: int = 64,
        cooldowns: Optional[Mapping[str, float]] = None,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[asyncio.Event, Optional[float]], Awaitable[None]] = _default_wait,
    ) -> None:
        """cooldowns 按 kind 限制两次开始间隔；clock 和等待机制可注入。"""
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._cooldowns: Dict[str, float] = {}
        for kind, seconds in (cooldowns or {}).items():
            if not isinstance(kind, str) or not kind or len(kind) > 128:
                raise ValueError("invalid cooldown kind")
            seconds = float(seconds)
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("cooldown must be finite and non-negative")
            self._cooldowns[kind] = seconds
        self._handler = handler
        self._capacity = max_pending
        self._clock = clock
        self._wait = wait
        # 只记录配置过的 kind，避免任意事件名使冷却表无限增长。
        self._last_started: Dict[str, float] = {}
        self._pending: Dict[Tuple[str, str], _Entry] = {}
        self._next_order = 0
        self._wakeup = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker: Optional[asyncio.Task] = None
        self._active: Optional[_Entry] = None
        self._active_task: Optional[asyncio.Task] = None
        self._cancel_status: Optional[OutcomeStatus] = None
        self._stopping = False
        self._closed = False

    def _check_loop(self) -> None:
        """拒绝跨线程/事件循环调用，确保无锁状态变更是原子的。"""
        if self._loop is None or asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("scheduler must be used on its start event loop")

    def start(self) -> None:
        """在当前事件循环启动唯一 worker；不允许重复启动或重启。"""
        if self._loop is not None or self._closed:
            raise RuntimeError("scheduler already started or closed")
        self._loop = asyncio.get_running_loop()
        self._worker = self._loop.create_task(self._run())

    @property
    def pending_count(self) -> int:
        """当前等待数，不包含正在运行的一个事件。"""
        return len(self._pending)

    def contains(self, kind: str, key: str) -> bool:
        """判断同一事件是否等待或执行，防止恢复扫描重复投递。"""
        self._check_loop()
        return ((kind, key) in self._pending or
                (self._active is not None and (self._active.event.kind, self._active.event.key) == (kind, key)))

    def submit(self, event: CognitiveEvent) -> Submission:
        """非阻塞投递；队满不阻塞生产者，也不悄悄丢弃关键事件。"""
        self._check_loop()
        if self._stopping or self._closed:
            return Submission(SubmissionStatus.REJECTED_STOPPED)
        if not isinstance(event, CognitiveEvent):
            raise TypeError("event must be a CognitiveEvent")
        if not isinstance(event.kind, str) or not event.kind or len(event.kind) > 128:
            raise ValueError("kind must contain 1..128 characters")
        if not isinstance(event.key, str) or not event.key or len(event.key) > 256:
            raise ValueError("key must contain 1..256 characters")
        if not isinstance(event.priority, Priority):
            raise ValueError("priority must be a Priority")
        if not isinstance(event.critical, bool):
            raise ValueError("critical must be a bool")
        if not isinstance(event.not_before, (int, float)) or not math.isfinite(event.not_before):
            raise ValueError("not_before must be a finite monotonic time")
        if event.priority == Priority.P0_INTERRUPT and event.not_before > self._clock():
            raise ValueError("P0 cannot be delayed")
        key = (event.kind, event.key)
        existing = self._pending.get(key)
        if existing is not None:
            previous = existing.event
            # 不允许合并操作降低优先级或清除关键性；最新内容取代旧内容。
            event = CognitiveEvent(event.kind, event.key, min(event.priority, previous.priority),
                                   event.payload, event.critical or previous.critical,
                                   min(event.not_before, previous.not_before))
            existing.event = event
            self._interrupt_if_needed(event)
            self._wakeup.set()
            return Submission(SubmissionStatus.COALESCED, existing.receipt)
        if len(self._pending) >= self._capacity:
            candidates = [entry for entry in self._pending.values()
                          if not self._is_critical(entry.event)]
            # 同级不驱逐，防止新事件反复淘汰先到者；关键事件可驱逐非关键事件。
            victim = max(candidates, key=lambda e: (e.event.priority, e.order), default=None)
            if victim is None or (not self._is_critical(event)
                                  and event.priority >= victim.event.priority):
                return Submission(SubmissionStatus.REJECTED_FULL)
            del self._pending[(victim.event.kind, victim.event.key)]
            victim.receipt._finish(Outcome(OutcomeStatus.EVICTED))
        receipt = Receipt(self._loop)
        self._pending[key] = _Entry(event, self._next_order, receipt)
        self._next_order += 1
        self._interrupt_if_needed(event)
        self._wakeup.set()
        return Submission(SubmissionStatus.ACCEPTED, receipt)

    @staticmethod
    def _is_critical(event: CognitiveEvent) -> bool:
        """P0 始终关键，其他事件可由生产者显式标记。"""
        return event.critical or event.priority == Priority.P0_INTERRUPT

    def _interrupt_if_needed(self, event: CognitiveEvent) -> None:
        """P0 安全中断可抢占其他认知任务；不并行运行 handler。"""
        if (event.priority == Priority.P0_INTERRUPT and self._active is not None
                and (self._active.event.priority != Priority.P0_INTERRUPT
                     or (event.kind == "story_preempt" and self._active.event.kind != "story_preempt"))
                and self._active_task is not None and not self._active_task.done()):
            if self._cancel_status is None:
                self._cancel_status = OutcomeStatus.INTERRUPTED
                self._active_task.cancel()

    def _ready_at(self, event: CognitiveEvent) -> float:
        """P0 无冷却；其他事件服从 not_before 和 kind 冷却。"""
        if event.priority == Priority.P0_INTERRUPT:
            return float('-inf')
        last = self._last_started.get(event.kind)
        return max(event.not_before, last + self._cooldowns[event.kind]
                   if last is not None else event.not_before)

    async def _invoke(self, event: CognitiveEvent) -> Any:
        """将同步调用异常和任意 awaitable 一并纳入单事件错误处理。"""
        return await self._handler(event)

    async def _run(self) -> None:
        """仅在 handler 退出后选择下一个就绪事件；冷却事件不阻塞其他事件。"""
        try:
            while not self._stopping:
                self._wakeup.clear()
                now = self._clock()
                ready = [e for e in self._pending.values() if self._ready_at(e.event) <= now]
                if not ready:
                    next_time = min((self._ready_at(e.event) for e in self._pending.values()),
                                    default=None)
                    await self._wait(self._wakeup, None if next_time is None else max(0.0, next_time - now))
                    continue
                entry = min(ready, key=lambda e: (e.event.priority, e.order))
                del self._pending[(entry.event.kind, entry.event.key)]
                self._active = entry
                self._cancel_status = None
                if entry.event.kind in self._cooldowns:
                    self._last_started[entry.event.kind] = now
                self._active_task = asyncio.create_task(self._invoke(entry.event))
                try:
                    value = await asyncio.shield(self._active_task)
                    outcome = Outcome(self._cancel_status or OutcomeStatus.COMPLETED, value=value)
                except asyncio.CancelledError:
                    if not self._active_task.cancelled():
                        # worker 自身被取消时，也要等待 handler 的协作清理。
                        self._active_task.cancel()
                        try:
                            await self._active_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        entry.receipt._finish(Outcome(OutcomeStatus.STOPPED))
                        raise
                    outcome = Outcome(self._cancel_status or OutcomeStatus.INTERRUPTED)
                except Exception as exc:  # 单事件失败不终止 worker
                    outcome = Outcome(self._cancel_status or OutcomeStatus.FAILED, error=exc)
                entry.receipt._finish(outcome)
                self._active = None
                self._active_task = None
        finally:
            self._stopping = True
            self._closed = True
            if self._active is not None:
                self._active.receipt._finish(Outcome(OutcomeStatus.STOPPED))
            for entry in self._pending.values():
                entry.receipt._finish(Outcome(OutcomeStatus.STOPPED))
            self._pending.clear()

    async def stop(self) -> None:
        """拒绝新投递并协作取消活动任务，等待 worker 与清理完成。"""
        self._check_loop()
        if self._active_task is asyncio.current_task():
            raise RuntimeError("handler cannot await its own scheduler stop")
        if not self._stopping:
            self._stopping = True
            if self._active_task is not None and not self._active_task.done():
                # 不重复 cancel，以免打断 handler 已开始的取消清理。
                if self._cancel_status is None:
                    self._cancel_status = OutcomeStatus.STOPPED
                    self._active_task.cancel()
            self._wakeup.set()
        if self._worker is not None:
            # stop 调用者取消时不应连带杀掉 worker 或共享清理过程。
            await asyncio.shield(self._worker)
