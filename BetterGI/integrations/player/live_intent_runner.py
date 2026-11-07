#!/usr/bin/env python3
"""Lumine 常驻意图运行器：租约常驻、意图热切换、停止 hold 窗口。

设计目标：消除意图切换时可见的操作空档。
- 进程常驻，一次 acquire + heartbeat + bind + commit 后停在 standby；
- 换目标时走桥接 /intent/switch：保留租约、绑定与最近动作块，宿主侧
  的按键续按不中断，只有 Motor 侧推理空窗；
- 停止时走 /intent/hold：保留租约与动作块 hold_ms，窗口内可再 switch，
  到期才松键撤销并换新租约回到 standby。

仅监听回环地址，Authorization: Bearer <intent token>。
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from player_active_gate import LiveInputLease  # noqa: E402

LOG = logging.getLogger("live-intent-runner")

DEFAULT_BGI = "http://127.0.0.1:30648"
DEFAULT_BRIDGE = "http://127.0.0.1:18311"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18312
DEFAULT_SESSION = "integration"
DEFAULT_BGI_TOKEN_FILE = r"E:\BetterAGI\tools\bettergi_token.txt"
DEFAULT_BRIDGE_TOKEN_FILE = r"E:\BetterAGI\tools\intent_token.txt"
DEFAULT_HOLD_MS = 3000
ACQUIRE_ATTEMPTS = 20
ACQUIRE_INTERVAL = 0.5
POLL_INTERVAL = 0.75
MOTOR_MODES = {"navigate", "combat", "interact", "explore", "ui", "wait", "dialogue"}
MAX_SECONDS = 600


def _read_token(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        token = handle.read().strip()
    if len(token) < 16:
        raise ValueError("token file is too short: " + path)
    return token


def _loopback(value: str, name: str) -> str:
    try:
        host = (value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0])
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError(name + " must be a loopback address")
    except ValueError as exc:
        raise ValueError("invalid " + name + ": " + str(exc)) from None
    return value.rstrip("/")


class LiveIntentRunner:
    """常驻租约持有者：standby -> active -> holding -> recovering -> standby。"""

    def __init__(self, *, bgi: str, bridge: str, session_id: str, bgi_token: str,
                 bridge_token: str, host: str, port: int, hold_ms: int) -> None:
        self.bgi = bgi
        self.bridge = bridge
        self.session_id = session_id
        self.bgi_token = bgi_token
        self.bridge_token = bridge_token
        self.host = host
        self.port = port
        self.default_hold_ms = hold_ms

        self.state = "starting"
        self.lease: Optional[LiveInputLease] = None
        self.lease_hb_stop: Optional[asyncio.Event] = None
        self.lease_hb_task: Optional[asyncio.Task] = None
        self.action_credentials: Optional[Dict[str, Any]] = None
        self.binding_identity: Optional[Dict[str, Any]] = None

        self.intent: Optional[Dict[str, Any]] = None
        self.intent_id: Optional[str] = None
        self.hold_until: Optional[float] = None
        self.hold_ms = 0
        self.last_error: Optional[str] = None
        self.bridge_state: Dict[str, Any] = {}

        self.stop_event = asyncio.Event()
        self._op_lock = asyncio.Lock()
        self._http_runner: Optional[web.AppRunner] = None

    # ---------- 基础请求 ----------
    async def _request(self, method: str, base: str, path: str, body: Any = None,
                       *, token: Optional[str] = None, timeout: float = 5.0) -> tuple:
        headers = {"Authorization": "Bearer " + (token if token is not None else self.bridge_token)}
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout, trust_env=False) as session:
                async with session.request(method, base + path, json=body, headers=headers,
                                           allow_redirects=False) as response:
                    raw = await response.content.read(262144)
                    try:
                        data = json.loads(raw) if raw else None
                    except ValueError:
                        data = None
                    return response.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return 0, None

    async def _bridge_get(self, path: str) -> tuple:
        return await self._request("GET", self.bridge, path, timeout=2.0)

    async def _bridge_post(self, path: str, body: Any, *, timeout: float = 6.0) -> tuple:
        return await self._request("POST", self.bridge, path, body, timeout=timeout)

    # ---------- 租约生命周期 ----------
    async def _heartbeat(self, lease: LiveInputLease, stop: asyncio.Event) -> None:
        async def on_failure() -> None:
            await self._recover("lease_renew_failed", expected=lease)

        try:
            await lease.heartbeat(stop, on_failure, interval=0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("租约心跳异常")

    async def _ensure_bound(self) -> bool:
        """拿一条全新租约并完成 bind/commit；失败返回 False 交给下一轮。"""
        if self.stop_event.is_set() or self.lease is not None:
            return False
        lease: Optional[LiveInputLease] = None
        for _ in range(ACQUIRE_ATTEMPTS):
            if self.stop_event.is_set():
                return False
            candidate = LiveInputLease(self.bgi, self.bgi_token, self.session_id, timeout=2.0)
            if await candidate.acquire():
                lease = candidate
                break
            await self._sleep(ACQUIRE_INTERVAL)
        if lease is None:
            self.last_error = "lease_acquire_failed"
            LOG.warning("租约获取失败；稍后重试")
            return False

        hb_stop = asyncio.Event()
        hb_task = asyncio.create_task(self._heartbeat(lease, hb_stop))
        request_id: Optional[str] = None
        try:
            request_id = await lease.publish_binding(self.bridge, self.bridge_token)
            if request_id is None:
                raise RuntimeError("lease_bind_rejected")
            if not await lease.wait_for_binding(self.bridge, self.bridge_token, request_id,
                                                hb_stop, status="prepared_bound"):
                raise RuntimeError("prepared_bound_timeout")
            if not await lease.commit_owner():
                raise RuntimeError("owner_commit_failed")
            if not await lease.commit_bridge(hb_stop):
                raise RuntimeError("bridge_commit_failed")
        except BaseException as exc:
            hb_stop.set()
            if not hb_task.done():
                hb_task.cancel()
            try:
                await lease.close()
            except Exception:
                pass
            if not isinstance(exc, asyncio.CancelledError):
                self.last_error = str(exc) or exc.__class__.__name__
                LOG.warning("租约绑定未完成：%s", self.last_error)
                await self._sleep(0.5)
                return False
            raise

        async with self._op_lock:
            if self.stop_event.is_set() or self.lease is not None:
                hb_stop.set()
                if not hb_task.done():
                    hb_task.cancel()
                try:
                    await lease.close()
                except Exception:
                    pass
                return False
            action = lease.action_binding()
            if action is None:
                hb_stop.set()
                if not hb_task.done():
                    hb_task.cancel()
                try:
                    await lease.close()
                except Exception:
                    pass
                self.last_error = "action_binding_missing"
                return False
            self.lease = lease
            self.lease_hb_stop = hb_stop
            self.lease_hb_task = hb_task
            self.action_credentials = dict(action["credentials"])
            self.binding_identity = dict(getattr(lease, "_binding_identity", None) or {})
            self.intent = None
            self.intent_id = None
            self.hold_until = None
            self.hold_ms = 0
            self.state = "standby"
            self.last_error = None
            LOG.info("租约已就绪：requestId=%s 连接=%s 代际=%s", request_id,
                     self.binding_identity.get("connectionId"),
                     self.binding_identity.get("motorConnectionGeneration"))
        return True

    async def _recover(self, reason: str, *, expected: Optional[LiveInputLease] = None) -> None:
        """撤销当前意图与租约，交给维护循环重新绑定（同租约不可二次绑定）。"""
        async with self._op_lock:
            if expected is not None and self.lease is not expected:
                return
            if self.lease is None and self.action_credentials is None:
                self.last_error = self.last_error or reason
                return
            lease = self.lease
            hb_stop = self.lease_hb_stop
            hb_task = self.lease_hb_task
            self.lease = None
            self.lease_hb_stop = None
            self.lease_hb_task = None
            self.action_credentials = None
            self.binding_identity = None
            self.intent = None
            self.intent_id = None
            self.hold_until = None
            self.hold_ms = 0
            if self.state != "stopping":
                self.state = "recovering"
            self.last_error = reason
            LOG.info("释放租约并重新绑定：%s", reason)
            if hb_stop is not None:
                hb_stop.set()
            if hb_task is not None and not hb_task.done():
                hb_task.cancel()
            if lease is not None:
                try:
                    await lease.close()
                except Exception:
                    LOG.warning("租约释放请求失败", exc_info=True)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ---------- 后台循环 ----------
    async def _maintain_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.lease is None and self.state in {"starting", "recovering", "standby"}:
                await self._ensure_bound()
            await self._sleep(0.25)

    async def _poll_loop(self) -> None:
        """轮询 /feedback：维持租约探活，并按 hold 截止兜底松键。"""
        while not self.stop_event.is_set():
            await self._sleep(POLL_INTERVAL)
            if self.stop_event.is_set():
                return
            status, data = await self._bridge_get("/feedback")
            if status != 200 or not isinstance(data, dict):
                self.bridge_state = {"reachable": False}
                continue
            binding = data.get("liveLeaseBinding")
            current = data.get("current")
            self.bridge_state = {
                "reachable": True,
                "bound": isinstance(binding, dict) and binding.get("status") == "bound",
                "status": binding.get("status") if isinstance(binding, dict) else None,
                "currentIntentId": (current or {}).get("intent_id") if isinstance(current, dict) else None,
                "motorGeneration": data.get("activeMotorGeneration"),
            }
            now = time.monotonic()
            if self.state == "holding" and self.hold_until is not None and now >= self.hold_until:
                # 兜底：桥接侧到期会自行松键；这里再补一次 cancel 并换租约。
                if isinstance(current, dict) and current:
                    await self._bridge_post("/intent", {"cancel": True}, timeout=3.0)
                await self._recover("hold_expired")
                continue
            if self.state in {"standby", "active", "holding"} and (self.lease is None or not self.lease.valid):
                await self._recover("lease_invalid")
                continue
            if self.state in {"standby", "active"} and self.lease is not None and not isinstance(binding, dict):
                await self._recover("lease_binding_missing")
                continue
            if self.state == "active" and isinstance(current, (dict, type(None))) and not current:
                self.state = "standby"
                self.intent = None
                self.intent_id = None

    # ---------- 状态视图 ----------
    def snapshot(self) -> Dict[str, Any]:
        lease = self.lease
        remaining = None
        if lease is not None:
            deadline = float(getattr(lease, "_deadline", 0.0) or 0.0)
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
        hold_remaining = None
        if self.hold_until is not None:
            hold_remaining = max(0, int((self.hold_until - time.monotonic()) * 1000))
        identity = self.binding_identity or {}
        return {
            "ok": True,
            "ready": (self.state in {"standby", "active", "holding"}
                      and lease is not None and lease.valid),
            "state": self.state,
            "intentId": self.intent_id,
            "holdRemainingMs": hold_remaining,
            "holdMs": self.hold_ms,
            "lease": (None if lease is None else {
                "requestId": identity.get("requestId"),
                "sessionId": identity.get("sessionId"),
                "instanceId": identity.get("instanceId"),
                "epoch": identity.get("epoch"),
                "connectionId": identity.get("connectionId"),
                "motorGeneration": identity.get("motorConnectionGeneration"),
                "expiresInMs": remaining,
                "phase": getattr(lease, "_phase", None),
                "valid": lease.valid,
                "active": lease.active,
            }),
            "bridge": dict(self.bridge_state),
            "lastError": self.last_error,
        }

    # ---------- HTTP ----------
    def _authorized(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        return hmac.compare_digest(auth.encode("utf-8"), ("Bearer " + self.bridge_token).encode("utf-8"))

    async def http_status(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response(self.snapshot())

    @staticmethod
    async def _json_body(request: web.Request) -> Any:
        try:
            return json.loads(await request.text())
        except (TypeError, ValueError):
            return None

    async def http_intent(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"accepted": False, "error": "unauthorized"}, status=401)
        body = await self._json_body(request)
        if not isinstance(body, dict):
            return web.json_response({"accepted": False, "error": "bad_json"}, status=400)
        goal = body.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return web.json_response({"accepted": False, "error": "bad_goal"}, status=400)
        motor_mode = body.get("motor_mode") or "navigate"
        if motor_mode not in MOTOR_MODES:
            return web.json_response({"accepted": False, "error": "bad_motor_mode"}, status=400)
        try:
            seconds = max(5, min(MAX_SECONDS, int(body.get("seconds") or 60)))
        except (TypeError, ValueError):
            return web.json_response({"accepted": False, "error": "bad_seconds"}, status=400)
        constraints = body.get("constraints") or []
        interrupts = body.get("interrupt_conditions") or []
        if not isinstance(constraints, list) or not isinstance(interrupts, list):
            return web.json_response({"accepted": False, "error": "bad_intent"}, status=400)
        lease = self.lease
        if (lease is None or not lease.valid or self.action_credentials is None
                or self.state not in {"standby", "active", "holding"}):
            return web.json_response({"accepted": False, "error": "not_ready",
                                      "state": self.state}, status=503)
        intent = {
            "intent_id": ("runner-" + uuid.uuid4().hex)[:80],
            "goal": goal.strip(),
            "reason": str(body.get("reason") or "player_goal")[:400],
            "motor_mode": motor_mode,
            "constraints": constraints,
            "interrupt_conditions": interrupts,
            "deadline_ms": (seconds + 5) * 1000,
        }
        if self.state in {"active", "holding"}:
            status, data = await self._bridge_post("/intent/switch",
                                                   {"intent": intent, "lease": dict(self.action_credentials)})
            if status == 200 and isinstance(data, dict) and data.get("accepted") is True:
                self.intent = intent
                self.intent_id = intent["intent_id"]
                self.state = "active"
                self.hold_until = None
                self.hold_ms = 0
                return web.json_response({"accepted": True, "via": "switch",
                                          "intent_id": intent["intent_id"],
                                          "motorGeneration": data.get("motorGeneration"),
                                          "executionConfirmed": False})
            error = (data or {}).get("error") if isinstance(data, dict) else None
            if status == 409:
                await self._recover("switch_rejected")
            return web.json_response({"accepted": False, "error": error or "switch_failed",
                                      "state": self.state}, status=status or 503)
        status, data = await self._bridge_post("/intent", intent)
        if status == 200 and isinstance(data, dict) and data.get("accepted") is True:
            self.intent = intent
            self.intent_id = intent["intent_id"]
            self.state = "active"
            return web.json_response({"accepted": True, "via": "intent",
                                      "intent_id": intent["intent_id"],
                                      "motorGeneration": data.get("motorGeneration"),
                                      "executionConfirmed": False})
        error = (data or {}).get("error") if isinstance(data, dict) else None
        if status in (409, 503):
            await self._recover("intent_rejected")
        return web.json_response({"accepted": False, "error": error or "intent_rejected",
                                  "state": self.state}, status=status or 503)

    async def http_stop(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        body = await self._json_body(request)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        try:
            hold_ms = int(body.get("hold_ms", self.default_hold_ms))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "bad_hold_ms"}, status=400)
        hold_ms = max(0, min(6000, hold_ms))
        if self.state == "active" and hold_ms > 0 and self.lease is not None:
            status, data = await self._bridge_post("/intent/hold", {"hold_ms": hold_ms})
            if status == 200 and isinstance(data, dict) and data.get("accepted") is True:
                self.state = "holding"
                self.hold_ms = hold_ms
                self.hold_until = time.monotonic() + hold_ms / 1000.0
                return web.json_response({"ok": True, "held": True, "holdMs": hold_ms,
                                          "intentId": self.intent_id,
                                          "executionConfirmed": False})
        await self._bridge_post("/intent", {"cancel": True}, timeout=3.0)
        await self._recover("stopped_by_agent")
        return web.json_response({"ok": True, "held": False, "state": self.state})

    async def http_shutdown(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        asyncio.get_running_loop().create_task(self._shutdown())
        return web.json_response({"ok": True, "stopping": True})

    async def _shutdown(self) -> None:
        await self._bridge_post("/intent", {"cancel": True}, timeout=3.0)
        self.state = "stopping"
        await self._recover("shutdown")
        self.stop_event.set()

    # ---------- 入口 ----------
    async def _start_http(self) -> None:
        app = web.Application()
        app.router.add_get("/status", self.http_status)
        app.router.add_post("/intent", self.http_intent)
        app.router.add_post("/stop", self.http_stop)
        app.router.add_post("/shutdown", self.http_shutdown)
        self._http_runner = web.AppRunner(app, access_log=None)
        await self._http_runner.setup()
        site = web.TCPSite(self._http_runner, self.host, self.port)
        await site.start()
        LOG.info("意图运行器监听 http://%s:%s（仅回环）", self.host, self.port)

    async def run(self) -> int:
        await self._start_http()
        tasks = [asyncio.create_task(self._maintain_loop()),
                 asyncio.create_task(self._poll_loop())]
        self.state = "recovering"
        try:
            await self.stop_event.wait()
        finally:
            self.state = "stopping"
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._recover("shutdown")
            self.state = "stopped"
            if self._http_runner is not None:
                await self._http_runner.cleanup()
        return 0


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lumine 常驻意图运行器（租约热切换 + hold 窗口）")
    parser.add_argument("--bgi", default=DEFAULT_BGI)
    parser.add_argument("--bridge", default=DEFAULT_BRIDGE)
    parser.add_argument("--session-id", default=DEFAULT_SESSION)
    parser.add_argument("--token-file", default=DEFAULT_BRIDGE_TOKEN_FILE)
    parser.add_argument("--bgi-token-file", default=DEFAULT_BGI_TOKEN_FILE)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hold-ms", type=int, default=DEFAULT_HOLD_MS)
    parser.add_argument("--log-file", default=None)
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    handlers: list = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=handlers)
    try:
        bgi = _loopback(args.bgi, "--bgi")
        bridge = _loopback(args.bridge, "--bridge")
        host = args.host
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("--host must be a loopback address")
        if not 1 <= args.port <= 65535:
            raise ValueError("invalid --port")
        token = _read_token(args.token_file)
        bgi_token = _read_token(args.bgi_token_file)
        if not isinstance(args.session_id, str) or not args.session_id.strip():
            raise ValueError("invalid --session-id")
    except (OSError, ValueError) as exc:
        print("live_intent_runner: " + str(exc), file=sys.stderr)
        return 2

    runner = LiveIntentRunner(bgi=bgi, bridge=bridge, session_id=args.session_id.strip(),
                              bgi_token=bgi_token, bridge_token=token, host=host,
                              port=args.port, hold_ms=max(0, min(6000, args.hold_ms)))

    def _stop(*_: Any) -> None:
        runner.stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    try:
        return asyncio.run(runner.run())
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print("live_intent_runner: " + str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
