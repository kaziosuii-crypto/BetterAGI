"""Streaming GameScaling inference-audit client (independent prototype branch).

This module is an OFFLINE audit prototype. It deliberately does NOT replace the
official synchronous client (audit/official_code/policy.py :: GameScalingPolicy);
it lives in prototypes/ and keeps its own request builder, incremental parser,
timestamped trace writer and verification helper.

Design goals
------------
* Byte-level parity with the official /chat/completions request body. The exact
  official field set is produced by build_official_body(); the streaming layer
  adds exactly one transport field -- "stream": true -- which the OpenAI-
  compatible server requires before it will emit SSE at all. No
  chat_template_kwargs, no model/prompt/system-prompt changes, no extra sampling
  fields.
* Per-chunk monotonic (time.perf_counter) timestamps -> traceable JSONL.
* Cumulative-text incremental parsing with on_mouse_header / on_group / on_done
  callbacks. The parser here is intentionally lightweight and self-contained;
  it is NOT the full incremental parser produced by the separate parser
  subagent, and this module does not import it.
* verify_against_official() loads the official action.py via importlib (no
  package dependency) and compares the finished decision with parse_action().

Constraints honoured: no GPU/server connection, no inference request, no SSH,
no real game input, no production deployment. All self-tests use a mocked SSE
stream and never touch the network.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import re
import sys
import time
import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, NamedTuple

import requests
from PIL import Image

# --------------------------------------------------------------------------- #
# Constants mirrored locally from audit/official_code/action.py so that this
# module runs standalone. verify_against_official() is the authority check.
# --------------------------------------------------------------------------- #
ACTION_START = "<|action_start|>"
ACTION_END = "<|action_end|>"
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
USER_TURN_TEXT = "current game screen"

THINK_PREFILL = THINK_OPEN
NO_THINK_PREFILL = "<think>\n\n</think>"

CHUNK_STEPS = 6
STEP_MS = 200.0 / CHUNK_STEPS
MAX_KEYS_PER_STEP = 4
MOUSE_LIMIT = 1000.0
SCROLL_LIMIT = 5.0
NOOP_ACTION_TEXT = f"{ACTION_START}0 0 0 ; ; ; ; ; ; {ACTION_END}"

LETTER_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGIT_KEYS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
FUNCTION_KEYS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
                 "Nine", "Ten", "Eleven", "Twelve")
MOUSE_BUTTON_KEYS = ("LB", "RB", "MB")
ARROW_KEYS = ("Up", "Down", "Left", "Right")
SPECIAL_KEYS = ("Shift", "Ctrl", "Alt", "Tab", "Caps", "Esc", "Space", "Enter",
                "Back", "Delete", "Insert", "Home", "End", "Pause")
PUNCTUATION_KEYS = ("Equal", "Minus", "Period", "Slash", "Quote")
KEY_VOCAB: frozenset[str] = frozenset(
    LETTER_KEYS + DIGIT_KEYS + FUNCTION_KEYS + MOUSE_BUTTON_KEYS + ARROW_KEYS
    + SPECIAL_KEYS + PUNCTUATION_KEYS
)

_RESAMPLE_SRC = getattr(Image, "Resampling", Image)
_RESAMPLE = {
    "bilinear": _RESAMPLE_SRC.BILINEAR,
    "lanczos": _RESAMPLE_SRC.LANCZOS,
    "bicubic": _RESAMPLE_SRC.BICUBIC,
}

_RE_MOUSE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)[\s,]+(-?\d+(?:\.\d+)?)\s*$"
)
_RE_TAG = re.compile(r"<\|action_start\|[>}](.*?)<\|action_end\|[>}]", re.DOTALL)
_RE_BARE = re.compile(r"^\s*-?\d")


# --------------------------------------------------------------------------- #
# Configuration -- shared fields/defaults are identical to official PolicyConfig
# --------------------------------------------------------------------------- #
@dataclass
class StreamingConfig:
    """Official PolicyConfig defaults + streaming-only audit knobs.

    The first 16 fields mirror audit/official_code/policy.py::PolicyConfig
    exactly (same names, same order, same default values). Streaming-only fields
    are appended and never alter the official request-body subset.
    """

    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "gamescaling"
    api_key: str = "EMPTY"
    history_len: int = 5
    history_think: int = 0                      # -1 = keep all reasoning
    image_size: tuple[int, int] | None = None
    image_format: str = "jpeg"                  # jpeg | png
    jpeg_quality: int = 90
    resample: str = "bilinear"
    reasoning: bool = True
    temperature: float = 1.0
    max_tokens: int = 2048
    stop: list[str] = field(default_factory=lambda: [ACTION_END])
    lenient_parse: bool = False
    timeout: float = 120.0

    # ---- streaming-only / audit-only knobs (not in official body) ----
    jsonl_path: str | None = None               # append one JSON object per line
    sse_chunk_size: int = 1                    # 1 = smallest client-side buffer
    verify_official: bool = True               # auto-run verify_against_official
    include_usage: bool = False                # opt-in stream_options (DEVIATES)
    official_code_dir: str | None = None       # override path to official_code/
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def prefill(self) -> str:
        return THINK_PREFILL if self.reasoning else NO_THINK_PREFILL

    def to_trace_dict(self) -> dict[str, Any]:
        """JSON-safe config snapshot; api_key is redacted unless it is EMPTY."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.api_key if self.api_key == "EMPTY" else "***REDACTED***",
            "history_len": self.history_len,
            "history_think": self.history_think,
            "image_size": list(self.image_size) if self.image_size else None,
            "image_format": self.image_format,
            "jpeg_quality": self.jpeg_quality,
            "resample": self.resample,
            "reasoning": self.reasoning,
            "prefill": self.prefill,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stop": list(self.stop),
            "lenient_parse": self.lenient_parse,
            "timeout": self.timeout,
            "jsonl_path": self.jsonl_path,
            "sse_chunk_size": self.sse_chunk_size,
            "verify_official": self.verify_official,
            "include_usage": self.include_usage,
            "official_code_dir": self.official_code_dir,
        }


class Turn(NamedTuple):
    image_url: str
    reasoning: str
    action_text: str


# --------------------------------------------------------------------------- #
# Faithful replicas of the official helpers (kept local, standalone)
# --------------------------------------------------------------------------- #
def encode_image(image: Image.Image, cfg: StreamingConfig) -> str:
    img = image
    if cfg.image_size is not None and img.size != tuple(cfg.image_size):
        img = img.resize(tuple(cfg.image_size), _RESAMPLE[cfg.resample])
    buf = io.BytesIO()
    if cfg.image_format == "png":
        img.convert("RGB").save(buf, format="PNG")
        mime = "image/png"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=cfg.jpeg_quality)
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def user_turn(image_url: str) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": USER_TURN_TEXT},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}


def assistant_turn(action_text: str, reasoning: str = "") -> dict:
    if reasoning:
        action_text = f"{THINK_OPEN}\n{reasoning.strip()}\n{THINK_CLOSE}\n\n{action_text}"
    return {"role": "assistant", "content": action_text}


