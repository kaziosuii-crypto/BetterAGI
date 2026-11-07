#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Belief Store：玩家相信/怀疑/证伪了什么。

只保存玩家认知，不保存世界真相；ground_truth 永不进入玩家 prompt。
证伪保留历史，不删除旧信念。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import atomic_write_json, backup_corrupt_file, clean_text, read_json, utc_now_iso

SCHEMA_VERSION = 1
STATUSES = {"known_fact", "belief", "hypothesis", "suspicion", "unknown", "disproven"}
_FORBIDDEN = {"ground_truth", "world_only"}


class BeliefError(ValueError):
    """信念状态非法或文件版本过新。"""


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _list(value: Any, limit: int = 8, item_limit: int = 200) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: List[str] = []
    for item in value:
        text = _text(item, item_limit)
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _ratio(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _fingerprint(proposition: str) -> str:
    return hashlib.sha1(proposition.lower().encode("utf-8")).hexdigest()[:20]


def normalize_belief(data: Any, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise BeliefError("belief 必须是 JSON 对象")
    proposition = _text(data.get("proposition"), 500)
    if not proposition:
        raise BeliefError("belief.proposition 不能为空")
    status = _text(data.get("status"), 30).lower() or "hypothesis"
    if status in _FORBIDDEN:
        raise BeliefError("ground_truth 不能进入玩家信念")
    if status not in STATUSES:
        status = "hypothesis"
    moment = int(now_ms if now_ms is not None else time.time() * 1000)
    belief_id = _text(data.get("belief_id"), 80) or "b-" + _fingerprint(proposition)
    return {
        "schema_version": SCHEMA_VERSION,
        "belief_id": belief_id,
        "proposition": proposition,
        "status": status,
        "confidence": _ratio(data.get("confidence"), 0.5),
        "evidence": _list(data.get("evidence")),
        "contradictions": _list(data.get("contradictions")),
        # 历史是按顺序排列的事件，不去重也不截断。
        "history": [_text(entry, 200) for entry in data.get("history", [])
                    if isinstance(entry, str) and _text(entry, 200)]
        if isinstance(data.get("history"), (list, tuple)) else [],
        "first_formed_ms": int(data.get("first_formed_ms") if data.get("first_formed_ms") is not None else moment),
        "last_updated_ms": int(data.get("last_updated_ms") if data.get("last_updated_ms") is not None else moment),
        "fingerprint": _text(data.get("fingerprint"), 40) or _fingerprint(proposition),
    }


class BeliefStore:
    """`state/belief_state.json` 的原子存储。"""

    def __init__(self, path: Path | str, *, clock: Optional[Callable[[], int]] = None, logger: Any = None):
        self.path = Path(path)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger

    def _empty(self) -> Dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "updated_at": "", "beliefs": []}

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = read_json(self.path)
        except ValueError:
            self._recover()
            return self._empty()
        if not isinstance(data, dict):
            self._recover()
            return self._empty()
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version > SCHEMA_VERSION:
            if isinstance(version, int) and version > SCHEMA_VERSION:
                raise BeliefError("belief 版本过新：{0}".format(version))
            self._recover()
            return self._empty()
        beliefs = []
        for item in data.get("beliefs") or []:
            try:
                beliefs.append(normalize_belief(item, now_ms=self._clock()))
            except BeliefError:
                continue
        return {"schema_version": SCHEMA_VERSION, "updated_at": _text(data.get("updated_at"), 40), "beliefs": beliefs}

    def apply(self, updates: Any) -> List[Dict[str, Any]]:
        if not isinstance(updates, (list, tuple)):
            return []
        data = self.load()
        beliefs = list(data.get("beliefs") or [])
        changed: List[Dict[str, Any]] = []
        for update in updates[:8]:
            try:
                normalized = normalize_belief(update, now_ms=self._clock())
            except BeliefError as exc:
                if self.log is not None:
                    self.log.warning("忽略非法 belief：%s", exc)
                continue
            normalized["last_updated_ms"] = self._clock()
            existing = next(
                (item for item in beliefs if item["fingerprint"] == normalized["fingerprint"]),
                None,
            )
            if existing is None:
                beliefs.append(normalized)
                changed.append(normalized)
                continue
            # 每次状态改变都追加一条转换，重复转换也须保留。
            old_status = existing.get("status")
            new_status = normalized["status"] if "status" in update else old_status
            if old_status is not None and new_status != old_status:
                history = list(existing.get("history") or [])
                history.append("{0} -> {1}".format(old_status, new_status))
                existing["history"] = history
            if new_status == "disproven" and old_status != "disproven":
                existing["contradictions"] = _list(
                    list(existing.get("contradictions") or []) + normalized["evidence"],
                    limit=8,
                )
            existing["contradictions"] = _list(
                list(existing.get("contradictions") or []) + normalized["contradictions"], limit=8
            )
            existing["status"] = new_status
            if "confidence" in update:
                existing["confidence"] = normalized["confidence"]
            existing["evidence"] = _list(
                list(existing.get("evidence") or []) + normalized["evidence"], limit=8
            )
            existing["last_updated_ms"] = normalized["last_updated_ms"]
            changed.append(existing)
        if changed:
            self._save(beliefs)
        return changed

    def _save(self, beliefs: List[Dict[str, Any]]) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now_iso(self._clock()),
                "beliefs": beliefs,
            },
        )

    def list(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        beliefs = self.load().get("beliefs") or []
        beliefs.sort(key=lambda item: int(item.get("last_updated_ms") or 0), reverse=True)
        return beliefs[:limit] if limit is not None else beliefs

    def summary(self, limit: int = 5) -> List[str]:
        lines: List[str] = []
        for item in self.list(limit=limit):
            if item.get("status") == "disproven":
                lines.append("我曾经以为「{0}」，后来发现猜错了。".format(item["proposition"]))
            elif item.get("status") == "suspicion":
                lines.append("我有点怀疑「{0}」。".format(item["proposition"]))
            elif item.get("status") in ("known_fact", "belief"):
                lines.append("我认为「{0}」。".format(item["proposition"]))
            else:
                lines.append("我还不确定「{0}」。".format(item["proposition"]))
        return lines

    def _recover(self) -> None:
        try:
            backup_corrupt_file(self.path, self._clock())
        except OSError:
            pass
