"""意图裁决测试：唯一控制通道是 set_intent 函数调用，文本不驱动动作。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import arbiter  # noqa: E402
import intent as intent_mod  # noqa: E402


def call(name="set_intent", **args):
    return {"name": name, "call_id": "c1", "arguments": args}


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ToolSpecTests(unittest.TestCase):
    def test_spec_is_openai_function_schema(self):
        spec = arbiter.INTENT_TOOL_SPECS[0]
        self.assertEqual(spec["type"], "function")
        self.assertEqual(spec["function"]["name"], "set_intent")
        self.assertEqual(
            spec["function"]["parameters"]["properties"]["type"]["enum"],
            ["NO_OP", "navigate", "explore", "combat", "interact", "wait"],
        )

    def test_only_type_is_required(self):
        self.assertEqual(arbiter.INTENT_TOOL_SPECS[0]["function"]["parameters"]["required"], ["type"])


class IntentFromCallTests(unittest.TestCase):
    def test_valid_call(self):
        parsed = arbiter.intent_from_call(call(type="navigate", target="蒙德城", urgency=0.6, confidence=0.9))
        self.assertEqual(parsed.type, "navigate")
        self.assertEqual(parsed.target, "蒙德城")
        self.assertEqual(parsed.urgency, 0.6)

    def test_arguments_may_be_json_string(self):
        parsed = arbiter.intent_from_call(
            {"name": "set_intent", "arguments": json.dumps({"type": "wait", "ttl": 5})}
        )
        self.assertEqual(parsed.type, "wait")
        self.assertEqual(parsed.ttl, 5.0)

    def test_unknown_tool_rejected(self):
        with self.assertRaises(arbiter.ArbiterError_):
            arbiter.intent_from_call(call(name="teleport", type="navigate", target="x"))

    def test_bad_type_rejected(self):
        with self.assertRaises(arbiter.ArbiterError_):
            arbiter.intent_from_call(call(type="teleport", target="x"))

    def test_missing_target_rejected_for_action(self):
        with self.assertRaises(arbiter.ArbiterError_):
            arbiter.intent_from_call(call(type="navigate"))

    def test_noop_allows_empty_target(self):
        self.assertEqual(arbiter.intent_from_call(call(type="NO_OP")).type, "NO_OP")

    def test_out_of_range_clamped(self):
        parsed = arbiter.intent_from_call(call(type="combat", target="丘丘人", urgency=5, confidence=-3))
        self.assertEqual(parsed.urgency, 1.0)
        self.assertEqual(parsed.confidence, 0.0)

    def test_bad_json_rejected(self):
        with self.assertRaises(arbiter.ArbiterError_):
            arbiter.intent_from_call({"name": "set_intent", "arguments": "{not json"})

    def test_no_call_rejected(self):
        with self.assertRaises(arbiter.ArbiterError_):
            arbiter.intent_from_call(None)


class ArbiterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.bus = intent_mod.IntentBus(min_dwell=1.0, min_confidence=0.5, clock=self.clock)
        self.arb = arbiter.IntentArbiter(self.bus, min_confidence=0.5)

    def test_text_alone_never_moves(self):
        result = self.arb.submit("我打算前往蒙德城找凯瑟琳")
        self.assertFalse(result.accepted)
        self.assertEqual(result.intent.type, "NO_OP")
        self.assertEqual(result.evidence, "text_only")

    def test_valid_call_accepted(self):
        result = self.arb.submit("", function_call=call(type="navigate", target="蒙德城"))
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(self.bus.current().type, "navigate")
        self.assertEqual(result.motor_mode, "navigate")
        self.assertEqual(result.evidence, "function_call")

    def test_unknown_tool_is_noop_with_evidence(self):
        result = self.arb.submit("", function_call=call(name="teleport", type="navigate", target="x"))
        self.assertFalse(result.accepted)
        self.assertEqual(result.intent.type, "NO_OP")
        self.assertEqual(result.evidence, "schema_violation")

    def test_low_confidence_downgraded(self):
        result = self.arb.submit("", function_call=call(type="explore", confidence=0.2))
        self.assertFalse(result.accepted)
        self.assertIn("置信度", result.reason)
        self.assertEqual(self.bus.current().type, "NO_OP")

    def test_dwell_blocks_rapid_switch(self):
        self.arb.submit("", function_call=call(type="navigate", target="蒙德城"))
        self.clock.now += 0.2
        result = self.arb.submit("", function_call=call(type="combat", target="丘丘人", urgency=0.5))
        self.assertFalse(result.accepted)
        self.assertEqual(self.bus.current().type, "navigate")

    def test_noop_bypasses_dwell(self):
        self.arb.submit("", function_call=call(type="navigate", target="蒙德城"))
        self.clock.now += 0.2
        result = self.arb.submit("", function_call=call(type="NO_OP"))
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(self.bus.current().type, "NO_OP")

    def test_wait_gets_no_motor_mode(self):
        result = self.arb.submit("", function_call=call(type="wait", ttl=3))
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.motor_mode, "")

    def test_result_serializable(self):
        payload = self.arb.submit("", function_call=call(type="interact", target="背包")).as_dict()
        self.assertEqual(payload["type"], "interact")
        self.assertEqual(payload["target"], "背包")


if __name__ == "__main__":
    unittest.main()