def build_official_body(messages: list[dict], cfg: StreamingConfig) -> dict[str, Any]:
    """EXACT replica of official build_request(). No streaming fields here.

    Verified key set (sorted):
      add_generation_prompt, continue_final_message, max_tokens, messages,
      model, skip_special_tokens, stop, temperature
    """
    body: dict[str, Any] = {
        "model": cfg.model,
        "messages": list(messages),
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "skip_special_tokens": False,
    }
    if cfg.stop:
        body["stop"] = list(cfg.stop)
    body["messages"].append({"role": "assistant", "content": cfg.prefill})
    body["continue_final_message"] = True
    body["add_generation_prompt"] = False
    return body


def build_streaming_body(messages: list[dict], cfg: StreamingConfig) -> dict[str, Any]:
    """Official body + the single transport field SSE requires.

    Added: stream=True (OpenAI-compatible streaming switch; vLLM decides to
    stream from the JSON body, not from the client's HTTP read mode).
    Optional opt-in: stream_options={"include_usage": True} -- OFF by default
    because the official body does not send it (see README "known limits").
    NEVER adds chat_template_kwargs.
    """
    body = build_official_body(messages, cfg)
    body["stream"] = True
    if cfg.include_usage:
        body["stream_options"] = {"include_usage": True}
    assert "chat_template_kwargs" not in body
    return body


def split_reasoning_local(text: str) -> tuple[str, str, bool]:
    """Replica of official split_reasoning()."""
    text = text or ""
    if THINK_CLOSE in text:
        head, _, tail = text.rpartition(THINK_CLOSE)
        return head.split(THINK_OPEN)[-1].strip(), tail, True
    if THINK_OPEN in text:
        return text.split(THINK_OPEN)[-1].strip(), "", False
    return "", text, True


def finalize_text(text: str, finish_reason: str | None, cfg: StreamingConfig) -> str:
    """EXACT replica of official finalize().

    Restores the prefill (not part of streamed text), repairs a bare body whose
    tags were stripped, and re-appends ACTION_END when the stop string removed
    it. ACTION_END is appended ONLY for finish_reason == "stop".
    """
    text = text or ""
    if not text.lstrip().startswith(THINK_OPEN):
        text = cfg.prefill + text
    _, tail, closed = split_reasoning_local(text)
    if closed and ACTION_START not in tail:
        bare = tail.strip()
        if re.match(r"^-?\d", bare) and ";" in bare:
            text = text[: len(text) - len(tail)] + ACTION_START + bare
    if (finish_reason == "stop" and ACTION_START in text
            and ACTION_END not in text[text.rfind(ACTION_START):]):
        text += ACTION_END
    return text


# --------------------------------------------------------------------------- #
# Lightweight local Action + compact strict parser (standalone, no imports)
# --------------------------------------------------------------------------- #
@dataclass
class LocalAction:
    mouse: tuple[float, float, float] = (0.0, 0.0, 0.0)
    steps: list[list[str]] = field(default_factory=lambda: [[] for _ in range(CHUNK_STEPS)])
    parsed: bool = False
    truncated: bool = False
    body: str = ""
    dropped: list[str] = field(default_factory=list)

    @property
    def history_text(self) -> str:
        if not self.parsed:
            return NOOP_ACTION_TEXT
        return f"{ACTION_START}{self.body}{ACTION_END}"

    @property
    def pressed(self) -> set[str]:
        return {k for group in self.steps for k in group}

    @property
    def is_noop(self) -> bool:
        return not any(self.steps) and self.mouse == (0.0, 0.0, 0.0)

    def to_text(self) -> str:
        x, y, z = (int(round(v)) for v in self.mouse)
        groups = " ; ".join(" ".join(g) for g in self.steps)
        return f"{ACTION_START}{x} {y} {z} ; {groups}{ACTION_END}"

    def signature(self) -> dict[str, Any]:
        """Normalised shape used for the official-parse comparison."""
        return {
            "parsed": self.parsed,
            "mouse": tuple(round(float(v), 6) for v in self.mouse),
            "steps": [list(g) for g in self.steps],
            "pressed": sorted(self.pressed),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "mouse": list(self.mouse), "steps": [list(g) for g in self.steps],
            "parsed": self.parsed, "truncated": self.truncated,
            "body": self.body, "dropped": list(self.dropped),
            "history_text": self.history_text, "to_text": self.to_text(),
        }


def _clamp(v: float, limit: float) -> float:
    return max(-limit, min(limit, v))


def local_strict_parse(text: str, lenient: bool = False) -> LocalAction:
    """Compact replica of official parse_action(strict path). Never raises."""
    _, tail, closed = split_reasoning_local(text)
    if closed:
        m = _RE_TAG.search(tail)
        if m:
            body = m.group(1)
        elif _RE_BARE.match(tail) and ";" in tail:
            body = tail.strip()
        else:
            body = ""
        if body:
            parts = [seg.strip() for seg in body.split(";")]
            if parts and parts[0]:
                nums: list[float] = []
                for tok in parts[0].split()[:3]:
                    try:
                        nums.append(float(tok))
                    except ValueError:
                        nums = []
                        break
                if len(nums) >= 3:
                    steps: list[list[str]] = []
                    dropped: list[str] = []
                    for group in parts[1:CHUNK_STEPS + 1]:
                        keys: list[str] = []
                        for tok in group.split():
                            (keys if tok in KEY_VOCAB else dropped).append(tok)
                        steps.append(keys[:MAX_KEYS_PER_STEP])
                    while len(steps) < CHUNK_STEPS:
                        steps.append([])
                    return LocalAction(
                        mouse=(_clamp(nums[0], MOUSE_LIMIT),
                               _clamp(nums[1], MOUSE_LIMIT),
                               _clamp(nums[2], SCROLL_LIMIT)),
                        steps=steps, parsed=True, body=body, dropped=dropped,
                    )
            if not lenient:
                return LocalAction()
    elif not lenient:
        return LocalAction(truncated=True)

    # Best-effort lenient fallback (prototype only; official via verify is boss).
    flat = (text or "").replace(THINK_OPEN, " ").replace(THINK_CLOSE, " ")
    bm = re.search(r"(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*(;[^\n<]*)", flat, re.DOTALL)
    if not bm:
        return LocalAction(truncated=not closed)
    nums = [float(bm.group(i)) for i in (1, 2, 3)]
    segments = [s.strip() for s in bm.group(4).split(";")]
    steps = []
    dropped = []
    for group in segments[:CHUNK_STEPS]:
        keys = []
        for tok in group.split():
            (keys if tok in KEY_VOCAB else dropped).append(tok)
        steps.append(keys[:MAX_KEYS_PER_STEP])
    while len(steps) < CHUNK_STEPS:
        steps.append([])
    return LocalAction(
        mouse=(_clamp(nums[0], MOUSE_LIMIT), _clamp(nums[1], MOUSE_LIMIT),
               _clamp(nums[2], SCROLL_LIMIT)),
        steps=steps, parsed=True, body=bm.group(0), dropped=dropped,
        truncated=not closed,
    )


