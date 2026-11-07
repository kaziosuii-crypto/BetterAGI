"""memory / intent 测试：group_id 隔离、TTL、dwell、克制门槛。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import intent as intent_mod  # noqa: E402
import memory as memory_mod  # noqa: E402
import profiles  # noqa: E402


class MemoryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.a = profiles.register_game("game-a", root=self.root)
        self.b = profiles.register_game("game-b", root=self.root)
        self.mem_a = memory_mod.build_memory(self.a, backend="local-json", root=self.root / "mem")
        self.mem_b = memory_mod.build_memory(self.b, backend="local-json", root=self.root / "mem")

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_ids_differ(self):
        self.assertEqual(self.mem_a.group_id, "game-a")
        self.assertEqual(self.mem_b.group_id, "game-b")

    def test_write_then_search_scoped(self):
        self.mem_a.add_episode(memory_mod.Episode(text="风起地有七天神像", kind="place"))
        self.mem_b.add_episode(memory_mod.Episode(text="黑塔空间站有宝箱", kind="place"))
        self.assertEqual(len(self.mem_a.search("七天神像")), 1)
        self.assertEqual(len(self.mem_b.search("七天神像")), 0)
        self.assertEqual(len(self.mem_b.search("宝箱")), 1)

    def test_cross_group_write_rejected(self):
        with self.assertRaises(memory_mod.MemoryError_):
            self.mem_a.add_episode(memory_mod.Episode(text="串味", group_id="game-b"))

    def test_bad_kind_rejected(self):
        with self.assertRaises(memory_mod.MemoryError_):
            self.mem_a.add_episode(memory_mod.Episode(text="x", kind="nonsense"))

    def test_kind_filter_and_count(self):
        self.mem_a.add_episode(memory_mod.Episode(text="按 F 交互", kind="keymap"))
        self.mem_a.add_episode(memory_mod.Episode(text="体力会耗尽", kind="rule"))
        self.assertEqual(self.mem_a.count(), 2)
        self.assertEqual(self.mem_a.count("rule"), 1)
        self.assertEqual(len(self.mem_a.search("", kinds=("keymap",))), 1)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class IntentBusTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bus = intent_mod.IntentBus(min_dwell=2.0, min_confidence=0.5, clock=self.clock)

    def test_starts_as_noop(self):
        self.assertEqual(self.bus.current().type, "NO_OP")

    def test_low_confidence_rejected(self):
        ok, why = self.bus.accept(intent_mod.Intent("navigate", "派蒙", urgency=0.9, confidence=0.3))
        self.assertFalse(ok)
        self.assertIn("置信度", why)
        self.assertEqual(self.bus.current().type, "NO_OP")

    def test_accept_then_dwell_blocks(self):
        ok, _ = self.bus.accept(intent_mod.Intent("navigate", "蒙德城", urgency=0.5, confidence=0.9))
        self.assertTrue(ok)
        self.clock.now += 0.5
        ok2, why2 = self.bus.accept(intent_mod.Intent("combat", "史莱姆", urgency=0.5, confidence=0.9))
        self.assertFalse(ok2)
        self.assertIn("最小驻留", why2)
        self.assertEqual(self.bus.current().type, "navigate")

    def test_urgent_can_preempt(self):
        self.bus.accept(intent_mod.Intent("navigate", "蒙德城", urgency=0.5, confidence=0.9))
        self.clock.now += 0.5
        ok, why = self.bus.accept(intent_mod.Intent("combat", "史莱姆", urgency=0.95, confidence=0.9))
        self.assertTrue(ok, why)
        self.assertEqual(self.bus.current().type, "combat")

    def test_ttl_expiry_falls_back_to_noop(self):
        self.bus.accept(intent_mod.Intent("navigate", "蒙德城", urgency=0.5, confidence=0.9, ttl=5.0))
        self.clock.now += 6.0
        self.assertEqual(self.bus.current().type, "NO_OP")

    def test_noop_stops_immediately(self):
        self.bus.accept(intent_mod.Intent("navigate", "蒙德城", urgency=0.5, confidence=0.9))
        self.clock.now += 0.1
        ok, why = self.bus.accept(intent_mod.Intent("NO_OP"))
        self.assertTrue(ok)
        self.assertEqual(self.bus.current().type, "NO_OP")

    def test_same_intent_renews_not_switches(self):
        self.bus.accept(intent_mod.Intent("navigate", "蒙德城", urgency=0.5, confidence=0.9))
        self.clock.now += 3.0
        ok, why = self.bus.accept(intent_mod.Intent("navigate", "蒙德城", urgency=0.6, confidence=0.9))
        self.assertFalse(ok)
        self.assertIn("续期", why)
        self.assertEqual(len(self.bus.history()), 1)

    def test_bad_type_rejected(self):
        with self.assertRaises(ValueError):
            intent_mod.Intent("teleport", "x")


if __name__ == "__main__":
    unittest.main()
