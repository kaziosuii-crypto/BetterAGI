"""prompt_builder.py 测试：三段注入、未知不编、置信度门控、版本可回溯。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profiles  # noqa: E402
import prompt_builder as pb  # noqa: E402


class PromptBuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = profiles.register_game(
            "genshin-impact", display_name="Genshin Impact", aliases=["原神"], root=self.root
        )
        self.p.save_keymap([profiles.KeyBinding("W", "向前移动"), profiles.KeyBinding("F", "交互")])
        self.p.save_rules([profiles.Rule("goal", "跟随左侧任务指引")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_sections_present(self):
        bundle = pb.build_system_prompt(self.p, version=1)
        self.assertIn("Genshin Impact", bundle.text)
        self.assertIn("跟随左侧任务指引", bundle.text)
        self.assertIn("- W -> 向前移动", bundle.text)
        self.assertIn("action_start", bundle.text)
        self.assertNotIn("{GAME_NAME}", bundle.text)
        self.assertNotIn("{KEY_BINDINGS}", bundle.text)

    def test_unknown_keys_listed_not_invented(self):
        bundle = pb.build_system_prompt(self.p, version=1)
        self.assertIn("E", bundle.learning_targets)
        self.assertIn(pb.UNKNOWN_MARK, bundle.text)
        # 没学到的键不许有编出来的效果
        self.assertNotIn("- E ->", bundle.text)

    def test_low_confidence_dropped(self):
        self.p.save_keymap([
            profiles.KeyBinding("W", "向前移动"),
            profiles.KeyBinding("Z", "也许是翻滚", confidence=0.2, source="learned"),
        ])
        bundle = pb.build_system_prompt(self.p, version=1)
        self.assertNotIn("也许是翻滚", bundle.text)
        self.assertIn("Z", bundle.dropped_low_confidence)

    def test_memory_entries_merge(self):
        bundle = pb.build_system_prompt(
            self.p,
            version=2,
            memory_rules=[profiles.Rule("combat", "元素反应造成额外伤害")],
            memory_bindings=[profiles.KeyBinding("E", "元素战技", source="learned")],
        )
        self.assertIn("元素反应造成额外伤害", bundle.text)
        self.assertIn("- E -> 元素战技", bundle.text)
        self.assertNotIn("E", bundle.learning_targets)

    def test_fingerprint_and_version_tracked(self):
        a = pb.build_system_prompt(self.p, version=3)
        b = pb.build_system_prompt(self.p, version=3)
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertTrue(a.fingerprint.startswith("sha256:"))
        pb.record_prompt_version(self.p, a)
        motor = self.p.load_config()["motor"]
        self.assertEqual(motor["system_prompt_version"], 3)
        self.assertEqual(motor["system_prompt_fingerprint"], a.fingerprint)

    def test_changed_keymap_changes_fingerprint(self):
        a = pb.build_system_prompt(self.p, version=1)
        self.p.save_keymap([profiles.KeyBinding("W", "向前移动")])
        b = pb.build_system_prompt(self.p, version=2)
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_missing_rules_says_learning(self):
        empty = profiles.register_game("empty-game", root=self.root)
        bundle = pb.build_system_prompt(empty, version=1)
        self.assertIn(pb.UNKNOWN_MARK, bundle.text)


if __name__ == "__main__":
    unittest.main()
