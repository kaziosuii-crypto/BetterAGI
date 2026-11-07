#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lumine_protocol 的单元测试。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import lumine_protocol as lp  # noqa: E402


class ParseActionTests(unittest.TestCase):
    """动作块解析。"""

    def test_missing_start_token_is_recovered(self):
        """vLLM 丢掉起始特殊 token 时，仍能用动作体加结束标记解析。"""

        text = "0 0 ; W ; W ; ; ; ;<|action_end|>"
        result = lp.parse_action(text)
        self.assertEqual(result["raw"], text)
        self.assertEqual(result["chunks"][0]["keys"], ["W"])
        self.assertEqual(result["chunks"][1]["keys"], ["W"])

    def test_relative_action(self):
        text = (
            "<|action_start|>5 -10 2 ; W ; W A ; Shift ; ; Space ; "
            "LB RB MB XB1 XB2<|action_end|>"
        )
        result = lp.parse_action(text)

        self.assertEqual(result["action"], text)
        self.assertEqual(result["raw"], text)
        self.assertIsNone(result["absolute"])
        self.assertIsNone(result["thought"])
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["chunks"]), 6)

        # 一个 200ms 动作只有一个移动前缀，只放在第一个键块。
        first = result["chunks"][0]
        self.assertEqual((first["dx"], first["dy"], first["scroll"]), (5, -10, 2))
        for chunk in result["chunks"][1:]:
            self.assertEqual((chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0))

        self.assertEqual(result["chunks"][0]["keys"], ["W"])
        self.assertEqual(result["chunks"][1]["keys"], ["W", "A"])
        self.assertEqual(result["chunks"][2]["keys"], ["Shift"])
        self.assertEqual(result["chunks"][3]["keys"], [])
        self.assertEqual(result["chunks"][4]["keys"], ["Space"])
        # 超过 4 个键时截断。
        self.assertEqual(result["chunks"][5]["keys"], ["LB", "RB", "MB", "XB1"])

    def test_absolute_action(self):
        text = (
            "<|action_start|><use_absolute> 640 360 3 ; LB ; ; ; ; ;"
            "<|action_end|>"
        )
        result = lp.parse_action(text)

        self.assertEqual(result["absolute"], {"x": 640, "y": 360})
        self.assertEqual(len(result["chunks"]), 6)
        self.assertEqual(result["chunks"][0]["scroll"], 3)
        for chunk in result["chunks"]:
            self.assertEqual(chunk["dx"], 0)
            self.assertEqual(chunk["dy"], 0)
        for chunk in result["chunks"][1:]:
            self.assertEqual(chunk["scroll"], 0)
        self.assertEqual(result["chunks"][0]["keys"], ["LB"])
        self.assertEqual(result["chunks"][5]["keys"], [])

    def test_thought_before_action(self):
        text = (
            "<|thought_start|>Move ahead carefully.<|thought_end|>"
            "<|action_start|>0 0 0 ; W ; ; ; ; ;<|action_end|>"
        )
        result = lp.parse_action(text)
        self.assertEqual(result["thought"], "Move ahead carefully.")
        self.assertEqual(result["chunks"][0]["keys"], ["W"])

    def test_thought_after_action(self):
        text = (
            "<|action_start|>0 0 0 ; W ; ; ; ; ;<|action_end|>"
            "<|thought_start|> done <|thought_end|>"
        )
        result = lp.parse_action(text)
        self.assertEqual(result["thought"], "done")

    def test_thought_example_is_not_used_as_action(self):
        text = (
            "<|thought_start|>Example: <|action_start|>99 0 0 ; W<|action_end|>"
            "<|thought_end|><|action_start|>7 0 0 ; W ; ; ; ; ;<|action_end|>"
        )
        result = lp.parse_action(text)
        self.assertNotIn("99", result["action"])
        self.assertEqual(result["chunks"][0]["dx"], 7)
        for chunk in result["chunks"][1:]:
            self.assertEqual(chunk["dx"], 0)

    def test_last_action_block_wins(self):
        text = (
            "<|action_start|>1 0 0 ; W ; ; ; ; ;<|action_end|>"
            "<|action_start|>2 0 0 ; A ; ; ; ; ;<|action_end|>"
        )
        result = lp.parse_action(text)
        self.assertEqual(result["chunks"][0]["keys"], ["A"])
        self.assertEqual(result["chunks"][0]["dx"], 2)

    def test_relative_clamps(self):
        result = lp.parse_action(
            "<|action_start|>5000 -5000 22 ; ; ; ; ; ;<|action_end|>"
        )
        chunk = result["chunks"][0]
        self.assertEqual((chunk["dx"], chunk["dy"], chunk["scroll"]), (1000, -1000, 5))
        for trailing in result["chunks"][1:]:
            self.assertEqual((trailing["dx"], trailing["dy"], trailing["scroll"]), (0, 0, 0))

    def test_absolute_clamps(self):
        result = lp.parse_action(
            "<|action_start|><use_absolute> -5 9999 -9 ; ; ; ; ; ;<|action_end|>"
        )
        self.assertEqual(result["absolute"], {"x": 0, "y": 719})
        self.assertEqual(result["chunks"][0]["scroll"], -5)

    def test_missing_chunks_are_padded(self):
        result = lp.parse_action(
            "<|action_start|>3 4 -1 ; W<|action_end|>"
        )
        self.assertEqual(len(result["chunks"]), 6)
        self.assertEqual(result["chunks"][0]["keys"], ["W"])
        self.assertEqual(
            (
                result["chunks"][0]["dx"],
                result["chunks"][0]["dy"],
                result["chunks"][0]["scroll"],
            ),
            (3, 4, -1),
        )
        for chunk in result["chunks"][1:]:
            self.assertEqual(chunk["keys"], [])
            self.assertEqual((chunk["dx"], chunk["dy"], chunk["scroll"]), (0, 0, 0))

    def test_extra_chunks_are_truncated(self):
        result = lp.parse_action(
            "<|action_start|>0 0 0 ; W ; W ; W ; W ; W ; W ; W ; W<|action_end|>"
        )
        self.assertEqual(len(result["chunks"]), 6)

    def test_prefix_only_has_empty_chunks(self):
        result = lp.parse_action("<|action_start|>0 0 0<|action_end|>")
        self.assertEqual(len(result["chunks"]), 6)
        self.assertTrue(all(not chunk["keys"] for chunk in result["chunks"]))

    def test_keys_are_deduped_and_unknown_tokens_ignored(self):
        result = lp.parse_action(
            "<|action_start|>0 0 0 ; W W Foo A ; zero One ; ; ; ;<|action_end|>"
        )
        self.assertEqual(result["chunks"][0]["keys"], ["W", "A"])
        self.assertEqual(result["chunks"][1]["keys"], ["zero", "One"])

    def test_mouse_and_named_keys_are_canonicalized(self):
        result = lp.parse_action(
            "<|action_start|>0 0 0 ; lb rb shift ; ; ; ; ;<|action_end|>"
        )
        self.assertEqual(result["chunks"][0]["keys"], ["LB", "RB", "Shift"])

    def test_digit_and_function_key_casing_is_significant(self):
        result = lp.parse_action(
            "<|action_start|>0 0 0 ; One Zero twelve ; ; ; ; ;<|action_end|>"
        )
        # one 合法，Zero/twelve 不是官方写法，被忽略。
        self.assertEqual(result["chunks"][0]["keys"], ["One"])

    def test_whitespace_and_newlines(self):
        result = lp.parse_action(
            "\n <|action_start|>\n 1   2  3 \n ;\n W\n ; ; ; ;\n"
            "<|action_end|>\n"
        )
        self.assertEqual(result["chunks"][0]["keys"], ["W"])
        self.assertEqual(result["chunks"][0]["dx"], 1)

    def test_missing_action_block(self):
        for text in ("", "   ", "no action here", "<|thought_start|>x<|thought_end|>"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                lp.parse_action(text)

    def test_non_string_input(self):
        for value in (None, 123, b"<|action_start|>0 0 0<|action_end|>"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lp.parse_action(value)

    def test_empty_action_block(self):
        with self.assertRaises(ValueError):
            lp.parse_action("<|action_start|>  <|action_end|>")

    def test_leading_end_token_is_stripped(self):
        """模型重复输出结束标记时，仍能解析后面的动作体。"""

        result = lp.parse_action("<|action_end|>0 0 0 ; W<|action_end|>")
        self.assertEqual(result["chunks"][0]["keys"], ["W"])

    def test_invalid_relative_prefix(self):
        for prefix in ("5 0 0 0", "x 0 0", "5.0 0 0", "5;0;0"):
            text = "<|action_start|>{0}<|action_end|>".format(prefix)
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                lp.parse_action(text)

    def test_empty_prefix_is_idle(self):
        """模型只输出空键块时，按原地不动处理。"""

        result = lp.parse_action("<|action_start|> ; ; ; ; ; ; <|action_end|>")
        first = result["chunks"][0]
        self.assertEqual((first["dx"], first["dy"], first["scroll"]), (0, 0, 0))
        self.assertTrue(all(chunk["keys"] == [] for chunk in result["chunks"]))

    def test_two_number_prefix_defaults_scroll_zero(self):
        """模型省略滚轮值时按 0 处理。"""

        result = lp.parse_action("<|action_start|>5 -2 ; W<|action_end|>")
        first = result["chunks"][0]
        self.assertEqual((first["dx"], first["dy"], first["scroll"]), (5, -2, 0))

    def test_invalid_absolute_prefix(self):
        for prefix in ("<use_absolute> 640 360", "<use_absolute> x 360 0"):
            text = "<|action_start|>{0}<|action_end|>".format(prefix)
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                lp.parse_action(text)


class PromptTests(unittest.TestCase):
    """提示词构造。"""

    def test_pretrain_messages(self):
        messages = lp.build_messages("沿着道路前进", "pretrain")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn(lp.ACTION_START, messages[0]["content"])
        self.assertIn(lp.ABSOLUTE_TOKEN, messages[0]["content"])
        self.assertIn("沿着道路前进", messages[1]["content"])
        self.assertIn("Guidance:", messages[1]["content"])

    def test_no_guidance(self):
        messages = lp.build_messages("", "pretrain")
        self.assertNotIn("Guidance:", messages[1]["content"])

    def test_instruct_prompt_is_richer(self):
        pretrain = lp.build_messages("g", "pretrain")
        instruct = lp.build_messages("g", "instruct")
        self.assertGreater(len(instruct[0]["content"]), len(pretrain[0]["content"]))
        self.assertIn("expert", instruct[0]["content"].lower())
        self.assertIn("g", instruct[1]["content"])

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            lp.build_messages("", "nope")

    def test_append_image(self):
        messages = lp.build_messages("guide", "pretrain")
        text = messages[1]["content"]
        result = lp.append_image(messages, "QUJD")
        self.assertIs(result, messages)
        content = result[1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": text})
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        )

    def test_append_data_url_is_kept(self):
        messages = lp.build_messages("", "pretrain")
        url = "data:image/jpeg;base64,QUJD"
        lp.append_image(messages, url)
        self.assertEqual(messages[1]["content"][1]["image_url"]["url"], url)

    def test_append_image_rejects_empty(self):
        messages = lp.build_messages("", "pretrain")
        with self.assertRaises(ValueError):
            lp.append_image(messages, "")

    def test_chunk_example_parses(self):
        parsed = lp.parse_action(lp.build_chunk_example())
        self.assertEqual(len(parsed["chunks"]), 6)
        self.assertTrue(all(chunk["keys"] == ["W"] for chunk in parsed["chunks"]))

    def test_format_chunks_round_trip(self):
        parsed = lp.parse_action(
            "<|action_start|>5 -4 2 ; W ; W A ; ; Space ; LB<|action_end|>"
        )
        rebuilt = lp.format_chunks(parsed["chunks"], parsed["absolute"])
        reparsed = lp.parse_action(rebuilt)
        self.assertEqual(reparsed["chunks"], parsed["chunks"])
        self.assertEqual(reparsed["absolute"], parsed["absolute"])


class MotorAttributionTests(unittest.TestCase):
    """归属字段严格校验，不接受隐式数值转换。"""

    def test_attribution(self):
        value = {"intentId": "i-1", "motorGeneration": 0, "actionGroupId": "g"}
        self.assertEqual(lp.motor_attribution(value), value)
        for generation in (True, -1, 1.0, "1", None):
            self.assertIsNone(lp.motor_attribution({"intentId": "i", "motorGeneration": generation}))
        for intent_id in (None, "", " x "):
            self.assertIsNone(lp.motor_attribution({"intentId": intent_id, "motorGeneration": 1}))
        for group in (None, "", " g ", 123, True, "x" * 129):
            self.assertIsNone(lp.motor_attribution({"intentId": "i", "motorGeneration": 1, "actionGroupId": group}))
        self.assertEqual(lp.motor_attribution({"intentId": "i", "motorGeneration": 1}),
                         {"intentId": "i", "motorGeneration": 1})


class MotorPromptTests(unittest.TestCase):
    """Motor 模式只执行 intent，不自行决定叙事目标。"""

    def setUp(self):
        self.intent = {
            "intent_id": "i-test",
            "goal": "跟随安柏去骑士团",
            "reason": "继续主线",
            "motor_mode": "navigate",
            "constraints": ["不要离开任务区域"],
            "target": {"kind": "npc", "name": "安柏"},
            "deadline_ms": 15000,
        }

    def test_motor_prompt_contains_intent_and_no_goal_choice(self):
        messages = lp.build_messages("保持距离", "motor", intent=self.intent)
        system = messages[0]["content"].lower()
        user = messages[1]["content"]
        self.assertIn("motorcontroller", system)
        self.assertIn("do not choose goals", system)
        self.assertNotIn("expert genshin impact player", system)
        self.assertIn("i-test", user)
        self.assertIn("跟随安柏去骑士团", user)
        self.assertIn("不要离开任务区域", user)
        self.assertIn("constraint", user.lower())

    def test_dialogue_permissions_are_exposed_without_implied_authorization(self):
        intent = {**self.intent, "motor_mode": "dialogue",
                  "dialogue_permissions": {"allow_skip": False, "allow_select": True}}
        messages = lp.build_motor_messages(intent)
        self.assertIn('"allow_skip":false', messages[1]["content"])
        self.assertIn('"allow_select":true', messages[1]["content"])
        system = messages[0]["content"].lower()
        self.assertIn("in wait or dialogue mode output only idle", system)
        self.assertIn("never choose an option or emit keys or clicks for dialogue", system)

    def test_motor_without_intent_requests_idle(self):
        messages = lp.build_motor_messages(None)
        self.assertIn("idle", messages[1]["content"].lower())
        with self.assertRaises(ValueError):
            lp.build_messages("", "unknown")

    def test_existing_modes_unchanged(self):
        before = lp.build_messages("向前走", "pretrain")
        self.assertIn("next 200 ms action", before[1]["content"])


if __name__ == "__main__":
    unittest.main()
