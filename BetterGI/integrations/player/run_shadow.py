#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以 shadow 模式运行 PersistentPlayerAgent。

它像一个只读观察者连接 BetterGI Live：接收帧、指导、解说与动作状态，
生成「如果是我会怎么想/怎么做」，写入独立的 shadow JSONL。
它不发送动作、指导、解说，也不调用任何 BetterGI HTTP 写接口。

示例::

    set BETTERGI_AGENT_TOKEN=...
    python integrations/player/run_shadow.py \
        --config integrations/player/player_config.example.json

无 LLM、只验证链路::

    python integrations/player/run_shadow.py --no-llm
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# 复用 lumine 的 LLM 传输层；player 自己不实现网络客户端。
LUMINE_DIR = HERE.parent / "lumine"
if str(LUMINE_DIR) not in sys.path:
    sys.path.insert(0, str(LUMINE_DIR))

from player_agent import PersistentPlayerAgent  # noqa: E402
from player_active_gate import GateConfig, GateDecision, PlayerActiveGate  # noqa: E402
from player_config import (  # noqa: E402
    PlayerConfig,
    build_ws_url,
    ensure_directories,
    redact_url,
    resolve_config,
)
from player_context_builder import ContextBuilder  # noqa: E402
from player_cognitive_transaction import CognitiveTransactionError, PlayerCognitiveTransaction  # noqa: E402
from player_dialogue import DialogueTracker  # noqa: E402
from player_event_log import EventLogConflict, EventLogError, PlayerEventLog  # noqa: E402
from player_experience import PlayerExperience  # noqa: E402
from player_beliefs import BeliefError, BeliefStore  # noqa: E402
from player_emotion import EmotionError  # noqa: E402
from player_relationships import RelationshipError, RelationshipStore  # noqa: E402
from player_threads import ThreadError, ThreadStore  # noqa: E402
from player_episodic_memory import EpisodeError  # noqa: E402
from player_scheduler import CognitiveEvent, PlayerScheduler, Priority, SubmissionStatus  # noqa: E402
from player_intent import IntentPublisher  # noqa: E402
from player_instance import InstanceLockError, PlayerInstanceLock  # noqa: E402
from player_knowledge import EvidenceStore  # noqa: E402
from player_profile import (  # noqa: E402
    ProfileError,
    load_profile,
    seed_default_profile,
)
from player_recap import RecapError  # noqa: E402
from player_semantic_memory import SemanticMemoryError  # noqa: E402
from player_state import StateError, StateStore  # noqa: E402
from player_working_memory import WorkingMemoryError, default_working  # noqa: E402
from player_vision import ScreenObserver, validate_local_pair  # noqa: E402
from shadow_recorder import ShadowRecorder, new_run_id, redact_secrets  # noqa: E402

try:
    import aiohttp  # type: ignore
except ImportError:  # pragma: no cover - 环境缺少依赖时由 main 报错
    aiohttp = None  # type: ignore

try:
    import llm_client  # type: ignore
except ImportError:  # pragma: no cover
    llm_client = None  # type: ignore

LOGGER = logging.getLogger("player.shadow")


def configure_stdio() -> None:
    """Windows 控制台默认编码可能不是 UTF-8，日志中文不能因此崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="以 shadow 模式观察实况并记录玩家提案（不发送任何操作）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["observer", "motor_active", "full_active"],
        default="observer", help="运行模式；主动模式在安全门完成之前不可启动",
    )
    parser.add_argument("--controlled-mvp", action="store_true",
                        help="明确启用人工确认的受控模式；不能证明游戏响应或输入独占")
    parser.add_argument("--config", default="", help="JSON 配置路径")
    parser.add_argument("--journey", action="store_true",
                        help="只读显示当前玩家可追溯的旅程回顾，不连接游戏")
    parser.add_argument("--journey-limit", type=int, default=30, help="旅程回顾条目上限（1–100）")
    parser.add_argument("--home", default="", help="玩家数据根目录")
    parser.add_argument("--profile", default="", help="玩家档案路径")
    parser.add_argument("--state", default="", help="resume 状态路径")
    parser.add_argument("--shadow-dir", default="", help="shadow JSONL 目录")
    parser.add_argument("--memory-dir", default="", help="结构化记忆根目录")
    parser.add_argument("--episodes-dir", default="", help="episodes 目录")
    parser.add_argument("--semantic-path", default="", help="semantic facts 文件")
    parser.add_argument("--working-path", default="", help="working memory 文件")
    parser.add_argument(
        "--context-builder",
        choices=["v2", "legacy"],
        default=None,
        help="ContextBuilder 版本；legacy 回退 Phase 1 拼接",
    )
    parser.add_argument(
        "--ws-url", default="", help="BetterGI Live WebSocket 地址"
    )
    parser.add_argument("--token", default="", help="访问令牌（建议用环境变量）")
    parser.add_argument("--token-file", default="", help="从文件读取访问令牌")
    parser.add_argument("--session-id", default="", help="会话标识")
    parser.add_argument("--observe-ocr", action="store_true", help="低频请求 BetterGI 截图与 OCR；会写 BetterGI 观察事件")
    parser.add_argument("--observe-http", default="http://127.0.0.1:30648", help="本机 BetterGI HTTP 服务地址")
    parser.add_argument("--observe-interval", type=float, default=1.0, help="事件驱动屏幕观察的最短间隔（秒）")
    parser.add_argument(
        "--ocr-roi", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"),
        help="截图原始像素坐标的 OCR 区域；未指定时只观察 UI，不做全屏 OCR",
    )
    parser.add_argument(
        "--named-ocr", action="append", nargs=5,
        metavar=("NAME", "X", "Y", "WIDTH", "HEIGHT"),
        help="具名原帧像素区域，可重复；NAME 为 dialogue_text/speaker/choice/quest/hud",
    )
    parser.add_argument(
        "--dialogue-roi", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="对话文本区归一化边界；需按实际界面校准",
    )
    parser.add_argument(
        "--choice-roi", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="对话选项区归一化边界；需按实际界面校准",
    )
    parser.add_argument(
        "--speaker-roi", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="说话人区归一化边界；需按实际界面校准",
    )
    parser.add_argument(
        "--quest-roi", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="任务区域归一化边界；只检测画面变化",
    )
    parser.add_argument(
        "--hud-roi", type=float, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="HUD 区域归一化边界；只检测画面变化",
    )
    parser.add_argument(
        "--fast-hz", type=float, default=10.0,
        help="已配置区域的轻量画面变化采样率（5–15 Hz，不是剧情识别置信度）",
    )
    parser.add_argument(
        "--intent-url", default="",
        help="Bridge v2 意图总线地址（如 http://127.0.0.1:30700）；设置后进入意图控制模式",
    )
    parser.add_argument(
        "--intent-token", default="",
        help="Bridge 意图总线 Bearer 令牌（默认读 BRIDGE_INTENT_TOKEN）",
    )
    parser.add_argument("--trusted-motor-config", default=os.environ.get("PLAYER_TRUSTED_MOTOR_CONFIG", ""),
                        help="玩家目录内的本机 Motor 信任配置 JSON，须显式指定可信 origin")
    parser.add_argument("--import-legacy-state", action="store_true",
                        help="显式将当前玩家目录中的旧 JSON 状态导入空 SQLite 投影；不会删除原文件")
    parser.add_argument("--embedding-model", default="", help="可选本地 sentence-transformers 模型目录；不下载模型")
    parser.add_argument("--interval", type=float, default=None, help="思考间隔秒数")
    parser.add_argument(
        "--max-frame-age-ms", type=int, default=None, help="超过该帧龄不思考"
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=None,
        help="不调用 LLM，只记录观察心跳（用于链路验证）",
    )
    parser.add_argument(
        "--disabled",
        dest="enabled",
        action="store_false",
        default=None,
        help="显式禁用（回滚时使用）",
    )
    parser.add_argument(
        "--duration", type=float, default=0.0, help="运行秒数；0 = 直到会话结束"
    )
    parser.add_argument("--stop-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return parser


def _safe_error(exc: BaseException, token: str) -> str:
    # 异常里可能带完整 URL；先做模式脱敏，再按原值/URL 编码值兜底。
    text = redact_secrets(str(exc))
    if token:
        text = text.replace(token, "***")
        encoded = urllib.parse.quote(token, safe="")
        if encoded != token:
            text = text.replace(encoded, "***")
    return text


class _GuardedIntentPublisher:
    """Check the health lease at dispatch; this is not a remote enforcement receipt."""

    def __init__(self, publisher: IntentPublisher, gate: PlayerActiveGate) -> None:
        self._publisher = publisher
        self._gate = gate

    @property
    def last_cancel_result(self) -> dict:
        """Expose the bridge's unmodified cancellation response."""
        return self._publisher.last_cancel_result

    async def publish(self, intent: Any) -> bool:
        """The ordinary Motor path is blocked whenever dialogue is latched."""
        if not self._gate.can_publish_motor:
            return False
        return await self._publisher.publish(intent)

    async def publish_dialogue(self, intent: Any) -> dict:
        """The host separately authorizes and executes a Player dialogue decision."""
        if not self._gate.is_allowed:
            return {"status": "not_executed", "reason": "active_gate_unavailable"}
        return await self._publisher.publish_dialogue(intent)

    async def cancel(self) -> bool:
        """Allow cancellation even after admission has been revoked."""
        return await self._publisher.cancel()

    async def fetch_feedback(self) -> dict:
        """Read feedback through the existing bridge transport."""
        return await self._publisher.fetch_feedback()


