#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LithosAI 客户端的离线单测：档位路由、请求体、重试与错误。

这些用例不联网——用 ``httpx.MockTransport`` 顶替真实连接，
重点验证"按紧迫程度选档"和"错误不被吞掉"这两件最容易写错的事。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lithos  # noqa: E402


class FakeResponse:
    """够用的假响应：状态码 + 文本 + json()。"""

    def __init__(self, status: int, body: dict | str):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def reply_body(text: str, tool_calls=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}], "usage": {"total_tokens": 7}}


class TierRoutingTests(unittest.TestCase):
    def test_urgency_maps_to_three_bands(self):
        self.assertEqual(lithos.choose_tier(0.0), "base")
        self.assertEqual(lithos.choose_tier(0.34), "base")
        self.assertEqual(lithos.choose_tier(0.5), "fast")
        self.assertEqual(lithos.choose_tier(0.74), "fast")
        self.assertEqual(lithos.choose_tier(0.75), "ultra")
        self.assertEqual(lithos.choose_tier(1.0), "ultra")

    def test_unknown_urgency_defaults_to_fast(self):
        self.assertEqual(lithos.choose_tier(None), "fast")

    def test_kind_beats_urgency_and_forced_beats_kind(self):
        self.assertEqual(lithos.choose_tier(0.99, kind="reflect"), "base")
        self.assertEqual(lithos.choose_tier(0.01, kind="react"), "ultra")
        self.assertEqual(lithos.choose_tier(0.99, kind="reflect", forced="ultra"), "ultra")

    def test_bad_tier_is_rejected(self):
        with self.assertRaises(lithos.LithosError):
            lithos.choose_tier(0.5, forced="turbo")

    def test_env_overrides_models_and_timeouts(self):
        config = lithos.LithosConfig.from_env(
            {
                "LITHOS_API_KEY": "k",
                "LITHOS_MODEL_ULTRA": "my-ultra",
                "LITHOS_TIMEOUT_ULTRA": "5",
                "LITHOS_BASE_URL": "https://example.test/v1/",
            }
        )
        self.assertEqual(config.models["ultra"], "my-ultra")
        self.assertEqual(config.timeouts["ultra"], 5.0)
        self.assertEqual(config.base_url, "https://example.test/v1")
        self.assertTrue(config.available())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.seen = []

    def make_client(self, handler, **overrides):
        config = lithos.LithosConfig(api_key="sk-test", **overrides)
        client = lithos.LithosClient(config)

        class Writer:
            def __init__(self, fn):
                self.fn = fn

            def post(self, url, headers=None, json=None, **kwargs):  # noqa: A002 - 对齐 httpx 签名
                self.fn(url, headers, json)
                return handler(url, headers, json)

        return client, Writer(lambda url, headers, body: self.seen.append({"url": url, "headers": headers, "body": body}))

    def run_complete(self, client, writer, **kwargs):
        original = lithos.httpx.Client

        class Patched:
            def __init__(self, *args, **kw):
                pass

            def __enter__(self):
                return writer

            def __exit__(self, *exc):
                return False

            def post(self, url, headers=None, json=None, **kwargs):  # noqa: A002
                # _http() 现在返回共享 client（keep-alive），不再走 with 语句，
                # 所以替身也必须自己提供 post，否则调用点拿到的是替身本身。
                return writer.post(url, headers=headers, json=json, **kwargs)

        lithos.httpx.Client = Patched  # type: ignore[assignment]
        try:
            return client.complete(**kwargs)
        finally:
            lithos.httpx.Client = original

    def test_request_shape_and_tier_model(self):
        client, writer = self.make_client(lambda url, h, b: FakeResponse(200, reply_body("好的")))
        reply = self.run_complete(
            client,
            writer,
            messages=[{"role": "user", "content": "hi"}],
            urgency=0.95,
        )
        self.assertEqual(reply.tier, "ultra")
        self.assertEqual(reply.text, "好的")
        sent = self.seen[0]
        self.assertTrue(sent["url"].endswith("/chat/completions"))
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(sent["body"]["model"], client.config.models["ultra"])
        self.assertFalse(sent["body"]["stream"])
        self.assertEqual(reply.usage, {"total_tokens": 7})

    def test_image_content_is_data_url_parts(self):
        content = lithos.build_user_content("看图", ["data:image/jpeg;base64,AAAA"])
        self.assertEqual(content[0], {"type": "text", "text": "看图"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64,AAAA")
        with self.assertRaises(lithos.LithosError):
            lithos.build_user_content("x", ["http://example.test/a.jpg"])

    def test_tool_calls_survive(self):
        calls = [{"id": "c1", "type": "function", "function": {"name": "observe", "arguments": "{}"}}]
        client, writer = self.make_client(lambda url, h, b: FakeResponse(200, reply_body("", calls)))
        reply = self.run_complete(client, writer, messages=[{"role": "user", "content": "x"}], tier="fast")
        self.assertEqual(reply.tool_calls, calls)
        self.assertIn("tool_calls", reply.as_message())

    def test_missing_key_is_explicit(self):
        client = lithos.LithosClient(lithos.LithosConfig(api_key=""))
        with self.assertRaises(lithos.LithosError) as ctx:
            client.complete([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.code, "missing_key")

    def test_4xx_is_not_retried(self):
        attempts = []

        def handler(url, headers, body):
            attempts.append(1)
            return FakeResponse(400, "bad request")

        client, writer = self.make_client(handler)
        with self.assertRaises(lithos.LithosError) as ctx:
            self.run_complete(client, writer, messages=[{"role": "user", "content": "x"}], tier="base")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(attempts), 1, "4xx 不应重试")

    def test_5xx_retries_then_succeeds(self):
        seen = {"n": 0}

        def handler(url, headers, body):
            seen["n"] += 1
            if seen["n"] == 1:
                return FakeResponse(503, "busy")
            return FakeResponse(200, reply_body("ok"))

        client, writer = self.make_client(handler)
        reply = self.run_complete(
            client, writer, messages=[{"role": "user", "content": "x"}], tier="base", max_retries=2
        )
        self.assertEqual(reply.text, "ok")
        self.assertEqual(seen["n"], 2)

    def test_bad_shape_is_reported(self):
        client, writer = self.make_client(lambda url, h, b: FakeResponse(200, {"nope": True}))
        with self.assertRaises(lithos.LithosError) as ctx:
            self.run_complete(client, writer, messages=[{"role": "user", "content": "x"}], tier="base")
        self.assertEqual(ctx.exception.code, "bad_shape")

    def test_redact_hides_secret(self):
        self.assertNotIn("abcd1234", lithos.redact("Authorization: Bearer abcd1234xyz"))
        self.assertIn("<redacted>", lithos.redact("Bearer abcd1234xyz"))


class ReplyMessageTests(unittest.TestCase):
    """as_message() 是给"下一轮请求"用的，thinking 模式下字段不能省。"""

    def test_tool_call_reply_keeps_empty_reasoning_content(self):
        calls = [{"id": "c1", "type": "function", "function": {"name": "scan", "arguments": "{}"}}]
        reply = lithos.LLMReply(text="", tool_calls=calls, tier="fast", model="deepseek-flash", latency_ms=1)
        message = reply.as_message()
        self.assertEqual(message["tool_calls"], calls)
        self.assertEqual(message["reasoning_content"], "")

    def test_tool_call_reply_keeps_real_reasoning(self):
        calls = [{"id": "c1", "type": "function", "function": {"name": "scan", "arguments": "{}"}}]
        reply = lithos.LLMReply(text="", tool_calls=calls, tier="fast", model="m", latency_ms=1, reasoning="先看")
        self.assertEqual(reply.as_message()["reasoning_content"], "先看")

    def test_text_reply_without_reasoning_omits_field(self):
        reply = lithos.LLMReply(text="hi", tool_calls=[], tier="fast", model="m", latency_ms=1)
        self.assertNotIn("reasoning_content", reply.as_message())


if __name__ == "__main__":
    unittest.main()
