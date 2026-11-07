#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机确定性 Motor 的协议与安全边界测试。

运行方式（无需 pytest）：
    python -m unittest discover -s integrations/lumine/tests -v
"""

import asyncio
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LUMINE_DIR = os.path.dirname(HERE)
if LUMINE_DIR not in sys.path:
    sys.path.insert(0, LUMINE_DIR)

import local_motor  # noqa: E402

try:
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed

    WS_AVAILABLE = True
except ImportError:  # pragma: no cover
    WS_AVAILABLE = False

TOKEN = "local-motor-test-token-0123456789"
PATH = "/ws/player"


def _intent(mode="navigate", intent_id="intent-1", generation=1, **extra):
    message = {
        "type": "intent",
        "intent_id": intent_id,
        "goal": "walk forward",
        "motor_mode": mode,
        "deadline_ms": 600,
        "intentId": intent_id,
        "motorGeneration": generation,
    }
    message.update(extra)
    return message


async def _server():
    handler = local_motor.build_handler(TOKEN, PATH)
    server = await serve(handler, "127.0.0.1", 0, max_size=4 * 1024 * 1024)
    port = server.sockets[0].getsockname()[1]
    return server, port


@unittest.skipUnless(WS_AVAILABLE, "websockets 不可用")
class LocalMotorSocketTests(unittest.TestCase):
    """通过真实 WebSocket 验证握手、归属与取消。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_rejects_missing_authorization(self):
        async def scenario():
            server, port = await _server()
            try:
                async with connect(f"ws://127.0.0.1:{port}{PATH}") as ws:
                    with self.assertRaises(ConnectionClosed) as raised:
                        await ws.recv()
                return raised.exception.rcvd.code if raised.exception.rcvd else None
            finally:
                server.close()
                await server.wait_closed()

        self.assertEqual(self._run(scenario()), 1008)

    def test_capability_ack_matches_bridge_contract(self):
        async def scenario():
            server, port = await _server()
            headers = {"Authorization": "Bearer " + TOKEN}
            try:
                async with connect(f"ws://127.0.0.1:{port}{PATH}", additional_headers=headers) as ws:
                    await ws.send(json.dumps({"type": "motor_capability_query",
                                              "nonce": "abc", "connectionGeneration": 3}))
                    return json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            finally:
                server.close()
                await server.wait_closed()

        ack = self._run(scenario())
        self.assertEqual(set(ack), {"type", "nonce", "connectionGeneration", "protocol",
                                    "intentRequired", "intentForcesMotor", "mock"})
        self.assertEqual(ack["type"], "motor_capability_ack")
        self.assertEqual(ack["nonce"], "abc")
        self.assertEqual(ack["connectionGeneration"], 3)
        self.assertEqual(ack["protocol"], "lumine-live-v2")
        self.assertIs(ack["intentRequired"], True)
        self.assertIs(ack["intentForcesMotor"], True)
        self.assertIs(ack["mock"], False)

    def test_navigate_intent_emits_attributed_actions(self):
        async def scenario():
            server, port = await _server()
            headers = {"Authorization": "Bearer " + TOKEN}
            try:
                async with connect(f"ws://127.0.0.1:{port}{PATH}", additional_headers=headers) as ws:
                    await ws.send(json.dumps(_intent("navigate")))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    action = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    return ack, action
            finally:
                server.close()
                await server.wait_closed()

        ack, action = self._run(scenario())
        self.assertEqual(ack["type"], "intent_ack")
        self.assertIs(ack["accepted"], True)
        self.assertEqual(ack["intentId"], "intent-1")
        self.assertEqual(ack["motorGeneration"], 1)
        self.assertEqual(action["type"], "action")
        self.assertEqual(action["intentId"], "intent-1")
        self.assertEqual(action["motorGeneration"], 1)
        self.assertEqual(len(action["chunks"]), 6)
        self.assertEqual(action["chunks"][0]["keys"], ["W"])
        self.assertIsNone(action["absolute"])
        # 只有第一块携带位移；其余块必须为零，避免重复推进。
        for chunk in action["chunks"][1:]:
            self.assertEqual((chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0))

    def test_wait_mode_never_emits_input(self):
        async def scenario():
            server, port = await _server()
            headers = {"Authorization": "Bearer " + TOKEN}
            try:
                async with connect(f"ws://127.0.0.1:{port}{PATH}", additional_headers=headers) as ws:
                    await ws.send(json.dumps(_intent("wait", intent_id="intent-wait")))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), timeout=0.6)
                    return ack
            finally:
                server.close()
                await server.wait_closed()

        ack = self._run(scenario())
        self.assertEqual(ack["type"], "intent_ack")

    def test_cancel_stops_further_actions(self):
        async def scenario():
            server, port = await _server()
            headers = {"Authorization": "Bearer " + TOKEN}
            try:
                async with connect(f"ws://127.0.0.1:{port}{PATH}", additional_headers=headers) as ws:
                    await ws.send(json.dumps(_intent("combat", intent_id="intent-c", generation=7)))
                    await ws.recv()  # intent_ack
                    await ws.recv()  # 至少一个 action
                    await ws.send(json.dumps({"type": "cancel_intent", "motorGeneration": 7}))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), timeout=0.6)
                    return ack
            finally:
                server.close()
                await server.wait_closed()

        ack = self._run(scenario())
        self.assertEqual(ack, {"type": "cancel_ack", "motorGeneration": 7})

    def test_bad_attribution_is_refused(self):
        async def scenario():
            server, port = await _server()
            headers = {"Authorization": "Bearer " + TOKEN}
            try:
                async with connect(f"ws://127.0.0.1:{port}{PATH}", additional_headers=headers) as ws:
                    await ws.send(json.dumps({"type": "intent", "motor_mode": "combat",
                                              "intentId": "intent-x", "motorGeneration": True}))
                    return json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            finally:
                server.close()
                await server.wait_closed()

        error = self._run(scenario())
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["code"], "bad_intent_attribution")


class ChunkShapeTests(unittest.TestCase):
    def test_single_press_only_uses_first_chunk(self):
        chunks = local_motor.build_chunks(("F",), single_press=True)
        self.assertEqual(len(chunks), 6)
        self.assertEqual(chunks[0]["keys"], ["F"])
        self.assertTrue(all(chunk["keys"] == [] for chunk in chunks[1:]))

    def test_held_keys_repeat_across_chunks(self):
        chunks = local_motor.build_chunks(("W",))
        self.assertTrue(all(chunk["keys"] == ["W"] for chunk in chunks))

    def test_attribution_requires_integer_generation(self):
        self.assertIsNone(local_motor.attribution_of({"intentId": "a", "motorGeneration": True}))
        self.assertIsNone(local_motor.attribution_of({"intentId": "", "motorGeneration": 0}))
        self.assertEqual(local_motor.attribution_of({"intentId": "a", "motorGeneration": 0}),
                         {"intentId": "a", "motorGeneration": 0})


if __name__ == "__main__":
    unittest.main()
