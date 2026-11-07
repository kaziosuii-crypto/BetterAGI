#!/usr/bin/env python3
"""Read-only, fail-closed player admission and continuous health monitoring.

The runner uses this gate for explicit active admission; it never sends an
intent or an input itself. The live status is a snapshot, not authorization or
proof of physical input delivery. Active admission remains denied until the
missing end-to-end enforcement and receipts can be independently established.

Usage: construct PlayerActiveGate, await start(), and inspect its decision and
is_allowed before use. Await close() on shutdown. A successful start is watched
until a check fails, the lease expires, or close() is called. The revoke callback
must independently stop the caller's work; it is not an input-release receipt.
The caller must also check is_allowed immediately before each protected use.
An event loop that is blocked cannot run callbacks on time.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import ipaddress
import json
import os
import math
import re
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import aiohttp

try:
    from .player_instance import InstanceLockError, PlayerInstanceLock
    from .player_speech_output import PlayerSpeechOutput
    from .player_event_log import PlayerEventLog
    from .player_cognitive_transaction import PlayerCognitiveTransaction, SESSION_BINDING
    from .player_profile import load_profile, seed_default_profile
    from .player_vision import ScreenObserver
    from .player_experience import PlayerExperience
except ImportError:
    from player_instance import InstanceLockError, PlayerInstanceLock
    from player_speech_output import PlayerSpeechOutput
    from player_event_log import PlayerEventLog
    from player_cognitive_transaction import PlayerCognitiveTransaction, SESSION_BINDING
    from player_profile import load_profile, seed_default_profile
    from player_vision import ScreenObserver
    from player_experience import PlayerExperience


@dataclass(frozen=True)
class GateConfig:
    """Local settings, never evidence of remote health or authority."""

    mode: str
    bettergi_url: str
    bettergi_token: str
    bridge_url: str = ""
    bridge_token: str = ""
    player_home: Optional[Path] = None
    max_frame_age_seconds: float = 2.0
    poll_interval_seconds: float = 0.5
    request_timeout_seconds: float = 1.0
    trusted_motor_config: Optional[Path] = None
    controlled_mvp: bool = False


@dataclass(frozen=True)
class GateDecision:
    """A bounded snapshot of admission; reasons are stable, non-secret codes."""

    mode: str
    allowed: bool
    reasons: tuple[str, ...]
    session_id: Optional[str] = None


RevokeCallback = Callable[[GateDecision], Optional[Awaitable[None]]]


def _origin(value: str) -> Optional[str]:
    """Accept only a literal loopback HTTP origin, with no proxy or URL tricks."""
    if not isinstance(value, str) or value != value.strip():
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "http" or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ("", "/")
                or url.query or url.fragment or url.port is None
                or not ipaddress.ip_address(url.hostname).is_loopback):
            return None
        return value.rstrip("/")
    except (ValueError, TypeError):
        return None


def _motor_origin(value: object) -> Optional[str]:
    """Canonicalize an explicitly trusted secure Motor origin without credentials."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in ("ws", "wss") or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ("", "/") or url.query or url.fragment
                or "%" in url.hostname):
            return None
        host = url.hostname.encode("idna").decode("ascii").lower()
        port = url.port or (443 if url.scheme == "wss" else 80)
        if not 1 <= port <= 65535:
            return None
        if url.scheme == "ws" and not ipaddress.ip_address(host).is_loopback:
            return None
        if ":" in host:
            host = "[" + host + "]"
        return f"{url.scheme}://{host}:{port}"
    except (ValueError, TypeError, UnicodeError):
        return None


def _token_valid(value: object) -> bool:
    """Require an unambiguous non-empty ASCII bearer credential."""
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and all(33 <= ord(c) <= 126 for c in value))


def _positive_number(value: object, maximum: float) -> bool:
    """Reject booleans, non-finite values, and unreasonable timeouts."""
    return (type(value) in (float, int) and math.isfinite(value)
            and 0 < value <= maximum)


def _frame_deadline(value: object, max_age: float) -> float:
    """Map remaining capture freshness to monotonic time; zero means invalid."""
    if not isinstance(value, str):
        return 0.0
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return 0.0
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        return time.monotonic() + max_age - age if 0 <= age < max_age else 0.0
    except (ValueError, OverflowError):
        return 0.0


def _persistent_store_reason(home: Optional[Path]) -> Optional[str]:
    """Inspect the existing player event store without creating or modifying it."""
    if home is None:
        return "persistent_store_unavailable"
    try:
        path = Path(home).resolve(strict=True) / "state" / "cognitive_events.sqlite"
        if not path.is_file():
            return "persistent_store_unavailable"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            integrity = db.execute("PRAGMA quick_check(1)").fetchone()[0]
            journal = db.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if (version != 2 or integrity != "ok" or str(journal).lower() != "wal"
                    or not {"events", "projections", "scoped_projections"} <= tables):
                return "persistent_store_invalid"
    except (OSError, ValueError, sqlite3.Error):
        return "persistent_store_unavailable"
    return None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _read_bounded(content, limit: int) -> bytes:
    try:
        await content.readexactly(limit + 1)
    except asyncio.IncompleteReadError as exc:
        return exc.partial
    raise ValueError("response exceeds size limit")


