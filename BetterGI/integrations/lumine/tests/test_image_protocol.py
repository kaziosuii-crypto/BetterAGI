#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stage-1 图像协议的回归测试。

官方 Runner（``Runner/client.go``）写明：stage-1 训练只有一条 image-only user
消息，图片是 1280x720 底部补 16 像素黑边的 PNG，采样温度 0、最多 192 token、
保留特殊 token。之前的实现发的是 JPEG + system 提示 + 指令文本，属于分布外
输入，模型因此长期输出 idle。本文件守住正确形状，避免再次回退。
"""

import base64
import io
import json
import pathlib
import sys
import unittest

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autodl_server as srv  # noqa: E402
import lumine_protocol as protocol  # noqa: E402


def _jpeg_base64(width: int = 320, height: int = 180) -> str:
    """生成一张纯色 JPEG，用于验证转码。"""
    image = Image.new("RGB", (width, height), (24, 48, 72))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class ImageOnlyMessageTests(unittest.TestCase):
    """消息必须是唯一一条、只有图像、没有文本。"""

    def test_single_image_message(self):
        messages = protocol.build_image_only_messages("data:image/png;base64,AAAA")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        content = messages[0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "image_url")
        self.assertEqual(content[0]["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_empty_url_rejected(self):
        with self.assertRaises(ValueError):
            protocol.build_image_only_messages("   ")


class TrainingFrameTests(unittest.TestCase):
    """帧必须转成 1280x720 再补到 1280x736 的 PNG。"""

    def test_frame_is_padded_png(self):
        url = srv.prepare_training_frame_data_url(_jpeg_base64())
        self.assertTrue(url.startswith("data:image/png;base64,"))
        raw = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1280, 736))
            # 补出来的 16 行必须是黑边，不能把画面拉伸进去。
            self.assertEqual(image.convert("RGB").getpixel((640, 730)), (0, 0, 0))
            self.assertNotEqual(image.convert("RGB").getpixel((640, 360)), (0, 0, 0))

    def test_frame_already_native_resolution(self):
        url = srv.prepare_training_frame_data_url(_jpeg_base64(1280, 720))
        raw = base64.b64decode(url.split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (1280, 736))


class _FakeResponse:
    """返回固定响应体的最小 aiohttp 响应替身。"""

    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


class _FakeHTTP:
    """记录最后一次请求体的 HTTP 客户端替身。"""

    closed = False

    def __init__(self, body):
        self.body = body
        self.payload = None

    def post(self, url, json=None):
        self.payload = json
        return _FakeResponse(200, self.body)


class ImageProtocolPayloadTests(unittest.IsolatedAsyncioTestCase):
    """真正发往 vLLM 的请求体必须与官方配方逐项一致。"""

    async def test_payload_matches_training_recipe(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "<|action_start|>0 0 0 ; W ; W ; W ; W ; W ; W"
                                "<|action_end|>"
                            )
                        }
                    }
                ]
            }
        )
        server = srv.LumineServer(srv.ServerConfig(mock=False))
        server.http = _FakeHTTP(body)

        # 即使带意图与指导语，image 协议也不得注入任何文本。
        parsed = await server.infer(
            _jpeg_base64(), "向前走", 1, {"intent_id": "i-1", "goal": "向前走"}
        )

        payload = server.http.payload
        self.assertEqual(payload["max_tokens"], srv.TRAINING_MAX_TOKENS)
        self.assertEqual(payload["max_tokens"], 192)
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["top_p"], 1.0)
        self.assertFalse(payload["skip_special_tokens"])
        self.assertFalse(payload["stream"])
        self.assertEqual(len(payload["messages"]), 1)
        message = payload["messages"][0]
        self.assertEqual(message["role"], "user")
        self.assertEqual(len(message["content"]), 1)
        self.assertEqual(message["content"][0]["type"], "image_url")
        self.assertTrue(message["content"][0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(parsed["chunks"][0]["keys"], ["W"])

    async def test_image_protocol_does_not_resample(self):
        """温度 0 下重采样不会改变结果，因此 image 协议只请求一次。"""
        body = json.dumps(
            {"choices": [{"message": {"content": "<|action_start|>0 0 0 ; ; ; ; ; ;<|action_end|>"}}]}
        )
        calls = []

        class Counting(_FakeHTTP):
            def post(self, url, json=None):
                calls.append(json)
                return _FakeResponse(200, self.body)

        server = srv.LumineServer(srv.ServerConfig(mock=False))
        server.http = Counting(body)
        await server.infer(_jpeg_base64(), "", 1, {"intent_id": "i", "goal": "前进"})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
