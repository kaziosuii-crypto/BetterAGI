"""Offline unittest suite for prototypes/streaming_executor.py.

Pure virtual-clock simulation only: no sleep, no wall-clock, no network,
no GPU, no game, no process operations. Run directly::

    python E:/BetterAGI/gs_realtime_audit/tests/test_streaming_executor.py

The suite covers A/B/C/D scheduling, mouse A/B, wheel-once, stream breaks
after g1/g2/g3/g5, the keyboard state machine (continuous keys / change /
empty group / cross-block / no stuck), >4-key truncation, illegal-key drop,
action_end + finish_reason endings, Action Underrun Rate and SimResult.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROTO = os.path.normpath(os.path.join(_HERE, "..", "prototypes"))
if _PROTO not in sys.path:
    sys.path.insert(0, _PROTO)

import streaming_executor as se  # noqa: E402


class TestPurity(unittest.TestCase):
    """The simulator must be a pure offline module."""

    def test_module_does_not_import_real_time_or_io(self):
        with open(se.__file__, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("time", "requests", "subprocess", "socket", "os",
                          "multiprocessing", "threading"):
            self.assertNotIn(forbidden, imported,
                             "simulator must not import %s" % forbidden)
        self.assertNotIn("sleep(", src)
        self.assertIn("class VirtualClock", src)

    def test_ast_parse_of_both_files(self):
        for path in (se.__file__, os.path.abspath(__file__)):
            with open(path, encoding="utf-8") as fh:
                ast.parse(fh.read())
        self.assertTrue(True)


class TestVirtualClock(unittest.TestCase):
    def test_monotonic_no_rewind_and_reset(self):
        c = se.VirtualClock(10.0)
        self.assertEqual(c.now(), 10.0)
        c.advance_to(50.0)
        self.assertEqual(c.now(), 50.0)
        c.advance_to(20.0)                       # must NOT rewind
        self.assertEqual(c.now(), 50.0)
        c.advance(25.0)
        self.assertEqual(c.now(), 75.0)
        self.assertGreaterEqual(c.advances, 2)
        c.reset(3.0)
        self.assertEqual(c.now(), 3.0)

    def test_executor_uses_injected_clock(self):
        c = se.VirtualClock()
        ex = se.StreamingExecutor(clock=c)
        ex.submit_group(1, ["W"], 0, 5)
        ex.apply_group(1, t_ms=42.0)
        self.assertEqual(c.now(), 42.0)
        self.assertTrue(any(e["t_ms"] == 42.0 for e in ex.sink.events))


class TestSanitizeKeys(unittest.TestCase):
    def test_legal_keys_kept_in_order(self):
        kept, illegal, overflow = se.sanitize_keys(["W", "Shift", "A"])
        self.assertEqual(kept, ["W", "Shift", "A"])
        self.assertEqual(illegal, [])
        self.assertEqual(overflow, [])

    def test_illegal_keys_dropped(self):
        kept, illegal, _ = se.sanitize_keys(["BOGUS", "W", "not_a_key", "Ctrl"])
        self.assertEqual(kept, ["W", "Ctrl"])
        self.assertEqual(illegal, ["BOGUS", "not_a_key"])

    def test_more_than_four_keys_truncated(self):
        kept, _, overflow = se.sanitize_keys(["W", "A", "S", "D", "Q", "E"])
        self.assertEqual(kept, ["W", "A", "S", "D"])
        self.assertEqual(overflow, ["Q", "E"])
        self.assertEqual(len(kept), se.MAX_KEYS_PER_STEP)

    def test_duplicates_collapsed_and_string_form(self):
        kept, _, _ = se.sanitize_keys("W W A A")
        self.assertEqual(kept, ["W", "A"])
        kept2, _, _ = se.sanitize_keys(["W", "W", "A"])
        self.assertEqual(kept2, ["W", "A"])

    def test_all_vocabulary_categories_accepted(self):
        # sanitize_keys caps at 4 by protocol, so check categories in batches
        for batch in (["A", "zero", "One", "LB"],
                      ["Up", "Shift", "Equal", "Space"],
                      ["D", "nine", "Twelve", "RB"],
                      ["Down", "Ctrl", "Esc", "Slash"]):
            kept, illegal, overflow = se.sanitize_keys(batch)
            self.assertEqual(kept, batch)
            self.assertEqual(illegal, [])
            self.assertEqual(overflow, [])


class TestGroupSubmission(unittest.TestCase):
    def test_submit_records_all_timestamps_and_immediate_enqueue(self):
        ex = se.StreamingExecutor()
        rec = ex.submit_group(2, ["W", "A"], t_generated_ms=12.0,
                              t_received_ms=20.0)
        self.assertEqual(rec.index, 2)
        self.assertEqual(rec.keys, ["W", "A"])
        self.assertEqual(rec.t_generated_ms, 12.0)
        self.assertEqual(rec.t_received_ms, 20.0)
        self.assertEqual(rec.t_submitted_ms, 20.0)   # immediate enqueue
        self.assertEqual(len(ex._queue), 1)
        self.assertEqual(ex._queue[0].index, 2)

    def test_duplicate_index_not_executed_twice(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W"], 0, 10)
        dup = ex.submit_group(1, ["A"], 0, 20)
        self.assertTrue(dup.duplicate)
        self.assertEqual(ex.duplicate_submissions, 1)
        self.assertEqual(sorted(ex._records_by_index), [1])
        r = ex.run(strategy="C", full_response_ms=50)
        keydowns = [e for e in r.events if e["kind"] == "keydown"]
        self.assertEqual([e["key"] for e in keydowns], ["W"])
        self.assertEqual(r.action_duplicate_count, 1)

    def test_group_flags_recorded_from_run(self):
        r = se.run_simulation(strategy="C")
        g = {x["index"]: x for x in r.groups}
        self.assertTrue(g[1]["underrun"])             # arrived 35 > slot 0
        self.assertTrue(g[1]["late"])
        self.assertFalse(g[1]["overlap"])
        self.assertGreater(g[1]["underrun_wait_ms"], 0.0)
        self.assertIn("queue_wait_ms", g[1])
        self.assertIn("actual_start_ms", g[1])
        self.assertIn("actual_end_ms", g[1])

    def test_missing_groups_never_guessed(self):
        r = se.run_simulation(se.default_submissions()[:2], strategy="C")
        self.assertEqual(r.executed_indices, [1, 2])
        self.assertEqual(r.missing_indices, [3, 4, 5, 6])
        self.assertEqual(r.guessed_indices, [])
        self.assertEqual(r.action_missing_count, 4)
        for g in r.groups:
            self.assertFalse(g["guessed"])


class TestKeyboardStateMachine(unittest.TestCase):
    def test_consecutive_same_key_stays_held(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W"], 0, 0)
        ex.submit_group(2, ["W"], 0, 33)
        ex.submit_group(3, ["W"], 0, 66)
        ex.apply_group(1)
        t2 = ex.apply_group(2)
        t3 = ex.apply_group(3)
        self.assertEqual(t2["press"], [])
        self.assertEqual(t2["keep"], ["W"])
        self.assertEqual(t3["keep"], ["W"])
        keydowns = [e for e in ex.sink.events if e["kind"] == "keydown"]
        self.assertEqual(len(keydowns), 1)           # no re-press
        self.assertEqual(ex.held, {"W"})

    def test_key_set_change_releases_old_presses_new(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W", "Shift"], 0, 0)
        ex.submit_group(2, ["A", "Shift"], 0, 33)
        ex.apply_group(1)
        t = ex.apply_group(2)
        self.assertEqual(t["release"], ["W"])
        self.assertEqual(t["press"], ["A"])
        self.assertEqual(t["keep"], ["Shift"])
        self.assertEqual(ex.held, {"A", "Shift"})

    def test_empty_group_releases_all(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W", "Shift"], 0, 0)
        ex.submit_group(2, [], 0, 33)
        ex.apply_group(1)
        t = ex.apply_group(2)
        self.assertEqual(sorted(t["release"]), ["Shift", "W"])
        self.assertEqual(t["press"], [])
        self.assertEqual(ex.held, set())
        keyups = [e for e in ex.sink.events if e["kind"] == "keyup"]
        self.assertEqual(sorted(e["key"] for e in keyups), ["Shift", "W"])

    def test_release_all_never_leaves_stuck_keys(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W", "A", "S"], 0, 0)
        ex.apply_group(1)
        self.assertEqual(ex.held, {"W", "A", "S"})
        ex.release_all(t_ms=99.0)
        self.assertEqual(ex.held, set())
        self.assertEqual(sorted(e["key"] for e in ex.sink.events
                                if e["kind"] == "keyup"), ["A", "S", "W"])

    def test_empty_group_inside_full_run_releases_all(self):
        subs = [
            {"index": 1, "keys": ["W", "Shift"], "t_generated_ms": 0, "t_received_ms": 0},
            {"index": 2, "keys": [], "t_generated_ms": 0, "t_received_ms": 0},
            {"index": 3, "keys": [], "t_generated_ms": 0, "t_received_ms": 0},
            {"index": 4, "keys": [], "t_generated_ms": 0, "t_received_ms": 0},
            {"index": 5, "keys": [], "t_generated_ms": 0, "t_received_ms": 0},
            {"index": 6, "keys": [], "t_generated_ms": 0, "t_received_ms": 0},
        ]
        r = se.run_simulation(subs, strategy="C")
        self.assertEqual(r.held_keys_at_end, [])
        self.assertEqual([e["key"] for e in r.events if e["kind"] == "keydown"],
                         ["W", "Shift"])

    def test_cross_block_state_is_preserved(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W"], 0, 0)
        ex.apply_group(1)
        self.assertEqual(ex.held, {"W"})
        ex.begin_block("block-1")                    # must NOT clear held
        self.assertEqual(ex.held, {"W"})
        ex.submit_group(1, ["W"], 0, 0)
        t = ex.apply_group(1)
        self.assertEqual(t["keep"], ["W"])
        self.assertEqual(t["press"], [])
        ex.submit_group(2, ["A"], 0, 33)
        t2 = ex.apply_group(2)
        self.assertEqual(t2["release"], ["W"])
        self.assertEqual(t2["press"], ["A"])

    def test_reset_clears_keyboard_state(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W"], 0, 0)
        ex.apply_group(1)
        ex.reset()
        self.assertEqual(ex.held, set())
        self.assertEqual(len(ex.sink.events), 0)
        self.assertEqual(ex.clock.now(), 0.0)


class TestMouseModes(unittest.TestCase):
    def _run(self, mode):
        mouse = {"x": 120.0, "y": -60.0, "z": 2.0, "t_mouse_ms": 10.0}
        return se.run_simulation(mouse=mouse, mouse_mode=mode, strategy="C")

    def test_mode_A_one_full_displacement(self):
        r = self._run("A")
        moves = [e for e in r.events if e["kind"] == "mouse_move"]
        self.assertEqual(len(moves), 1)
        self.assertAlmostEqual(moves[0]["dx"], 120.0)
        self.assertAlmostEqual(moves[0]["dy"], -60.0)
        self.assertEqual(moves[0]["group"], 0)
        self.assertEqual(r.mouse_mode, "A")

    def test_mode_B_six_even_slices_sum_to_full(self):
        r = self._run("B")
        moves = [e for e in r.events if e["kind"] == "mouse_move"]
        self.assertEqual(len(moves), se.CHUNK_STEPS)
        self.assertAlmostEqual(sum(m["dx"] for m in moves), 120.0)
        self.assertAlmostEqual(sum(m["dy"] for m in moves), -60.0)
        for m in moves:
            self.assertAlmostEqual(m["dx"], 20.0)
            self.assertAlmostEqual(m["dy"], -10.0)
        starts = [m["t_ms"] for m in moves]
        for a, b in zip(starts, starts[1:]):
            self.assertAlmostEqual(b - a, se.STEP_MS, places=3)

    def test_wheel_triggers_exactly_once_in_both_modes(self):
        for mode in ("A", "B"):
            r = self._run(mode)
            wheels = [e for e in r.events if e["kind"] == "wheel"]
            self.assertEqual(len(wheels), 1, "mode %s" % mode)
            self.assertEqual(r.wheel_trigger_count, 1)
            self.assertAlmostEqual(wheels[0]["dz"], 2.0)

    def test_duplicate_mouse_submission_never_retriggers_wheel(self):
        ex = se.StreamingExecutor()
        ex.submit_mouse(10, 10, 1, mode="A")
        plan = ex.submit_mouse(99, 99, 5, mode="B")   # ignored duplicate
        self.assertEqual(plan["mode"], "A")
        self.assertEqual(ex.duplicate_mouse_submissions, 1)
        for s in se.default_submissions():
            ex.submit_group(s["index"], s["keys"],
                            s["t_generated_ms"], s["t_received_ms"])
        r = ex.run(strategy="C")
        self.assertEqual(r.wheel_trigger_count, 1)
        self.assertEqual(len([e for e in r.events if e["kind"] == "wheel"]), 1)
        self.assertEqual(r.action_duplicate_count, 1)

    def test_zero_scroll_emits_no_wheel(self):
        r = se.run_simulation(mouse={"x": 5.0, "y": 5.0, "z": 0.0,
                                     "t_mouse_ms": 1.0}, mouse_mode="A",
                              strategy="C")
        self.assertEqual(r.wheel_trigger_count, 0)
        self.assertEqual([e for e in r.events if e["kind"] == "wheel"], [])

    def test_mouse_clamped_to_protocol_limits(self):
        r = se.run_simulation(mouse={"x": 99999.0, "y": -99999.0,
                                     "z": 99.0, "t_mouse_ms": 1.0},
                              mouse_mode="A", strategy="C")
        moves = [e for e in r.events if e["kind"] == "mouse_move"]
        wheels = [e for e in r.events if e["kind"] == "wheel"]
        self.assertAlmostEqual(moves[0]["dx"], se.MOUSE_LIMIT)
        self.assertAlmostEqual(moves[0]["dy"], -se.MOUSE_LIMIT)
        self.assertAlmostEqual(wheels[0]["dz"], se.SCROLL_LIMIT)


class TestFinishAndAbort(unittest.TestCase):
    def test_clean_stop_keeps_keys_and_marks_complete(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W"], 0, 0)
        ex.apply_group(1)
        ex.finish(t_ms=200.0, finish_reason="stop", action_end_seen=True)
        self.assertTrue(ex.complete)
        self.assertFalse(ex.incomplete)
        self.assertFalse(ex.truncated)
        self.assertEqual(ex.held, {"W"})              # carry into next block
        fin = [e for e in ex.sink.events if e["kind"] == "finish"][0]
        self.assertTrue(fin["complete"])
        self.assertTrue(fin["action_end_seen"])

    def test_length_or_missing_action_end_releases_keys(self):
        for finish, action_end in (("length", False), ("stop", False),
                                   ("content_filter", True)):
            ex = se.StreamingExecutor()
            ex.submit_group(1, ["W"], 0, 0)
            ex.apply_group(1)
            ex.finish(t_ms=200.0, finish_reason=finish,
                      action_end_seen=action_end)
            self.assertTrue(ex.incomplete, finish)
            self.assertTrue(ex.truncated, finish)
            self.assertFalse(ex.complete, finish)
            self.assertEqual(ex.held, set(), finish)

    def test_run_respects_finish_reason(self):
        r = se.run_simulation(strategy="C", finish_reason="length",
                              action_end_seen=False)
        self.assertTrue(r.incomplete)
        self.assertTrue(r.truncated)
        self.assertEqual(r.finish_reason, "length")
        self.assertEqual(r.held_keys_at_end, [])

    def test_abort_releases_all_and_never_guesses(self):
        ex = se.StreamingExecutor()
        ex.submit_group(1, ["W", "Shift"], 0, 0)
        ex.apply_group(1)
        evs = ex.abort(t_ms=77.0, reason="stream_break")
        self.assertEqual(ex.held, set())
        self.assertTrue(ex.incomplete)
        self.assertFalse(ex.complete)
        kinds = [e["kind"] for e in evs]
        self.assertIn("keyup", kinds)
        self.assertIn("abort", kinds)
        self.assertEqual([e for e in evs if e["kind"] == "abort"][0]["reason"],
                         "stream_break")

    def test_event_stream_is_chronological(self):
        r = se.run_simulation(strategy="C")
        times = [e["t_ms"] for e in r.events]
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(r.clock.now() if hasattr(r, "clock") else 0, 0)


class TestStreamInterruption(unittest.TestCase):
    """Break after g1 / g2 / g3 / g5 with a PARTIAL trace (realistic)."""

    def _check(self, b):
        subs = se.default_submissions()[:b]
        r = se.run_simulation(subs, break_after=b, strategy="C")
        self.assertEqual(r.executed_indices, list(range(1, b + 1)))
        self.assertEqual(r.missing_indices, list(range(b + 1, se.CHUNK_STEPS + 1)))
        self.assertEqual(r.guessed_indices, [])
        self.assertEqual(r.action_missing_count, se.CHUNK_STEPS - b)
        self.assertTrue(r.incomplete)
        self.assertFalse(r.complete)
        self.assertEqual(r.finish_reason, "stream_break")
        self.assertEqual(r.held_keys_at_end, [])     # all keys released
        self.assertEqual(r.break_after, b)
        # no keydown belongs to a group after the break
        for kd in (e for e in r.events if e["kind"] == "keydown"):
            self.assertLessEqual(kd["group"], b)
        # an abort event terminated the stream, no finish event
        kinds = [e["kind"] for e in r.events]
        self.assertIn("abort", kinds)
        self.assertNotIn("finish", kinds)
        # no group event is emitted for a missing group
        for g in r.groups:
            if g["index"] > b:
                self.assertTrue(g["missing"])
                self.assertFalse(g["executed"])
                self.assertFalse(g["guessed"])
        return r

    def test_break_after_g1(self):
        r = self._check(1)
        self.assertEqual([e["key"] for e in r.events if e["kind"] == "keydown"],
                         ["W"])

    def test_break_after_g2(self):
        r = self._check(2)
        self.assertEqual([e["key"] for e in r.events if e["kind"] == "keydown"],
                         ["W"])

    def test_break_after_g3(self):
        r = self._check(3)
        self.assertEqual(sorted(e["key"] for e in r.events
                                if e["kind"] == "keydown"), ["Shift", "W"])

    def test_break_after_g5(self):
        self._check(5)

    def test_break_stops_unconfirmed_input_in_time(self):
        b = 3
        r = se.run_simulation(se.default_submissions()[:b], break_after=b,
                              strategy="C")
        last_exec_end = max(g["actual_end_ms"] for g in r.groups if g["executed"])
        # the abort is emitted exactly at t_break == last_exec_end
        aborts = [e for e in r.events if e["kind"] == "abort"]
        self.assertEqual(len(aborts), 1)
        self.assertAlmostEqual(aborts[0]["t_ms"], last_exec_end, places=3)
        # no actuating input (keydown / mouse / wheel) may survive the break
        late = [e for e in r.events if e["t_ms"] > last_exec_end + 1e-6]
        effective_after = [e for e in late
                           if e["kind"] in ("keydown", "mouse_move", "wheel")]
        self.assertEqual(effective_after, [])

    def test_invalid_break_after_raises(self):
        with self.assertRaises(ValueError):
            se.run_simulation(break_after=0)
        with self.assertRaises(ValueError):
            se.run_simulation(break_after=6)


class TestUnderrun(unittest.TestCase):
    def test_underrun_rate_definition_on_C(self):
        r = se.run_simulation(strategy="C")
        self.assertEqual(r.total_groups, 6)
        # g1, g5, g6 arrived after their probe -> 3 starvation groups
        self.assertEqual(r.underrun_count, 3)
        self.assertAlmostEqual(r.action_underrun_rate, 3.0 / 6.0)
        self.assertEqual(r.action_underrun_rate,
                         r.underrun_count / r.total_groups)
        self.assertGreater(r.max_wait_ms, 0.0)
        self.assertGreaterEqual(r.max_wait_ms, r.max_queue_wait_ms)

    def test_gap_overlap_late_counts_available(self):
        r = se.run_simulation(strategy="C")
        self.assertGreater(r.gap_count, 0)
        self.assertEqual(r.overlap_count, 0)         # fixed slices never overlap
        self.assertEqual(r.late_count, 5)            # transport property
        for key in ("gap_count", "overlap_count", "late_count", "max_wait_ms",
                    "max_queue_wait_ms", "action_underrun_rate"):
            self.assertIn(key, r.summarize())

    def test_all_groups_early_has_zero_underrun(self):
        subs = [{"index": k, "keys": ["W"], "t_generated_ms": 0.0,
                 "t_received_ms": 0.0} for k in range(1, 7)]
        r = se.run_simulation(subs, strategy="C")
        self.assertEqual(r.underrun_count, 0)
        self.assertEqual(r.action_underrun_rate, 0.0)
        self.assertEqual(r.overlap_count, 0)

    def test_empty_queue_yields_six_missing_groups(self):
        r = se.run_simulation([], strategy="C",
                              mouse={"x": 0, "y": 0, "z": 0, "t_mouse_ms": 0})
        self.assertEqual(r.executed_indices, [])
        self.assertEqual(r.missing_indices, [1, 2, 3, 4, 5, 6])
        self.assertEqual(r.action_missing_count, 6)
        self.assertEqual(r.action_underrun_rate, 0.0)
        self.assertEqual(r.underrun_count, 0)
        self.assertFalse(any(g["guessed"] for g in r.groups))


class TestStrategies(unittest.TestCase):
    def test_compare_strategies_returns_all_five_variants(self):
        cmp = se.compare_strategies()
        self.assertEqual(sorted(cmp), ["A", "B", "C", "D_extend", "D_release"])
        self.assertTrue(all(isinstance(v, dict) for v in cmp.values()))

    def test_A_sync_baseline_waits_for_full_response(self):
        r = se.run_simulation(strategy="A")
        # full_response_ms = max arrival (210) + 5 = 215
        self.assertEqual(r.first_effective_input_ms, 215.0)
        self.assertEqual(r.action_underrun_rate, 0.0)   # all queued by then
        self.assertAlmostEqual(r.total_action_duration_ms, 200.0, places=3)
        # every keyboard group starts at/after the full response
        for g in r.groups:
            self.assertGreaterEqual(g["actual_start_ms"], 215.0)

    def test_B_immediate_dispatches_on_arrival(self):
        r = se.run_simulation(strategy="B")
        self.assertEqual(r.first_effective_input_ms, 10.0)   # early mouse
        self.assertEqual(r.action_underrun_rate, 0.0)
        # ASAP allows slices to overlap (50 and 60 arrive inside g1's 33 ms)
        self.assertGreater(r.overlap_count, 0)
        self.assertEqual(r.groups[0]["actual_start_ms"], 35.0)
        self.assertEqual(r.groups[1]["actual_start_ms"], 50.0)

    def test_C_fixed_slices_never_start_early_or_overlap(self):
        r = se.run_simulation(strategy="C")
        self.assertEqual(r.overlap_count, 0)
        for g in r.groups:
            self.assertGreaterEqual(g["actual_start_ms"], g["probe_ms"])
        # g2 probe 68.33 > its arrival 50 -> it waited for the slot
        self.assertGreater(r.groups[1]["actual_start_ms"],
                           r.groups[1]["t_received_ms"])
        self.assertAlmostEqual(r.action_underrun_rate, 0.5)

    def test_C_is_earlier_than_A_on_first_input(self):
        a = se.run_simulation(strategy="A")
        c = se.run_simulation(strategy="C")
        self.assertLess(c.first_effective_input_ms, a.first_effective_input_ms)

    def test_D_extend_vs_release_late_subpolicies(self):
        ext = se.run_simulation(strategy="D", late_policy="extend")
        rel = se.run_simulation(strategy="D", late_policy="release")
        self.assertEqual(ext.late_policy, "extend")
        self.assertEqual(rel.late_policy, "release")
        # same schedule -> same underrun, different safety posture
        self.assertEqual(ext.action_underrun_rate, rel.action_underrun_rate)
        self.assertGreater(ext.key_stuck_count, 0)      # keys held across gap
        self.assertEqual(rel.key_stuck_count, 0)        # safe release
        ext_notes = [e.get("note") for e in ext.events if e["kind"] == "info"]
        rel_notes = [e.get("note") for e in rel.events if e["kind"] == "info"]
        self.assertIn("extend_previous", ext_notes)
        self.assertIn("release_all", rel_notes)
        ext_kups = len([e for e in ext.events if e["kind"] == "keyup"])
        rel_kups = len([e for e in rel.events if e["kind"] == "keyup"])
        self.assertGreaterEqual(rel_kups, ext_kups)    # extra safe keyups

    def test_D_defaults_to_extend_when_policy_missing(self):
        r = se.run_simulation(strategy="D")
        self.assertEqual(r.late_policy, "extend")
        self.assertEqual(r.action_underrun_rate, 0.5)

    def test_unknown_strategy_raises(self):
        with self.assertRaises(ValueError):
            se.run_simulation(strategy="Z")

    def test_comparable_metrics_present_in_every_variant(self):
        required = {"first_effective_input_ms", "total_action_duration_ms",
                    "action_underrun_rate", "key_stuck_count",
                    "action_missing_count", "action_duplicate_count",
                    "gap_count", "overlap_count", "late_count", "max_wait_ms"}
        for name, summary in se.compare_strategies().items():
            self.assertTrue(required.issubset(summary), name)
            self.assertTrue(summary["groups"], name)


class TestSimResult(unittest.TestCase):
    def test_summarize_is_json_serializable_dict(self):
        r = se.run_simulation(strategy="C")
        d = r.summarize()
        self.assertIsInstance(d, dict)
        encoded = json.dumps(d)                      # must not raise
        self.assertGreater(len(encoded), 100)
        self.assertEqual(d["strategy"], "C")
        self.assertEqual(d["total_groups"], 6)
        self.assertTrue(d["complete"])
        self.assertEqual(d["wheel_trigger_count"], 1)
        self.assertEqual(d["held_keys_at_end"], ["A", "D"])
        self.assertEqual(len(d["groups"]), 6)
        self.assertEqual(d["event_count"], len(r.events))

    def test_last_result_and_run_id(self):
        ex = se.StreamingExecutor()
        ex.submit_mouse(1, 1, 1)
        for s in se.default_submissions():
            ex.submit_group(s["index"], s["keys"],
                            s["t_generated_ms"], s["t_received_ms"])
        r = ex.run(strategy="A")
        self.assertIs(ex.last_result, r)
        self.assertEqual(r.block_id, "block-0")
        self.assertIsInstance(r.groups, list)
        self.assertIsInstance(r.events, list)

    def test_injected_sink_callable_records_events(self):
        seen = []
        ex = se.StreamingExecutor(sink=seen.append)
        ex.submit_group(1, ["W"], 0, 0)
        ex.apply_group(1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["kind"], "keydown")
        self.assertEqual(len(ex.sink.events), 1)

    def test_custom_sink_object(self):
        class Sink:
            def __init__(self):
                self.events = []
            def emit(self, e):
                self.events.append(dict(e))
        sink = Sink()
        ex = se.StreamingExecutor(sink=sink)
        ex.submit_group(1, ["W"], 0, 0)
        ex.apply_group(1)
        self.assertEqual(len(sink.events), 1)

    def test_constants_match_official_protocol(self):
        self.assertEqual(se.CHUNK_MS, 200.0)
        self.assertEqual(se.CHUNK_STEPS, 6)
        self.assertAlmostEqual(se.STEP_MS, 200.0 / 6.0)
        self.assertEqual(se.MAX_KEYS_PER_STEP, 4)
        self.assertEqual(se.MOUSE_LIMIT, 1000.0)
        self.assertEqual(se.SCROLL_LIMIT, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
