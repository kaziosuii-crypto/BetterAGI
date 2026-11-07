#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考导演的单元测试。

纯函数测试不依赖网络或 aiohttp；真实 HTTP 集成测试在 aiohttp 缺失时跳过。
可以直接运行::

    python integrations/lumine/tests/test_director.py
    python -m unittest discover -s integrations/lumine/tests
"""

import asyncio
import os
import socket
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

import live_director as director  # noqa: E402
import llm_client  # noqa: E402

HAVE_AIOHTTP = director.aiohttp is not None and llm_client.aiohttp is not None


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class RecordingClient(director.BetterGIClient):
    """记录 HTTP 路径与载荷，不发起真实请求。"""

    def __init__(self, session_id="vtuber"):
        super().__init__(
            session=None,
            base_url="http://example.invalid",
            token="token",
            session_id=session_id,
        )
        self.requests = []
        self.memory = {"lastScene": "initial"}

    async def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if method == "GET" and path.endswith("/memory"):
            # 与 C# AgentMemoryDto 一致；旧测试曾自造 {"memory": ...}。
            return {
                "sessionId": self.session_id,
                "updatedAtUtc": "2026-10-27T00:00:00Z",
                "values": dict(self.memory),
            }
        if method == "POST" and path.endswith("/memory"):
            self.memory.update(payload or {})
            return {"ok": True}
        if path.endswith("/checkpoints"):
            return {"ok": True, "name": (payload or {}).get("name")}
        if path.endswith("/live/status"):
            return {"running": True}
        if path.endswith("/live/guidance"):
            return {"ok": True}
        return {"ok": True}


def make_director(sent, logger=None):
    """构造一个使用测试发送钩子的导演。"""

    async def send(payload):
        sent.append(dict(payload))

    instance = director.LiveDirector(
        ws_url="ws://127.0.0.1:30648/ws/v1/agent/live",
        http_url="http://127.0.0.1:30648",
        token="token",
        session_id="vtuber",
        persona=dict(director.DEFAULT_PERSONA),
        interval=4.0,
        llm=None,
        logger=logger,
        send_hook=send,
    )
    instance.http = RecordingClient()
    return instance


# ----------------------------------------------------------------------
# JSON 提取
# ----------------------------------------------------------------------


class ExtractJsonTests(unittest.TestCase):
    VALID = (
        '{"thought":"先观察","guidance":"沿路前进",'
        '"commentary":[{"kind":"reaction","text":"哇","emotion":"surprised"}],'
        '"memory_patch":{"goal":"神像"},"tool_calls":[]}'
    )

    def test_valid_json(self):
        data = director.extract_director_json(self.VALID)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["guidance"], "沿路前进")
        self.assertEqual(data["commentary"][0]["text"], "哇")
        self.assertEqual(data["memory_patch"], {"goal": "神像"})

    def test_fenced_json(self):
        text = "```json\n" + self.VALID + "\n```"
        data = director.extract_director_json(text)
        self.assertEqual(data["guidance"], "沿路前进")

    def test_fenced_with_surrounding_prose(self):
        text = "这是本轮结果：\n```json\n" + self.VALID + "\n```\n以上。"
        data = director.extract_director_json(text)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["thought"], "先观察")

    def test_plain_fence(self):
        text = "```\n" + self.VALID + "\n```"
        data = director.extract_director_json(text)
        self.assertEqual(data["guidance"], "沿路前进")

    def test_braces_inside_strings(self):
        text = '前缀 {"guidance":"用 {W} 和 } 都不怕","thought":"ok"} 后缀'
        data = director.extract_director_json(text)
        self.assertEqual(data["guidance"], "用 {W} 和 } 都不怕")

    def test_partial_missing_closing_brace(self):
        text = '{"thought":"观察中","guidance":"沿路走，注意体力"'
        data = director.extract_director_json(text)
        self.assertIsNotNone(data)
        self.assertEqual(data["guidance"], "沿路走，注意体力")

    def test_partial_unterminated_string(self):
        text = '{"thought":"观察中","guidance":"沿路走'
        data = director.extract_director_json(text)
        self.assertIsNotNone(data)
        self.assertEqual(data["guidance"], "沿路走")

    def test_partial_commentary_salvage(self):
        text = '{"guidance":"小心","commentary":[{"kind":"reaction","text":"哇"'
        data = director.extract_director_json(text)
        self.assertIsNotNone(data)
        self.assertEqual(data["commentary"][0]["text"], "哇")

    def test_invalid_returns_none(self):
        self.assertIsNone(director.extract_director_json("完全不是 JSON"))
        self.assertIsNone(director.extract_director_json(""))
        self.assertIsNone(director.extract_director_json(None))

    def test_accepts_dict(self):
        value = {"guidance": "x"}
        self.assertIs(director.extract_director_json(value), value)


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------


class PromptTests(unittest.TestCase):
    def test_prompt_contains_persona_state_and_only_recent_commentary(self):
        persona = {
            "name": "小光",
            "personality": "好奇",
            "tone": "活泼",
            "language": "简体中文",
            "catchphrases": ["咦？"],
            "content_dimensions": {
                "reaction": "即时反应",
                "lore": "不剧透的世界观",
            },
        }
        commentary = [
            {"kind": "chat", "text": "第{0}条".format(index)} for index in range(10)
        ]
        prompt = director.build_prompt(
            persona,
            guidance="沿着道路前进",
            commentary=commentary,
            action_state={"keys": ["W"], "queueDepth": 2},
            memory={"goal": "七天神像"},
            frame_seq=123,
        )
        system = prompt["system"]
        user = prompt["user"]

        self.assertIn("小光", system)
        self.assertIn("即时反应", system)
        self.assertIn("不剧透的世界观", system)
        self.assertIn("VTuber", system)
        self.assertIn("80", system)
        self.assertIn("memory_patch", system)
        self.assertIn("bgi_live_set_guidance", system)
        self.assertIn("bgi_agent_memory_set", system)

        self.assertIn("沿着道路前进", user)
        self.assertIn("七天神像", user)
        self.assertIn("123", user)
        self.assertIn("第2条", user)
        self.assertIn("第9条", user)
        self.assertNotIn("第0条", user)
        self.assertNotIn("第1条", user)

    def test_prompt_includes_short_and_long_memory(self):
        prompt = director.build_prompt(
            None,
            short_term="当前场景：须弥城；最近事件：看到宝箱",
            long_term="- [设定] 须弥是草神的国度",
        )
        user = prompt["user"]
        self.assertIn("短期记忆", user)
        self.assertIn("当前场景：须弥城", user)
        self.assertIn("长期记忆", user)
        self.assertIn("须弥是草神的国度", user)

    def test_prompt_handles_missing_optional_fields(self):
        prompt = director.build_prompt(None)
        self.assertIn(director.DEFAULT_PERSONA["name"], prompt["system"])
        self.assertIn("（暂无）", prompt["user"])
        self.assertIn("{}", prompt["user"])

    def test_prompt_handles_list_dimensions(self):
        persona = {
            "name": "测试",
            "content_dimensions": ["reaction", "plan", "chat"],
        }
        prompt = director.build_prompt(persona)
        self.assertIn("reaction、plan、chat", prompt["system"])

    def test_memory_is_truncated(self):
        prompt = director.build_prompt(
            None, memory={"blob": "x" * 5000}, memory_limit=100
        )
        self.assertIn("已截断", prompt["user"])


# ----------------------------------------------------------------------
# 指导 / 解说规范化
# ----------------------------------------------------------------------


class NormalizationTests(unittest.TestCase):
    def test_normalize_commentary(self):
        items = [
            {"kind": "lore", "text": "  这是 一句 台词  ", "emotion": "happy"},
            {"kind": "bogus", "text": "非法类型", "emotion": "angry"},
            {"kind": "reaction", "text": ""},
            {"text": "字" * 100, "kind": "reaction", "emotion": "calm"},
        ]
        normalized = director.normalize_commentary(items, frame_seq="42")
        self.assertEqual(len(normalized), 3)
        self.assertEqual(normalized[0]["text"], "这是 一句 台词")
        self.assertEqual(normalized[0]["emotion"], "happy")
        self.assertEqual(normalized[0]["frameSeq"], 42)
        self.assertEqual(normalized[1]["kind"], "reaction")
        self.assertEqual(normalized[1]["emotion"], "calm")
        self.assertLessEqual(len(normalized[2]["text"]), 80)

    def test_normalize_commentary_limit(self):
        items = [
            {"kind": "reaction", "text": "第{0}条".format(index)} for index in range(10)
        ]
        normalized = director.normalize_commentary(items, max_items=2)
        self.assertEqual(len(normalized), 2)

    def test_normalize_guidance_collapses_lines(self):
        self.assertEqual(
            director.normalize_guidance(" 第一行\n第二行 "), "第一行 第二行"
        )
        self.assertEqual(director.normalize_guidance(None), "")

    def test_detect_significant_change(self):
        self.assertTrue(director.detect_significant_change("旧", "新", [], []))
        self.assertFalse(director.detect_significant_change("同", "同", [], []))
        self.assertTrue(director.detect_significant_change("", "", [], ["W"]))
        self.assertFalse(director.detect_significant_change("", "", ["W"], ["W"]))

    def test_build_ws_url_and_redaction(self):
        url = director.build_ws_url(
            "ws://127.0.0.1:30648/ws/v1/agent/live?x=1", "secret-token"
        )
        self.assertIn("access_token=secret-token", url)
        self.assertIn("x=1", url)
        redacted = director.redact_url(url)
        self.assertNotIn("secret-token", redacted)
        self.assertIn("x=1", redacted)


# ----------------------------------------------------------------------
# 工具白名单
# ----------------------------------------------------------------------


class ToolWhitelistTests(unittest.TestCase):
    def test_allowed_tools_are_exactly_the_five(self):
        self.assertEqual(
            director.ALLOWED_TOOL_NAMES,
            frozenset(
                {
                    "bgi_agent_memory_set",
                    "bgi_agent_memory_get",
                    "bgi_agent_checkpoint_create",
                    "bgi_live_status",
                    "bgi_live_set_guidance",
                }
            ),
        )

    def test_filter_tool_calls(self):
        calls = [
            {
                "name": "bgi_agent_memory_set",
                "arguments": {"values": {"goal": "x"}},
            },
            {"name": "bgi_agent_evil", "arguments": {}},
            {"name": "bgi_live_status"},
            {"name": "bgi_live_status", "arguments": []},
            "not-a-dict",
        ]
        accepted = director.filter_tool_calls(calls)
        self.assertEqual(
            [call["name"] for call in accepted],
            ["bgi_agent_memory_set", "bgi_live_status"],
        )
        self.assertEqual(accepted[1]["arguments"], {})

    def test_memory_values_from_arguments(self):
        self.assertEqual(
            director.memory_values_from_arguments({"values": {"a": 1, "b": 2}}),
            {"a": 1, "b": 2},
        )
        self.assertEqual(
            director.memory_values_from_arguments({"key": "a", "value": 3}),
            {"a": 3},
        )
        self.assertEqual(
            director.memory_values_from_arguments(
                {"goal": "x", "sessionId": "ignored"}
            ),
            {"goal": "x"},
        )


class ExecuteToolCallsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_whitelisted_calls_execute(self):
        client = RecordingClient()
        results = await director.execute_tool_calls(
            client,
            [
                {
                    "name": "bgi_agent_memory_set",
                    "arguments": {"values": {"goal": "x"}},
                },
                {"name": "bgi_agent_delete_everything", "arguments": {}},
            ],
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(
            client.requests,
            [
                (
                    "POST",
                    "/api/v1/agent/sessions/vtuber/memory",
                    {"goal": "x"},
                )
            ],
        )

    async def test_tool_endpoint_mapping(self):
        client = RecordingClient()

        await client.execute_tool(
            "bgi_agent_checkpoint_create",
            {"name": "before-boss", "note": "准备"},
        )
        await client.execute_tool("bgi_live_status", {})
        await client.execute_tool("bgi_live_set_guidance", {"text": " 先 观察 "})
        value = await client.execute_tool("bgi_agent_memory_get", {"key": "lastScene"})

        paths = [(method, path) for method, path, _payload in client.requests]
        self.assertIn(("POST", "/api/v1/agent/sessions/vtuber/checkpoints"), paths)
        self.assertIn(("GET", "/api/v1/agent/live/status"), paths)
        self.assertIn(("POST", "/api/v1/agent/live/guidance"), paths)
        self.assertIn(("GET", "/api/v1/agent/sessions/vtuber/memory"), paths)
        self.assertEqual(value, {"key": "lastScene", "value": "initial"})

        guidance_payload = [
            payload
            for method, path, payload in client.requests
            if path.endswith("/live/guidance")
        ][0]
        self.assertEqual(guidance_payload["text"], "先 观察")
        self.assertEqual(guidance_payload["source"], "director")

    async def test_unknown_tool_raises(self):
        client = RecordingClient()
        with self.assertRaises(director.BetterGIError):
            await client.execute_tool("bgi_agent_evil", {})


# ----------------------------------------------------------------------
# 解说转发
# ----------------------------------------------------------------------


class CommentaryForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_guidance_only_sent_when_changed_and_commentary_forwarded(self):
        sent = []
        instance = make_director(sent)
        response = {
            "thought": "先观察",
            "guidance": "沿着道路前进",
            "commentary": [
                {
                    "kind": "reaction",
                    "text": "哇，前面有丘丘人！",
                    "emotion": "surprised",
                },
                {"kind": "invalid-kind", "text": "回退反应", "emotion": "odd"},
                {"kind": "reaction", "text": ""},
            ],
            "memory_patch": {"goal": "七天神像"},
        }

        await instance._process_response(response, {"seq": 42})

        guidance_messages = [
            message for message in sent if message.get("type") == "guidance"
        ]
        commentary_messages = [
            message for message in sent if message.get("type") == "commentary"
        ]
        internal_messages = [
            message
            for message in commentary_messages
            if (message.get("meta") or {}).get("internal")
        ]
        spoken_messages = [
            message
            for message in commentary_messages
            if not (message.get("meta") or {}).get("internal")
        ]
        self.assertEqual(
            guidance_messages,
            [{"type": "guidance", "text": "沿着道路前进"}],
        )
        self.assertEqual(len(internal_messages), 1)
        self.assertEqual(internal_messages[0]["text"], "先观察")
        self.assertEqual(internal_messages[0]["kind"], "plan")
        self.assertEqual(len(spoken_messages), 2)
        self.assertEqual(spoken_messages[0]["frameSeq"], 42)
        self.assertEqual(spoken_messages[0]["emotion"], "surprised")
        self.assertEqual(spoken_messages[1]["kind"], "reaction")
        self.assertEqual(spoken_messages[1]["emotion"], "calm")
        self.assertEqual(instance.memory["goal"], "七天神像")
        self.assertEqual(instance.http.memory["goal"], "七天神像")

        # 相同指导与相同解说被去重，不产生新的 WS 消息。
        before = len(sent)
        await instance._process_response(response, {"seq": 42})
        self.assertEqual(len(sent), before)

    async def test_tool_calls_are_whitelisted_and_ignore_others(self):
        sent = []
        instance = make_director(sent)
        response = {
            "tool_calls": [
                {
                    "name": "bgi_agent_checkpoint_create",
                    "arguments": {"name": "cp-1", "note": "记录"},
                },
                {"name": "bgi_agent_exec_shell", "arguments": {"cmd": "rm"}},
            ]
        }
        await instance._process_response(response, {"seq": 7})

        requests = instance.http.requests
        self.assertEqual(len(requests), 1)
        method, path, payload = requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/checkpoints"))
        self.assertEqual(payload["name"], "cp-1")

    async def test_memory_tool_updates_local_memory_and_guidance_tool_state(self):
        sent = []
        instance = make_director(sent)
        response = {
            "tool_calls": [
                {
                    "name": "bgi_agent_memory_set",
                    "arguments": {"values": {"quest": "daily"}},
                },
                {
                    "name": "bgi_live_set_guidance",
                    "arguments": {"text": "先去七天神像"},
                },
            ]
        }
        await instance._process_response(response, {"seq": 9})

        self.assertEqual(instance.memory["quest"], "daily")
        self.assertEqual(instance._last_sent_guidance, "先去七天神像")
        paths = [path for _method, path, _payload in instance.http.requests]
        self.assertIn("/api/v1/agent/sessions/vtuber/memory", paths)
        self.assertIn("/api/v1/agent/live/guidance", paths)

    async def test_no_http_client_does_not_crash(self):
        sent = []
        instance = make_director(sent)
        instance.http = None
        await instance._process_response(
            {"tool_calls": [{"name": "bgi_live_status", "arguments": {}}]},
            {"seq": 1},
        )
        self.assertEqual(sent, [])

    async def test_no_llm_tick_only_logs_and_never_sends(self):
        sent = []
        instance = make_director(sent)
        instance.no_llm = True
        instance._latest_frame = {
            "seq": 5,
            "jpeg": "base64",
            "uiCategory": "Main",
        }
        instance._action_state = {"keys": ["W"], "queueDepth": 1}
        await instance._tick()
        self.assertEqual(sent, [])

    async def test_tick_calls_llm_with_frame_and_applies_fenced_json(self):
        class FakeLLM:
            def __init__(self):
                self.calls = []

            async def complete(self, system, user, image_b64=None, media_type=None):
                self.calls.append((system, user, image_b64, media_type))
                return (
                    "```json\n"
                    '{"thought":"ok","guidance":"冲刺",'
                    '"commentary":[{"kind":"plan",'
                    '"text":"先冲刺再滑翔","emotion":"excited"}]}\n'
                    "```"
                )

        sent = []
        instance = make_director(sent)
        fake = FakeLLM()
        instance.llm = fake
        instance._latest_frame = {
            "seq": 11,
            "jpeg": "frame-base64",
            "width": 1280,
            "height": 720,
        }
        await instance._tick()

        self.assertEqual(len(fake.calls), 1)
        system, user, image_b64, media_type = fake.calls[0]
        self.assertIn(director.DEFAULT_PERSONA["name"], system)
        self.assertIn("帧序号：11", user)
        self.assertEqual(image_b64, "frame-base64")
        self.assertEqual(media_type, "image/jpeg")
        self.assertEqual(
            sent,
            [
                {
                    "type": "commentary",
                    "kind": "plan",
                    "text": "ok",
                    "emotion": "thinking",
                    "meta": {"internal": True, "thought": True},
                    "frameSeq": 11,
                },
                {"type": "guidance", "text": "冲刺"},
                {
                    "type": "commentary",
                    "kind": "plan",
                    "text": "先冲刺再滑翔",
                    "emotion": "excited",
                    "frameSeq": 11,
                },
            ],
        )

    async def test_invalid_llm_json_is_skipped(self):
        class FakeLLM:
            async def complete(self, *args, **kwargs):
                return "这不是 JSON"

        sent = []
        instance = make_director(sent)
        instance.llm = FakeLLM()
        instance._latest_frame = {"seq": 1, "jpeg": "frame"}
        await instance._tick()
        self.assertEqual(sent, [])


# ----------------------------------------------------------------------
# LLM 客户端（纯函数部分）
# ----------------------------------------------------------------------


class MemoryContextTests(unittest.TestCase):
    def test_short_term_text_includes_scene_and_events(self):
        instance = make_director([])
        instance.memory = {
            "scene": "须弥城",
            "objective": "找到宝箱",
            "plot": "刚进入须弥",
        }
        instance._record_short_event("解说", "看到一个宝箱")
        text = instance._short_term_text()
        self.assertIn("当前场景：须弥城", text)
        self.assertIn("当前目标：找到宝箱", text)
        self.assertIn("剧情进展：刚进入须弥", text)
        self.assertIn("看到一个宝箱", text)


class MemoryShapeTests(unittest.TestCase):
    def test_extract_memory_values_supports_real_and_legacy_shapes(self):
        self.assertEqual(
            director.extract_memory_values(
                {"sessionId": "s", "updatedAtUtc": "t", "values": {"scene": "须弥"}}
            ),
            {"scene": "须弥"},
        )
        self.assertEqual(
            director.extract_memory_values({"memory": {"scene": "蒙德"}}),
            {"scene": "蒙德"},
        )
        self.assertEqual(
            director.extract_memory_values({"scene": "璃月"}), {"scene": "璃月"}
        )
        self.assertEqual(director.extract_memory_values({"sessionId": "s"}), {})

    def test_load_initial_memory_uses_values(self):
        instance = make_director([])
        instance.http.memory = {"scene": "须弥城", "plot": "刚到这里"}

        async def scenario():
            await instance._load_initial_memory(ensure=False)

        asyncio.run(scenario())
        self.assertEqual(instance.memory["scene"], "须弥城")
        self.assertEqual(instance.memory["plot"], "刚到这里")


class GuidanceSendTests(unittest.TestCase):
    def test_guidance_not_marked_sent_without_websocket(self):
        instance = director.LiveDirector(
            ws_url="ws://127.0.0.1:30648/ws/v1/agent/live",
            http_url="http://127.0.0.1:30648",
            token="token",
            session_id="vtuber",
            persona=dict(director.DEFAULT_PERSONA),
            llm=None,
            send_hook=None,
        )
        instance.http = RecordingClient()
        self.assertIsNone(instance._ws)

        async def scenario():
            await instance._process_response(
                {"guidance": "去七天神像", "commentary": []}, {"seq": 1}
            )

        asyncio.run(scenario())
        self.assertEqual(instance._last_sent_guidance, "")
        self.assertEqual(instance._latest_guidance, "")
        self.assertEqual(instance._pending_guidance, "去七天神像")

        # 下一轮即使没有新的 guidance，也要重试待发指导。
        sent = []

        async def hook(payload):
            sent.append(payload)

        instance.send_hook = hook
        asyncio.run(instance._process_response({"commentary": []}))
        self.assertEqual(instance._last_sent_guidance, "去七天神像")
        self.assertEqual(instance._pending_guidance, "")
        self.assertEqual(sent, [{"type": "guidance", "text": "去七天神像"}])


class LLMClientTests(unittest.TestCase):
    def test_anthropic_payload(self):
        payload = llm_client.build_anthropic_payload(
            "claude-test",
            "system prompt",
            "user prompt",
            image_b64="abc123",
        )
        self.assertEqual(payload["system"], "system prompt")
        self.assertEqual(payload["model"], "claude-test")
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["type"], "base64")
        self.assertEqual(content[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(content[0]["source"]["data"], "abc123")
        self.assertEqual(content[1], {"type": "text", "text": "user prompt"})

    def test_openai_payload(self):
        payload = llm_client.build_openai_payload(
            "gpt-test",
            "system prompt",
            "user prompt",
            image_b64="abc123",
        )
        self.assertEqual(
            payload["messages"][0],
            {
                "role": "system",
                "content": "system prompt",
            },
        )
        content = payload["messages"][1]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertEqual(
            content[0]["image_url"]["url"],
            "data:image/jpeg;base64,abc123",
        )
        self.assertEqual(content[1], {"type": "text", "text": "user prompt"})

    def test_provider_resolution(self):
        self.assertEqual(
            llm_client.resolve_provider(env={"LLM_PROVIDER": "openai"}),
            "openai",
        )
        self.assertEqual(
            llm_client.resolve_provider(env={"ANTHROPIC_API_KEY": "k"}),
            "anthropic",
        )
        with self.assertRaises(llm_client.LLMError):
            llm_client.resolve_provider(env={})
        with self.assertRaises(llm_client.LLMError):
            llm_client.resolve_provider(env={"LLM_PROVIDER": "other"})

    def test_from_env_respects_max_tokens_and_reasoning_effort(self):
        client = llm_client.LLMClient.from_env(
            env={
                "LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": "k",
                "OPENAI_MODEL": "deepseek-flash",
                "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
                "LLM_MAX_TOKENS": "4096",
            }
        )
        self.assertEqual(client.max_tokens, 4096)
        old = os.environ.get("OPENAI_REASONING_EFFORT")
        os.environ["OPENAI_REASONING_EFFORT"] = "low"
        try:
            payload = client.build_payload("s", "u")
        finally:
            if old is None:
                os.environ.pop("OPENAI_REASONING_EFFORT", None)
            else:
                os.environ["OPENAI_REASONING_EFFORT"] = old
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["reasoning_effort"], "low")

    def test_from_env_requires_key(self):
        with self.assertRaises(llm_client.LLMError):
            llm_client.LLMClient.from_env(
                env={"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": ""}
            )

    def test_endpoints(self):
        anthropic = llm_client.LLMClient(provider="anthropic", api_key="k")
        self.assertTrue(anthropic.endpoint.endswith("/v1/messages"))

        openai = llm_client.LLMClient(
            provider="openai",
            api_key="k",
            base_url="http://127.0.0.1:8000/v1",
        )
        self.assertEqual(openai.endpoint, "http://127.0.0.1:8000/v1/chat/completions")

    def test_response_text_extraction(self):
        self.assertEqual(
            llm_client.extract_anthropic_text(
                {
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "text", "text": "第二段"},
                    ]
                }
            ),
            "第一段\n第二段",
        )
        self.assertEqual(
            llm_client.extract_openai_text(
                {"choices": [{"message": {"content": "纯文本"}}]}
            ),
            "纯文本",
        )
        self.assertEqual(
            llm_client.extract_openai_text(
                {
                    "choices": [
                        {"message": {"content": [{"type": "text", "text": "分段"}]}}
                    ]
                }
            ),
            "分段",
        )

    def test_http_error_is_readable(self):
        error = llm_client.make_http_error(
            "anthropic",
            401,
            '{"error":{"message":"invalid x-api-key"}}',
        )
        self.assertEqual(error.status, 401)
        self.assertIn("401", str(error))
        self.assertIn("invalid x-api-key", str(error))


# ----------------------------------------------------------------------
# aiohttp 集成测试（缺失时跳过）
# ----------------------------------------------------------------------


@unittest.skipUnless(HAVE_AIOHTTP, "aiohttp 未安装，跳过 HTTP 集成测试")
class AiohttpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _start_server(self, app):
        """在随机端口启动 aiohttp 测试服务。"""
        from aiohttp import web

        runner = web.AppRunner(app)
        await runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        site = web.SockSite(runner, sock)
        await site.start()
        return runner, port

    async def test_bettergi_memory_set_posts_bearer(self):
        from aiohttp import web

        records = []

        async def handler(request):
            records.append(
                (
                    request.method,
                    request.path,
                    request.headers.get("Authorization"),
                    await request.json(),
                )
            )
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/api/v1/agent/sessions/vtuber/memory", handler)
        runner, port = await self._start_server(app)
        try:
            timeout = llm_client.aiohttp.ClientTimeout(total=5)
            async with llm_client.aiohttp.ClientSession(timeout=timeout) as session:
                client = director.BetterGIClient(
                    session,
                    "http://127.0.0.1:{0}".format(port),
                    "test-token",
                    "vtuber",
                )
                result = await client.memory_set({"goal": "statue"})
        finally:
            await runner.cleanup()

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(records), 1)
        method, path, authorization, payload = records[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/v1/agent/sessions/vtuber/memory")
        self.assertEqual(authorization, "Bearer test-token")
        self.assertEqual(payload, {"goal": "statue"})

    async def test_llm_non_200_error_is_readable(self):
        from aiohttp import web

        async def handler(request):
            return web.json_response(
                {"error": {"message": "invalid test key"}}, status=401
            )

        app = web.Application()
        app.router.add_post("/v1/messages", handler)
        runner, port = await self._start_server(app)
        try:
            timeout = llm_client.aiohttp.ClientTimeout(total=5)
            async with llm_client.aiohttp.ClientSession(timeout=timeout) as session:
                client = llm_client.LLMClient(
                    provider="anthropic",
                    api_key="bad",
                    base_url="http://127.0.0.1:{0}/v1/messages".format(port),
                    session=session,
                )
                with self.assertRaises(llm_client.LLMError) as context:
                    await client.complete("system", "user")
        finally:
            await runner.cleanup()

        self.assertEqual(context.exception.status, 401)
        self.assertIn("401", str(context.exception))
        self.assertIn("invalid test key", str(context.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
