#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 的最小 deliberative 循环。

只做「按固定间隔调用 agent.tick」，不实现 fast/reactive/deep 多尺度循环
（那属于 Phase 3+）。单线程顺序执行，天然避免同一 agent 并发 tick。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

LOGGER = logging.getLogger("player.loop")

MIN_INTERVAL_S = 0.1
MIN_WAIT_S = 0.05


class DeliberativeLoop:
    """固定间隔的思考循环；tick 本身不应该超过 interval。"""

    def __init__(
        self,
        agent: Any,
        interval: float,
        *,
        clock: Optional[Callable[[], float]] = None,
        logger: Any = None,
    ):
        self.agent = agent
        self.interval = max(MIN_INTERVAL_S, float(interval))
        self._clock = clock or time.monotonic
        self.log = logger or LOGGER

    def wait_seconds(self, started_at: float, now: Optional[float] = None) -> float:
        """返回距离下一次 tick 还需要等待的秒数（可测试的纯计算）。"""
        moment = self._clock() if now is None else now
        return max(MIN_WAIT_S, self.interval - max(0.0, moment - started_at))

    async def run_once(self) -> Any:
        """立即执行一轮（不等待间隔），便于测试与启动首轮。"""
        return await self.agent.tick(force=True)

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """持续运行直到 stop_event 被设置或任务被取消。"""
        while stop_event is None or not stop_event.is_set():
            started_at = self._clock()
            try:
                await self.agent.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单轮异常不能终止整场录制
                self.log.exception("shadow 思考循环单轮失败；下一轮继续")
            delay = self.wait_seconds(started_at)
            if stop_event is None:
                await asyncio.sleep(delay)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
