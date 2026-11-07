#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认知调度器的独立测试：不连接游戏、网络或实际模型。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_scheduler import (  # noqa: E402
    CognitiveEvent, OutcomeStatus, PlayerScheduler, Priority, SubmissionStatus,
)


def event(key, priority=Priority.P2_PLAN, *, kind="think", critical=False,
          not_before=0.0, payload=None):
    """构造简洁的测试事件。"""
    return CognitiveEvent(kind, key, priority, payload, critical, not_before)


def run(coro):
    """以超时包住场景，避免回归时测试无限挂起。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=3))


def test_contains_reports_pending_and_active_without_requeue():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(item):
            entered.set()
            await release.wait()
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        receipt = scheduler.submit(event("conversation", Priority.P3_REFLECTION, kind="deep_reflection"))
        assert scheduler.contains("deep_reflection", "conversation")
        await entered.wait()
        assert scheduler.contains("deep_reflection", "conversation")
        assert not scheduler.contains("deep_reflection", "other")
        release.set()
        await receipt.receipt.wait()
        assert not scheduler.contains("deep_reflection", "conversation")
        await scheduler.stop()
    run(scenario())


def test_priority_fifo_and_latest_coalescing():
    """同级 FIFO；相同键保留首次顺序并用最新内容，回执共享。"""
    async def scenario():
        seen = []
        async def handler(item):
            seen.append((item.key, item.payload, item.priority, item.critical))
            return item.payload
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        first = scheduler.submit(event("a", Priority.P3_REFLECTION, payload=1))
        scheduler.submit(event("b", Priority.P1_REACTIVE))
        merged = scheduler.submit(event("a", Priority.P2_PLAN, payload=2, critical=True))
        last = scheduler.submit(event("c", Priority.P2_PLAN))
        assert merged.status is SubmissionStatus.COALESCED
        assert first.receipt is merged.receipt
        assert (await first.receipt.wait()).value == 2
        await last.receipt.wait()
        await scheduler.stop()
        assert seen == [("b", None, Priority.P1_REACTIVE, False),
                        ("a", 2, Priority.P2_PLAN, True),
                        ("c", None, Priority.P2_PLAN, False)]
    run(scenario())


def test_capacity_eviction_and_critical_rejection_are_observable():
    """队满驱逐低优先级非关键事件；已接收的关键事件绝不被驱逐。"""
    async def scenario():
        scheduler = PlayerScheduler(lambda item: asyncio.sleep(0), max_pending=2)
        scheduler.start()
        weak = scheduler.submit(event("weak", Priority.P3_REFLECTION))
        critical = scheduler.submit(event("critical", Priority.P2_PLAN, critical=True))
        urgent = scheduler.submit(event("urgent", Priority.P0_INTERRUPT))
        assert urgent.status is SubmissionStatus.ACCEPTED
        assert (await weak.receipt.wait()).status is OutcomeStatus.EVICTED
        refused = scheduler.submit(event("other", Priority.P0_INTERRUPT))
        assert refused.status is SubmissionStatus.REJECTED_FULL
        assert refused.receipt is None
        assert scheduler.pending_count == 2
        assert (await urgent.receipt.wait()).status is OutcomeStatus.COMPLETED
        assert (await critical.receipt.wait()).status is OutcomeStatus.COMPLETED
        await scheduler.stop()
    run(scenario())


def test_equal_priority_does_not_evict_and_critical_can_evict():
    """同级新事件拒绝，关键事件即使优先级较低也可驱逐非关键事件。"""
    async def scenario():
        scheduler = PlayerScheduler(lambda item: asyncio.sleep(0), max_pending=1)
        scheduler.start()
        old = scheduler.submit(event("old", Priority.P1_REACTIVE))
        assert scheduler.submit(event("new", Priority.P1_REACTIVE)).status is SubmissionStatus.REJECTED_FULL
        critical = scheduler.submit(event("critical", Priority.P3_REFLECTION, critical=True))
        assert critical.status is SubmissionStatus.ACCEPTED
        assert (await old.receipt.wait()).status is OutcomeStatus.EVICTED
        assert (await critical.receipt.wait()).status is OutcomeStatus.COMPLETED
        await scheduler.stop()
    run(scenario())


def test_p0_interrupt_waits_for_cleanup_and_never_overlaps():
    """P0 请求中断后等待取消清理，不会并发调用主 handler。"""
    async def scenario():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active = 0
        maximum = 0
        seen = []
        async def handler(item):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            seen.append(item.key)
            try:
                if item.key == "plan":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cleaning.set()
                        await release.wait()
                        raise
            finally:
                active -= 1
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        plan = scheduler.submit(event("plan"))
        await started.wait()
        p0 = scheduler.submit(event("stop", Priority.P0_INTERRUPT))
        await cleaning.wait()
        await asyncio.sleep(0)
        assert seen == ["plan"]
        release.set()
        assert (await plan.receipt.wait()).status is OutcomeStatus.INTERRUPTED
        assert (await p0.receipt.wait()).status is OutcomeStatus.COMPLETED
        assert seen == ["plan", "stop"] and maximum == 1
        await scheduler.stop()
    run(scenario())


def test_active_duplicate_has_one_bounded_followup():
    """运行中同键的后续事件合并，不与当前运行的回执混淆。"""
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        values = []
        async def handler(item):
            values.append(item.payload)
            if item.payload == 1:
                started.set()
                await release.wait()
        scheduler = PlayerScheduler(handler, max_pending=1)
        scheduler.start()
        first = scheduler.submit(event("same", payload=1))
        await started.wait()
        second = scheduler.submit(event("same", payload=2))
        third = scheduler.submit(event("same", payload=3))
        assert second.receipt is third.receipt and first.receipt is not second.receipt
        assert scheduler.pending_count == 1
        release.set()
        await first.receipt.wait()
        await third.receipt.wait()
        assert values == [1, 3]
        await scheduler.stop()
    run(scenario())


def test_cooldown_injected_clock_and_wait_other_events_progress():
    """注入时钟与等待器；冷却期间其他类别仍可执行，时间到期再执行。"""
    async def scenario():
        now = [10.0]
        waiting = asyncio.Event()
        delays = []
        seen = []
        async def waiter(wake, delay):
            delays.append(delay)
            waiting.set()
            await wake.wait()
        async def handler(item):
            seen.append((item.key, now[0]))
        scheduler = PlayerScheduler(handler, cooldowns={"think": 5},
                                    clock=lambda: now[0], wait=waiter)
        scheduler.start()
        first = scheduler.submit(event("one"))
        await first.receipt.wait()
        second = scheduler.submit(event("two", Priority.P1_REACTIVE))
        other = scheduler.submit(event("other", kind="other"))
        await other.receipt.wait()
        await waiting.wait()
        assert seen == [("one", 10.0), ("other", 10.0)]
        assert delays[-1] == 5.0 and not second.receipt.done
        now[0] = 15.0
        # 一个新投递唤醒假等待器，模拟时钟到期。
        wake = scheduler.submit(event("wake", kind="other"))
        await second.receipt.wait()
        await wake.receipt.wait()
        assert ("two", 15.0) in seen
        await scheduler.stop()
    run(scenario())


def test_not_before_and_p0_bypass_cooldown():
    """P0 不受同类冷却阻挡；非 P0 遵守 not_before。"""
    async def scenario():
        now = [1.0]
        wait_called = asyncio.Event()
        async def waiter(wake, delay):
            wait_called.set()
            await wake.wait()
        seen = []
        async def handler(item):
            seen.append(item.key)
        scheduler = PlayerScheduler(handler, cooldowns={"think": 100},
                                    clock=lambda: now[0], wait=waiter)
        scheduler.start()
        await scheduler.submit(event("first")).receipt.wait()
        delayed = scheduler.submit(event("delayed", not_before=10))
        p0 = scheduler.submit(event("p0", Priority.P0_INTERRUPT))
        await p0.receipt.wait()
        assert seen == ["first", "p0"]
        await wait_called.wait()
        assert not delayed.receipt.done
        await scheduler.stop()
        assert (await delayed.receipt.wait()).status is OutcomeStatus.STOPPED
    run(scenario())


def test_stop_cancels_active_and_pending_and_is_idempotent():
    """关闭可观察且幂等，关闭期间拒绝新事件。"""
    async def scenario():
        started = asyncio.Event()
        async def handler(item):
            started.set()
            await asyncio.Event().wait()
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        active = scheduler.submit(event("active"))
        await started.wait()
        pending = scheduler.submit(event("pending"))
        await scheduler.stop()
        assert (await active.receipt.wait()).status is OutcomeStatus.STOPPED
        assert (await pending.receipt.wait()).status is OutcomeStatus.STOPPED
        assert scheduler.submit(event("late")).status is SubmissionStatus.REJECTED_STOPPED
        await scheduler.stop()
    run(scenario())


def test_handler_failure_does_not_kill_worker_and_receipt_wait_is_shielded():
    """处理异常有回执且 worker 继续；取消一个回执等待者不影响处理。"""
    async def scenario():
        release, started = asyncio.Event(), asyncio.Event()
        async def handler(item):
            if item.key == "bad":
                raise ValueError("bad")
            started.set()
            await release.wait()
            return 42
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        bad = scheduler.submit(event("bad"))
        outcome = await bad.receipt.wait()
        assert outcome.status is OutcomeStatus.FAILED and isinstance(outcome.error, ValueError)
        good = scheduler.submit(event("good"))
        await started.wait()
        waiter = asyncio.create_task(good.receipt.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert (await good.receipt.wait()).value == 42
        await scheduler.stop()
    run(scenario())


def test_synchronous_handler_error_and_future_return():
    """同步调用失败可见；普通 Future 也能作为 handler 的 awaitable。"""
    async def scenario():
        def handler(item):
            if item.key == "bad":
                raise ValueError("synchronous failure")
            future = asyncio.get_running_loop().create_future()
            future.set_result("ok")
            return future
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        bad = scheduler.submit(event("bad"))
        good = scheduler.submit(event("good"))
        result = await bad.receipt.wait()
        assert result.status is OutcomeStatus.FAILED and isinstance(result.error, ValueError)
        assert (await good.receipt.wait()).value == "ok"
        await scheduler.stop()
    run(scenario())


def test_suppressed_interrupt_is_reported_and_remains_serial():
    """handler 吞掉取消仍等待其退出，回执不得误报正常完成。"""
    async def scenario():
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []
        async def handler(item):
            seen.append(item.key)
            if item.key == "plan":
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                    return "suppressed"
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        plan = scheduler.submit(event("plan"))
        await started.wait()
        interrupt = scheduler.submit(event("interrupt", Priority.P0_INTERRUPT))
        await cancelled.wait()
        assert not plan.receipt.done and seen == ["plan"]
        release.set()
        assert (await plan.receipt.wait()).status is OutcomeStatus.INTERRUPTED
        await interrupt.receipt.wait()
        assert seen == ["plan", "interrupt"]
        await scheduler.stop()
    run(scenario())


def test_bounded_duplicate_storm():
    """大量重复事件不增加队列和回执，饱和队列明确拒绝新键。"""
    async def scenario():
        scheduler = PlayerScheduler(lambda item: asyncio.sleep(0), max_pending=2)
        scheduler.start()
        original = scheduler.submit(event("same"))
        for n in range(10000):
            duplicate = scheduler.submit(event("same", payload=n))
            assert duplicate.receipt is original.receipt
        scheduler.submit(event("second"))
        assert scheduler.pending_count == 2
        assert scheduler.submit(event("third")).status is SubmissionStatus.REJECTED_FULL
        assert (await original.receipt.wait()).status is OutcomeStatus.COMPLETED
        await scheduler.stop()
    run(scenario())


def test_input_and_lifecycle_validation():
    """拒绝无效配置、延迟 P0 及重复启动。"""
    with pytest.raises(ValueError):
        PlayerScheduler(lambda item: asyncio.sleep(0), max_pending=0)
    with pytest.raises(ValueError):
        PlayerScheduler(lambda item: asyncio.sleep(0), cooldowns={"x": float("nan")})
    async def scenario():
        scheduler = PlayerScheduler(lambda item: asyncio.sleep(0), clock=lambda: 1.0)
        scheduler.start()
        with pytest.raises(RuntimeError):
            scheduler.start()
        with pytest.raises(ValueError):
            scheduler.submit(event("p0", Priority.P0_INTERRUPT, not_before=2.0))
        with pytest.raises(ValueError):
            scheduler.submit(event("invalid", not_before=float("nan")))
        await scheduler.stop()
    run(scenario())
