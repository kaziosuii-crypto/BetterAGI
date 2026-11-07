"""StreamingExecutor -- a pure, offline simulator for GameScaling 200 ms actions.

WHY THIS MODULE EXISTS
----------------------
The audit project measures whether a streaming (token-by-token) inference
client can drive a game in real time. A GameScaling decision is one 200 ms
chunk (see audit/official_code/action.py)::

    <|action_start|>X Y Z ; g1 ; g2 ; g3 ; g4 ; g5 ; g6<|action_end|>

  * "X Y" mouse displacement over the whole chunk (per-mille, clamped +-1000),
  * "Z" wheel clicks (clamped +-5), emitted EXACTLY ONCE per chunk,
  * g1..g6 six 33.333 ms keyboard substeps, at most 4 keys each.

Keyboard semantics (the state machine implemented here):
  * a key present in consecutive groups stays HELD (no re-press),
  * a group whose key set changes releases old keys and presses new ones,
  * an empty group releases everything,
  * state persists ACROSS action blocks (a key held at g6 stays down into the
    next block's g1),
  * nothing may ever get stuck: release_all() / abort() always clear it.

This is a SIMULATOR. It never calls a real input API, never uses wall-clock
time (time / sleep are not imported), never touches network, GPU, a game or a
process. All time comes from an injectable VirtualClock and every simulated
input is written to an injectable sink / recorder.

WHAT IT PROVIDES
----------------
  * VirtualClock, RecordingSink -- injectable time + recorder.
  * StreamingExecutor -- keyboard state machine plus submit_group(),
    submit_mouse(), release_all(), abort(), finish(), apply_group() and the
    strategy driver run().
  * Action Underrun Rate = (#groups not in the queue at the moment the
    scheduler wanted to start them, i.e. starvation / need-to-wait) / 6,
    plus gap / overlap / late counts and max wait ms.
  * Four comparable scheduling strategies (A/B/C/D), each producing a
    SimResult with summarize() -> dict (ready for jsonl).
  * Stream-interruption simulation (break after g1/g2/g3/g5): stops
    unconfirmed actions, releases every key, never guesses missing groups.
  * run_simulation() / compare_strategies() batch helpers.

Strategies
----------
A  sync baseline      : wait for the COMPLETE response (action_end / stop)
                         before emitting anything, then replay six slices.
B  immediate / ASAP   : dispatch each group the instant it arrives; no fixed
                         slices, no waiting for a slot (overlaps possible).
C  fixed 33 ms slices : group k may not start before slot (k-1)*STEP_MS nor
                         overlap the previous slice; if it has not arrived by
                         then the queue is empty -> underrun, we wait.
D  fixed slices + a late-handling sub-policy applied at each underrun:
     D_extend  "extend previous action" : keep holding the previous keys
               across the starvation gap (continuous input, stuck-key risk).
     D_release "safe release keys"      : release_all() the moment starvation
               is detected (no held key across the gap, extra key churn).

KNOWN LIMITATIONS
-----------------
  1. Pure simulation: no OS input, no real time, no network/GPU/game/process.
  2. Underrun is strategy-relative: B dispatches on arrival so its desired
     start IS the arrival and it has zero underrun by construction; read it
     together with first_effective_input_ms / total_action_duration_ms.
  3. D shares C's schedule (identical underrun); they differ in
     key_stuck_count and keyup/keydown churn, not in timing.
  4. The mouse header is gated by full_response_ms only for strategy A; for
     B/C/D it fires at its own arrival, so B/C/D share an early first-input
     time on the same trace.
  5. "late" (arrive > ideal slot) is a transport property of the trace, so it
     is identical across strategies by design.
  6. key_stuck_count counts key-holds still present across a starvation /
     idle window AFTER the late sub-policy is applied (risk exposure), not
     literal OS-level stuck keys; abort() always empties held keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Constants mirrored from audit/official_code/action.py. Kept local so this
# standalone simulator never imports or modifies production code.
CHUNK_MS = 200.0
CHUNK_STEPS = 6
STEP_MS = CHUNK_MS / CHUNK_STEPS                 # 33.333333... ms
MAX_KEYS_PER_STEP = 4
MOUSE_LIMIT = 1000.0
SCROLL_LIMIT = 5.0
EPS = 1e-6

LETTER_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGIT_KEYS = ("zero", "one", "two", "three", "four", "five", "six", "seven",
              "eight", "nine")
FUNCTION_KEYS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven",
                 "Eight", "Nine", "Ten", "Eleven", "Twelve")
MOUSE_BUTTON_KEYS = ("LB", "RB", "MB")
ARROW_KEYS = ("Up", "Down", "Left", "Right")
SPECIAL_KEYS = ("Shift", "Ctrl", "Alt", "Tab", "Caps", "Esc", "Space", "Enter",
                "Back", "Delete", "Insert", "Home", "End", "Pause")
PUNCTUATION_KEYS = ("Equal", "Minus", "Period", "Slash", "Quote")
KEY_VOCAB = frozenset(
    LETTER_KEYS + DIGIT_KEYS + FUNCTION_KEYS + MOUSE_BUTTON_KEYS + ARROW_KEYS
    + SPECIAL_KEYS + PUNCTUATION_KEYS
)


def _r(value: Any, nd: int = 4) -> Any:
    """Round floats for stable, JSON-friendly output; pass None / others through."""
    if value is None:
        return None
    try:
        return round(float(value), nd)
    except (TypeError, ValueError):
        return value


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def is_effective(event: dict) -> bool:
    """Does this event actually actuate something (first-input detection)?"""
    kind = event.get("kind")
    if kind == "keydown":
        return True
    if kind == "mouse_move":
        return abs(event.get("dx", 0.0)) > EPS or abs(event.get("dy", 0.0)) > EPS
    if kind == "wheel":
        return abs(event.get("dz", 0.0)) > EPS
    return False


# --------------------------------------------------------------------------- #
# Injectable clock + sink / recorder
# --------------------------------------------------------------------------- #
class VirtualClock:
    """Monotonic virtual clock in milliseconds. No real time is ever read.

    advance_to() clamps (never rewinds) so chronological, stable-sorted event
    batches are safe. reset() is an explicit simulator operation.
    """

    def __init__(self, t0_ms: float = 0.0) -> None:
        self._t = float(t0_ms)
        self.advances = 0

    def now(self) -> float:
        return self._t

    def advance_to(self, t_ms: float) -> float:
        t = float(t_ms)
        if t > self._t:
            self._t = t
            self.advances += 1
        return self._t

    def advance(self, dt_ms: float) -> float:
        return self.advance_to(self._t + float(dt_ms))

    def reset(self, t_ms: float = 0.0) -> float:
        self._t = float(t_ms)
        return self._t

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "VirtualClock(t=%.3fms, advances=%d)" % (self._t, self.advances)


class RecordingSink:
    """Default recorder: accumulates every emitted input event as a dict."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(dict(event))

    def clear(self) -> None:
        self.events.clear()

    def __len__(self) -> int:
        return len(self.events)


