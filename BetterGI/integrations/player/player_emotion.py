#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连续情绪状态：衰减 + 事件强化，而不是每句话独立贴标签。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import atomic_write_json, backup_corrupt_file, read_json, utc_now_iso

SCHEMA_VERSION = 1
DIMENSIONS = (
    "joy",
    "sadness",
    "fear",
    "anger",
    "curiosity",
    "confusion",
    "excitement",
    "nostalgia",
    "tension",
)
# 半衰期：普通情绪 20 分钟；重大事件通过强化值保留更久。
HALF_LIFE_MS = 20 * 60 * 1000
MAX_DELTA = 0.35


class EmotionError(ValueError):
    """情绪状态非法。"""


def _ratio(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _empty() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": "",
        "last_updated_ms": 0,
        **{key: 0.0 for key in DIMENSIONS},
    }


def _decayed(values: Dict[str, Any], elapsed_ms: int) -> Dict[str, float]:
    factor = 0.5 ** (max(0, elapsed_ms) / HALF_LIFE_MS)
    return {key: round(_ratio(values.get(key)) * factor, 4) for key in DIMENSIONS}


class EmotionStore:
    """`state/emotion_state.json` 的原子存储。"""

    def __init__(self, path: Path | str, *, clock: Optional[Callable[[], int]] = None, logger: Any = None):
        self.path = Path(path)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return _empty()
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            self._recover()
            return _empty()
        if not isinstance(data, dict):
            self._recover()
            return _empty()
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            self._recover()
            return _empty()
        if version > SCHEMA_VERSION:
            raise EmotionError("emotion 版本过新：{0}".format(version))
        state = _empty()
        state["updated_at"] = str(data.get("updated_at") or "")[:40]
        state["last_updated_ms"] = int(data.get("last_updated_ms") or 0)
        for key in DIMENSIONS:
            state[key] = _ratio(data.get(key))
        return state

    def view(self, *, now_ms: Optional[int] = None) -> Dict[str, float]:
        state = self.load()
        last = int(state.get("last_updated_ms") or 0)
        now = int(now_ms if now_ms is not None else self._clock())
        if last <= 0:
            return {key: 0.0 for key in DIMENSIONS}
        return _decayed(state, now - last)

    def apply(self, changes: Any, *, now_ms: Optional[int] = None) -> Dict[str, float]:
        """事件强化：每个维度最多变化 MAX_DELTA，并先按时间衰减。"""
        if not isinstance(changes, dict):
            return self.view(now_ms=now_ms)
        now = int(now_ms if now_ms is not None else self._clock())
        state = self.load()
        last = int(state.get("last_updated_ms") or now)
        current = _decayed(state, now - last)
        for key, value in list(changes.items())[:len(DIMENSIONS)]:
            if key not in DIMENSIONS:
                continue
            target = _ratio(value)
            delta = max(-MAX_DELTA, min(MAX_DELTA, target - current[key]))
            current[key] = round(min(1.0, max(0.0, current[key] + delta)), 4)
        state.update(current)
        state["last_updated_ms"] = now
        state["updated_at"] = utc_now_iso(now)
        atomic_write_json(self.path, state)
        return current

    def summary(self, *, now_ms: Optional[int] = None, limit: int = 4) -> List[str]:
        values = self.view(now_ms=now_ms)
        labels = sorted(
            ((key, value) for key, value in values.items() if value >= 0.25),
            key=lambda item: item[1],
            reverse=True,
        )
        names = {
            "joy": "开心",
            "sadness": "难过",
            "fear": "害怕",
            "anger": "生气",
            "curiosity": "好奇",
            "confusion": "困惑",
            "excitement": "兴奋",
            "nostalgia": "怀念",
            "tension": "紧张",
        }
        return ["{0}".format(names[key]) for key, _ in labels[:limit]]

    def _recover(self) -> None:
        try:
            backup_corrupt_file(self.path, self._clock())
        except OSError:
            pass
