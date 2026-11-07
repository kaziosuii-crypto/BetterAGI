#!/usr/bin/env python3
"""Player speech delivery through a session-bound recorder capability.

Binding is obtained before the model generates text. A timeout is ambiguous and
must never be retried automatically; only a verified recorder receipt counts
as timeline delivery. The caller remains responsible for the active-mode gate.
"""
from __future__ import annotations

import asyncio
import copy
import math
import os
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional


class PlayerSpeechOutput:
    """Adapt synchronous recorder IPC functions to the cognition event loop."""

    def __init__(self, endpoint: str, secret: str, *, read_endpoint: Callable,
                 send_speech: Callable, announce_producer: Optional[Callable] = None,
                 read_heartbeat: Optional[Callable] = None, timeout: float = 5.0) -> None:
        if not endpoint or not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("speech endpoint and capability secret are required")
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("speech timeout must be a positive finite number")
        self._endpoint = endpoint
        self._secret = secret
        self._read_endpoint = read_endpoint
        self._send_speech = send_speech
        self._announce_producer = announce_producer
        self._read_heartbeat = read_heartbeat
        self._timeout = timeout
        # A cancelled coroutine may leave its worker running; never submit its thought again.
        self._attempted: set[tuple[str, str, str]] = set()
        self._prepared: set[tuple[str, str]] = set()
        self._ready_descriptor: Optional[Dict[str, Any]] = None
        self._heartbeat_sequence = 0
        self._ready_observed = False
        self._attempt_lock = threading.Lock()

    @classmethod
    def from_environment(cls, *, read_endpoint: Callable, send_speech: Callable,
                         announce_producer: Optional[Callable] = None,
                         read_heartbeat: Optional[Callable] = None,
                         environ: Optional[Mapping[str, str]] = None,
                         timeout: float = 5.0) -> Optional["PlayerSpeechOutput"]:
        """Construct an opt-in capability; caller must enforce its active-mode gate.

        The runner may omit both variables. A partial configuration is an error,
        never a reason to silently fall back to an unbound speech producer.
        """
        env = os.environ if environ is None else environ
        endpoint = env.get("BETTERGI_PLAYER_SPEECH_ENDPOINT", "")
        secret = env.get("BETTERGI_PLAYER_SPEECH_SECRET", "")
        if not endpoint and not secret:
            return None
        return cls(endpoint, secret, read_endpoint=read_endpoint, send_speech=send_speech,
                   announce_producer=announce_producer, read_heartbeat=read_heartbeat, timeout=timeout)

    async def announce_ready(self) -> None:
        """先获得会话绑定的认证回执，再等待同一录制器的心跳。"""
        if self._announce_producer is None or self._read_heartbeat is None:
            raise RuntimeError("speech 生产者就绪交接不可用")
        before = await asyncio.to_thread(self._read_endpoint, self._endpoint, self._secret)
        if (not isinstance(before, dict) or not all(
                isinstance(before.get(key), str) and before[key]
                for key in ("session_id", "instance", "challenge", "live_session_id", "directory", "formal_chain_id"))
                or before.get("speech_mode") != "player-exclusive"
                or type(before.get("orchestrator_pid")) is not int
                or before["orchestrator_pid"] != os.getppid()
                or len(before["formal_chain_id"]) != 32
                or any(char not in "0123456789abcdef" for char in before["formal_chain_id"])):
            raise ValueError("speech 录制会话身份不完整")
        receipt = await asyncio.to_thread(self._announce_producer, self._endpoint, self._secret,
                                          timeout=self._timeout)
        after = await asyncio.to_thread(self._read_endpoint, self._endpoint, self._secret)
        if (after != before or not isinstance(receipt, dict) or receipt.get("status") != "ready"
                or type(receipt.get("pid")) is not int or receipt["pid"] != os.getpid()
                or any(receipt.get(key) != before.get(key) for key in ("session_id", "instance", "challenge"))):
            raise RuntimeError("speech 就绪期间录制会话发生变化")
        with self._attempt_lock:
            self._ready_descriptor = copy.deepcopy(before)
            self._heartbeat_sequence = 0
            self._ready_observed = False
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if await self.ready_deadline(before["live_session_id"]) > time.monotonic():
                return
            await asyncio.sleep(0.05)
        with self._attempt_lock:
            self._ready_descriptor = None
        raise TimeoutError("speech 录制器心跳未确认")

    async def ready_deadline(self, live_session_id: str) -> float:
        """返回认证心跳的本地截止时刻；失效后本次能力不可恢复。"""
        with self._attempt_lock:
            descriptor = copy.deepcopy(self._ready_descriptor)
            established = self._ready_observed
        if descriptor is None or self._read_heartbeat is None or descriptor.get("live_session_id") != live_session_id:
            return 0.0
        try:
            heartbeat = await asyncio.to_thread(self._read_heartbeat, self._endpoint, self._secret)
            current = await asyncio.to_thread(self._read_endpoint, self._endpoint, self._secret)
            now = time.monotonic_ns()
            timestamp = heartbeat.get("monotonic_ns") if isinstance(heartbeat, dict) else None
            sequence = heartbeat.get("sequence") if isinstance(heartbeat, dict) else None
            valid = (current == descriptor and isinstance(heartbeat, dict)
                     and all(heartbeat.get(key) == descriptor.get(key)
                             for key in ("session_id", "instance", "challenge", "live_session_id",
                                         "formal_chain_id", "speech_mode", "orchestrator_pid"))
                     and type(heartbeat.get("orchestrator_pid")) is int
                     and heartbeat["orchestrator_pid"] == os.getppid()
                     and heartbeat.get("orchestrator") == "run_episode"
                     and heartbeat.get("player_mode") == "full_active"
                     and heartbeat.get("speech_mode") == "player-exclusive"
                     and heartbeat.get("producer") == "persistent_player"
                     and heartbeat.get("orchestrator_director_configured") is False
                     and heartbeat.get("legacy_formal_speech_excluded") is True
                     and heartbeat.get("pid") == os.getpid() and type(heartbeat.get("pid")) is int
                     and type(timestamp) is int and 0 <= now - timestamp < 1_500_000_000
                     and type(sequence) is int and sequence > 0)
            with self._attempt_lock:
                if self._ready_descriptor != descriptor or not valid or sequence < self._heartbeat_sequence:
                    if established:
                        self._ready_descriptor = None
                    return 0.0
                self._heartbeat_sequence = sequence
                self._ready_observed = True
            return (timestamp + 1_500_000_000) / 1_000_000_000
        except (OSError, ValueError, TypeError, KeyError):
            if established:
                with self._attempt_lock:
                    self._ready_descriptor = None
            return 0.0

    async def prepare(self) -> Dict[str, str]:
        """Capture recorder identity before the model is called."""
        descriptor = await asyncio.to_thread(self._read_endpoint, self._endpoint, self._secret)
        session, instance = descriptor.get("session_id"), descriptor.get("instance")
        if not isinstance(session, str) or not session or not isinstance(instance, str) or not instance:
            raise ValueError("speech recorder descriptor is incomplete")
        if self._announce_producer is not None:
            with self._attempt_lock:
                ready = copy.deepcopy(self._ready_descriptor)
            if (ready != descriptor or await self.ready_deadline(descriptor.get("live_session_id")) <= time.monotonic()):
                raise RuntimeError("speech 录制会话未就绪或已撤销")
        with self._attempt_lock:
            self._prepared.add((session, instance))
        return {"session_id": session, "instance": instance}

    async def deliver(self, binding: Dict[str, str], event: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a thought at most once, including on timeout or cancellation.

        Only the recorder's verified timeline receipt indicates delivery. A
        timeout or cancellation remains unknown: the worker may still write.
        """
        if not isinstance(binding, dict) or not all(
            isinstance(binding.get(key), str) and binding[key]
            for key in ("session_id", "instance")
        ):
            raise ValueError("a pre-generation speech binding is required")
        if self._announce_producer is not None:
            with self._attempt_lock:
                ready = copy.deepcopy(self._ready_descriptor)
            if (ready is None or ready.get("session_id") != binding["session_id"]
                    or ready.get("instance") != binding["instance"]
                    or await self.ready_deadline(ready.get("live_session_id")) <= time.monotonic()):
                raise RuntimeError("speech 录制会话未就绪或已撤销")
        thought_id = event.get("thought_id") if isinstance(event, dict) else None
        if not isinstance(thought_id, str) or not thought_id.strip():
            raise ValueError("speech requires a persisted thought_id")
        # Freeze the submission before handing it to a worker thread.
        event = copy.deepcopy(event)
        key = (binding["session_id"], binding["instance"], thought_id)
        with self._attempt_lock:
            if key[:2] not in self._prepared:
                raise ValueError("speech binding was not prepared by this output")
            if key in self._attempted:
                raise ValueError("speech thought already submitted; do not retry")
            self._attempted.add(key)
        receipt = await asyncio.to_thread(
            self._send_speech, self._endpoint, self._secret, event,
            expected_session_id=binding["session_id"], expected_instance=binding["instance"],
            timeout=self._timeout,
        )
        if (not isinstance(receipt, dict) or receipt.get("type") != "speech"
                or receipt.get("source") != "persistent_player"
                or type(receipt.get("t")) is not int or receipt["t"] < 0
                or type(receipt.get("dur")) is not int or receipt["dur"] <= 0
                or any(receipt.get(key) != value for key, value in event.items())):
            raise ValueError("speech receipt identity mismatch; delivery is unknown")
        return receipt