class CallableSink:
    """Adapter so a plain callable can be injected as a sink too."""

    def __init__(self, fn: Callable[[dict], None]) -> None:
        self.fn = fn
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        e = dict(event)
        self.events.append(e)
        self.fn(e)

    def clear(self) -> None:
        self.events.clear()


def _normalize_sink(sink: Any) -> Any:
    if sink is None:
        return RecordingSink()
    if callable(getattr(sink, "emit", None)):
        return sink
    if callable(sink):
        return CallableSink(sink)
    raise TypeError("sink must provide .emit(event) or be a callable")


# --------------------------------------------------------------------------- #
# Key sanitization: illegal-key drop, duplicate collapse, >4 truncation.
# Mirrors action._build's vocabulary check and 4-key cap.
# --------------------------------------------------------------------------- #
def sanitize_keys(keys: Iterable[str] | str) -> tuple[list[str], list[str], list[str]]:
    """Return (kept, dropped_illegal, dropped_overflow)."""
    tokens = keys.split() if isinstance(keys, str) else [str(k) for k in keys]
    legal: list[str] = []
    dropped_illegal: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok not in KEY_VOCAB:
            dropped_illegal.append(tok)
            continue
        if tok in seen:
            continue
        seen.add(tok)
        legal.append(tok)
    return legal[:MAX_KEYS_PER_STEP], dropped_illegal, legal[MAX_KEYS_PER_STEP:]


# --------------------------------------------------------------------------- #
# Per-group record + simulation result
# --------------------------------------------------------------------------- #
@dataclass
class GroupRecord:
    """Timing + disposition of one 33 ms keyboard group."""

    index: int
    keys: list[str] = field(default_factory=list)
    raw_keys: list[str] = field(default_factory=list)
    dropped_illegal: list[str] = field(default_factory=list)
    dropped_overflow: list[str] = field(default_factory=list)
    t_generated_ms: float | None = None
    t_received_ms: float | None = None
    t_submitted_ms: float | None = None      # == t_received (immediate enqueue)
    actual_start_ms: float | None = None
    actual_end_ms: float | None = None
    queue_wait_ms: float = 0.0
    underrun: bool = False
    gap: bool = False
    overlap: bool = False
    late: bool = False
    executed: bool = False
    skipped: bool = False
    missing: bool = False
    guessed: bool = False
    duplicate: bool = False

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "keys": list(self.keys),
            "raw_keys": list(self.raw_keys),
            "dropped_illegal": list(self.dropped_illegal),
            "dropped_overflow": list(self.dropped_overflow),
            "t_generated_ms": _r(self.t_generated_ms),
            "t_received_ms": _r(self.t_received_ms),
            "t_submitted_ms": _r(self.t_submitted_ms),
            "actual_start_ms": _r(self.actual_start_ms),
            "actual_end_ms": _r(self.actual_end_ms),
            "queue_wait_ms": _r(self.queue_wait_ms),
            "underrun": self.underrun,
            "gap": self.gap,
            "overlap": self.overlap,
            "late": self.late,
            "executed": self.executed,
            "skipped": self.skipped,
            "missing": self.missing,
            "guessed": self.guessed,
            "duplicate": self.duplicate,
        }


