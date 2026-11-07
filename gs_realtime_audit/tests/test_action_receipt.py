"""Tests for the execution receipt and the executed-truth history.

The receipt is what the next prompt is built from, so the rules under test
are the ones the task book states: a predicted group that never ran must
never appear as executed, a partial chunk keeps only what ran, and a chunk
that did not run at all becomes the official no-op.

Run: python tests/test_action_receipt.py
"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "prototypes"))

import action_execution_receipt as ar


def _receipt(**kw):
    base = dict(chunk_id="c1", observation_id="o1", round_index=1,
                predicted_action="<|action_start|>1 2 0 ; A ; B ; C ; D ; E ; F<|action_end|>",
                predicted_groups=6, confirmed_groups=6)
    base.update(kw)
    return ar.ActionExecutionReceipt(**base)


class TestReceiptDefaults(unittest.TestCase):
    def test_scalar_and_container_defaults_are_not_tuples(self):
        r = ar.ActionExecutionReceipt()
        self.assertEqual(r.finish_reason, "")
        self.assertEqual(r.interruption_reason, "")
        self.assertEqual(r.executed_actions, [])
        self.assertEqual(r.execution_errors, [])
        self.assertEqual(r.notes, {})

    def test_to_dict_is_json_safe(self):
        import json
        r = _receipt()
        s = json.dumps(r.to_dict())
        self.assertIn("observation_age_at_first_action_ms", s)
        self.assertIn("is_complete", s)


class TestExecutedTruth(unittest.TestCase):
    def _executed(self, groups, mouse=(1, 2, 0), interrupted=False, reason=""):
        acts = [{"index": i + 1, "keys": list(g)} for i, g in enumerate(groups)]
        return _receipt(executed_groups=len(groups), executed_actions=acts,
                        executed_mouse=list(mouse), interrupted=interrupted,
                        interruption_reason=reason)

    def test_full_run_is_recorded_as_six_groups(self):
        text = ar.receipt_to_history_text(
            self._executed([["A"], ["B"], ["C"], ["D"], ["E"], ["F"]]))
        self.assertTrue(text.startswith(ar.ACTION_START))
        self.assertTrue(text.endswith(ar.ACTION_END))
        self.assertIn("1 2 0 ; A ; B ; C ; D ; E ; F", text)

    def test_partial_run_keeps_only_executed_groups(self):
        text = ar.receipt_to_history_text(
            self._executed([["A"], ["B"], ["C"]], interrupted=True,
                           reason=ar.INTERRUPTED_NETWORK))
        body = text.split(ar.ACTION_START)[1].split(ar.ACTION_END)[0]
        slots = [s.strip() for s in body.split(";")[1:]]
        self.assertEqual(slots[:3], ["A", "B", "C"])
        self.assertEqual(slots[3:], ["", "", ""],
                         "unexecuted groups must stay empty, not be filled in")
        self.assertIn("network", text)

    def test_zero_executed_becomes_official_noop(self):
        r = _receipt(executed_groups=0, executed_actions=[], interrupted=True,
                     interruption_reason=ar.INTERRUPTED_TIMEOUT)
        self.assertEqual(ar.receipt_to_history_text(r), ar.NOOP_ACTION_TEXT)

    def test_partial_text_is_still_parseable_by_official_format(self):
        text = ar.receipt_to_history_text(
            self._executed([["LB"], ["LB"]], interrupted=True,
                           reason=ar.INTERRUPTED_SUPERSEDED))
        body = text.split(ar.ACTION_START)[1].split(ar.ACTION_END)[0]
        self.assertEqual(len(body.split(";")), 1 + ar.CHUNK_STEPS)

    def test_predicted_and_executed_are_separate_fields(self):
        r = self._executed([["A"]], interrupted=True, reason=ar.INTERRUPTED_NETWORK)
        self.assertEqual(r.predicted_groups, 6)
        self.assertEqual(r.executed_groups, 1)
        self.assertFalse(r.is_complete)

    def test_complete_flag_requires_no_errors(self):
        r = self._executed([["A"], ["B"], ["C"], ["D"], ["E"], ["F"]])
        self.assertTrue(r.is_complete)
        r.execution_errors.append("socket died")
        self.assertFalse(r.is_complete)


class TestExecutionHistory(unittest.TestCase):
    def test_history_stores_executed_not_predicted(self):
        h = ar.ExecutionHistory(max_len=5)
        r = _receipt(executed_groups=3,
                     executed_actions=[{"index": i + 1, "keys": ["A"]} for i in range(3)],
                     executed_mouse=[0, 0, 0], interrupted=True,
                     interruption_reason=ar.INTERRUPTED_NETWORK)
        e = h.add("o1", "delta://cur", r)
        self.assertEqual(e.receipt, r)
        self.assertIn("A ; A ; A", e.assistant_text)
        self.assertNotIn("B", e.assistant_text)

    def test_history_is_bounded_and_drops_oldest(self):
        h = ar.ExecutionHistory(max_len=2)
        for i in range(4):
            h.add("o%d" % i, "u", _receipt(round_index=i))
        self.assertEqual([e.observation_id for e in h], ["o2", "o3"])

    def test_history_keeps_chunk_id_for_correlation(self):
        h = ar.ExecutionHistory(max_len=5)
        h.add("o1", "u", _receipt(chunk_id="chunk-42"))
        self.assertEqual(h.entries[0].chunk_id, "chunk-42")


class TestTiming(unittest.TestCase):
    def test_observation_age_uses_first_action(self):
        r = _receipt(observation_timestamp=1000.0, first_action_timestamp=1350.0)
        self.assertAlmostEqual(r.observation_age_at_first_action_ms, 350.0)

    def test_observation_age_is_zero_without_timestamps(self):
        self.assertEqual(ar.ActionExecutionReceipt().observation_age_at_first_action_ms, 0.0)

    def test_inference_latency_and_span(self):
        r = _receipt(inference_start_timestamp=1200.0,
                     first_action_timestamp=1350.0,
                     execution_start_timestamp=1400.0,
                     execution_end_timestamp=1600.0)
        self.assertAlmostEqual(r.inference_latency_ms, 150.0)
        self.assertAlmostEqual(r.execution_span_ms, 200.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
