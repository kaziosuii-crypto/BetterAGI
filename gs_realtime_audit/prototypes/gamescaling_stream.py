"""Stage 3: streaming policy wrapper around the official GameScaling policy.

Keeps gamescaling/policy.py untouched (no production change). This module only:
  * builds the request body with the official build_request()/messages()/prefill
  * adds stream=True (+stream_options.include_usage)
  * feeds assistant content deltas into IncrementalActionParser as they arrive
  * fires a callback the moment a group is confirmed complete

Events (semantic contract, names may differ):
  on_mouse_header(header)                     mouse X/Y/Z complete
  on_action_group(index, keys, t_ms)          group i confirmed by its ";"
  on_action_complete(action, receipt)         g6 (or finish_reason) confirmed
  on_stream_error(kind, detail)               timeout / disconnect / parse error

Every timestamp is time.perf_counter() based, relative to t0 = request start.
Measured facts (real vLLM 0.33.0, 2B checkpoint): the "<|action_end|>" stop
string is stripped by the server, so g6 can only be confirmed by
finish_reason=stop/length; skip_special_tokens=False keeps <|action_start|>
and </think> visible but they arrive split across chunks.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

try:
    from gamescaling.action import (
        ACTION_END,
        ACTION_START,
        THINK_CLOSE,
        THINK_OPEN,
        parse_action,
    )
    from gamescaling.policy import (
        GameScalingPolicy,
        PolicyConfig,
        build_request,
        encode_image,
        finalize,
        split_reasoning,
    )
except Exception as exc:  # pragma: no cover - import guard for offline tests
    raise SystemExit("gamescaling package not importable: %s" % exc)

from incremental_action_parser import IncrementalActionParser


@dataclass
class GroupArrival:
    """One confirmed action group plus when it became usable by an executor."""

    index: int
    keys: list[str]
    text: str
    t_confirmed_ms: float
    t_prev_group_ms: float | None = None

    @property
    def interval_ms(self) -> float | None:
        if self.t_prev_group_ms is None:
            return None
        return round(self.t_confirmed_ms - self.t_prev_group_ms, 2)

    def to_dict(self) -> dict:
        return {"index": self.index, "keys": list(self.keys), "text": self.text,
                "t_confirmed_ms": round(self.t_confirmed_ms, 2),
                "interval_ms": self.interval_ms}


@dataclass
class StreamReceipt:
    """Timing + outcome of one streaming decision (stage 5 feeds on this)."""

    t0_ms: float = 0.0
    t_http_send_ms: float | None = None
    t_first_byte_ms: float | None = None
    t_first_content_ms: float | None = None
    t_think_close_ms: float | None = None
    t_action_start_ms: float | None = None
    t_mouse_header_ms: float | None = None
    t_g1_ms: float | None = None
    t_g6_ms: float | None = None
    t_finish_ms: float | None = None
    t_done_ms: float | None = None
    finish_reason: str | None = None
    n_chunks: int = 0
    n_content_chunks: int = 0
    groups: list[GroupArrival] = field(default_factory=list)
    text: str = ""
    raw_text: str = ""
    usage: dict | None = None
    error: str | None = None
    error_kind: str | None = None
    cancelled: bool = False
    http_status: int | None = None
    stream_headers: dict = field(default_factory=dict)
    image_url: str = ""            # the encoded frame this decision observed
    mouse_header = None            # MouseHeader once X/Y/Z are complete

    @property
    def action_ttft_ms(self) -> float | None:
        if self.t_g1_ms is None or self.t_http_send_ms is None:
            return None
        return round(self.t_g1_ms - self.t_http_send_ms, 2)

    @property
    def e2e_ms(self) -> float | None:
        end = self.t_done_ms or self.t_finish_ms
        return None if end is None else round(end - self.t0_ms, 2)

    @property
    def generation_tail_ms(self) -> float | None:
        a, b = self.t_g1_ms, (self.t_done_ms or self.t_finish_ms)
        if a is None or b is None:
            return None
        return round(b - a, 2)

    def to_dict(self) -> dict:
        return {
            "t0_ms": round(self.t0_ms, 2),
            "t_http_send_ms": self.t_http_send_ms,
            "t_first_byte_ms": self.t_first_byte_ms,
            "t_first_content_ms": self.t_first_content_ms,
            "t_think_close_ms": self.t_think_close_ms,
            "t_action_start_ms": self.t_action_start_ms,
            "t_mouse_header_ms": self.t_mouse_header_ms,
            "t_g1_ms": self.t_g1_ms,
            "t_g6_ms": self.t_g6_ms,
            "t_finish_ms": self.t_finish_ms,
            "t_done_ms": self.t_done_ms,
            "action_ttft_ms": self.action_ttft_ms,
            "e2e_ms": self.e2e_ms,
            "generation_tail_ms": self.generation_tail_ms,
            "finish_reason": self.finish_reason,
            "n_chunks": self.n_chunks,
            "n_content_chunks": self.n_content_chunks,
            "groups": [g.to_dict() for g in self.groups],
            "usage": self.usage,
            "error": self.error,
            "error_kind": self.error_kind,
            "cancelled": self.cancelled,
            "http_status": self.http_status,
            "stream_headers": self.stream_headers,
            "mouse": (list(self.mouse_header.as_tuple())
                      if self.mouse_header is not None else None),
            "text": self.text,
        }


def _noop(*_a, **_k):
    return None


class StreamingPolicy:
    """Same history/prefix/sampling semantics as GameScalingPolicy, but streaming.

    streaming_enabled defaults to False: with it off, act() simply delegates to
    the official synchronous path, so this class is a drop-in replacement that
    can be rolled back by a single flag.
    """

    def __init__(self, system_prompt: str, config: PolicyConfig | None = None,
                 streaming_enabled: bool = False):
        self._official = GameScalingPolicy(system_prompt, config)
        self.cfg = self._official.cfg
        self.system_prompt = system_prompt
        self.streaming_enabled = streaming_enabled
        self._session = self._official._session

    @property
    def base_url(self) -> str:
        return self._official.base_url

    def reset(self) -> None:
        self._official.reset()

    def health_check(self) -> bool:
        return self._official.health_check()

    # ---- request construction (identical to official, plus stream=True) -----

    def build_body(self, image, *, stream: bool) -> tuple[dict, str]:
        url = encode_image(image, self.cfg)
        body = build_request(self._official.messages(url), self.cfg)
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body, url

    # ---- streaming path ---------------------------------------------------

    def act_stream(self, image, *, on_mouse_header: Callable | None = None,
                   on_action_group: Callable | None = None,
                   on_action_complete: Callable | None = None,
                   on_stream_error: Callable | None = None,
                   should_cancel: Callable[[], bool] | None = None,
                   record_history: bool = True) -> StreamReceipt:
        """One 200ms decision, delivered incrementally.

        Callbacks fire inline as data arrives (from the caller's thread).
        should_cancel() is polled per chunk; returning True aborts the request
        mid-stream, which also releases the socket (no orphan generation).
        """
        on_mouse_header = on_mouse_header or _noop
        on_action_group = on_action_group or _noop
        on_action_complete = on_action_complete or _noop
        on_stream_error = on_stream_error or _noop

        body, url = self.build_body(image, stream=True)
        rec = StreamReceipt()
        rec.image_url = url
        # The prefill we send can already open the reasoning block, in which
        # case vLLM consumes "<think>" and the first delta is ordinary prose.
        # Telling the parser avoids the "no reasoning at all" fallback, which
        # would read prose as an action body (found against real vLLM).
        prefill_text = str(body.get("messages", [{}])[-1].get("content", "") or "")
        starts_in_reasoning = (THINK_OPEN in prefill_text
                               and THINK_CLOSE not in prefill_text)
        parser = IncrementalActionParser(starts_in_reasoning=starts_in_reasoning)
        t0 = time.perf_counter()
        rec.t0_ms = 0.0
        text_parts: list[str] = []
        think_closed = False
        action_started = False

        def now_ms() -> float:
            return (time.perf_counter() - t0) * 1000.0

        try:
            t_send = now_ms()
            resp = self._session.post(
                f"{self.base_url}/chat/completions",
                json=body,
                timeout=self.cfg.timeout,
                stream=True,
                headers={"Accept": "text/event-stream",
                         "Authorization": f"Bearer {self.cfg.api_key}"},
            )
            rec.t_http_send_ms = round(t_send, 2)
            rec.http_status = resp.status_code
            rec.stream_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code >= 400:
                # Classified here, not by an exception: requests.HTTPError is a
                # RequestException subclass, so raising it would be reported as
                # a network failure and hide the real cause.
                body = getattr(resp, "text", "") or ""
                rec.error_kind = "http"
                rec.error = "HTTP %s: %s" % (resp.status_code, body[:400])
                on_stream_error("http", rec.error)
                return rec

            for line in resp.iter_lines(decode_unicode=False):
                t = now_ms()
                if should_cancel is not None and should_cancel():
                    rec.cancelled = True
                    rec.error_kind = "cancelled"
                    rec.error = "cancelled by caller"
                    resp.close()
                    break
                if not line:
                    continue
                rec.n_chunks += 1
                if rec.t_first_byte_ms is None:
                    rec.t_first_byte_ms = round(t, 2)
                s = line.decode("utf-8", "replace")
                if not s.startswith("data:"):
                    continue
                payload = s[5:].strip()
                if payload == "[DONE]":
                    rec.t_done_ms = round(now_ms(), 2)
                    break
                try:
                    obj = json.loads(payload)
                except ValueError as exc:
                    rec.error_kind = "bad_json"
                    rec.error = "%s: %s" % (type(exc).__name__, exc)
                    on_stream_error("bad_json", rec.error)
                    continue
                if obj.get("usage"):
                    rec.usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        rec.n_content_chunks += 1
                        if rec.t_first_content_ms is None:
                            rec.t_first_content_ms = round(t, 2)
                        text_parts.append(piece)
                        # fire semantic callbacks as the accumulate advances
                        joined = "".join(text_parts)
                        if not think_closed and "</think>" in joined:
                            think_closed = True
                            rec.t_think_close_ms = round(t, 2)
                        if not action_started and ACTION_START in joined:
                            action_started = True
                            rec.t_action_start_ms = round(t, 2)
                        for ev in parser.feed(piece):
                            _dispatch(ev, parser, rec, t, on_mouse_header,
                                      on_action_group)
                    if ch.get("finish_reason"):
                        rec.finish_reason = ch["finish_reason"]
                        rec.t_finish_ms = round(t, 2)
        except requests.HTTPError as exc:
            rec.error_kind = "http"
            rec.error = "%s: %s" % (type(exc).__name__, exc)
            on_stream_error("http", rec.error)
        except requests.Timeout as exc:
            rec.error_kind = "timeout"
            rec.error = "%s: %s" % (type(exc).__name__, exc)
            on_stream_error("timeout", rec.error)
        except requests.RequestException as exc:
            rec.error_kind = "network"
            rec.error = "%s: %s" % (type(exc).__name__, exc)
            on_stream_error("network", rec.error)
        except Exception as exc:  # noqa: BLE001 - barrier: never kill the game loop
            rec.error_kind = "error"
            rec.error = "%s: %s" % (type(exc).__name__, exc)
            on_stream_error("error", rec.error)

        rec.raw_text = "".join(text_parts)
        t_end = now_ms()
        # finish()/abort() can still emit the final group (g6 is confirmed by
        # finish_reason because vLLM consumed the <|action_end|> stop string).
        if rec.cancelled or rec.error_kind in ("network", "timeout", "error"):
            tail_events = parser.abort(rec.error_kind)
        else:
            tail_events = parser.finish(rec.finish_reason)
        for ev in tail_events:
            _dispatch(ev, parser, rec, t_end, on_mouse_header, on_action_group)

        text = finalize(rec.raw_text, rec.finish_reason, self.cfg)
        rec.text = text
        action = parser.to_action()
        if action is None:
            # incomplete / unparsable: fall back to the official parse so the
            # caller still sees whatever action text was produced.
            action = parse_action(text, lenient=self.cfg.lenient_parse)

        if rec.groups:
            rec.t_g1_ms = rec.groups[0].t_confirmed_ms
            rec.t_g6_ms = rec.groups[-1].t_confirmed_ms
        if rec.t_mouse_header_ms is None and parser.header is not None:
            rec.t_mouse_header_ms = round(t_end, 2)

        if record_history and not rec.error_kind:
            reasoning, _, closed = split_reasoning(text)
            self._remember(url, reasoning if closed else "", action.history_text)

        if not rec.error_kind and not rec.cancelled:
            on_action_complete(action, rec)
        return rec

    def remember_executed(self, url: str, reasoning: str, executed_text: str) -> None:
        """Append what ACTUALLY ran to the history (stage 5 requirement).

        The official act() stores the model's prediction; this stores the
        executor's truth so the next round sees what really happened.
        """
        self._remember(url, reasoning, executed_text)

    def _remember(self, url: str, reasoning: str, action_text: str) -> None:
        from gamescaling.policy import Turn
        h = self._official._history
        h.append(Turn(url, reasoning, action_text))
        while len(h) > self.cfg.history_len:
            h.popleft()

    # ---- synchronous fallback --------------------------------------------

    def act(self, image):
        """Official synchronous path; used as the A-baseline and the rollback."""
        return self._official.act(image)


def _dispatch(ev, parser, rec, t, on_mouse_header, on_action_group) -> None:
    """Translate parser events into the streaming callback contract + receipt.

    Parser event kinds are exactly: "mouse_header" | "group" | "complete" | "error".
    on_action_complete is fired once, after the whole reply is finalized, so it
    never races with the per-group callbacks.
    """
    if ev.kind == "mouse_header":
        if rec.t_mouse_header_ms is None:
            rec.t_mouse_header_ms = round(t, 2)
        hdr = ev.header if ev.header is not None else parser.header
        if hdr is not None:
            rec.mouse_header = hdr
        on_mouse_header(hdr)
    elif ev.kind == "group":
        g = ev.group if ev.group is not None else parser.groups[-1]
        prev = rec.groups[-1].t_confirmed_ms if rec.groups else None
        arrival = GroupArrival(index=g.index, keys=list(g.keys), text=g.text,
                               t_confirmed_ms=round(t, 2), t_prev_group_ms=prev)
        rec.groups.append(arrival)
        on_action_group(arrival.index, list(arrival.keys), arrival.t_confirmed_ms)
    elif ev.kind == "error":
        rec.error_kind = rec.error_kind or "parse"
        rec.error = rec.error or str(ev.reason)

