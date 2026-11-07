"""Isolated gate tests: only ephemeral local HTTP servers, never game actions."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from player_active_gate import GateConfig, GateDecision, LiveInputLease, PlayerActiveGate  # noqa: E402
from player_event_log import PlayerEventLog  # noqa: E402
from player_instance import PlayerInstanceLock  # noqa: E402


def now():
    return datetime.now(timezone.utc).isoformat()


def healthy():
    timestamp = now()
    return {
        "agent": {"enabled": True, "runtimeRunning": True, "stopRequested": False,
                  "runtime": {"isAlive": True, "isForeground": True, "isMinimized": False}},
        "live": {"running": True, "sessionId": "session-1", "frameSeq": 1,
                 "lastFrameAtUtc": timestamp, "pressedKeys": [], "actionQueueDepth": 0,
                 "safety": {"capabilityVersion": 1, "strictModeActive": True,
                            "readyForV2": True, "dialogueGate": "open",
                            "canConfirmDialogueExit": True, "canAuthorizeDialogueActions": True,
                            "inputReleaseCallsReturned": True, "inputReleaseConfirmed": True,
                            "frameRecognitionHealth": "healthy", "frameSeq": 1,
                            "frameTimestampUtc": timestamp}},
        "bridge": {"protocol": "v2", "current": None, "feedback": []},
        "requests": [], "unprotected": False, "delay": 0,
    }


@asynccontextmanager
async def fake_server(state):
    async def handle(request):
        state["requests"].append((request.method, request.path, request.headers.get("Authorization")))
        if state["delay"]:
            await asyncio.sleep(state["delay"])
        expected = "Bearer bridge-secret" if request.path == "/feedback" else "Bearer bgi-secret"
        if not state["unprotected"] and request.headers.get("Authorization") != expected:
            return web.json_response({"error": "unauthorized"}, status=401)
        if state.get("redirect"):
            raise web.HTTPFound("http://192.0.2.1/should-not-be-requested")
        key = {"/api/v1/agent/status": "agent", "/api/v1/agent/live/status": "live",
               "/feedback": "bridge"}[request.path]
        return web.json_response(state[key])

    app = web.Application()
    for path in ("/api/v1/agent/status", "/api/v1/agent/live/status", "/feedback"):
        app.router.add_get(path, handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def config(url, mode="observer", **kwargs):
    values = dict(mode=mode, bettergi_url=url, bettergi_token="bgi-secret",
                  max_frame_age_seconds=1.0, poll_interval_seconds=0.05,
                  request_timeout_seconds=0.2)
    values.update(kwargs)
    return GateConfig(**values)


def run(coro):
    return asyncio.run(coro)


def create_store(home):
    (home / "state").mkdir(parents=True, exist_ok=True)
    db = PlayerEventLog(home / "state" / "cognitive_events.sqlite")
    db.close()


def test_active_gate_reports_motor_transport_and_release_separately(tmp_path):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        state["bridge"].update(motorConnectionActive=True, bettergiConnectionActive=True,
                               verifiedLiveSessionId="session-1", motorPromptProtocol="v2_requires_motor_prompt",
                               inputReleaseUnconfirmed=False)
        async with fake_server(state) as url:
            first = PlayerActiveGate(config(url, "motor_active", bridge_url=url, bridge_token="bridge-secret",
                                            player_home=tmp_path), lambda _: None)
            decision = await first.start()
            assert not decision.allowed
            assert "motor_transport_unverified" not in decision.reasons
            assert "bridge_release_unconfirmed" not in decision.reasons
            await first.close()
            state["bridge"]["verifiedLiveSessionId"] = "different"
            state["bridge"]["inputReleaseUnconfirmed"] = True
            second = PlayerActiveGate(config(url, "motor_active", bridge_url=url, bridge_token="bridge-secret",
                                             player_home=tmp_path), lambda _: None)
            decision = await second.start()
            assert {"motor_transport_unverified", "bridge_release_unconfirmed"} <= set(decision.reasons)
            await second.close()
    run(scenario())


def test_live_input_lease_keeps_owner_secret_out_of_bridge():
    async def scenario():
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        received = []
        async def handler(request):
            body = await request.json()
            received.append((request.path, body, request.headers.get("Authorization")))
            if request.path.endswith("/acquire"):
                return web.json_response({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"})
            if request.path.endswith("/renew"):
                return web.json_response({"renewed": body == owner})
            if request.path.endswith("/release"):
                return web.json_response({"released": body == owner})
            return web.json_response({"accepted": True, "requestId": body["requestId"], "status": "pending"})
        app = web.Application()
        app.router.add_post("/api/v1/agent/live/lease/acquire", handler)
        app.router.add_post("/api/v1/agent/live/lease/renew", handler)
        app.router.add_post("/api/v1/agent/live/lease/release", handler)
        app.router.add_post("/lease/bind", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        lease = LiveInputLease(origin, "local-token", "session-1")
        try:
            assert await lease.acquire()
            assert lease.action_binding() == {"sessionId": "session-1", "credentials": action}
            binding_id = await lease.publish_binding(origin, "bus-token")
            assert isinstance(binding_id, str) and len(binding_id) == 32
            assert await lease.publish_binding(origin, "bus-token") is None
            bridge_request = next(body for path, body, _ in received if path == "/lease/bind")
            assert bridge_request["credentials"] == action and owner["secret"] not in str(bridge_request)
            assert await lease.renew()
            assert await lease.close()
            assert not lease.valid and lease.action_binding() is None
            assert not await lease.acquire()
        finally:
            await runner.cleanup()
    run(scenario())


def test_live_lease_accepts_chunked_acquisition_response():
    async def scenario():
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        async def acquire(request):
            import json
            raw = json.dumps({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"}).encode()
            response = web.StreamResponse(status=200)
            await response.prepare(request)
            await response.write(raw[:15])
            await asyncio.sleep(0.02)
            await response.write(raw[15:])
            await response.write_eof()
            return response
        async def release(request):
            return web.json_response({"released": True})
        app = web.Application()
        app.router.add_post("/api/v1/agent/live/lease/acquire", acquire)
        app.router.add_post("/api/v1/agent/live/lease/release", release)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        lease = LiveInputLease(origin, "token", "session")
        try:
            assert await lease.acquire()
            assert await lease.close()
        finally:
            await runner.cleanup()
    run(scenario())


def test_live_lease_close_revokes_locally_before_inflight_renewal_finishes():
    async def scenario():
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        entered, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            if request.path.endswith("/acquire"):
                return web.json_response({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"})
            if request.path.endswith("/renew"):
                entered.set()
                await release.wait()
                return web.json_response({"renewed": True})
            return web.json_response({"released": True})
        app = web.Application()
        for path in ("acquire", "renew", "release"):
            app.router.add_post("/api/v1/agent/live/lease/" + path, handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        lease = LiveInputLease(origin, "token", "session")
        try:
            assert await lease.acquire()
            renewal = asyncio.create_task(lease.renew())
            await entered.wait()
            closing = asyncio.create_task(lease.close())
            await asyncio.sleep(0)
            assert not lease.valid and lease.action_binding() is None
            release.set()
            assert not await renewal
            assert await closing
        finally:
            release.set()
            await runner.cleanup()
    run(scenario())


def test_binding_ack_cannot_revive_lease_after_stop():
    async def scenario():
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def acquire(request):
            return web.json_response({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"})
        async def feedback(request):
            entered.set()
            await release.wait()
            return web.json_response({"protocol": "v2", "bettergiConnectionActive": True,
                "motorConnectionActive": True, "verifiedLiveSessionId": "session",
                "liveLeaseBinding": {"requestId": "c" * 32, "sessionId": "session",
                "instanceId": "a" * 32, "epoch": 1, "status": "bound"}})
        async def bind(request):
            body = await request.json()
            return web.json_response({"accepted": True, "requestId": body["requestId"], "status": "pending"})
        async def close(request):
            return web.json_response({"released": True})
        app = web.Application()
        app.router.add_post("/api/v1/agent/live/lease/acquire", acquire)
        app.router.add_post("/api/v1/agent/live/lease/release", close)
        app.router.add_post("/lease/bind", bind)
        app.router.add_get("/feedback", feedback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        lease = LiveInputLease(origin, "token", "session")
        try:
            assert await lease.acquire()
            request_id = await lease.publish_binding(origin, "bridge-token")
            assert request_id is not None
            waiter = asyncio.create_task(lease.wait_for_binding(origin, "bridge-token", request_id, stop, status="prepared_bound"))
            await entered.wait()
            stop.set()
            assert await lease.close()
            release.set()
            assert not await waiter
        finally:
            release.set()
            await runner.cleanup()
    run(scenario())


def test_fake_player_bridge_lease_binding_and_renewal_chain():
    import json
    import socket
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lumine"))
    import lumine_bridge

    class Socket:
        closed = False
        def __init__(self): self.sent = []
        async def send_str(self, value): self.sent.append(json.loads(value))

    async def scenario():
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        calls = []
        async def handle(request):
            body = await request.json()
            calls.append((request.path, body))
            if request.path.endswith("/acquire"):
                return web.json_response({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"})
            if request.path.endswith("/renew"):
                return web.json_response({"renewed": body == owner})
            return web.json_response({"released": body == owner})
        app = web.Application()
        for path in ("acquire", "renew", "release"):
            app.router.add_post("/api/v1/agent/live/lease/" + path, handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        bridge = lumine_bridge.LumineBridge(lumine_bridge.BridgeConfig(
            motor_protocol="v2", intent_port=port, intent_host="127.0.0.1", intent_token="bridge-token"))
        bridge._bettergi_ws, bridge._lumine_ws = Socket(), Socket()
        bridge._motor_connection_authenticated = True
        bridge._motor_connection_generation = 1
        bridge._motor_connected_origin = lumine_bridge._motor_origin(bridge.config.lumine_ws)
        bridge._motor_connected_endpoint = lumine_bridge._motor_endpoint_identity(bridge.config.lumine_ws, bridge.config.lumine_token)
        bridge._motor_connected_auth = lumine_bridge._motor_auth_identity(bridge.config.lumine_ws, bridge.config.lumine_token)
        await bridge._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live"}),
                                               source_ws=bridge._bettergi_ws)
        await bridge._start_intent_server()
        bridge_origin = f"http://127.0.0.1:{port}"
        lease = LiveInputLease(origin, "bgi-token", "live")
        try:
            assert await lease.acquire()
            request_id = await lease.publish_binding(bridge_origin, "bridge-token")
            assert request_id and bridge._bettergi_ws.sent[-1]["lease"] == action
            ack = {"type": "lease.bound", "leaseRequestId": request_id, "leaseInstanceId": action["instanceId"],
                   "leaseEpoch": action["epoch"], "sessionId": "live", "leaseConnectionId": "d" * 32,
                   "leaseStatus": "prepared", "leaseProbeId": request_id, "leaseExpiresInMs": 3000}
            await bridge._handle_bettergi_message(json.dumps(ack), source_ws=bridge._bettergi_ws)
            assert await lease.wait_for_binding(bridge_origin, "bridge-token", request_id, asyncio.Event(), status="prepared_bound")
            assert await lease.renew()
            assert owner["secret"] not in json.dumps(bridge.intent_snapshot())
            assert all(owner["secret"] not in json.dumps(message) for message in bridge._bettergi_ws.sent)
            assert await lease.close()
            assert any(path.endswith("/renew") and body == owner for path, body in calls)
        finally:
            await bridge._stop_intent_server()
            await runner.cleanup()
    run(scenario())


def test_live_input_lease_renewal_failure_is_terminal():
    async def scenario():
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        renewed = []
        async def handler(request):
            if request.path.endswith("/acquire"):
                return web.json_response({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"})
            renewed.append(await request.json())
            return web.json_response({"renewed": False}, status=403)
        app = web.Application()
        app.router.add_post("/api/v1/agent/live/lease/acquire", handler)
        app.router.add_post("/api/v1/agent/live/lease/renew", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        lease = LiveInputLease(origin, "local-token", "session-1")
        failures = []
        try:
            assert await lease.acquire()
            await lease.heartbeat(asyncio.Event(), lambda: failures.append("revoked"), interval=0.01)
            assert failures == ["revoked"] and renewed == [owner]
            assert not lease.valid and lease.action_binding() is None
            assert not await lease.renew() and not await lease.acquire()
        finally:
            await lease.close()
            await runner.cleanup()
    run(scenario())


def test_fake_admission_binds_and_releases_live_lease(monkeypatch):
    async def scenario():
        import time
        action = {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}
        owner = {"secret": "B" * 64, "instanceId": "a" * 32, "epoch": 1}
        received = []
        binding = {}
        async def handler(request):
            received.append(request.path)
            if request.path.endswith("/acquire"):
                return web.json_response({"credentials": action, "renewalCredentials": owner, "expiresInMs": 3000, "status": "prepared"})
            if request.path == "/lease/bind":
                body = await request.json()
                assert body["credentials"] == action
                binding["id"] = body["requestId"]
                return web.json_response({"accepted": True, "requestId": binding["id"], "status": "pending"})
            if request.path == "/api/v1/agent/live/lease/commit":
                body = await request.json()
                assert body["credentials"] == owner and body["leaseRequestId"] == binding["id"]
                return web.json_response({"sessionId": "live", "instanceId": "a" * 32, "epoch": 1,
                                          "requestId": binding["id"], "connectionId": "d" * 32,
                                          "status": "owner_committed", "expiresInMs": 3000})
            if request.path == "/lease/commit":
                body = await request.json()
                assert owner["secret"] not in str(body)
                binding["active"] = True
                return web.json_response({"accepted": True, "requestId": binding["id"], "status": "commit_pending"})
            if request.path == "/lease/abandon":
                return web.json_response({"abandoned": True, "requestId": (await request.json())["requestId"]})
            if request.path == "/feedback":
                return web.json_response({"protocol": "v2", "current": None, "bettergiConnectionActive": True,
                    "motorConnectionActive": True, "verifiedLiveSessionId": "live",
                    "liveLeaseBinding": {"requestId": binding["id"], "sessionId": "live", "instanceId": "a" * 32,
                                         "epoch": 1, "connectionId": "d" * 32, "motorConnectionGeneration": 1,
                                         "expiresInMs": 3000, "status": "bound" if binding.get("active") else "prepared_bound"}})
            return web.json_response({"released": True})
        app = web.Application()
        app.router.add_post("/api/v1/agent/live/lease/acquire", handler)
        app.router.add_post("/api/v1/agent/live/lease/commit", handler)
        app.router.add_post("/api/v1/agent/live/lease/release", handler)
        app.router.add_post("/lease/bind", handler)
        app.router.add_post("/lease/commit", handler)
        app.router.add_post("/lease/abandon", handler)
        app.router.add_get("/feedback", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        async def simulated_inspect(gate):
            gate._observed_frame_deadline = time.monotonic() + 2
            gate._motor_deadline = time.monotonic() + 2
            gate._execution_channel_deadline = time.monotonic() + 2
            if gate._input_lease is None or not gate._input_lease.active:
                return GateDecision("motor_active", False, ("action_dispatch_lease_unenforced",), "live")
            return GateDecision("motor_active", True, (), "live")
        monkeypatch.setattr(PlayerActiveGate, "_inspect", simulated_inspect)
        monkeypatch.setattr(PlayerActiveGate, "_motor_trust_file_valid", lambda self: True)
        monkeypatch.setattr(PlayerActiveGate, "_observation_chain_valid", lambda self: True)
        monkeypatch.setattr(PlayerActiveGate, "_identity_files_valid", lambda self: True)
        gate = PlayerActiveGate(config(origin, "motor_active", bridge_url=origin, bridge_token="bridge-token"), lambda _: None)
        try:
            assert (await gate.start()).allowed and gate.is_allowed
            await gate.close()
            assert not gate.is_allowed
            assert received == ["/api/v1/agent/live/lease/acquire", "/lease/bind", "/feedback",
                                "/api/v1/agent/live/lease/commit", "/lease/commit", "/feedback",
                                "/api/v1/agent/live/lease/release", "/lease/abandon"]
        finally:
            await gate.close()
            await runner.cleanup()
    run(scenario())


def test_observer_authenticates_and_revokes_on_health_change():
    async def scenario():
        state, notifications = healthy(), []
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url), notifications.append)
            result = await gate.start()
            assert result.allowed and gate.is_allowed and result.session_id == "session-1"
            assert len(state["requests"]) >= 4
            assert all(method == "GET" for method, _, _ in state["requests"])
            state["agent"]["stopRequested"] = True
            for _ in range(100):
                if notifications:
                    break
                await asyncio.sleep(0.01)
            assert not gate.is_allowed
            assert notifications[0].reasons == ("bettergi_runtime_unhealthy",)
            state["agent"]["stopRequested"] = False
            assert not (await gate.start()).allowed
            await gate.close()
            assert len(notifications) == 1
    run(scenario())


def test_close_revokes_once_and_callbacks_can_be_async():
    async def scenario():
        state, calls = healthy(), []
        async def callback(decision):
            calls.append(decision)
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url), callback)
            assert (await gate.start()).allowed
            await gate.close()
            await gate.close()
            assert [d.reasons for d in calls] == [("gate_closed",)]
            assert not gate.is_allowed
    run(scenario())


def test_expired_frame_revokes_even_when_endpoint_keeps_returning_it():
    async def scenario():
        state, calls = healthy(), []
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, max_frame_age_seconds=0.25), calls.append)
            assert (await gate.start()).allowed
            await asyncio.sleep(0.4)
            assert not gate.is_allowed and calls
            assert calls[0].reasons in (("health_lease_expired",), ("visual_frame_stale_or_unknown",))
            await gate.close()
    run(scenario())


def test_session_change_revokes_and_callback_error_is_recorded():
    async def scenario():
        state = healthy()
        def callback(_decision):
            raise RuntimeError("consumer failed")
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url), callback)
            assert (await gate.start()).allowed
            state["live"]["sessionId"] = "other"
            await asyncio.sleep(0.2)
            assert gate.decision.reasons == ("session_changed",)
            assert isinstance(gate.callback_error, RuntimeError)
            await gate.close()
    run(scenario())


@pytest.mark.parametrize("field,value,reason", [
    ("enabled", False, "bettergi_runtime_unhealthy"),
    ("runtimeRunning", False, "bettergi_runtime_unhealthy"),
    ("stopRequested", None, "bettergi_runtime_unhealthy"),
    ("runtime", None, "bettergi_runtime_unhealthy"),
])
def test_runtime_unknown_or_unhealthy_rejects(field, value, reason):
    async def scenario():
        state = healthy()
        state["agent"][field] = value
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url), lambda _: None)
            decision = await gate.start()
            assert not decision.allowed and reason in decision.reasons
            await gate.close()
    run(scenario())


@pytest.mark.parametrize("field,value,reason", [
    ("running", False, "live_session_unhealthy"),
    ("sessionId", "", "live_session_unhealthy"),
    ("frameSeq", True, "visual_frame_stale_or_unknown"),
    ("lastFrameAtUtc", "unknown", "visual_frame_stale_or_unknown"),
    ("lastFrameAtUtc", (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(), "visual_frame_stale_or_unknown"),
    ("lastFrameAtUtc", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "visual_frame_stale_or_unknown"),
])
def test_live_unknown_or_stale_rejects(field, value, reason):
    async def scenario():
        state = healthy()
        state["live"][field] = value
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url), lambda _: None)
            decision = await gate.start()
            assert not decision.allowed and reason in decision.reasons
            await gate.close()
    run(scenario())


@pytest.mark.parametrize("url", ["http://localhost:1234", "http://192.0.2.1:1234", "https://127.0.0.1:1234", "http://user@127.0.0.1:1234", "http://127.0.0.1:1234/path", "http://127.0.0.1:1234?token=x", "http://127.0.0.1", ""])
def test_non_literal_loopback_or_unsafe_origin_is_never_requested(url):
    async def scenario():
        gate = PlayerActiveGate(config(url), lambda _: None)
        decision = await gate.start()
        assert not decision.allowed and "bettergi_not_loopback_origin" in decision.reasons
    run(scenario())


@pytest.mark.parametrize("token", ["", " ", "abc\r\nX:x", None])
def test_empty_or_unsafe_token_rejects_without_request(token):
    async def scenario():
        state = healthy()
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, bettergi_token=token), lambda _: None)
            decision = await gate.start()
            assert not decision.allowed and "bettergi_token_invalid" in decision.reasons
            assert not state["requests"]
    run(scenario())


def test_wrong_token_unprotected_and_redirect_all_reject():
    async def scenario():
        state = healthy()
        async with fake_server(state) as url:
            for changes in ({"bettergi_token": "wrong"}, {}, {}):
                if not changes:
                    if not state["unprotected"]:
                        state["unprotected"] = True
                    else:
                        state["unprotected"] = False
                        state["redirect"] = True
                gate = PlayerActiveGate(config(url, **changes), lambda _: None)
                decision = await gate.start()
                assert not decision.allowed and "bettergi_status_unverified" in decision.reasons
            assert all(method == "GET" for method, _, _ in state["requests"])
    run(scenario())


@pytest.mark.parametrize("field,value,reason", [
    ("capabilityVersion", True, "strict_safety_unverified"),
    ("capabilityVersion", 2, "strict_safety_unverified"),
    ("strictModeActive", False, "strict_safety_unverified"),
    ("readyForV2", False, "strict_safety_unverified"),
    ("dialogueGate", "latched", "dialogue_gate_not_open"),
    # 对话退出证据与对话动作授权只限制 full_active：motor_active 不点对话选项，
    # 一旦识别到对话界面 Talk 闩锁会把 dialogueGate 变成 latched，由上一行拦住。
    ("canConfirmDialogueExit", False, None),
    ("canAuthorizeDialogueActions", False, None),
    ("inputReleaseCallsReturned", False, "input_release_unverified"),
    ("inputReleaseConfirmed", False, "input_release_unverified"),
    ("frameRecognitionHealth", "stale", "visual_recognition_unverified"),
    ("frameSeq", 2, "visual_recognition_unverified"),
    ("frameTimestampUtc", None, "visual_recognition_unverified"),
])
def test_active_checks_each_safety_observation(tmp_path, field, value, reason):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        state["live"]["safety"][field] = value
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, "motor_active", bridge_url=url,
                                           bridge_token="bridge-secret", player_home=tmp_path), lambda _: None)
            decision = await gate.start()
            if reason is None:
                # 这两项在 motor_active 下不构成拒绝理由，但 full_active 必须拒绝。
                full = PlayerActiveGate(config(url, "full_active", bridge_url=url,
                                               bridge_token="bridge-secret", player_home=tmp_path),
                                        lambda _: None)
                full_decision = await full.start()
                assert not full_decision.allowed
                await full.close()
            else:
                assert not decision.allowed and reason in decision.reasons
            await gate.close()
    run(scenario())


@pytest.mark.parametrize("mode", ["motor_active", "full_active"])
def test_fabricated_healthy_status_cannot_enable_active(tmp_path, mode):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, mode, bridge_url=url, bridge_token="bridge-secret",
                                           player_home=tmp_path), lambda _: None)
            decision = await gate.start()
            assert not decision.allowed and not gate.is_allowed
            assert {"action_dispatch_lease_unenforced", "motor_prompt_unverified",
                    "execution_confirmation_unavailable", "input_release_confirmation_unavailable",
                    "persistent_store_binding_unverified",
                    "single_writer_lock_unavailable"} <= set(decision.reasons)
            assert "bridge_protocol_unverified" not in decision.reasons
            assert "persistent_store_unavailable" not in decision.reasons
            if mode == "full_active":
                assert "speech_exclusive_readiness_unverified" in decision.reasons
            assert all(method == "GET" for method, _, _ in state["requests"])
            with PlayerInstanceLock(tmp_path / "state" / "player.lock"):
                pass  # denied admission must release its lock
    run(scenario())


def test_bridge_protocol_lock_and_store_fail_closed(tmp_path):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        state["bridge"]["protocol"] = "v1"
        async with fake_server(state) as url:
            with PlayerInstanceLock(tmp_path / "state" / "player.lock"):
                gate = PlayerActiveGate(config(url, "motor_active", bridge_url=url,
                                               bridge_token="bridge-secret", player_home=tmp_path), lambda _: None)
                decision = await gate.start()
                assert "single_writer_lock_unavailable" in decision.reasons
                assert "bridge_protocol_unverified" in decision.reasons
            (tmp_path / "state" / "cognitive_events.sqlite").unlink()
            gate = PlayerActiveGate(config(url, "motor_active", bridge_url=url,
                                           bridge_token="bridge-secret", player_home=tmp_path), lambda _: None)
            assert "persistent_store_unavailable" in (await gate.start()).reasons
    run(scenario())


def test_observer_forbids_intent_config_and_unknown_mode():
    async def scenario():
        state = healthy()
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, bridge_url=url), lambda _: None)
            assert "observer_intent_config_forbidden" in (await gate.start()).reasons
            gate = PlayerActiveGate(config(url, "other"), lambda _: None)
            assert (await gate.start()).reasons == ("unknown_mode",)
    run(scenario())


def test_close_before_start_is_terminal_and_does_not_request():
    async def scenario():
        state = healthy()
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url), lambda _: None)
            await gate.close()
            assert (await gate.start()).reasons == ("gate_closed",)
            assert state["requests"] == []
    run(scenario())


def test_stalled_health_check_revokes_on_original_frame_deadline():
    async def scenario():
        state, calls = healthy(), []
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, max_frame_age_seconds=0.3,
                                           request_timeout_seconds=0.8), calls.append)
            assert (await gate.start()).allowed
            state["delay"] = 0.7
            await asyncio.sleep(0.4)
            assert not gate.is_allowed and calls
            assert calls[0].reasons == ("health_lease_expired",)
            await gate.close()
    run(scenario())


def test_revocation_callback_can_close_gate():
    async def scenario():
        state, calls = healthy(), []
        async with fake_server(state) as url:
            async def callback(decision):
                calls.append(decision)
                await gate.close()
            gate = PlayerActiveGate(config(url), callback)
            assert (await gate.start()).allowed
            state["agent"]["enabled"] = False
            await asyncio.sleep(0.2)
            assert len(calls) == 1 and not gate.is_allowed
            await asyncio.wait_for(gate.close(), 0.5)
    run(scenario())


def test_cancelled_admission_releases_instance_lock(tmp_path):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        state["delay"] = 0.3
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, "motor_active", bridge_url=url,
                                           bridge_token="bridge-secret", player_home=tmp_path), lambda _: None)
            task = asyncio.create_task(gate.start())
            while not state["requests"]:
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert gate.decision.reasons == ("admission_interrupted",)
            with PlayerInstanceLock(tmp_path / "state" / "player.lock"):
                pass
    run(scenario())


    run(scenario())


def test_active_bridge_auth_and_unknown_safety_are_not_assumed(tmp_path):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        state["live"]["safety"] = None
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, "motor_active", bridge_url=url,
                                           bridge_token="wrong", player_home=tmp_path), lambda _: None)
            decision = await gate.start()
            assert {"strict_safety_unverified", "bridge_protocol_unverified"} <= set(decision.reasons)
    run(scenario())


def test_controlled_mvp_requires_confirmation_and_does_not_claim_game_release(tmp_path):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        state["live"]["safety"]["inputReleaseConfirmed"] = False
        async with fake_server(state) as url:
            settings = config(url, "motor_active", bridge_url=url, bridge_token="bridge-secret",
                              player_home=tmp_path, controlled_mvp=True)
            denied = PlayerActiveGate(settings, lambda _: None)
            result = await denied._inspect()
            assert "operator_confirmation_required" in result.reasons
            assert "input_release_unverified" in result.reasons
            acknowledged = PlayerActiveGate(settings, lambda _: None, controlled_mvp_confirmed=True)
            result = await acknowledged._inspect()
            assert "operator_confirmation_required" not in result.reasons
            assert "input_release_unverified" not in result.reasons
            assert state["live"]["safety"]["inputReleaseConfirmed"] is False
            state["live"]["pressedKeys"] = ["W"]
            assert "input_release_unverified" in (await acknowledged._inspect()).reasons
    run(scenario())


def test_controlled_mvp_talk_stays_guarded_and_session_change_revokes(tmp_path):
    async def scenario():
        create_store(tmp_path)
        state = healthy()
        async with fake_server(state) as url:
            gate = PlayerActiveGate(config(url, "motor_active", bridge_url=url,
                                           bridge_token="bridge-secret", player_home=tmp_path,
                                           controlled_mvp=True), lambda _: None,
                                    controlled_mvp_confirmed=True)
            await gate._inspect()
            gate._decision = GateDecision("motor_active", True, (), "session-1")
            state["live"]["safety"].update(dialogueGate="latched", readyForV2=False,
                                           inputReleaseConfirmed=False)
            result = await gate._inspect()
            assert "dialogue_gate_not_open" not in result.reasons
            assert "strict_safety_unverified" not in result.reasons
            assert "input_release_unverified" not in result.reasons
            assert not gate.can_publish_motor
            state["live"]["sessionId"] = "session-2"
            assert "operator_confirmation_session_changed" in (await gate._inspect()).reasons
            state["agent"]["runtime"]["isForeground"] = False
            assert "bettergi_runtime_unhealthy" in (await gate._inspect()).reasons
    run(scenario())


def test_controlled_confirmation_rejects_piped_input(monkeypatch):
    import io
    import run_shadow
    monkeypatch.setattr(sys, "stdin", io.StringIO("我已确认\n"))
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert not run_shadow._confirm_controlled_mvp()


def test_controlled_confirmation_requires_exact_interactive_answer(monkeypatch):
    import io
    import run_shadow
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(sys, "stderr", Terminal())
    monkeypatch.setattr("builtins.input", lambda _: "我已确认")
    assert run_shadow._confirm_controlled_mvp()
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert not run_shadow._confirm_controlled_mvp()