@dataclass
class SimResult:
    """Everything one simulated decision produced; summarize() -> dict."""

    strategy: str
    late_policy: str | None
    mouse_mode: str
    block_id: str
    complete: bool
    incomplete: bool
    truncated: bool
    finish_reason: str
    break_after: int | None
    total_groups: int
    executed_indices: list[int]
    missing_indices: list[int]
    guessed_indices: list[int]
    first_effective_input_ms: float | None
    total_action_duration_ms: float
    action_end_ms: float | None
    end_to_end_ms: float
    action_underrun_rate: float
    underrun_count: int
    gap_count: int
    overlap_count: int
    late_count: int
    max_wait_ms: float
    max_queue_wait_ms: float
    key_stuck_count: int
    action_missing_count: int
    action_duplicate_count: int
    wheel_trigger_count: int
    held_keys_at_end: list[str]
    groups: list[dict]
    events: list[dict]

    def summarize(self) -> dict:
        """Flat, fully JSON-serializable dict (ready for jsonl writers)."""
        return {
            "strategy": self.strategy,
            "late_policy": self.late_policy,
            "mouse_mode": self.mouse_mode,
            "block_id": self.block_id,
            "complete": self.complete,
            "incomplete": self.incomplete,
            "truncated": self.truncated,
            "finish_reason": self.finish_reason,
            "break_after": self.break_after,
            "total_groups": self.total_groups,
            "executed_indices": list(self.executed_indices),
            "missing_indices": list(self.missing_indices),
            "guessed_indices": list(self.guessed_indices),
            "executed_group_count": len(self.executed_indices),
            "first_effective_input_ms": _r(self.first_effective_input_ms),
            "total_action_duration_ms": _r(self.total_action_duration_ms),
            "action_end_ms": _r(self.action_end_ms),
            "end_to_end_ms": _r(self.end_to_end_ms),
            "action_underrun_rate": _r(self.action_underrun_rate, 6),
            "underrun_count": self.underrun_count,
            "gap_count": self.gap_count,
            "overlap_count": self.overlap_count,
            "late_count": self.late_count,
            "max_wait_ms": _r(self.max_wait_ms),
            "max_queue_wait_ms": _r(self.max_queue_wait_ms),
            "key_stuck_count": self.key_stuck_count,
            "action_missing_count": self.action_missing_count,
            "action_duplicate_count": self.action_duplicate_count,
            "wheel_trigger_count": self.wheel_trigger_count,
            "held_keys_at_end": list(self.held_keys_at_end),
            "event_count": len(self.events),
            "groups": self.groups,
        }

