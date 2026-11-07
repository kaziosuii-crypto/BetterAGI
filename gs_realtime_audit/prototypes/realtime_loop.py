"""The closed loop: observe -> stream -> execute -> receipt -> next round.

One round is exactly the task-book definition of a decision:

    capture frame  ->  observation id / timestamp
    stream request ->  mouse header, then g1..g6 as they are confirmed
    executor       ->  33 ms slots, inference and execution overlap
    receipt        ->  what was predicted vs what actually ran
    history        ->  the NEXT round is told what ran, not what was predicted

Three independent switches, all default False so production behaviour is
untouched unless a caller opts in:

  streaming_enabled          use act_stream() instead of the synchronous act()
  streaming_executor_enabled schedule confirmed groups on the 33 ms grid
  execution_receipt_enabled  feed the EXECUTED action back as history

With all three off the loop IS the official client: one blocking request per
round and the model's own prediction in the history. Each switch can be turned
on alone, which is what the task book asks for ("均可独立回退").

Honest-execution note: if execution_receipt_enabled is on but
streaming_executor_enabled is off, nothing actually ran, so the receipt says
executed_groups=0 + interrupted=True (reason "executor_disabled") and the next
round is told NOOP. We do not pretend an unscheduled group was executed.

The executor is a SHADOW executor unless a real sink is injected: it records
the inputs it would send. Nothing here touches a device, a window or a socket.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Optional

from action_execution_receipt import (
    ActionExecutionReceipt,
    ExecutionHistory,
    INTERRUPTED_CANCELLED,
    INTERRUPTED_ERROR,
    INTERRUPTED_NETWORK,
    INTERRUPTED_TIMEOUT,
    executed_action_text,
    receipt_to_history_text,
)
from streaming_executor import StreamingExecutor, VirtualClock

STEP_MS = 1000.0 / 30.0 / 6.0        # 33.333 ms, mirrors streaming_executor

ERROR_TO_REASON = {
    "network": INTERRUPTED_NETWORK,
    "timeout": INTERRUPTED_TIMEOUT,
    "cancelled": INTERRUPTED_CANCELLED,
    "http": INTERRUPTED_ERROR,
    "parse": INTERRUPTED_ERROR,
    "error": INTERRUPTED_ERROR,
}


class _Mouse:
    """Uniform mouse view for both the parser header and the official tuple."""

    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)

    def as_tuple(self):
        return (self.x, self.y, self.z)

    def __repr__(self) -> str:
        return "_Mouse(%g, %g, %g)" % (self.x, self.y, self.z)


def _as_mouse(m) -> Optional[_Mouse]:
    if m is None:
        return None
    if hasattr(m, "x"):
        return _Mouse(m.x, m.y, m.z)
    return _Mouse(m[0], m[1], m[2])


class RoundRecord:
    """Everything one round produced, keyed by the same chunk id."""

    def __init__(self) -> None:
        self.round_index = 0
        self.chunk_id = ""
        self.observation_id = ""
        self.observation_timestamp = 0.0
        self.observation_wall_time = 0.0
        self.inference_start_timestamp = 0.0
        self.first_action_timestamp: Optional[float] = None
        self.confirmed_groups = 0
        self.executed_groups = 0
        self.executed_indices: list = []
        self.observation_age_ms = 0.0
        self.action_ttft_ms: Optional[float] = None
        self.e2e_ms: Optional[float] = None
        self.underflow_count = 0
        self.first_effective_input_ms: Optional[float] = None
        self.finish_reason = ""
        self.error: Optional[str] = None
        self.streamed = False
        self.scheduled = False
        self.receipted = False
        self.history_text = ""
        self.receipt: Optional[dict] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class RealtimeLoop:
    """Drives GameScaling one 200 ms decision at a time.

    policy must expose act() (official) and, when streaming is on, act_stream().
    image_source() returns the current frame and is called once per round, so a
    real capture loop can be dropped in without touching this code.
    """

    def __init__(self, policy, image_source: Callable[[], Any], *,
                 streaming_enabled: bool = False,
                 streaming_executor_enabled: bool = False,
                 execution_receipt_enabled: bool = False,
                 executor: Optional[StreamingExecutor] = None,
                 history: Optional[ExecutionHistory] = None,
                 mouse_mode: str = "A",
                 strategy: str = "B",
                 clock: Optional[Callable[[], float]] = None,
                 wall_clock: Optional[Callable[[], float]] = None) -> None:
        if streaming_enabled and not hasattr(policy, "act_stream"):
            raise TypeError("streaming_enabled=True needs a policy with act_stream()")
        self.policy = policy
        self.image_source = image_source
        self.streaming_enabled = bool(streaming_enabled)
        self.streaming_executor_enabled = bool(streaming_executor_enabled)
        self.execution_receipt_enabled = bool(execution_receipt_enabled)
        self.executor = executor or StreamingExecutor(clock=VirtualClock(),
                                                      mouse_mode=mouse_mode)
        cfg = getattr(policy, "cfg", None)
        self.history = history or ExecutionHistory(
            max_len=int(getattr(cfg, "history_len", 5)))
        self.strategy = str(strategy).upper()
        self.clock = clock or (lambda: time.perf_counter() * 1000.0)
        self.wall_clock = wall_clock or time.time
        self.round_index = 0
        self.records: list = []
        self.summary = {
            "rounds": 0,
            "streamed": 0,
            "scheduled": 0,
            "receipted": 0,
            "groups_confirmed": 0,
            "groups_executed": 0,
            "underflow": 0,
            "errors": 0,
            "interrupted": 0,
            "observation_age_p50": None,
            "observation_age_p95": None,
        }

    # ------------------------------------------------------------------ #
    def new_ids(self):
        n = self.round_index
        return ("chunk-%04d-%s" % (n, uuid.uuid4().hex[:8]),
                "obs-%04d-%s" % (n, uuid.uuid4().hex[:8]))

    def cancel_pending_actions(self, reason: str = INTERRUPTED_CANCELLED) -> None:
        """Interruption API: stop what is queued, release every held key."""
        self.executor.abort()
        self._last_cancel_reason = reason

    # ------------------------------------------------------------------ #
    def step(self) -> RoundRecord:
        image = self.image_source()
        chunk_id, obs_id = self.new_ids()
        t_obs = self.clock()
        wall = self.wall_clock()
        rec = RoundRecord()
        rec.round_index = self.round_index
        rec.chunk_id = chunk_id
        rec.observation_id = obs_id
        rec.observation_timestamp = t_obs
        rec.observation_wall_time = wall
        rec.streamed = self.streaming_enabled
        arrivals: dict = {}
        stream_receipt = None
        decision = None
        predicted_text = ""
        image_url = ""

        # ---------------------- inference ---------------------- #
        if self.streaming_enabled:
            stream_receipt = self.policy.act_stream(
                image,
                on_action_group=lambda idx, keys, t_ms: arrivals.__setitem__(
                    int(idx), (list(keys), t_ms)),
                record_history=False,
            )
            rec.inference_start_timestamp = t_obs + (
                stream_receipt.t_http_send_ms or 0.0)
            rec.finish_reason = stream_receipt.finish_reason or ""
            rec.error = (stream_receipt.error_kind or stream_receipt.error)
            rec.action_ttft_ms = stream_receipt.action_ttft_ms
            rec.e2e_ms = stream_receipt.e2e_ms
            predicted_text = stream_receipt.text or ""
            image_url = stream_receipt.image_url
            mouse = _as_mouse(stream_receipt.mouse_header)
        else:
            decision = self.policy.act(image)
            action = getattr(decision, "action", None)
            rec.inference_start_timestamp = t_obs
            rec.finish_reason = getattr(decision, "finish_reason", "") or ""
            rec.error = getattr(decision, "error", None)
            rec.action_ttft_ms = getattr(decision, "latency_ms", None)
            rec.e2e_ms = getattr(decision, "latency_ms", None)
            predicted_text = getattr(decision, "text", "") or ""
            mouse = _as_mouse(getattr(action, "mouse", None))
            steps = list(getattr(action, "steps", []) or [])
            for i, group in enumerate(steps, start=1):
                arrivals[i] = (list(group), None)
            url = getattr(self.policy, "_last_url", "")
            image_url = url or ""

        groups = {k: v for k, v in arrivals.items() if k >= 1}
        rec.confirmed_groups = len(groups)
        if 1 in groups and groups[1][1] is not None:
            first_t = t_obs + groups[1][1]
        elif rec.action_ttft_ms is not None:
            first_t = rec.inference_start_timestamp + rec.action_ttft_ms
        else:
            first_t = None
        rec.first_action_timestamp = first_t
        if first_t is not None:
            rec.observation_age_ms = round(first_t - t_obs, 3)

        # ---------------------- execution ---------------------- #
        executed_groups = 0
        executed_indices: list = []
        first_input_ms = None
        underflow = 0
        if self.streaming_executor_enabled:
            self.executor.begin_block(chunk_id)
            if mouse is not None:
                t_mouse = (getattr(stream_receipt, "t_mouse_header_ms", None)
                           if stream_receipt is not None else None)
                self.executor.submit_mouse(
                    mouse.x, mouse.y, mouse.z,
                    t_mouse_ms=(t_mouse if t_mouse is not None else 0.0))
            # Anchor on g1: the executor's slot grid starts at the first
            # confirmed group, so a 160 ms inference does not make every
            # group look "late". Absolute times live in the receipt only.
            t_anchor = (groups[1][1] if 1 in groups and groups[1][1] is not None
                        else 0.0)
            for idx in sorted(groups):
                keys, t_ms = groups[idx]
                rel = 0.0 if t_ms is None else max(0.0, t_ms - t_anchor)
                self.executor.submit_group(idx, keys, t_generated_ms=rel,
                                           t_received_ms=rel)
            sim = self.executor.run(strategy=self.strategy,
                                    finish_reason=rec.finish_reason or "stop")
            summ = sim.summarize()
            executed_indices = list(summ["executed_indices"])
            executed_groups = len(executed_indices)
            first_input_ms = summ["first_effective_input_ms"]
            underflow = summ["underrun_count"]
            rec.scheduled = True
        else:
            rec.scheduled = False

        rec.executed_groups = executed_groups
        rec.executed_indices = executed_indices
        rec.underflow_count = underflow
        rec.first_effective_input_ms = first_input_ms

        # ------------------- receipt + history ------------------- #
        if self.execution_receipt_enabled:
            disabled = not self.streaming_executor_enabled
            reason = ""
            if rec.error:
                reason = ERROR_TO_REASON.get(rec.error or "", INTERRUPTED_ERROR)
            elif disabled:
                reason = "executor_disabled"
            receipt = ActionExecutionReceipt(
                chunk_id=chunk_id, observation_id=obs_id,
                round_index=self.round_index,
                observation_timestamp=t_obs,
                inference_start_timestamp=rec.inference_start_timestamp,
                first_action_timestamp=(first_t or 0.0),
                execution_start_timestamp=(t_obs + (first_input_ms or 0.0)),
                execution_end_timestamp=(t_obs + (first_input_ms or 0.0)
                                         + STEP_MS * max(executed_groups, 0)),
                observation_wall_time=wall,
                inference_start_wall_time=wall,
                predicted_action=predicted_text,
                predicted_groups=rec.confirmed_groups,
                confirmed_groups=rec.confirmed_groups,
                executed_groups=executed_groups,
                executed_actions=[{"index": i, "keys": groups[i][0]}
                                  for i in executed_indices if i in groups],
                executed_mouse=(mouse.as_tuple() if mouse is not None else None),
                interrupted=bool(reason),
                interruption_reason=reason,
                underflow_count=underflow,
                observation_age_ms=rec.observation_age_ms,
                finish_reason=rec.finish_reason,
                notes={"executor_disabled": disabled,
                       "strategy": self.strategy,
                       "streamed": self.streaming_enabled},
            )
            rec.receipted = True
            rec.receipt = receipt.to_dict()
            self.history.add(observation_id=obs_id, image_url=image_url,
                             receipt=receipt)
            rec.history_text = receipt_to_history_text(receipt)
            self._remember(image_url, "", rec.history_text)
        else:
            rec.history_text = predicted_text
            if not self.streaming_enabled:
                # official act() already appended its own prediction
                pass

        self.round_index += 1
        self.records.append(rec)
        s = self.summary
        s["rounds"] += 1
        s["streamed"] += int(rec.streamed)
        s["scheduled"] += int(rec.scheduled)
        s["receipted"] += int(rec.receipted)
        s["groups_confirmed"] += rec.confirmed_groups
        s["groups_executed"] += rec.executed_groups
        s["underflow"] += rec.underflow_count
        s["errors"] += int(bool(rec.error))
        s["interrupted"] += int(bool(rec.receipt and
                                     rec.receipt.get("interrupted")))
        return rec

    def _remember(self, image_url: str, reasoning: str, text: str) -> None:
        """Write the executed truth into whichever history the policy keeps."""
        if hasattr(self.policy, "remember_executed"):
            self.policy.remember_executed(image_url, reasoning, text)
            return
        h = getattr(self.policy, "_history", None)
        if h is None:
            return
        try:
            from gamescaling.policy import Turn
        except Exception:
            return
        h.append(Turn(image_url, reasoning, text))
        limit = int(getattr(getattr(self.policy, "cfg", None),
                            "history_len", 5))
        while len(h) > limit:
            h.popleft()

    # ------------------------------------------------------------------ #
    def run(self, rounds: int) -> list:
        out = [self.step() for _ in range(int(rounds))]
        ages = sorted(r.observation_age_ms for r in out
                      if r.observation_age_ms is not None)
        if ages:
            self.summary["observation_age_p50"] = ages[len(ages) // 2]
            self.summary["observation_age_p95"] = ages[
                min(len(ages) - 1, int(round(0.95 * (len(ages) - 1))))]
        return out

    def summary_dict(self) -> dict:
        return dict(self.summary)


__all__ = ["RealtimeLoop", "RoundRecord", "ERROR_TO_REASON", "STEP_MS"]
