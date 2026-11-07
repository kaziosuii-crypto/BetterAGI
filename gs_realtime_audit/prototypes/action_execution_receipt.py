"""Action execution receipt: what the model predicted vs what actually ran.

The GameScaling history manager used to store the model's own last reply,
which is the *prediction*. With streaming execution the two can differ: a
chunk may be interrupted after group 3, a group may be skipped because it
arrived too late, a network error may cut the stream. Feeding the prediction
back into the context teaches the model that actions it never performed
happened, so the real executed action has to be recorded separately.

This module defines the receipt and the history adapter that turns an
executed receipt into the assistant message the next prompt expects.

Design rules
------------
* The predicted action and the executed action are stored separately.
* Only groups that really ran enter the executed history. A predicted group
  that was never executed must never appear as if it had run.
* A partial execution is expressed in the smallest compatible way: the
  official format has exactly six groups, so a partial chunk is written with
  the groups that ran and the remaining slots empty. The interruption is
  recorded in the receipt and (optionally) in the reasoning channel, so the
  information is not lost.
* Every chunk has a unique id, so observation, inference and execution can
  be correlated.
* Timestamps: monotonic for durations, wall clock for cross-service joins.

This module has no I/O and no dependency on the game or the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

try:  # the official constants are the source of truth
    from gamescaling.action import (  # type: ignore
        ACTION_END,
        ACTION_START,
        CHUNK_STEPS,
        NOOP_ACTION_TEXT,
    )
    OFFICIAL_AVAILABLE = True
except Exception:  # pragma: no cover - standalone checkout
    ACTION_START = "<|action_start|>"
    ACTION_END = "<|action_end|>"
    CHUNK_STEPS = 6
    NOOP_ACTION_TEXT = f"{ACTION_START}0 0 0 ; ; ; ; ; ; {ACTION_END}"
    OFFICIAL_AVAILABLE = False


# Interruption reasons.
INTERRUPTED_SUPERSEDED = "superseded"     # a newer chunk replaced this one
INTERRUPTED_CANCELLED = "cancelled"       # the client cancelled the chunk
INTERRUPTED_NETWORK = "network"           # the stream died
INTERRUPTED_TIMEOUT = "timeout"           # a deadline passed
INTERRUPTED_ERROR = "error"               # an unexpected failure
INTERRUPTED_GAME_EXIT = "game_exit"       # the game went away


@dataclass
class ActionExecutionReceipt:
    """The record of one action chunk: predicted, executed and why."""

    # --- identity ---
    chunk_id: str = ""
    observation_id: str = ""
    round_index: int = 0

    # --- timing (monotonic milliseconds) ---
    observation_timestamp: float = 0.0
    inference_start_timestamp: float = 0.0
    first_action_timestamp: float = 0.0
    execution_start_timestamp: float = 0.0
    execution_end_timestamp: float = 0.0

    # --- timing (wall clock, for cross-service correlation) ---
    observation_wall_time: float = 0.0
    inference_start_wall_time: float = 0.0

    # --- what the model produced ---
    predicted_action: str = ""
    predicted_groups: int = 0
    confirmed_groups: int = 0

    # --- what actually ran ---
    executed_groups: int = 0
    executed_actions: List[Dict[str, Any]] = field(default_factory=list)
    executed_mouse: Optional[List[float]] = None

    # --- outcome ---
    interrupted: bool = False
    interruption_reason: str = ""
    underflow_count: int = 0
    execution_errors: List[str] = field(default_factory=list)

    # --- extra ---
    observation_age_ms: float = 0.0
    finish_reason: str = ""
    notes: Dict[str, Any] = field(default_factory=dict)

    # ---- derived -------------------------------------------------------
    @property
    def observation_age_at_first_action_ms(self) -> float:
        """How old the observation was when the first action was executed.

        This is the honest measure of how stale the decision was. Streaming
        shortens the wait but does NOT make the picture fresher.
        """
        if not self.first_action_timestamp or not self.observation_timestamp:
            return 0.0
        return self.first_action_timestamp - self.observation_timestamp

    @property
    def inference_latency_ms(self) -> float:
        if not self.inference_start_timestamp or not self.first_action_timestamp:
            return 0.0
        return self.first_action_timestamp - self.inference_start_timestamp

    @property
    def execution_span_ms(self) -> float:
        if not self.execution_end_timestamp or not self.execution_start_timestamp:
            return 0.0
        return self.execution_end_timestamp - self.execution_start_timestamp

    @property
    def is_complete(self) -> bool:
        return (not self.interrupted
                and self.executed_groups == self.predicted_groups
                and not self.execution_errors)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["observation_age_at_first_action_ms"] = self.observation_age_at_first_action_ms
        out["inference_latency_ms"] = self.inference_latency_ms
        out["execution_span_ms"] = self.execution_span_ms
        out["is_complete"] = self.is_complete
        return out


def executed_action_text(mouse: Optional[List[float]],
                          groups: List[List[str]],
                          *,
                          partial: bool = False,
                          interruption_reason: str = "") -> str:
    """Build the assistant message that represents what really happened.

    The official history format is a full six-group chunk. When only part of
    the chunk ran, the remaining slots are left empty (the official parser
    reads an empty group as "release everything"), and the partial state is
    reported through the reasoning channel when ``partial`` is set.

    ``partial`` is only a documentation hint; the text itself stays in the
    official format so the model sees what it expects.
    """
    if mouse is None:
        mx = my = mz = 0.0
    else:
        mx, my, mz = (list(mouse) + [0.0, 0.0, 0.0])[:3]
    steps: List[List[str]] = [list(g) for g in groups[:CHUNK_STEPS]]
    while len(steps) < CHUNK_STEPS:
        steps.append([])
    body = "%d %d %d ; %s" % (int(round(mx)), int(round(my)), int(round(mz)),
                             " ; ".join(" ".join(g) for g in steps))
    text = f"{ACTION_START}{body}{ACTION_END}"
    if partial and interruption_reason:
        # The reasoning channel carries the interruption. The official chat
        # template renders <think>...</think>, and the history keeps the
        # reasoning only when the policy is configured to keep it.
        note = "partially executed (%s)" % interruption_reason
        text = f"<think>\n{note}\n</think>\n\n{text}"
    return text


def receipt_to_history_text(receipt: ActionExecutionReceipt) -> str:
    """The assistant message for the next round: the executed truth.

    * A chunk that did not run at all becomes the explicit no-op, exactly
      like the official Action.history_text does for an unparseable turn.
    * A chunk that ran partially keeps the groups that ran and leaves the
      rest empty.
    """
    if receipt.executed_groups <= 0:
        return NOOP_ACTION_TEXT
    return executed_action_text(
        receipt.executed_mouse,
        [list(g.get("keys", [])) for g in receipt.executed_actions],
        partial=receipt.interrupted,
        interruption_reason=receipt.interruption_reason,
    )


@dataclass
class ExecutionHistoryEntry:
    """One round of history: what was observed, what ran."""

    observation_id: str = ""
    chunk_id: str = ""
    image_url: str = ""
    assistant_text: str = ""
    receipt: Optional[ActionExecutionReceipt] = None


class ExecutionHistory:
    """A bounded history that stores executed reality, not predictions.

    The official policy keeps a deque of (image_url, reasoning, action_text).
    This class keeps the same shape but the action_text comes from the
    receipt, so a partially executed chunk is recorded as such.
    """

    def __init__(self, max_len: int = 5):
        self.max_len = max(0, int(max_len))
        self._entries: List[ExecutionHistoryEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(list(self._entries))

    @property
    def entries(self) -> List[ExecutionHistoryEntry]:
        return list(self._entries)

    def add(self, observation_id: str, image_url: str,
            receipt: ActionExecutionReceipt) -> ExecutionHistoryEntry:
        """Record one round using the receipt's executed truth."""
        entry = ExecutionHistoryEntry(
            observation_id=observation_id,
            chunk_id=receipt.chunk_id,
            image_url=image_url,
            assistant_text=receipt_to_history_text(receipt),
            receipt=receipt,
        )
        self._entries.append(entry)
        while len(self._entries) > self.max_len:
            self._entries.pop(0)
        return entry

    def clear(self) -> None:
        self._entries.clear()


__all__ = [
    "ActionExecutionReceipt",
    "ExecutionHistory",
    "ExecutionHistoryEntry",
    "executed_action_text",
    "receipt_to_history_text",
    "INTERRUPTED_SUPERSEDED",
    "INTERRUPTED_CANCELLED",
    "INTERRUPTED_NETWORK",
    "INTERRUPTED_TIMEOUT",
    "INTERRUPTED_ERROR",
    "INTERRUPTED_GAME_EXIT",
]
