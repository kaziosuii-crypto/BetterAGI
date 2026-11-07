#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_shadow 启动开关与错误路径测试（不连接真实 WS）。"""

from __future__ import annotations

import asyncio
import ast
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import fake_live_hub  # noqa: E402
import run_shadow  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """避免开发机的灰度/回滚环境变量让测试结果漂移。"""
    for name in list(os.environ):
        if name.startswith("PLAYER_") or name.startswith("BETTERGI_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CONTEXT_BUILDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)


class FakeMessage:
    def __init__(self, payload):
        self.type = run_shadow.aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class FakeWS:
    """只实现异步迭代；任何 send 调用都会因属性不存在而失败。"""

    def __init__(self, payloads):
        self.payloads = payloads

    async def __aiter__(self):
        for payload in self.payloads:
            yield FakeMessage(payload)


class RecordingAgent:
    def __init__(self):
        self.seen = []

    def observe(self, payload):
        self.seen.append(payload)


def test_receive_loop_is_receive_only_and_stops_on_live_stopped() -> None:
    agent = RecordingAgent()
    event = asyncio.Event()
    ws = FakeWS(
        [
            {"type": "hello", "sessionId": "s"},
            {"type": "frame", "seq": 1, "jpeg": "x"},
            {"type": "live.stopped"},
        ]
    )
    reason = asyncio.run(run_shadow._receive_loop(ws, agent, event))
    assert reason == "live.stopped"
    assert event.is_set()
    assert [item["type"] for item in agent.seen] == ["hello", "frame", "live.stopped"]
    # FakeWS 没有 send_str/send_json，接收循环若尝试发送会直接失败。
    assert not hasattr(ws, "send_str")


def run_async_main(argv):
    args = run_shadow.build_parser().parse_args(argv)
    return asyncio.run(run_shadow.async_main(args))


def test_reflection_backoff_limits_retries_without_blocking_other_targets():
    now = [100.0]
    backoff = run_shadow._ReflectionBackoff(clock=lambda: now[0])
    first = {"session_id": "p:s", "conversation_event_id": "first"}
    other = {"session_id": "p:s", "conversation_event_id": "other"}
    assert backoff.ready(first)
    backoff.mark(first, success=False)
    assert not backoff.ready(first) and backoff.ready(other)
    now[0] += 5
    assert backoff.ready(first)
    backoff.mark(first, success=False)
    now[0] += 9
    assert not backoff.ready(first)
    now[0] += 1
    assert backoff.ready(first)
    backoff.mark(first, success=True)
    assert backoff.ready(first)


def test_reflection_recovery_rotates_past_failing_old_entries():
    from types import SimpleNamespace

    class Store:
        def pending_conversation_page(self, player_id, *, after_seq, limit, player_scope):
            end = min(after_seq + limit, 40)
            return {"items": [{"session_id": "p:session", "conversation_event_id": f"conv-{i}"}
                              for i in range(after_seq, end)], "next_after_seq": end if end < 40 else None}

    async def scenario():
        stop = asyncio.Event()
        seen = []
        class Scheduler:
            pending_count = 0
            def contains(self, kind, key): return False
            def submit(self, event):
                assert event.critical is False
                seen.append(event.payload["target"]["conversation_event_id"])
                if "conv-39" in seen:
                    stop.set()
                return SimpleNamespace(status=run_shadow.SubmissionStatus.ACCEPTED)
        agent = SimpleNamespace(experience_store=Store(), profile=SimpleNamespace(player_id="p"),
                                cognitive_transaction=None, _session_generation=0)
        await asyncio.wait_for(run_shadow._recover_reflections_loop(agent, Scheduler(), stop, interval=0.001), timeout=2)
        assert seen == [f"conv-{i}" for i in range(40)]
    asyncio.run(scenario())


def test_player_websocket_uses_header_and_removes_old_query_token(tmp_path: Path) -> None:
    from test_player_agent import make_agent

    async def scenario():
        seen = []
        async def handler(request):
            seen.append((request.query_string, request.headers.get("Authorization")))
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json({"type": "hello", "sessionId": "test"})
            await socket.send_json({"type": "live.stopped"})
            await socket.close()
            return socket
        app = web.Application()
        app.router.add_get("/ws", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        agent, *_ = make_agent(tmp_path)
        config = run_shadow.resolve_config(home=tmp_path, env={}, no_llm=True,
            ws_url=f"ws://127.0.0.1:{port}/ws?access_token=old&x=1", token="new-token")
        try:
            await run_shadow._run_connection(config, agent, asyncio.Event())
        finally:
            agent.close()
            await runner.cleanup()
        assert seen == [("x=1", "Bearer new-token")]
    asyncio.run(scenario())


def test_disabled_returns_zero_without_touching_profile(tmp_path: Path) -> None:
    code = run_async_main(["--disabled", "--no-llm", "--home", str(tmp_path)])
    assert code == 0
    assert not (tmp_path / "profile").exists()


def test_shadow_zero_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYER_AGENT_SHADOW", "0")
    monkeypatch.delenv("PLAYER_AGENT_ENABLED", raising=False)
    code = run_async_main(
        ["--no-llm", "--home", str(tmp_path), "--session-id", "test"]
    )
    assert code == 2


def test_observer_rejects_explicit_and_environment_intent_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    assert run_async_main([
        "--home", str(home), "--no-llm", "--intent-url", "http://127.0.0.1:9000"
    ]) == 2
    assert not home.exists()
    monkeypatch.setenv("PLAYER_INTENT_URL", "http://127.0.0.1:9000")
    assert run_async_main(["--home", str(home), "--no-llm"]) == 2
    assert not home.exists()


@pytest.mark.parametrize("mode", ["motor_active", "full_active"])
def test_active_modes_fail_closed_until_safety_gates_exist(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("未授权的主动模式不得进行网络请求")

    async def preflight(args, config, detector, gate, stop_event, runtime):
        assert not gate.is_allowed and gate._input_lease is None
        assert runtime["identity"] is not None
        return 2

    monkeypatch.setattr(run_shadow, "_run_configured", preflight)
    monkeypatch.setattr(run_shadow.aiohttp.ClientSession, "_request", unexpected)
    home = tmp_path / "home"
    assert run_async_main(["--home", str(home), "--no-llm", "--mode", mode]) == 2
    with run_shadow.PlayerInstanceLock(home / "state" / "player.lock"):
        pass


def test_missing_custom_profile_is_an_error(tmp_path: Path) -> None:
    custom = tmp_path / "custom" / "missing.json"
    code = run_async_main(
        [
            "--no-llm",
            "--home",
            str(tmp_path / "home"),
            "--profile",
            str(custom),
            "--session-id",
            "test",
        ]
    )
    assert code == 2
    assert not custom.exists()


def test_safe_error_masks_raw_and_urlencoded_token() -> None:
    token = "abc+def/=="
    encoded = "abc%2Bdef%2F%3D%3D"
    text = run_shadow._safe_error(
        RuntimeError("connect ws://x/ws?access_token=" + encoded), token
    )
    assert token not in text
    assert encoded not in text
    assert "***" in text


def test_default_profile_is_seeded_before_ws(tmp_path: Path) -> None:
    # 用不可达地址让连接失败；关键断言是档案已在连接前创建。
    code = run_async_main(
        [
            "--no-llm",
            "--home",
            str(tmp_path / "home"),
            "--ws-url",
            "ws://127.0.0.1:1/ws",
            "--duration",
            "0.2",
            "--session-id",
            "test",
        ]
    )
    assert code == 0
    assert (tmp_path / "home" / "profile" / "profile.json").exists()
    assert (tmp_path / "home" / "state" / "resume_state.json").exists()


def test_local_embedding_is_wired_to_sqlite_experience_retrieval(tmp_path: Path, monkeypatch) -> None:
    import player_embedding

    class Adapter:
        def __init__(self, directory):
            self.directory = directory
        def embed_query(self, text): return [1.0, 0.0]
        def embed_document(self, source, text): return [1.0, 0.0]

    constructed = []
    real_agent = run_shadow.PersistentPlayerAgent
    def make_agent(*args, **kwargs):
        constructed.append(kwargs)
        return real_agent(*args, **kwargs)

    monkeypatch.setattr(player_embedding, "LocalSentenceTransformerAdapter", Adapter)
    monkeypatch.setattr(run_shadow, "PersistentPlayerAgent", make_agent)
    assert run_async_main(["--no-llm", "--home", str(tmp_path / "home"),
                           "--ws-url", "ws://127.0.0.1:1/ws", "--duration", "0.2",
                           "--embedding-model", str(tmp_path)]) == 0
    config = constructed[0]["experience_embedding"]
    assert isinstance(config.adapter, Adapter) and config.adapter.directory == str(tmp_path)
    assert constructed[0]["experience_store"] is not None
    assert constructed[0]["cognitive_transaction"] is not None


@pytest.mark.parametrize("stale", [False, True])
def test_reflection_update_recovers_after_process_restart(tmp_path: Path, stale: bool) -> None:
    import hashlib
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_reflection import reflect_conversation_async

    class LLM:
        async def complete(self, system, user, **kwargs):
            source = json.loads(user.split("UNTRUSTED_OBSERVATIONS_JSON=", 1)[1])["observations"][0]
            return json.dumps({"status": "ok", "items": [{"kind": "emotion_proposal", "text": "我有些好奇",
                "uncertainty": "OCR 可能有误", "source_event_ids": [source["event_id"]],
                "update": {"curiosity": 0.8}}]}, ensure_ascii=False)

    path = tmp_path / "events.sqlite"
    scope = "player-" + hashlib.sha256(b"lumi").hexdigest()
    session = "lumi:old"
    with PlayerEventLog(path) as log:
        tx, memory = PlayerCognitiveTransaction(log), PlayerExperience(log)
        tx.bind_session(session_id=session, player_scope=scope, event_id="bind")
        memory.record_observation(session_id=session, event_id="line", timestamp="t", source_id="frame",
                                  channel="dialogue", text="发现遗迹", visibility="player", conversation_id="c")
        memory.end_conversation(session_id=session, event_id="conv", timestamp="t", conversation_id="c",
                                source_id="frame-end", incomplete=True)
        revision = tx.read(session, player_scope=scope)["revision"]
        result = asyncio.run(reflect_conversation_async(session_id=session, conversation_event_id="conv",
            experience=memory, llm=LLM(), timestamp="t", cognitive_revision=revision))
        assert result["status"] == "recorded"
        if stale:
            tx.commit(session_id=session, player_scope=scope, source_event_id="line", event_id="later-turn",
                      thought="新想法", intention=None, speech=None, updates={"emotion": {"fear": 0.8}})
    with PlayerEventLog(path) as log:
        tx = PlayerCognitiveTransaction(log)
        run_shadow._recover_pending_reflection_updates(log, tx, "lumi", scope, "")
        state = tx.read(session, player_scope=scope)["state"]
        assert state["emotion"]["curiosity"] == (0 if stale else 0.35)
        run_shadow._recover_pending_reflection_updates(log, tx, "lumi", scope, "")
        applied = [event for event in log.list_events(session) if event["type"] == "cognitive.reflection_applied"]
        assert len(applied) == (0 if stale else 1)


def test_conversation_recovery_does_not_claim_other_scope(tmp_path: Path) -> None:
    from player_cognitive_transaction import PlayerCognitiveTransaction
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience

    with PlayerEventLog(tmp_path / "events.sqlite") as log:
        memory, tx = PlayerExperience(log), PlayerCognitiveTransaction(log)
        tx.bind_session(session_id="p:own", player_scope="player-p", event_id="bind-own")
        tx.bind_session(session_id="p:other", player_scope="player-other", event_id="bind-other")
        for session, name in (("p:own", "own"), ("p:other", "other")):
            memory.record_observation(session_id=session, event_id="line-" + name, timestamp="t",
                source_id="frame:one", channel="dialogue", text="台词", visibility="player", conversation_id="c")
            memory.stage_conversation_end(session_id=session, event_id="conv-" + name, timestamp="t",
                                          conversation_id="c", anchor_event_id="line-" + name)
        run_shadow._recover_pending_conversation_ends(memory, "p", "", player_scope="player-p")
        assert log.get_event("p:own", "conv-own") is not None
        assert log.get_event("p:other", "conv-other") is None


def test_fake_hub_matches_real_payload_contract() -> None:
    frame = fake_live_hub.frame_payload(1)
    assert frame["uiCategory"] == "Unknown"
    assert frame["isInTalkUi"] is None
    assert frame["capture"]["width"] == 640
    assert frame["runtime"]["isMinimized"] is False
    assert frame["jpeg"] == fake_live_hub.JPEG

    action = fake_live_hub.action_payload(2)
    assert action["block"]["chunks"][0]["keys"] == ["W"]
    assert action["block"]["thought"] == "向前走"
    assert "keys" not in action  # 真实回显不在顶层


def test_player_modules_have_no_outbound_game_calls() -> None:
    """静态检查：player 代码不得出现发送动作/指导/解说的调用。"""
    forbidden_attrs = {
        "send_json",
        "send_str",
        "send_bytes",
        "send_frame",
        "stop_actions",
    }
    forbidden_http = {"post", "put", "patch", "delete"}
    http_allowlist = {"player_intent.py", "player_vision.py", "player_active_gate.py"}
    package = Path(__file__).resolve().parents[1]
    for path in package.glob("player_*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name in {"player_intent.py", "player_vision.py"}:
            # 意图和观察模块不能访问游戏写端点。
            assert "/api/v1/agent/live/" not in text
            assert "/api/v1/agent/runtime/" not in text
            assert "stop_actions" not in text
        elif path.name == "player_active_gate.py":
            endpoints = {node.value for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Constant)
                         and isinstance(node.value, str) and node.value.startswith("/api/v1/agent/")}
            assert endpoints <= {"/api/v1/agent/status", "/api/v1/agent/live/status",
                                 "/api/v1/agent/live/lease/acquire", "/api/v1/agent/live/lease/renew",
                                 "/api/v1/agent/live/lease/commit", "/api/v1/agent/live/lease/release"}
            assert "stop_actions" not in text
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            assert name not in forbidden_attrs, "{0} 调用了 {1}".format(
                path.name, name
            )
            if path.name not in http_allowlist:
                assert name not in forbidden_http, "{0} 调用了 HTTP {1}".format(
                    path.name, name
                )
    for path in (package / "run_shadow.py", package / "shadow_recorder.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            assert node.func.attr not in forbidden_attrs, "{0} 调用了 {1}".format(
                path.name, node.func.attr
            )
            assert node.func.attr not in forbidden_http, "{0} 调用了 HTTP {1}".format(
                path.name, node.func.attr
            )


def test_stop_file_ends_shadow_gracefully(tmp_path: Path) -> None:
    stop_file = tmp_path / "stop"
    stop_file.write_text("stop", encoding="utf-8")
    event = asyncio.Event()

    async def scenario():
        await run_shadow._watch_stop_file(stop_file, event)

    asyncio.run(scenario())
    assert event.is_set()


def test_shadow_end_to_end_with_local_hub(tmp_path: Path) -> None:
    fake_live_hub.RECEIVED.clear()

    async def scenario() -> int:
        app = fake_live_hub.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            args = run_shadow.build_parser().parse_args(
                [
                    "--no-llm",
                    "--home",
                    str(tmp_path / "home"),
                    "--ws-url",
                    "ws://127.0.0.1:{0}/ws/v1/agent/live".format(port),
                    "--interval",
                    "1",
                    "--session-id",
                    "e2e",
                    "--duration",
                    "15",
                ]
            )
            return await run_shadow.async_main(args)
        finally:
            await runner.cleanup()

    code = asyncio.run(scenario())
    assert code == 0
    # 服务端没有收到任何应用层消息（actions/guidance/commentary/stop_actions）。
    assert fake_live_hub.RECEIVED == []

    shadow_path = tmp_path / "home" / "state" / "shadow" / "e2e.jsonl"
    records = [
        json.loads(line)
        for line in shadow_path.read_text(encoding="utf-8").splitlines()
    ]
    types = [record["type"] for record in records]
    assert types[0] == "shadow_start"
    assert types[-1] == "shadow_end"
    assert "shadow_live_stopped" in types
    assert "shadow_observation" in types
    assert all(record["sent"] is False for record in records)
    assert (tmp_path / "home" / "state" / "resume_state.json").exists()
