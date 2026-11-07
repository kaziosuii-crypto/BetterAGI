"""学习回传闭环测试：写入 → 重建提示词 → 版本记录 → 重启仍可读。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import learning  # noqa: E402
import memory as memory_mod  # noqa: E402
import profiles  # noqa: E402
import prompt_builder as pb  # noqa: E402


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        profiles.register_game("learn-game", root=self.root)
        self.profile = profiles.open_profile("learn-game", root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_key_changes_prompt_and_version(self):
        before_bundle = pb.build_system_prompt(self.profile)
        pb.record_prompt_version(self.profile, before_bundle)
        result = learning.learn_key(self.profile, "R", "释放元素爆发")
        self.assertEqual(result.action, "added")
        self.assertTrue(result.changed)
        self.assertNotEqual(result.fingerprint_before, result.fingerprint_after)
        bundle = pb.build_system_prompt(self.profile)
        self.assertIn("R -> 释放元素爆发", bundle.text)
        self.assertGreater(result.version, before_bundle.version)

    def test_learned_key_removed_from_learning_targets(self):
        # learning_targets 是「本次构建后的待学清单」，所以要对比学习前后的两次构建。
        before = pb.build_system_prompt(self.profile)
        self.assertIn("F", before.learning_targets)
        learning.learn_key(self.profile, "F", "交互")
        after = pb.build_system_prompt(self.profile)
        self.assertNotIn("F", after.learning_targets)

    def test_duplicate_is_noop(self):
        learning.learn_key(self.profile, "G", "打开背包")
        result = learning.learn_key(self.profile, "G", "打开背包", confidence=0.5)
        self.assertEqual(result.action, "duplicate")
        self.assertFalse(result.changed)
        self.assertEqual(result.fingerprint_before, result.fingerprint_after)

    def test_manual_binding_not_overwritten(self):
        self.profile.save_keymap([profiles.KeyBinding("F", "交互", confidence=1.0, source="manual")])
        result = learning.learn_key(self.profile, "F", "投掷武器", confidence=0.6)
        self.assertEqual(result.action, "rejected")
        self.assertFalse(result.changed)
        bindings = {b.key: b for b in self.profile.load_keymap()}
        self.assertEqual(bindings["F"].effect, "交互")

    def test_learned_binding_can_be_corrected(self):
        learning.learn_key(self.profile, "X", "跳跃", confidence=0.6)
        result = learning.learn_key(self.profile, "X", "下落攻击", confidence=0.9)
        self.assertEqual(result.action, "updated")
        bindings = {b.key: b for b in self.profile.load_keymap()}
        self.assertEqual(bindings["X"].effect, "下落攻击")
        self.assertEqual(bindings["X"].confidence, 0.9)

    def test_empty_key_rejected(self):
        with self.assertRaises(learning.LearnError_):
            learning.learn_key(self.profile, "  ", "什么也没发生")

    def test_bad_confidence_rejected(self):
        with self.assertRaises(learning.LearnError_):
            learning.learn_key(self.profile, "Y", "冲刺", confidence=1.5)

    def test_learn_rule_added_and_deduped(self):
        first = learning.learn_rule(self.profile, "体力耗尽无法冲刺", section="stamina")
        self.assertEqual(first.action, "added")
        self.assertTrue(first.changed)
        second = learning.learn_rule(self.profile, "体力耗尽无法冲刺", section="stamina")
        self.assertEqual(second.action, "duplicate")

    def test_memory_receives_episode(self):
        mem = memory_mod.build_memory(self.profile, backend="local-json", root=self.root / "mem")
        learning.learn_key(self.profile, "B", "打开商店", memory=mem)
        self.assertEqual(mem.count("keymap"), 1)
        self.assertIn("B -> 打开商店", mem.search("商店")[0].text)

    def test_restart_keeps_learned_knowledge(self):
        result = learning.learn_key(self.profile, "V", "元素视野")
        check = learning.verify_restart(
            "learn-game", root=self.root, expect_key="V", expect_version=result.version
        )
        self.assertTrue(check["key_present"], check)
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["fingerprint_on_disk"], check["fingerprint_rebuilt"])


if __name__ == "__main__":
    unittest.main()