class _ActivityClock:
    """最近真实事件决定计划频率；不把普通视频帧视为剧情变化。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last_event = None

    def mark(self) -> None:
        self._last_event = self._clock()

    def planning_interval(self, configured: float) -> float:
        base = min(15.0, max(3.0, float(configured)))
        if self._last_event is None:
            return 15.0
        age = max(0.0, self._clock() - self._last_event)
        if age < 10:
            return 3.0
        if age < 30:
            return base
        return 15.0


async def _receive_loop(
    ws: Any, agent: PersistentPlayerAgent, stop_event: asyncio.Event,
    scheduler: Optional[PlayerScheduler] = None,
    fast_queue: Optional[asyncio.Queue] = None,
) -> str:
    """接收 Live 数据，并把可验证的状态变化交给统一调度器。"""
    async for message in ws:
        if message.type == aiohttp.WSMsgType.TEXT:
            try:
                payload = json.loads(message.data)
            except (TypeError, ValueError):
                LOGGER.warning("收到无法解析的 WS 文本消息")
                continue
            if not isinstance(payload, dict):
                continue
            agent.observe(payload)
            if fast_queue is not None and payload.get("type") == "frame":
                if fast_queue.full():
                    fast_queue.get_nowait()
                fast_queue.put_nowait((getattr(agent, "_session_generation", 0), payload))
            if scheduler is not None:
                story_events = []
                tracker = getattr(agent, "dialogue", None)
                valid_frame = payload.get("type") == "frame" and isinstance(payload.get("jpeg"), str) and bool(payload["jpeg"])
                if valid_frame and isinstance(agent, PersistentPlayerAgent):
                    jpeg = payload["jpeg"]
                    if len(jpeg) > 4 * 1024 * 1024:
                        valid_frame = False
                    else:
                        try:
                            image = base64.b64decode(jpeg, validate=True)
                            valid_frame = image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")
                        except (ValueError, binascii.Error):
                            valid_frame = False
                    latest = agent._latest_frame
                    age = agent._frame_age_ms(latest, agent._clock()) if latest else 2**63 - 1
                    valid_frame = bool(valid_frame and latest and latest.get("seq") == payload.get("seq")
                                       and age < 2**63 - 1
                                       and (not agent.max_frame_age_ms or age <= agent.max_frame_age_ms))
                if valid_frame and tracker is not None:
                    story_events = tracker.observe_live_frame(payload).get("events", [])
                    on_story = getattr(agent, "handle_live_story_events", None)
                    if on_story is not None and story_events:
                        on_story(story_events)
                    callback = getattr(agent, "story_event_callback", None)
                    if callback is not None and story_events:
                        callback(story_events)
                if payload.get("type") == "error":
                    story_events.append({"type": "live_error"})
                if payload.get("type") == "action_attempt":
                    attempt = payload.get("executionAttempt")
                    current = getattr(getattr(agent, "intent_tracker", None), "current", None) or {}
                    if (isinstance(attempt, dict) and attempt.get("status") in {"failed", "aborted", "dropped"}
                            and attempt.get("intentId") == current.get("intent_id")
                            and attempt.get("motorGeneration") == current.get("motorGeneration")
                            and attempt.get("actionGroupId") == current.get("actionGroupId")
                            and type(attempt.get("motorGeneration")) is int):
                        story_events.append({"type": "motor_error", "attempt": attempt})
                for event in story_events:
                    kind = str(event.get("type") or "")
                    if not kind:
                        continue
                    priority = (Priority.P0_INTERRUPT if kind in {
                        "dialogue_started", "choice_presented", "motor_error"
                    } else Priority.P1_REACTIVE)
                    result = scheduler.submit(CognitiveEvent(
                        kind=kind, key="current", priority=priority,
                        payload=event, critical=priority == Priority.P0_INTERRUPT,
                    ))
                    if result.status in {SubmissionStatus.REJECTED_FULL,
                                        SubmissionStatus.REJECTED_STOPPED}:
                        LOGGER.error("认知事件未入队：%s (%s)", kind, result.status.value)
            if payload.get("type") == "live.stopped":
                stop_event.set()
                return "live.stopped"
        elif message.type == aiohttp.WSMsgType.ERROR:
            raise ConnectionError("WebSocket 错误：{0}".format(ws.exception()))
        elif message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        ):
            return "closed"
    return "closed"


async def _fast_perception_loop(
    queue: asyncio.Queue, scheduler: PlayerScheduler, detector: Any,
    agent: PersistentPlayerAgent, activity: Optional[_ActivityClock] = None,
) -> None:
    """单工作者消费最新帧；像素变化只作为复查信号，不视为剧情事实。"""
    generation = None
    while True:
        item = await queue.get()
        if item is None:
            return
        frame_generation, frame = item
        if frame_generation != generation:
            detector.reset()
            generation = frame_generation
        events = await asyncio.to_thread(detector.observe, frame)
        if frame_generation != getattr(agent, "_session_generation", 0):
            continue
        for event in events:
            if activity is not None:
                activity.mark()
            result = scheduler.submit(CognitiveEvent(
                kind="region_changed", key=str(event["region"]),
                priority=Priority.P1_REACTIVE, payload=event,
            ))
            if result.status == SubmissionStatus.REJECTED_FULL:
                LOGGER.warning("画面变化事件未入队：%s", event["region"])


async def _story_observation_loop(
    agent: PersistentPlayerAgent, scheduler: PlayerScheduler, stop_event: asyncio.Event,
    interval: float, activity: Optional[_ActivityClock] = None,
) -> None:
    """Poll the configured region independently from model latency."""
    while not stop_event.is_set():
        events = await agent.observe_story_once()
        for event in events:
            kind = str(event.get("type") or "")
            if not kind:
                continue
            if activity is not None:
                activity.mark()
            priority = (Priority.P0_INTERRUPT if kind in {"dialogue_started", "dialogue_choice_presented"}
                        else Priority.P3_REFLECTION if kind == "deep_reflection"
                        else Priority.P1_REACTIVE)
            key = ((event.get("target") or {}).get("conversation_event_id") or "current") if kind == "deep_reflection" else "current"
            outcome = scheduler.submit(CognitiveEvent(
                kind=kind, key=key, priority=priority, payload=event,
                critical=priority in {Priority.P0_INTERRUPT, Priority.P3_REFLECTION},
            ))
            if outcome.status in {SubmissionStatus.REJECTED_FULL, SubmissionStatus.REJECTED_STOPPED}:
                LOGGER.error("剧情事件未入队：%s (%s)", kind, outcome.status.value)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.5, interval))
        except asyncio.TimeoutError:
            pass


class _ReflectionBackoff:
    """Bound retry frequency while preserving durable pending conversations."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._entries: dict[tuple[str, str], tuple[int, float]] = {}

    def ready(self, target: dict) -> bool:
        key = (target["session_id"], target["conversation_event_id"])
        return self._clock() >= self._entries.get(key, (0, 0.0))[1]

    def mark(self, target: dict, *, success: bool) -> None:
        key = (target["session_id"], target["conversation_event_id"])
        if success:
            self._entries.pop(key, None)
            return
        attempts = self._entries.pop(key, (0, 0.0))[0] + 1
        self._entries[key] = (attempts, self._clock() + min(300.0, 5.0 * 2 ** min(attempts - 1, 6)))
        if len(self._entries) > 512:
            self._entries.pop(next(iter(self._entries)))


