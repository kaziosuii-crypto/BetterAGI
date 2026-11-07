#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lumine_bridge 的纯函数、队列与转发测试。

运行方式（无需 pytest）：
    python -m unittest discover -s integrations/lumine/tests -v

没有安装 aiohttp 时，涉及发送连接的用例会自动跳过，纯函数测试仍会执行。
"""

import asyncio
import base64
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LUMINE_DIR = os.path.dirname(HERE)
if LUMINE_DIR not in sys.path:
    sys.path.insert(0, LUMINE_DIR)

import lumine_bridge as bridge  # noqa: E402

try:
    import aiohttp  # noqa: F401

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False


def _chunk(dx=0, dy=0, scroll=0, keys=None):
    """构造一个合法动作键块。"""
    chunk = {"dx": dx, "dy": dy, "scroll": scroll}
    if keys is not None:
        chunk["keys"] = keys
    return chunk


class BuildWsUrlTests(unittest.TestCase):
    def test_appends_token_query(self):
        url = bridge.build_ws_url(
            "ws://127.0.0.1:30648/ws/v1/agent/live",
            "a b&c",
            "access_token",
        )
        self.assertIn("access_token=a+b%26c", url)
        self.assertTrue(url.startswith("ws://127.0.0.1:30648/ws/v1/agent/live?"))

    def test_replaces_existing_token(self):
        url = bridge.build_ws_url(
            "ws://127.0.0.1:8010/ws/player?token=old&x=1", "new", "token"
        )
        self.assertIn("token=new", url)
        self.assertNotIn("token=old", url)
        self.assertIn("x=1", url)

    def test_without_token_keeps_url(self):
        source = "wss://example.test/ws/player?token=keep"
        self.assertEqual(bridge.build_ws_url(source, None, "token"), source)

    def test_ws_handshake_moves_tokens_to_header_without_logging_secrets(self):
        url, headers = bridge._ws_endpoint_and_headers("ws://127.0.0.1:30648/ws?token=a%2Bb&x=1", "")
        self.assertEqual(url, "ws://127.0.0.1:30648/ws?x=1")
        self.assertEqual(headers, {"Authorization": "Bearer a+b"})
        self.assertNotIn("a+b", bridge._safe_error("request token=a%2Bb Bearer a+b", "a+b"))
        self.assertNotIn("a%2Bb", bridge._safe_error("request token=a%2Bb Bearer a+b", "a+b"))
        self.assertNotIn("private", bridge._safe_error("Authorization=Bearer private"))
        self.assertEqual(bridge._safe_endpoint("wss://user:secret@example.test/ws?token=hidden"),
                         "wss://example.test/ws")
        with self.assertRaises(ValueError):
            bridge._ws_endpoint_and_headers("ws://127.0.0.1/ws?token=a&access_token=b", "")

    def test_rejects_http_scheme(self):
        with self.assertRaises(ValueError):
            bridge.build_ws_url("http://127.0.0.1/ws", "t", "token")


class TimeTests(unittest.TestCase):
    def test_iso_z_to_epoch_ms(self):
        self.assertEqual(bridge.iso_to_epoch_ms("1970-01-01T00:00:01Z"), 1000)

    def test_naive_iso_is_utc(self):
        self.assertEqual(bridge.iso_to_epoch_ms("1970-01-01T00:00:01.500"), 1500)

    def test_numeric_and_invalid(self):
        self.assertEqual(bridge.iso_to_epoch_ms(1234), 1234)
        self.assertIsNone(bridge.iso_to_epoch_ms("not a time"))
        self.assertIsNone(bridge.iso_to_epoch_ms(None))
        self.assertIsNone(bridge.iso_to_epoch_ms(True))


class ChunkValidationTests(unittest.TestCase):
    def test_accepts_6_chunks_and_strips_unknown_fields(self):
        chunks = [
            {
                "dx": 5.0,
                "dy": -4,
                "scroll": 0,
                "keys": ["W", "Shift"],
                "evil": "ignored",
            }
            for _ in range(6)
        ]
        normalized = bridge.normalize_chunks(chunks)
        self.assertEqual(len(normalized), 6)
        self.assertEqual(
            normalized[0],
            {"dx": 5, "dy": -4, "scroll": 0, "keys": ["W", "Shift"]},
        )
        self.assertNotIn("evil", normalized[0])

    def test_missing_keys_becomes_empty_list(self):
        self.assertEqual(
            bridge.normalize_chunks([{"dx": 1, "dy": 2, "scroll": 3}]),
            [{"dx": 1, "dy": 2, "scroll": 3, "keys": []}],
        )

    def test_rejects_empty_or_too_many_chunks(self):
        self.assertIsNone(bridge.normalize_chunks([]))
        self.assertIsNone(bridge.normalize_chunks([_chunk() for _ in range(7)]))
        self.assertIsNone(bridge.normalize_chunks("not a list"))

    def test_rejects_too_many_keys(self):
        self.assertIsNone(
            bridge.normalize_chunks([_chunk(keys=["A", "B", "C", "D", "E"])])
        )

    def test_rejects_invalid_values(self):
        self.assertIsNone(bridge.normalize_chunks([_chunk(dx=True)]))
        self.assertIsNone(bridge.normalize_chunks([_chunk(dx="5")]))
        self.assertIsNone(bridge.normalize_chunks([_chunk(keys=[1])]))
        self.assertIsNone(bridge.normalize_chunks([_chunk(keys="W")]))
        self.assertIsNone(bridge.normalize_chunks(["not a chunk"]))

    def test_normalize_absolute(self):
        self.assertEqual(
            bridge.normalize_absolute({"x": 640.0, "y": 360}), {"x": 640, "y": 360}
        )
        self.assertIsNone(bridge.normalize_absolute({"x": "640", "y": 360}))
        self.assertIsNone(bridge.normalize_absolute(None))


class FrameMessageTests(unittest.TestCase):
    def test_builds_protocol_fields_only(self):
        frame = {
            "type": "frame",
            "seq": 9,
            "ts": "1970-01-01T00:00:01Z",
            "jpeg": "abc",
            "guidance": "向前走",
            "capture": {"x": 0},
        }
        result = bridge.build_frame_message(frame)
        self.assertEqual(
            result,
            {
                "type": "frame",
                "seq": 9,
                "tsMs": 1000,
                "jpeg": "abc",
                "guidance": "向前走",
            },
        )

    def test_latest_guidance_wins_over_stale_inline(self):
        result = bridge.build_frame_message(
            {"seq": 1, "jpeg": "x", "guidance": "旧指导"}, "新指导"
        )
        self.assertEqual(result["guidance"], "新指导")

    def test_falls_back_to_latest_guidance(self):
        result = bridge.build_frame_message({"seq": 1, "jpeg": "x"}, "保持前进")
        self.assertEqual(result["guidance"], "保持前进")
        self.assertIsInstance(result["tsMs"], int)

    def test_invalid_frame_returns_none(self):
        self.assertIsNone(bridge.build_frame_message({"seq": 1}))
        self.assertIsNone(bridge.build_frame_message({"jpeg": "x"}))
        self.assertIsNone(bridge.build_frame_message({"seq": True, "jpeg": "x"}))


class BridgeStateTests(unittest.TestCase):
    def test_live_stopped_clears_guidance_and_frame(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig())
        instance.latest_guidance = "旧场景指导"
        instance.latest_guidance_at_ms = 123
        instance.pump.put({"seq": 7})

        asyncio.run(
            instance._handle_bettergi_message(json.dumps({"type": "live.stopped"}))
        )

        self.assertEqual(instance.latest_guidance, "")
        self.assertEqual(instance.latest_guidance_at_ms, 0)
        self.assertIsNone(instance.pump.pending)

    def test_action_seq_is_monotonic_across_frame_reset(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig())
        sent = []

        async def fake_send(payload):
            sent.append(payload)
            return True

        instance._send_bettergi = fake_send  # type: ignore[method-assign]

        async def scenario():
            await instance._handle_lumine_message(
                json.dumps(
                    {
                        "type": "action",
                        "forFrameSeq": 5,
                        "chunks": [_chunk(keys=["W"])],
                    }
                )
            )
            await instance._handle_lumine_message(
                json.dumps(
                    {
                        "type": "action",
                        "forFrameSeq": 1,
                        "chunks": [_chunk(keys=["W"])],
                    }
                )
            )

        asyncio.run(scenario())
        self.assertEqual([item["seq"] for item in sent], [1, 2])


class FramePumpTests(unittest.TestCase):
    def test_samples_at_target_rate_and_keeps_latest(self):
        pump = bridge.FramePump(5)  # 200ms 间隔
        pump.put({"seq": 1})
        self.assertEqual(pump.poll(0.0)["seq"], 1)

        pump.put({"seq": 2})
        pump.put({"seq": 3})
        self.assertEqual(pump.dropped, 1)

        self.assertIsNone(pump.poll(0.1))
        self.assertEqual(pump.poll(0.2)["seq"], 3)
        self.assertIsNone(pump.pending)

    def test_skips_stale_sequence(self):
        pump = bridge.FramePump(10)
        pump.put({"seq": 10})
        self.assertEqual(pump.poll(0.0)["seq"], 10)

        pump.put({"seq": 10})
        self.assertIsNone(pump.poll(0.2))
        self.assertEqual(pump.dropped, 1)

    def test_reset_allows_new_session_lower_seq(self):
        pump = bridge.FramePump(5)
        pump.put({"seq": 100})
        self.assertEqual(pump.poll(0.0)["seq"], 100)

        pump.reset()
        pump.put({"seq": 1})
        self.assertEqual(pump.poll(0.01)["seq"], 1)

    def test_sequence_regression_is_treated_as_new_session(self):
        pump = bridge.FramePump(5)
        pump.put({"seq": 100})
        self.assertEqual(pump.poll(0.0)["seq"], 100)

        # 同一连接上 BetterGI 重启会话时，序号会从 1 重新开始。
        pump.put({"seq": 1})
        self.assertEqual(pump.poll(0.2)["seq"], 1)

    def test_ignores_non_dict_and_invalid_fps(self):
        pump = bridge.FramePump(5)
        pump.put("bad")
        self.assertIsNone(pump.pending)
        with self.assertRaises(ValueError):
            bridge.FramePump(0)


class ActionsMessageTests(unittest.TestCase):
    def test_forwards_structured_chunks(self):
        message = {
            "type": "action",
            "forFrameSeq": 123,
            "action": "<raw text ignored because chunks exist>",
            # AutoDL 会把 200ms 块的移动前缀复制到全部键块。
            "chunks": [_chunk(dx=5, dy=4, scroll=2, keys=["W"]) for _ in range(6)],
            "absolute": {"x": 640, "y": 360},
            "thought": "沿路前进",
            "latencyMs": 180,
            "raw": "不可回传的内容",
        }
        result = bridge.build_actions_message(message)
        self.assertEqual(result["type"], "actions")
        self.assertEqual(result["seq"], 123)
        self.assertEqual(len(result["chunks"]), 6)
        self.assertEqual(
            result["chunks"][0],
            {"dx": 5, "dy": 4, "scroll": 2, "keys": ["W"]},
        )
        # 位移/滚轮只在首块执行一次，其余块必须为零且保留按键。
        for chunk in result["chunks"][1:]:
            self.assertEqual(chunk["dx"], 0)
            self.assertEqual(chunk["dy"], 0)
            self.assertEqual(chunk["scroll"], 0)
            self.assertEqual(chunk["keys"], ["W"])
        self.assertEqual(result["absolute"], {"x": 640, "y": 360})
        self.assertEqual(result["thought"], "沿路前进")
        self.assertNotIn("latencyMs", result)
        self.assertNotIn("raw", result)

    def test_movement_prefix_applied_only_once(self):
        chunks = [
            {"dx": 5, "dy": -4, "scroll": 3, "keys": ["W"]},
            {"dx": 5, "dy": -4, "scroll": 3, "keys": ["W", "Shift"]},
            {"dx": 5, "dy": -4, "scroll": 3, "keys": []},
        ]
        result = bridge.apply_movement_prefix_once(chunks)
        self.assertEqual(result[0], {"dx": 5, "dy": -4, "scroll": 3, "keys": ["W"]})
        self.assertEqual(
            result[1],
            {"dx": 0, "dy": 0, "scroll": 0, "keys": ["W", "Shift"]},
        )
        self.assertEqual(result[2], {"dx": 0, "dy": 0, "scroll": 0, "keys": []})
        # 输入列表不被修改。
        self.assertEqual(chunks[1]["dx"], 5)
        self.assertEqual(bridge.apply_movement_prefix_once([]), [])

    def test_seq_falls_back_to_message_seq(self):
        result = bridge.build_actions_message(
            {"type": "action", "seq": 7, "chunks": [_chunk()]}
        )
        self.assertEqual(result["seq"], 7)

    def test_missing_seq_becomes_none(self):
        result = bridge.build_actions_message({"type": "action", "chunks": [_chunk()]})
        self.assertIsNone(result["seq"])

    def test_parses_raw_action_when_chunks_missing(self):
        parsed = {
            "chunks": [_chunk(dx=5, keys=["W"]) for _ in range(6)],
            "absolute": None,
            "thought": "解析出的思考",
        }
        with mock.patch.object(bridge, "_external_parse_action", lambda raw: parsed):
            result = bridge.build_actions_message(
                {
                    "type": "action",
                    "forFrameSeq": 4,
                    "action": "5 0 0 ; W ; W",
                }
            )
        self.assertEqual(result["seq"], 4)
        self.assertEqual(len(result["chunks"]), 6)
        self.assertEqual(result["chunks"][0]["dx"], 5)
        for chunk in result["chunks"][1:]:
            self.assertEqual(chunk["dx"], 0)
            self.assertEqual(chunk["keys"], ["W"])
        self.assertEqual(result["thought"], "解析出的思考")

    def test_parser_absent_is_safe(self):
        with mock.patch.object(bridge, "_external_parse_action", None):
            self.assertIsNone(
                bridge.build_actions_message(
                    {"type": "action", "forFrameSeq": 1, "action": "5 0 0 ; W"}
                )
            )

    def test_parser_returning_garbage_is_safe(self):
        with mock.patch.object(
            bridge, "_external_parse_action", lambda raw: {"chunks": []}
        ):
            self.assertIsNone(
                bridge.build_actions_message({"type": "action", "action": "raw"})
            )

    @unittest.skipUnless(
        bridge._external_parse_action is not None,
        "lumine_protocol 不可用，跳过真实解析测试",
    )
    def test_parses_raw_action_with_real_lumine_protocol(self):
        message = {
            "type": "action",
            "forFrameSeq": 11,
            "action": ("<|action_start|>5 0 0 ; W ; W ; W ; W ; W ; W<|action_end|>"),
        }
        result = bridge.build_actions_message(message)
        self.assertIsNotNone(result)
        self.assertEqual(result["seq"], 11)
        self.assertEqual(len(result["chunks"]), 6)
        self.assertEqual(result["chunks"][0]["dx"], 5)
        self.assertEqual(result["chunks"][0]["keys"], ["W"])
        for chunk in result["chunks"][1:]:
            self.assertEqual(chunk["dx"], 0)
            self.assertEqual(chunk["scroll"], 0)
            self.assertEqual(chunk["keys"], ["W"])
        self.assertIsNone(result["absolute"])

    def test_rejects_empty_or_invalid_chunks(self):
        self.assertIsNone(
            bridge.build_actions_message({"type": "action", "chunks": []})
        )
        self.assertIsNone(
            bridge.build_actions_message(
                {"type": "action", "chunks": [_chunk(keys=["A"] * 5)]}
            )
        )
        self.assertIsNone(
            bridge.build_actions_message(
                {"type": "something-else", "chunks": [_chunk()]}
            )
        )

    def test_thought_is_truncated(self):
        result = bridge.build_actions_message(
            {
                "type": "action",
                "chunks": [_chunk()],
                "thought": "x" * (bridge.MAX_THOUGHT_CHARS + 100),
            }
        )
        self.assertEqual(len(result["thought"]), bridge.MAX_THOUGHT_CHARS)


class RecordingQueueTests(unittest.TestCase):
    def test_drops_oldest_when_full(self):
        queue = bridge.RecordingQueue(maxsize=2)
        self.assertTrue(queue.put(b"1"))
        self.assertTrue(queue.put(b"2"))
        self.assertTrue(queue.put(b"3"))
        self.assertEqual(len(queue), 2)
        self.assertEqual(queue.dropped, 1)
        self.assertEqual(queue.get(timeout=0), b"2")
        self.assertEqual(queue.get(timeout=0), b"3")
        self.assertIsNone(queue.get(timeout=0))

    def test_close_rejects_new_items(self):
        queue = bridge.RecordingQueue(maxsize=1)
        queue.close()
        self.assertTrue(queue.closed)
        self.assertFalse(queue.put(b"x"))
        self.assertIsNone(queue.get(timeout=0.01))

    def test_rejects_invalid_capacity(self):
        with self.assertRaises(ValueError):
            bridge.RecordingQueue(0)


class RecorderTests(unittest.TestCase):
    def test_submit_before_start_is_ignored(self):
        recorder = bridge.Recorder("out.mp4", 10)
        self.assertFalse(recorder.running)
        self.assertFalse(recorder.submit_jpeg("YWJj"))
        recorder.close()

    def test_start_without_ffmpeg_disables_recording(self):
        recorder = bridge.Recorder("out.mp4", 10)
        with mock.patch.object(bridge.shutil, "which", return_value=None):
            self.assertFalse(recorder.start())
        self.assertFalse(recorder.running)
        recorder.close()

    def test_submit_decodes_jpeg_into_bounded_queue(self):
        recorder = bridge.Recorder("out.mp4", 10)
        recorder._running = True
        encoded = base64.b64encode(b"fake-jpeg").decode("ascii")
        self.assertTrue(recorder.submit_jpeg(encoded))
        self.assertEqual(recorder.queue.get(timeout=0), b"fake-jpeg")
        self.assertFalse(recorder.submit_jpeg("not-base64!!"))
        recorder._running = False
        recorder.close()

    def test_invalid_fps(self):
        with self.assertRaises(ValueError):
            bridge.Recorder("out.mp4", 0)


class StatsTests(unittest.TestCase):
    def test_latency_and_snapshot(self):
        stats = bridge.BridgeStats()
        stats.frames_in = 10
        stats.frames_out = 8
        stats.actions_in = 4
        stats.actions_out = 3
        stats.record_latency(180)
        stats.record_latency(200)
        stats.record_latency(-1)
        stats.record_latency("bad")

        snapshot = stats.snapshot(frame_drops=2, record_drops=1)
        self.assertEqual(snapshot["framesIn"], 10)
        self.assertEqual(snapshot["framesOut"], 8)
        self.assertEqual(snapshot["actionsIn"], 4)
        self.assertEqual(snapshot["actionsOut"], 3)
        self.assertEqual(snapshot["latencyLastMs"], 200)
        self.assertEqual(snapshot["latencyAvgMs"], 190.0)
        self.assertEqual(snapshot["frameDrops"], 2)
        self.assertEqual(snapshot["recordDrops"], 1)


class ConfigTests(unittest.TestCase):
    def test_example_config_has_all_defaults(self):
        path = os.path.join(LUMINE_DIR, "bridge_config.example.json")
        with open(path, "r", encoding="utf-8") as source:
            config = json.load(source)
        for key in (
            "bettergiWs",
            "bettergiToken",
            "lumineWs",
            "lumineToken",
            "fps",
            "record",
            "recordFps",
            "reconnect",
            "statsIntervalSeconds",
        ):
            self.assertIn(key, config)
        self.assertEqual(config["fps"], 5)
        self.assertEqual(config["recordFps"], 10)
        self.assertTrue(config["reconnect"])

    def test_parser_uses_config_defaults(self):
        with mock.patch.dict(
            os.environ, {"BETTERGI_AGENT_TOKEN": "", "LUMINE_SERVER_TOKEN": ""}
        ):
            parser = bridge.build_parser(
                {
                    "bettergiWs": "ws://127.0.0.1:1/bg",
                    "lumineWs": "ws://127.0.0.1:2/lm",
                    "fps": 3,
                    "recordFps": 6,
                    "reconnect": False,
                }
            )
            args = parser.parse_args([])
        self.assertEqual(args.bettergi_ws, "ws://127.0.0.1:1/bg")
        self.assertEqual(args.lumine_ws, "ws://127.0.0.1:2/lm")
        self.assertEqual(args.fps, 3.0)
        self.assertEqual(args.record_fps, 6.0)
        self.assertFalse(args.reconnect)

    def test_cli_overrides_config(self):
        parser = bridge.build_parser({"fps": 3, "reconnect": True})
        args = parser.parse_args(["--fps", "8", "--no-reconnect"])
        self.assertEqual(args.fps, 8.0)
        self.assertFalse(args.reconnect)


class _FakeWebSocket:
    """记录发送内容的伪 WebSocket。"""

    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_str(self, text):
        self.sent.append(text)


@unittest.skipUnless(AIOHTTP_AVAILABLE, "aiohttp 未安装，跳过连接集成测试")
class SendIntegrationTests(unittest.TestCase):
    def test_send_bettergi_serializes_actions(self):
        bridge_instance = bridge.LumineBridge(bridge.BridgeConfig())
        fake = _FakeWebSocket()
        bridge_instance._bettergi_ws = fake

        async def scenario():
            return await bridge_instance._send_bettergi(
                {
                    "type": "actions",
                    "seq": 1,
                    "chunks": [_chunk()],
                    "absolute": None,
                    "thought": None,
                }
            )

        self.assertTrue(asyncio.run(scenario()))
        payload = json.loads(fake.sent[0])
        self.assertEqual(payload["type"], "actions")
        self.assertEqual(payload["seq"], 1)

    def test_send_when_disconnected_returns_false(self):
        bridge_instance = bridge.LumineBridge(bridge.BridgeConfig())

        async def scenario():
            return await bridge_instance._send_lumine({"type": "frame", "seq": 1})

        self.assertFalse(asyncio.run(scenario()))


def _configure_motor_v2(instance):
    if not instance.config.lumine_token:
        instance.config.lumine_token = "motor-fixture-token"
    instance._motor_connection_authenticated = True
    instance._motor_connection_generation = 1
    instance._motor_connected_origin = bridge._motor_origin(instance.config.lumine_ws)
    instance._motor_connected_endpoint = bridge._motor_endpoint_identity(instance.config.lumine_ws, instance.config.lumine_token)
    instance._motor_connected_auth = bridge._motor_auth_identity(instance.config.lumine_ws, instance.config.lumine_token)


async def _bind_active_v2(instance):
    """建立模拟宿主的 prepared → commit → active 租约。"""
    import socket

    if instance._bettergi_ws is None:
        instance._bettergi_ws = _FakeWebSocket()
    if instance._lumine_ws is None:
        instance._lumine_ws = _FakeWebSocket()
    _configure_motor_v2(instance)
    instance._verified_bettergi_ws = instance._bettergi_ws
    instance._verified_bettergi_session = "fixture-live"
    credentials = {"secret": "a" * 64, "instanceId": "b" * 32, "epoch": 1}
    request_id = await instance.bind_live_lease({"sessionId": "fixture-live", "credentials": credentials})
    assert request_id
    connection = "c" * 32
    receipt = {"type": "lease.bound", "leaseRequestId": request_id, "leaseInstanceId": credentials["instanceId"],
               "leaseEpoch": 1, "sessionId": "fixture-live", "leaseConnectionId": connection,
               "leaseStatus": "prepared", "leaseProbeId": request_id, "leaseExpiresInMs": 3000}
    await instance._handle_bettergi_message(json.dumps(receipt), source_ws=instance._bettergi_ws)
    assert instance._lease_binding["status"] == "prepared_bound"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        instance.config.intent_port = sock.getsockname()[1]
    instance.config.intent_token = "fixture-bus-token"
    await instance._start_intent_server()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(f"http://127.0.0.1:{instance.config.intent_port}/lease/commit",
                                   headers={"Authorization": "Bearer fixture-bus-token"},
                                   json={"requestId": request_id, "sessionId": "fixture-live",
                                         "instanceId": credentials["instanceId"], "epoch": 1,
                                         "connectionId": connection, "motorConnectionGeneration": 1}) as response:
                assert response.status == 200
                assert (await response.json())["status"] == "commit_pending"
        receipt.update(leaseStatus="active", leaseProbeId=instance._lease_binding["probe_id"])
        await instance._handle_bettergi_message(json.dumps(receipt), source_ws=instance._bettergi_ws)
        assert instance._lease_binding["status"] == "bound"
    finally:
        await instance._stop_intent_server()
    instance._bettergi_ws.sent.clear()
    instance._lumine_ws.sent.clear()


class IntentBridgeTests(unittest.TestCase):
    """v2 意图总线的校验、转发与反馈。"""

    def test_normalize_and_forward_intent(self):
        bridge_instance = bridge.LumineBridge(
            bridge.BridgeConfig(motor_protocol="v2")
        )
        fake = _FakeWebSocket()
        bridge_instance._lumine_ws = fake

        async def scenario():
            await _bind_active_v2(bridge_instance)
            return await bridge_instance.set_intent(
                {
                    "intent_id": "i-1",
                    "goal": " 跟随 安柏 ",
                    "constraints": ["不要离开任务区"],
                    "motor_mode": "navigate",
                }
            )

        intent = asyncio.run(scenario())
        self.assertEqual(intent["intent_id"], "i-1")
        self.assertEqual(intent["goal"], "跟随 安柏")
        sent = json.loads(fake.sent[0])
        self.assertEqual(sent["type"], "intent")
        self.assertEqual((sent["intentId"], sent["motorGeneration"]), ("i-1", 1))
        self.assertTrue(bridge_instance.intent_feedback)

    def test_invalid_intent_is_rejected(self):
        bridge_instance = bridge.LumineBridge(bridge.BridgeConfig())
        self.assertIsNone(asyncio.run(bridge_instance.set_intent({"goal": "x"})))
        self.assertIsNone(asyncio.run(bridge_instance.set_intent({"intent_id": "i"})))

    def test_action_attempt_feedback_requires_exact_generation(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "i", "goal": "go"})
            await instance._handle_lumine_message(json.dumps({"type": "action", "intentId": "i",
                "motorGeneration": 1, "chunks": [_chunk(keys=["W"])]}), source_ws=instance._lumine_ws)
            count = len(instance.intent_feedback)
            attempt = {"type": "action_attempt", "seq": 1, "attemptId": "attempt-1", "executionAttempt": {
                "intentId": "i", "motorGeneration": 0, "seq": 1, "attemptId": "attempt-1", "status": "failed"}}
            await instance._handle_bettergi_message(json.dumps(attempt), source_ws=instance._bettergi_ws)
            self.assertEqual(len(instance.intent_feedback), count)
            attempt["executionAttempt"].update(motorGeneration=1, status="unknown")
            await instance._handle_bettergi_message(json.dumps(attempt), source_ws=instance._bettergi_ws)
            feedback = instance.intent_feedback[-1]
            self.assertEqual(feedback["reason"], "input_attempt_unknown")
            self.assertFalse(feedback["executionConfirmed"])
            self.assertIn("未知", feedback["observation"])
            await instance._handle_bettergi_message(json.dumps({"type": "input_evidence", "seq": 1,
                "attemptId": "attempt-1", "intentId": "i", "motorGeneration": 1,
                "inputEvidence": {"status": "accepted", "operation": "KeyDown", "attemptId": "attempt-1"}}),
                source_ws=instance._bettergi_ws)
            feedback = instance.intent_feedback[-1]
            self.assertEqual(feedback["reason"], "input_system_accepted")
            self.assertFalse(feedback["executionConfirmed"])
            self.assertNotEqual(feedback["status"], "done")
        asyncio.run(scenario())

    def test_v2_wait_and_navigation_reject_unauthorized_inputs(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "wait", "goal": "等待", "motor_mode": "wait"})
            await instance._handle_lumine_message(json.dumps({
                "type": "action", "chunks": [{"keys": ["F"], "dx": 0, "dy": 0, "scroll": 0}],
                "intentId": "wait", "motorGeneration": 1,
            }))
            sent = [json.loads(item) for item in instance._bettergi_ws.sent]
            self.assertEqual([item["type"] for item in sent], ["stop_actions"])
            self.assertTrue(any(row["reason"] == "motor_mode_violation" for row in instance.intent_feedback))
            self.assertIsNone(instance.latest_intent)
            self.assertFalse(bridge.action_matches_motor_mode({"chunks": [{"keys": ["F"]}]}, "navigate"))
            self.assertTrue(bridge.action_matches_motor_mode({"chunks": [{"keys": ["W"]}]}, "navigate"))
            self.assertFalse(bridge.action_matches_motor_mode({"chunks": [{"keys": [], "dx": 1}]}, "wait"))

        asyncio.run(scenario())

    def test_v2_fails_closed_when_send_or_stop_fails(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))

        async def scenario():
            self.assertIsNone(await instance.set_intent({"intent_id": "offline", "goal": "go"}))
            self.assertIsNone(instance.latest_intent)
            instance._lumine_ws = _FakeWebSocket()
            instance._bettergi_ws = _FakeWebSocket()
            self.assertIsNone(await instance.set_intent({"intent_id": "old", "goal": "go"}))
            await instance.clear_intent("recover_uncertain_send")
            await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": instance._pending_cancel_generation}), source_ws=instance._lumine_ws)
            instance._bettergi_ws = None
            self.assertIsNone(await instance.set_intent({"intent_id": "old", "goal": "go"}))
            instance._bettergi_ws = _FakeWebSocket()
            status = await instance.clear_intent("retry_release")
            self.assertTrue(status["remoteStopSent"] and status["inputReleaseSent"])
            self.assertEqual(status["motorGeneration"], instance._pending_cancel_generation)
            self.assertIn(status["stopRequestId"], instance._stop_requests)
            await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": instance._pending_cancel_generation}), source_ws=instance._lumine_ws)
            self.assertTrue(instance._release_unconfirmed)
            self.assertIsNone(await instance.set_intent({"intent_id": "retry", "goal": "go"}))

        asyncio.run(scenario())

    def test_action_and_ack_become_progress_feedback(self):
        bridge_instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        bridge_instance._lumine_ws = _FakeWebSocket()

        async def scenario():
            await _bind_active_v2(bridge_instance)
            await bridge_instance.set_intent({"intent_id": "i-1", "goal": "前进"})
            await bridge_instance._handle_bettergi_message(json.dumps({"type": "action", "seq": 1}))
            await bridge_instance._handle_lumine_message(
                json.dumps({"type": "intent_ack", "intentId": "i-1", "motorGeneration": 1, "accepted": True}),
                source_ws=bridge_instance._lumine_ws)
            statuses = [item["status"] for item in bridge_instance.intent_feedback]
            self.assertIn("progress", statuses)
            await bridge_instance.clear_intent("test")
            self.assertIsNone(bridge_instance.latest_intent)
            self.assertEqual(bridge_instance.intent_feedback[-1]["status"], "cancelled")
            self.assertEqual(bridge_instance.intent_feedback[-1]["motorGeneration"], 1)

        asyncio.run(scenario())

    def test_v2_rejects_stale_and_unattributed_actions_and_echoes(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "same", "goal": "new", "actionGroupId": "group"})
            for fields in ({}, {"intentId": "same", "motorGeneration": 0, "actionGroupId": "group"},
                           {"intentId": "same", "motorGeneration": True, "actionGroupId": "group"},
                           {"intentId": "same", "motorGeneration": 1}):
                await instance._handle_lumine_message(json.dumps({"type": "action", "chunks": [_chunk()], **fields}))
            before = len(instance.intent_feedback)
            await instance._handle_bettergi_message(json.dumps({"type": "action", "seq": 1}))
            await instance._handle_lumine_message(json.dumps({"type": "intent_ack", "intentId": "same", "motorGeneration": 0, "accepted": True}))
            self.assertEqual(len(instance.intent_feedback), before)
            await instance._handle_lumine_message(json.dumps({"type": "action", "chunks": [_chunk()], "intentId": "same", "motorGeneration": 1, "actionGroupId": "group"}))
            self.assertEqual(instance.stats.actions_rejected, 4)
            sent = [json.loads(x) for x in instance._bettergi_ws.sent]
            self.assertEqual([x["type"] for x in sent], ["actions"])
            self.assertEqual(sent[-1]["motorGeneration"], 1)
            self.assertEqual(instance.intent_feedback[-1]["reason"], "action_forwarded")
            self.assertEqual(instance.intent_feedback[-1]["motorGeneration"], 1)
            self.assertEqual(instance.intent_feedback[-1]["actionGroupId"], "group")
            result = await instance.clear_intent("cancelled")
            self.assertEqual(instance.intent_feedback[-1]["status"], "cancelled")
            self.assertEqual(instance.intent_feedback[-1]["motorGeneration"], 1)
            self.assertEqual(instance.intent_feedback[-1]["actionGroupId"], "group")
            self.assertTrue(result["remoteStopSent"] and result["inputReleaseSent"])
            self.assertEqual(result["motorGeneration"], 2)
            self.assertIn(result["stopRequestId"], instance._stop_requests)
            self.assertEqual(json.loads(instance._lumine_ws.sent[-1]), {"type": "cancel_intent", "motorGeneration": 2})
            await instance._handle_lumine_message(json.dumps({"type": "action", "chunks": [_chunk()], "intentId": "same", "motorGeneration": 1, "actionGroupId": "group"}))
            self.assertEqual(instance.stats.actions_out, 1)
        asyncio.run(scenario())

    def test_v2_error_attribution_and_failed_forward_revoke(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "same", "goal": "go", "actionGroupId": "g"})
            before = list(instance.intent_feedback)
            for fields in ({}, {"intentId": "same", "motorGeneration": 0, "actionGroupId": "g"},
                           {"intentId": "same", "motorGeneration": 1}):
                await instance._handle_lumine_message(json.dumps({"type": "error", "code": "inference_failed", **fields}))
            self.assertEqual(list(instance.intent_feedback), before)
            self.assertEqual(instance.latest_intent["goal"], "go")
            await instance._handle_lumine_message(json.dumps({"type": "error", "code": "inference_failed", "intentId": "same", "motorGeneration": 1, "actionGroupId": "g"}))
            self.assertIsNone(instance.latest_intent)
            failed = instance.intent_feedback[-1]
            self.assertEqual((failed["status"], failed["motorGeneration"], failed["actionGroupId"]), ("failed", 1, "g"))
            await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": 2}), source_ws=instance._lumine_ws)
            self.assertTrue(instance._release_unconfirmed)
            self.assertIsNone(await instance.set_intent({"intent_id": "next", "goal": "go"}))

            other = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
            other._lumine_ws = _FakeWebSocket()
            await _bind_active_v2(other)
            other._bettergi_ws.closed = True
            self.assertIsNotNone(await other.set_intent({"intent_id": "forward", "goal": "go"}))
            await other._handle_lumine_message(json.dumps({"type": "action", "chunks": [_chunk()], "intentId": "forward", "motorGeneration": 1}))
            self.assertIsNone(other.latest_intent)
            self.assertTrue(other._release_unconfirmed)
            self.assertIsNone(await other.set_intent({"intent_id": "next", "goal": "go"}))

        asyncio.run(scenario())

    def test_cancel_is_ordered_after_inflight_forward(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        started = asyncio.Event()
        release = asyncio.Event()
        sends = []

        async def send(payload, **kwargs):
            if payload["type"] == "actions":
                started.set()
                await release.wait()
            sends.append(payload["type"])
            return True

        async def scenario():
            await _bind_active_v2(instance)
            instance._send_bettergi = send
            self.assertIsNotNone(await instance.set_intent({"intent_id": "i", "goal": "go"}))
            task = asyncio.create_task(instance._handle_lumine_message(json.dumps({"type": "action", "chunks": [_chunk()], "intentId": "i", "motorGeneration": 1}), source_ws=instance._lumine_ws))
            await asyncio.wait_for(started.wait(), timeout=2)
            cancel = asyncio.create_task(instance.clear_intent("cancelled"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task, cancel)
            self.assertEqual(sends, ["actions", "stop_actions"])
        asyncio.run(scenario())

    def test_motor_disconnect_revokes_intent_and_releases_input(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        ws = _FakeWebSocket()

        class Connection:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *args):
                instance._running = False

        class Session:
            def ws_connect(self, *args, **kwargs):
                return Connection()

        async def empty_messages():
            if False:
                yield None

        # 模拟一次正常建立后立即断开的 WebSocket。
        class Socket(_FakeWebSocket):
            def __aiter__(self):
                return empty_messages()

        ws = Socket()
        instance._session = Session()
        async def scenario():
            await _bind_active_v2(instance)
            self.assertIsNotNone(await instance.set_intent({"intent_id": "i", "goal": "go"}))
            instance._running = True
            await instance._lumine_loop()
            self.assertIsNone(instance.latest_intent)
            self.assertEqual(json.loads(ws.sent[0])["protocol"], "lumine-live-v2")
            self.assertEqual(json.loads(ws.sent[1])["type"], "cancel_intent")
            self.assertEqual(json.loads(ws.sent[1])["motorGeneration"], 2)
            self.assertEqual(json.loads(instance._bettergi_ws.sent[-1])["type"], "stop_actions")
        asyncio.run(scenario())

    def test_both_websocket_loops_use_auth_headers_without_query_tokens(self):
        async def scenario(which):
            config = bridge.BridgeConfig(bettergi_ws="ws://127.0.0.1:1/live?x=1&access_token=old",
                                         bettergi_token="bgi-secret",
                                         lumine_ws="ws://127.0.0.1:2/player?token=old&x=2",
                                         lumine_token="motor-secret", reconnect=False)
            instance = bridge.LumineBridge(config)
            calls = []
            class Socket(_FakeWebSocket):
                def __aiter__(self):
                    async def empty():
                        if False:
                            yield None
                    return empty()
            class Connection:
                async def __aenter__(self): return Socket()
                async def __aexit__(self, *args): instance._running = False
            class Session:
                def ws_connect(self, url, **kwargs):
                    calls.append((url, kwargs))
                    return Connection()
            instance._session = Session()
            instance._running = True
            await (instance._bettergi_loop() if which == "bettergi" else instance._lumine_loop())
            return calls[0]
        for which, expected, token in (("bettergi", "ws://127.0.0.1:1/live?x=1", "bgi-secret"),
                                       ("motor", "ws://127.0.0.1:2/player?x=2", "motor-secret")):
            url, kwargs = asyncio.run(scenario(which))
            self.assertEqual(url, expected)
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + token})
            self.assertNotIn(token, url)

    def test_cancel_disconnected_reports_unconfirmed(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        async def scenario():
            await instance.set_intent({"intent_id": "i", "goal": "go"})
            return await instance.clear_intent()
        result = asyncio.run(scenario())
        self.assertFalse(result["remoteStopSent"] or result["inputReleaseSent"])
        self.assertIn(result["stopRequestId"], instance._stop_requests)
        self.assertIsNone(instance.latest_intent)

    @unittest.skipUnless(bridge.aiohttp is not None, "需要 aiohttp")
    def test_intent_http_bus_roundtrip(self):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        bridge_instance = bridge.LumineBridge(
            bridge.BridgeConfig(
                motor_protocol="v2",
                intent_port=port,
                intent_token="intent-secret",
            )
        )
        bridge_instance._lumine_ws = _FakeWebSocket()

        async def scenario():
            await _bind_active_v2(bridge_instance)
            bridge_instance.config.intent_port = port
            bridge_instance.config.intent_token = "intent-secret"
            await bridge_instance._start_intent_server()
            headers = {"Authorization": "Bearer intent-secret"}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "http://127.0.0.1:{0}/intent".format(port),
                    json={"intent_id": "i-http", "goal": "去城门"},
                    headers=headers,
                ) as response:
                    self.assertEqual(response.status, 200)
                    data = await response.json()
                async with session.get(
                    "http://127.0.0.1:{0}/feedback".format(port),
                    headers=headers,
                ) as response:
                    feedback = await response.json()
                async with session.post(
                    "http://127.0.0.1:{0}/intent".format(port),
                    json={"intent_id": "x", "goal": "y"},
                ) as response:
                    self.assertEqual(response.status, 401)
            await bridge_instance._stop_intent_server()
            return data, feedback

        data, feedback = asyncio.run(scenario())
        self.assertTrue(data["accepted"])
        self.assertEqual(feedback["current"]["intent_id"], "i-http")


class ActionHoldTests(unittest.TestCase):
    """意图活跃期在模型推理空窗内按 5fps 重发最近动作块。"""

    @staticmethod
    def _instance():
        """构造带伪 WebSocket 的 v2 桥接实例。"""
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()
        return instance

    def test_active_intent_resends_last_action_block(self):
        """同一 200ms 窗口只续按一次；新模型块到达后立即替换续按内容。"""
        instance = self._instance()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "hold", "goal": "前进"})
            await instance._handle_lumine_message(json.dumps({
                "type": "action", "intentId": "hold", "motorGeneration": 1,
                "chunks": [_chunk(keys=["W"]) for _ in range(6)],
            }), source_ws=instance._lumine_ws)
            first = json.loads(instance._bettergi_ws.sent[-1])
            self.assertEqual((first["type"], first["thought"]), ("actions", None))
            self.assertEqual(instance.stats.actions_held, 0)
            self.assertEqual(instance._held_action["attribution"], instance._current_attribution())
            # 模拟一次模型推理空窗，不实际等待 200ms。
            instance._held_action_at -= bridge.ACTION_HOLD_INTERVAL_SECONDS * 2
            await instance._action_hold_tick()
            sent = [json.loads(item) for item in instance._bettergi_ws.sent]
            self.assertEqual(len(sent), 2)
            repeated = sent[-1]
            self.assertEqual(repeated["type"], "actions")
            self.assertEqual(repeated["seq"], first["seq"] + 1)
            self.assertEqual(repeated["chunks"], first["chunks"])
            self.assertIsNone(repeated["thought"])
            self.assertEqual(repeated["intentId"], "hold")
            self.assertEqual(repeated["motorGeneration"], 1)
            self.assertEqual(repeated["lease"], first["lease"])
            self.assertEqual(instance.stats.actions_held, 1)
            # 同一 200ms 窗口内不得重复发送。
            await instance._action_hold_tick()
            self.assertEqual(len(instance._bettergi_ws.sent), 2)
            # 新模型块立即替换最近块，后续重复新块。
            await instance._handle_lumine_message(json.dumps({
                "type": "action", "intentId": "hold", "motorGeneration": 1,
                "chunks": [_chunk(keys=["D"]) for _ in range(6)],
            }), source_ws=instance._lumine_ws)
            second = json.loads(instance._bettergi_ws.sent[-1])
            self.assertNotEqual(second["chunks"], first["chunks"])
            instance._held_action_at -= bridge.ACTION_HOLD_INTERVAL_SECONDS * 2
            await instance._action_hold_tick()
            latest = json.loads(instance._bettergi_ws.sent[-1])
            self.assertEqual(latest["chunks"], second["chunks"])
            self.assertEqual(latest["seq"], second["seq"] + 1)
            self.assertEqual(instance.stats.actions_held, 2)
            forwarded = [row for row in instance.intent_feedback if row["reason"] == "action_forwarded"]
            self.assertEqual(len(forwarded), 2)

        asyncio.run(scenario())

    def test_clear_intent_stops_resend_and_releases_held_block(self):
        """意图结束或取消后不得再发送任何续按动作。"""
        instance = self._instance()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "hold", "goal": "前进"})
            await instance._handle_lumine_message(json.dumps({
                "type": "action", "intentId": "hold", "motorGeneration": 1,
                "chunks": [_chunk(keys=["W"]) for _ in range(6)],
            }), source_ws=instance._lumine_ws)
            self.assertIsNotNone(instance._held_action)
            await instance.clear_intent("cancelled")
            self.assertIsNone(instance.latest_intent)
            self.assertIsNone(instance._held_action)
            self.assertIsNone(instance._held_action_at)
            count = len(instance._bettergi_ws.sent)
            await instance._action_hold_tick()
            self.assertEqual(len(instance._bettergi_ws.sent), count)
            sent = [json.loads(item) for item in instance._bettergi_ws.sent]
            self.assertEqual([item["type"] for item in sent], ["actions", "stop_actions"])
            self.assertEqual(instance.stats.actions_held, 0)

        asyncio.run(scenario())

    def test_expired_lease_stops_resend_and_revokes_intent(self):
        """租约失效时立即停止续按并撤销意图。"""
        instance = self._instance()

        async def scenario():
            await _bind_active_v2(instance)
            await instance.set_intent({"intent_id": "hold", "goal": "前进"})
            await instance._handle_lumine_message(json.dumps({
                "type": "action", "intentId": "hold", "motorGeneration": 1,
                "chunks": [_chunk(keys=["W"]) for _ in range(6)],
            }), source_ws=instance._lumine_ws)
            instance._lease_binding["lease_deadline"] = 0.0
            instance._held_action_at -= bridge.ACTION_HOLD_INTERVAL_SECONDS * 2
            await instance._action_hold_tick()
            self.assertIsNone(instance.latest_intent)
            self.assertIsNone(instance._held_action)
            sent = [json.loads(item) for item in instance._bettergi_ws.sent]
            self.assertEqual([item["type"] for item in sent], ["actions", "stop_actions"])
            self.assertEqual(json.loads(instance._lumine_ws.sent[-1])["type"], "cancel_intent")
            self.assertEqual(instance.stats.actions_held, 0)

        asyncio.run(scenario())


class MotorCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2", lumine_token="secret"))
        self.socket = _FakeWebSocket()
        self.instance._lumine_ws = self.socket
        self.instance._motor_connection_generation = 1
        self.instance._motor_connection_authenticated = True
        self.instance._motor_connected_origin = bridge._motor_origin(self.instance.config.lumine_ws)
        self.instance._motor_connected_endpoint = bridge._motor_endpoint_identity(
            self.instance.config.lumine_ws, self.instance.config.lumine_token)
        self.instance._motor_connected_auth = bridge._motor_auth_identity(
            self.instance.config.lumine_ws, self.instance.config.lumine_token)

    async def challenge(self):
        async with self.instance._intent_lock:
            await self.instance._request_motor_capability_locked()
        return json.loads(self.socket.sent[-1])

    @staticmethod
    def ack(query, **overrides):
        return {"type": "motor_capability_ack", "nonce": query["nonce"],
                "connectionGeneration": query["connectionGeneration"], "protocol": "lumine-live-v2",
                "intentRequired": True, "intentForcesMotor": True, "mock": False, **overrides}

    async def test_only_valid_self_report_on_current_connection_is_visible(self):
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        query = await self.challenge()
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        await self.instance._handle_lumine_message(json.dumps(self.ack(query)), source_ws=self.socket)
        snapshot = self.instance.intent_snapshot()
        self.assertEqual(snapshot["motorPromptProtocol"], "v2_requires_motor_prompt")
        self.assertEqual(snapshot["motorPromptEvidence"], "server_self_report")
        self.socket.closed = True
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())

    async def test_old_server_mock_or_unauthenticated_never_promotes(self):
        query = await self.challenge()
        await self.instance._handle_lumine_message(json.dumps({"type": "hello"}), source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        await self.instance._handle_lumine_message(json.dumps(self.ack(query, mock=True)), source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        self.instance._motor_connection_authenticated = False
        await self.challenge()
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())

    async def test_stale_ack_generation_nonce_and_expiry_are_rejected(self):
        old = await self.challenge()
        old_socket = self.socket
        self.socket = _FakeWebSocket()
        self.instance._lumine_ws = self.socket
        self.instance._motor_connection_generation += 1
        fresh = await self.challenge()
        await self.instance._handle_lumine_message(json.dumps(self.ack(old)), source_ws=old_socket)
        await self.instance._handle_lumine_message(json.dumps(self.ack(old)), source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        await self.instance._handle_lumine_message(json.dumps(self.ack(fresh, connectionGeneration=True)), source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        fresh = await self.challenge()
        self.instance._motor_capability["deadline"] = bridge.time.monotonic() - 1
        await self.instance._handle_lumine_message(json.dumps(self.ack(fresh)), source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        refreshed = await self.challenge()
        self.assertNotEqual(fresh["nonce"], refreshed["nonce"])
        await self.instance._handle_lumine_message(json.dumps(self.ack(refreshed)), source_ws=self.socket)
        self.assertEqual(self.instance.intent_snapshot()["motorPromptEvidence"], "server_self_report")

    @unittest.skipUnless(AIOHTTP_AVAILABLE, "aiohttp 未安装")
    async def test_connection_loop_challenges_and_clears_on_disconnect(self):
        seen = []
        instance = self.instance

        class Socket(_FakeWebSocket):
            def __aiter__(self):
                async def incoming():
                    query = next(json.loads(text) for text in self.sent
                                 if json.loads(text).get("type") == "motor_capability_query")
                    yield mock.Mock(type=aiohttp.WSMsgType.TEXT, data=json.dumps(MotorCapabilityTests.ack(query)))
                    seen.append(instance.intent_snapshot())
                return incoming()

        socket = Socket()

        class Connection:
            async def __aenter__(self):
                return socket
            async def __aexit__(self, *args):
                instance._running = False

        class Session:
            def ws_connect(self, *args, **kwargs):
                return Connection()

        instance._session = Session()
        instance._running = True
        await instance._lumine_loop()
        self.assertEqual(seen[0]["motorPromptEvidence"], "server_self_report")
        self.assertIsNone(instance._motor_capability)
        self.assertNotIn("motorPromptProtocol", instance.intent_snapshot())

    async def test_transport_errors_invalidate_capability(self):
        query = await self.challenge()
        await self.instance._handle_lumine_message(json.dumps(self.ack(query)), source_ws=self.socket)
        self.assertIn("motorPromptProtocol", self.instance.intent_snapshot())
        await self.instance._handle_lumine_message(
            json.dumps({"type": "error", "code": "unknown_type"}), source_ws=self.socket)
        self.assertIsNone(self.instance._motor_capability)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())

    async def test_duplicate_json_keys_and_unexpected_fields_do_not_promote(self):
        query = await self.challenge()
        raw = json.dumps(self.ack(query))[:-1] + ', "mock": false}'
        await self.instance._handle_lumine_message(raw, source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())
        query = await self.challenge()
        await self.instance._handle_lumine_message(json.dumps(self.ack(query, unexpected=1)), source_ws=self.socket)
        self.assertNotIn("motorPromptProtocol", self.instance.intent_snapshot())


class MotorSafetyTests(unittest.IsolatedAsyncioTestCase):
    """安全门仅验证本地协议行为，不启动游戏或外部服务。"""

    async def test_bus_requires_token_and_literal_loopback(self):
        for host, token in (("0.0.0.0", "secret"), ("192.168.1.1", "secret"),
                            ("localhost", "secret"), ("127.0.0.1", ""),
                            ("::1", "   ")):
            instance = bridge.LumineBridge(bridge.BridgeConfig(
                motor_protocol="v2", intent_port=1234, intent_host=host, intent_token=token))
            with self.subTest(host=host, token=token), self.assertRaises(ValueError):
                await instance._start_intent_server()
            self.assertFalse(instance._intent_started)
        # v1 does not turn on the bus even with unsafe bus settings.
        instance = bridge.LumineBridge(bridge.BridgeConfig(intent_port=1234, intent_host="0.0.0.0"))
        await instance._start_intent_server()
        self.assertFalse(instance._intent_started)

    async def test_bus_handlers_require_matching_bearer_without_listening(self):
        class Router:
            def __init__(self):
                self.handlers = {}
            def add_post(self, path, handler):
                self.handlers[("POST", path)] = handler
            def add_get(self, path, handler):
                self.handlers[("GET", path)] = handler

        class Application:
            def __init__(self):
                self.router = Router()

        class Runner:
            def __init__(self, app):
                self.app = app
            async def setup(self):
                pass
            async def cleanup(self):
                pass

        class Site:
            def __init__(self, runner, host, port):
                pass
            async def start(self):
                pass

        class Request:
            def __init__(self, authorization):
                self.headers = {"Authorization": authorization}
            async def json(self):
                return {"intent_id": "i", "goal": "go"}

        web = mock.Mock(Application=Application, AppRunner=Runner, TCPSite=Site,
                        json_response=lambda data, status=200: (status, data))
        instance = bridge.LumineBridge(bridge.BridgeConfig(
            motor_protocol="v2", intent_port=1234, intent_host="127.0.0.1", intent_token="secret"))
        instance._lumine_ws = _FakeWebSocket()
        await _bind_active_v2(instance)
        instance.config.intent_port = 1234
        instance.config.intent_token = "secret"
        with mock.patch.object(bridge, "aiohttp_web", web):
            await instance._start_intent_server()
            router = instance._intent_runner.app.router
            for method, path in (("POST", "/intent"), ("GET", "/feedback")):
                handler = router.handlers[(method, path)]
                self.assertEqual((await handler(Request("")))[0], 401)
                self.assertEqual((await handler(Request("Bearer wrong")))[0], 401)
                self.assertEqual((await handler(Request("Bearer secret")))[0], 200)
            await instance._stop_intent_server()

    async def test_hello_reissues_unbound_stop_for_verified_session(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        await instance.clear_intent("connected_before_hello")
        first = next(iter(instance._stop_requests.values()))
        self.assertIsNone(first["sessionId"])
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live-a"}),
                                                source_ws=instance._bettergi_ws)
        requests = list(instance._stop_requests.values())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1]["sessionId"], "live-a")
        self.assertGreater(requests[-1]["motorGeneration"], first["motorGeneration"])
        self.assertTrue(instance._release_unconfirmed)

    async def test_live_restart_binds_new_stop_and_rejects_old_receipt(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        socket = instance._bettergi_ws
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "old"}), source_ws=socket)
        await instance.clear_intent("old")
        old_id = next(iter(instance._stop_requests))
        await instance._handle_bettergi_message(json.dumps({"type": "live.started", "sessionId": "new"}), source_ws=socket)
        new_request = list(instance._stop_requests.values())[-1]
        self.assertEqual(new_request["sessionId"], "new")
        stale = {"type": "stop_actions_ack", "sessionId": "old", "stopRequestId": old_id,
                 "stopCallReturned": True, "safety": {"scope": "stop_atomic", "epoch": 1,
                 "trackedKeyCount": 0, "inputReleaseCallsReturned": True,
                 "inputReleaseConfirmed": None, "systemReleaseRequestsAccepted": True}}
        await instance._handle_bettergi_message(json.dumps(stale), source_ws=socket)
        self.assertIsNone(instance._stop_requests[old_id]["ack"])
        # 会话切换本身会复位标记；旧回执必须被忽略，不能凭它解锁。
        self.assertFalse(instance._release_unconfirmed)

    async def test_same_session_name_restart_invalidates_old_stop_receipt(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        socket = instance._bettergi_ws
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "same"}), source_ws=socket)
        old = await instance.clear_intent("old")
        await instance._handle_bettergi_message(json.dumps({"type": "live.started", "sessionId": "same"}), source_ws=socket)
        receipt = {"type": "stop_actions_ack", "sessionId": "same", "stopRequestId": old["stopRequestId"],
                   "stopCallReturned": True, "safety": {"scope": "stop_atomic", "epoch": 2,
                   "trackedKeyCount": 0, "inputReleaseCallsReturned": True,
                   "inputReleaseConfirmed": None, "systemReleaseRequestsAccepted": True}}
        await instance._handle_bettergi_message(json.dumps(receipt), source_ws=socket)
        self.assertIsNone(instance._stop_requests[old["stopRequestId"]]["ack"])
        # 同名会话重启同样复位；旧回执仍被忽略。
        self.assertFalse(instance._release_unconfirmed)

    async def test_atomic_stop_receipt_requires_same_session_and_connection(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        socket = instance._bettergi_ws
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live-a"}), source_ws=socket)
        await instance.clear_intent("test")
        request_id = next(iter(instance._stop_requests))
        receipt = {"type": "stop_actions_ack", "sessionId": "live-a", "stopRequestId": request_id,
                   "stopCallReturned": True, "safety": {"scope": "stop_atomic", "epoch": 4,
                   "trackedKeyCount": 1, "inputReleaseCallsReturned": True,
                   "inputReleaseConfirmed": None, "systemReleaseRequestsAccepted": True}}
        for change in ({"sessionId": "live-b"}, {"stopRequestId": "other"}):
            await instance._handle_bettergi_message(json.dumps({**receipt, **change}), source_ws=socket)
            self.assertIsNone(instance._stop_requests[request_id]["ack"])
        invalid = {**receipt, "safety": {**receipt["safety"], "systemReleaseRequestsAccepted": 1}}
        await instance._handle_bettergi_message(json.dumps(invalid), source_ws=socket)
        await instance._handle_bettergi_message(json.dumps(receipt), source_ws=_FakeWebSocket())
        self.assertIsNone(instance._stop_requests[request_id]["ack"])
        await instance._handle_bettergi_message(json.dumps(receipt), source_ws=socket)
        self.assertIs(instance._stop_requests[request_id]["ack"]["safety"]["systemReleaseRequestsAccepted"], True)
        await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": 1}), source_ws=instance._lumine_ws)
        self.assertTrue(instance._release_unconfirmed)
        self.assertIsNone(await instance.set_intent({"intent_id": "new", "goal": "go"}))

    async def test_stop_receipt_history_is_bounded_and_old_ack_cannot_unlock(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        socket = instance._bettergi_ws
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live"}), source_ws=socket)
        first = None
        with mock.patch.object(bridge, "MAX_STOP_REQUEST_HISTORY", 2):
            for _ in range(3):
                result = await instance.clear_intent("test")
                first = first or result["stopRequestId"]
        self.assertEqual(len(instance._stop_requests), 2)
        self.assertNotIn(first, instance._stop_request_connections)
        receipt = {"type": "stop_actions_ack", "sessionId": "live", "stopRequestId": first,
                   "stopCallReturned": True, "safety": {"scope": "stop_atomic", "epoch": 1,
                   "trackedKeyCount": 0, "inputReleaseCallsReturned": True,
                   "inputReleaseConfirmed": None, "systemReleaseRequestsAccepted": True}}
        await instance._handle_bettergi_message(json.dumps(receipt), source_ws=socket)
        self.assertTrue(instance._release_unconfirmed)
        self.assertTrue(all(row["ack"] is None for row in instance._stop_requests.values()))

    async def test_live_lease_binding_is_correlated_and_never_forwarded_to_motor(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        _configure_motor_v2(instance)
        socket = instance._bettergi_ws
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live"}), source_ws=socket)
        secret = "A" * 64
        credentials = {"secret": secret, "instanceId": "a" * 32, "epoch": 2}
        request_id = await instance.bind_live_lease({"sessionId": "live", "credentials": credentials})
        sent = json.loads(socket.sent[-1])
        self.assertEqual(sent, {"type": "lease.bind", "leaseRequestId": request_id, "lease": credentials})
        self.assertNotIn(secret, json.dumps(instance.intent_snapshot()))
        ack = {"type": "lease.bound", "leaseRequestId": request_id, "leaseInstanceId": "a" * 32, "leaseEpoch": 2,
               "sessionId": "live", "leaseConnectionId": "c" * 32, "leaseStatus": "prepared",
               "leaseProbeId": request_id, "leaseExpiresInMs": 3000}
        for modified in ({"leaseRequestId": "b" * 32}, {"leaseEpoch": 3}, {"leaseInstanceId": "c" * 32},
                         {"extra": "untrusted"}):
            await instance._handle_bettergi_message(json.dumps({**ack, **modified}), source_ws=socket)
        await instance._handle_bettergi_message(json.dumps(ack), source_ws=_FakeWebSocket())
        self.assertEqual(instance.intent_snapshot()["liveLeaseBinding"]["status"], "pending")
        self.assertIsNone(await instance.set_intent({"intent_id": "x", "goal": "go"}))
        await instance._handle_bettergi_message(json.dumps(ack), source_ws=socket)
        self.assertEqual(instance.intent_snapshot()["liveLeaseBinding"]["status"], "prepared_bound")
        self.assertIsNone(await instance.set_intent({"intent_id": "x", "goal": "go"}))
        import socket as socket_module
        with socket_module.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            instance.config.intent_port = listener.getsockname()[1]
        instance.config.intent_token = "fixture-bus-token"
        await instance._start_intent_server()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(f"http://127.0.0.1:{instance.config.intent_port}/lease/commit",
                    headers={"Authorization": "Bearer fixture-bus-token"}, json={"requestId": request_id,
                    "sessionId": "live", "instanceId": "a" * 32, "epoch": 2,
                    "connectionId": "c" * 32, "motorConnectionGeneration": 1}) as response:
                    self.assertEqual((await response.json())["status"], "commit_pending")
            ack.update(leaseStatus="active", leaseProbeId=instance._lease_binding["probe_id"])
            await instance._handle_bettergi_message(json.dumps(ack), source_ws=socket)
        finally:
            await instance._stop_intent_server()
        self.assertEqual(instance.intent_snapshot()["liveLeaseBinding"]["status"], "bound")
        self.assertIsNotNone(await instance.set_intent({"intent_id": "x", "goal": "go"}))
        self.assertNotIn(secret, "".join(instance._lumine_ws.sent))
        await instance._handle_lumine_message(json.dumps({"type": "action", "intentId": "x",
            "motorGeneration": 1, "chunks": [_chunk(keys=["W"])]}), source_ws=instance._lumine_ws)
        self.assertEqual(json.loads(socket.sent[-1])["lease"], credentials)
        self.assertNotIn(secret, json.dumps(instance.intent_snapshot()))
        await instance.clear_intent("test")
        self.assertNotIn("liveLeaseBinding", instance.intent_snapshot())

    async def test_lease_binding_http_requires_bearer_and_rejects_ambiguous_json(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2", intent_port=port,
                                                           intent_host="127.0.0.1", intent_token="bus-secret"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        _configure_motor_v2(instance)
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live"}),
                                                source_ws=instance._bettergi_ws)
        await instance._start_intent_server()
        url = f"http://127.0.0.1:{port}/lease/bind"
        payload = {"sessionId": "live", "credentials": {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}}
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(url, json=payload) as reply:
                    self.assertEqual(reply.status, 401)
                async with client.post(url, data='{"sessionId":"live","sessionId":"live","credentials":{}}',
                                       headers={"Authorization": "Bearer bus-secret"}) as reply:
                    self.assertEqual(reply.status, 400)
                async with client.post(url, json=payload, headers={"Authorization": "Bearer bus-secret"}) as reply:
                    self.assertEqual(reply.status, 200)
                    body = await reply.json()
                    self.assertEqual(body["status"], "pending")
                    self.assertNotIn(payload["credentials"]["secret"], json.dumps(body))
                instance._lease_binding["probe_sent_at"] -= 1.1
                async with client.get(f"http://127.0.0.1:{port}/feedback",
                                      headers={"Authorization": "Bearer bus-secret"}) as reply:
                    self.assertEqual((await reply.json())["liveLeaseBinding"]["status"], "pending")
                probe = json.loads(instance._bettergi_ws.sent[-1])
                self.assertEqual(probe["type"], "lease.probe")
                self.assertEqual(probe["leaseRequestId"], body["requestId"])
                self.assertEqual(probe["leaseProbeId"], instance._lease_binding["probe_id"])
                self.assertNotIn(payload["credentials"]["secret"], json.dumps(probe))
        finally:
            await instance._stop_intent_server()

    async def test_binding_abandon_is_correlated_and_never_releases_newer_attempt(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2", intent_port=port,
                                                           intent_host="127.0.0.1", intent_token="bus-secret"))
        instance._bettergi_ws, instance._lumine_ws = _FakeWebSocket(), _FakeWebSocket()
        _configure_motor_v2(instance)
        await instance._handle_bettergi_message(json.dumps({"type": "hello", "sessionId": "live"}),
                                                source_ws=instance._bettergi_ws)
        await instance._start_intent_server()
        origin = f"http://127.0.0.1:{port}"
        headers = {"Authorization": "Bearer bus-secret"}
        payload = {"sessionId": "live", "requestId": "a" * 32,
                   "credentials": {"secret": "A" * 64, "instanceId": "a" * 32, "epoch": 1}}
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(origin + "/lease/bind", json=payload, headers=headers) as reply:
                    self.assertEqual((await reply.json())["requestId"], payload["requestId"])
                async with client.post(origin + "/lease/abandon", json={"requestId": "b" * 32}, headers=headers) as reply:
                    self.assertEqual(reply.status, 409)
                self.assertEqual(instance.intent_snapshot()["liveLeaseBinding"]["requestId"], payload["requestId"])
                async with client.post(origin + "/lease/abandon", json={"requestId": payload["requestId"]}, headers=headers) as reply:
                    data = await reply.json()
                    self.assertTrue(data["abandoned"] and data["stopRequested"])
                self.assertNotIn("liveLeaseBinding", instance.intent_snapshot())
                self.assertTrue(instance._release_unconfirmed)
                async with client.post(origin + "/lease/abandon", json={"requestId": payload["requestId"]}, headers=headers) as reply:
                    self.assertEqual(reply.status, 409)
        finally:
            await instance._stop_intent_server()

    async def test_cancel_ack_is_bound_to_motor_connection_and_request(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        result = await instance.clear_intent("test")
        request = instance._stop_requests[result["stopRequestId"]]
        payload = json.dumps({"type": "cancel_ack", "motorGeneration": result["motorGeneration"]})
        await instance._handle_lumine_message(payload, source_ws=_FakeWebSocket())
        await instance._handle_lumine_message(payload)
        await instance._handle_lumine_message('{"type":"cancel_ack","motorGeneration":1,"motorGeneration":1}',
                                              source_ws=instance._lumine_ws)
        self.assertFalse(request["motorCancelAcknowledged"])
        self.assertEqual(instance._pending_cancel_generation, result["motorGeneration"])
        await instance._handle_lumine_message(payload, source_ws=instance._lumine_ws)
        self.assertTrue(request["motorCancelAcknowledged"])
        self.assertIsNone(instance._pending_cancel_generation)
        self.assertTrue(instance._release_unconfirmed)

    async def test_cancel_ack_early_stale_and_missing(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._bettergi_ws = _FakeWebSocket()
        instance._lumine_ws = _FakeWebSocket()
        await _bind_active_v2(instance)
        await instance.set_intent({"intent_id": "old", "goal": "go"})
        entered, release = asyncio.Event(), asyncio.Event()
        original = instance._send_lumine

        async def blocked_send(payload):
            if payload["type"] == "cancel_intent":
                entered.set()
                await release.wait()
            return await original(payload)

        instance._send_lumine = blocked_send
        cancelling = asyncio.create_task(instance.clear_intent("test"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        ack = asyncio.create_task(instance._handle_lumine_message(
            json.dumps({"type": "cancel_ack", "motorGeneration": 2}), source_ws=instance._lumine_ws))
        await asyncio.sleep(0)
        self.assertEqual(instance._pending_cancel_generation, 2)
        release.set()
        await asyncio.gather(cancelling, ack)
        self.assertIsNone(instance._pending_cancel_generation)
        self.assertIsNone(await instance.set_intent({"intent_id": "new", "goal": "go"}))
        await instance.clear_intent("second")
        self.assertIsNone(await instance.set_intent({"intent_id": "blocked", "goal": "go"}))
        for generation in (2, True, "3", 4):
            await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": generation}), source_ws=instance._lumine_ws)
            self.assertEqual(instance._pending_cancel_generation, 3)
        await instance._handle_lumine_message(json.dumps({"type": "cancel_ack", "motorGeneration": 3}), source_ws=instance._lumine_ws)
        self.assertIsNone(instance._pending_cancel_generation)
        self.assertTrue(instance._release_unconfirmed)
        self.assertIsNone(await instance.set_intent({"intent_id": "allowed", "goal": "go"}))

    async def test_wait_does_not_forward_even_idle_actions(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()
        await _bind_active_v2(instance)
        await instance.set_intent({"intent_id": "i", "goal": "wait", "motor_mode": "wait"})
        await instance._handle_lumine_message(json.dumps({"type": "action", "intentId": "i", "motorGeneration": 1, "chunks": [_chunk(keys=[])]}))
        self.assertEqual(instance._bettergi_ws.sent, [])
        self.assertEqual(instance.stats.actions_out, 0)
        self.assertEqual([row["reason"] for row in instance.intent_feedback], ["intent_sent"])

    async def test_dialogue_permissions_are_explicit_and_wait_is_idle(self):
        idle = {"absolute": None, "chunks": [_chunk(keys=[])]}
        for action in ({"chunks": [_chunk(keys=["LB"])]},
                       {"chunks": [_chunk(keys=["Space"])]},
                       {"chunks": [_chunk(dx=1, keys=[])]},
                       {"absolute": {"x": 0, "y": 0}, "chunks": [_chunk(keys=[])]},
                       {"chunks": [_chunk(scroll=1, keys=[])]}):
            for permissions in (None, {}, {"allow_skip": True}, {"allow_select": True},
                                {"allow_skip": "true", "allow_select": True}):
                self.assertFalse(bridge.action_matches_motor_mode(action, "dialogue", permissions))
            self.assertFalse(bridge.action_matches_motor_mode(action, "dialogue", {"allow_skip": True, "allow_select": True}))
            self.assertFalse(bridge.action_matches_motor_mode(action, "wait", {"allow_skip": True, "allow_select": True}))
        self.assertTrue(bridge.action_matches_motor_mode(idle, "dialogue"))
        self.assertTrue(bridge.action_matches_motor_mode(idle, "wait"))
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()
        await _bind_active_v2(instance)
        self.assertIsNone(await instance.set_intent({"intent_id": "i", "goal": "talk", "motor_mode": "dialogue", "dialogue_permissions": {"allow_skip": "true"}}))
        await instance.set_intent({"intent_id": "i", "goal": "talk", "motor_mode": "dialogue"})
        await instance._handle_lumine_message(json.dumps({"type": "action", "intentId": "i", "motorGeneration": 1, "chunks": [_chunk(keys=["LB"])]}))
        self.assertEqual([json.loads(x)["type"] for x in instance._bettergi_ws.sent], ["stop_actions"])
        self.assertEqual(instance.intent_feedback[-1]["reason"], "motor_mode_violation")
        self.assertIsNone(instance.latest_intent)

    async def test_invalid_mode_and_identity_are_not_coerced(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        for intent in ({"intent_id": " i ", "goal": "go"},
                       {"intent_id": "x" * 81, "goal": "go"},
                       {"intent_id": "i", "goal": "go", "motor_mode": "future"},
                       {"intent_id": "i", "goal": "go", "motor_mode": ""}):
            self.assertIsNone(await instance.set_intent(intent))
        self.assertEqual(instance._lumine_ws.sent, [])

    async def test_replacement_feedback_keeps_previous_generation(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()
        await _bind_active_v2(instance)
        await instance.set_intent({"intent_id": "same", "goal": "one", "actionGroupId": "old"})
        self.assertIsNone(await instance.set_intent({"intent_id": "same", "goal": "two", "actionGroupId": "new"}))
        old = instance.intent_feedback[-1]
        self.assertEqual((old["status"], old["reason"], old["motorGeneration"], old["actionGroupId"]),
                         ("cancelled", "replaced_by_new_intent", 1, "old"))
        self.assertIs(old["executionConfirmed"], False)
        self.assertTrue(instance._release_unconfirmed)
        self.assertFalse(any(row.get("actionGroupId") == "new" for row in instance.intent_feedback))

    async def test_delayed_ack_cannot_be_attributed_to_replacement(self):
        instance = bridge.LumineBridge(bridge.BridgeConfig(motor_protocol="v2"))
        instance._lumine_ws = _FakeWebSocket()
        instance._bettergi_ws = _FakeWebSocket()
        await _bind_active_v2(instance)
        await instance.set_intent({"intent_id": "same", "goal": "one"})
        await instance._intent_lock.acquire()
        ack = asyncio.create_task(instance._handle_lumine_message(json.dumps({
            "type": "intent_ack", "intentId": "same", "motorGeneration": 1, "accepted": True})))
        await asyncio.sleep(0)
        instance._motor_generation = 2
        instance.latest_intent["goal"] = "two"
        instance._intent_lock.release()
        await ack
        self.assertFalse(any(x["reason"] == "intent_ack" for x in instance.intent_feedback))


if __name__ == "__main__":
    unittest.main()