async def _abandon_reported_binding(session, bridge_origin: str, bridge_token: str,
                                    session_id: str) -> bool:
    """按 Bridge 报告的 requestId 精确放弃遗留绑定。

    只接受与当前 Live 会话一致的绑定；无法确认身份时返回 False，绝不猜测。
    """
    origin = bridge_origin.rstrip("/")
    headers = {"Authorization": "Bearer " + bridge_token}
    try:
        async with session.get(origin + "/feedback", headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                return False
            data = json.loads(await _read_bounded(response.content, 65536),
                              object_pairs_hook=_unique_json_object)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
        return False
    binding = data.get("liveLeaseBinding") if isinstance(data, dict) else None
    if not isinstance(binding, dict) or binding.get("sessionId") != session_id:
        return False
    request_id = binding.get("requestId")
    if not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
        return False
    try:
        async with session.post(origin + "/lease/abandon", json={"requestId": request_id},
                                headers=headers, allow_redirects=False) as response:
            if response.status != 200:
                return False
            result = json.loads(await _read_bounded(response.content, 8192),
                                object_pairs_hook=_unique_json_object)
            return isinstance(result, dict) and result.get("abandoned") is True
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
        return False


def _lease_credentials(value: object) -> Optional[dict]:
    if (not isinstance(value, dict) or set(value) != {"secret", "instanceId", "epoch"}
            or not isinstance(value.get("secret"), str) or re.fullmatch(r"[0-9A-Fa-f]{64}", value["secret"]) is None
            or not isinstance(value.get("instanceId"), str) or re.fullmatch(r"[0-9a-f]{32}", value["instanceId"]) is None
            or type(value.get("epoch")) is not int or not 1 <= value["epoch"] <= 2**63 - 1):
        return None
    return dict(value)


class LiveInputLease:
    """Explicit same-process Live lease owner; never bypasses PlayerActiveGate."""

    def __init__(self, origin: str, token: str, session_id: str, *, timeout: float = 1.0):
        if _origin(origin) is None or not _token_valid(token) or not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("invalid local Live lease configuration")
        if not _positive_number(timeout, 2.0):
            raise ValueError("invalid lease timeout")
        self._origin = origin.rstrip("/")
        self._token = token
        self._session_id = session_id
        self._timeout = timeout
        self._owner = None
        self._action = None
        self._deadline = 0.0
        self._ttl = 0.0
        self._operation_lock = asyncio.Lock()
        self._binding_lock = threading.Lock()
        self._used = False
        self._binding_attempted = False
        self._binding_request_id = None
        self._binding_identity: Optional[dict] = None
        self._bridge_deadline = 0.0
        self._phase = "new"
        self._bridge_origin = None
        self._bridge_token = None
        self._closed = False

    @property
    def valid(self) -> bool:
        return not self._closed and self._owner is not None and time.monotonic() < self._deadline

    @property
    def active(self) -> bool:
        """Prepared and owner-committed capabilities must not authorize actions."""
        return (self._phase == "bound" and self.valid and self._binding_identity is not None
                and time.monotonic() < self._bridge_deadline)

    @property
    def motor_connection_generation(self) -> Optional[int]:
        """The Motor connection generation from the current prepared or bound lease."""
        if (not self.valid or self._binding_identity is None
                or time.monotonic() >= self._bridge_deadline):
            return None
        return self._binding_identity.get("motorConnectionGeneration")

    @property
    def action_deadline(self) -> float:
        """The earliest host or Bridge deadline, only after active commit."""
        return min(self._deadline, self._bridge_deadline) if self.active else 0.0

    async def _post(self, path: str, body: dict) -> Optional[dict]:
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(self._origin + path, json=body,
                    headers={"Authorization": "Bearer " + self._token}, allow_redirects=False) as response:
                    if response.status != 200:
                        return None
                    raw = await _read_bounded(response.content, 8192)
                    result = json.loads(raw, object_pairs_hook=_unique_json_object)
                    return result if isinstance(result, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            return None

    async def acquire(self) -> bool:
        async with self._operation_lock:
            if self._used or self._closed:
                return False
            self._used = True
            started = time.monotonic()
            data = await self._post("/api/v1/agent/live/lease/acquire", {})
            if not isinstance(data, dict):
                return False
            owner, action, ttl = (_lease_credentials(data.get("renewalCredentials")),
                                  _lease_credentials(data.get("credentials")), data.get("expiresInMs"))
            if (data.get("status") != "prepared" or owner is None or action is None
                    or owner["secret"] == action["secret"]
                    or owner["instanceId"] != action["instanceId"] or owner["epoch"] != action["epoch"]
                    or type(ttl) is not int or not 500 <= ttl <= 10000):
                return False
            with self._binding_lock:
                self._owner, self._action = owner, action
                self._phase = "prepared"
                self._ttl = ttl / 1000
                self._deadline = started + self._ttl
                valid = not self._closed and time.monotonic() < self._deadline
            return valid

    def action_binding(self) -> Optional[dict]:
        """Return the action-only capability; never return the renewal secret."""
        with self._binding_lock:
            if (self._closed or self._owner is None or self._action is None
                    or time.monotonic() >= self._deadline):
                return None
            return {"sessionId": self._session_id, "credentials": dict(self._action)}

    async def publish_binding(self, bridge_origin: str, bridge_token: str) -> Optional[str]:
        """Send action-only credentials to the authenticated local bridge."""
        if _origin(bridge_origin) is None or not _token_valid(bridge_token):
            return None
        async with self._operation_lock:
            binding = self.action_binding()
            if binding is None or self._binding_attempted:
                return None
            self._binding_attempted = True
            request_id = secrets.token_hex(16)
            binding["requestId"] = request_id
            self._binding_request_id = request_id
            self._bridge_origin, self._bridge_token = bridge_origin.rstrip("/"), bridge_token
            try:
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    for attempt in (1, 2):
                        async with session.post(bridge_origin.rstrip("/") + "/lease/bind", json=binding,
                            headers={"Authorization": "Bearer " + bridge_token}, allow_redirects=False) as response:
                            if response.status != 200:
                                if attempt == 2 or response.status != 409:
                                    return None
                                # 上一次尝试的绑定可能停在 prepared_bound 或
                                # commit_pending，Bridge 会拒绝新的绑定；按它自己
                                # 报告的 requestId 精确放弃后再试一次。
                                if not await _abandon_reported_binding(session, bridge_origin, bridge_token,
                                                                       binding["sessionId"]):
                                    return None
                                continue
                            raw = await _read_bounded(response.content, 8192)
                            result = json.loads(raw, object_pairs_hook=_unique_json_object)
                            returned_id = result.get("requestId") if isinstance(result, dict) else None
                            if (isinstance(result, dict) and result.get("accepted") is True
                                    and result.get("status") in {"pending", "prepared_bound"}
                                    and returned_id == request_id and self.valid):
                                return request_id
                            return None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
                return None
            return None

    async def abandon_binding(self) -> bool:
        """Invalidate only this attempt; an unknown response is never treated as success."""
        async with self._operation_lock:
            request_id, origin, token = self._binding_request_id, self._bridge_origin, self._bridge_token
            if request_id is None or origin is None or token is None:
                return False
            self._binding_request_id = None
            self._bridge_origin = self._bridge_token = None
            try:
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    async with session.post(origin + "/lease/abandon", json={"requestId": request_id},
                        headers={"Authorization": "Bearer " + token}, allow_redirects=False) as response:
                        if response.status != 200:
                            return False
                        data = json.loads(await _read_bounded(response.content, 8192), object_pairs_hook=_unique_json_object)
                        return isinstance(data, dict) and data.get("abandoned") is True and data.get("requestId") == request_id
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
                return False

    async def renew(self) -> bool:
        async with self._operation_lock:
            if not self.valid:
                self._deadline = 0.0
                return False
            previous_deadline = self._deadline
            started = time.monotonic()
            result = await self._post("/api/v1/agent/live/lease/renew", self._owner)
            if (self._closed or result is None or result.get("renewed") is not True
                    or started >= previous_deadline or time.monotonic() >= previous_deadline):
                self._deadline = 0.0
                return False
            self._deadline = started + self._ttl
            return self.valid

    def binding_matches(self, data: object, *, status: str, observed_at: float) -> bool:
        """Validate the current Bridge socket and full, non-secret binding identity."""
        expected = self.action_binding()
        binding = data.get("liveLeaseBinding") if isinstance(data, dict) else None
        if (not self.valid or expected is None or not isinstance(data, dict)
                or data.get("protocol") != "v2" or data.get("bettergiConnectionActive") is not True
                or data.get("motorConnectionActive") is not True
                or data.get("verifiedLiveSessionId") != self._session_id
                or not isinstance(binding, dict) or binding.get("status") != status
                or binding.get("requestId") != self._binding_request_id
                or binding.get("sessionId") != self._session_id
                or binding.get("instanceId") != expected["credentials"]["instanceId"]
                or type(binding.get("epoch")) is not int
                or binding["epoch"] != expected["credentials"]["epoch"]
                or not isinstance(binding.get("connectionId"), str)
                or not binding["connectionId"] or len(binding["connectionId"]) > 128
                or type(binding.get("motorConnectionGeneration")) is not int
                or binding["motorConnectionGeneration"] < 0
                or type(binding.get("expiresInMs")) is not int
                or not 0 < binding["expiresInMs"] <= 10000):
            return False
        identity = {key: binding[key] for key in ("requestId", "sessionId", "instanceId", "epoch",
                                                   "connectionId", "motorConnectionGeneration")}
        deadline = min(self._deadline, observed_at + binding["expiresInMs"] / 1000)
        if deadline <= time.monotonic():
            return False
        if status == "prepared_bound":
            if data.get("current") is not None or (self._binding_identity is not None
                                                  and self._binding_identity != identity):
                return False
            self._binding_identity = identity
        elif self._binding_identity != identity:
            return False
        self._bridge_deadline = deadline
        return True

    async def wait_for_binding(self, bridge_origin: str, bridge_token: str, request_id: str,
                               stop_event: asyncio.Event, *, status: str) -> bool:
        """Await the prepared or bound acknowledgement for this exact connection."""
        if (_origin(bridge_origin) is None or not _token_valid(bridge_token)
                or not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
                or request_id != self._binding_request_id or status not in {"prepared_bound", "bound"}):
            return False
        deadline = min(self._deadline, time.monotonic() + self._ttl * 0.6)
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                while not stop_event.is_set() and self.valid and time.monotonic() < deadline:
                    observed_at = time.monotonic()
                    async with session.get(bridge_origin.rstrip("/") + "/feedback",
                        headers={"Authorization": "Bearer " + bridge_token}, allow_redirects=False) as response:
                        if response.status != 200:
                            return False
                        data = json.loads(await _read_bounded(response.content, 65536),
                                          object_pairs_hook=_unique_json_object)
                    binding = data.get("liveLeaseBinding") if isinstance(data, dict) else None
                    if not isinstance(binding, dict) or binding.get("requestId") != request_id:
                        return False
                    if self.binding_matches(data, status=status, observed_at=observed_at):
                        return not stop_event.is_set() and self.valid and time.monotonic() < deadline
                    pending = {"pending"} if status == "prepared_bound" else {"pending", "prepared_bound", "owner_committed", "commit_pending"}
                    if binding.get("status") not in pending:
                        return False
                    if (status == "bound" and binding.get("status") != "pending"
                            and (self._binding_identity is None or any(
                                binding.get(key) != value for key, value in self._binding_identity.items()))):
                        return False
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            return False
        return False

    async def commit_owner(self) -> bool:
        """Only the player sends renewal credentials to the host; this remains non-active."""
        async with self._operation_lock:
            identity = dict(self._binding_identity) if self._binding_identity is not None else None
            if (self._phase != "prepared" or not self.valid or identity is None
                    or time.monotonic() >= self._bridge_deadline):
                return False
            previous_deadline = self._deadline
            started = time.monotonic()
            result = await self._post("/api/v1/agent/live/lease/commit", {
                "credentials": dict(self._owner), "leaseRequestId": identity["requestId"],
                "sessionId": identity["sessionId"], "connectionId": identity["connectionId"],
            })
            if (not isinstance(result, dict) or result.get("status") != "owner_committed"
                    or any(result.get(key) != identity[key]
                           for key in ("sessionId", "instanceId", "epoch", "requestId", "connectionId"))
                    or type(result.get("epoch")) is not int or type(result.get("expiresInMs")) is not int
                    or not 0 < result["expiresInMs"] <= 10000
                    or not self.valid or time.monotonic() >= previous_deadline
                    or time.monotonic() >= self._bridge_deadline):
                self._deadline = 0.0
                return False
            self._deadline = min(previous_deadline, started + result["expiresInMs"] / 1000)
            if not self.valid:
                return False
            self._phase = "owner_committed"
            return True

    async def commit_bridge(self, stop_event: asyncio.Event) -> bool:
        """Commit the exact prepared connection without sending the owner credential."""
        async with self._operation_lock:
            identity = dict(self._binding_identity) if self._binding_identity is not None else None
            origin, token = self._bridge_origin, self._bridge_token
            if (self._phase != "owner_committed" or not self.valid or identity is None
                    or origin is None or token is None or time.monotonic() >= self._bridge_deadline):
                return False
            try:
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    async with session.post(origin + "/lease/commit", json=identity,
                        headers={"Authorization": "Bearer " + token}, allow_redirects=False) as response:
                        if response.status != 200:
                            return False
                        data = json.loads(await _read_bounded(response.content, 8192),
                                          object_pairs_hook=_unique_json_object)
                if (not isinstance(data, dict) or data.get("accepted") is not True
                        or data.get("status") != "commit_pending"
                        or ("requestId" in data and data["requestId"] != identity["requestId"])
                        or not self.valid or time.monotonic() >= self._bridge_deadline):
                    return False
                self._phase = "commit_pending"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
                return False
        if not await self.wait_for_binding(origin, token, identity["requestId"], stop_event, status="bound"):
            return False
        if not self.valid or time.monotonic() >= self._bridge_deadline:
            return False
        self._phase = "bound"
        return True

    async def heartbeat(self, stop_event: asyncio.Event, on_failure: Callable[[], object], *,
                        interval: float = 0.5) -> None:
        """Renew from the player task; a failure triggers cancellation and never reauthorizes."""
        if not isinstance(stop_event, asyncio.Event) or not callable(on_failure) or not _positive_number(interval, 1.0):
            raise ValueError("invalid heartbeat configuration")
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            if not await self.renew():
                result = on_failure()
                if inspect.isawaitable(result):
                    await result
                return

    async def close(self) -> bool:
        with self._binding_lock:
            self._closed = True
            self._phase = "revoked"
            self._deadline = 0.0
            self._bridge_deadline = 0.0
        async with self._operation_lock:
            with self._binding_lock:
                owner = self._owner
                self._owner = self._action = None
            if owner is None:
                return False
            result = await self._post("/api/v1/agent/live/lease/release", owner)
            return result is not None and result.get("released") is True


class PlayerActiveGate:
    """Own a monitoring lease; no dependency injection can authorize actions."""

    def __init__(self, config: GateConfig, on_revoke: RevokeCallback, *,
                 speech_output: Optional[PlayerSpeechOutput] = None,
                 controlled_mvp_confirmed: bool = False) -> None:
        """Configure endpoints and a mandatory stop-notification callback."""
        if not callable(on_revoke):
            raise TypeError("on_revoke must be callable")
        self._config = config
        # This is an operator acknowledgement, not proof of input ownership or game response.
        self._controlled_mvp_confirmed = config.controlled_mvp and controlled_mvp_confirmed is True
        self._controlled_session_id: Optional[str] = None
        self._dialogue_open = False
        self._on_revoke = on_revoke
        self._speech_output = speech_output
        self._speech_deadline = 0.0
        self._motor_trust: Optional[dict] = None
        self._motor_trust_revoked = False
        self._motor_deadline = 0.0
        self._motor_generation: Optional[int] = None
        self._motor_protocol_status = "unverified"
        self._execution_channel_status = "unverified"
        self._execution_channel_deadline = 0.0
        self._monitor: Optional[asyncio.Task[None]] = None
        self._operation_lock = asyncio.Lock()
        self._deadline = 0.0
        self._observed_frame_deadline = 0.0
        self._decision = GateDecision(config.mode, False, ("not_started",))
        self._session_id: Optional[str] = None
        self._instance_lock: Optional[PlayerInstanceLock] = None
        self._identity: Optional[dict] = None
        self._identity_agent = None
        self._observation_binding: Optional[dict] = None
        self._observation_revoked = False
        self._input_lease: Optional[LiveInputLease] = None
        self._lease_heartbeat: Optional[asyncio.Task] = None
        self._lease_release_lock = asyncio.Lock()
        self.callback_error: Optional[Exception] = None

    def _load_motor_trust(self, home: Path) -> None:
        """Snapshot an explicit local trust configuration; never trust a remote URL alone."""
        path = self._config.trusted_motor_config
        if path is None:
            return
        try:
            requested = Path(path).absolute()
            resolved = requested.resolve(strict=True)
            if not resolved.is_relative_to(home) or not resolved.is_file():
                return
            with resolved.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                raw = handle.read(8193)
            current = requested.stat()
            if (len(raw) > 8192 or not opened.st_ino
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                return
            config = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            if not isinstance(config, dict) or set(config) != {"trusted_motor_origin"}:
                return
            origin = _motor_origin(config["trusted_motor_origin"])
            if origin is None:
                return
            self._motor_trust = {"requested": requested, "resolved": resolved,
                                 "identity": (opened.st_dev, opened.st_ino),
                                 "digest": hashlib.sha256(raw).digest(), "origin": origin}
        except (OSError, ValueError, TypeError, UnicodeError):
            self._motor_trust = None

    def _motor_trust_file_valid(self) -> bool:
        """A changed or replaced trust file permanently revokes this admission."""
        trust = self._motor_trust
        if trust is None or self._motor_trust_revoked:
            return False
        try:
            if trust["requested"].resolve(strict=True) != trust["resolved"]:
                raise ValueError("Motor trust file changed")
            with trust["resolved"].open("rb") as handle:
                stat = os.fstat(handle.fileno())
                raw = handle.read(8193)
            current = trust["requested"].stat()
            if (len(raw) > 8192 or (stat.st_dev, stat.st_ino) != trust["identity"]
                    or (current.st_dev, current.st_ino) != trust["identity"]
                    or hashlib.sha256(raw).digest() != trust["digest"]):
                raise ValueError("Motor trust file changed")
            return True
        except (OSError, ValueError, TypeError):
            self._motor_trust_revoked = True
            return False

    def _motor_protocol_deadline(self, feedback: object, observed_at: float) -> float:
        """Accept only a trusted connection's short-lived protocol self-report."""
        lease = self._input_lease
        if not self._motor_trust_file_valid() or lease is None:
            return 0.0
        generation = lease.motor_connection_generation
        capability = feedback.get("motorCapability") if isinstance(feedback, dict) else None
        declarations = capability.get("declarations") if isinstance(capability, dict) else None
        if (type(generation) is not int or generation <= 0 or not isinstance(feedback, dict)
                or feedback.get("protocol") != "v2" or feedback.get("motorConnectionActive") is not True
                or not isinstance(capability, dict) or capability.get("evidence") != "server_self_report"
                or capability.get("bridgeConstraintProtocol") != "intent-v2-mode-enforcement-v1"
                or type(capability.get("connectionGeneration")) is not int
                or capability["connectionGeneration"] != generation
                or not isinstance(capability.get("nonce"), str)
                or re.fullmatch(r"[0-9a-f]{32}", capability["nonce"]) is None
                or type(capability.get("expiresInMs")) is not int
                or not 0 < capability["expiresInMs"] <= 60000
                or not isinstance(declarations, dict)
                or set(declarations) != {"protocol", "mock", "intentRequired", "intentForcesMotor"}
                or declarations.get("protocol") != "lumine-live-v2" or declarations.get("mock") is not False
                or declarations.get("intentRequired") is not True or declarations.get("intentForcesMotor") is not True):
            return 0.0
        origin = _motor_origin(capability.get("origin"))
        transport = capability.get("transport")
        if (origin is None or origin != self._motor_trust["origin"]
                or (transport == "wss_authenticated" and not origin.startswith("wss://"))
                or (transport == "loopback_ws" and not origin.startswith("ws://"))
                or transport not in {"wss_authenticated", "loopback_ws"}
                or (self._motor_generation is not None and self._motor_generation != generation)):
            return 0.0
        deadline = observed_at + capability["expiresInMs"] / 1000
        if deadline <= time.monotonic():
            return 0.0
        self._motor_generation = generation
        self._motor_protocol_status = "trusted_server_protocol_declaration"
        return deadline

    def prepare_identity(self, profile_path: Path, session_name: str, *, seed_default: bool = False) -> dict:
        """在禁止输入的预备阶段持锁并原子绑定本次玩家身份。"""
        if self._decision.reasons != ("not_started",) or self._instance_lock is not None:
            raise ValueError("玩家身份准备只能执行一次")
        if (not isinstance(session_name, str) or not session_name.strip() or len(session_name) > 120
                or any(ord(char) < 32 or ord(char) == 127 for char in session_name)):
            raise ValueError("玩家会话身份无效")
        if self._config.player_home is None:
            raise ValueError("玩家目录缺失")
        home = Path(self._config.player_home).resolve(strict=True)
        state_dir = (home / "state").resolve(strict=True)
        profile_file = Path(profile_path).resolve()
        if state_dir != home / "state" or not profile_file.is_relative_to(home):
            raise ValueError("玩家状态或档案不在固定玩家目录内")
        lock_path = state_dir / "player.lock"
        if lock_path.resolve() != lock_path:
            raise ValueError("玩家锁路径不可重定向")
        lock = PlayerInstanceLock(lock_path)
        log = None
        try:
            lock.__enter__()
            if not lock.is_held:
                raise InstanceLockError("无法确认玩家目录锁")
            if seed_default and not profile_file.exists():
                seed_default_profile(profile_file)
            profile_file = profile_file.resolve(strict=True)
            if not profile_file.is_relative_to(home):
                raise ValueError("玩家档案不在固定玩家目录内")
            profile_bytes = profile_file.read_bytes()
            profile = load_profile(profile_file)
            if profile_file.read_bytes() != profile_bytes:
                raise ValueError("玩家档案加载期间发生变化")
            scope = "player-" + hashlib.sha256(profile.player_id.encode("utf-8")).hexdigest()
            run_id = secrets.token_hex(16)
            db_path = state_dir / "cognitive_events.sqlite"
            if db_path.resolve() != db_path:
                raise ValueError("玩家数据库路径不可重定向")
            log = PlayerEventLog(db_path)
            stat = db_path.stat()
            if not stat.st_ino:
                raise ValueError("无法确认玩家数据库文件身份")
            transaction = PlayerCognitiveTransaction(log)
            with log._lock:
                foreign = log._db.execute(
                    "SELECT 1 FROM scoped_projections WHERE scope != ? LIMIT 1", (scope,)).fetchone()
                sessions = [row[0] for row in log._db.execute(
                    "SELECT session_id FROM projections WHERE name = ?", (SESSION_BINDING,)).fetchall()]
            if foreign:
                raise ValueError("玩家数据库包含其他玩家的投影")
            for session in sessions:
                transaction._binding(session, scope)
            prefix = f"{profile.player_id}:{session_name}:{run_id}"
            owner_session = f"{profile.player_id}:__home_owner__"
            bootstrap_session = prefix + ":bootstrap"
            generation_session = prefix + ":0"
            for session in (owner_session, bootstrap_session, generation_session):
                event_id = "binding-" + hashlib.sha256(session.encode("utf-8")).hexdigest()
                transaction.bind_session(session_id=session, player_scope=scope, event_id=event_id)
            self._instance_lock = lock
            self._identity = {"profile": profile, "profile_file": profile_file,
                              "profile_hash": hashlib.sha256(profile_bytes).digest(),
                              "scope": scope, "run_id": run_id, "session_name": session_name,
                              "db_path": db_path, "db_identity": (stat.st_dev, stat.st_ino),
                              "log": log, "transaction": transaction,
                              "owner_session": owner_session, "bootstrap_session": bootstrap_session,
                              "generation_session": generation_session, "live_session": None,
                              "live_binding_session": None}
            self._load_motor_trust(home)
            return {"profile": profile, "run_id": run_id, "event_log": log,
                    "transaction": transaction, "scope": scope, "bootstrap_session": bootstrap_session}
        except BaseException:
            try:
                if log is not None:
                    log.close()
            finally:
                lock.__exit__(None, None, None)
            raise

    def attach_identity_agent(self, agent: object) -> None:
        """将已绑定的零代玩家对象接入门控，换代时立即禁止输出。"""
        identity = self._identity
        if (identity is None or self._identity_agent is not None
                or type(getattr(agent, "_session_generation", None)) is not int
                or agent._session_generation != 0 or getattr(agent, "profile", None) is not identity["profile"]
                or getattr(agent, "session_id", None) != identity["session_name"]
                or getattr(getattr(agent, "recorder", None), "run_id", None) != identity["run_id"]
                or getattr(agent, "cognitive_transaction", None) is not identity["transaction"]):
            raise ValueError("玩家运行代次或身份无效")
        self._identity_agent = agent
        observer = getattr(agent, "observation_provider", None)
        experience = getattr(agent, "experience_store", None)
        regions = getattr(observer, "_named_regions", None)
        if (type(observer) is ScreenObserver and type(experience) is PlayerExperience
                and experience._log is identity["log"] and isinstance(regions, dict)
                and bool({"quest", "hud"}.intersection(regions))
                and _origin(observer.base_url) == _origin(self._config.bettergi_url)
                and observer.token == self._config.bettergi_token
                and getattr(agent, "context_mode", None) == "v2"
                and getattr(agent, "_cognitive_scope", None) == identity["scope"]
                and getattr(agent, "intent_publisher", None) is not None
                and getattr(agent, "speech_output", None) is self._speech_output
                and getattr(agent, "_active_gate", None) is self):
            self._observation_binding = {
                "observer": observer, "experience": experience, "evidence": observer.evidence,
                "publisher": agent.intent_publisher, "speech": agent.speech_output,
                "session_id": observer.session_id, "regions_ref": regions,
                "regions": {name: dict(roi) for name, roi in regions.items()},
            }

    def _observation_chain_valid(self) -> bool:
        binding, identity, agent = self._observation_binding, self._identity, self._identity_agent
        if binding is None or identity is None or agent is None or self._observation_revoked:
            return False
        observer = binding["observer"]
        valid = (self._identity_files_valid() and getattr(agent, "_closed", True) is False
                and getattr(agent, "_active_gate", None) is self
                and getattr(agent, "observation_provider", None) is observer
                and getattr(agent, "experience_store", None) is binding["experience"]
                and binding["experience"]._log is identity["log"]
                and getattr(agent, "cognitive_transaction", None) is identity["transaction"]
                and getattr(agent, "context_mode", None) == "v2"
                and getattr(agent, "_cognitive_scope", None) == identity["scope"]
                and getattr(agent, "intent_publisher", None) is binding["publisher"]
                and getattr(agent, "speech_output", None) is binding["speech"]
                and binding["speech"] is self._speech_output
                and observer.evidence is binding["evidence"]
                and observer.session_id == binding["session_id"]
                and observer._named_regions is binding["regions_ref"]
                and observer._named_regions == binding["regions"]
                and _origin(observer.base_url) == _origin(self._config.bettergi_url)
                and observer.token == self._config.bettergi_token)
        if not valid:
            self._observation_revoked = True
        return valid

    def _identity_files_valid(self) -> bool:
        identity, lock = self._identity, self._instance_lock
        if identity is None or lock is None or not lock.is_held:
            return False
        try:
            stat = identity["db_path"].stat()
            return ((stat.st_dev, stat.st_ino) == identity["db_identity"] and stat.st_ino != 0
                    and hashlib.sha256(identity["profile_file"].read_bytes()).digest() == identity["profile_hash"]
                    and not identity["log"]._closed
                    and (self._identity_agent is None or
                         (type(getattr(self._identity_agent, "_session_generation", None)) is int
                          and self._identity_agent._session_generation == 0
                          and getattr(self._identity_agent, "profile", None) is identity["profile"]
                          and getattr(self._identity_agent, "session_id", None) == identity["session_name"]
                          and getattr(getattr(self._identity_agent, "recorder", None), "run_id", None) == identity["run_id"]
                          and getattr(self._identity_agent, "cognitive_transaction", None) is identity["transaction"])))
        except (OSError, ValueError):
            return False

    def _identity_valid_for_live(self, live_session: str) -> bool:
        """绑定真实 Live 会话并验证数据库绑定及其他 scope 不冲突。"""
        if not self._identity_files_valid() or not isinstance(live_session, str) or not live_session.strip():
            return False
        identity = self._identity
        try:
            if identity["live_session"] is not None and identity["live_session"] != live_session:
                return False
            transaction = identity["transaction"]
            if identity["live_binding_session"] is None:
                name = (identity["generation_session"] + ":live-" +
                        hashlib.sha256(live_session.encode("utf-8")).hexdigest())
                event_id = "binding-" + hashlib.sha256(name.encode("utf-8")).hexdigest()
                transaction.bind_session(session_id=name, player_scope=identity["scope"], event_id=event_id)
                identity["live_session"], identity["live_binding_session"] = live_session, name
            log = identity["log"]
            with log._lock:
                db = log._db
                db_file = db.execute("PRAGMA database_list").fetchone()[2]
                if Path(db_file).resolve() != identity["db_path"]:
                    return False
                if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                    return False
                if db.execute("SELECT 1 FROM scoped_projections WHERE scope != ? LIMIT 1", (identity["scope"],)).fetchone():
                    return False
                rows = db.execute(
                    "SELECT p.session_id, p.state, e.event_id, e.session_id, e.type, e.source, e.payload "
                    "FROM projections p LEFT JOIN events e ON e.seq = p.revision WHERE p.name = ?",
                    (SESSION_BINDING,)).fetchall()
                for row in rows:
                    state = json.loads(row[1], object_pairs_hook=_unique_json_object)
                    event = json.loads(row[6], object_pairs_hook=_unique_json_object) if row[6] else None
                    if (not isinstance(state, dict) or set(state) != {"version", "session_id", "player_scope", "event_id"}
                            or type(state.get("version")) is not int or state["version"] != 1
                            or state.get("player_scope") != identity["scope"] or state.get("session_id") != row[0]
                            or state.get("event_id") != row[2] or row[3] != row[0]
                            or row[4] != "cognitive.session_binding" or row[5] != "player_cognitive_transaction"
                            or event != state):
                        return False
                for key in ("owner_session", "bootstrap_session", "generation_session", "live_binding_session"):
                    transaction._binding(identity[key], identity["scope"])
            return self._identity_files_valid()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return False

    @property
    def motor_protocol_status(self) -> str:
        """Report only the trusted server's protocol declaration, not model compliance."""
        if time.monotonic() >= self._motor_deadline or not self._motor_trust_file_valid():
            return "unverified"
        return self._motor_protocol_status

    @property
    def execution_feedback_channel_status(self) -> str:
        """A declared channel is not evidence of input or a game response."""
        if (time.monotonic() >= self._execution_channel_deadline
                or self._input_lease is None or not self._input_lease.active):
            return "unverified"
        return self._execution_channel_status

    @property
    def decision(self) -> GateDecision:
        """Return the latest result, treating a missed health deadline as revoked."""
        if self._decision.allowed and not self.is_allowed:
            return GateDecision(self._config.mode, False, ("health_lease_expired",), self._session_id)
        return self._decision

    @property
    def is_allowed(self) -> bool:
        """Fail closed when the monitor stopped or its freshness lease expired."""
        return (self._decision.allowed and self._monitor is not None
                and not self._monitor.done() and time.monotonic() < self._deadline
                and (self._config.mode != "full_active" or time.monotonic() < self._speech_deadline)
                and (self._config.mode == "observer" or
                     (time.monotonic() < self._motor_deadline and self._motor_trust_file_valid()
                      and time.monotonic() < self._execution_channel_deadline
                      and self._observation_chain_valid()))
                and (self._config.mode == "observer" or self._identity_files_valid())
                and (self._config.mode == "observer" or (self._input_lease is not None
                     and self._input_lease.active and self._lease_heartbeat is not None
                     and not self._lease_heartbeat.done())))

    @property
    def can_publish_motor(self) -> bool:
        """The host independently enforces the dialogue latch at injection time."""
        return self.is_allowed and self._dialogue_open

    async def _prepare_live_lease(self, session_id: str) -> bool:
        """Acquire an inactive lease and bind it to the current Bridge connection."""
        lease = LiveInputLease(self._config.bettergi_url, self._config.bettergi_token, session_id,
                               timeout=self._config.request_timeout_seconds)
        self._input_lease = lease
        if not await lease.acquire():
            return False
        stop = asyncio.Event()

        async def failed() -> None:
            if self._decision.allowed:
                await self._revoke(("live_input_lease_expired",))

        self._lease_heartbeat = asyncio.create_task(lease.heartbeat(stop, failed), name="player-live-lease")
        request_id = await lease.publish_binding(self._config.bridge_url, self._config.bridge_token)
        return (request_id is not None and await lease.wait_for_binding(
                    self._config.bridge_url, self._config.bridge_token, request_id, stop, status="prepared_bound")
                and lease.valid and not self._lease_heartbeat.done())

    async def _commit_live_lease(self) -> bool:
        """Commit only after every independent safety condition has passed."""
        lease = self._input_lease
        if (lease is None or not lease.valid or self._lease_heartbeat is None
                or self._lease_heartbeat.done()):
            return False
        if not await lease.commit_owner():
            return False
        return await lease.commit_bridge(asyncio.Event()) and lease.active and not self._lease_heartbeat.done()

    async def _release_live_lease(self) -> None:
        """Revoke host input first, then abandon only this Bridge binding."""
        heartbeat, self._lease_heartbeat = self._lease_heartbeat, None
        if heartbeat is not None and heartbeat is not asyncio.current_task():
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with self._lease_release_lock:
            lease, self._input_lease = self._input_lease, None
            if lease is not None:
                async def revoke_remote() -> None:
                    try:
                        await lease.close()
                    finally:
                        await lease.abandon_binding()
                cleanup = asyncio.create_task(revoke_remote())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise

    async def start(self) -> GateDecision:
        """Inspect real endpoints and start monitoring only on successful admission.

        One instance may be started once. A denied or revoked gate is terminal;
        create a new instance for another admission attempt.
        """
        async with self._operation_lock:
            if self._decision.reasons != ("not_started",):
                return self.decision
            try:
                result = await self._inspect()
                if self._config.mode != "observer":
                    blockers = {"invalid_timing", "bettergi_not_loopback_origin", "bettergi_token_invalid",
                                "bridge_not_loopback_origin", "bridge_token_invalid", "motor_trust_unavailable",
                                "single_writer_lock_unavailable",
                                "persistent_store_unavailable", "persistent_store_invalid", "persistent_store_binding_unverified",
                                "bettergi_status_unverified", "bettergi_runtime_unhealthy", "live_status_unverified",
                                "live_session_unhealthy", "visual_frame_stale_or_unknown", "bridge_protocol_unverified",
                                "motor_transport_unverified", "bridge_release_unconfirmed"}
                    if result.session_id is not None and not blockers.intersection(result.reasons):
                        if await self._prepare_live_lease(result.session_id):
                            # Prepared binding is non-executable. Re-read all independent gates before commit.
                            result = await self._inspect()
                            if result.reasons == ("action_dispatch_lease_unenforced",):
                                if await self._commit_live_lease():
                                    result = await self._inspect()
                                else:
                                    result = GateDecision(self._config.mode, False,
                                                          ("live_input_lease_unavailable",), result.session_id)
                        else:
                            result = GateDecision(self._config.mode, False,
                                tuple(dict.fromkeys((*result.reasons, "live_input_lease_unavailable"))), result.session_id)
            except BaseException:
                try:
                    await self._release_live_lease()
                finally:
                    self._decision = GateDecision(self._config.mode, False, ("admission_interrupted",))
                raise
            self._decision = result
            if result.allowed:
                self._session_id = result.session_id
                self._deadline = self._observed_frame_deadline
                self._monitor = asyncio.create_task(self._watch(), name="player-active-gate")
            else:
                await self._release_live_lease()
            return self.decision

    async def close(self) -> None:
        """Revoke once, stop monitoring, and release any owned instance lock."""
        async with self._operation_lock:
            monitor, self._monitor = self._monitor, None
            notify = self._decision.allowed
            if notify:
                self._deadline = 0.0
                self._decision = GateDecision(self._config.mode, False, ("gate_closed",), self._session_id)
            elif self._decision.reasons == ("not_started",):
                self._decision = GateDecision(self._config.mode, False, ("gate_closed",))
            decision = self._decision
        if monitor is not None and monitor is not asyncio.current_task():
            # A monitor already notifying a revocation should finish that callback.
            if notify:
                monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if notify:
            await self._notify(decision)
        try:
            await self._release_live_lease()
        finally:
            self._release_lock()

    async def _revoke(self, reasons: tuple[str, ...]) -> None:
        """Change state before notifying; a failing callback never restores it."""
        self._deadline = 0.0
        self._dialogue_open = False
        self._controlled_mvp_confirmed = False
        self._decision = GateDecision(self._config.mode, False, reasons, self._session_id)
        # Keep the single-writer lock until the owner has stopped its work and
        # calls close(); a stop request is not a physical input-release receipt.
        try:
            await self._notify(self._decision)
        finally:
            await self._release_live_lease()

    async def _notify(self, decision: GateDecision) -> None:
        """Run a synchronous or asynchronous callback without restoring admission."""
        try:
            result = self._on_revoke(decision)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            self.callback_error = exc

    def _release_lock(self) -> None:
        """先关闭本次数据库连接，再释放该玩家的系统锁。"""
        identity, self._identity = self._identity, None
        self._identity_agent = None
        self._observation_binding = None
        lock, self._instance_lock = self._instance_lock, None
        try:
            if identity is not None:
                identity["log"].close()
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)

    async def _watch(self) -> None:
        """Recheck without ever automatically reauthorizing after failure."""
        try:
            while True:
                deadline = (min(self._deadline, self._speech_deadline)
                            if self._config.mode == "full_active" else self._deadline)
                if self._config.mode != "observer":
                    deadline = min(deadline, self._motor_deadline, self._execution_channel_deadline,
                                   self._input_lease.action_deadline if self._input_lease else 0.0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._revoke(("health_lease_expired",))
                    return
                await asyncio.sleep(min(self._config.poll_interval_seconds, remaining))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    await self._revoke(("health_lease_expired",))
                    return
                try:
                    result = await asyncio.wait_for(self._inspect(), timeout=remaining)
                except asyncio.TimeoutError:
                    await self._revoke(("health_lease_expired",))
                    return
                if not result.allowed:
                    await self._revoke(result.reasons)
                    return
                if result.session_id != self._session_id:
                    await self._revoke(("session_changed",))
                    return
                self._deadline = self._observed_frame_deadline
                self._decision = result
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._revoke(("health_monitor_failed",))

    async def _get(self, session: aiohttp.ClientSession, origin: str, path: str,
                   token: str) -> Optional[dict]:
        """Verify rejection of a wrong credential, then read bounded JSON over HTTP."""
        wrong = "gate-invalid-" + secrets.token_hex(24)
        if wrong == token:
            wrong += "x"
        try:
            async with session.get(origin + path, headers={"Authorization": "Bearer " + wrong},
                                   allow_redirects=False) as response:
                if response.status not in (401, 403):
                    return None
            async with session.get(origin + path, headers={"Authorization": "Bearer " + token},
                                   allow_redirects=False) as response:
                if response.status != 200:
                    return None
                if response.content_length is not None and response.content_length > 65536:
                    return None
                try:
                    await response.content.readexactly(65537)
                    return None
                except asyncio.IncompleteReadError as exc:
                    raw = exc.partial
                import json
                result = json.loads(raw)
                return result if isinstance(result, dict) else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            return None

    async def _inspect(self) -> GateDecision:
        """Collect independent local configuration and authenticated observations."""
        c = self._config
        reasons: list[str] = []
        self._dialogue_open = False
        dialogue_open = False
        if c.mode not in ("observer", "motor_active", "full_active"):
            return GateDecision(c.mode, False, ("unknown_mode",))
        if (not _positive_number(c.max_frame_age_seconds, 5.0)
                or not _positive_number(c.poll_interval_seconds, 2.0)
                or not _positive_number(c.request_timeout_seconds, 2.0)
                or c.poll_interval_seconds >= c.max_frame_age_seconds):
            return GateDecision(c.mode, False, ("invalid_timing",))
        origin = _origin(c.bettergi_url)
        if origin is None:
            reasons.append("bettergi_not_loopback_origin")
        if not _token_valid(c.bettergi_token):
            reasons.append("bettergi_token_invalid")
        active = c.mode != "observer"
        bridge = _origin(c.bridge_url) if active else None
        if not active and (c.bridge_url or c.bridge_token):
            reasons.append("observer_intent_config_forbidden")
        if active:
            if bridge is None:
                reasons.append("bridge_not_loopback_origin")
            if not _token_valid(c.bridge_token):
                reasons.append("bridge_token_invalid")
            if not self._motor_trust_file_valid():
                reasons.append("motor_trust_unavailable")
            if self._instance_lock is None or not self._instance_lock.is_held:
                reasons.append("single_writer_lock_unavailable")
            store_reason = _persistent_store_reason(c.player_home)
            if store_reason:
                reasons.append(store_reason)
            if c.controlled_mvp:
                if not self._controlled_mvp_confirmed:
                    reasons.append("operator_confirmation_required")
            else:
                reasons.append("input_release_confirmation_unavailable")
        agent = live = feedback = None
        bridge_observed_at = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=c.request_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            if origin and _token_valid(c.bettergi_token):
                agent = await self._get(session, origin, "/api/v1/agent/status", c.bettergi_token)
                live = await self._get(session, origin, "/api/v1/agent/live/status", c.bettergi_token)
            if bridge and _token_valid(c.bridge_token):
                bridge_observed_at = time.monotonic()
                feedback = await self._get(session, bridge, "/feedback", c.bridge_token)
        if agent is None:
            reasons.append("bettergi_status_unverified")
        elif (agent.get("enabled") is not True or agent.get("runtimeRunning") is not True
              or agent.get("stopRequested") is not False
              or not isinstance(agent.get("runtime"), dict)
              or agent["runtime"].get("isAlive") is not True
              or agent["runtime"].get("isForeground") is not True
              or agent["runtime"].get("isMinimized") is not False):
            reasons.append("bettergi_runtime_unhealthy")
        if c.mode == "full_active":
            # 只有 full_active 会由 Player 亲自推进或选择对话，因此也必须由
            # 标定过的宿主策略提供退出证据与动作授权。motor_active 只做移动、
            # 战斗与交互；对话一旦触发，宿主的 Talk 闩锁会让 dialogueGate
            # 变为 "latched"，下面的 gate 检查随即拒绝主动执行。
            safety = live.get("safety") if isinstance(live, dict) else None
            if not isinstance(safety, dict) or safety.get("canConfirmDialogueExit") is not True:
                reasons.append("dialogue_exit_evidence_unavailable")
            if not isinstance(safety, dict) or safety.get("canAuthorizeDialogueActions") is not True:
                reasons.append("dialogue_action_authorization_unavailable")
        session_id = None
        if live is None:
            reasons.append("live_status_unverified")
        else:
            session_id = live.get("sessionId")
            if (live.get("running") is not True or not isinstance(session_id, str)
                    or not session_id.strip()):
                reasons.append("live_session_unhealthy")
                session_id = None
            if active and c.controlled_mvp and self._controlled_mvp_confirmed and session_id is not None:
                if self._controlled_session_id is None:
                    self._controlled_session_id = session_id
                elif self._controlled_session_id != session_id:
                    reasons.append("operator_confirmation_session_changed")
            frame_deadline = _frame_deadline(live.get("lastFrameAtUtc"), c.max_frame_age_seconds)
            self._observed_frame_deadline = frame_deadline
            if (type(live.get("frameSeq")) is not int or live["frameSeq"] <= 0
                    or frame_deadline <= time.monotonic()):
                reasons.append("visual_frame_stale_or_unknown")
            if active:
                safety = live.get("safety")
                if not isinstance(safety, dict):
                    reasons.append("strict_safety_unverified")
                else:
                    dialogue_open = safety.get("dialogueGate") == "open"
                    talk_active = (c.controlled_mvp and self._controlled_mvp_confirmed
                                   and self._decision.allowed and safety.get("dialogueGate") == "latched")
                    if (type(safety.get("capabilityVersion")) is not int
                            or safety["capabilityVersion"] != 1
                            or safety.get("strictModeActive") is not True
                            or (safety.get("readyForV2") is not True and not talk_active)):
                        reasons.append("strict_safety_unverified")
                    if not dialogue_open and not talk_active:
                        reasons.append("dialogue_gate_not_open")
                    if c.controlled_mvp and self._controlled_mvp_confirmed:
                        keys, depth = live.get("pressedKeys"), live.get("actionQueueDepth")
                        if (not isinstance(keys, list) or type(depth) is not int or depth < 0
                                or ((not self._decision.allowed or talk_active or (not keys and depth == 0))
                                    and (safety.get("inputReleaseCallsReturned") is not True
                                         or keys != [] or depth != 0))):
                            reasons.append("input_release_unverified")
                    elif (safety.get("inputReleaseCallsReturned") is not True
                            or safety.get("inputReleaseConfirmed") is not True
                            or live.get("pressedKeys") != []
                            or type(live.get("actionQueueDepth")) is not int
                            or live["actionQueueDepth"] != 0):
                        reasons.append("input_release_unverified")
                    if (safety.get("frameRecognitionHealth") != "healthy"
                            or type(safety.get("frameSeq")) is not int
                            or safety["frameSeq"] != live.get("frameSeq")
                            or _frame_deadline(safety.get("frameTimestampUtc"), c.max_frame_age_seconds) <= time.monotonic()
                            or safety.get("frameTimestampUtc") != live.get("lastFrameAtUtc")):
                        reasons.append("visual_recognition_unverified")
        if active and (session_id is None or not self._identity_valid_for_live(session_id)):
            reasons.append("persistent_store_binding_unverified")
        if c.mode == "full_active":
            speech_deadline = 0.0
            if type(self._speech_output) is PlayerSpeechOutput and session_id is not None:
                try:
                    speech_deadline = await self._speech_output.ready_deadline(session_id)
                except Exception:
                    speech_deadline = 0.0
            self._speech_deadline = speech_deadline
            if speech_deadline <= time.monotonic():
                reasons.append("speech_exclusive_readiness_unverified")
        # 旧 Director 的隔离由运行链组合保证：CLI 与 run_episode 不会启动
        # integrations/lumine/live_director.py；若存在录制器，full_active 的
        # speech_exclusive_readiness 还会核对 heartbeat 里的
        # orchestrator_director_configured is False。这里不再伪造一个无生产者
        # 的运行时证据字段（此前该理由对 motor_active 恒为真，导致主动模式
        # 永远无法通过）。
        if active:
            self._motor_deadline = self._motor_protocol_deadline(feedback, bridge_observed_at)
            if self._motor_deadline <= time.monotonic():
                self._motor_protocol_status = "unverified"
                reasons.append("motor_prompt_unverified")
            lease = self._input_lease
            lease_alive = (lease is not None and self._lease_heartbeat is not None
                           and not self._lease_heartbeat.done())
            binding_valid = (lease_alive and lease.active
                             and lease.binding_matches(feedback, status="bound", observed_at=bridge_observed_at))
            prepared_valid = (lease_alive and not lease.active
                              and lease.binding_matches(feedback, status="prepared_bound", observed_at=bridge_observed_at))
            if not binding_valid:
                reasons.append("action_dispatch_lease_unenforced")
            if ((binding_valid or prepared_valid) and isinstance(feedback, dict)
                    and feedback.get("executionFeedbackProtocol") == "intent-v2-system-input-v1"
                    and feedback.get("executionFeedbackEvidence") == "host_channel_declaration"
                    and "executionFeedbackGameResponseConfirmed" in feedback
                    and feedback["executionFeedbackGameResponseConfirmed"] is None):
                self._execution_channel_status = "host_channel_declaration"
                self._execution_channel_deadline = min(lease._deadline, lease._bridge_deadline)
            else:
                self._execution_channel_status = "unverified"
                self._execution_channel_deadline = 0.0
            if (self._execution_channel_deadline <= time.monotonic()
                    or session_id is None or not self._observation_chain_valid()):
                reasons.append("execution_confirmation_unavailable")
        if active and (feedback is None or feedback.get("protocol") != "v2"):
            reasons.append("bridge_protocol_unverified")
        elif active:
            if (feedback.get("motorConnectionActive") is not True
                    or feedback.get("bettergiConnectionActive") is not True
                    or feedback.get("verifiedLiveSessionId") != session_id
                    or feedback.get("motorPromptProtocol") != "v2_requires_motor_prompt"):
                reasons.append("motor_transport_unverified")
            if feedback.get("inputReleaseUnconfirmed") is not False:
                reasons.append("bridge_release_unconfirmed")
        self._dialogue_open = dialogue_open and not reasons
        return GateDecision(c.mode, not reasons, tuple(dict.fromkeys(reasons)), session_id)
