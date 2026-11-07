# -*- coding: utf-8 -*-
"""便利贴（sticky）与三槽位 system 拼装的测试。

覆盖的硬约束（02_schemas.md §7 / §4.2）：
* 1K+1K 配额：单块 1025 字、合计 2049 字都必须在写前拒绝，且旧文件不动；
* 原子写：os.replace 失败时目标文件保持旧内容、.bak 留存、tmp 清理；
* 读不存在的存档返回空块（不抛）；
* 词表外键名只告警、不删原文；
* 只有慢系统能写；
* assemble_motor_system 填满三槽位、不留花括号；
* profiles 旧便签接口在新后端上照常工作。
"""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profiles
import prompt_builder as pb
import sticky


class StickyTestCase(unittest.TestCase):
    """公共夹具：每个用例一个临时根目录。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.game = "eldenring"
        self.save = "run3"

    def write(self, key_binding=None, game_rules=None, **kwargs):
        return sticky.write_sticky(
            self.game, self.save, key_binding, game_rules, root=self.root, **kwargs
        )


class ReadWriteTests(StickyTestCase):
    def test_read_missing_save_returns_empty_blocks(self):
        info = sticky.read_sticky(self.game, self.save, root=self.root)
        self.assertEqual(info["key_binding"], "")
        self.assertEqual(info["game_rules"], "")
        self.assertEqual(info["raw"], "")
        self.assertEqual(info["warnings"], [])
        self.assertEqual(info["updated_at"], "")
        self.assertTrue(info["path"].endswith("run3.md"))

    def test_path_layout_is_per_save(self):
        path = sticky.sticky_path(self.game, self.save, root=self.root)
        parts = Path(path).parts
        self.assertEqual(parts[-3:], ("sticky", self.game, self.save + ".md"))
        other = sticky.sticky_path(self.game, "run4", root=self.root)
        self.assertNotEqual(path, other)

    def test_write_then_read_roundtrip(self):
        info = self.write(
            "- W -> 前进\n- E -> 交互 / 拾取",
            "- 攻击用鼠标左键\n- 药水按 R",
        )
        self.assertEqual(info["key_binding"], "- W -> 前进\n- E -> 交互 / 拾取")
        self.assertEqual(info["game_rules"], "- 攻击用鼠标左键\n- 药水按 R")
        self.assertTrue(info["raw"].startswith("<!-- sticky v1 | game: eldenring"))
        self.assertIn("save: run3", info["raw"])
        self.assertIn("## key_binding", info["raw"])
        self.assertIn("## game_rules", info["raw"])
        self.assertTrue(info["updated_at"])
        self.assertEqual(info["warnings"], [])

        again = sticky.read_sticky(self.game, self.save, root=self.root)
        self.assertEqual(again["key_binding"], info["key_binding"])
        self.assertEqual(again["game_rules"], info["game_rules"])

    def test_write_keeps_unsent_block_and_preserves_unknown_sections(self):
        self.write("- W -> 前进", "- 旧规则")
        path = Path(sticky.sticky_path(self.game, self.save, root=self.root))
        raw = path.read_text(encoding="utf-8")
        path.write_text(raw + "\n## notes\n- 自测备注\n", encoding="utf-8")

        info = self.write(game_rules="- 新规则")
        self.assertEqual(info["key_binding"], "- W -> 前进")
        self.assertEqual(info["game_rules"], "- 新规则")
        # 未知块不能被「读-改-写」吞掉：那是数据丢失，不是格式整理。
        self.assertIn("## notes", info["raw"])
        self.assertIn("- 自测备注", info["raw"])

    def test_replace_rewrites_both_blocks(self):
        self.write("- W -> 前进", "- 旧规则")
        info = self.write("- A -> 左移", "- 新规则", replace=True)
        self.assertEqual(info["key_binding"], "- A -> 左移")
        self.assertEqual(info["game_rules"], "- 新规则")
        self.assertNotIn("前进", info["raw"])

    def test_replace_requires_both_blocks(self):
        self.write("- W -> 前进", "- 旧规则")
        with self.assertRaises(sticky.StickyError) as ctx:
            self.write("- A -> 左移", replace=True)
        self.assertIn("key_binding", str(ctx.exception))
        self.assertIn("game_rules", str(ctx.exception))


class QuotaTests(StickyTestCase):
    def test_block_over_limit_1025_rejected_before_write(self):
        with self.assertRaises(sticky.StickyOverflow) as ctx:
            self.write("W" * 1025, "- 规则")
        error = ctx.exception
        self.assertEqual(error.chars, 1025)
        self.assertEqual(error.limit, 1024)
        self.assertEqual(error.block, "key_binding")
        self.assertIn("1025", str(error))
        self.assertIn("1024", str(error))
        self.assertFalse(
            Path(sticky.sticky_path(self.game, self.save, root=self.root)).exists()
        )

    def test_exactly_1024_is_allowed(self):
        info = self.write("W" * 1024, "R" * 1024)
        self.assertEqual(len(info["key_binding"]), 1024)
        self.assertEqual(len(info["game_rules"]), 1024)
        self.assertEqual(sticky.sticky_chars(info), 2048)

    def test_total_2049_rejected(self):
        with self.assertRaises(sticky.StickyOverflow):
            self.write("W" * 1024, "R" * 1025)

    def test_total_limit_branch_reports_total(self):
        original = sticky.STICKY_TOTAL_LIMIT
        sticky.STICKY_TOTAL_LIMIT = 2047
        try:
            with self.assertRaises(sticky.StickyOverflow) as ctx:
                self.write("W" * 1024, "R" * 1024)
            self.assertEqual(ctx.exception.chars, 2048)
            self.assertEqual(ctx.exception.limit, 2047)
        finally:
            sticky.STICKY_TOTAL_LIMIT = original
        self.assertFalse(
            Path(sticky.sticky_path(self.game, self.save, root=self.root)).exists()
        )

    def test_rejected_write_leaves_old_file_intact(self):
        old = self.write("- W -> 前进", "- 旧规则")
        before = Path(old["path"]).read_bytes()
        with self.assertRaises(sticky.StickyOverflow):
            self.write("W" * 1025, "- 新规则")
        self.assertEqual(Path(old["path"]).read_bytes(), before)


class AtomicWriteTests(StickyTestCase):
    def test_os_replace_failure_keeps_old_content_and_backup(self):
        first = self.write("- W -> 前进", "- 旧规则")
        path = Path(first["path"])
        before = path.read_text(encoding="utf-8")
        bak = path.with_name(path.name + ".bak")

        with mock.patch("sticky.os.replace", side_effect=OSError("磁盘写满")):
            with self.assertRaises(OSError) as ctx:
                self.write("- W -> 冲", replace=True, game_rules="- 新规则")
        self.assertIn("磁盘写满", str(ctx.exception))

        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_text(encoding="utf-8"), before)
        self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_second_write_leaves_single_backup(self):
        self.write("- W -> 前进", "- 规则一")
        info = self.write("- W -> 冲", "- 规则二", replace=True)
        path = Path(info["path"])
        bak = path.with_name(path.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertIn("规则一", bak.read_text(encoding="utf-8"))
        leftovers = list(path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class VocabularyWarningTests(StickyTestCase):
    def test_unknown_token_warns_but_text_is_kept(self):
        info = self.write(
            "- W -> 前进\n- Jump -> 跳跃\n- E -> 交互",
            "- 空格跳",
        )
        joined = "\n".join(info["warnings"])
        self.assertTrue(joined)
        self.assertIn("Jump", joined)
        self.assertNotIn("'W'", joined)
        # 词表外的 token 只能告警，不能悄悄删掉——原文必须原样留着。
        self.assertIn("- Jump -> 跳跃", info["key_binding"])
        self.assertIn("- W -> 前进", info["key_binding"])

    def test_vocab_tokens_do_not_warn(self):
        info = self.write(
            "- W A S D -> 移动\n- Mouse X Y -> 转动视角\n- Enter -> 确认\n- LB -> 左键",
            "- 规则",
        )
        self.assertEqual(info["warnings"], [])


class WriterPermissionTests(StickyTestCase):
    def test_fast_system_cannot_write(self):
        with self.assertRaises(PermissionError) as ctx:
            self.write("- W -> 前进", "- 规则", source="fast")
        self.assertIn("fast", str(ctx.exception))
        self.assertFalse(
            Path(sticky.sticky_path(self.game, self.save, root=self.root)).exists()
        )

    def test_slow_source_is_default(self):
        self.assertEqual(sticky.SLOW_SOURCE, "slow")
        info = self.write("- W -> 前进", "- 规则")
        self.assertEqual(info["game_rules"], "- 规则")


class ContentValidationTests(StickyTestCase):
    def test_script_tag_rejected(self):
        with self.assertRaises(sticky.StickyError):
            self.write("- W -> 前进", "<script>alert(1)</script>")

    def test_heading_inside_block_rejected(self):
        with self.assertRaises(sticky.StickyError):
            self.write("- W -> 前进\n## game_rules\n- 注入", "- 规则")

    def test_bad_save_id_rejected(self):
        with self.assertRaises(sticky.StickyError):
            sticky.write_sticky(self.game, "../escape", "- W -> 前进", root=self.root)


class AssembleMotorSystemTests(unittest.TestCase):
    def test_three_slots_filled_and_no_placeholders_left(self):
        system = pb.assemble_motor_system(
            "Elden Ring",
            "- 敌人近身就翻滚",
            "- W -> 前进\n- E -> 交互 / 拾取",
        )
        self.assertIn("Elden Ring", system)
        self.assertIn("Common Keys in Elden Ring", system)
        self.assertIn("- 敌人近身就翻滚", system)
        self.assertIn("- W -> 前进", system)
        self.assertIn("- E -> 交互 / 拾取", system)
        # 官方段头原文必须保留（04_acceptance L0-4）
        self.assertIn("**Game Rules**", system)
        self.assertIn("**Output Format**", system)
        self.assertIn("**Key Naming Rules**", system)
        for leftover in (
            "{GAME_NAME}",
            "{GAME_RULES}",
            "{KEY_BINDINGS}",
            "{LEARNING}",
            "{TASK}",
            "{DONE_HINT}",
            "{SPEAK_POLICY}",
        ):
            self.assertNotIn(leftover, system)

    def test_empty_slots_become_empty_strings(self):
        system = pb.assemble_motor_system("", None, None)
        self.assertNotIn("{", system)
        self.assertNotIn("}", system)
        self.assertIn("**Output Format**", system)

    def test_mouse_line_is_fixed_and_deduped(self):
        system = pb.assemble_motor_system(
            "Elden Ring",
            "- 规则",
            "- Mouse X Y -> 转动视角 / 瞄准（X>0 右转）\n- W -> 前进",
        )
        self.assertIn(pb.MOUSE_BINDING_LINE, system)
        self.assertEqual(system.count("Mouse X Y"), 1)
        self.assertIn("- W -> 前进", system)

    def test_mouse_line_present_even_without_sticky(self):
        system = pb.assemble_motor_system("Elden Ring", "- 规则", "")
        self.assertIn("- Mouse X Y -> aim / rotate the camera", system)

    def test_missing_template_raises(self):
        with self.assertRaises(FileNotFoundError):
            pb.load_motor_template(Path("definitely-missing-template.txt"))

    def test_unknown_placeholder_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            pb.assemble_motor_system("G", "R", "K", template="{BOGUS_SLOT}")
        self.assertIn("BOGUS_SLOT", str(ctx.exception))


class ProfilesDelegationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_old_note_api_still_works(self):
        profile = profiles.register_game(
            "sticky-note-test",
            display_name="Sticky Note Test",
            root=self.root,
            note_limit=50,
        )
        profile.write_note("目标是一")
        self.assertEqual(profile.note_stats().limit, 50)
        profile.write_note("再记一条")
        self.assertIn("再记一条", profile.read_notes())
        self.assertIn("目标是一", profile.read_notes())

        before = profile.read_notes()
        with self.assertRaises(profiles.ProfileError) as ctx:
            profile.write_note("x" * 60)
        self.assertIn("便签上限", str(ctx.exception))
        self.assertEqual(profile.read_notes(), before)

        profile.clear_notes()
        self.assertEqual(profile.read_notes(), "")
        self.assertEqual(profile.note_stats().chars, 0)

    def test_note_limits_follow_sticky_quota(self):
        self.assertEqual(profiles.DEFAULT_NOTE_LIMIT, sticky.STICKY_TOTAL_LIMIT)
        self.assertEqual(profiles.MAX_NOTE_CHUNK, sticky.STICKY_BLOCK_LIMIT)

        default_profile = profiles.register_game("notes-default", root=self.root)
        self.assertEqual(default_profile.note_stats().limit, 2048)

        oversized = profiles.register_game(
            "notes-oversized", root=self.root, note_limit=4000
        )
        self.assertEqual(oversized.note_stats().limit, 2048)

    def test_single_chunk_limit_still_enforced(self):
        profile = profiles.register_game("notes-chunk", root=self.root)
        with self.assertRaises(profiles.ProfileError):
            profile.write_note("x" * (profiles.MAX_NOTE_CHUNK + 1))
        self.assertEqual(profile.read_notes(), "")

    def test_note_written_atomically(self):
        profile = profiles.register_game("notes-atomic", root=self.root)
        profile.write_note("第一行")
        path = Path(profile.note_path)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "第一行")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
