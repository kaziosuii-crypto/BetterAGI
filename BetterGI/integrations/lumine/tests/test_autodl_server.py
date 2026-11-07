#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autodl_server 的单元测试。

aiohttp 未安装时，纯函数测试仍会运行；WebSocket 集成测试自动跳过。
"""

import asyncio
import base64
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import autodl_server as srv  # noqa: E402
import lumine_protocol as lp  # noqa: E402

try:
    import aiohttp
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:  # 本地尚未安装 aiohttp 时跳过集成测试
    aiohttp = None
    TestClient = None
    TestServer = None
    HAS_AIOHTTP = False

# 最小 JPEG 结构：SOI + APP0 标记 + 填充 + EOI。服务器只校验 JPEG magic。
TINY_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
TINY_JPEG_B64 = base64.b64encode(TINY_JPEG).decode("ascii")


class MockActionTests(unittest.TestCase):
    """Mock 脚本动作。"""

    def test_all_phases_parse(self):
        names = set()
        for seq in range(srv.MOCK_CYCLE_FRAMES * 2):
            parsed = lp.parse_action(srv.mock_response_text(seq))
            self.assertEqual(len(parsed["chunks"]), 6)
            for chunk in parsed["chunks"]:
                self.assertLessEqual(len(chunk["keys"]), 4)
            names.add(srv.mock_phase(seq)["name"])
        self.assertEqual(
            names,
            {"walk", "camera_right", "camera_left", "jump", "attack", "absolute", "idle"},
        )

    def test_deterministic_by_seq(self):
        for seq in (0, 7, 18, 32):
            self.assertEqual(
                srv.mock_response_text(seq),
                srv.mock_response_text(seq + srv.MOCK_CYCLE_FRAMES),
            )

    def test_absolute_phase(self):
        # walk 10 + right 6 + left 6 + jump 5 + attack 5 = 32
        parsed = lp.parse_action(srv.mock_response_text(32))
        self.assertEqual(parsed["absolute"], {"x": 640, "y": 360})
        self.assertEqual(parsed["chunks"][0]["keys"], ["LB"])

    def test_thought_is_included(self):
        text = srv.mock_response_text(0)
        self.assertIn("<|thought_start|>", text)
        self.assertEqual(lp.parse_action(text)["thought"], "Walk forward along the path.")


class FrameValidationTests(unittest.TestCase):
    """JPEG base64 校验。"""

    def test_valid_jpeg(self):
        self.assertEqual(srv.decode_jpeg_base64(TINY_JPEG_B64), TINY_JPEG)

    def test_data_url(self):
        url = "data:image/jpeg;base64," + TINY_JPEG_B64
        self.assertEqual(srv.decode_jpeg_base64(url), TINY_JPEG)

    def test_whitespace_is_tolerated(self):
        spaced = TINY_JPEG_B64[:8] + "\n" + TINY_JPEG_B64[8:]
        self.assertEqual(srv.decode_jpeg_base64(spaced), TINY_JPEG)

    def test_invalid_base64(self):
        for value in ("not base64!!", "====", "", None, 123):
            with self.subTest(value=value), self.assertRaises(srv.FrameError):
                srv.normalize_jpeg_base64(value)

    def test_not_jpeg(self):
        payload = base64.b64encode(b"hello world").decode("ascii")
        with self.assertRaises(srv.FrameError):
            srv.normalize_jpeg_base64(payload)

    def test_oversize(self):
        with self.assertRaises(srv.FrameError):
            srv.normalize_jpeg_base64("A" * (srv.MAX_FRAME_BASE64_CHARS + 1))


class ConfigTests(unittest.TestCase):
    """CLI 与环境变量。"""

    def test_env_defaults(self):
        env = {
            "LUMINE_SERVER_TOKEN": "env-token",
            "LUMINE_MOCK": "1",
            "LUMINE_PORT": "9999",
            "LUMINE_MODEL": "env-model",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = srv.parse_args([])
        self.assertEqual(config.token, "env-token")
        self.assertTrue(config.mock)
        self.assertEqual(config.port, 9999)
        self.assertEqual(config.model, "env-model")

    def test_cli_overrides_env(self):
        with mock.patch.dict(os.environ, {"LUMINE_SERVER_TOKEN": "env-token"}, clear=True):
            config = srv.parse_args(
                [
                    "--mock",
                    "--port",
                    "9000",
                    "--token",
                    "cli-token",
                    "--vllm-url",
                    "http://example:1234",
                    "--max-tokens",
                    "64",
                ]
            )
        self.assertTrue(config.mock)
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.token, "cli-token")
        self.assertEqual(config.vllm_url, "http://example:1234")
        self.assertEqual(config.max_tokens, 64)

    def test_invalid_values(self):
        for argv in (
            ["--port", "0"],
            ["--max-tokens", "0"],
            ["--temperature", "-1"],
            ["--top-p", "0"],
            ["--top-p", "1.5"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    srv.parse_args(argv)

    def test_chat_url_normalization(self):
        for value in ("http://127.0.0.1:8000", "http://127.0.0.1:8000/"):
            config = srv.ServerConfig(vllm_url=value)
            self.assertEqual(
                config.chat_completions_url(),
                "http://127.0.0.1:8000/v1/chat/completions",
            )
        config = srv.ServerConfig(vllm_url="http://h:1/v1")
        self.assertEqual(config.chat_completions_url(), "http://h:1/v1/chat/completions")
        config = srv.ServerConfig(vllm_url="http://h:1/v1/chat/completions")
        self.assertEqual(config.chat_completions_url(), "http://h:1/v1/chat/completions")


class ExtractContentTests(unittest.TestCase):
    """vLLM 响应文本提取。"""

    def test_string_content(self):
        data = {"choices": [{"message": {"content": "hello"}}]}
        self.assertEqual(srv._extract_message_content(data), "hello")

    def test_list_content(self):
        data = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "a"},
                            {"type": "text", "text": "b"},
                        ]
                    }
                }
            ]
        }
        self.assertEqual(srv._extract_message_content(data), "ab")

    def test_invalid_responses(self):
        for data in ({}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}, None):
            with self.subTest(data=data), self.assertRaises(srv.InferenceError):
                srv._extract_message_content(data)


class _FakeResponse:
    """模拟 aiohttp 响应的异步上下文管理器。"""

    def __init__(self, status=200, body=""):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


class _FakeHTTP:
    """记录请求并返回固定响应的 HTTP 客户端替身。"""

    closed = False

    def __init__(self, status=200, body=""):
        self.status = status
        self.body = body
        self.url = None
        self.payload = None

    def post(self, url, json=None):
        self.url = url
        self.payload = json
        return _FakeResponse(self.status, self.body)


class InferencePayloadTests(unittest.IsolatedAsyncioTestCase):
    """不依赖 aiohttp 的 vLLM 请求体与响应解析测试。"""

    async def test_chat_completions_payload(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<|action_start|>5 0 0 ; W ; W ; W ; W ; W ; W"
                                "<|action_end|>"
                            )
                        }
                    }
                ]
            }
        )
        server = srv.LumineServer(
            srv.ServerConfig(
                mock=False, model="m", temperature=0.3, max_tokens=64, protocol="text"
            )
        )
        server.http = _FakeHTTP(200, body)

        parsed = await server.infer(TINY_JPEG_B64, "go", 3)

        self.assertEqual(
            server.http.url, "http://127.0.0.1:8000/v1/chat/completions"
        )
        payload = server.http.payload
        self.assertEqual(payload["model"], "m")
        self.assertEqual(payload["max_tokens"], 64)
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["top_p"], 0.95)
        self.assertFalse(payload["stream"])
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        content = payload["messages"][-1]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "image_url"])
        self.assertIn("data:image/jpeg;base64,", content[1]["image_url"]["url"])

        self.assertEqual(parsed["chunks"][0]["dx"], 5)
        for chunk in parsed["chunks"][1:]:
            self.assertEqual((chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0))

    async def test_motor_mode_injects_intent_not_best_action_prompt(self):
        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": "<|action_start|>5 0 0 ; W ; W ; W ; W ; W ; W<|action_end|>"}}
                ]
            }
        )
        server = srv.LumineServer(srv.ServerConfig(mock=False, prompt_mode="motor", protocol="text"))
        server.http = _FakeHTTP(200, body)
        intent = {"intent_id": "i-1", "goal": "跟随安柏", "motor_mode": "navigate"}
        await server.infer(TINY_JPEG_B64, "保持距离", 1, intent)
        messages = server.http.payload["messages"]
        self.assertIn("MotorController", messages[0]["content"])
        self.assertNotIn("expert Genshin Impact player", messages[0]["content"])
        user_content = server.http.payload["messages"][1]["content"]
        self.assertEqual(user_content[0]["type"], "text")
        self.assertIn("跟随安柏", user_content[0]["text"])

    async def test_default_pretrain_forces_motor_when_intent_present(self):
        body = json.dumps({"choices": [{"message": {"content":
            "<|action_start|>0 0 0 ; ; ; ; ; ;<|action_end|>"}}]})
        server = srv.LumineServer(srv.ServerConfig(mock=False, protocol="text"))
        self.assertEqual(server.config.prompt_mode, "pretrain")
        server.http = _FakeHTTP(200, body)
        await server.infer(TINY_JPEG_B64, "", 1, {"intent_id": "i", "goal": "go"})
        self.assertIn("MotorController", server.http.payload["messages"][0]["content"])
        self.assertIn("go", server.http.payload["messages"][1]["content"][0]["text"])

    async def test_session_intent_lifecycle(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True))
        ws = _FakeWebSocket()
        session = srv.PlayerSession("s", ws, server)
        self.assertIsNone(session.set_intent({"intent_id": "i-1"}))
        intent = session.set_intent({"intent_id": "i-1", "goal": "前进"})
        self.assertEqual(intent["intent_id"], "i-1")
        self.assertEqual(session.snapshot()["intentId"], "i-1")
        session.reset()
        self.assertIsNone(session.intent)
        self.assertEqual(session.snapshot()["intentId"], "")

    async def test_vllm_http_error(self):
        server = srv.LumineServer(srv.ServerConfig(mock=False, protocol="text"))
        server.http = _FakeHTTP(500, "boom")
        with self.assertRaises(srv.InferenceError) as context:
            await server.infer(TINY_JPEG_B64, "", 1)
        self.assertEqual(context.exception.code, "vllm_http_error")

    async def test_unparsable_model_output(self):
        body = json.dumps({"choices": [{"message": {"content": "no action here"}}]})
        server = srv.LumineServer(srv.ServerConfig(mock=False, protocol="text"))
        server.http = _FakeHTTP(200, body)
        with self.assertRaises(srv.InferenceError) as context:
            await server.infer(TINY_JPEG_B64, "", 1)
        self.assertEqual(context.exception.code, "bad_action")


class _FakeWebSocket:
    """记录 send_json 输出的最小 WebSocket 替身。"""

    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class SessionWorkerTests(unittest.IsolatedAsyncioTestCase):
    """不依赖 aiohttp 的会话工作线程测试（mock 推理）。"""

    async def test_v2_without_intent_never_infers_or_publishes(self):
        server = srv.LumineServer(srv.ServerConfig(mock=False, token="secret"))
        server.infer = mock.AsyncMock()
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        session.protocol_v2 = True
        try:
            session.enqueue_frame(1, TINY_JPEG_B64, None)
            await asyncio.wait_for(session.worker, 3)
            server.infer.assert_not_awaited()
            self.assertFalse(any(m.get("type") == "action" for m in ws.messages))
        finally:
            await session.close()

    async def test_capability_requires_v2_nonmock_and_authentication(self):
        query = {"type": "motor_capability_query", "nonce": "a" * 32, "connectionGeneration": 3}
        for protocol_v2, is_mock, token, expected in (
            (True, False, "secret", True), (False, False, "secret", False),
            (True, True, "secret", False), (True, False, "", False),
        ):
            with self.subTest(protocol_v2=protocol_v2, is_mock=is_mock, token=bool(token)):
                server = srv.LumineServer(srv.ServerConfig(mock=is_mock, token=token))
                ws = _FakeWebSocket()
                session = srv.PlayerSession("test", ws, server)
                session.protocol_v2 = protocol_v2
                await srv._handle_player_message(session, json.dumps(query))
                self.assertEqual(ws.messages[-1]["type"], "motor_capability_ack" if expected else "error")
                if expected:
                    self.assertEqual(ws.messages[-1], {
                        "type": "motor_capability_ack", "nonce": "a" * 32, "connectionGeneration": 3,
                        "protocol": "lumine-live-v2", "intentRequired": True,
                        "intentForcesMotor": True, "mock": False,
                    })
                await session.close()

    async def test_capability_rejects_malformed_challenge(self):
        server = srv.LumineServer(srv.ServerConfig(mock=False, token="secret"))
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        session.protocol_v2 = True
        for nonce, generation in (("bad", 1), ("a" * 32, True), ("a" * 32, -1)):
            await srv._handle_player_message(session, json.dumps(
                {"type": "motor_capability_query", "nonce": nonce, "connectionGeneration": generation}))
            self.assertEqual(ws.messages[-1]["code"], "bad_motor_capability_query")
        await session.close()

    async def test_latest_frame_wins_and_drops_stale_frames(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True, mock_latency=0.02))
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        try:
            session.enqueue_frame(1, TINY_JPEG_B64, None)
            # 让工作线程先取走第 1 帧，后续帧才会在推理期间覆盖同一个槽位。
            await asyncio.sleep(0)
            for seq in range(2, 21):
                session.enqueue_frame(seq, TINY_JPEG_B64, None)
            worker = session.worker
            self.assertIsNotNone(worker)
            await asyncio.wait_for(worker, timeout=3.0)

            actions = [m for m in ws.messages if m["type"] == "action"]
            self.assertEqual(actions[0]["forFrameSeq"], 1)
            self.assertEqual(actions[-1]["forFrameSeq"], 20)
            self.assertEqual(session.latest_seq, 20)
            self.assertGreaterEqual(session.dropped, 1)
            self.assertEqual(session.received_frames, 20)

            chunks = actions[-1]["chunks"]
            self.assertEqual(len(chunks), 6)
            for chunk in chunks[1:]:
                self.assertEqual(
                    (chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0)
                )
            self.assertFalse(session.inflight)
        finally:
            await session.close()

    async def test_reset_discards_inflight_result(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True, mock_latency=0.1))
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        try:
            session.enqueue_frame(7, TINY_JPEG_B64, None)
            worker = session.worker
            await asyncio.sleep(0.01)
            session.reset()
            await asyncio.wait_for(worker, timeout=3.0)

            self.assertEqual(session.actions, 0)
            self.assertFalse(
                any(message["type"] == "action" for message in ws.messages)
            )
            self.assertEqual(session.latest_seq, 0)
            self.assertIsNone(session.pending)
        finally:
            await session.close()

    async def test_v2_replacement_and_cancel_discard_inflight(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True))
        started = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def infer(jpeg, guidance, seq, intent):
            seen.append((seq, intent["goal"]))
            if seq == 1:
                started.set()
                await release.wait()
            return lp.parse_action(srv.mock_response_text(seq))

        server.infer = infer
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        try:
            await srv._handle_player_message(session, json.dumps({"type": "hello", "protocol": "lumine-live-v2"}))
            await srv._handle_player_message(session, json.dumps({"type": "intent", "intent": {"intent_id": "i", "goal": "old"}, "intentId": "i", "motorGeneration": 1}))
            session.enqueue_frame(1, TINY_JPEG_B64, None)
            await started.wait()
            await srv._handle_player_message(session, json.dumps({"type": "intent", "intent": {"intent_id": "i", "goal": "new"}, "intentId": "i", "motorGeneration": 2, "actionGroupId": "g"}))
            session.enqueue_frame(2, TINY_JPEG_B64, None)
            release.set()
            await asyncio.wait_for(session.worker, 3)
            actions = [x for x in ws.messages if x["type"] == "action"]
            self.assertEqual(seen, [(1, "old"), (2, "new")])
            self.assertEqual(len(actions), 1)
            self.assertEqual((actions[0]["intentId"], actions[0]["motorGeneration"], actions[0]["actionGroupId"]), ("i", 2, "g"))
            await srv._handle_player_message(session, json.dumps({"type": "cancel_intent", "motorGeneration": 3}))
            session.enqueue_frame(3, TINY_JPEG_B64, None)
            await asyncio.wait_for(session.worker, 3)
            self.assertEqual(len([x for x in ws.messages if x["type"] == "action"]), 1)
            await srv._handle_player_message(session, json.dumps({"type": "intent", "intent": {"intent_id": "i", "goal": "stale"}, "intentId": "i", "motorGeneration": 2}))
            self.assertIsNone(session.intent)
        finally:
            await session.close()

    async def test_v2_rejects_mismatched_nested_action_group(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True))
        session = srv.PlayerSession("test", _FakeWebSocket(), server)
        session.protocol_v2 = True
        attribution = {"intentId": "i", "motorGeneration": 1, "actionGroupId": "g"}
        self.assertIsNone(session.set_intent({"intent_id": "i", "goal": "go", "actionGroupId": "other"}, attribution))
        self.assertIsNone(session.set_intent({"intent_id": "i", "goal": "go", "actionGroupId": None}, attribution))
        self.assertEqual(session.motor_generation, -1)
        self.assertIsNotNone(session.set_intent({"intent_id": "i", "goal": "go", "actionGroupId": "g"}, attribution))
        await session.close()

    async def test_v2_inference_error_preserves_attribution(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True))

        async def infer(jpeg, guidance, seq, intent):
            raise RuntimeError("inference unavailable")

        server.infer = infer
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        session.protocol_v2 = True
        try:
            session.set_intent({"intent_id": "same", "goal": "go"},
                               {"intentId": "same", "motorGeneration": 7, "actionGroupId": "g"})
            session.enqueue_frame(1, TINY_JPEG_B64, None)
            await asyncio.wait_for(session.worker, 3)
            error = next(message for message in ws.messages if message["type"] == "error")
            self.assertEqual((error["intentId"], error["motorGeneration"], error["actionGroupId"]), ("same", 7, "g"))
            self.assertEqual(error["forFrameSeq"], 1)
        finally:
            await session.close()

    async def test_v2_cancel_aborts_local_inference(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        server = srv.LumineServer(srv.ServerConfig(mock=True))

        async def infer(jpeg, guidance, seq, intent):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        server.infer = infer
        session = srv.PlayerSession("test", _FakeWebSocket(), server)
        session.protocol_v2 = True
        session.set_intent({"intent_id": "i", "goal": "go"}, {"intentId": "i", "motorGeneration": 1})
        session.enqueue_frame(1, TINY_JPEG_B64, None)
        await started.wait()
        worker = session.worker
        self.assertTrue(session.cancel_intent(2))
        await asyncio.wait_for(worker, 2)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(session.inflight)
        await session.close()

    async def test_server_zeroes_movement_after_first_chunk(self):
        server = srv.LumineServer(srv.ServerConfig(mock=True, mock_latency=0))

        async def repeated_movement_infer(jpeg, guidance, seq):
            chunks = [
                {"dx": 5, "dy": -4, "scroll": 2, "keys": ["W"]}
                for _ in range(6)
            ]
            return {
                "action": "<|action_start|>5 -4 2 ; W ; W ; W ; W ; W ; W<|action_end|>",
                "chunks": chunks,
                "absolute": None,
                "thought": None,
                "raw": "",
            }

        server.infer = repeated_movement_infer
        ws = _FakeWebSocket()
        session = srv.PlayerSession("test", ws, server)
        try:
            session.enqueue_frame(1, TINY_JPEG_B64, None)
            await asyncio.wait_for(session.worker, timeout=3.0)
            action = next(m for m in ws.messages if m["type"] == "action")
            self.assertEqual(
                (action["chunks"][0]["dx"], action["chunks"][0]["dy"], action["chunks"][0]["scroll"]),
                (5, -4, 2),
            )
            for chunk in action["chunks"][1:]:
                self.assertEqual(
                    (chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0)
                )
                self.assertEqual(chunk["keys"], ["W"])
        finally:
            await session.close()


@unittest.skipUnless(HAS_AIOHTTP, "aiohttp is not installed")
class PlayerServerTests(unittest.IsolatedAsyncioTestCase):
    """WebSocket 集成测试（mock 推理，不需要 vLLM）。"""

    async def test_authenticated_capability_round_trip_with_bridge(self):
        import lumine_bridge as bridge

        app = srv.create_app(srv.ServerConfig(mock=False, token="secret"))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with self.assertRaises(aiohttp.WSServerHandshakeError):
                await client.ws_connect("/ws/player", headers={"Authorization": "Bearer wrong"})
            ws = await client.ws_connect("/ws/player", headers={"Authorization": "Bearer secret"})
            try:
                self.assertEqual((await ws.receive_json())["type"], "hello")
                await ws.send_json({"type": "hello", "protocol": "lumine-live-v2"})
                motor_url = str(client.make_url("/ws/player").with_scheme("ws"))
                instance = bridge.LumineBridge(bridge.BridgeConfig(
                    motor_protocol="v2", lumine_ws=motor_url, lumine_token="secret"))
                instance._lumine_ws = ws
                instance._motor_connection_generation = 1
                instance._motor_connection_authenticated = True
                instance._motor_connected_origin = bridge._motor_origin(motor_url)
                instance._motor_connected_endpoint = bridge._motor_endpoint_identity(motor_url, "secret")
                instance._motor_connected_auth = bridge._motor_auth_identity(motor_url, "secret")
                async with instance._intent_lock:
                    await instance._request_motor_capability_locked()
                receipt = await ws.receive_json(timeout=2)
                await instance._handle_lumine_message(json.dumps(receipt), source_ws=ws)
                self.assertEqual(instance.intent_snapshot()["motorPromptEvidence"], "server_self_report")
            finally:
                await ws.close()
        finally:
            await client.close()

    async def asyncSetUp(self):
        # 放宽 mock 延迟，保证连续发送的帧会覆盖队列中的旧帧。
        self.config = srv.ServerConfig(
            host="127.0.0.1",
            port=0,
            token="secret",
            mock=True,
            mock_latency=0.15,
        )
        self.app = srv.create_app(self.config)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def _recv_json(self, ws, timeout=3.0):
        message = await asyncio.wait_for(ws.receive(), timeout=timeout)
        self.assertEqual(message.type, aiohttp.WSMsgType.TEXT, message)
        return json.loads(message.data)

    async def _recv_until(self, ws, wanted, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.fail("timed out waiting for message type {0}".format(wanted))
            data = await self._recv_json(ws, remaining)
            if data.get("type") == wanted:
                return data

    async def _status(self):
        async with self.client.get("/status") as response:
            self.assertEqual(response.status, 200)
            return await response.json()

    async def _connect(self, query="?token=secret", headers=None):
        ws = await self.client.ws_connect("/ws/player" + query, headers=headers)
        hello = await self._recv_json(ws)
        self.assertEqual(hello["type"], "hello")
        return ws, hello

    async def test_hello_and_ping(self):
        ws, hello = await self._connect()
        try:
            self.assertEqual(hello["model"], self.config.model)
            self.assertEqual(hello["fps"], 5)
            self.assertTrue(hello["mock"])
            self.assertEqual(hello["protocol"], srv.PROTOCOL_NAME)

            await ws.send_json({"type": "ping"})
            pong = await self._recv_until(ws, "pong")
            self.assertIsInstance(pong["ts"], int)
        finally:
            await ws.close()

    async def test_frame_produces_action(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json(
                {
                    "type": "hello",
                    "sessionId": "test-session",
                    "fps": 5,
                    "protocol": srv.PROTOCOL_NAME,
                }
            )
            await ws.send_json(
                {"type": "frame", "seq": 1, "tsMs": 123, "jpeg": TINY_JPEG_B64}
            )
            action = await self._recv_until(ws, "action")
            self.assertEqual(action["forFrameSeq"], 1)
            self.assertEqual(len(action["chunks"]), 6)
            self.assertIn("<|action_start|>", action["action"])
            self.assertIsInstance(action["latencyMs"], int)
            self.assertGreaterEqual(action["latencyMs"], 0)
            self.assertIsNone(action["absolute"])
            self.assertIn("thought", action)
        finally:
            await ws.close()

    async def test_stale_frames_are_dropped(self):
        ws, _ = await self._connect()
        try:
            jpeg = TINY_JPEG_B64
            # 先确认第一帧会被正常处理，再快速灌入 2..20 帧；后者在
            # 推理忙时只保留最新一帧，测试“陈旧帧被丢弃”的确定性。
            await ws.send_json(
                {"type": "frame", "seq": 1, "jpeg": jpeg, "guidance": "go"}
            )
            first = await self._recv_until(ws, "action")
            self.assertEqual(first["forFrameSeq"], 1)

            for seq in range(2, 21):
                await ws.send_json(
                    {"type": "frame", "seq": seq, "jpeg": jpeg, "guidance": "go"}
                )

            last = first
            while last["forFrameSeq"] < 20:
                last = await self._recv_until(ws, "action")
            self.assertEqual(last["forFrameSeq"], 20)

            # 等待工作线程收尾，再读取汇总状态。
            status = await self._status()
            self.assertEqual(status["latestSeq"], 20)
            self.assertGreaterEqual(status["dropped"], 1)
            self.assertGreaterEqual(status["sessions"][0]["receivedFrames"], 20)
        finally:
            await ws.close()

    async def test_absolute_action_from_mock(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json({"type": "frame", "seq": 32, "jpeg": TINY_JPEG_B64})
            action = await self._recv_until(ws, "action")
            self.assertEqual(action["forFrameSeq"], 32)
            self.assertEqual(action["absolute"], {"x": 640, "y": 360})
            self.assertEqual(action["chunks"][0]["keys"], ["LB"])
        finally:
            await ws.close()

    async def test_movement_is_only_on_first_chunk(self):
        ws, _ = await self._connect()
        try:
            # seq=10 是 mock 的 camera_right 阶段，前缀为 10 0 0。
            await ws.send_json({"type": "frame", "seq": 10, "jpeg": TINY_JPEG_B64})
            action = await self._recv_until(ws, "action")
            self.assertEqual(action["chunks"][0]["dx"], 10)
            for chunk in action["chunks"][1:]:
                self.assertEqual(
                    (chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0)
                )
        finally:
            await ws.close()

    async def test_guidance_reaches_status(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json({"type": "guidance", "text": "先观察左侧"})
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while True:
                status = await self._status()
                if any(s["guidance"] == "先观察左侧" for s in status["sessions"]):
                    break
                if loop.time() > deadline:
                    self.fail("guidance did not reach /status")
                await asyncio.sleep(0.02)
        finally:
            await ws.close()

    async def test_reset_clears_state(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json({"type": "guidance", "text": "go"})
            await ws.send_json({"type": "reset"})
            reset_status = await self._recv_until(ws, "status")
            self.assertEqual(reset_status["queue"], 0)
            self.assertEqual(reset_status["dropped"], 0)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + 3.0
            while True:
                status = await self._status()
                session = status["sessions"][0]
                if session["guidance"] == "" and session["latestSeq"] == 0:
                    break
                if loop.time() > deadline:
                    self.fail("reset did not clear session state")
                await asyncio.sleep(0.02)
        finally:
            await ws.close()

    async def test_invalid_frames_are_rejected_then_recover(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json({"type": "frame", "seq": 1, "jpeg": "not base64!!"})
            error = await self._recv_until(ws, "error")
            self.assertEqual(error["code"], "bad_frame")
            self.assertEqual(error["forFrameSeq"], 1)

            await ws.send_json({"type": "frame", "jpeg": TINY_JPEG_B64})
            error = await self._recv_until(ws, "error")
            self.assertEqual(error["code"], "bad_frame")

            await ws.send_json({"type": "frame", "seq": 2, "jpeg": TINY_JPEG_B64})
            action = await self._recv_until(ws, "action")
            self.assertEqual(action["forFrameSeq"], 2)
        finally:
            await ws.close()

    async def test_oversize_frame_rejected(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json(
                {
                    "type": "frame",
                    "seq": 1,
                    "jpeg": "A" * (srv.MAX_FRAME_BASE64_CHARS + 1),
                }
            )
            error = await self._recv_until(ws, "error", timeout=10.0)
            self.assertEqual(error["code"], "bad_frame")
            self.assertIn("too large", error["message"])
        finally:
            await ws.close()

    async def test_query_token_required(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as context:
            await self.client.ws_connect("/ws/player")
        self.assertEqual(context.exception.status, 401)

    async def test_bearer_token_accepted(self):
        ws, hello = await self._connect(
            query="", headers={"Authorization": "Bearer secret"}
        )
        self.assertEqual(hello["type"], "hello")
        await ws.close()

    async def test_wrong_token_rejected(self):
        with self.assertRaises(aiohttp.WSServerHandshakeError) as context:
            await self.client.ws_connect("/ws/player?token=wrong")
        self.assertEqual(context.exception.status, 401)

    async def test_http_health_and_status(self):
        async with self.client.get("/health") as response:
            self.assertEqual(response.status, 200)
            health = await response.json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["model"], self.config.model)
        self.assertTrue(health["mock"])

        status = await self._status()
        for key in ("model", "inflight", "queue", "latestSeq", "dropped", "lastLatencyMs"):
            self.assertIn(key, status)

    async def test_unknown_message_type(self):
        ws, _ = await self._connect()
        try:
            await ws.send_json({"type": "nope"})
            error = await self._recv_until(ws, "error")
            self.assertEqual(error["code"], "unknown_type")
        finally:
            await ws.close()


class CancellationOrderingTests(unittest.IsolatedAsyncioTestCase):
    """取消确认不得越过旧动作的发送。"""

    async def test_cancel_ack_waits_for_started_action_send(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingSocket(_FakeWebSocket):
            async def send_json(self, payload):
                if payload["type"] == "action":
                    entered.set()
                    await release.wait()
                await super().send_json(payload)

        ws = BlockingSocket()
        server = srv.LumineServer(srv.ServerConfig(mock=True, mock_latency=0))
        session = srv.PlayerSession("test", ws, server)
        await srv._handle_player_message(session, json.dumps({"type": "hello", "protocol": "lumine-live-v2"}))
        await srv._handle_player_message(session, json.dumps({"type": "intent", "intent": {"intent_id": "i", "goal": "go"}, "intentId": "i", "motorGeneration": 1}))
        session.enqueue_frame(1, TINY_JPEG_B64, None)
        await asyncio.wait_for(entered.wait(), 2)
        cancel = asyncio.create_task(srv._handle_player_message(session, json.dumps({"type": "cancel_intent", "motorGeneration": 2})))
        await asyncio.sleep(0)
        self.assertFalse(any(x["type"] == "cancel_ack" for x in ws.messages))
        release.set()
        await asyncio.wait_for(cancel, 2)
        await session.close()
        types = [x["type"] for x in ws.messages]
        self.assertLess(types.index("action"), types.index("cancel_ack"))

    async def test_cancel_first_drops_result_waiting_to_publish(self):
        ws = _FakeWebSocket()
        server = srv.LumineServer(srv.ServerConfig(mock=True))
        session = srv.PlayerSession("test", ws, server)
        session.protocol_v2 = True
        session.set_intent({"intent_id": "i", "goal": "go"}, {"intentId": "i", "motorGeneration": 1})
        generation = session.generation
        await session._motor_lock.acquire()
        cancel = asyncio.create_task(srv._handle_player_message(session, json.dumps({"type": "cancel_intent", "motorGeneration": 2})))
        await asyncio.sleep(0)
        action = asyncio.create_task(session._send_current_action({"type": "action"}, generation))
        session._motor_lock.release()
        await asyncio.wait_for(cancel, 2)
        self.assertIsNone(await asyncio.wait_for(action, 2))
        self.assertEqual([x["type"] for x in ws.messages], ["cancel_ack"])
        await session.close()


if __name__ == "__main__":
    unittest.main()
