"""Graphiti 接入的隔离与降级测试：不装 graphiti、不连 Neo4j，全部用假客户端。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memory as memory_mod  # noqa: E402


class SanitizeTests(unittest.TestCase):
    def test_steam_id_colon_mapped(self):
        self.assertEqual(memory_mod.sanitize_group_id("steam:730"), "steam_730")

    def test_spaces_and_slashes_collapsed(self):
        self.assertEqual(memory_mod.sanitize_group_id("a b/c"), "a_b_c")

    def test_chinese_falls_back_to_stable_hash(self):
        first = memory_mod.sanitize_group_id("原神")
        second = memory_mod.sanitize_group_id("原神")
        self.assertTrue(first.startswith("g_"))
        self.assertEqual(first, second)
        self.assertTrue(first.replace("_", "").isalnum())

    def test_already_valid_untouched(self):
        self.assertEqual(memory_mod.sanitize_group_id("genshin-impact"), "genshin-impact")

    def test_empty_rejected(self):
        with self.assertRaises(memory_mod.MemoryError_):
            memory_mod.sanitize_group_id("   ")


class FakeGraphiti:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def add_episode(self, **kwargs):
        self.calls.append(("add", kwargs))

    async def search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return []

    async def close(self):
        self.closed = True


class GraphitiMemoryTests(unittest.TestCase):
    def _mem(self, group_id="steam:730", client=None, **kwargs):
        return memory_mod.GraphitiMemory(
            group_id, uri="bolt://x", user="neo4j", password="p",
            client=client or FakeGraphiti(), **kwargs,
        )

    def test_group_id_sanitized_on_construction(self):
        self.assertEqual(self._mem().group_id, "steam_730")

    def test_expected_group_is_sanitized_too(self):
        mem = memory_mod.GraphitiMemory(
            "steam:730", uri="bolt://x", user="neo4j", password="p",
            expected_group="steam:730", client=FakeGraphiti(),
        )
        self.assertEqual(mem.group_id, "steam_730")

    def test_mismatched_group_still_rejected(self):
        with self.assertRaises(memory_mod.MemoryError_):
            memory_mod.GraphitiMemory(
                "a", uri="bolt://x", user="neo4j", password="p",
                expected_group="b", client=FakeGraphiti(),
            )

    def test_add_episode_uses_sanitized_group(self):
        fake = FakeGraphiti()
        mem = self._mem(client=fake)
        mem.add_episode(memory_mod.Episode(text="按 F 交互"))
        kind, kwargs = fake.calls[0]
        self.assertEqual(kind, "add")
        self.assertEqual(kwargs["group_id"], "steam_730")

    def test_search_never_passes_empty_group_ids(self):
        fake = FakeGraphiti()
        mem = self._mem(client=fake)
        mem.search("宝箱")
        kind, kwargs = fake.calls[0]
        self.assertEqual(kind, "search")
        self.assertEqual(kwargs["group_ids"], ["steam_730"])
        self.assertTrue(all(kwargs["group_ids"]))

    def test_cross_encoder_disabled_by_default(self):
        fake = FakeGraphiti()
        mem = self._mem(client=fake)
        self.assertFalse(mem.use_cross_encoder)

    def test_noop_cross_encoder_is_optional(self):
        # graphiti 未安装时返回 None，不应该抛异常
        self.assertIn(memory_mod._make_noop_cross_encoder(), (None,))


if __name__ == "__main__":
    unittest.main()
