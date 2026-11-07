#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenViking 记忆桥的纯逻辑测试（不需要真实服务）。"""

import asyncio
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))

import memory_bridge  # noqa: E402


class FakeClient:
    async def search(self, **kwargs):
        return {
            "memories": [
                {
                    "uri": "viking://user/default/memories/genshin/live/s.md",
                    "level": 2,
                    "abstract": "在须弥城拿到了宝箱。",
                }
            ],
            "resources": [
                {
                    "uri": "viking://user/default/resources/genshin/a.md",
                    "level": 2,
                    "abstract": "目录摘要",
                },
                {
                    "uri": "viking://user/default/resources/genshin/.abstract.md",
                    "level": 0,
                    "abstract": "应被跳过",
                },
            ],
        }

    async def read(self, uri, limit=-1):
        if uri.endswith("a.md"):
            return "# 须弥\n须弥是草神的国度，核心城市是须弥城。"
        return "在须弥城拿到了宝箱。"

    async def write(self, *args, **kwargs):
        return {"ok": True}

    async def close(self):
        return None


class SettingsTests(unittest.TestCase):
    def test_defaults_are_disabled(self):
        settings = memory_bridge.MemorySettings.from_env({})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.url, "http://127.0.0.1:1933")

    def test_url_enables(self):
        settings = memory_bridge.MemorySettings.from_env(
            {"OPENVIKING_URL": "http://127.0.0.1:1933"}
        )
        self.assertTrue(settings.enabled)


class CaptureClient:
    def __init__(self):
        self.writes = []

    async def write(self, uri, content, mode="replace", wait=False):
        self.writes.append((uri, content, mode, wait))
        return {"ok": True}


class WriteTests(unittest.TestCase):
    def test_remember_session_uses_unique_run_uri(self):
        bridge_a = memory_bridge.MemoryBridge(
            memory_bridge.MemorySettings(enabled=True)
        )
        bridge_b = memory_bridge.MemoryBridge(
            memory_bridge.MemorySettings(enabled=True)
        )
        client_a = CaptureClient()
        client_b = CaptureClient()
        bridge_a.client = client_a
        bridge_b.client = client_b
        bridge_a.available = True
        bridge_b.available = True

        asyncio.run(bridge_a.remember_session("s1", "摘要 A"))
        asyncio.run(bridge_b.remember_session("s1", "摘要 B"))

        uri_a = client_a.writes[0][0]
        uri_b = client_b.writes[0][0]
        self.assertIn("/live/s1-", uri_a)
        self.assertIn("/live/s1-", uri_b)
        self.assertNotEqual(uri_a, uri_b)
        self.assertEqual(client_a.writes[0][2], "replace")


class RecallTests(unittest.TestCase):
    def test_recall_formats_memories_and_resources(self):
        bridge = memory_bridge.MemoryBridge(memory_bridge.MemorySettings(enabled=True))
        bridge.client = FakeClient()
        bridge.available = True
        text = asyncio.run(bridge.recall("须弥城 宝箱"))
        self.assertIn("[往事]", text)
        self.assertIn("拿到了宝箱", text)
        self.assertIn("[设定]", text)
        self.assertIn("草神的国度", text)
        self.assertNotIn("应被跳过", text)

    def test_compact_collapses_whitespace(self):
        self.assertEqual(memory_bridge._compact("a\n\n b\tc", 20), "a b c")


if __name__ == "__main__":
    unittest.main()
