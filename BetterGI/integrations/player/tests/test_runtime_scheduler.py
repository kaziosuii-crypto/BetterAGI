#!/usr/bin/env python3
"""Observer runtime wiring with synthetic Live messages, without BetterGI."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_dialogue import DialogueTracker  # noqa: E402
from player_fast_perception import FastPerception  # noqa: E402
from player_scheduler import PlayerScheduler  # noqa: E402
from run_shadow import _ActivityClock, _fast_perception_loop, _receive_loop  # noqa: E402


class _Message:
    def __init__(self, payload):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class _Socket:
    def __init__(self, payloads):
        self.payloads = payloads

    async def __aiter__(self):
        for payload in self.payloads:
            yield _Message(payload)
            await asyncio.sleep(0)


class _Agent:
    def __init__(self):
        self.dialogue = DialogueTracker()
        self.seen = []

    def observe(self, payload):
        self.seen.append(payload)


def test_planning_slows_when_scene_is_stable():
    now = [0.0]
    activity = _ActivityClock(clock=lambda: now[0])
    assert activity.planning_interval(12) == 15
    activity.mark()
    assert activity.planning_interval(12) == 3
    now[0] = 15
    assert activity.planning_interval(12) == 12
    now[0] = 31
    assert activity.planning_interval(12) == 15
    activity.mark()
    assert activity.planning_interval(12) == 3


def test_live_events_reach_serial_scheduler_without_network():
    async def scenario():
        handled = []
        async def handler(event):
            handled.append(event.kind)
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        agent = _Agent()
        stop = asyncio.Event()
        await _receive_loop(_Socket([
            {"type": "frame", "seq": 1, "uiCategory": "Talk", "jpeg": "synthetic"},
            {"type": "frame", "seq": 2, "uiCategory": "Talk", "jpeg": "synthetic"},
            {"type": "error", "code": "temporary"},
        ]), agent, stop, scheduler)
        for _ in range(30):
            if len(handled) == 2:
                break
            await asyncio.sleep(0.001)
        await scheduler.stop()
        assert handled == ["dialogue_started", "live_error"]
        assert len(agent.seen) == 3
        assert agent.dialogue.is_locked()
    asyncio.run(scenario())


def test_fast_roi_changes_reach_scheduler_without_claiming_dialogue():
    from PIL import Image

    def frame(seq, color):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), color).save(buffer, "JPEG")
        return {"type": "frame", "seq": seq, "timestampUtc": "2026-01-01T00:00:00Z",
                "jpeg": base64.b64encode(buffer.getvalue()).decode()}

    async def scenario():
        handled = []
        async def handler(event):
            handled.append(event)
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        agent = _Agent()
        queue = asyncio.Queue(maxsize=1)
        detector = FastPerception({"dialogue_text": {"x": 0, "y": 0, "width": 1, "height": 1}})
        worker = asyncio.create_task(_fast_perception_loop(queue, scheduler, detector, agent))
        await queue.put((0, frame(1, "black")))
        await asyncio.sleep(0.12)
        await queue.put((0, frame(2, "white")))
        for _ in range(50):
            if handled:
                break
            await asyncio.sleep(0.01)
        await queue.put(None)
        await worker
        await scheduler.stop()
        assert [event.kind for event in handled] == ["region_changed"]
        assert handled[0].payload["frame_seq"] == 2
        assert handled[0].payload["region"] == "dialogue_text"
        assert not agent.dialogue.is_locked()
    asyncio.run(scenario())


def test_unclassified_live_frames_do_not_invent_story_events():
    async def scenario():
        handled = []
        async def handler(event):
            handled.append(event.kind)
        scheduler = PlayerScheduler(handler)
        scheduler.start()
        agent = _Agent()
        await _receive_loop(_Socket([
            {"type": "frame", "seq": 1, "jpeg": "ignored"},
            {"type": "frame", "seq": 2, "jpeg": "ignored"},
        ]), agent, asyncio.Event(), scheduler)
        await asyncio.sleep(0.01)
        await scheduler.stop()
        assert handled == []
        assert not agent.dialogue.is_locked()
    asyncio.run(scenario())