async def _recover_reflections_loop(agent: PersistentPlayerAgent, scheduler: PlayerScheduler,
                                    stop_event: asyncio.Event, *, interval: float = 5.0,
                                    backoff: Optional[_ReflectionBackoff] = None) -> None:
    """轮转扫描持久待反思对话；失败项不会永久挤占后续记录。"""
    cursor = 0
    while not stop_event.is_set():
        if scheduler.pending_count < 32:
            try:
                page = await asyncio.to_thread(agent.experience_store.pending_conversation_page,
                    agent.profile.player_id, after_seq=cursor, limit=8,
                    player_scope=agent._cognitive_scope if agent.cognitive_transaction is not None else None)
                rejected = False
                for target in page["items"]:
                    if (backoff is not None and not backoff.ready(target)) or scheduler.contains(
                            "deep_reflection", target["conversation_event_id"]):
                        continue
                    result = scheduler.submit(CognitiveEvent(
                        kind="deep_reflection", key=target["conversation_event_id"], priority=Priority.P3_REFLECTION,
                        payload={"target": target, "generation": agent._session_generation, "historical": True},
                        critical=False))
                    if result.status in {SubmissionStatus.REJECTED_FULL, SubmissionStatus.REJECTED_STOPPED}:
                        rejected = True
                        break
                if not rejected:
                    cursor = page["next_after_seq"] or 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - retain cursor and retry without exposing secrets
                LOGGER.warning("待恢复反思扫描失败：%s", redact_secrets(str(exc)))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _schedule_plans(
    scheduler: PlayerScheduler, stop_event: asyncio.Event, interval: float,
    activity: Optional[_ActivityClock] = None,
) -> None:
    """按最近事件调整计划间隔，不与事件触发的思考并发。"""
    while not stop_event.is_set():
        result = scheduler.submit(CognitiveEvent(
            kind="planning", key="current", priority=Priority.P2_PLAN,
        ))
        if result.status == SubmissionStatus.REJECTED_FULL:
            LOGGER.warning("计划事件队列已满，本轮计划未入队")
        try:
            delay = activity.planning_interval(interval) if activity is not None else max(0.1, interval)
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def _run_connection(
    config: PlayerConfig, agent: PersistentPlayerAgent, stop_event: asyncio.Event,
    fast_detector: Any = None, gate: Optional[PlayerActiveGate] = None,
) -> str:
    """建立一次 WS 连接；主动模式在断线后必须停止而非自动重授权。"""
    ws_url = build_ws_url(config.ws_url, "")
    headers = {"Authorization": "Bearer " + config.token} if config.token else {}
    timeout = aiohttp.ClientTimeout(total=None, connect=15.0, sock_connect=15.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(
            ws_url, headers=headers, heartbeat=30.0, max_msg_size=0
        ) as ws:
            LOGGER.info("shadow 已连接：%s", redact_url(ws_url))
            reflection_backoff = _ReflectionBackoff()
            async def handle(event: CognitiveEvent) -> Any:
                try:
                    if gate is not None and not gate.is_allowed:
                        stop_event.set()
                        return None
                    if event.kind == "story_preempt":
                        return None
                    if event.kind == "deep_reflection":
                        result = await agent.reflect_story(event.payload)
                        target = event.payload.get("target") if isinstance(event.payload, dict) else None
                        if isinstance(target, dict) and all(isinstance(target.get(key), str)
                                for key in ("session_id", "conversation_event_id")):
                            success = (isinstance(result, dict) and result.get("status") in
                                       {"recorded", "duplicate", "recorded_batches", "duplicate_batches", "completed"}
                                       and result.get("state_update") not in {"stale", "unavailable", "session_changed"})
                            reflection_backoff.mark(target, success=success)
                        return result
                    return await agent.tick(
                        force=event.priority <= Priority.P1_REACTIVE,
                        event_kind=event.kind,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 单次思考失败不终止接收
                    target = event.payload.get("target") if isinstance(event.payload, dict) else None
                    if event.kind == "deep_reflection" and isinstance(target, dict) and all(
                            isinstance(target.get(key), str) for key in ("session_id", "conversation_event_id")):
                        reflection_backoff.mark(target, success=False)
                    LOGGER.exception("认知事件处理失败：%s", event.kind)
                    raise

            scheduler = PlayerScheduler(handle, cooldowns={
                "live_error": 2.0, "region_changed": 3.0, "dialogue_line_changed": 1.0,
            })
            scheduler.start()
            def preempt_story(events: list[dict]) -> None:
                if not any(event.get("type") in {"dialogue_started", "dialogue_choice_presented"}
                           for event in events if isinstance(event, dict)):
                    return
                result = scheduler.submit(CognitiveEvent(
                    kind="story_preempt", key="current", priority=Priority.P0_INTERRUPT, critical=True,
                ))
                if result.status in {SubmissionStatus.REJECTED_FULL, SubmissionStatus.REJECTED_STOPPED}:
                    LOGGER.error("剧情安全抢占失败；停止当前连接")
                    stop_event.set()
            agent.story_event_callback = preempt_story
            recovery_worker = (asyncio.create_task(_recover_reflections_loop(
                agent, scheduler, stop_event, backoff=reflection_backoff))
                if agent.experience_store is not None and agent.llm is not None else None)
            activity = _ActivityClock()
            fast_queue = asyncio.Queue(maxsize=1) if fast_detector is not None else None
            if fast_detector is not None:
                fast_detector.reset()
            fast_worker = (asyncio.create_task(_fast_perception_loop(fast_queue, scheduler, fast_detector, agent, activity))
                           if fast_queue is not None else None)
            receiver = asyncio.ensure_future(_receive_loop(ws, agent, stop_event, scheduler, fast_queue))
            ticker = asyncio.ensure_future(_schedule_plans(scheduler, stop_event, config.interval, activity))
            agent.external_story_monitor = agent.observation_provider is not None
            story_worker = (asyncio.create_task(_story_observation_loop(
                agent, scheduler, stop_event, agent.observation_provider.interval, activity
            )) if agent.external_story_monitor else None)
            try:
                workers = ([receiver, ticker]
                           + ([recovery_worker] if recovery_worker is not None else [])
                           + ([story_worker] if story_worker is not None else [])
                           + ([fast_worker] if fast_worker is not None else []))
                done, _pending = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None:
                        raise error
                return "stopped"
            finally:
                if gate is not None:
                    # A disconnected active session cannot silently reconnect under
                    # a lease bound to an earlier observation.
                    stop_event.set()
                for task in (receiver, ticker, recovery_worker):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (receiver, ticker, recovery_worker) if task is not None),
                                     return_exceptions=True)
                if fast_queue is not None and fast_worker is not None:
                    if fast_queue.full():
                        fast_queue.get_nowait()
                    fast_queue.put_nowait(None)
                    outcome = await asyncio.gather(fast_worker, return_exceptions=True)
                    if isinstance(outcome[0], Exception):
                        LOGGER.warning("轻量感知工作者退出：%s", outcome[0])
                if story_worker is not None:
                    story_worker.cancel()
                    await asyncio.gather(story_worker, return_exceptions=True)
                agent.external_story_monitor = False
                agent.story_event_callback = None
                if agent.intent_publisher is not None:
                    agent._request_intent_cancel("connection_closed", schedule=False)
                # Stop in-flight cognition before flushing the final cancellation.
                await scheduler.stop()
                if agent.intent_publisher is not None:
                    try:
                        if not await agent._flush_intent_cancel():
                            LOGGER.error("断线撤销未获确认；不能证明输入已停止")
                    except Exception as exc:  # noqa: BLE001 - retain fail-closed cancellation state
                        LOGGER.warning("断线时撤销意图失败：%s", _safe_error(exc, config.token))


