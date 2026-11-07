"""Incremental action parser for the GameScaling streaming protocol.

The official client waits for the whole reply and then calls
``gamescaling.action.parse_action``. That is fine for a synchronous loop but
useless for streaming execution: the executor needs group g1 as soon as it is
complete, not after g6 has been generated.

This module parses the same protocol incrementally, out of a byte stream that
may be cut anywhere:

    <|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>

The SSE chunk boundary is not a token boundary and not an action-group
boundary, so the parser keeps a text buffer and a state machine:

    WAIT_ACTION_START -> READ_MOUSE_HEADER -> READ_GROUP_1 .. READ_GROUP_6
                       -> DONE
    (plus ERROR for malformed input)

Guarantees
----------
* A group is only emitted after the delimiter that terminates it was seen.
  g1..g5 are terminated by ';'. g6 is terminated by <|action_end|> or by a
  clean finish (finish_reason == "stop"); a finish_reason of "length" never
  confirms a group.
* The mouse header is only emitted after all three numbers were seen.
* Empty groups are legal and mean "release everything".
* Key validation, the 4-key cap, the +-1000 mouse clamp and the +-5 scroll
  clamp are identical to the official parser, so a complete reply parsed
  incrementally yields exactly the same Action as ``parse_action``.
* Thinking text is never mistaken for an action: the parser only looks at the
  text after the last </think>.
* On a broken connection the confirmed groups are kept and the missing ones
  are never guessed.

The official ``parse_action`` is not modified or replaced; this module
imports it for the parity check.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Tuple

# The official module is the single source of truth for these constants.
try:  # normal case: the package is importable
    from gamescaling.action import (  # type: ignore
        ACTION_END,
        ACTION_START,
        CHUNK_STEPS,
        KEY_VOCAB,
        MAX_KEYS_PER_STEP,
        MOUSE_LIMIT,
        SCROLL_LIMIT,
        THINK_CLOSE,
        THINK_OPEN,
        Action,
        parse_action,
        split_reasoning,
    )
    OFFICIAL_AVAILABLE = True
except Exception:  # pragma: no cover - fallback for a standalone checkout
    ACTION_START = "<|action_start|>"
    ACTION_END = "<|action_end|>"
    THINK_OPEN = "<think>"
    THINK_CLOSE = "</think>"
    CHUNK_STEPS = 6
    MAX_KEYS_PER_STEP = 4
    MOUSE_LIMIT = 1000.0
    SCROLL_LIMIT = 5.0
    LETTER_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    DIGIT_KEYS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    FUNCTION_KEYS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
                     "Nine", "Ten", "Eleven", "Twelve")
    MOUSE_BUTTON_KEYS = ("LB", "RB", "MB")
    ARROW_KEYS = ("Up", "Down", "Left", "Right")
    SPECIAL_KEYS = ("Shift", "Ctrl", "Alt", "Tab", "Caps", "Esc", "Space", "Enter", "Back",
                    "Delete", "Insert", "Home", "End", "Pause")
    PUNCTUATION_KEYS = ("Equal", "Minus", "Period", "Slash", "Quote")
    KEY_VOCAB = frozenset(
        LETTER_KEYS + DIGIT_KEYS + FUNCTION_KEYS + MOUSE_BUTTON_KEYS + ARROW_KEYS
        + SPECIAL_KEYS + PUNCTUATION_KEYS
    )
    Action = None  # type: ignore
    parse_action = None  # type: ignore
    split_reasoning = None  # type: ignore
    OFFICIAL_AVAILABLE = False

# Parser states.
WAIT_ACTION_START = "WAIT_ACTION_START"
READ_MOUSE_HEADER = "READ_MOUSE_HEADER"
READ_GROUP_1 = "READ_GROUP_1"
READ_GROUP_2 = "READ_GROUP_2"
READ_GROUP_3 = "READ_GROUP_3"
READ_GROUP_4 = "READ_GROUP_4"
READ_GROUP_5 = "READ_GROUP_5"
READ_GROUP_6 = "READ_GROUP_6"
DONE = "DONE"
ERROR = "ERROR"

_INT_TOKEN_RE = re.compile(r"^(-?\d+)$")


class ParserError(Exception):
    """Raised (and recorded) when the stream cannot be a valid action."""


@dataclass
class MouseHeader:
    """The X Y Z triple: whole-chunk mouse displacement plus scroll."""

    x: float
    y: float
    z: float

    def as_tuple(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass
class ActionGroup:
    """One 33 ms substep: the keys held during that slice."""

    index: int          # 1..6
    keys: List[str]
    dropped: List[str] = field(default_factory=list)
    truncated: bool = False   # more than MAX_KEYS_PER_STEP keys were given

    def is_empty(self) -> bool:
        return not self.keys

    def text(self) -> str:
        return " ".join(self.keys)


@dataclass
class ParseEvent:
    """One thing that happened while parsing.

    kind is one of:
      "mouse_header"   a MouseHeader was confirmed
      "group"          an ActionGroup was confirmed
      "complete"       the whole chunk was confirmed
      "error"          the stream is unusable (reason carries the detail)
    """

    kind: str
    index: int = 0
    header: Optional[MouseHeader] = None
    group: Optional[ActionGroup] = None
    reason: str = ""


@dataclass
class ParseResult:
    """Everything the parser learned from a stream."""

    header: Optional[MouseHeader] = None
    groups: List[ActionGroup] = field(default_factory=list)
    complete: bool = False
    error: Optional[str] = None
    finish_reason: Optional[str] = None
    text: str = ""            # the accumulated raw text
    dropped: List[str] = field(default_factory=list)
    truncated: bool = False   # the model ran out of tokens mid-chunk

    @property
    def confirmed_groups(self) -> int:
        return len(self.groups)


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _split_keys(raw: str) -> Tuple[List[str], List[str], bool]:
    """Validate a group's key tokens exactly like the official parser.

    The official ``_build`` puts only *invalid* tokens into ``Action.dropped``
    and silently keeps the first MAX_KEYS_PER_STEP valid ones. This function
    does the same so the two parsers stay byte-identical in their output.

    Returns (kept, dropped, truncated).
    """
    kept: List[str] = []
    dropped: List[str] = []
    for tok in raw.split():
        if tok in KEY_VOCAB:
            kept.append(tok)
        else:
            dropped.append(tok)
    truncated = len(kept) > MAX_KEYS_PER_STEP
    return kept[:MAX_KEYS_PER_STEP], dropped, truncated


class IncrementalActionParser:
    """Stateful parser fed with arbitrary chunks of the reply text.

    Typical use::

        parser = IncrementalActionParser()
        for chunk in sse_text_chunks:
            for event in parser.feed(chunk):
                handle(event)
        parser.finish(finish_reason)   # confirms or rejects the last group

    Every method is pure text in / events out; there is no I/O here.
    """

    def __init__(self, *, starts_in_reasoning: Optional[bool] = None) -> None:
        self._raw = ""              # every chunk seen, for the parity check
        self._state = WAIT_ACTION_START
        self._header: Optional[MouseHeader] = None
        self._groups: List[ActionGroup] = []
        self._dropped: List[str] = []
        self._error: Optional[str] = None
        self._complete = False
        self._finish_reason: Optional[str] = None
        self._truncated = False
        self._think_closed = not starts_in_reasoning  # whether a </think> was seen
        self._starts_in_reasoning = starts_in_reasoning
        self._tail = ""            # text after the last </think>
        self._parsed_upto = 0      # how much of _tail was consumed
        self._reasoning_end = -1   # offset of the tail inside _raw (-1 = unknown)

    # ---- introspection ---------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def header(self) -> Optional[MouseHeader]:
        return self._header

    @property
    def groups(self) -> List[ActionGroup]:
        return list(self._groups)

    @property
    def confirmed_groups(self) -> int:
        return len(self._groups)

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def raw_text(self) -> str:
        return self._raw

    # ---- main entry point ------------------------------------------------
    def feed(self, text: str) -> List[ParseEvent]:
        """Consume a chunk of reply text and return the events it produced."""
        events: List[ParseEvent] = []
        if not text:
            return events
        self._raw += text

        if self._state in (DONE, ERROR):
            return events

        # --- reasoning guard ------------------------------------------------
        # Only the text after the LAST </think> may ever be parsed; the
        # official parser does the same (split_reasoning -> rpartition).
        #
        # The boundary is tracked as an offset into _raw and only ever moves
        # forward, so the parse cursor never has to rewind.
        if self._reasoning_end < 0:
            # Still looking for the end of the reasoning.
            close = self._raw.rfind(THINK_CLOSE)
            if close >= 0:
                self._reasoning_end = close + len(THINK_CLOSE)
                self._tail = self._raw[self._reasoning_end:]
                self._think_closed = True
            elif self._starts_in_reasoning or THINK_OPEN in self._raw:
                # Reasoning still open: nothing may be parsed yet.
                #
                # starts_in_reasoning is set by the caller when the server
                # already consumed an unterminated "<think>" prefill -- vLLM
                # does exactly that -- so the stream begins *inside* the
                # reasoning block and the first chunk is ordinary prose.
                # Without that hint the "no reasoning at all" fallback below
                # reads the prose as an action body: real vLLM exposed this,
                # a sentence beginning "100 to 87 ..." was taken as a mouse
                # header and the whole reply was rejected.
                return events
            else:
                # No reasoning at all: the whole reply is the tail.
                self._reasoning_end = 0
                self._tail = self._raw
                self._think_closed = True
        else:
            self._tail = self._raw[self._reasoning_end:]

        events.extend(self._drain())

        if self._complete and self._state != DONE:
            self._state = DONE
        return events
    def _drain(self) -> List[ParseEvent]:
        events: List[ParseEvent] = []
        buf = self._tail[self._parsed_upto:]
        consumed = 0
        while True:
            if self._state == WAIT_ACTION_START:
                idx = buf.find(ACTION_START)
                if idx < 0:
                    # The tag may still be split across chunks: keep the tail.
                    # A bare body ("X Y Z ; ...") is also legal for the
                    # official parser, so check whether the buffer already
                    # starts with the three numbers.
                    keep = len(ACTION_START)
                    stripped = buf.lstrip()
                    if stripped and (stripped[0].isdigit() or stripped[0] == "-"):
                        self._state = READ_MOUSE_HEADER
                        continue
                    if len(buf) > keep:
                        consumed += len(buf) - keep
                        buf = buf[-keep:]
                    break
                consumed += idx + len(ACTION_START)
                buf = buf[idx + len(ACTION_START):]
                self._state = READ_MOUSE_HEADER
                continue

            if self._state == READ_MOUSE_HEADER:
                # The header is "X Y Z" and ends at the first ';'.
                semi = buf.find(";")
                if semi < 0:
                    break
                head = buf[:semi].strip()
                consumed += semi + 1
                buf = buf[semi + 1:]
                nums: List[float] = []
                for tok in head.split()[:3]:
                    if not _INT_TOKEN_RE.match(tok):
                        self._fail("bad mouse header: %r" % head, events)
                        return events
                    nums.append(float(tok))
                if len(nums) < 3:
                    self._fail("mouse header needs 3 numbers: %r" % head, events)
                    return events
                header = MouseHeader(
                    x=_clamp(nums[0], MOUSE_LIMIT),
                    y=_clamp(nums[1], MOUSE_LIMIT),
                    z=_clamp(nums[2], SCROLL_LIMIT),
                )
                self._header = header
                events.append(ParseEvent(kind="mouse_header", index=0, header=header))
                self._state = READ_GROUP_1
                continue

            # READ_GROUP_1 .. READ_GROUP_5: a group ends at ';'.
            # The last group of the body may also end at <|action_end|>
            # without a trailing delimiter, because the official parser
            # splits the body on ';'.
            if self._state in (READ_GROUP_1, READ_GROUP_2, READ_GROUP_3,
                               READ_GROUP_4, READ_GROUP_5):
                semi = buf.find(";")
                end_idx = buf.find(ACTION_END)
                if semi < 0 and end_idx < 0:
                    break
                if semi < 0 or (0 <= end_idx < semi):
                    # The body ends before the next delimiter: the closing
                    # tag is there, so the pending text is the last group.
                    raw = buf[:end_idx] if end_idx >= 0 else buf
                    consumed += len(raw)
                    buf = buf[len(raw):]
                    group = self._make_group(raw)
                    self._groups.append(group)
                    events.append(ParseEvent(kind="group", index=group.index, group=group))
                    self._state = "READ_GROUP_%d" % (group.index + 1)
                    if end_idx >= 0:
                        consumed += len(ACTION_END)
                        buf = buf[len(ACTION_END):]
                        self._complete = True
                        events.append(ParseEvent(kind="complete"))
                        break
                    continue
                raw = buf[:semi]
                consumed += semi + 1
                buf = buf[semi + 1:]
                group = self._make_group(raw)
                self._groups.append(group)
                events.append(ParseEvent(kind="group", index=group.index, group=group))
                self._state = "READ_GROUP_%d" % (group.index + 1)
                continue

            # READ_GROUP_6: the group text ends at the last ';' before
            # <|action_end|>, exactly like the official body.split(";").
            if self._state == READ_GROUP_6:
                idx = buf.find(ACTION_END)
                if idx < 0:
                    break
                raw = buf[:idx]
                # The official parser splits the body on ';', so the
                # delimiter before <|action_end|> is not part of g6.
                semi = raw.rfind(";")
                if semi >= 0:
                    raw = raw[:semi]
                consumed += idx + len(ACTION_END)
                buf = buf[idx + len(ACTION_END):]
                group = self._make_group(raw)
                self._groups.append(group)
                events.append(ParseEvent(kind="group", index=group.index, group=group))
                self._complete = True
                events.append(ParseEvent(kind="complete"))
                break

            break
        self._parsed_upto += consumed
        return events

    def _make_group(self, raw: str) -> ActionGroup:
        kept, dropped, truncated = _split_keys(raw.strip())
        self._dropped.extend(dropped)
        index = len(self._groups) + 1
        return ActionGroup(index=index, keys=kept, dropped=dropped, truncated=truncated)

    def _fail(self, reason: str, events: List[ParseEvent]) -> None:
        self._error = reason
        self._state = ERROR
        events.append(ParseEvent(kind="error", reason=reason))

    # ---- stream end ------------------------------------------------------
    def finish(self, finish_reason: Optional[str] = None) -> List[ParseEvent]:
        """Signal the end of the stream.

        ``finish_reason == "stop"`` (or None) confirms the last group when it
        is syntactically complete. ``"length"`` never confirms: the reply was
        cut by the token budget and the group may be half written.
        """
        events: List[ParseEvent] = []
        self._finish_reason = finish_reason
        if self._state in (DONE, ERROR):
            return events

        if self._state == READ_GROUP_6:
            if finish_reason == "length":
                self._truncated = True
                self._error = None
                self._state = ERROR
                events.append(ParseEvent(kind="error", reason="truncated by max_tokens"))
                return events
            # A clean finish confirms g6: the server's stop string matched,
            # so <|action_end|> was consumed by vLLM and the body ends here.
            raw = self._tail[self._parsed_upto:]
            semi = raw.rfind(";")
            if semi >= 0:
                raw = raw[:semi]
            self._parsed_upto = len(self._tail)
            group = self._make_group(raw)
            self._groups.append(group)
            events.append(ParseEvent(kind="group", index=group.index, group=group))
            self._complete = True
            events.append(ParseEvent(kind="complete"))
            self._state = DONE
            return events

        if self._state in (READ_GROUP_1, READ_GROUP_2, READ_GROUP_3,
                           READ_GROUP_4, READ_GROUP_5):
            # The stream ended in the middle of a group and no
            # <|action_end|> was seen. The official parser needs the closing
            # tag, so this reply is not a valid action.
            self._truncated = True
            self._error = "stream ended before the chunk was complete"
            self._state = ERROR
            events.append(ParseEvent(kind="error", reason=self._error))
            return events

        if self._starts_in_reasoning and self._reasoning_end < 0:
            # The reply never left the reasoning block: it was cut while the
            # model was still thinking, so there is no action at all.
            self._truncated = True
            self._fail("stream ended inside reasoning", events)
            return events

        if self._state == WAIT_ACTION_START and not self._tail[self._parsed_upto:].strip():
            # Nothing was ever produced.
            self._fail("no action in stream", events)
            return events

        # Any other state means the reply ended in the middle of a group.
        self._truncated = True
        self._error = "stream ended before the chunk was complete"
        self._state = ERROR
        events.append(ParseEvent(kind="error", reason=self._error))
        return events

    def abort(self, reason: str = "connection lost") -> List[ParseEvent]:
        """The connection died. Keep what was confirmed, never guess the rest."""
        events: List[ParseEvent] = []
        if self._state in (DONE, ERROR):
            return events
        self._error = reason
        self._state = ERROR
        events.append(ParseEvent(kind="error", reason=reason))
        return events

    # ---- results ---------------------------------------------------------
    def result(self) -> ParseResult:
        return ParseResult(
            header=self._header,
            groups=list(self._groups),
            complete=self._complete,
            error=self._error,
            finish_reason=self._finish_reason,
            text=self._raw,
            dropped=list(self._dropped),
            truncated=self._truncated,
        )

    def to_action(self) -> Any:
        """Build the official Action object from the confirmed groups.

        Returns None when the chunk was not complete.
        """
        if not self._complete or self._header is None:
            return None
        if Action is None:
            raise RuntimeError("gamescaling.action is not importable")
        steps: List[List[str]] = []
        for group in self._groups[:CHUNK_STEPS]:
            steps.append(list(group.keys))
        while len(steps) < CHUNK_STEPS:
            steps.append([])
        body_parts = ["%d %d %d" % (int(round(self._header.x)),
                                    int(round(self._header.y)),
                                    int(round(self._header.z)))]
        for group in self._groups[:CHUNK_STEPS]:
            body_parts.append(" ".join(group.keys))
        body = " ; ".join(body_parts)
        return Action(
            mouse=self._header.as_tuple(),
            steps=steps,
            parsed=True,
            body=body,
            dropped=list(self._dropped),
        )


def compare_with_official(text: str, *, finish_reason: Optional[str] = "stop",
                           lenient: bool = False) -> dict:
    """Parse a complete reply both ways and report whether they agree.

    ``text`` is the raw reply as the server would return it (including the
    <think> block if the model produced one).
    """
    official = parse_action(text, lenient=lenient)
    parser = IncrementalActionParser()
    parser.feed(text)
    parser.finish(finish_reason)
    incremental = parser.to_action()
    result = parser.result()

    fields = ("mouse", "steps", "parsed", "dropped")
    matches: dict = {}
    for name in fields:
        a = getattr(official, name, None)
        b = getattr(incremental, name, None) if incremental is not None else None
        if name == "mouse":
            a = tuple(a) if a is not None else None
            b = tuple(b) if b is not None else None
        if incremental is None and not official.parsed:
            # No action was produced at all. The official parser returns an
            # all-default Action in that case, so the two agree.
            matches[name] = True
        else:
            matches[name] = (a == b)
    return {
        "official": official,
        "incremental": incremental,
        "matches": matches,
        "equal": all(matches.values()),
        "parser_state": parser.state,
        "confirmed_groups": result.confirmed_groups,
        "error": result.error,
    }


def parse_sse_lines(lines: Iterable[str], *, finish_reason: Optional[str] = "stop",
                    on_event: Optional[Callable[[ParseEvent], None]] = None) -> ParseResult:
    """Convenience: feed raw SSE ``data:`` payload lines into a parser."""
    parser = IncrementalActionParser()
    for line in lines:
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            chunk = delta.get("content")
            if not chunk:
                continue
            for event in parser.feed(chunk):
                if on_event is not None:
                    on_event(event)
    for event in parser.finish(finish_reason):
        if on_event is not None:
            on_event(event)
    return parser.result()


__all__ = [
    "IncrementalActionParser",
    "ParseEvent",
    "ParseResult",
    "ParserError",
    "MouseHeader",
    "ActionGroup",
    "compare_with_official",
    "parse_sse_lines",
    "WAIT_ACTION_START",
    "READ_MOUSE_HEADER",
    "READ_GROUP_1",
    "READ_GROUP_2",
    "READ_GROUP_3",
    "READ_GROUP_4",
    "READ_GROUP_5",
    "READ_GROUP_6",
    "DONE",
    "ERROR",
]
