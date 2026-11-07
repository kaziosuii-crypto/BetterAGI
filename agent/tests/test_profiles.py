"""profiles.py 的纯逻辑测试：多游戏隔离、名称严格对齐、便签上限。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profiles  # noqa: E402


class ProfileRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_slugify(self):
        self.assertEqual(profiles.slugify("Genshin Impact"), "genshin-impact")
        self.assertEqual(profiles.slugify("  原神 "), "原神")
        self.assertEqual(profiles.slugify("Honkai: Star Rail!"), "honkai-star-rail")

    def test_register_and_resolve_by_alias(self):
        profiles.register_game("genshin-impact", display_name="Genshin Impact",
                               aliases=["原神", "genshin"], root=self.root)
        for probe in ("genshin-impact", "Genshin Impact", "原神", "genshin"):
            p = profiles.resolve_game(probe, root=self.root)
            self.assertEqual(p.game_id, "genshin-impact")
            self.assertEqual(p.display_name, "Genshin Impact")
            self.assertEqual(p.group_id, "genshin-impact")

    def test_unknown_game_raises(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.resolve_game("zelda", root=self.root)

    def test_alias_conflict_rejected(self):
        profiles.register_game("genshin-impact", display_name="Genshin Impact", root=self.root)
        with self.assertRaises(profiles.ProfileError):
            profiles.register_game("star-rail", display_name="Star Rail",
                                   aliases=["Genshin Impact"], root=self.root)

    def test_two_games_are_isolated(self):
        a = profiles.register_game("game-a", root=self.root)
        b = profiles.register_game("game-b", root=self.root)
        a.write_note("A 的笔记")
        b.write_note("B 的笔记")
        self.assertIn("A 的笔记", a.read_notes())
        self.assertNotIn("A 的笔记", b.read_notes())
        self.assertNotEqual(a.dir, b.dir)
        self.assertNotEqual(a.group_id, b.group_id)


class ScratchpadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = profiles.register_game("game-a", root=self.root, note_limit=50)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_replace(self):
        self.p.write_note("第一行")
        self.p.write_note("第二行")
        text = self.p.read_notes()
        self.assertIn("第一行", text)
        self.assertIn("第二行", text)
        self.p.write_note("只有这行", mode="replace")
        self.assertEqual(self.p.read_notes(), "只有这行")

    def test_over_limit_is_rejected_not_truncated(self):
        self.p.write_note("a" * 40)
        with self.assertRaises(profiles.ProfileError) as ctx:
            self.p.write_note("b" * 20)
        self.assertIn("便签上限", str(ctx.exception))
        # 拒绝后原内容必须完好，不能截断
        self.assertEqual(len(self.p.read_notes()), 40)

    def test_single_chunk_limit(self):
        with self.assertRaises(profiles.ProfileError):
            self.p.write_note("x" * (profiles.MAX_NOTE_CHUNK + 1))

    def test_stats_and_clear(self):
        self.p.write_note("hello")
        stats = self.p.note_stats()
        self.assertEqual(stats.chars, 5)
        self.assertEqual(stats.limit, 50)
        self.assertEqual(stats.remaining, 45)
        self.p.clear_notes()
        self.assertEqual(self.p.note_stats().chars, 0)

    def test_notes_survive_reopen(self):
        self.p.write_note("重启后还要在")
        again = profiles.resolve_game("game-a", root=self.root)
        self.assertIn("重启后还要在", again.read_notes())


class KeymapAndRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = profiles.register_game("game-a", root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keymap_roundtrip(self):
        self.p.save_keymap([profiles.KeyBinding("W", "前进"),
                            profiles.KeyBinding("F", "交互", confidence=0.6, source="learned")])
        loaded = self.p.load_keymap()
        self.assertEqual([b.key for b in loaded], ["W", "F"])
        self.assertEqual(loaded[1].source, "learned")
        self.assertAlmostEqual(loaded[1].confidence, 0.6)

    def test_keymap_accepts_short_string_form(self):
        self.p.keymap_path.write_text(json.dumps({"bindings": ["W -> 前进"]}), encoding="utf-8")
        loaded = self.p.load_keymap()
        self.assertEqual((loaded[0].key, loaded[0].effect), ("W", "前进"))

    def test_rules_roundtrip(self):
        self.p.save_rules([profiles.Rule("goal", "跟随任务指引")])
        self.assertEqual(self.p.load_rules()[0].section, "goal")

    def test_bad_json_surfaces(self):
        self.p.keymap_path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(profiles.ProfileError):
            self.p.load_keymap()


if __name__ == "__main__":
    unittest.main()