# --------------------------------------------------------------------------- #
# The executor
# --------------------------------------------------------------------------- #
class StreamingExecutor:
    """Pure-simulator driver for one or more 200 ms GameScaling decisions.

    Parameters
    ----------
    clock : VirtualClock | None
        Injectable virtual clock (a fresh one is created when omitted).
    sink : object | None
        Injectable recorder: an object with .emit(event: dict) or a callable.
        Defaults to RecordingSink.
    mouse_mode : "A" | "B"
        Default mouse mode for submit_mouse().

    The keyboard state (self.held) is deliberately NOT reset by begin_block()
    so it persists across action blocks.
    """

    def __init__(self, clock: VirtualClock | None = None, sink: Any = None,
                 *, mouse_mode: str = "A") -> None:
        self.clock = clock if clock is not None else VirtualClock()
        self.sink = _normalize_sink(sink)
        self.mouse_mode = mouse_mode.upper() if mouse_mode.upper() in ("A", "B") else "A"
        self.held: set[str] = set()               # cross-block keyboard state
        self.blocks_run = 0
        self.last_result: SimResult | None = None
        self.begin_block("block-0")

    # ----- per-block setup / flags ---------------------------------------- #
    def begin_block(self, block_id: str = "block") -> None:
        """Reset per-block bookkeeping but KEEP self.held (cross-chunk state)."""
        self.block_id = block_id
        self._records: list[GroupRecord] = []
        self._records_by_index: dict[int, GroupRecord] = {}
        self._queue: list[GroupRecord] = []
        self._seen_indices: set[int] = set()
        self._mouse_plan: dict | None = None
        self._planned: list[tuple[float, int, dict]] = []
        self.duplicate_submissions = 0
        self.duplicate_mouse_submissions = 0
        self.complete = False
        self.incomplete = False
        self.truncated = False
        self.finish_reason: str | None = None
        self.blocks_run += 1

    def reset(self) -> None:
        """Full reset including keyboard state and the recorder."""
        self.held = set()
        self.clock.reset(0.0)
        if hasattr(self.sink, "clear"):
            self.sink.clear()
        self.last_result = None
        self.blocks_run = 0
        self.begin_block("block-0")

    # ----- public submission API ------------------------------------------ #
    def submit_group(self, index: int, keys: Iterable[str] | str,
                     t_generated_ms: float, t_received_ms: float) -> GroupRecord:
        """A COMPLETE group arrived: enqueue it immediately and record timing.

        t_submitted_ms == t_received_ms because enqueue is immediate on
        receipt. Unknown keys are dropped, >4 keys are truncated, duplicate
        indices are rejected (counted, not executed twice).
        """
        kept, illegal, overflow = sanitize_keys(keys)
        raw = keys.split() if isinstance(keys, str) else [str(k) for k in keys]
        rec = GroupRecord(
            index=int(index), keys=kept, raw_keys=raw,
            dropped_illegal=illegal, dropped_overflow=overflow,
            t_generated_ms=float(t_generated_ms),
            t_received_ms=float(t_received_ms),
            t_submitted_ms=float(t_received_ms),
        )
        if rec.index in self._seen_indices:
            rec.duplicate = True
            self.duplicate_submissions += 1
            self._records.append(rec)
            return rec
        self._seen_indices.add(rec.index)
        self._records_by_index[rec.index] = rec
        self._records.append(rec)
        self._queue.append(rec)                   # immediate enqueue
        return rec

    def submit_mouse(self, x: float, y: float, z: float, *, mode: str | None = None,
                     t_mouse_ms: float | None = None) -> dict:
        """Register the "X Y Z" mouse header.

        Mode A: one move with the full displacement. Mode B: the same
        displacement split evenly into six 33 ms slices. The wheel fires
        EXACTLY ONCE in both modes; a second call in the same block is a
        counted duplicate and is ignored (never re-triggers the wheel).
        """
        if self._mouse_plan is not None:
            self.duplicate_mouse_submissions += 1
            return self._mouse_plan
        m = (mode or self.mouse_mode).upper()
        if m not in ("A", "B"):
            m = "A"
        self._mouse_plan = {
            "x": _clamp(x, -MOUSE_LIMIT, MOUSE_LIMIT),
            "y": _clamp(y, -MOUSE_LIMIT, MOUSE_LIMIT),
            "z": _clamp(z, -SCROLL_LIMIT, SCROLL_LIMIT),
            "mode": m,
            "t_mouse_ms": 0.0 if t_mouse_ms is None else float(t_mouse_ms),
        }
        return self._mouse_plan

    # ----- internal event plumbing ---------------------------------------- #
    def _add(self, t_ms: float, prio: int, kind: str, **kw: Any) -> None:
        ev: dict = {"t_ms": _r(t_ms, 6), "kind": kind, "block_id": self.block_id}
        ev.update(kw)
        self._planned.append((ev["t_ms"], prio, ev))

    def _apply_held(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "keydown":
            self.held.add(ev["key"])
        elif kind == "keyup":
            self.held.discard(ev["key"])

    def _emit_events(self, events: list[tuple[float, int, dict]]) -> list[dict]:
        """Stable-sort by (t, prio), drive the virtual clock, emit, replay held."""
        events = sorted(events, key=lambda item: (item[0], item[1]))
        emitted: list[dict] = []
        for t, _prio, ev in events:
            self.clock.advance_to(t)
            self.sink.emit(ev)
            self._apply_held(ev)
            emitted.append(ev)
        return emitted

    def _emit_planned(self) -> list[dict]:
        out = self._emit_events(self._planned)
        self._planned = []
        return out

    # ----- public keyboard-safety / live state-machine API ------------------ #
    def release_all(self, t_ms: float | None = None,
                    note: str = "release_all") -> list[dict]:
        """Immediately release every held key (anti-stuck escape hatch)."""
        t = self.clock.now() if t_ms is None else float(t_ms)
        evs: list[tuple[float, int, dict]] = []
        released = sorted(self.held)
        for key in released:
            ev = {"t_ms": _r(t, 6), "kind": "keyup", "block_id": self.block_id,
                  "key": key, "group": 0, "note": note}
            evs.append((ev["t_ms"], 1, ev))
        info = {"t_ms": _r(t, 6), "kind": "info", "block_id": self.block_id,
                "note": note, "keys": released, "group": 0}
        evs.append((info["t_ms"], 4, info))
        return self._emit_events(evs)

    def abort(self, t_ms: float | None = None,
              reason: str = "manual_abort") -> list[dict]:
        """Abort the stream: release all keys, mark incomplete, never guess."""
        t = self.clock.now() if t_ms is None else float(t_ms)
        evs: list[tuple[float, int, dict]] = []
        released = sorted(self.held)
        for key in released:
            ev = {"t_ms": _r(t, 6), "kind": "keyup", "block_id": self.block_id,
                  "key": key, "group": 0, "note": "abort_release"}
            evs.append((ev["t_ms"], 1, ev))
        done = {"t_ms": _r(t, 6), "kind": "abort", "block_id": self.block_id,
                "reason": reason, "keys": released}
        evs.append((done["t_ms"], 5, done))
        emitted = self._emit_events(evs)
        self.incomplete = True
        self.complete = False
        self.finish_reason = reason
        return emitted

    def finish(self, t_ms: float | None = None, finish_reason: str = "stop",
               action_end_seen: bool = True) -> list[dict]:
        """Close a decision on <|action_end|> / finish_reason.

        A clean "stop" + action_end is COMPLETE: keys are intentionally NOT
        released (they carry into the next block). A truncated / length /
        missing-action_end decision is incomplete and SAFE-releases all keys.
        """
        t = self.clock.now() if t_ms is None else float(t_ms)
        complete = (finish_reason == "stop" and bool(action_end_seen))
        evs: list[tuple[float, int, dict]] = []
        if not complete:
            for key in sorted(self.held):
                ev = {"t_ms": _r(t, 6), "kind": "keyup",
                      "block_id": self.block_id, "key": key, "group": 0,
                      "note": "truncated_release"}
                evs.append((ev["t_ms"], 1, ev))
        done = {"t_ms": _r(t, 6), "kind": "finish", "block_id": self.block_id,
                "finish_reason": finish_reason,
                "action_end_seen": bool(action_end_seen), "complete": complete}
        evs.append((done["t_ms"], 6, done))
        emitted = self._emit_events(evs)
        self.complete = complete
        self.incomplete = not complete
        self.truncated = not complete
        self.finish_reason = finish_reason
        return emitted

    def apply_group(self, index: int, t_ms: float | None = None) -> dict | None:
        """Live state-machine step: execute one enqueued group right now.

        Convenience for exercising the keyboard state machine directly without
        running a full strategy. Releases old keys, presses new keys, keeps
        held keys down; returns the transition description.
        """
        rec = self._records_by_index.get(int(index))
        if rec is None or rec.executed:
            return None
        t = self.clock.now() if t_ms is None else float(t_ms)
        evs: list[tuple[float, int, dict]] = []
        new = set(rec.keys)
        release = sorted(self.held - new)
        press = [k for k in rec.keys if k not in self.held]
        keep = sorted(self.held & new)
        for key in release:
            ev = {"t_ms": _r(t, 6), "kind": "keyup", "block_id": self.block_id,
                  "key": key, "group": rec.index}
            evs.append((ev["t_ms"], 1, ev))
        for key in press:
            ev = {"t_ms": _r(t, 6), "kind": "keydown", "block_id": self.block_id,
                  "key": key, "group": rec.index}
            evs.append((ev["t_ms"], 2, ev))
        if keep:
            ev = {"t_ms": _r(t, 6), "kind": "hold", "block_id": self.block_id,
                  "keys": keep, "group": rec.index}
            evs.append((ev["t_ms"], 3, ev))
        self._emit_events(evs)
        rec.executed = True
        rec.actual_start_ms = t
        rec.actual_end_ms = t + STEP_MS
        return {"release": release, "press": press, "keep": keep}

    # ----- mouse event planning ------------------------------------------- #
    def _plan_mouse(self, strategy: str, full_response_ms: float,
                    cap_ms: float | None) -> int:
        """Add mouse/wheel events to self._planned; return wheel trigger count."""
        p = self._mouse_plan
        if p is None:
            return 0
        gate = full_response_ms if strategy == "A" else 0.0
        base = max(p["t_mouse_ms"], gate)
        x, y, z, mode = p["x"], p["y"], p["z"], p["mode"]
        wheel = 0
        if mode == "A":
            if cap_ms is None or base <= cap_ms + EPS:
                self._add(base, 0, "mouse_move", dx=_r(x, 6), dy=_r(y, 6),
                          group=0, note="mouse_A_once")
        else:  # mode B: six even slices
            for k in range(1, CHUNK_STEPS + 1):
                t = base + (k - 1) * STEP_MS
                if cap_ms is not None and t > cap_ms + EPS:
                    continue
                self._add(t, 0, "mouse_move",
                          dx=_r(x / CHUNK_STEPS, 6),
                          dy=_r(y / CHUNK_STEPS, 6),
                          group=k, note="mouse_B_slice")
        if abs(z) > EPS:
            if cap_ms is None or base <= cap_ms + EPS:
                self._add(base, 0, "wheel", dz=_r(z, 6), group=0,
                          note="wheel_once")
                wheel = 1
        return wheel

    # ----- the strategy driver -------------------------------------------- #
    def run(self, *, strategy: str = "C", late_policy: str | None = None,
            full_response_ms: float | None = None, break_after: int | None = None,
            finish_reason: str = "stop", action_end_seen: bool = True) -> SimResult:
        """Execute the registered groups + mouse under one scheduling strategy.

        Returns a SimResult (also stored as self.last_result). No real time,
        no real input, no network: only VirtualClock + sink.emit() are used.
        """
        strategy = strategy.upper()
        if strategy not in ("A", "B", "C", "D"):
            raise ValueError("unknown strategy %r; expected A/B/C/D" % (strategy,))
        if strategy == "D":
            if late_policy not in ("extend", "release"):
                late_policy = "extend"
        else:
            late_policy = None

        by_index = dict(self._records_by_index)
        if full_response_ms is None:
            times = [r.t_received_ms for r in self._records
                     if r.t_received_ms is not None]
            if self._mouse_plan is not None:
                times.append(self._mouse_plan["t_mouse_ms"])
            full_response_ms = (max(times) if times else 0.0) + 5.0

        def slot(k: int) -> float:
            return (k - 1) * STEP_MS

        # ---- compute the schedule for all six expected slots ---------------- #
        schedule: dict[int, dict] = {}
        prev_end: float | None = None
        for k in range(1, CHUNK_STEPS + 1):
            rec = by_index.get(k)
            arrive = rec.t_received_ms if rec else None
            if strategy == "A":
                # nothing executes until the complete response has arrived
                probe = (full_response_ms if k == 1 else
                         (prev_end if prev_end is not None else full_response_ms))
            elif strategy == "B":
                # ASAP: the "should start" moment is the arrival itself
                probe = arrive if arrive is not None else full_response_ms
            else:
                # C / D: fixed 33 ms slices; never early, never overlapping
                base = prev_end if prev_end is not None else slot(k)
                probe = slot(k) if k == 1 else max(slot(k), base)
            if arrive is None or (break_after is not None and k > break_after):
                # never arrived / after a stream break -> MISSING, never guessed
                schedule[k] = dict(probe=probe, arrive=arrive, actual_start=None,
                                   actual_end=None, underrun=False, late=False,
                                   gap=False, overlap=False, queue_wait=0.0,
                                   underrun_wait=0.0, wait=0.0, missing=True,
                                   executed=False)
                prev_end = max(prev_end or 0.0, probe)
                continue
            actual_start = max(arrive, probe)
            actual_end = actual_start + STEP_MS
            underrun = arrive > probe + EPS            # wanted to start, queue empty
            late = arrive > slot(k) + EPS              # transport: missed ideal slot
            gap = bool(prev_end is not None and actual_start > prev_end + EPS)
            overlap = bool(prev_end is not None and actual_start < prev_end - EPS)
            queue_wait = actual_start - arrive         # sat in queue before start
            underrun_wait = max(0.0, arrive - probe)   # starvation wait
            wait = max(queue_wait, underrun_wait)
            schedule[k] = dict(probe=probe, arrive=arrive,
                               actual_start=actual_start, actual_end=actual_end,
                               underrun=underrun, late=late, gap=gap,
                               overlap=overlap, queue_wait=queue_wait,
                               underrun_wait=underrun_wait, wait=wait,
                               missing=False, executed=True)
            prev_end = actual_end

        # ---- interruption: only groups 1..break_after may execute ---------- #
        if break_after is not None:
            break_after = int(break_after)
            if not (1 <= break_after <= CHUNK_STEPS - 1):
                raise ValueError("break_after must be in 1..5 (g1/g2/g3/g5 ...)")
            executed_indices = [k for k in range(1, break_after + 1)
                                if schedule[k]["executed"]]
            tb = schedule[break_after]["actual_end"]
            t_break = tb if tb is not None else schedule[break_after]["probe"]
            is_break = True
        else:
            executed_indices = [k for k in range(1, CHUNK_STEPS + 1)
                                if schedule[k]["executed"]]
            t_break = None
            is_break = False
        missing_indices = [k for k in range(1, CHUNK_STEPS + 1)
                           if k not in executed_indices]
        guessed_indices: list[int] = []          # we NEVER guess missing actions

        # ---- build planned events in chronological order ------------------- #
        self._planned = []
        cap = t_break if is_break else None
        wheel_count = self._plan_mouse(strategy, full_response_ms, cap)
        planned_held = set(self.held)            # shadow of held for planning
        key_stuck_count = 0

        for k in executed_indices:
            s = schedule[k]
            rec = by_index.get(k)
            keys = rec.keys if rec else []
            t = s["actual_start"]

            # underrun / late handling (D sub-policies)
            if s["underrun"]:
                self._add(s["probe"], 4, "info", note="underrun", group=k,
                          wait_ms=_r(s["underrun_wait"]))
                if late_policy == "release":
                    released = sorted(planned_held)
                    for key in released:
                        self._add(s["probe"], 1, "keyup", key=key, group=k,
                                  note="safe_release")
                    self._add(s["probe"], 4, "info", note="release_all",
                              group=k, keys=released)
                    planned_held = set()        # safe: no key held across gap
                elif late_policy == "extend":
                    self._add(s["probe"], 4, "info", note="extend_previous",
                              group=k, keys=sorted(planned_held))

            # stuck-key risk: keys held across an idle/late window
            if s["gap"] and planned_held:
                key_stuck_count += len(planned_held)
                self._add(t, 4, "info", note="held_across_gap", group=k,
                          keys=sorted(planned_held), count=len(planned_held))

            # keyboard state-machine transition
            new = set(keys)
            release = sorted(planned_held - new)
            press = [x for x in keys if x not in planned_held]
            keep = sorted(planned_held & new)
            for key in release:
                self._add(t, 1, "keyup", key=key, group=k)
            for key in press:
                self._add(t, 2, "keydown", key=key, group=k)
            if keep:
                self._add(t, 3, "hold", keys=keep, group=k)
            planned_held = new

            if rec is not None:
                rec.actual_start_ms = t
                rec.actual_end_ms = s["actual_end"]
                rec.queue_wait_ms = s["queue_wait"]
                rec.underrun = s["underrun"]
                rec.late = s["late"]
                rec.gap = s["gap"]
                rec.overlap = s["overlap"]
                rec.executed = True

        # mark unexecuted (missing) records explicitly; never guessed
        for k in missing_indices:
            rec = by_index.get(k)
            if rec is not None:
                rec.skipped = True
                rec.missing = True
                rec.guessed = False
                rec.executed = False

        # drop any event scheduled after a stream break: stop unconfirmed input
        if is_break:
            self._planned = [it for it in self._planned if it[0] <= t_break + EPS]
        main_events = self._emit_planned()

        # Recount the wheel from actually emitted events (the break-time cap
        # filter above may legitimately drop a slice / the wheel for mode B).
        wheel_count = sum(1 for ev in main_events if ev.get("kind") == "wheel")

        # ---- close the decision (finish or abort) -------------------------- #
        max_main_t = max((e["t_ms"] for e in main_events), default=0.0)
        if is_break:
            close_events = self.abort(t_break, reason="stream_break")
        else:
            t_finish = max(full_response_ms, max_main_t)
            close_events = self.finish(t_finish, finish_reason=finish_reason,
                                       action_end_seen=action_end_seen)
        all_events = main_events + close_events

        # ---- metrics ------------------------------------------------------- #
        starts = [schedule[k]["actual_start"] for k in executed_indices
                  if schedule[k]["actual_start"] is not None]
        ends = [schedule[k]["actual_end"] for k in executed_indices
                if schedule[k]["actual_end"] is not None]
        first_group_start = min(starts) if starts else None
        last_action_end = max(ends) if ends else None
        total_action_duration = (
            last_action_end - first_group_start
            if first_group_start is not None and last_action_end is not None
            else 0.0
        )
        first_effective = min((e["t_ms"] for e in all_events
                               if is_effective(e)), default=None)
        underrun_count = sum(1 for k in executed_indices
                             if schedule[k]["underrun"])
        gap_count = sum(1 for k in executed_indices if schedule[k]["gap"])
        overlap_count = sum(1 for k in executed_indices
                            if schedule[k]["overlap"])
        late_count = sum(1 for k in range(1, CHUNK_STEPS + 1)
                         if schedule[k].get("late", False))
        max_wait = max((schedule[k]["wait"] for k in executed_indices), default=0.0)
        max_queue_wait = max((schedule[k]["queue_wait"] for k in executed_indices),
                             default=0.0)
        action_duplicate_count = (self.duplicate_submissions
                                  + self.duplicate_mouse_submissions)

        groups: list[dict] = []
        for k in range(1, CHUNK_STEPS + 1):
            rec = by_index.get(k)
            gd = rec.to_dict() if rec is not None else GroupRecord(index=k).to_dict()
            s = schedule[k]
            gd.update({
                "probe_ms": _r(s["probe"]),
                "underrun_wait_ms": _r(s["underrun_wait"]),
                "wait_ms": _r(s["wait"]),
                "executed": s["executed"] and k in executed_indices,
                "skipped": k in missing_indices,
                "missing": k in missing_indices,
                "guessed": False,
            })
            groups.append(gd)

        result = SimResult(
            strategy=strategy,
            late_policy=late_policy,
            mouse_mode=(self._mouse_plan["mode"] if self._mouse_plan else "none"),
            block_id=self.block_id,
            complete=self.complete,
            incomplete=self.incomplete,
            truncated=self.truncated,
            finish_reason=self.finish_reason or "",
            break_after=break_after if is_break else None,
            total_groups=CHUNK_STEPS,
            executed_indices=executed_indices,
            missing_indices=missing_indices,
            guessed_indices=guessed_indices,
            first_effective_input_ms=first_effective,
            total_action_duration_ms=total_action_duration,
            action_end_ms=last_action_end,
            end_to_end_ms=max((e["t_ms"] for e in all_events), default=0.0),
            action_underrun_rate=underrun_count / CHUNK_STEPS,
            underrun_count=underrun_count,
            gap_count=gap_count,
            overlap_count=overlap_count,
            late_count=late_count,
            max_wait_ms=max_wait,
            max_queue_wait_ms=max_queue_wait,
            key_stuck_count=key_stuck_count,
            action_missing_count=len(missing_indices),
            action_duplicate_count=action_duplicate_count,
            wheel_trigger_count=wheel_count,
            held_keys_at_end=sorted(self.held),
            groups=groups,
            events=all_events,
        )
        self.last_result = result
        return result


# --------------------------------------------------------------------------- #
# Canonical scenario + one-shot / comparison drivers
# --------------------------------------------------------------------------- #
def default_submissions() -> list[dict]:
    """Canonical streaming trace (mix of early + late groups)."""
    return [
        {"index": 1, "keys": ["W"],          "t_generated_ms": 30,  "t_received_ms": 35},
        {"index": 2, "keys": ["W"],          "t_generated_ms": 33,  "t_received_ms": 50},
        {"index": 3, "keys": ["W", "Shift"], "t_generated_ms": 45,  "t_received_ms": 60},
        {"index": 4, "keys": ["Shift"],      "t_generated_ms": 120, "t_received_ms": 130},
        {"index": 5, "keys": [],             "t_generated_ms": 165, "t_received_ms": 175},
        {"index": 6, "keys": ["A", "D"],     "t_generated_ms": 200, "t_received_ms": 210},
    ]


def default_mouse() -> dict:
    return {"x": 120.0, "y": -60.0, "z": 2.0, "t_mouse_ms": 10.0}


def run_simulation(submissions: list[dict] | None = None,
                   mouse: dict | None = None,
                   *, strategy: str = "C", late_policy: str | None = None,
                   mouse_mode: str = "A", break_after: int | None = None,
                   finish_reason: str = "stop", action_end_seen: bool = True,
                   full_response_ms: float | None = None,
                   clock: VirtualClock | None = None,
                   sink: Any = None) -> SimResult:
    """Build one executor, feed a trace, run one strategy, return SimResult."""
    submissions = list(default_submissions() if submissions is None else submissions)
    mouse = dict(default_mouse() if mouse is None else mouse)
    ex = StreamingExecutor(clock=clock, sink=sink, mouse_mode=mouse_mode)
    ex.submit_mouse(mouse.get("x", 0.0), mouse.get("y", 0.0),
                    mouse.get("z", 0.0), mode=mouse_mode,
                    t_mouse_ms=mouse.get("t_mouse_ms", 0.0))
    for item in submissions:
        ex.submit_group(item["index"], item["keys"],
                        item["t_generated_ms"], item["t_received_ms"])
    return ex.run(strategy=strategy, late_policy=late_policy,
                  full_response_ms=full_response_ms, break_after=break_after,
                  finish_reason=finish_reason, action_end_seen=action_end_seen)


def compare_strategies(submissions: list[dict] | None = None,
                       mouse: dict | None = None, *,
                       mouse_mode: str = "A",
                       break_after: int | None = None,
                       finish_reason: str = "stop",
                       action_end_seen: bool = True,
                       full_response_ms: float | None = None) -> dict:
    """Run A/B/C/D_extend/D_release on the SAME trace; return summarize() dicts.

    Keys: "A", "B", "C", "D_extend", "D_release".
    """
    variants = [
        ("A", "A", None),
        ("B", "B", None),
        ("C", "C", None),
        ("D_extend", "D", "extend"),
        ("D_release", "D", "release"),
    ]
    out: dict = {}
    for name, strategy, policy in variants:
        res = run_simulation(submissions, mouse, strategy=strategy,
                             late_policy=policy, mouse_mode=mouse_mode,
                             break_after=break_after,
                             finish_reason=finish_reason,
                             action_end_seen=action_end_seen,
                             full_response_ms=full_response_ms)
        out[name] = res.summarize()
    return out


__all__ = [
    "CHUNK_MS", "CHUNK_STEPS", "STEP_MS", "MAX_KEYS_PER_STEP",
    "KEY_VOCAB", "VirtualClock", "RecordingSink", "CallableSink",
    "sanitize_keys", "is_effective", "GroupRecord", "SimResult",
    "StreamingExecutor", "default_submissions", "default_mouse",
    "run_simulation", "compare_strategies",
]
# __APPEND_MARKER_2__


