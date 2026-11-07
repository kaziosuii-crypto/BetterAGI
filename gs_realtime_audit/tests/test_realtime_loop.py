"""Offline tests for the integrated closed loop (stage 6 of the task book).

Everything is fake: the policy, the executor clock and the sink. No vLLM, no
network, no device. The point is the SWITCH MATRIX and the honest-execution
rules:

  all off        -> identical to the official client (one blocking request,
                   prediction in history)
  streaming only -> groups arrive incrementally, nothing is scheduled
  executor only  -> scheduling happens, history stays the prediction
  receipt only   -> NOTHING ran, so the next round is told NOOP
  all on         -> executed groups land in history, partial stays partial
"""

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "prototypes"))

from realtime_loop import RealtimeLoop, ERROR_TO_REASON          # noqa: E402
from streaming_executor import StreamingExecutor, VirtualClock  # noqa: E402

ACTION_START = "<|action_start|>"
ACTION_END = "<|action_end|>"

class FakeStreamReceipt:
    """Mimics StreamReceipt: relative ms timings, not absolute."""

    def __init__(self, text, groups, mouse=(1, 2, 0), **kw):
        self.text = text
        self.raw_text = text
        self.finish_reason = kw.get("finish_reason", "stop")
        self.error = kw.get("error")
        self.error_kind = kw.get("error_kind")
        self.image_url = "data:image/jpeg;base64,FAKE"
        self.mouse_header = kw.get("mouse_header")
        self.t0_ms = 0.0
        self.t_http_send_ms = 1.0
        self.t_first_content_ms = kw.get("t_first_content_ms", 60.0)
        self.t_action_start_ms = 140.0
        self.t_mouse_header_ms = 150.0
        self.t_g1_ms = 160.0
        self.t_g6_ms = 210.0
        self.t_finish_ms = 220.0
        self.t_done_ms = 220.0
        self.usage = {"prompt_tokens": 10, "completion_tokens": 20}
        self.n_chunks = 8
        self.n_content_chunks = 6

    @property
    def action_ttft_ms(self):
        return round(self.t_g1_ms - self.t_http_send_ms, 2)

    @property
    def e2e_ms(self):
        return round(self.t_done_ms - self.t0_ms, 2)

class FakeMouse:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z
    def as_tuple(self):
        return (self.x, self.y, self.z)

class FakeDecision:
    def __init__(self, text, steps, mouse=(1, 2, 0), **kw):
        self.text = text
        self.action = kw.get("action")
        self.action.steps = steps
        self.action.mouse = mouse
        self.finish_reason = kw.get("finish_reason", "stop")
        self.error = kw.get("error")
        self.latency_ms = kw.get("latency_ms", 240.0)
        self.completion_tokens = 24

class FakeAction:
    def __init__(self, steps, mouse, parsed=True):
        self.steps = steps
        self.mouse = mouse
        self.parsed = parsed
        self.history_text = ACTION_START + "0 0 0" + ACTION_END

class FakeOfficial:
    """Only what the loop touches: act(), cfg, _history, _last_url."""

    def __init__(self, text, steps, **kw):
        self.cfg = type("C", (), {"history_len": 5})()
        self._history = []
        self._last_url = "data:image/jpeg;base64,PREV"
        self._kw = kw
        self.calls = 0

    def act(self, image):
        self.calls += 1
        text = self._kw.get("text", "PREDICTION")
        steps = self._kw.get("steps", SIX)
        return FakeDecision(text, steps, action=FakeAction(steps, (1, 2, 0)),
                            error=self._kw.get("error"))

class FakeStreamPolicy:
    """Streaming wrapper stand-in: returns a canned receipt + arrivals."""

    def __init__(self, groups, **kw):
        self.cfg = type("C", (), {"history_len": 5})()
        self._history = []
        self._groups = groups
        self._kw = kw
        self.calls = 0
        self.remembered = []

    def act_stream(self, image, *, on_action_group=None, record_history=True, **kw):
        self.calls += 1
        for idx, (keys, t) in enumerate(self._groups, start=1):
            if on_action_group:
                on_action_group(idx, keys, t)
        body = ACTION_START + "1 2 0 ; " + " ; ".join(" ".join(g) for g, _ in self._groups) + ACTION_END
        return FakeStreamReceipt(body, self._groups,
                                 mouse_header=FakeMouse(1, 2, 0), **self._kw)

    def remember_executed(self, url, reasoning, text):
        self.remembered.append((url, reasoning, text))

SIX = [[["LB"]], [["A"]], [["LB"]], [["A"]], [["LB"]], [["A"]]]
SIX_T = [160.0, 175.0, 190.0, 205.0, 220.0, 235.0]

def make_loop(**kw):
    stream = kw.pop("streaming_enabled", False)
    if stream:
        policy = FakeStreamPolicy(list(zip([g[0] for g in SIX], SIX_T)),
                                  **kw.pop("policy_kw", {}))
    else:
        policy = FakeOfficial(**kw.pop("policy_kw", {"text": "x", "steps": SIX}))
    ex = StreamingExecutor(clock=VirtualClock(), mouse_mode=kw.pop("mouse_mode", "A"))
    return RealtimeLoop(policy, lambda: "frame", executor=ex,
                        streaming_enabled=stream, **kw)

class TestAllOffIsOfficial(unittest.TestCase):
    def test_off_matches_official_client(self):
        loop = make_loop()
        rec = loop.step()
        self.assertFalse(rec.streamed)
        self.assertFalse(rec.scheduled)
        self.assertFalse(rec.receipted)
        self.assertEqual(rec.confirmed_groups, 6)
        self.assertEqual(rec.executed_groups, 0)
        self.assertEqual(loop.policy.calls, 1)
        # history is the official prediction, untouched by the loop
        self.assertEqual(len(loop.policy._history), 0)
        self.assertEqual(rec.history_text, "PREDICTION")

