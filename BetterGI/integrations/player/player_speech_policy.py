#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Speech Policy：决定这一刻是否开口，以及 TTS 的语速/音量/停顿提示。

Shadow 阶段只记录决定；未来 Active 模式把同一决定交给 TTS/渲染。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Dict, Optional

MIN_GAP_MS = 6000
IMPORTANT_EMOTIONS = {"surprised", "excited", "worried", "angry", "sad"}


class SpeechPolicy:
    def __init__(self, *, min_gap_ms: int = MIN_GAP_MS, clock: Optional[Callable[[], int]] = None):
        self.min_gap_ms = max(1000, int(min_gap_ms))
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.last_spoken_ms = 0
        self.spoken_count = 0
        self.last_speech_proposed: Optional[Dict[str, Any]] = None
        self.last_speech_delivered: Optional[Dict[str, Any]] = None
        self.last_speech_suppressed: Optional[Dict[str, Any]] = None
        self.last_speech_unknown: Optional[Dict[str, Any]] = None
        self._pending: Dict[str, Dict[str, Any]] = {}

    def decide(
        self,
        proposal: Dict[str, Any],
        *,
        dialogue_locked: bool = False,
        now_ms: Optional[int] = None,
        silence_bias: str = "medium",
        emotion_view: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        now = int(now_ms if now_ms is not None else self._clock())
        speech = proposal.get("speech") if isinstance(proposal, dict) else None
        speech = speech if isinstance(speech, dict) else {}
        text = speech.get("text") if isinstance(speech.get("text"), str) else ""
        text = " ".join(text.split())[:160]
        self.last_speech_proposed = {"text": text, "timestamp_ms": now}
        if not text:
            return self._suppress(text, "empty", now)
        if speech.get("should_speak") is False:
            return self._suppress(text, "model_silent", now)
        if dialogue_locked:
            return self._suppress(text, "dialogue_lock", now)
        try:
            raw_intensity = speech.get("intensity")
            intensity = min(1.0, max(0.0, float(0.5 if raw_intensity is None else raw_intensity)))
        except (TypeError, ValueError):
            intensity = 0.5
        emotion = str(speech.get("emotion") or "calm").lower()
        gap = now - self.last_spoken_ms
        important = intensity >= 0.8 or emotion in IMPORTANT_EMOTIONS
        if self.spoken_count and gap < self.min_gap_ms and not important:
            return self._suppress(text, "cooldown", now)
        if silence_bias == "high" and intensity < 0.6 and not important:
            return self._suppress(text, "silence_bias", now)
        if intensity >= 0.75:
            pace = "fast"
        elif intensity <= 0.3:
            pace = "slow"
        else:
            pace = "normal"
        mood_damped = False
        if isinstance(emotion_view, dict) and float(emotion_view.get("sadness") or 0) >= 0.5:
            if emotion in ("excited", "happy") and intensity < 0.95:
                # 余韵：重大难过之后，普通宝箱不会立刻变成狂喜。
                intensity = min(intensity, 0.5)
                pace = "normal"
                mood_damped = True
        decision_id = uuid.uuid4().hex
        decision = {
            "speak": True,
            "reason": "ok",
            "decision_id": decision_id,
            "text": text,
            "emotion": emotion,
            "intensity": intensity,
            "pace": pace,
            "volume": "soft" if emotion in ("sad", "worried", "nervous") else "normal",
            "pause_before_ms": 300 if emotion in ("sad", "worried", "thinking") else 120,
            "interruptible": emotion not in ("angry", "determined"),
            "mood_damped": mood_damped,
        }
        if len(self._pending) >= 32:
            self._pending.pop(next(iter(self._pending)))
        self._pending[decision_id] = {"text": text, "timestamp_ms": now}
        return decision

    def _suppress(self, text: str, reason: str, now: int) -> Dict[str, Any]:
        self.last_speech_suppressed = {
            "text": text, "reason": reason, "timestamp_ms": now,
        }
        return {"speak": False, "reason": reason, "text": text}

    def acknowledge_delivery(self, decision_id: str, *, now_ms: Optional[int] = None) -> bool:
        """Only a confirmed timeline write counts as delivered; unknown IDs fail closed."""
        proposed = self._pending.pop(decision_id, None)
        if proposed is None:
            return False
        now = int(now_ms if now_ms is not None else self._clock())
        self.last_speech_delivered = {**proposed, "decision_id": decision_id, "delivered_ms": now}
        self.last_speech_suppressed = None
        self.last_speech_unknown = None
        self.last_spoken_ms = max(self.last_spoken_ms, now)
        self.spoken_count += 1
        return True

    def reject_delivery(self, decision_id: str, reason: str, *, now_ms: Optional[int] = None) -> bool:
        """Record a failed delivery without starting the speech cooldown."""
        proposed = self._pending.pop(decision_id, None)
        if proposed is None:
            return False
        self._suppress(proposed["text"], str(reason)[:80] or "delivery_failed",
                       int(now_ms if now_ms is not None else self._clock()))
        return True

    def mark_delivery_unknown(self, decision_id: str, *, now_ms: Optional[int] = None) -> bool:
        """Do not infer whether a timed-out timeline write happened or retry it."""
        proposed = self._pending.pop(decision_id, None)
        if proposed is None:
            return False
        self.last_speech_unknown = {**proposed, "decision_id": decision_id,
                                    "timestamp_ms": int(now_ms if now_ms is not None else self._clock())}
        return True

    def reset(self) -> None:
        self.last_spoken_ms = 0
        self.spoken_count = 0
        self.last_speech_proposed = None
        self.last_speech_delivered = None
        self.last_speech_suppressed = None
        self.last_speech_unknown = None
        self._pending.clear()
