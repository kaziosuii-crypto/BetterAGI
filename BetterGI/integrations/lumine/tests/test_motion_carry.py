"""动作沿用策略的单元测试（不依赖 GPU / vLLM）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autodl_server  # noqa: E402


def idle():
    return {"action": "idle", "chunks": [{"keys": [], "dx": 0, "dy": 0, "scroll": 0}],
            "absolute": None, "thought": None}


def walk():
    return {"action": "walk", "chunks": [{"keys": ["W"], "dx": 0, "dy": 0, "scroll": 0}],
            "absolute": None, "thought": None}


class WantsMotionTests(unittest.TestCase):
    def test_only_navigate_and_explore_require_motion(self):
        self.assertTrue(autodl_server.wants_motion({"motor_mode": "navigate"}))
        self.assertTrue(autodl_server.wants_motion({"motor_mode": "explore"}))
        self.assertFalse(autodl_server.wants_motion({"motor_mode": "wait"}))
        self.assertFalse(autodl_server.wants_motion({"motor_mode": "dialogue"}))
        self.assertFalse(autodl_server.wants_motion(None))

    def test_missing_mode_defaults_to_navigate(self):
        self.assertTrue(autodl_server.wants_motion({"goal": "go"}))


class MotionCarryTests(unittest.TestCase):
    def test_motion_action_is_used_as_is(self):
        result, carried = autodl_server.apply_motion_carry(
            walk(), {"motor_mode": "navigate"}, walk(), 10.0, 10.2)
        self.assertIsNone(carried)
        self.assertIsNone(result.get("carriedOverMs"))

    def test_idle_within_window_reuses_last_motion(self):
        result, carried = autodl_server.apply_motion_carry(
            idle(), {"motor_mode": "navigate"}, walk(), 10.0, 10.5)
        self.assertEqual(carried, 500)
        self.assertEqual(result["chunks"][0]["keys"], ["W"])
        self.assertEqual(result["carriedOverMs"], 500)

    def test_idle_after_window_does_not_reuse(self):
        result, carried = autodl_server.apply_motion_carry(
            idle(), {"motor_mode": "navigate"}, walk(), 10.0, 12.0)
        self.assertIsNone(carried)
        self.assertEqual(result["chunks"][0]["keys"], [])

    def test_wait_intent_never_reuses_motion(self):
        result, carried = autodl_server.apply_motion_carry(
            idle(), {"motor_mode": "wait"}, walk(), 10.0, 10.1)
        self.assertIsNone(carried)
        self.assertEqual(result["chunks"][0]["keys"], [])

    def test_missing_history_keeps_idle(self):
        result, carried = autodl_server.apply_motion_carry(
            idle(), {"motor_mode": "navigate"}, None, 0.0, 10.0)
        self.assertIsNone(carried)
        self.assertEqual(result["chunks"][0]["keys"], [])

    def test_clock_going_backwards_is_not_treated_as_fresh(self):
        result, carried = autodl_server.apply_motion_carry(
            idle(), {"motor_mode": "navigate"}, walk(), 10.0, 9.0)
        self.assertIsNone(carried)


if __name__ == "__main__":
    unittest.main()