# --------------------------------------------------------------------------- #
# Incremental cumulative-text parser with the three required callbacks
# --------------------------------------------------------------------------- #
class IncrementalActionParser:
    """Scans cumulative text on every chunk; emits once-per-milestone.

    Callbacks receive a plain dict (JSON-friendly):
      on_mouse_header : {run_id, t_arrive, t_rel_ms, mouse_text, cumulative_text}
      on_group        : {run_id, group_index (1..6), t_arrive, t_rel_ms,
                         group_text, cumulative_text}
      on_done         : {run_id, t_arrive, t_rel_ms, metrics, action, ...}

    g1..g5 are considered complete when the following ";" arrives; g6 is
    complete when <|action_end|> arrives or the stream ends (the stop string
    usually removes ACTION_END, so end-of-stream is the normal trigger).
    """

    def __init__(self, cfg: StreamingConfig, run_id: str, t_http_send: float,
                 on_mouse_header: Callable[[dict], None] | None = None,
                 on_group: Callable[[dict], None] | None = None,
                 on_done: Callable[[dict], None] | None = None):
        self.cfg = cfg
        self.run_id = run_id
        self.t_http_send = t_http_send
        self.cb_header = on_mouse_header
        self.cb_group = on_group
        self.cb_done = on_done

        self.text = ""                       # raw cumulative generated deltas
        self.warnings: list[str] = []
        self._emitted_header = False
        self._emitted_groups: set[int] = set()
        self._t_action_start: float | None = None
        self._t_mouse_header: float | None = None
        self._t_groups: dict[int, float] = {}
        self._group_texts: dict[int, str] = {}
        self._mouse_text = ""
        self._nums: tuple[float, float, float] | None = None
        self._body = ""
        self._done = False
        self.group_intervals_ms: dict[str, float] = {}
        self.metrics_extra: dict[str, Any] = {}

    def _effective(self) -> str:
        """Reconstruct the full turn (mirror finalize's prefill restoration)."""
        if self.text.lstrip().startswith(THINK_OPEN):
            return self.text
        return self.cfg.prefill + self.text

    def _locate(self) -> int | None:
        """Index right after ACTION_START (or bare body start), else None."""
        eff = self._effective()
        if THINK_CLOSE not in eff:
            return None                     # still inside <think>; quoted tags are noise
        tail = eff.rpartition(THINK_CLOSE)[2]
        base = len(eff) - len(tail)
        i = tail.rfind(ACTION_START)
        if i >= 0:
            return base + i + len(ACTION_START)
        m = re.match(r"^\s*(-?\d)", tail)
        if m and ";" in tail:
            return base + m.start(1)
        return None

    def _fire(self, cb: Callable[[dict], None] | None, info: dict) -> None:
        if cb is None:
            return
        try:
            cb(info)
        except Exception as exc:                     # a user callback must not kill the stream
            self.warnings.append(f"callback error: {type(exc).__name__}: {exc}")

    def _rel(self, t: float) -> float:
        return round((t - self.t_http_send) * 1000.0, 3)

    def _scan(self, t_arrive: float) -> None:
        eff = self._effective()
        start = self._locate()
        if start is None:
            return
        if self._t_action_start is None:
            self._t_action_start = t_arrive
        end_i = eff.find(ACTION_END, start)
        core_end = end_i if end_i >= 0 else len(eff)
        core = eff[start:core_end]
        self._body = core
        semis = [j for j, c in enumerate(core) if c == ";"]

        if semis and not self._emitted_header:
            head = core[: semis[0]].strip()
            m = _RE_MOUSE.match(head)
            if m:
                self._emitted_header = True
                self._t_mouse_header = t_arrive
                self._mouse_text = head
                self._nums = tuple(float(m.group(k)) for k in (1, 2, 3))  # type: ignore[assignment]
                self._fire(self.cb_header, {
                    "run_id": self.run_id, "t_arrive": t_arrive,
                    "t_rel_ms": self._rel(t_arrive), "mouse_text": head,
                    "cumulative_text": self.text,
                })

        for k in range(1, CHUNK_STEPS):     # g1..g5
            if len(semis) >= k + 1 and k not in self._emitted_groups:
                gtext = core[semis[k - 1] + 1: semis[k]].strip()
                self._emitted_groups.add(k)
                self._t_groups[k] = t_arrive
                self._group_texts[k] = gtext
                self._fire(self.cb_group, {
                    "run_id": self.run_id, "group_index": k,
                    "t_arrive": t_arrive, "t_rel_ms": self._rel(t_arrive),
                    "group_text": gtext, "cumulative_text": self.text,
                })

        if end_i >= 0 and CHUNK_STEPS not in self._emitted_groups and len(semis) >= CHUNK_STEPS:
            gtext = core[semis[CHUNK_STEPS - 1] + 1:].strip()
            self._emitted_groups.add(CHUNK_STEPS)
            self._t_groups[CHUNK_STEPS] = t_arrive
            self._group_texts[CHUNK_STEPS] = gtext
            self._fire(self.cb_group, {
                "run_id": self.run_id, "group_index": CHUNK_STEPS,
                "t_arrive": t_arrive, "t_rel_ms": self._rel(t_arrive),
                "group_text": gtext, "cumulative_text": self.text,
            })

    def feed(self, delta: str, t_arrive: float) -> None:
        if delta:
            self.text += delta
        self._scan(t_arrive)

    def finalize(self, t_done: float) -> LocalAction:
        self._scan(t_done)
        eff = self._effective()
        start = self._locate()
        if start is not None:
            end_i = eff.find(ACTION_END, start)
            core_end = end_i if end_i >= 0 else len(eff)
            core = eff[start:core_end]
            self._body = core
            semis = [j for j, c in enumerate(core) if c == ";"]
            if (len(semis) >= CHUNK_STEPS
                    and CHUNK_STEPS not in self._emitted_groups):
                gtext = core[semis[CHUNK_STEPS - 1] + 1:].strip()
                self._emitted_groups.add(CHUNK_STEPS)
                self._t_groups[CHUNK_STEPS] = t_done
                self._group_texts[CHUNK_STEPS] = gtext
                self._fire(self.cb_group, {
                    "run_id": self.run_id, "group_index": CHUNK_STEPS,
                    "t_arrive": t_done, "t_rel_ms": self._rel(t_done),
                    "group_text": gtext, "cumulative_text": self.text,
                })

        anchors: list[tuple[str, float | None]] = [
            ("mouse_header", self._t_mouse_header),
        ] + [(f"g{k}", self._t_groups.get(k)) for k in range(1, CHUNK_STEPS + 1)]
        for (na, ta), (nb, tb) in zip(anchors, anchors[1:]):
            if ta is not None and tb is not None:
                self.group_intervals_ms[f"{na}->{nb}"] = round((tb - ta) * 1000.0, 3)

        action = self._build_action()
        self.metrics_extra = {
            "t_action_start": self._t_action_start,
            "t_mouse_header": self._t_mouse_header,
            "group_arrivals": {f"g{k}": self._t_groups.get(k) for k in range(1, CHUNK_STEPS + 1)},
            "group_intervals_ms": self.group_intervals_ms,
            "groups_seen": sorted(self._emitted_groups),
            "mouse_text": self._mouse_text,
            "group_texts": {f"g{k}": self._group_texts.get(k, "") for k in range(1, CHUNK_STEPS + 1)},
        }
        self._done = True
        return action

    def _build_action(self) -> LocalAction:
        if not self._emitted_header or self._nums is None:
            return LocalAction(truncated=True)
        nums = self._nums
        steps: list[list[str]] = []
        dropped: list[str] = []
        for k in range(1, CHUNK_STEPS + 1):
            keys: list[str] = []
            for tok in self._group_texts.get(k, "").split():
                (keys if tok in KEY_VOCAB else dropped).append(tok)
            steps.append(keys[:MAX_KEYS_PER_STEP])
        missing = [k for k in range(1, CHUNK_STEPS + 1) if k not in self._emitted_groups]
        return LocalAction(
            mouse=(_clamp(nums[0], MOUSE_LIMIT), _clamp(nums[1], MOUSE_LIMIT),
                   _clamp(nums[2], SCROLL_LIMIT)),
            steps=steps, parsed=True,
            truncated=bool(missing), body=self._body, dropped=dropped,
        )

    def fire_done(self, t_done: float, info: dict) -> None:
        payload = {
            "run_id": self.run_id, "t_arrive": t_done,
            "t_rel_ms": self._rel(t_done), "cumulative_text": self.text,
            **(info or {}),
        }
        self._fire(self.cb_done, payload)


