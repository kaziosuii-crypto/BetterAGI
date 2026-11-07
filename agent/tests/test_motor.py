import unittest

from agent import BetterGITools, Session  # noqa: F401  (import 顺序与运行时而一致)
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from profiles import open_profile


class MotorSurfaceTests(unittest.TestCase):
    def test_motor_is_preferred_tool_for_non_genshin(self):
        p = open_profile("undertale")
        t = BetterGITools.__new__(BetterGITools)
        t.profile = p
        t.keymap = None
        names = [s["function"]["name"] for s in t.tool_specs()]
        # 双系统分工（2026-11-04 用户口径 m05832）：慢系统只下意图，动作只有一个入口 lumine。
        # gamescaling 一次 1.6s 且不给键盘组，只能当操作器内部的参考，绝不暴露给慢系统。
        self.assertIn("lumine", names)
        for forbidden in ("motor", "act", "press", "type", "finish_intent"):
            self.assertNotIn(forbidden, names)
        # 2026-11-04：screen（让快系统替慢系统读屏）已删除。慢系统直接 look 拿真图，
        # 快系统只在执行意图后回一段时段总结，绝不充当眼睛。
        self.assertNotIn("screen", names)
        hand = [s for s in t.tool_specs() if s["function"]["name"] == "lumine"][0]
        self.assertIn("意图", hand["function"]["description"])

    def test_motor_reports_unavailable_clearly(self):
        import motor
        c = motor.GamescalingClient(endpoint="http://127.0.0.1:9")
        r = c.health(timeout=1.0)
        self.assertFalse(r["available"])
        self.assertIn("endpoint", r)


class ActionParseTests(unittest.TestCase):
    """gamescaling 的动作串是 7 段：鼠标 XYZ + 恰好 6 组按键。

    格式见 agent/templates/gamescaling_system_prompt.txt 第 14 行。
    """

    def test_full_seven_part_action(self):
        import motor
        out = motor._parse_action(
            "<|action_start|>-3 12 1 ; z ; up up ; ; x ; ; enter<|action_end|>")
        self.assertTrue(out["parsed"])
        self.assertEqual(out["mouse"], {"x": -3, "y": 12, "z": 1})
        self.assertEqual(out["steps"], [["z"], ["up", "up"], [], ["x"], [], ["enter"]])
        self.assertEqual(out["keys"], ["z", "up", "x", "enter"])
        self.assertEqual(out["duration_ms"], 200)
        self.assertEqual(out["step_ms"], 33)

    def test_empty_groups_stay_six_steps(self):
        """全零动作是合法决策：6 组空，steps 仍然要有 6 个空列表。"""
        import motor
        out = motor._parse_action("<|action_start|>0 0 0 ;   ;   ;   ;   ;   ;  <|action_end|>")
        self.assertTrue(out["parsed"])
        self.assertEqual(out["mouse"], {"x": 0, "y": 0, "z": 0})
        self.assertEqual(out["steps"], [[], [], [], [], [], []])
        self.assertEqual(out["keys"], [])

    def test_pads_missing_groups_to_six(self):
        import motor
        out = motor._parse_action("<|action_start|>1 2 3 ; z<|action_end|>")
        self.assertEqual(len(out["steps"]), 6)
        self.assertEqual(out["steps"][0], ["z"])
        self.assertEqual(out["steps"][5], [])

    def test_group_capped_at_four_keys(self):
        import motor
        out = motor._parse_action("<|action_start|>0 0 0 ; w a s d q<|action_end|>")
        self.assertEqual(out["steps"][0], ["w", "a", "s", "d"])

    def test_steps_preserve_simultaneous_presses(self):
        """拍平会丢掉"哪几个键同时按"，连招就打成乱按——这里必须是分组。"""
        import motor
        out = motor._parse_action("<|action_start|>0 0 0 ; w ; ; w ; ; <|action_end|>")
        self.assertEqual(out["steps"][0], ["w"])
        self.assertEqual(out["steps"][1], [])
        self.assertEqual(out["steps"][2], ["w"])

    def test_parse_action_without_marker(self):
        import motor
        out = motor._parse_action("press x")
        self.assertFalse(out["parsed"])
        self.assertEqual(out["keys"], [])
        self.assertEqual(len(out["steps"]), 6)

    def test_template_matches_parser_contract(self):
        """模板里写的格式必须和解析器一致，否则模型照着写、我们读不懂。"""
        import motor
        from prompt_builder import TEMPLATE_PATH
        text = TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn("X Y Z ; k1 k2 k3 ; k4 k5 ; k6 ; k7 ; k8 ; k9 k10", text)
        sample = "<|action_start|>X Y Z ; k1 k2 k3 ; k4 k5 ; k6 ; k7 ; k8 ; k9 k10<|action_end|>"
        out = motor._parse_action(sample)
        self.assertEqual(len(out["steps"]), 6)
        self.assertEqual(out["mouse"], {"x": 0, "y": 0, "z": 0})



def test_parse_truncated_action_block_keeps_keys():
    """服务端 stop='<|action_end|>' 会切掉收尾标签；此时动作体依然必须被采用。

    2026-11-04 实测：2B 每轮都以思考开头，最后一个 action 块没有 <|action_end|>，
    旧正则会匹配不到 → raw='' → 整轮被当成空动作，手一动不动。
    """
    import motor as motor_mod

    close_think = "</" + "think>"
    text = " I should press Z.\n" + close_think + "\n\n<|action_start|>3 -2 0 ; Z ;   Z ;   ;   ;   "
    parsed = motor_mod._parse_action(text)
    assert parsed["parsed"] is True
    assert parsed["keys"] == ["Z"]
    assert parsed["steps"][0] == ["Z"]
    assert parsed["mouse"] == {"x": 3, "y": -2, "z": 0}


def test_parse_prefers_last_complete_block_over_earlier_draft():
    import motor as motor_mod

    close_think = "</" + "think>"
    text = (
        "<|action_start|>0 0 0 ; Enter ;   ;   ;   ;   <|action_end|>"
        + close_think
        + "\n<|action_start|>1 0 0 ; Z ;   ;   ;   ;   <|action_end|>"
    )
    parsed = motor_mod._parse_action(text)
    assert parsed["keys"] == ["Z"]


if __name__ == "__main__":
    unittest.main()