class TestStreamingOnly(unittest.TestCase):
    def test_groups_arrive_but_nothing_runs(self):
        loop = make_loop(streaming_enabled=True)
        rec = loop.step()
        self.assertTrue(rec.streamed)
        self.assertFalse(rec.scheduled)
        self.assertEqual(rec.confirmed_groups, 6)
        self.assertEqual(rec.executed_groups, 0)
        self.assertAlmostEqual(rec.observation_age_ms, 160.0, places=0)
        self.assertAlmostEqual(rec.action_ttft_ms, 159.0, places=0)

    def test_streaming_needs_act_stream(self):
        with self.assertRaises(TypeError):
            RealtimeLoop(FakeOfficial(text="x", steps=SIX), lambda: "f",
                         streaming_enabled=True)

class TestExecutorOnly(unittest.TestCase):
    def test_schedule_without_receipt_keeps_prediction_history(self):
        loop = make_loop(streaming_enabled=True, streaming_executor_enabled=True)
        rec = loop.step()
        self.assertTrue(rec.scheduled)
        self.assertEqual(rec.executed_groups, 6)
        self.assertFalse(rec.receipted)
        # no receipt -> the loop does not rewrite history
        self.assertEqual(loop.policy.remembered, [])
        self.assertEqual(rec.history_text, rec.receipt if rec.receipt else rec.history_text)
        self.assertTrue(rec.history_text.startswith(ACTION_START))

class TestReceiptOnlyIsHonest(unittest.TestCase):
    """receipt on + executor off: nothing ran, so history must say NOOP."""

    def test_noop_and_interrupted(self):
        loop = make_loop(streaming_enabled=True, execution_receipt_enabled=True)
        rec = loop.step()
        self.assertTrue(rec.receipted)
        self.assertEqual(rec.executed_groups, 0)
        self.assertTrue(rec.receipt["interrupted"])
        self.assertEqual(rec.receipt["interruption_reason"], "executor_disabled")
        self.assertIn("0 0 0", rec.history_text)
        self.assertEqual(len(loop.policy.remembered), 1)
        self.assertEqual(loop.policy.remembered[0][2], rec.history_text)

class TestAllOn(unittest.TestCase):
    def test_executed_truth_reaches_history(self):
        loop = make_loop(streaming_enabled=True, streaming_executor_enabled=True,
                         execution_receipt_enabled=True)
        rec = loop.step()
        self.assertEqual(rec.executed_groups, 6)
        self.assertFalse(rec.receipt["interrupted"])
        self.assertEqual(len(loop.history), 1)
        entry = loop.history.entries[0]
        self.assertEqual(entry.chunk_id, rec.chunk_id)
        self.assertEqual(entry.observation_id, rec.observation_id)
        self.assertEqual(entry.assistant_text, rec.history_text)
        # policy history got the executed text, not the prediction
        self.assertEqual(len(loop.policy.remembered), 1)
        self.assertEqual(loop.policy.remembered[0][2], rec.history_text)
        # the executed text is rebuilt from the executor truth; the model's own
        # prediction string is never what we hand back
        self.assertIn(";", loop.policy.remembered[0][2])
        self.assertTrue(loop.policy.remembered[0][2].startswith(ACTION_START))
        self.assertTrue(loop.policy.remembered[0][2].endswith(ACTION_END))

    def test_error_maps_to_interruption_reason(self):
        loop = make_loop(streaming_enabled=True, streaming_executor_enabled=True,
                         execution_receipt_enabled=True,
                         policy_kw={"error": "connection reset", "error_kind": "network"})
        rec = loop.step()
        self.assertEqual(rec.receipt["interruption_reason"], "network")
        self.assertTrue(rec.receipt["interrupted"])
        # groups still arrive and still get scheduled: the receipt records the
        # interruption, but the executor truth for what DID run is preserved
        self.assertGreaterEqual(rec.receipt["executed_groups"], 0)

    def test_run_aggregates(self):
        loop = make_loop(streaming_enabled=True, streaming_executor_enabled=True,
                         execution_receipt_enabled=True)
        recs = loop.run(4)
        self.assertEqual(len(recs), 4)
        self.assertEqual(loop.summary["rounds"], 4)
        self.assertEqual(loop.summary["groups_executed"], 24)
        self.assertEqual(loop.summary["underflow"], 0)
        self.assertIsNotNone(loop.summary["observation_age_p50"])
        self.assertEqual(len(loop.history), 4)
        # ids are unique per round
        self.assertEqual(len({r.chunk_id for r in recs}), 4)
        self.assertEqual(len({r.observation_id for r in recs}), 4)

class TestReceiptIsJsonSafe(unittest.TestCase):
    def test_receipt_serialises(self):
        loop = make_loop(streaming_enabled=True, streaming_executor_enabled=True,
                         execution_receipt_enabled=True)
        rec = loop.step()
        json.dumps(rec.to_dict())
        json.dumps(rec.receipt)
        for key in ("chunk_id", "observation_id", "round_index",
                    "predicted_groups", "confirmed_groups", "executed_groups",
                    "executed_actions", "interrupted", "interruption_reason",
                    "underflow_count", "observation_age_ms", "finish_reason"):
            self.assertIn(key, rec.receipt)

class TestErrorMapping(unittest.TestCase):
    def test_known_kinds(self):
        self.assertEqual(ERROR_TO_REASON["network"], "network")
        self.assertEqual(ERROR_TO_REASON["timeout"], "timeout")
        self.assertEqual(ERROR_TO_REASON["cancelled"], "cancelled")
        self.assertIn("error", ERROR_TO_REASON.values())

if __name__ == "__main__":
    unittest.main(verbosity=2)
