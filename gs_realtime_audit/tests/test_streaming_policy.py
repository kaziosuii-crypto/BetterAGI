"""Offline tests for gamescaling_stream.StreamingPolicy.

The HTTP layer is replaced by a fake session, so the tests are hermetic: they
still drive the real parser, the real request builder and the real callback
contract. The one thing they cannot prove is that vLLM streams incrementally --
that is what stage3_verify.py does against the real server.

Run: python tests/test_streaming_policy.py
"""
import importlib.util, io, json, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OFFICIAL_DIR = os.path.join(ROOT, "audit", "official_code")
sys.path.insert(0, os.path.join(ROOT, "prototypes"))


def _load_official():
    """Load the audit copy of the gamescaling package as a real package."""
    pkg_dir = OFFICIAL_DIR
    spec = importlib.util.spec_from_file_location(
        "gs_official", os.path.join(pkg_dir, "__init__.py"),
        submodule_search_locations=[pkg_dir])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gs_official"] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    import gamescaling  # noqa: F401  (real package available)
    HAVE_REAL = True
except Exception:
    HAVE_REAL = False


def _sse(pieces, finish_reason="stop", usage=None):
    """Build the SSE body vLLM would send for these content pieces."""
    lines = []
    for p in pieces:
        payload = {"choices": [{"index": 0, "delta": {"content": p},
                                "finish_reason": None}]}
        lines.append("data: " + json.dumps(payload))
        lines.append("")
    payload = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    lines.append("data: " + json.dumps(payload))
    lines.append("")
    if usage:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}))
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines)


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self.headers = {"content-type": "text/event-stream; charset=utf-8"}
        self._body = body
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for line in self._body.split("\n"):
            yield line.encode("utf-8") if not decode_unicode else line

    def close(self):
        self.closed = True

    def json(self):
        return {"choices": [{"message": {"content": ""}}]}


class FakeSession:
    """Stands in for requests.Session; records the body it was given."""

    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.last_json = None
        self.last_kwargs = None
        self.response = None

    def post(self, url, json=None, **kw):
        self.last_json = json
        self.last_kwargs = kw
        self.response = FakeResponse(self.body, self.status)
        return self.response


@unittest.skipUnless(HAVE_REAL, "gamescaling package not importable")
class TestStreamingPolicy(unittest.TestCase):
    ACTION = ("<|action_start|>-36 -4 0 ; LB S ; LB ; LB ; D LB ; D LB ; D LB"
              "<|action_end|>")

    def setUp(self):
        from gamescaling.policy import PolicyConfig
        import gamescaling_stream as gs
        self.gs = gs
        self.cfg = PolicyConfig(base_url="http://x/v1", model="m", api_key="EMPTY",
                                history_len=0, reasoning=True, max_tokens=64)

    def _policy(self, body, status=200):
        p = self.gs.StreamingPolicy("SYS", self.cfg, streaming_enabled=True)
        fake = FakeSession(body, status)
        p._session = fake
        p._official._session = fake
        return p, fake

    def test_callbacks_fire_in_order(self):
        body = _sse(["\n\n</think>", "<|action_start|>", "-36 -4 0 ; ",
                     "LB S ; ", "LB ; ", "LB ; ", "D LB ; ", "D LB ; ",
                     "D LB"], usage={"prompt_tokens": 10, "completion_tokens": 5})
        p, _ = self._policy(body)
        seen = []
        groups = []
        rec = p.act_stream(
            _dummy_image(),
            on_mouse_header=lambda h: seen.append(("mouse", h.as_tuple())),
            on_action_group=lambda i, k, t: (groups.append(i), seen.append(("g", i))),
            on_action_complete=lambda a, r: seen.append(("done", len(groups))))
        self.assertIsNone(rec.error)
        self.assertEqual(len(groups), 6)
        self.assertEqual(seen[0][0], "mouse")
        self.assertEqual([s[1] for s in seen if s[0] == "g"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(seen[-1], ("done", 6))

    def test_mouse_header_is_complete_before_g1(self):
        body = _sse(["\n\n</think><|action_start|>-36 -4 0 ; LB ; LB ; LB ; LB ; LB ; LB"])
        p, _ = self._policy(body)
        order = []
        p.act_stream(_dummy_image(),
                     on_mouse_header=lambda h: order.append("mouse"),
                     on_action_group=lambda i, k, t: order.append("g%d" % i))
        self.assertEqual(order[0], "mouse")

    def test_stream_that_dies_mid_group_keeps_confirmed_only(self):
        body = _sse(["\n\n</think><|action_start|>-36 -4 0 ; LB ; LB ; "])
        p, _ = self._policy(body)
        groups = []
        rec = p.act_stream(_dummy_image(),
                           on_action_group=lambda i, k, t: groups.append(i))
        self.assertEqual(groups, [1, 2])
        self.assertEqual(rec.finish_reason, "stop")
        self.assertLess(len(rec.groups), 6)

    def test_length_finish_does_not_confirm_g6(self):
        body = _sse(["\n\n</think><|action_start|>-36 -4 0 ; A ; B ; C ; D ; E ; F"],
                    finish_reason="length")
        p, _ = self._policy(body)
        rec = p.act_stream(_dummy_image())
        self.assertTrue(rec.error)
        self.assertEqual(len(rec.groups), 5)

    def test_cancel_stops_reading(self):
        # One group per chunk, so cancelling half-way has something to stop:
        # a single-chunk body would hand over all six groups before any check.
        body = _sse(["\n\n</think><|action_start|>-36 -4 0 ; ",
                     "LB ; ", "LB ; ", "LB ; ", "LB ; ", "LB ; ", "LB"])
        p, fake = self._policy(body)
        state = {"n": 0}

        def should_cancel():
            state["n"] += 1
            return state["n"] > 4

        rec = p.act_stream(_dummy_image(), should_cancel=should_cancel)
        self.assertTrue(rec.cancelled)
        self.assertEqual(rec.error_kind, "cancelled")
        # Whatever was confirmed before the cancel stays, but the rest is not
        # invented: the socket was released mid-generation.
        self.assertLess(len(rec.groups), 6)
        self.assertTrue(fake.response.closed)

    def test_streaming_disabled_delegates_to_official(self):
        p = self.gs.StreamingPolicy("SYS", self.cfg, streaming_enabled=False)
        called = {"n": 0}
        p._official.act = lambda img: called.__setitem__("n", called["n"] + 1)
        p.act(_dummy_image())
        self.assertEqual(called["n"], 1)

    def test_request_body_only_adds_stream_flags(self):
        body = _sse(["\n\n</think>" + self.ACTION])
        p, fake = self._policy(body)
        p.act_stream(_dummy_image())
        sent = dict(fake.last_json)
        self.assertIs(sent.pop("stream"), True)
        sent.pop("stream_options")
        for key in ("model", "messages", "temperature", "max_tokens",
                    "skip_special_tokens", "stop", "continue_final_message",
                    "add_generation_prompt"):
            self.assertIn(key, sent)
        self.assertIs(sent["skip_special_tokens"], False)
        self.assertIs(sent["continue_final_message"], True)

    def test_http_error_is_reported_not_swallowed(self):
        p, _ = self._policy("{}", status=500)
        rec = p.act_stream(_dummy_image())
        self.assertEqual(rec.http_status, 500)
        self.assertEqual(rec.error_kind, "http")


def _dummy_image():
    from PIL import Image
    return Image.new("RGB", (64, 36), (10, 20, 30))


if __name__ == "__main__":
    unittest.main(verbosity=2)
