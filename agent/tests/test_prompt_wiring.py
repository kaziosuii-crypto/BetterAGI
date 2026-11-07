"""多游戏提示词装配测试：插入位置、人设保留、缺 profile 回退。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
import profiles  # noqa: E402


class ComposeTests(unittest.TestCase):
    def test_knowledge_goes_before_hard_rules(self):
        base = "你是主播。\n\n硬规则：\n1. 不许编。\n"
        out = agent.compose_system_prompt(base, "【键位】F -> 交互")
        self.assertLess(out.index("【键位】"), out.index("硬规则："))
        self.assertIn("你是主播。", out)
        self.assertIn("1. 不许编。", out)

    def test_persona_still_first(self):
        out = agent.compose_system_prompt(agent.SYSTEM_PROMPT, "【规则】体力会耗尽")
        self.assertTrue(out.startswith(agent.SYSTEM_PROMPT.split("\n")[0]))

    def test_empty_knowledge_returns_base(self):
        self.assertEqual(agent.compose_system_prompt("abc\n硬规则：x", "  "), "abc\n硬规则：x")

    def test_missing_seam_appends(self):
        out = agent.compose_system_prompt("没有分界", "知识块")
        self.assertIn("知识块", out)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_game_returns_static(self):
        text, meta = agent.resolve_system_prompt("")
        self.assertEqual(text, agent.SYSTEM_PROMPT)
        self.assertEqual(meta["source"], "static")

    def test_unknown_game_falls_back_with_reason(self):
        text, meta = agent.resolve_system_prompt("不存在的游戏", profile_root=self.root)
        self.assertEqual(text, agent.SYSTEM_PROMPT)
        self.assertEqual(meta["source"], "static")
        self.assertTrue(meta["fallback_reason"])

    def test_known_game_injects_keymap(self):
        profiles.register_game("test-game", root=self.root)
        profile = profiles.open_profile("test-game", root=self.root)
        profile.save_keymap([profiles.KeyBinding("X", "特殊能力按键")])
        text, meta = agent.resolve_system_prompt("test-game", profile_root=self.root)
        self.assertEqual(meta["source"], "profile")
        self.assertEqual(meta["game"], "test-game")
        self.assertIn("特殊能力按键", text)
        self.assertIn("硬规则：", text)
        self.assertTrue(meta["fingerprint"].startswith("sha256:"))

    def test_direct_channel_gets_operator_persona(self):
        """非原神通道拿到"操作器"人设：慢系统只下意图，按键不在它手上。"""

        profiles.register_game("direct-game", root=self.root)
        text, meta = agent.resolve_system_prompt("direct-game", profile_root=self.root)
        self.assertEqual(meta["channel"], "direct")
        self.assertIn("操作器", text)
        self.assertIn("按键不在你手上", text)
        self.assertNotIn("你只有一条“腿”", text)
        self.assertIn("lumine", text)
        # 慢系统不该拿到任何按键原语（REQ-2026-11-04 FR-9：慢系统只产出意图）
        for forbidden in ("finish_intent(", "act(keys)", "press(spec)", "type(text)"):
            self.assertNotIn(forbidden, text)

    def test_genshin_channel_keeps_lumine_persona(self):
        text, meta = agent.resolve_system_prompt("genshin-impact", profile_root=self.root)
        self.assertEqual(meta["source"], "static")  # 测试环境没有原神 profile
        self.assertTrue(agent.channel_uses_lumine(None))
        self.assertIn("你只有一条“腿”", agent.base_system_prompt(None))

    def test_channel_helpers(self):
        class FakeProfile:
            game_id = "undertale"

        self.assertFalse(agent.channel_uses_lumine(FakeProfile()))
        self.assertEqual(agent.base_system_prompt(FakeProfile()), agent.DIRECT_SYSTEM_PROMPT)
        self.assertTrue(agent.channel_uses_lumine(type("P", (), {"game_id": "genshin-impact"})()))

    def test_resolve_records_version(self):
        profiles.register_game("ver-game", root=self.root)
        agent.resolve_system_prompt("ver-game", profile_root=self.root)
        profile = profiles.open_profile("ver-game", root=self.root)
        motor = profile.ensure_config().get("motor") or {}
        self.assertGreaterEqual(int(motor.get("system_prompt_version", 0)), 1)
        self.assertTrue(str(motor.get("system_prompt_fingerprint", "")).startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