async def _watch_stop_file(stop_file: Path, stop_event: asyncio.Event) -> None:
    """轮询停止文件；出现后优雅结束，不依赖 live.stopped 广播。"""
    while not stop_event.is_set():
        try:
            if stop_file.exists():
                LOGGER.info("收到停止信号：%s", stop_file.name)
                stop_event.set()
                return
        except OSError:
            pass
        await asyncio.sleep(0.5)


async def _run_forever(
    config: PlayerConfig, agent: PersistentPlayerAgent, stop_file: Optional[Path] = None,
    fast_detector: Any = None, stop_event: Optional[asyncio.Event] = None,
    gate: Optional[PlayerActiveGate] = None,
) -> None:
    stop_event = stop_event or asyncio.Event()
    watcher = None
    if stop_file is not None:
        watcher = asyncio.ensure_future(_watch_stop_file(stop_file, stop_event))
    try:
        while not stop_event.is_set():
            try:
                reason = await _run_connection(config, agent, stop_event, fast_detector, gate)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                LOGGER.warning(
                    "shadow 连接中断：%s；%.1f 秒后重连",
                    _safe_error(exc, config.token),
                    config.reconnect_delay,
                )
            except Exception:  # noqa: BLE001 - 重连前记录完整栈
                LOGGER.exception(
                    "shadow 连接异常；%.1f 秒后重连", config.reconnect_delay
                )
            else:
                if reason == "live.stopped" or stop_event.is_set():
                    break
            if stop_event.is_set():
                break
            if gate is not None:
                # Never carry a health lease across a broken connection.
                LOGGER.error("主动连接结束；停止运行，不能自动重连并沿用原租约")
                stop_event.set()
                break
            # 断线重连后旧帧/旧指导不可信；清空并留一条诊断记录。
            agent.reset_observation()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=config.reconnect_delay)
            except asyncio.TimeoutError:
                pass
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


def _build_config(args: argparse.Namespace) -> PlayerConfig:
    return resolve_config(
        args.config or None,
        home=args.home or None,
        profile=args.profile or None,
        state=args.state or None,
        shadow_dir=args.shadow_dir or None,
        memory_dir=args.memory_dir or None,
        episodes_dir=args.episodes_dir or None,
        semantic_path=args.semantic_path or None,
        working_path=args.working_path or None,
        context_builder=args.context_builder,
        ws_url=args.ws_url or None,
        token=args.token or None,
        token_file=args.token_file or None,
        session_id=args.session_id or None,
        interval=args.interval,
        max_frame_age_ms=args.max_frame_age_ms,
        enabled=args.enabled,
        no_llm=args.no_llm,
    )