# --------------------------------------------------------------------------- #
# SSE line parsing (pure function -> easy offline self-test)
# --------------------------------------------------------------------------- #
def parse_sse_lines(lines: Iterable[str]) -> Iterator[str]:
    """Yield one 'data' payload per SSE event. Handles multi-line data, comments."""
    buf: list[str] = []
    for line in lines:
        if line is None:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\r")
        if line == "":
            if buf:
                yield "\n".join(buf)
                buf = []
            continue
        if line.startswith(":"):
            continue                                   # keep-alive / comment
        if line.startswith("data:"):
            payload = line[5:]
            if payload.startswith(" "):
                payload = payload[1:]
            buf.append(payload)
        # event:, id:, retry: are ignored
    if buf:
        yield "\n".join(buf)


# --------------------------------------------------------------------------- #
# Trace writer -> JSONL, one self-contained object per line
# --------------------------------------------------------------------------- #
class TraceWriter:
    def __init__(self, run_id: str, path: str | None, config: dict[str, Any],
                 t_http_send: float):
        self.run_id = run_id
        self.path = path
        self.records: list[dict[str, Any]] = []
        self.t_http_send = t_http_send
        self.emit({
            "event": "run_start",
            "config": config,
            "t_http_send": t_http_send,
            "t_http_send_wall": datetime.now(timezone.utc).isoformat(),
        })

    def emit(self, obj: dict[str, Any]) -> dict[str, Any]:
        rec = {"run_id": self.run_id, **obj}
        self.records.append(rec)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                # Never let trace I/O break the audit stream.
                self.records.append({"run_id": self.run_id, "event": "trace_io_error",
                                     "error": f"{type(exc).__name__}: {exc}"})
        return rec

    def rel(self, t: float | None) -> float | None:
        return None if t is None else round((t - self.t_http_send) * 1000.0, 3)


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #
@dataclass
class StreamEvent:
    type: str                                   # start|chunk|mouse_header|group|done|error
    run_id: str
    index: int | None = None
    t_arrive: float | None = None
    t_rel_ms: float | None = None
    delta: str | None = None
    cumulative_text: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    group_index: int | None = None
    group_text: str | None = None
    decision: "StreamingDecision | None" = None
    error: str | None = None
    raw: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        if d.get("decision") is not None:
            d["decision"] = "<StreamingDecision>"
        return d


@dataclass
class StreamingDecision:
    """Outcome of one act()/act_stream() call. Always returned, even on error."""
    action: LocalAction
    text: str                                   # finalized (prefill restored, ACTION_END re-appended)
    raw_text: str                               # raw cumulative deltas, no finalize
    reasoning: str
    finish_reason: str | None
    usage: dict[str, Any] | None
    completion_tokens: int | None
    error: str | None
    run_id: str
    config: dict[str, Any]
    t_http_send: float
    t_headers: float | None
    t_first_token: float | None
    t_last_token: float | None
    t_done: float
    metrics: dict[str, Any]
    chunks: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    verify: dict[str, Any] | None = None
    truncated: bool = False

    def as_dict(self, *, include_trace: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": self.run_id, "action": self.action.as_dict(),
            "text": self.text, "raw_text": self.raw_text,
            "reasoning": self.reasoning, "finish_reason": self.finish_reason,
            "usage": self.usage, "completion_tokens": self.completion_tokens,
            "error": self.error, "truncated": self.truncated,
            "config": self.config, "t_http_send": self.t_http_send,
            "t_headers": self.t_headers, "t_first_token": self.t_first_token,
            "t_last_token": self.t_last_token, "t_done": self.t_done,
            "metrics": self.metrics, "chunks": self.chunks,
            "verify": self.verify,
        }
        if include_trace:
            d["trace"] = self.trace
        return d

    def verify_against_official(self, official_code_dir: str | None = None,
                                lenient: bool = False) -> dict[str, Any]:
        """Load official action.py via importlib and compare parse_action().

        No package import: action.py has no relative imports, so it loads with a
        bare spec_from_file_location. Falls back cleanly if the file is absent.
        """
        mod = load_official_action(official_code_dir)
        if mod is None:
            return {"available": False,
                    "reason": "official action.py not found; standalone module still works"}
        official = mod.parse_action(self.text, lenient=lenient)
        local_full = local_strict_parse(self.text, lenient=lenient)
        off_sig = {
            "parsed": bool(official.parsed),
            "mouse": tuple(round(float(v), 6) for v in official.mouse),
            "steps": [list(g) for g in official.steps],
            "pressed": sorted({k for g in official.steps for k in g}),
        }
        inc_sig = self.action.signature()
        full_sig = local_full.signature()
        return {
            "available": True,
            "official": off_sig,
            "incremental": inc_sig,
            "local_full": full_sig,
            "incremental_matches_official": inc_sig == off_sig,
            "local_full_matches_official": full_sig == off_sig,
            "official_body": getattr(official, "body", ""),
            "official_truncated": bool(official.truncated),
            "finish_reason": self.finish_reason,
        }


# --------------------------------------------------------------------------- #
# Official-code loaders (importlib only; never imports the official package)
# --------------------------------------------------------------------------- #
_OFFICIAL_ROOT = Path(__file__).resolve().parents[1] / "audit" / "official_code"


def _official_dir(override: str | None) -> Path:
    return Path(override) if override else _OFFICIAL_ROOT


class _NoBytecode:
    """Suppress __pycache__ writes while loading read-only official sources.

    spec.loader.exec_module() would otherwise create __pycache__/*.pyc next
    to the official files, modifying a directory this prototype must not touch.
    """
    def __enter__(self):
        self._prev = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        return self

    def __exit__(self, *exc):
        sys.dont_write_bytecode = self._prev
        return False


def load_official_action(official_code_dir: str | None = None):
    """action.py is standalone (no relative imports) -> direct file load."""
    path = _official_dir(official_code_dir) / "action.py"
    name = "gs_stream_audit_official_action"
    if name in sys.modules:
        return sys.modules[name]
    if not path.is_file():
        return None
    try:
        with _NoBytecode():
            spec = importlib.util.spec_from_file_location(name, str(path))
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    except Exception:
        sys.modules.pop(name, None)
        return None


