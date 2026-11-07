"""Unit tests for incremental_action_parser.

The official parser is loaded straight from audit/official_code/action.py so
the comparison runs against the real production code, not a copy.

Run:  python tests/test_incremental_parser.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OFFICIAL = os.path.join(ROOT, "audit", "official_code", "action.py")
sys.path.insert(0, os.path.join(ROOT, "prototypes"))


def _load_official():
    spec = importlib.util.spec_from_file_location("gs_official_action", OFFICIAL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gs_official_action"] = mod
    spec.loader.exec_module(mod)
    return mod


official = _load_official()

import incremental_action_parser as iap  # noqa: E402

# Point the parser at the official constants and Action class.
for _name in ("ACTION_START", "ACTION_END", "THINK_OPEN", "THINK_CLOSE", "CHUNK_STEPS",
              "MAX_KEYS_PER_STEP", "MOUSE_LIMIT", "SCROLL_LIMIT", "KEY_VOCAB", "Action",
              "parse_action", "split_reasoning"):
    setattr(iap, _name, getattr(official, _name))

AS = official.ACTION_START
AE = official.ACTION_END

FULL = AS + "-36 -4 0 ; LB S ; LB ; LB ; D LB ; D LB ; D LB" + AE


def feed_all(text, chunks=None):
    """Feed text through the parser, optionally split into pieces."""
    parser = iap.IncrementalActionParser()
    events = []
    if chunks is None:
        events.extend(parser.feed(text))
    else:
        for piece in chunks:
            events.extend(parser.feed(piece))
    events.extend(parser.finish("stop"))
    return parser, events


class TestNormal(unittest.TestCase):
    def test_full_output(self):
        parser, events = feed_all(FULL)
        self.assertTrue(parser.complete)
        self.assertEqual(parser.confirmed_groups, 6)
        self.assertIsNotNone(parser.header)
        self.assertEqual(parser.header.as_tuple(), (-36.0, -4.0, 0.0))
        kinds = [e.kind for e in events]
        self.assertEqual(kinds.count("group"), 6)
        self.assertEqual(kinds.count("mouse_header"), 1)
        self.assertIn("complete", kinds)

    def test_keys_per_group(self):
        parser, _ = feed_all(FULL)
        got = [g.keys for g in parser.groups]
        self.assertEqual(got, [["LB", "S"], ["LB"], ["LB"], ["D", "LB"], ["D", "LB"], ["D", "LB"]])

    def test_group_indexes(self):
        parser, _ = feed_all(FULL)
        self.assertEqual([g.index for g in parser.groups], [1, 2, 3, 4, 5, 6])


class TestParityWithOfficial(unittest.TestCase):
    """A complete reply must parse identically to the official parse_action."""

    def _assert_parity(self, text, finish_reason="stop"):
        cmp_ = iap.compare_with_official(text, finish_reason=finish_reason)
        self.assertTrue(cmp_["equal"],
                        "mismatch for %r: %r" % (text, cmp_["matches"]))
        return cmp_

    def test_parity_simple(self):
        self._assert_parity(FULL)

    def test_parity_empty_groups(self):
        self._assert_parity(AS + "0 0 0 ; ; ; ; ; ;" + AE)

    def test_parity_mixed(self):
        self._assert_parity(AS + "12 -7 3 ; Shift A ; ; Ctrl ; A ; Enter ; Esc" + AE)

    def test_parity_illegal_key_dropped(self):
        cmp_ = self._assert_parity(AS + "1 2 3 ; A BOGUS C ; ; ; ; ; ;" + AE)
        self.assertIn("BOGUS", cmp_["official"].dropped)

    def test_parity_too_many_keys(self):
        self._assert_parity(AS + "1 2 3 ; A B C D E F ; ; ; ; ; ;" + AE)

    def test_parity_mouse_clamped(self):
        self._assert_parity(AS + "5000 -5000 99 ; ; ; ; ; ; ;" + AE)

    def test_parity_reasoning_only_is_unparsed_in_both(self):
        text = "<think>" + AE + " the format is " + AS + "9 9 9 ; A ; B ; C ; D ; E ; F" + AE + "</think>"
        cmp_ = iap.compare_with_official(text)
        self.assertTrue(cmp_["equal"], "mismatch: %r" % (cmp_["matches"],))
        self.assertFalse(cmp_["official"].parsed)

    def test_parity_reasoning_then_action(self):
        text = ("<think>" + AS + "9 9 9 ; X ; Y ; Z ; W ; V ; U" + AE + "</think>"
                + AS + "1 2 3 ; A ; B ; C ; D ; E ; F" + AE)
        cmp_ = self._assert_parity(text)
        self.assertEqual(cmp_["incremental"].mouse, (1.0, 2.0, 3.0))

    def test_parity_token_by_token(self):
        """One character per chunk must still yield the official action."""
        cmp_ = iap.compare_with_official(FULL)
        self.assertTrue(cmp_["equal"])
        parser, _ = feed_all(FULL, chunks=list(FULL))
        action = parser.to_action()
        off = official.parse_action(FULL)
        self.assertEqual(tuple(action.mouse), tuple(off.mouse))
        self.assertEqual(action.steps, off.steps)


class TestChunking(unittest.TestCase):
    def test_every_split_point(self):
        """Split the reply at every possible position; the result must not change."""
        for cut in range(1, len(FULL)):
            parser, _ = feed_all(FULL, chunks=[FULL[:cut], FULL[cut:]])
            self.assertTrue(parser.complete, "not complete at cut %d" % cut)
            action = parser.to_action()
            off = official.parse_action(FULL)
            self.assertEqual(tuple(action.mouse), tuple(off.mouse), "cut %d" % cut)
            self.assertEqual(action.steps, off.steps, "cut %d" % cut)

    def test_special_token_split(self):
        parts = [AS[:5], AS[5:], "-36 -4 0 ; LB S ; LB ; LB ; D LB ; D LB ; D LB", AE[:4], AE[4:]]
        parser, _ = feed_all(None, chunks=parts)
        self.assertTrue(parser.complete)
        self.assertEqual(parser.confirmed_groups, 6)

    def test_semicolon_split(self):
        parts = ["-36 -4 0 ;", "LB S ;", "LB ;", "LB ;", "D LB ;", "D LB ;", "D LB" + AE]
        parser, _ = feed_all(None, chunks=parts)
        self.assertTrue(parser.complete)
        self.assertEqual([g.keys for g in parser.groups][0], ["LB", "S"])

    def test_number_split(self):
        parts = ["-3", "6 -", "4 0 ;", "LB S ;", ";", ";", ";", ";", ";", AE]
        parser, _ = feed_all(None, chunks=parts)
        self.assertTrue(parser.complete)
        self.assertEqual(parser.header.as_tuple(), (-36.0, -4.0, 0.0))


class TestGroupConfirmation(unittest.TestCase):
    def test_g1_emitted_before_end(self):
        """g1 must be emitted as soon as its delimiter arrives."""
        parser = iap.IncrementalActionParser()
        events = parser.feed(AS + "-36 -4 0 ; LB S ;")
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["mouse_header", "group"])
        self.assertEqual(parser.confirmed_groups, 1)
        self.assertFalse(parser.complete)

    def test_incomplete_group_not_emitted(self):
        parser = iap.IncrementalActionParser()
        events = parser.feed(AS + "-36 -4 0 ; LB S")
        self.assertEqual([e.kind for e in events], ["mouse_header"])
        self.assertEqual(parser.confirmed_groups, 0)

    def test_mouse_header_not_emitted_before_three_numbers(self):
        parser = iap.IncrementalActionParser()
        events = parser.feed(AS + "-36 -4")
        self.assertEqual([e.kind for e in events], [])
        self.assertIsNone(parser.header)

    def test_empty_group_is_emitted(self):
        parser = iap.IncrementalActionParser()
        events = parser.feed(AS + "0 0 0 ; ;")
        self.assertEqual([e.kind for e in events], ["mouse_header", "group"])
        self.assertTrue(parser.groups[0].is_empty())


class TestInterruptions(unittest.TestCase):
    def test_interrupt_after_g1(self):
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "-36 -4 0 ; LB S ;")
        events = parser.abort("connection lost")
        self.assertEqual([e.kind for e in events], ["error"])
        self.assertEqual(parser.confirmed_groups, 1)
        self.assertFalse(parser.complete)
        self.assertIsNotNone(parser.error)

    def test_interrupt_after_g3(self):
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "-36 -4 0 ; LB S ; LB ; LB ;")
        parser.abort("connection lost")
        self.assertEqual(parser.confirmed_groups, 3)
        self.assertFalse(parser.complete)

    def test_interrupt_keeps_confirmed_and_never_guesses(self):
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "-36 -4 0 ; LB S ; LB ;")
        parser.abort()
        self.assertEqual(len(parser.groups), 2)
        self.assertEqual(parser.groups[1].keys, ["LB"])

    def test_finish_length_does_not_confirm_g6(self):
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "-36 -4 0 ; LB S ; LB ; LB ; LB ; LB ; LB")
        events = parser.finish("length")
        self.assertEqual([e.kind for e in events], ["error"])
        self.assertFalse(parser.complete)
        self.assertEqual(parser.confirmed_groups, 5)
        self.assertTrue(parser.result().truncated)

    def test_finish_stop_confirms_g6(self):
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "-36 -4 0 ; LB S ; LB ; LB ; LB ; LB ; LB")
        events = parser.finish("stop")
        self.assertIn("complete", [e.kind for e in events])
        self.assertTrue(parser.complete)
        self.assertEqual(parser.confirmed_groups, 6)

    def test_g6_after_action_end(self):
        parser, events = feed_all(FULL)
        self.assertTrue(parser.complete)
        self.assertEqual(parser.confirmed_groups, 6)


class TestMalformed(unittest.TestCase):
    def test_fewer_than_six_groups(self):
        """Five groups: the missing one is never invented."""
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "-36 -4 0 ; A ; B ; C ; D ; E")
        events = parser.finish("stop")
        self.assertFalse(parser.complete)
        # Only the groups whose delimiter was seen are confirmed.
        self.assertEqual(parser.confirmed_groups, 4)
        self.assertIn("error", [e.kind for e in events])

    def test_more_than_six_groups(self):
        """A seventh group must not be executed."""
        parser = iap.IncrementalActionParser()
        parser.feed(AS + "1 2 3 ; A ; B ; C ; D ; E ; F ; G")
        events = parser.finish("stop")
        self.assertEqual(parser.confirmed_groups, 6)
        self.assertNotIn("G", [k for g in parser.groups for k in g.keys])

    def test_think_containing_action_like_text(self):
        """Action-looking text inside <think> must never be executed."""
        text = ("<think>" + AS + "999 999 9 ; X ; Y ; Z ; W ; V ; U" + AE
                + "</think>" + AS + "1 2 3 ; A ; B ; C ; D ; E ; F" + AE)
        parser, _ = feed_all(text)
        self.assertTrue(parser.complete)
        self.assertEqual(parser.header.as_tuple(), (1.0, 2.0, 3.0))
        self.assertEqual([g.keys for g in parser.groups][0], ["A"])

    def test_unclosed_think_yields_nothing(self):
        parser = iap.IncrementalActionParser()
        parser.feed("<think>" + AS + "1 2 3 ; A ; B ; C ; D ; E ; F")
        events = parser.finish("stop")
        self.assertEqual(parser.confirmed_groups, 0)
        self.assertFalse(parser.complete)

    def test_illegal_keys_are_dropped_not_executed(self):
        parser, _ = feed_all(AS + "1 2 3 ; A NOPE ; B ; C ; D ; E ; F" + AE)
        self.assertEqual(parser.groups[0].keys, ["A"])
        self.assertIn("NOPE", parser.groups[0].dropped)

    def test_more_than_four_keys_capped(self):
        parser, _ = feed_all(AS + "1 2 3 ; A B C D E ; F ; G ; H ; I ; J" + AE)
        self.assertEqual(len(parser.groups[0].keys), 4)
        self.assertTrue(parser.groups[0].truncated)

    def test_bad_mouse_header_errors(self):
        parser = iap.IncrementalActionParser()
        events = parser.feed(AS + "a b c ; A ; B ; C ; D ; E ; F")
        self.assertEqual([e.kind for e in events], ["error"])
        self.assertEqual(parser.state, iap.ERROR)


class TestStates(unittest.TestCase):
    def test_state_sequence(self):
        parser = iap.IncrementalActionParser()
        self.assertEqual(parser.state, iap.WAIT_ACTION_START)
        parser.feed(AS)
        self.assertEqual(parser.state, iap.READ_MOUSE_HEADER)
        parser.feed("-1 2 3 ;")
        self.assertEqual(parser.state, iap.READ_GROUP_1)
        parser.feed("A ;")
        self.assertEqual(parser.state, iap.READ_GROUP_2)
        parser.feed("B ;")
        self.assertEqual(parser.state, iap.READ_GROUP_3)
        parser.feed("C ;")
        self.assertEqual(parser.state, iap.READ_GROUP_4)
        parser.feed("D ;")
        self.assertEqual(parser.state, iap.READ_GROUP_5)
        parser.feed("E ;")
        self.assertEqual(parser.state, iap.READ_GROUP_6)
        parser.feed("F" + AE)
        self.assertEqual(parser.state, iap.DONE)


class TestPrefillOpensReasoning(unittest.TestCase):
    """Real vLLM consumes the "<think>" prefill and streams the prose inside
    the reasoning block first. Without the hint the parser read that prose as
    an action body (a real reply starting "100 to 87 ..." was taken as a mouse
    header and the whole chunk was thrown away)."""

    PROSE = ("My health has fallen from 100 to 87 while the enemy is in front "
             "of the player, so I need to create space immediately.\n</think>")

    def test_prose_before_think_close_is_not_parsed(self):
        parser = iap.IncrementalActionParser(starts_in_reasoning=True)
        parser.feed(self.PROSE)
        self.assertEqual(parser.state, iap.WAIT_ACTION_START)
        self.assertIsNone(parser.error)
        self.assertEqual(parser.confirmed_groups, 0)

    def test_action_after_think_close_parses(self):
        parser = iap.IncrementalActionParser(starts_in_reasoning=True)
        parser.feed(self.PROSE)
        events = parser.feed("-36 -4 0 ; LB S ; LB ; LB ; D LB ; D LB ; D LB" + AE)
        parser.finish("stop")
        self.assertIsNone(parser.error)
        self.assertEqual(parser.confirmed_groups, 6)
        self.assertEqual(parser.header.as_tuple(), (-36.0, -4.0, 0.0))
        self.assertTrue(any(e.kind == "complete" for e in events))

    def test_stream_cut_inside_reasoning_yields_no_action(self):
        parser = iap.IncrementalActionParser(starts_in_reasoning=True)
        parser.feed("still thinking about the room")
        events = parser.finish("length")
        self.assertFalse(parser.complete)
        self.assertEqual(parser.confirmed_groups, 0)
        self.assertIn("error", [e.kind for e in events])

    def test_default_without_hint_keeps_old_behaviour(self):
        # No hint and no "<think>" in the text: whole reply is the body.
        parser = iap.IncrementalActionParser()
        parser.feed("-36 -4 0 ; LB ; LB ; LB ; LB ; LB ; LB" + AE)
        self.assertEqual(parser.confirmed_groups, 6)


class TestSSELines(unittest.TestCase):
    def test_parse_sse_lines(self):
        import json
        def sse(text):
            # split into small pieces but keep every character
            pieces = [text[i:i + 3] for i in range(0, len(text), 3)]
            for piece in pieces:
                payload = json.dumps({"choices": [{"delta": {"content": piece}}]})
                yield "data: " + payload
            yield "data: [DONE]"
        result = iap.parse_sse_lines(sse(FULL))
        self.assertTrue(result.complete)
        self.assertEqual(result.confirmed_groups, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
