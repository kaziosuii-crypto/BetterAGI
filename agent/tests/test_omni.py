"""Omni 快系统客户端测试：不联网，全部用 ScriptedTransport 驱动。"""

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import omni  # noqa: E402


JPEG = b"\xff\xd8\xff\xe0" + bytes(64)


class ConfigTests(unittest.TestCase):
    def test_url_uses_workspace_and_model(self):
        cfg = omni.OmniConfig(api_key="k", workspace_id="ws1")
        self.assertEqual(
            cfg.resolve_url(),
            "wss://ws1.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?model=qwen3.8-omni-flash-realtime",
        )

    def test_base_url_gets_query_separator(self):
        cfg = omni.OmniConfig(api_key="k", base_url="wss://edge/api-ws/v1/realtime?x=1")
        self.assertIn("?x=1&model=", cfg.resolve_url())

    def test_missing_key_raises(self):
        # key 解析现在的顺序是：环境变量 → DSH 凭据文件 → 仓库 keys/dashscope.key，
        # 所以"没有 key"这件事必须把这三种来源都堵死，才算真的缺。
        cfg = omni.OmniConfig(workspace_id="ws1")
        import fast_system

        saved = os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            with mock.patch.object(fast_system, "resolve_dashscope_key", return_value=""):
                with self.assertRaises(omni.OmniError_):
                    cfg.resolve_key()
        finally:
            if saved is not None:
                os.environ["DASHSCOPE_API_KEY"] = saved

    def test_key_falls_back_to_shared_resolver(self):
        """环境变量缺失时必须还能从共享解析器拿到 key，否则子进程里双系统就断腿。"""
        import fast_system

        cfg = omni.OmniConfig(workspace_id="ws1")
        saved = os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            with mock.patch.object(fast_system, "resolve_dashscope_key", return_value="resolved-key"):
                self.assertEqual(cfg.resolve_key(), "resolved-key")
        finally:
            if saved is not None:
                os.environ["DASHSCOPE_API_KEY"] = saved

    def test_session_update_forces_text_modality(self):
        cfg = omni.OmniConfig(api_key="k", workspace_id="ws1", instructions="你负责看画面")
        evt = cfg.session_update()
        self.assertEqual(evt["type"], "session.update")
        self.assertEqual(evt["session"]["modalities"], ["text"])
        self.assertIsNone(evt["session"]["turn_detection"])