def load_official_policy(official_code_dir: str | None = None):
    """policy.py uses relative imports; load it under a synthetic package.

    prompt.py imports .keymaps (PLAYWRIGHT_TO_TOKEN) which is absent from the
    repo, so a stub module is injected first. Used only by
    check_request_parity() -- never at request time.
    """
    d = _official_dir(official_code_dir)
    pkg_name = "gs_stream_audit_official_pkg"
    full_name = pkg_name + ".policy"
    if full_name in sys.modules:
        return sys.modules[full_name]
    if not (d / "policy.py").is_file():
        return None
    try:
        with _NoBytecode():
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = [str(d)]
            sys.modules[pkg_name] = pkg
            km = types.ModuleType(pkg_name + ".keymaps")
            km.PLAYWRIGHT_TO_TOKEN = {}
            sys.modules[km.__name__] = km
            for sub in ("action", "prompt", "policy"):
                sub_name = f"{pkg_name}.{sub}"
                spec = importlib.util.spec_from_file_location(
                    sub_name, str(d / (sub + ".py")))
                m = importlib.util.module_from_spec(spec)
                sys.modules[sub_name] = m
                spec.loader.exec_module(m)
            return sys.modules[full_name]
    except Exception:
        for k in [pkg_name, pkg_name + ".keymaps", pkg_name + ".action",
                  pkg_name + ".prompt", full_name]:
            sys.modules.pop(k, None)
        return None


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #
class StreamingGameScalingClient:
    """Independent streaming prototype. Mirrors GameScalingPolicy's public API.

        client = StreamingGameScalingClient(system_prompt, StreamingConfig(...))
        decision = client.act(frame)              # blocking, per-chunk callbacks fire
        for ev in client.act_stream(frame):       # generator version
            ...

    Nothing here connects anywhere by default; a real call only happens when you
    invoke act()/act_stream() against base_url. This module's self-test replaces
    the Session with a mock and never opens a socket.
    """

    OFFICIAL_BODY_KEYS = ("model", "messages", "temperature", "max_tokens",
                          "skip_special_tokens", "stop",
                          "continue_final_message", "add_generation_prompt")

    def __init__(self, system_prompt: str = "", config: StreamingConfig | None = None,
                 *, session: Any = None, jsonl_path: str | None = None,
                 on_chunk: Callable[[dict], None] | None = None,
                 on_mouse_header: Callable[[dict], None] | None = None,
                 on_group: Callable[[dict], None] | None = None,
                 on_done: Callable[[dict], None] | None = None):
        self.system_prompt = system_prompt          # used VERBATIM, never rebuilt
        self.cfg = config or StreamingConfig()
        if jsonl_path is not None:
            self.cfg.jsonl_path = jsonl_path
        self._session = session if session is not None else requests.Session()
        self._history: list[Turn] = []
        self._callbacks = {
            "on_chunk": on_chunk,
            "on_mouse_header": on_mouse_header,
            "on_group": on_group,
            "on_done": on_done,
        }

    @property
    def base_url(self) -> str:
        return self.cfg.base_url.rstrip("/")

    def reset(self) -> None:
        self._history.clear()

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def health_check(self) -> bool:
        """Optional: NOT called automatically (would be a network request)."""
        try:
            r = self._session.get(f"{self.base_url}/models", timeout=10,
                                  headers={"Authorization": f"Bearer {self.cfg.api_key}"})
            return r.status_code < 400
        except requests.RequestException:
            return False

    # ---- message construction (faithful replica of official messages()) ----
    def messages(self, image_url: str) -> list[dict]:
        out: list[dict] = ([{"role": "system", "content": self.system_prompt}]
                           if self.system_prompt else [])
        n = len(self._history)
        for i, turn in enumerate(self._history):
            out.append(user_turn(turn.image_url))
            age = n - i
            keep = self.cfg.history_think < 0 or age <= self.cfg.history_think
            out.append(assistant_turn(turn.action_text, turn.reasoning if keep else ""))
        out.append(user_turn(image_url))
        return out

    def _fresh_messages(self, image_url: str) -> list[dict]:
        """Empty-history message list, used by the parity check."""
        out: list[dict] = ([{"role": "system", "content": self.system_prompt}]
                           if self.system_prompt else [])
        out.append(user_turn(image_url))
        return out

    # ---- official request-body parity verification (offline, importlib) ----
    def check_request_parity(self, official_code_dir: str | None = None) -> dict[str, Any]:
        """Executable proof that our body equals official build_request().

        Compares (a) shared config defaults, (b) the exact 8-key body produced
        with an empty history, (c) absence of chat_template_kwargs, (d) prefill.
        """
        pol = load_official_policy(official_code_dir or self.cfg.official_code_dir)
        result: dict[str, Any] = {"available": pol is not None}
        if pol is None:
            result["reason"] = "official policy.py could not be loaded"
            return result

        shared = ["base_url", "model", "api_key", "history_len", "history_think",
                  "image_size", "image_format", "jpeg_quality", "resample",
                  "reasoning", "temperature", "max_tokens", "stop",
                  "lenient_parse", "timeout"]
        mine = StreamingConfig()
        theirs = pol.PolicyConfig()
        defaults_diff = {}
        for name in shared:
            a, b = getattr(mine, name, None), getattr(theirs, name, None)
            if a != b:
                defaults_diff[name] = {"ours": a, "official": b}
        result["shared_defaults_match"] = not defaults_diff
        result["shared_defaults_diff"] = defaults_diff

        url = "data:image/jpeg;base64,AA=="
        msgs = self._fresh_messages(url)
        my_body = build_official_body(msgs, self.cfg)
        off_body = pol.build_request(msgs, theirs)
        result["our_body_keys"] = sorted(my_body.keys())
        result["official_body_keys"] = sorted(off_body.keys())
        result["body_exact_match"] = my_body == off_body
        result["chat_template_kwargs_absent"] = "chat_template_kwargs" not in my_body
        result["prefill"] = self.cfg.prefill
        result["official_prefill"] = theirs.prefill
        result["prefill_match"] = self.cfg.prefill == theirs.prefill
        sb = build_streaming_body(msgs, self.cfg)
        result["streaming_only_additions"] = sorted(set(sb) - set(off_body))
        result["official_subset_intact"] = all(k in sb and sb[k] == v
                                               for k, v in off_body.items())
        return result

    def verify_against_official(self, decision: StreamingDecision) -> dict[str, Any]:
        res = decision.verify_against_official(self.cfg.official_code_dir,
                                               lenient=self.cfg.lenient_parse)
        decision.verify = res
        return res

    # ------------------------------------------------------------------ #
    # Core streaming engine
    # ------------------------------------------------------------------ #
    def act_stream(self, image: Image.Image, *, run_id: str | None = None,
                   on_chunk: Callable[[dict], None] | None = None,
                   on_mouse_header: Callable[[dict], None] | None = None,
                   on_group: Callable[[dict], None] | None = None,
                   on_done: Callable[[dict], None] | None = None,
                   ) -> Iterator[StreamEvent]:
        """Generator: yields StreamEvent per SSE chunk + milestones + final done.

        Per-chunk callbacks fire during iteration. The LAST event has
        type == "done" and carries the full StreamingDecision.
        """
        rid = run_id or uuid.uuid4().hex
        cb_chunk = on_chunk or self._callbacks["on_chunk"]
        cb_header = on_mouse_header or self._callbacks["on_mouse_header"]
        cb_group = on_group or self._callbacks["on_group"]
        cb_done = on_done or self._callbacks["on_done"]

        cfg = self.cfg
        image_url = encode_image(image, cfg)
        msgs = self.messages(image_url)
        body = build_streaming_body(msgs, cfg)

        t_http_send = time.perf_counter()
        writer = TraceWriter(rid, cfg.jsonl_path, cfg.to_trace_dict(), t_http_send)
        writer.emit({
            "event": "request",
            "url": f"{self.base_url}/chat/completions",
            "method": "POST", "stream": True,
            "body_keys": sorted(body.keys()),
            "body_model": body["model"],
            "message_count": len(body["messages"]),
            "prefill": cfg.prefill,
            "chat_template_kwargs": "chat_template_kwargs" in body,
        })

        parser = IncrementalActionParser(
            cfg, rid, t_http_send,
            on_mouse_header=lambda info: (
                writer.emit({"event": "mouse_header", **_jsonable(info)}),
                _safe_call(cb_header, info),
            ),
            on_group=lambda info: (
                writer.emit({"event": "group", **_jsonable(info)}),
                _safe_call(cb_group, info),
            ),
            on_done=None,
        )

        t_headers: float | None = None
        t_first_token: float | None = None
        t_last_token: float | None = None
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        error: str | None = None
        chunks: list[dict[str, Any]] = []
        chunk_index = 0
        done_sentinel = False
        resp = None

        yield StreamEvent(type="start", run_id=rid, t_arrive=t_http_send, t_rel_ms=0.0,
                          decision=None)

        try:
            headers = {"Authorization": f"Bearer {cfg.api_key}",
                       "Accept": "text/event-stream", **dict(cfg.extra_headers)}
            resp = self._session.post(
                f"{self.base_url}/chat/completions", json=body,
                timeout=cfg.timeout, stream=True, headers=headers,
            )
            t_headers = time.perf_counter()
            writer.emit({"event": "response_headers", "t_headers": t_headers,
                         "t_rel_ms": writer.rel(t_headers),
                         "status_code": getattr(resp, "status_code", None)})

            status = getattr(resp, "status_code", 200)
            if status >= 400:
                try:
                    detail = (resp.text or "")[:500]
                except Exception:
                    detail = ""
                raise requests.HTTPError(f"HTTP {status}: {detail}")

            lines = resp.iter_lines(decode_unicode=True, chunk_size=cfg.sse_chunk_size)
            for data in parse_sse_lines(lines):
                t_arrive = time.perf_counter()
                if data.strip() == "[DONE]":
                    done_sentinel = True
                    writer.emit({"event": "sse_done", "t_arrive": t_arrive,
                                 "t_rel_ms": writer.rel(t_arrive)})
                    break

                try:
                    payload = json.loads(data)
                except (ValueError, TypeError) as exc:
                    rec = {"event": "chunk", "index": chunk_index,
                           "t_arrive": t_arrive, "t_rel_ms": writer.rel(t_arrive),
                           "delta": None, "finish_reason": None, "usage": None,
                           "parse_error": f"{type(exc).__name__}: {exc}",
                           "raw": data[:500]}
                    chunks.append(rec)
                    writer.emit(rec)
                    chunk_index += 1
                    yield StreamEvent(type="chunk", run_id=rid, index=chunk_index - 1,
                                      t_arrive=t_arrive, t_rel_ms=writer.rel(t_arrive),
                                      raw=data[:500], error=rec["parse_error"])
                    continue

                choices = payload.get("choices") or []
                delta = ""
                if choices:
                    ch = choices[0] or {}
                    delta = (ch.get("delta") or {}).get("content") or ""
                    fr = ch.get("finish_reason")
                    if fr:
                        finish_reason = fr
                u = payload.get("usage")
                if isinstance(u, dict) and u:
                    usage = u

                if delta:
                    if t_first_token is None:
                        t_first_token = t_arrive
                    t_last_token = t_arrive

                parser.feed(delta, t_arrive)

                rec = {"event": "chunk", "index": chunk_index,
                       "t_arrive": t_arrive, "t_rel_ms": writer.rel(t_arrive),
                       "delta": delta, "cumulative_len": len(parser.text),
                       "finish_reason": finish_reason if choices else None,
                       "usage": usage if isinstance(u, dict) else None}
                chunks.append(rec)
                writer.emit(rec)
                info = {"run_id": rid, "index": chunk_index, "t_arrive": t_arrive,
                        "t_rel_ms": writer.rel(t_arrive), "delta": delta,
                        "cumulative_text": parser.text,
                        "finish_reason": finish_reason, "usage": usage}
                _safe_call(cb_chunk, info)
                yield StreamEvent(type="chunk", run_id=rid, index=chunk_index,
                                  t_arrive=t_arrive, t_rel_ms=writer.rel(t_arrive),
                                  delta=delta, cumulative_text=parser.text,
                                  finish_reason=finish_reason, usage=usage)
                chunk_index += 1

        except requests.Timeout as exc:
            error = f"timeout: {type(exc).__name__}: {exc}"
        except requests.HTTPError as exc:
            error = f"http_error: {exc}"
        except requests.RequestException as exc:
            error = f"network_error: {type(exc).__name__}: {exc}"
        except (ValueError, KeyError, TypeError) as exc:
            error = f"decode_error: {type(exc).__name__}: {exc}"
        except Exception as exc:                       # never leak a half-built stream
            error = f"unexpected_error: {type(exc).__name__}: {exc}"
        finally:
            t_done = time.perf_counter()
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass

            truncated = bool(error) or finish_reason == "length" or not done_sentinel
            incremental_action = parser.finalize(t_done)
            raw_text = parser.text
            finalized = finalize_text(raw_text, finish_reason if not error else None, cfg)

            if not incremental_action.parsed:
                fallback = local_strict_parse(finalized, lenient=cfg.lenient_parse)
                action = fallback
                action_source = "local_strict_fallback"
            else:
                action = incremental_action
                action_source = "incremental"
            action.truncated = action.truncated or truncated

            reasoning, _, closed = split_reasoning_local(finalized)
            completion_tokens = None
            if isinstance(usage, dict):
                completion_tokens = usage.get("completion_tokens")
            usage_missing = usage is None

            ttft_ms = None if t_first_token is None else (t_first_token - t_http_send) * 1000.0
            action_ttft_ms = (None if parser.metrics_extra.get("t_action_start") is None
                              else (parser.metrics_extra["t_action_start"] - t_http_send) * 1000.0)
            full_gen_ms = (None if t_first_token is None or t_last_token is None
                           else (t_last_token - t_first_token) * 1000.0)
            e2e_ms = (t_done - t_http_send) * 1000.0
            inter = []
            for a, b in zip(chunks, chunks[1:]):
                inter.append(round((b["t_arrive"] - a["t_arrive"]) * 1000.0, 3))

            metrics: dict[str, Any] = {
                "ttft_ms": _r3(ttft_ms),
                "action_ttft_ms": _r3(action_ttft_ms),
                "full_generation_latency_ms": _r3(full_gen_ms),
                "end_to_end_latency_ms": _r3(e2e_ms),
                "time_to_headers_ms": _r3(None if t_headers is None
                                          else (t_headers - t_http_send) * 1000.0),
                "first_token_to_headers_ms": _r3(
                    None if t_first_token is None or t_headers is None
                    else (t_first_token - t_headers) * 1000.0),
                "inter_chunk_intervals_ms": inter,
                "chunk_count": len(chunks),
                "nonempty_delta_count": sum(1 for c in chunks if c.get("delta")),
                "cumulative_chars": len(raw_text),
                "finish_reason": finish_reason,
                "done_sentinel_seen": done_sentinel,
                "usage": usage,
                "usage_missing": usage_missing,
                "completion_tokens": completion_tokens,
                "truncated": truncated,
                "length_truncated": finish_reason == "length",
                "action_source": action_source,
                "group_arrivals": parser.metrics_extra.get("group_arrivals", {}),
                "group_intervals_ms": parser.metrics_extra.get("group_intervals_ms", {}),
                "groups_seen": parser.metrics_extra.get("groups_seen", []),
                "mouse_text": parser.metrics_extra.get("mouse_text", ""),
                "group_texts": parser.metrics_extra.get("group_texts", {}),
                "parser_warnings": parser.warnings,
                "error": error,
            }

            decision = StreamingDecision(
                action=action, text=finalized, raw_text=raw_text,
                reasoning=reasoning if closed else "",
                finish_reason=finish_reason, usage=usage,
                completion_tokens=completion_tokens, error=error, run_id=rid,
                config=cfg.to_trace_dict(), t_http_send=t_http_send,
                t_headers=t_headers, t_first_token=t_first_token,
                t_last_token=t_last_token, t_done=t_done,
                metrics=metrics, chunks=chunks, trace=writer.records,
                truncated=truncated,
            )

            if cfg.verify_official:
                try:
                    self.verify_against_official(decision)
                except Exception as exc:
                    decision.verify = {"available": False,
                                       "reason": f"verify raised: {type(exc).__name__}: {exc}"}

            writer.emit({"event": "metrics", **_jsonable({"metrics": metrics})})
            writer.emit({"event": "done", "t_done": t_done,
                         "t_rel_ms": writer.rel(t_done),
                         "finish_reason": finish_reason, "usage": usage,
                         "usage_missing": usage_missing, "error": error,
                         "truncated": truncated, "text": finalized,
                         "action": action.as_dict(),
                         "verify": decision.verify})
            if error:
                writer.emit({"event": "error", "error": error,
                             "t_done": t_done, "t_rel_ms": writer.rel(t_done)})
                yield StreamEvent(type="error", run_id=rid, t_arrive=t_done,
                                  t_rel_ms=writer.rel(t_done), error=error)

            parser.fire_done(t_done, {"metrics": metrics, "action": action.as_dict(),
                                      "decision": decision.as_dict()})
            _safe_call(cb_done, {"run_id": rid, "t_arrive": t_done,
                                 "t_rel_ms": writer.rel(t_done),
                                 "decision": decision, "metrics": metrics})

            # history bookkeeping (same rule as official act())
            self._history.append(Turn(image_url, reasoning if closed else "",
                                      action.history_text))
            while len(self._history) > cfg.history_len:
                self._history.pop(0)

            yield StreamEvent(type="done", run_id=rid, t_arrive=t_done,
                              t_rel_ms=writer.rel(t_done), decision=decision,
                              error=error)

    def act(self, image: Image.Image, **kwargs) -> StreamingDecision:
        """Synchronous, blocking until the full response; per-chunk callbacks fire.

        Implemented by consuming act_stream(), so callback timing is identical.
        """
        decision: StreamingDecision | None = None
        for ev in self.act_stream(image, **kwargs):
            if ev.type == "done" and ev.decision is not None:
                decision = ev.decision
        if decision is None:                         # defensive; should not happen
            raise RuntimeError("act_stream() ended without a done event")
        return decision


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _r3(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _safe_call(cb: Callable[[dict], None] | None, info: dict) -> None:
    if cb is None:
        return
    try:
        cb(info)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# OFFLINE SELF-TEST -- mocked SSE only, never opens a socket
# --------------------------------------------------------------------------- #
def _mock_sse_lines(deltas: list[str], *, finish_reason: str = "stop",
                    with_usage: bool = True, done_sentinel: bool = True,
                    drop_after: int | None = None) -> list[str]:
    out: list[str] = []
    for i, d in enumerate(deltas):
        if drop_after is not None and i >= drop_after:
            break
        obj = {"id": "mock", "object": "chat.completion.chunk", "model": "gamescaling",
               "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}]}
        out.append("data: " + json.dumps(obj, ensure_ascii=False))
        out.append("")
    if drop_after is None:
        tail_obj: dict[str, Any] = {
            "id": "mock", "object": "chat.completion.chunk", "model": "gamescaling",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        if with_usage:
            tail_obj["usage"] = {"prompt_tokens": 1234, "completion_tokens": 42,
                                 "total_tokens": 1276}
        out.append("data: " + json.dumps(tail_obj, ensure_ascii=False))
        out.append("")
        if done_sentinel:
            out.append("data: [DONE]")
            out.append("")
    return out


class _MockResponse:
    def __init__(self, lines: list[str], status_code: int = 200,
                 body_text: str = "", *, raise_on_iter: Exception | None = None):
        self._lines = lines
        self.status_code = status_code
        self.text = body_text
        self.closed = False
        self._raise_on_iter = raise_on_iter

    def iter_lines(self, decode_unicode=None, chunk_size=None):
        for i, line in enumerate(self._lines):
            if self._raise_on_iter is not None and i == max(1, len(self._lines) // 2):
                raise self._raise_on_iter
            time.sleep(0.0015)                      # make t_arrive values distinct
            yield line

    def close(self):
        self.closed = True


class _MockSession:
    def __init__(self, response: _MockResponse):
        self.response = response
        self.last_kwargs: dict[str, Any] = {}
        self.calls = 0

    def post(self, url, **kwargs):
        self.calls += 1
        self.last_kwargs = {"url": url, **kwargs}
        return self.response

    def get(self, url, **kwargs):
        return _MockResponse([], status_code=200)

    def close(self):
        pass


_DEMO_DELTAS = [
    "<think>I should", " strafe and", " keep firing", "</think>",
    "<|action_start|>", "-37 -3 0 ; ", "LB S ; ", "LB ; ", "LB ; ",
    "LB ; ", "LB ; ", "LB",
]


def self_test(verbose: bool = True) -> dict[str, Any]:
    """Pure-function offline self-test. NO network, NO inference, NO SSH."""
    results: dict[str, Any] = {}

    def check(name: str, cond: bool, detail: Any = None) -> None:
        results[name] = {"ok": bool(cond), "detail": detail}
        if verbose:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if detail is None else f" :: {detail}"))

    if verbose:
        print("== streaming_client offline self-test ==")

    # 1. Request-body parity against the official module (importlib).
    c0 = StreamingGameScalingClient("SYS", StreamingConfig())
    parity = c0.check_request_parity()
    check("official_policy_loaded", parity.get("available") is True, parity.get("reason"))
    check("shared_defaults_match", parity.get("shared_defaults_match") is True,
          parity.get("shared_defaults_diff"))
    check("body_exact_match", parity.get("body_exact_match") is True,
          {"ours": parity.get("our_body_keys"), "official": parity.get("official_body_keys")})
    check("chat_template_kwargs_absent", parity.get("chat_template_kwargs_absent") is True)
    check("prefill_match", parity.get("prefill_match") is True, parity.get("prefill"))
    check("official_subset_intact_after_stream", parity.get("official_subset_intact") is True)
    check("streaming_only_addition_is_stream",
          parity.get("streaming_only_additions") == ["stream"],
          parity.get("streaming_only_additions"))

    # 2. no-reasoning prefill.
    cfg_nr = StreamingConfig(reasoning=False)
    check("no_think_prefill", cfg_nr.prefill == "<think>\n\n</think>", repr(cfg_nr.prefill))
    check("think_prefill", StreamingConfig(reasoning=True).prefill == "<think>")

    # 3. finalize(): stop string + ACTION_END re-append; length must NOT append.
    raw = "<think>x</think><|action_start|>-37 -3 0 ; LB S ; LB ; LB ; LB ; LB ; LB"
    f_stop = finalize_text(raw, "stop", StreamingConfig())
    f_len = finalize_text(raw, "length", StreamingConfig())
    check("finalize_reappends_action_end", f_stop.endswith(ACTION_END))
    check("finalize_length_no_action_end", not f_len.endswith(ACTION_END))
    check("finalize_prepends_prefill", f_stop.startswith("<think>"))

    # 4. mocked streaming run with callbacks + JSONL trace.
    events: list[str] = []
    chunks_seen: list[str] = []
    sess = _MockSession(_MockResponse(_mock_sse_lines(_DEMO_DELTAS)))
    img = Image.new("RGB", (8, 8), (10, 20, 30))
    client = StreamingGameScalingClient(
        "SYSTEM-PROMPT-VERBATIM", StreamingConfig(verify_official=True),
        session=sess,
        on_chunk=lambda i: chunks_seen.append(i["delta"]),
        on_mouse_header=lambda i: events.append("mouse_header"),
        on_group=lambda i: events.append(f"g{i['group_index']}"),
        on_done=lambda i: events.append("done"),
    )
    d = client.act(img, run_id="selftest-run-1")
    check("act_returns_decision", isinstance(d, StreamingDecision))
    check("no_error", d.error is None, d.error)
    check("parsed_action", d.action.parsed is True, d.action.as_dict())
    check("action_mouse", d.action.mouse == (-37.0, -3.0, 0.0), d.action.mouse)
    check("action_groups_g1", d.action.steps[0] == ["LB", "S"], d.action.steps)
    check("official_verify_available", d.verify and d.verify.get("available") is True, d.verify)
    check("official_verify_matches", d.verify and d.verify.get("incremental_matches_official") is True,
          d.verify)
    check("callbacks_all", all(x in events for x in
                              ["mouse_header", "g1", "g2", "g3", "g4", "g5", "g6", "done"]),
          events)
    check("per_chunk_callbacks", len(chunks_seen) == len(d.chunks), len(chunks_seen))
    check("g1_to_g6_arrivals", all(d.metrics["group_arrivals"].get(f"g{k}") is not None
                                   for k in range(1, 7)), d.metrics["group_arrivals"])
    check("group_intervals", len(d.metrics["group_intervals_ms"]) >= 6,
          d.metrics["group_intervals_ms"])
    check("ttft_present", d.metrics["ttft_ms"] is not None, d.metrics["ttft_ms"])
    check("action_ttft_present", d.metrics["action_ttft_ms"] is not None,
          d.metrics["action_ttft_ms"])
    check("e2e_present", d.metrics["end_to_end_latency_ms"] is not None)
    check("finish_reason_stop", d.finish_reason == "stop", d.finish_reason)
    check("usage_captured", d.completion_tokens == 42, d.usage)
    check("text_ends_with_action_end", d.text.endswith(ACTION_END))
    check("body_sent_with_stream", sess.last_kwargs.get("json", {}).get("stream") is True)
    check("body_no_chat_template_kwargs",
          "chat_template_kwargs" not in sess.last_kwargs.get("json", {}))
    check("body_keepalive_session_used", sess.calls == 1)
    check("history_len_1", len(client._history) == 1)
    check("trace_has_run_start_and_metrics",
          any(r["event"] == "run_start" for r in d.trace)
          and any(r["event"] == "metrics" for r in d.trace))
    check("trace_chunk_timestamps", all("t_arrive" in r for r in d.trace
                                        if r.get("event") == "chunk"))

    # 5. act_stream generator yields events and a final done.
    sess2 = _MockSession(_MockResponse(_mock_sse_lines(_DEMO_DELTAS)))
    c2 = StreamingGameScalingClient("SYS", StreamingConfig(), session=sess2)
    evs = list(c2.act_stream(img, run_id="selftest-run-2"))
    check("act_stream_yields_done", evs[-1].type == "done" and evs[-1].decision is not None)
    check("act_stream_yields_chunks", any(e.type == "chunk" for e in evs))

    # 6. usage missing -> graceful.
    sess3 = _MockSession(_MockResponse(_mock_sse_lines(_DEMO_DELTAS, with_usage=False)))
    d3 = StreamingGameScalingClient("SYS", StreamingConfig(), session=sess3).act(img)
    check("usage_missing_handled", d3.metrics["usage_missing"] is True
          and d3.completion_tokens is None and d3.error is None)

    # 7. finish_reason=length -> truncated, no ACTION_END appended.
    sess4 = _MockSession(_MockResponse(_mock_sse_lines(_DEMO_DELTAS, finish_reason="length")))
    d4 = StreamingGameScalingClient("SYS", StreamingConfig(), session=sess4).act(img)
    check("length_truncated", d4.truncated is True and d4.metrics["length_truncated"] is True)
    check("length_no_action_end", not d4.text.endswith(ACTION_END))

    # 8. HTTP 500.
    sess5 = _MockSession(_MockResponse([], status_code=500, body_text="boom"))
    d5 = StreamingGameScalingClient("SYS", StreamingConfig(), session=sess5).act(img)
    check("http_500_error", d5.error is not None and "HTTP 500" in d5.error, d5.error)

    # 9. mid-stream disconnect.
    sess6 = _MockSession(_MockResponse(
        _mock_sse_lines(_DEMO_DELTAS),
        raise_on_iter=requests.ConnectionError("simulated disconnect")))
    d6 = StreamingGameScalingClient("SYS", StreamingConfig(), session=sess6).act(img)
    check("disconnect_error", d6.error is not None and "network_error" in d6.error, d6.error)
    check("disconnect_partial_text", len(d6.raw_text) > 0, len(d6.raw_text))

    # 10. reasoning=False path (prefill restore needed for incremental parse).
    sess7 = _MockSession(_MockResponse(_mock_sse_lines([
        "\n\n", "<|action_start|>", "12 0 0 ; ", "W ; ", "W ; ", "W ; ", "W ; ", "W ; ", "W",
    ])))
    cfg7 = StreamingConfig(reasoning=False)
    d7 = StreamingGameScalingClient("SYS", cfg7, session=sess7).act(img)
    check("no_reasoning_parsed", d7.action.parsed is True, d7.action.as_dict())
    check("no_reasoning_mouse", d7.action.mouse == (12.0, 0.0, 0.0), d7.action.mouse)

    ok = all(v.get("ok") for v in results.values())
    results["_all_passed"] = ok
    results["_count"] = len([k for k in results if not k.startswith("_")])
    if verbose:
        print(f"== {'ALL PASS' if ok else 'SOME FAILED'} "
              f"({sum(1 for k, v in results.items() if not k.startswith('_') and v.get('ok'))}"
              f"/{results['_count']}) ==")
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="GameScaling streaming audit prototype")
    ap.add_argument("--self-test", action="store_true", help="offline mock-SSE self-test")
    ap.add_argument("--parity", action="store_true", help="print request-body parity report")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.parity:
        print(json.dumps(StreamingGameScalingClient("SYS").check_request_parity(),
                         ensure_ascii=False, indent=2, default=str))
    if args.self_test or not args.parity:
        res = self_test(verbose=not args.quiet)
        sys.exit(0 if res["_all_passed"] else 1)
