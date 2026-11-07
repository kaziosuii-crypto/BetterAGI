#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lithos 慢系统的两个新边界：流式截止（deadline）与直播固定 fast 档。

全程离线：httpx.Client 用假件顶掉，时钟用假 monotonic 推进，
所以"超时"是确定性的，不需要 sleep，也不会真联网。
"""

from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lithos  # noqa: E402


def sse_lines(*chunks: str) -> list:
    """把若干段正文拼成 OpenAI 兼容的 SSE 行（末尾补 [DONE]）。"""
    lines = []
    for text in chunks:
        body = {"choices": [{"delta": {"content": text}}]}
        lines.append("data: " + json.dumps(body))
    lines.append("data: [DONE]")
    return lines


class FakeClock:
    """假 monotonic：每次调用先返回当前值，再前进 step 秒。"""

    def __init__(self, start: float = 1000.0, step: float = 0.0):
        self.now = start
        self.step = step
        self.calls = 0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        self.calls += 1
        return value


class FakeStreamResponse:
    """够用的假流式响应：状态码 + 逐行迭代 + read()。"""

    def __init__(self, lines, status: int = 200, body: bytes = b""):
        self.status_code = status
        self._lines = list(lines)
        self._body = body

    def read(self) -> bytes:
        return self._body

    def iter_lines(self):
        for line in self._lines:
            yield line


class StreamContext:
    """client.stream(...) 返回的上下文管理器。"""

    def __init__(self, response: FakeStreamResponse):
        self._response = response

    def __enter__(self) -> FakeStreamResponse:
        return self._response

    def __exit__(self, *exc) -> bool:
        return False


def make_fake_client(lines):
    """造一个只实现 stream() 的假 httpx.Client，并把请求体记在类属性上。"""

    class FakeClient:
        last_request = None
        client_kwargs = None

        def __init__(self, *args, **kwargs):
            FakeClient.client_kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None, **kwargs):  # noqa: A002
            FakeClient.last_request = {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json,
            }
            return StreamContext(FakeStreamResponse(lines))

    return FakeClient


class StreamDeadlineTests(unittest.TestCase):
    def test_script_request_can_omit_max_tokens(self):
        fake_client = make_fake_client(sse_lines("<script>Z</script>"))
        client = lithos.LithosClient(lithos.LithosConfig(api_key="sk-test"))
        with mock.patch.object(client, "_http", return_value=fake_client()):
            client.stream_complete([{"role": "user", "content": "test"}], omit_max_tokens=True)
        self.assertNotIn("max_tokens", fake_client.last_request["body"])

    def setUp(self):
        self.events = []
        self.client = lithos.LithosClient(lithos.LithosConfig(api_key="sk-test"))

    def collect(self, kind, text):
        self.events.append((kind, text))

    def kinds(self):
        return [kind for kind, _ in self.events]

    def run_stream(self, lines, clock, with_callback: bool = True, **call_kwargs):
        fake = make_fake_client(lines)
        self.assertIsNotNone(lithos.httpx, "需要 httpx 才能走流式路径")
        callback = self.collect if with_callback else None
        with mock.patch.object(lithos.httpx, "Client", fake):
            with mock.patch.object(lithos.time, "monotonic", clock):
                return self.client.stream_complete(
                    [{"role": "user", "content": "hi"}],
                    on_delta=callback,
                    **call_kwargs,
                )

    # ---- 1. 超预算：发一个中止事件并立刻停 ----
    def test_deadline_emits_abort_event_and_stops_stream(self):
        # started=1000.0；每读一行时钟 +0.4s；第 3 行时 elapsed=1.2s > 1.0s。
        clock = FakeClock(start=1000.0, step=0.4)
        reply = self.run_stream(sse_lines("A", "B", "C", "D", "E"), clock, deadline_s=1.0)

        self.assertTrue(reply.aborted)
        self.assertEqual(reply.text, "AB", "中止后不应再收后续片段")
        self.assertEqual(self.kinds(), ["text", "text", "abort"])
        # 中止事件沿用 on_delta(kind, text) 的同一套形状：两个位置参数、都是字符串。
        for event in self.events:
            self.assertEqual(len(event), 2)
            self.assertIsInstance(event[0], str)
            self.assertIsInstance(event[1], str)
        abort_piece = self.events[-1][1]
        self.assertIn("deadline", abort_piece)
        self.assertLessEqual(len(self.events), 4, "中止后不应继续转抄片段")

    # ---- 2. 未超预算：完整收完、不出现中止事件 ----
    def test_no_abort_when_under_deadline(self):
        clock = FakeClock(start=1000.0, step=0.4)
        reply = self.run_stream(sse_lines("A", "B", "C"), clock, deadline_s=5.0)

        self.assertFalse(reply.aborted)
        self.assertEqual(reply.text, "ABC")
        self.assertEqual(self.kinds(), ["text", "text", "text"])
        self.assertNotIn("abort", self.kinds())

    # ---- 3. 默认 deadline_s 就是 30.0 秒（2026-11-04 用户把慢系统预算放到 30s） ----
    def test_default_deadline_is_thirty_seconds(self):
        clock = FakeClock(start=1000.0, step=3.0)
        reply = self.run_stream(sse_lines("A", "B"), clock)  # 不传 deadline_s

        self.assertFalse(reply.aborted, "默认 30s 预算下 3s 不该被砍")
        self.assertEqual(reply.text, "AB")
        self.assertEqual(self.kinds(), ["text", "text"])

    # ---- 4. deadline_s=None 关闭截止 ----
    def test_none_disables_deadline(self):
        clock = FakeClock(start=1000.0, step=10.0)
        reply = self.run_stream(sse_lines("A", "B", "C"), clock, deadline_s=None)

        self.assertFalse(reply.aborted)
        self.assertEqual(reply.text, "ABC")
        self.assertNotIn("abort", self.kinds())

    # ---- 5. 没有 on_delta 时中止也不抛 ----
    def test_abort_without_callback_is_silent(self):
        clock = FakeClock(start=1000.0, step=0.1)
        reply = self.run_stream(sse_lines("A", "B"), clock, with_callback=False, deadline_s=0.0)

        self.assertTrue(reply.aborted)
        self.assertEqual(reply.text, "")

    # ---- 6. 请求体仍然按 selected tier 发、stream=True ----
    def test_stream_request_still_uses_selected_tier(self):
        factory = make_fake_client(sse_lines("A"))
        with mock.patch.object(lithos.httpx, "Client", factory):
            with mock.patch.object(lithos.time, "monotonic", FakeClock(1000.0, 0.0)):
                self.client.stream_complete(
                    [{"role": "user", "content": "hi"}], urgency=0.0, deadline_s=5.0
                )
        sent = factory.last_request
        self.assertEqual(sent["method"], "POST")
        self.assertTrue(sent["url"].endswith("/chat/completions"))
        body = sent["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["model"], self.client.config.models["base"])


class LiveTierTests(unittest.TestCase):
    def test_live_is_always_fast(self):
        self.assertEqual(lithos.choose_tier(0.99, live=True), "fast")
        self.assertEqual(lithos.choose_tier(0.0, live=True), "fast")
        self.assertEqual(lithos.choose_tier(None, live=True), "fast")
        # 直播档优先于 kind/forced：直播只有 fast 一条路。
        self.assertEqual(lithos.choose_tier(0.99, kind="react", live=True), "fast")
        self.assertEqual(lithos.choose_tier(0.99, kind="react", forced="ultra", live=True), "fast")

    def test_legacy_routing_unchanged(self):
        self.assertEqual(lithos.choose_tier(0.0), "base")
        self.assertEqual(lithos.choose_tier(0.34), "base")
        self.assertEqual(lithos.choose_tier(0.5), "fast")
        self.assertEqual(lithos.choose_tier(0.74), "fast")
        self.assertEqual(lithos.choose_tier(0.75), "ultra")
        self.assertEqual(lithos.choose_tier(1.0), "ultra")
        self.assertEqual(lithos.choose_tier(None), "fast")
        self.assertEqual(lithos.choose_tier(0.99, kind="reflect"), "base")
        self.assertEqual(lithos.choose_tier(0.01, kind="react"), "ultra")
        self.assertEqual(lithos.choose_tier(0.99, kind="reflect", forced="ultra"), "ultra")
        with self.assertRaises(lithos.LithosError):
            lithos.choose_tier(0.5, forced="turbo")

    def test_live_is_keyword_only_and_default_off(self):
        parameter = inspect.signature(lithos.choose_tier).parameters["live"]
        self.assertIs(parameter.default, False)
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        with self.assertRaises(TypeError):
            lithos.choose_tier(0.5, False)  # type: ignore[misc]

    def test_reply_defaults_to_not_aborted(self):
        reply = lithos.LLMReply(text="x", tool_calls=[], tier="fast", model="m", latency_ms=1)
        self.assertFalse(reply.aborted)


if __name__ == "__main__":
    unittest.main()