def _make_fast_detector(args: argparse.Namespace) -> Any:
    """按显式校准区域创建帧变化检测器；没有区域时不解码帧。"""
    rois = {}
    for name, bounds in (("dialogue_text", args.dialogue_roi), ("choice", args.choice_roi),
                         ("speaker", args.speaker_roi), ("quest", args.quest_roi),
                         ("hud", args.hud_roi)):
        if bounds is not None:
            left, top, right, bottom = bounds
            rois[name] = {"x": left, "y": top, "width": right - left, "height": bottom - top}
    if not rois:
        return None
    from player_fast_perception import FastPerception
    return FastPerception(rois=rois, hz=args.fast_hz)


def _confirm_controlled_mvp() -> bool:
    """Request an explicit local-terminal acknowledgement, not a game or input receipt."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        LOGGER.error("受控模式需要本机交互式终端；不能通过管道或服务自动确认")
        return False
    print("受控模式风险：系统只能确认已提交输入，不能证明游戏已处理松键；"
          "也不能阻止其他程序或用户同时输入。请检查游戏并松开全部按键，"
          "停止其他自动化，运行期间不要操作键鼠；确认后请将游戏切回前台。"
          "失焦、停止、断连或会话变化后须重新启动并再次确认。", file=sys.stderr, flush=True)
    try:
        return input("输入‘我已确认’以继续，其他输入均取消：").strip() == "我已确认"
    except (EOFError, KeyboardInterrupt):
        return False


async def async_main(args: argparse.Namespace) -> int:
    if aiohttp is None:
        LOGGER.error("需要 aiohttp：pip install aiohttp")
        return 2

    try:
        config = _build_config(args)
    except (OSError, ValueError) as exc:
        LOGGER.error("配置错误：%s", exc)
        return 2

    if not config.enabled:
        LOGGER.info("PLAYER_AGENT_ENABLED=0 / enabled=false，shadow 玩家未启动")
        return 0
    if not config.shadow_mode:
        LOGGER.error(
            "旧的 PLAYER_AGENT_SHADOW=0 不支持；请使用显式 --mode 选择运行模式"
        )
        return 2

    intent_url = (args.intent_url or os.environ.get("PLAYER_INTENT_URL") or "").strip()
    controlled_mvp = getattr(args, "controlled_mvp", False)
    if controlled_mvp and args.mode == "observer":
        LOGGER.error("observer 不需要也不能启用受控输入模式")
        return 2
    if args.mode == "observer" and intent_url:
        LOGGER.error("observer 禁止配置意图总线：它可能间接产生游戏输入")
        return 2
    try:
        fast_detector = _make_fast_detector(args)
    except (ValueError, ImportError) as exc:
        LOGGER.error("轻量感知配置错误：%s", exc)
        return 2

    ensure_directories(config)
    if args.mode == "observer":
        LOGGER.info("玩家运行模式：observer；不会发送意图或语音")
        try:
            with PlayerInstanceLock(config.home / "state" / "player.lock"):
                return await _run_configured(args, config, fast_detector)
        except InstanceLockError as exc:
            LOGGER.error("玩家进程启动被拒绝：%s", exc)
            return 2

    # A terminal acknowledgement is an explicit operating assumption, not proof.
    if controlled_mvp and not _confirm_controlled_mvp():
        LOGGER.error("主动模式被拒绝：未完成本机操作员确认")
        return 2
    if controlled_mvp:
        LOGGER.warning("受控 MVP：游戏响应及其他输入源仍未知；确认不会产生系统或硬件证明")

    # The gate owns the only player lock for the whole active lifetime.
    try:
        validate_local_pair(config.ws_url, args.observe_http)
    except ValueError as exc:
        LOGGER.error("主动模式被拒绝：live_http_binding_unverified (%s)", exc)
        return 2
    stop_event = asyncio.Event()
    runtime: dict[str, Any] = {}
    speech_output = None
    if args.mode == "full_active":
        session_dir = HERE.parent / "session"
        if str(session_dir) not in sys.path:
            sys.path.insert(0, str(session_dir))
        from player_speech_output import PlayerSpeechOutput
        from record_session import (announce_player_speech_producer, read_player_speech_endpoint,
                                    read_player_speech_heartbeat, send_player_speech)
        try:
            speech_output = PlayerSpeechOutput.from_environment(
                read_endpoint=read_player_speech_endpoint, send_speech=send_player_speech,
                announce_producer=announce_player_speech_producer,
                read_heartbeat=read_player_speech_heartbeat,
            )
            if speech_output is None:
                raise ValueError("speech_capability_missing")
        except Exception:
            LOGGER.error("主动模式被拒绝：speech_exclusive_readiness_unverified")
            return 2
        runtime["speech_output"] = speech_output

    def revoke(decision: GateDecision) -> None:
        """Stop work and request cancellation without claiming physical release."""
        LOGGER.error("主动安全租约撤销：%s", ", ".join(decision.reasons))
        stop_event.set()
        agent = runtime.get("agent")
        if agent is not None:
            agent._request_intent_cancel("gate_revoked", schedule=False)

    gate = PlayerActiveGate(GateConfig(
        mode=args.mode, bettergi_url=args.observe_http, bettergi_token=config.token,
        bridge_url=intent_url,
        bridge_token=args.intent_token or os.environ.get("BRIDGE_INTENT_TOKEN", ""),
        player_home=config.home,
        trusted_motor_config=Path(args.trusted_motor_config) if args.trusted_motor_config else None,
        controlled_mvp=controlled_mvp,
    ), revoke, speech_output=speech_output, controlled_mvp_confirmed=controlled_mvp)
    try:
        try:
            runtime["identity"] = gate.prepare_identity(
                config.profile_path, config.session_id, seed_default=config.profile_is_default)
            if speech_output is not None:
                await speech_output.announce_ready()
        except Exception:
            LOGGER.error("主动模式被拒绝：玩家身份绑定或语音就绪未确认")
            return 2
        return await _run_configured(args, config, fast_detector, gate, stop_event, runtime)
    finally:
        # _run_configured has stopped its workers and closed state before this
        # releases the gate-owned lock, including on disconnect or interruption.
        await gate.close()


def _recover_pending_conversation_ends(store: PlayerExperience, player_id: str, token: str,
                                       *, player_scope: str | None = None) -> None:
    """Bound startup recovery; blocked entries remain available for later review."""
    cursor = None
    # At most four batches of 32 entries and five source pages per batch.
    for _ in range(4):
        try:
            page = store.pending_conversation_ends(player_id, before_seq=cursor, limit=32, max_pages=5)
        except Exception as exc:  # noqa: BLE001 - retain unresolved records
            LOGGER.warning("扫描未确认对话结束失败，保留待恢复记录：%s", _safe_error(exc, token))
            return
        for item in page["items"]:
            if not store._belongs_to_scope(item["session_id"], player_scope):
                continue
            if item.get("status") != "pending":
                LOGGER.warning("未确认对话结束记录被阻塞，保留待人工检查：%s", item.get("pending_event_id", "unknown"))
                continue
            try:
                store.complete_pending_conversation_end(
                    session_id=item["session_id"], pending_event_id=item["pending_event_id"],
                )
            except Exception as exc:  # noqa: BLE001 - no claim that a failed item was completed
                LOGGER.warning("恢复未确认对话片段失败，记录保留：%s", _safe_error(exc, token))
        next_cursor = page.get("next_before_seq")
        if next_cursor is None:
            return
        if next_cursor == cursor:
            LOGGER.warning("对话恢复游标未前进，保留余下记录")
            return
        cursor = next_cursor
    LOGGER.warning("对话恢复达到本次扫描上限，余下记录保留待后续恢复")


def _recover_pending_reflection_updates(log: PlayerEventLog, transaction: PlayerCognitiveTransaction,
                                        player_id: str, scope: str, token: str) -> None:
    """按原始顺序恢复有版本的反思；过时和损坏条目保留而不覆盖新状态。"""
    from player_reflection import _reflection_id
    cursor = 0
    processed = set()
    counts = {"applied": 0, "stale": 0, "blocked": 0}
    while True:
        rows = log.query_events(session_prefix=player_id + ":", event_types=["experience.reflection"],
                                visibility="player", after_seq=cursor, limit=100)
        if not rows:
            break
        for row in rows:
            cursor = row["seq"]
            try:
                payload = row.get("payload")
                document = json.loads(payload.get("text", "")) if isinstance(payload, dict) else None
                if (not isinstance(document, dict) or type(document.get("cognitive_revision")) is not int
                        or not isinstance(document.get("items"), list)):
                    continue
                total = document.get("batch_total", 1)
                conversation_id = document.get("conversation_event_id")
                if (type(total) is not int or not 1 <= total <= 63
                        or not isinstance(conversation_id, str) or not conversation_id):
                    continue
                versioned = document.get("version") == 2
                if versioned and document.get("player_scope") != scope:
                    continue
                first_id = _reflection_id(row["session_id"], conversation_id, 0, total,
                                          player_scope=scope if versioned else None,
                                          cognitive_revision=document["cognitive_revision"] if versioned else None)
                key = (row["session_id"], first_id)
                if key in processed:
                    continue
                processed.add(key)
                result = transaction.apply_reflection(session_id=row["session_id"], player_scope=scope,
                                                      reflection_id=first_id)
                if result["status"] == "applied":
                    counts["applied"] += 1
            except EventLogConflict:
                counts["stale"] += 1
            except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as exc:
                counts["blocked"] += 1
                LOGGER.warning("反思状态恢复被阻止，原记录保留：%s", _safe_error(exc, token))
        if len(rows) < 100:
            break
    if any(counts.values()):
        LOGGER.info("反思状态恢复：已应用 %d，已过时 %d，待检查 %d",
                    counts["applied"], counts["stale"], counts["blocked"])


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    """拒绝任意嵌套对象中的重复 JSON 字段。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    """Python 默认接受 NaN/Infinity，但这些不是合法 JSON。"""
    raise ValueError("非标准 JSON 数字：" + value)


