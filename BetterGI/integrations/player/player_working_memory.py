#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Working Memory：当前几十秒到几分钟的短期状态。

Phase 2 只做结构化存储与更新；情绪/关系/线程的长期状态仍留给后续阶段。
Working 是 ContextBuilder 的必选块，也是 episodic 抽取的原料。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import (
    atomic_write_json,
    backup_corrupt_file,
    clean_text,
    file_lock,
    read_json,
    utc_now_iso,
)

from shadow_recorder import _sanitize

SCHEMA_VERSION = 1

_MAX_CHARACTERS = 12
_MAX_DIALOGUE = 20
_MAX_EVENTS = 20
_MAX_QUESTIONS = 10
_MAX_PLAN = 8
_MAX_EMOTIONS = 12


class WorkingMemoryError(ValueError):
    """Working memory 文件版本过新或无法安全读取。"""


class WorkingMemorySchemaError(WorkingMemoryError):
    """文件版本高于当前程序，拒绝降级覆盖。"""


def _text(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _text_list(value: Any, limit: int, item_limit: int = 120) -> List[str]:
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


def _ratio(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def default_working() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": "",
        "location": "",
        "characters": [],
        "dialogue": [],
        "recent_events": [],
        "current_activity": "",
        "why": "",
        "open_questions": [],
        "emotion": {},
        "short_plan": [],
        "last_motor_feedback": {},
    }


def _normalize_event(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        text = _text(item, 240)
        return {"t_ms": 0, "kind": "event", "text": text} if text else None
    if not isinstance(item, dict):
        return None
    text = _text(item.get("text") or item.get("what"), 240)
    if not text:
        return None
    try:
        t_ms = max(0, int(item.get("t_ms") or 0))
    except (TypeError, ValueError):
        t_ms = 0
    return {"t_ms": t_ms, "kind": _text(item.get("kind"), 30) or "event", "text": text}


def _normalize_dialogue(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        return None
    text = _text(item.get("text"), 300)
    if not text:
        return None
    try:
        t_ms = max(0, int(item.get("t_ms") or 0))
    except (TypeError, ValueError):
        t_ms = 0
    return {
        "speaker": _text(item.get("speaker"), 60),
        "text": text,
        "emotion": _text(item.get("emotion"), 30),
        "t_ms": t_ms,
        "confidence": _ratio(item.get("confidence"), 0.5),
    }


def normalize_working(data: Any) -> Dict[str, Any]:
    """规范化 working 字段；保留未知字段以便后续阶段只增不删。"""
    base = default_working()
    if not isinstance(data, dict):
        return base
    data = _sanitize(data)

    merged = dict(data)
    merged.update(
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _text(data.get("updated_at"), 40),
            "location": _text(data.get("location"), 200),
            "current_activity": _text(data.get("current_activity"), 240),
            "why": _text(data.get("why"), 300),
        }
    )
    merged["characters"] = _text_list(
        data.get("characters"), _MAX_CHARACTERS, item_limit=40
    )
    merged["open_questions"] = _text_list(
        data.get("open_questions"), _MAX_QUESTIONS, item_limit=160
    )
    merged["short_plan"] = _text_list(data.get("short_plan"), _MAX_PLAN, item_limit=160)

    dialogue: List[Dict[str, Any]] = []
    if isinstance(data.get("dialogue"), (list, tuple)):
        for item in list(data["dialogue"])[-_MAX_DIALOGUE:]:
            normalized = _normalize_dialogue(item)
            if normalized:
                dialogue.append(normalized)
    merged["dialogue"] = dialogue

    events: List[Dict[str, Any]] = []
    if isinstance(data.get("recent_events"), (list, tuple)):
        for item in list(data["recent_events"])[-_MAX_EVENTS:]:
            normalized = _normalize_event(item)
            if normalized:
                events.append(normalized)
    merged["recent_events"] = events

    emotion: Dict[str, float] = {}
    if isinstance(data.get("emotion"), dict):
        for key, value in list(data["emotion"].items())[:_MAX_EMOTIONS]:
            name = _text(key, 30)
            if name:
                emotion[name] = _ratio(value)
    merged["emotion"] = emotion
    merged["last_motor_feedback"] = (
        dict(data.get("last_motor_feedback"))
        if isinstance(data.get("last_motor_feedback"), dict)
        else {}
    )
    return merged


def reset_working_session(working: Dict[str, Any]) -> Dict[str, Any]:
    """会话边界只清会话级字段；保留跨场仍有效的 open_questions/short_plan。"""
    updated = dict(working)
    updated["location"] = ""
    updated["characters"] = []
    updated["dialogue"] = []
    updated["recent_events"] = []
    updated["current_activity"] = ""
    updated["why"] = ""
    updated["emotion"] = {}
    updated["last_motor_feedback"] = {}
    return updated


def append_recent_event(
    working: Dict[str, Any], text: str, t_ms: int, kind: str = "event"
) -> Dict[str, Any]:
    """追加一条短期事件，保留最近 N 条；返回同一份 working。"""
    event = _normalize_event({"t_ms": t_ms, "kind": kind, "text": text})
    if event is None:
        return working
    events = list(working.get("recent_events") or [])
    events.append(event)
    working["recent_events"] = events[-_MAX_EVENTS:]
    return working


class WorkingMemoryStore:
    """`state/working.json` 的版本化原子存储。"""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Optional[Callable[[], int]] = None,
        logger: Any = None,
    ):
        self.path = Path(path)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger
        self.loaded_from_disk = False
        self.recovered_corrupt: Optional[Path] = None
        self.load_warning = ""

    def load(self) -> Dict[str, Any]:
        self.loaded_from_disk = False
        self.recovered_corrupt = None
        self.load_warning = ""
        if not self.path.exists():
            return default_working()
        try:
            data = read_json(self.path)
        except ValueError as exc:
            return self._recover("JSON 解析失败：{0}".format(exc))
        except OSError as exc:
            raise WorkingMemoryError("读取 working memory 失败：{0}".format(exc))
        if not isinstance(data, dict):
            return self._recover("顶层不是 JSON 对象")
        version = data.get("schema_version", 0)
        if isinstance(version, bool) or not isinstance(version, int):
            return self._recover("schema_version 不是整数")
        if version > SCHEMA_VERSION:
            raise WorkingMemorySchemaError(
                "working memory 版本 {0} 高于当前支持的 {1}：{2}".format(
                    version, SCHEMA_VERSION, self.path
                )
            )
        self.loaded_from_disk = True
        if version < SCHEMA_VERSION:
            self.load_warning = "working memory 从 v{0} 迁移到 v{1}".format(
                version, SCHEMA_VERSION
            )
        return normalize_working(data)

    def save(
        self, working: Dict[str, Any], *, now_ms: Optional[int] = None
    ) -> Path:
        with file_lock(self.path):
            if self.path.exists():
                existing = read_json(self.path)
                if not isinstance(existing, dict):
                    raise WorkingMemoryError("拒绝覆盖无效的 working 文件")
                version = existing.get("schema_version")
                if (
                    isinstance(version, bool)
                    or not isinstance(version, int)
                    or version > SCHEMA_VERSION
                ):
                    raise WorkingMemorySchemaError("拒绝覆盖不兼容的 working 文件")
            merged = normalize_working(working)
            merged["updated_at"] = utc_now_iso(
                self._clock() if now_ms is None else now_ms
            )
            atomic_write_json(self.path, merged)
        return self.path

    def _recover(self, reason: str) -> Dict[str, Any]:
        try:
            backup = backup_corrupt_file(self.path, self._clock())
        except OSError as exc:
            raise WorkingMemoryError(
                "working memory 损坏且无法备份：{0}（{1}；{2}）".format(
                    self.path, reason, exc
                )
            )
        self.recovered_corrupt = backup
        self.load_warning = "working memory 损坏（{0}），已备份到 {1}".format(
            reason, backup
        )
        if self.log is not None:
            self.log.warning(self.load_warning)
        return default_working()