class FrameEncodeTests(unittest.TestCase):
    def test_rejects_png(self):
        with self.assertRaises(omni.OmniError_):
            omni.encode_jpeg_frame(b"\x89PNG\r\n\x1a\n")

    def test_rejects_oversize(self):
        big = b"\xff\xd8\xff" + bytes(omni.MAX_IMAGE_BYTES)
        with self.assertRaises(omni.OmniError_):
            omni.encode_jpeg_frame(big)

    def test_encodes_jpeg_to_base64(self):
        out = omni.encode_jpeg_frame(JPEG)
        self.assertEqual(base64.b64decode(out), JPEG)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, script):
        tr = omni.ScriptedTransport(script)
        cfg = omni.OmniConfig(api_key="k", workspace_id="ws1")
        return omni.OmniClient(cfg, tr), tr

    async def test_connect_sends_session_update(self):
        client, tr = self._client([])
        await client.connect()
        self.assertEqual(tr.types(), ["session.update"])

    async def test_frame_auto_seeds_audio_first(self):
        client, tr = self._client([])
        await client.connect()
        await client.send_frame(JPEG)
        self.assertEqual(
            tr.types(),
            ["session.update", "input_audio_buffer.append", "input_image_buffer.append"],
        )

    async def test_audio_not_reseeded_twice(self):
        client, tr = self._client([])
        await client.connect()
        await client.send_frame(JPEG)
        await client.send_frame(JPEG)
        self.assertEqual(tr.types().count("input_audio_buffer.append"), 1)

    async def test_ask_collects_text(self):
        client, tr = self._client([
            {"type": "response.text.delta", "delta": "向左"},
            {"type": "response.text.delta", "delta": "走两步"},
            {"type": "response.text.done", "text": "向左走两步"},
            {"type": "response.done", "response": {"output": []}},
        ])
        await client.connect()
        reply = await client.ask(JPEG, instructions="目标在左边")
        self.assertEqual(reply.text, "向左走两步")
        self.assertEqual(reply.raw_events, 4)  # 必须吃到 response.done 才停，否则残留事件错位下一轮
        self.assertEqual(tr.types()[-2:], ["input_audio_buffer.commit", "response.create"])

    async def test_function_call_collected(self):
        client, tr = self._client([
            {"type": "response.function_call_arguments.done", "name": "move",
             "call_id": "c1", "arguments": '{"direction": "left", "ms": 400}'},
            {"type": "response.done", "response": {"output": []}},
        ])
        await client.connect()
        reply = await client.ask(JPEG)
        call = reply.first_call("move")
        self.assertIsNotNone(call)
        self.assertEqual(call["arguments"]["direction"], "left")

    async def test_server_error_raises(self):
        client, tr = self._client([{"type": "error", "error": {"code": "bad_key", "message": "鉴权失败"}}])
        await client.connect()
        with self.assertRaises(omni.OmniError_):
            await client.ask(JPEG)

    async def test_error_event_clears_inflight(self):
        """服务端报错之后必须把通道收干净，否则下一轮会撞 active response。"""

        client, tr = self._client([
            {"type": "error", "error": {"code": "buffer", "message": "buffer too small"}},
            {"type": "response.done", "response": {"output": []}},
        ])
        await client.connect()
        with self.assertRaises(omni.OmniError_):
            await client.ask(JPEG)
        self.assertFalse(client._inflight)

    async def test_dangling_response_is_cancelled_before_next_ask(self):
        """上一轮没等到 response.done，下一轮要先 response.cancel 再问。"""

        client, tr = self._client([{"type": "response.text.done", "text": "半句"}])
        await client.connect()
        with self.assertRaises(omni.OmniError_):
            await client.ask(JPEG)
        self.assertTrue(client._inflight)
        tr._script.extend([
            {"type": "response.cancelled"},
            {"type": "response.done", "response": {"output": []}},
        ])
        await client.ask(JPEG)
        self.assertIn("response.cancel", tr.types())
        self.assertFalse(client._inflight)

    async def test_unknown_events_are_tolerated(self):
        client, tr = self._client([
            {"type": "session.created"},
            {"type": "response.audio_transcript.delta", "delta": "x"},
            {"type": "response.text.done", "text": "好"},
            {"type": "response.done", "response": {"output": []}},
        ])
        await client.connect()
        reply = await client.ask(JPEG)
        self.assertEqual(reply.text, "好")

    async def test_context_manager_closes(self):
        tr = omni.ScriptedTransport([])
        cfg = omni.OmniConfig(api_key="k", workspace_id="ws1")
        async with omni.OmniClient(cfg, tr) as client:
            self.assertIsNotNone(client)
        self.assertTrue(tr.closed)


class FastStreamAskTests(unittest.TestCase):
    """连续视频流的 ask 顺序：没新帧也要垫音频，上一轮没完要先撤。"""

    def test_ask_seeds_audio_even_without_new_frame(self):
        import fast_stream

        class Stub:
            def __init__(self):
                self.calls = []

            async def cancel_inflight(self, budget_s=8.0):
                self.calls.append("cancel")

            async def seed_audio(self, pcm=None):
                self.calls.append("audio")

            async def send_frame(self, jpeg):
                self.calls.append("frame")

            async def commit(self):
                self.calls.append("commit")

            async def create_response(self, instructions=""):
                self.calls.append("create")

            async def collect(self):
                self.calls.append("collect")
                return omni.OmniReply(text="ok")

        stream = fast_stream.FastStream(config=omni.OmniConfig(api_key="k"), fps=1.0)
        stub = Stub()
        stream._client = stub
        try:
            self.assertEqual(stream.ask("指令"), "ok")
        finally:
            stream.stop()
        self.assertEqual(stub.calls, ["cancel", "audio", "commit", "create", "collect"])


if __name__ == "__main__":
    unittest.main()