def _strict_legacy_json(path: Path) -> Any:
    """读取未经恢复或规范化的 JSON，拒绝重复键及非标准数字。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"),
                      object_pairs_hook=_unique_json_pairs, parse_constant=_invalid_json_constant)
    # 例如 1e999 会被 Python 解成无穷大，孤立代理项也不能归档为 UTF-8。
    json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return data


def _same_legacy_default(value: Any, default: Any) -> bool:
    """只有结构和类型均匹配原始默认值才视为没有旧状态。"""
    if isinstance(default, dict):
        return (isinstance(value, dict) and value.keys() == default.keys()
                and all(_same_legacy_default(value[key], item) for key, item in default.items()))
    if isinstance(default, list):
        return isinstance(value, list) and len(value) == len(default) and all(
            _same_legacy_default(item, expected) for item, expected in zip(value, default)
        )
    if type(default) is float:
        return type(value) in (int, float) and value == default
    return type(value) is type(default) and value == default


def _nonempty_legacy(paths: dict[str, Path], player_scope: str) -> dict[str, Path]:
    """只读校验旧文件，不调用会修复或改写源文件的 store.load。"""
    from player_emotion import _empty as empty_emotion
    from player_legacy_migration import _build, _inspect, _text_fields, _version

    defaults = {
        "working": default_working(),
        "beliefs": BeliefStore(paths["beliefs"])._empty(),
        "relationships": RelationshipStore(paths["relationships"])._empty(),
        "emotion": empty_emotion(),
        "threads": ThreadStore(paths["threads"])._empty(),
    }
    nonempty = {}
    for name, path in paths.items():
        if (path.exists() or path.is_symlink()) and not path.is_file():
            raise ValueError(f"旧状态路径不是普通文件：{path}")
        if not path.is_file():
            continue
        data = _strict_legacy_json(path)
        if not isinstance(data, dict):
            raise ValueError(f"旧状态不是 JSON 对象：{path}")
        _version(data, name, working_store=name == "working")
        _inspect(data, player_scope)
        _text_fields(data, {"updated_at": 40})
        # 与显式迁移共用字段校验，禁止先修复损坏内容再误判为空。
        _build({name: {"data": data}}, "legacy-validation", "legacy-validation")
        if not _same_legacy_default(data, defaults[name]):
            nonempty[name] = path
    return nonempty


def _reject_legacy_session_projections(event_log: PlayerEventLog) -> None:
    """旧 session 投影不能凭 session 名或当前 profile 推定归属。"""
    try:
        with event_log._lock:
            row = event_log._db.execute(
                "SELECT session_id FROM projections WHERE name = ? LIMIT 1", ("cognitive.v1",)
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("无法检查历史 session 认知投影") from exc
    if row is not None:
        raise ValueError("发现旧 session cognitive.v1 投影，无法安全判定归属；请先人工备份并迁移，未修改原投影")


def _legacy_sources(config: PlayerConfig, paths: dict[str, Path], player_id: str,
                    scope: str, event_log: PlayerEventLog) -> dict:
    """仅声明当前目录内、未发现归属冲突的来源；声明不证明所有权。"""
    home = config.home.resolve()
    protected = [config.profile_path, config.state_path, config.home / "state" / "cognitive_events.sqlite"]
    selected: list[Path] = []
    sources = {}
    for name, path in paths.items():
        resolved = path.resolve()
        try:
            resolved.relative_to(home)
        except ValueError as exc:
            raise ValueError(f"旧状态来源不在当前玩家目录内：{path}") from exc
        for other in [*selected, *protected]:
            if resolved == other.resolve() or (path.exists() and other.exists() and os.path.samefile(path, other)):
                raise ValueError(f"旧状态来源路径冲突：{path} / {other}")
        selected.append(path)
        sources[name] = {"player_scope": scope, "path": str(resolved),
                         "data": _strict_legacy_json(path)}

    # resume 是独立状态，不参与迁移，但它的明确归属不能被忽略。
    if config.state_path.exists():
        resume = _strict_legacy_json(config.state_path)
        if not isinstance(resume, dict):
            raise ValueError("resume 状态不是 JSON 对象，不能判定玩家归属")
        owner = resume.get("player_id")
        if owner is not None and (not isinstance(owner, str) or not owner.strip() or owner.strip() != player_id):
            raise ValueError("resume 状态玩家归属与当前玩家冲突或无效")
        if owner is None:
            LOGGER.warning("resume 状态无玩家归属；其来源未核实")

    # 数据库中的其他 scope、未知 session 或冲突绑定都不能被当前 profile 认领。
    # 使用完整查询而不是有上限的近期事件查询；运行在玩家单写者锁下。
    db = event_log._db
    try:
        with event_log._lock:
            foreign_scope = db.execute("SELECT 1 FROM scoped_projections WHERE scope != ? LIMIT 1", (scope,)).fetchone()
            prefix = player_id + ":"
            foreign_session = db.execute(
                "SELECT 1 FROM events WHERE substr(session_id, 1, ?) != ? LIMIT 1",
                (len(prefix), prefix),
            ).fetchone()
            bindings = db.execute("SELECT state FROM projections WHERE name = ?", ("cognitive.session_owner.v1",)).fetchall()
        for row in bindings:
            binding = json.loads(row[0], object_pairs_hook=_unique_json_pairs,
                                 parse_constant=_invalid_json_constant)
            if not isinstance(binding, dict) or binding.get("player_scope") != scope:
                raise ValueError("事件数据库包含不同或无效的玩家绑定")
        if foreign_scope or foreign_session:
            raise ValueError("事件数据库包含其他玩家或归属不明的会话，拒绝认领旧状态")
    except sqlite3.Error as exc:
        raise ValueError("无法检查事件数据库的玩家归属") from exc
    LOGGER.warning("旧 JSON 来源归属未核实；仅按显式请求导入为主观未核实状态")
    return sources


async def _run_configured(
    args: argparse.Namespace, config: PlayerConfig, fast_detector: Any,
    gate: Optional[PlayerActiveGate] = None, stop_event: Optional[asyncio.Event] = None,
    runtime: Optional[dict[str, Any]] = None,
) -> int:
    """Run under the observer lock or the active gate's single-writer lock."""
    identity = runtime.get("identity") if runtime is not None else None
    try:
        if gate is not None:
            if not isinstance(identity, dict) or identity.get("profile") is None:
                raise ProfileError("主动模式缺少已绑定的玩家身份")
            profile = identity["profile"]
        else:
            if config.profile_is_default and not config.profile_path.exists():
                seed_default_profile(config.profile_path)
                LOGGER.info("已从模板创建默认玩家档案：%s", config.profile_path)
            profile = load_profile(config.profile_path)
    except ProfileError as exc:
        LOGGER.error("玩家档案错误：%s", exc)
        return 2

    state_store = StateStore(config.state_path, profile.player_id, logger=LOGGER)
    memory_bridge = None
    experience_log = None
    recorder = None
    llm = None
    try:
        # 构造 agent 时就会加载 resume；版本/归属错误在这里暴露。
        recorder = ShadowRecorder(
            config.shadow_dir,
            config.session_id,
            profile.player_id,
            run_id=identity["run_id"] if identity is not None else new_run_id(),
            logger=LOGGER,
        )
        # 在任何可能恢复/改写旧 store 的构造或启动恢复之前检查真实来源。
        legacy_paths = {"working": config.working_path, "beliefs": config.belief_path,
                        "relationships": config.relationship_path, "emotion": config.emotion_path,
                        "threads": config.thread_path}
        experience_log = (identity["event_log"] if identity is not None else
                          PlayerEventLog(config.home / "state" / "cognitive_events.sqlite"))
        experience_store = PlayerExperience(experience_log)
        cognitive_transaction = (identity["transaction"] if identity is not None else
                                 PlayerCognitiveTransaction(experience_log))
        scope = "player-" + hashlib.sha256(profile.player_id.encode("utf-8")).hexdigest()
        _reject_legacy_session_projections(experience_log)
        existing_legacy = _nonempty_legacy(legacy_paths, scope)
        bootstrap_session = (identity["bootstrap_session"] if identity is not None else
                             f"{profile.player_id}:{config.session_id}:{recorder.run_id}:bootstrap")
        projection = experience_log.get_projection(bootstrap_session, "cognitive.v1", projection_scope=scope)
        if projection is None and existing_legacy and not args.import_legacy_state:
            raise ValueError("发现非空旧 JSON 状态但 SQLite 投影为空；请显式使用 --import-legacy-state")
        sources = (_legacy_sources(config, existing_legacy, profile.player_id, scope, experience_log)
                   if args.import_legacy_state and existing_legacy else {})
        cognitive_transaction.bind_session(session_id=bootstrap_session, player_scope=scope,
                                             event_id="binding-" + hashlib.sha256(bootstrap_session.encode("utf-8")).hexdigest())
        if sources:
            from player_legacy_migration import import_legacy_player
            import_legacy_player(transaction=cognitive_transaction, session_id=bootstrap_session,
                                 player_scope=scope, sources=sources)
            LOGGER.info("旧玩家状态已归档并导入；原 JSON 文件未修改")
        elif args.import_legacy_state:
            LOGGER.info("未发现可导入的旧 JSON 状态")
        _recover_pending_conversation_ends(experience_store, profile.player_id, config.token, player_scope=scope)
        await asyncio.to_thread(_recover_pending_reflection_updates, experience_log, cognitive_transaction,
                                profile.player_id, scope, config.token)
        cognitive_snapshot = cognitive_transaction.read(bootstrap_session, player_scope=scope)
        cognitive_state = cognitive_snapshot["state"]

        # SQLite 已接管认知状态；不再实例化可能读取或修复旧 JSON 的 store。
        # episodes、semantic、recap 及 evidence 没有可验证的玩家所有权，
        # 不能按配置目录或 profile 推定归属，源文件保留但不参与召回。
        LOGGER.warning("旧 episodes、semantic、recap 未验证玩家归属，已禁用其召回并保留源数据")
        if (os.environ.get("PLAYER_OPENVIKING") or "").strip().lower() in (
            "1", "true", "yes", "on"
        ):
            LOGGER.warning("OpenViking 玩家知识来源尚未隔离；已禁用外部召回")
        experience_embedding = None
        if args.embedding_model:
            if config.context_builder != "v2":
                raise ValueError("本地经历向量召回需要 v2 上下文模式")
            from player_embedding import EmbeddingConfig, LocalSentenceTransformerAdapter
            adapter = await asyncio.to_thread(LocalSentenceTransformerAdapter, args.embedding_model)
            experience_embedding = EmbeddingConfig(adapter=adapter, provenance="local-sentence-transformer",
                                                    max_candidates=32)
            LOGGER.info("已启用本地玩家经历向量召回；不启用旧记忆或外部知识源")
        context_builder = ContextBuilder(profile, mode=config.context_builder)
        observation_provider = None
        if args.observe_ocr:
            if config.context_builder != "v2":
                LOGGER.warning("屏幕观察仅在 v2 上下文模式生效")
            else:
                validate_local_pair(config.ws_url, args.observe_http)
                named = {}
                for region in args.named_ocr or []:
                    name, *coordinates = region
                    if name in named:
                        raise ValueError("具名 OCR 区域不能重复")
                    named[name] = dict(zip(("x", "y", "width", "height"), map(int, coordinates)))
                # 每次运行使用独占目录，不读取共享目录中归属不明的旧证据。
                evidence_dir = config.home / "state" / "screen_evidence" / scope / recorder.run_id
                evidence_dir.mkdir(parents=True, exist_ok=False)
                observation_provider = ScreenObserver(
                    args.observe_http, config.token, "player-shadow-ocr",
                    EvidenceStore(evidence_dir), interval=args.observe_interval,
                    ocr_roi=(dict(zip(("x", "y", "width", "height"), args.ocr_roi))
                             if args.ocr_roi else None),
                    named_regions=named or None,
                )
                LOGGER.info("已启用屏幕观察；BetterGI 会记录观察事件")
                if args.ocr_roi is None and not named:
                    LOGGER.warning("未指定 OCR 像素区域：只分析 UI，不请求全屏 OCR")
        intent_publisher = None
        intent_url = (args.intent_url or os.environ.get("PLAYER_INTENT_URL") or "").strip()
        if intent_url:
            if gate is None or args.mode == "observer":
                raise ValueError("intent_dispatch_gate_not_available")
            intent_publisher = _GuardedIntentPublisher(
                IntentPublisher(intent_url, args.intent_token or os.environ.get("BRIDGE_INTENT_TOKEN", "")),
                gate,
            )
            LOGGER.warning(
                "已启用意图控制模式：只向 Bridge 发送目标，不直接发送游戏输入"
            )
        llm = None
        if not config.no_llm:
            if llm_client is None:
                raise ValueError("缺少 llm_client 模块；可用 --no-llm 先验证链路")
            try:
                llm = llm_client.LLMClient.from_env()
            except llm_client.LLMError as exc:
                raise ValueError(f"LLM 初始化失败：{exc}") from exc
        speech_output = runtime.get("speech_output") if runtime is not None else None
        if args.mode == "full_active":
            if gate is None or speech_output is None:
                raise ValueError("speech_gate_not_available")
        agent = PersistentPlayerAgent(
            profile,
            state_store,
            recorder,
            mode=args.mode,
            active_gate=gate,
            speech_output=speech_output,
            llm=llm,
            session_id=config.session_id,
            logger=LOGGER,
            max_frame_age_ms=config.max_frame_age_ms,
            state_save_interval_ms=config.state_save_interval_ms,
            context_builder=context_builder,
            context_mode=config.context_builder,
            observation_provider=observation_provider,
            experience_store=experience_store,
            experience_embedding=experience_embedding,
            cognitive_transaction=cognitive_transaction,
            dialogue_tracker=DialogueTracker(
                dialogue_roi=args.dialogue_roi, choice_roi=args.choice_roi,
                speaker_roi=args.speaker_roi,
            ),
            intent_publisher=intent_publisher,
        )
        if gate is not None:
            gate.attach_identity_agent(agent)
        agent._cognitive_session = bootstrap_session
        agent._cognitive_state = cognitive_state
        agent._working = cognitive_state["working"]
    except (
        StateError,
        WorkingMemoryError,
        EpisodeError,
        SemanticMemoryError,
        RecapError,
        BeliefError,
        RelationshipError,
        EmotionError,
        ThreadError,
        EventLogError,
        CognitiveTransactionError,
        ValueError,
        OverflowError,
        OSError,
        RuntimeError,
    ) as exc:
        LOGGER.error("玩家状态/记忆错误：%s", exc)
        if experience_log is not None and gate is None:
            experience_log.close()
        if memory_bridge is not None:
            try:
                await memory_bridge.close()
            except Exception:  # noqa: BLE001
                pass
        if llm is not None:
            try:
                await llm.close()
            except Exception:  # noqa: BLE001
                pass
        if recorder is not None:
            try:
                recorder.close(error="memory_state_error")
            except OSError:
                pass
        return 2

    LOGGER.info("shadow 配置：%s", config.describe())
    LOGGER.info("shadow 记忆：SQLite 玩家投影；旧文件召回已禁用；context=%s", config.context_builder)
    if runtime is not None:
        runtime["agent"] = agent
    stop_file = Path(args.stop_file) if args.stop_file else None
    try:
        if gate is not None:
            decision = await gate.start()
            if not decision.allowed or not gate.is_allowed:
                LOGGER.error("主动模式 %s 被拒绝：%s", args.mode, ", ".join(gate.decision.reasons))
                return 2
        agent.start()
        if args.duration and args.duration > 0:
            try:
                await asyncio.wait_for(
                    _run_forever(config, agent, stop_file, fast_detector, stop_event, gate), timeout=args.duration
                )
            except asyncio.TimeoutError:
                LOGGER.info("达到运行时长 %.0f 秒，结束 shadow", args.duration)
        else:
            await _run_forever(config, agent, stop_file, fast_detector, stop_event, gate)
    finally:
        agent.close()
        if experience_log is not None and gate is None:
            experience_log.close()
        if memory_bridge is not None:
            try:
                await memory_bridge.close()
            except Exception as exc:  # noqa: BLE001 - 收尾失败不影响退出
                LOGGER.warning("关闭 OpenViking 失败：%s", exc)
        if llm is not None:
            try:
                await llm.close()
            except Exception as exc:  # noqa: BLE001 - 收尾失败不影响退出
                LOGGER.warning("关闭 LLM 客户端失败：%s", exc)
        if runtime is not None:
            runtime.pop("agent", None)
        LOGGER.info("shadow 已结束：%s", agent.stats)
    if gate is not None and not gate.is_allowed:
        LOGGER.error("主动运行结束于安全租约撤销：%s", ", ".join(gate.decision.reasons))
        return 2
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.journey:
        if not args.home:
            print("只读旅程回顾需要显式指定 --home。", file=sys.stderr)
            return 2
        try:
            from player_recap import read_journey
            result = read_journey(args.home, profile_path=args.profile or None, limit=args.journey_limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print("读取旅程回顾失败：" + str(exc), file=sys.stderr)
            return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOGGER.info("收到中断，shadow 已收尾")
        return 130
    except (OSError, ValueError) as exc:
        LOGGER.error("shadow 启动失败：%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
