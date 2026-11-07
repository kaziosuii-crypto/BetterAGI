#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent.py 的离线单测：工具表里不许有手动输入，主循环要能正确串起工具调用。

不联网：模型用假的 client，BetterGI 用假的 tools。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import lithos  # noqa: E402


class ToolSurfaceTests(unittest.TestCase):
    def test_tool_surface_has_single_leg_and_no_finish(self):
        names = {spec["function"]["name"] for spec in agent.TOOL_SPECS}
        # pad（虚拟手柄）已按需求 FR-1 完全移除：动作只经 lumine 一条腿。
        # end_test 是开发环境专用的收尾工具，面里一直列着（能否生效由运行时三重判定）。
        self.assertEqual(names, {"scan", "look", "lumine", "end_test"})
        self.assertNotIn("finish", names, "root 代理不该有宣布完成的工具")
        text = json.dumps(agent.TOOL_SPECS, ensure_ascii=False)
        for forbidden in ('"key"', '"click"', '"moveMouse"', "betteragi_play"):
            self.assertNotIn(forbidden, text, f"工具表里不该出现 {forbidden}")

    def test_system_prompt_has_no_pad_and_forbids_raw_injection(self):
        # FR-1：pad 彻底删除，提示词里不许再留第二条腿。
        self.assertNotIn("pad", agent.SYSTEM_PROMPT.lower())
        self.assertIn("lumine", agent.SYSTEM_PROMPT)
        self.assertIn("不要试图用键盘/鼠标 API 直接注入", agent.SYSTEM_PROMPT)

    def test_scan_schema_is_plain_json_schema(self):
        spec = next(item for item in agent.TOOL_SPECS if item["function"]["name"] == "scan")
        self.assertEqual(spec["function"]["parameters"]["type"], "object")
        self.assertNotIn('"json"', json.dumps(spec))


class FakeTools:
    def __init__(self):
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "look":
            return agent.ToolResult("look", True, '{"frameId":"f1"}', "data:image/jpeg;base64,AAAA")
        if name == "lumine":
            return agent.ToolResult("lumine", True, '{"ok":true,"heldSec":8}')
        return agent.ToolResult(name, True, '{"uiCategory":"Main"}')


class FakeClient:
    """按脚本逐轮返回预设的工具调用。"""

    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    def complete(self, messages, *, urgency=None, tools=None, **kwargs):
        self.seen.append(list(messages))
        call = self.script.pop(0) if self.script else None
        if call is None:
            return lithos.LLMReply(text="结束", tool_calls=[], tier="fast", model="m", latency_ms=1)
        name, args = call
        tool_calls = [
            {"id": f"call-{len(self.seen)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        ]
        return lithos.LLMReply(text="", tool_calls=tool_calls, tier="fast", model="m", latency_ms=1)


class LoopTests(unittest.TestCase):
    def setUp(self):
        self._orig_tools = agent.BetterGITools
        self._orig_client = lithos.LithosClient
        self.fake_tools = FakeTools()

    def tearDown(self):
        agent.BetterGITools = self._orig_tools
        lithos.LithosClient = self._orig_client

    def _install(self, script):
        fake_tools = self.fake_tools
        agent.BetterGITools = lambda session, **kw: fake_tools  # type: ignore[assignment]
        lithos.LithosClient = lambda *a, **kw: FakeClient(script)  # type: ignore[assignment]

    def test_text_reply_ends_the_turn_without_finish(self):
        # 没有 finish 工具后，正常收尾就是这一轮不调工具、直接说话。
        self._install([("scan", {})])
        result = agent.run("测试", steps=5, log_root=Path("E:/BetterAGI/_diag/agent-runs-test"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "结束")
        self.assertEqual([call[0] for call in self.fake_tools.calls], ["scan"])

    def test_image_result_becomes_user_content(self):
        self._install([("look", {})])
        client_holder = {}

        class CapturingClient(FakeClient):
            def complete(self, messages, **kwargs):
                client_holder["messages"] = list(messages)
                return super().complete(messages, **kwargs)

        agent.BetterGITools = lambda session, **kw: self.fake_tools  # type: ignore[assignment]
        lithos.LithosClient = lambda *a, **kw: CapturingClient([("look", {})])  # type: ignore[assignment]
        agent.run("看图", steps=3, log_root=Path("E:/BetterAGI/_diag/agent-runs-test"))
        contents = [message.get("content") for message in client_holder["messages"]]
        image_message = next(
            (content for content in contents if isinstance(content, list) and any(part.get("type") == "image_url" for part in content)),
            None,
        )
        self.assertIsNotNone(image_message, "图片没有作为 image_url 内容块进入消息")
        self.assertTrue(image_message[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_tool_failure_is_reported_not_raised(self):
        class Broken(FakeTools):
            def dispatch(self, name, arguments):
                raise RuntimeError("BetterGI 502")

        agent.BetterGITools = lambda session, **kw: Broken()  # type: ignore[assignment]
        lithos.LithosClient = lambda *a, **kw: FakeClient([("scan", {})])  # type: ignore[assignment]
        result = agent.run("测试", steps=3, log_root=Path("E:/BetterAGI/_diag/agent-runs-test"))
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
