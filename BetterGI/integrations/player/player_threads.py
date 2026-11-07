#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Narrative Threads：未解决的线索、伏笔与当前理论。"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import atomic_write_json, backup_corrupt_file, clean_text, read_json, utc_now_iso

SCHEMA_VERSION = 1
STATUSES = {"open", "dormant", "resolved", "abandoned"}
_EVENT_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,79}\Z")
_GENERIC_MATCHES = {"任务", "线索", "事件", "角色", "地点", "故事", "秘密", "发现", "调查", "the", "and", "quest", "story", "event", "character", "clue", "secret", "mission", "investigation"}
_GENERIC_CJK_PARTS = re.compile(r"(?:当前|新的|新|旧的|旧|相关|重要|未知|某个|一个|一个新的|神秘的|神秘|这个|那个|任务|线索|事件|角色|地点|故事|秘密|发现|调查|的)+\Z")
_GENERIC_ASCII_WORDS = _GENERIC_MATCHES | {"a", "an", "current", "new", "old", "important", "unknown", "mysterious", "related", "this", "that", "of", "in", "about"}
_MAX_EVENT_ACTIVATIONS = 8


class ThreadError(ValueError):
    """线程状态非法。"""


def _text(value: Any, limit: int = 300) -> str:
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


def _fingerprint(title: str) -> str:
    return hashlib.sha1(title.lower().encode("utf-8")).hexdigest()[:20]


def _specific(value: Any, *, topic: bool = False) -> str:
    """仅使用完整的非通用实体或主题，不用单字、二元组或通用词。"""
    text = _text(value, 120).lower()
    if not text or text in _GENERIC_MATCHES:
        return ""
    compact = re.sub(r"[\W_]+", "", text, flags=re.UNICODE)
    words = re.findall(r"[a-z0-9]+", text)
    if _GENERIC_CJK_PARTS.fullmatch(compact) or (not re.search(r"[一-鿿]", compact) and words and all(word in _GENERIC_ASCII_WORDS for word in words)):
        return ""
    minimum = (4 if topic else 2) if re.search(r"[一-鿿]", compact) else (5 if topic else 3)
    return text if len(compact) >= minimum else ""


def _contains_topic(text: str, topic: str) -> bool:
    """英文主题要求词边界，避免在其他词内部误命中。"""
    if not topic:
        return False
    pattern = re.escape(topic)
    if topic[0].isascii() and topic[0].isalnum():
        pattern = r"(?<![a-z0-9_])" + pattern
    if topic[-1].isascii() and topic[-1].isalnum():
        pattern += r"(?![a-z0-9_])"
    return bool(re.search(pattern, text))


def _record_status(thread: Dict[str, Any], old: str, new: str, now: int, **details: Any) -> None:
    """追加状态变化，不重写已有历史。"""
    history = thread.get("status_history")
    if not isinstance(history, list):
        history = []
    history.append({"from_status": old, "to_status": new, "time_ms": now, **details})
    thread["status_history"] = history


def normalize_thread(data: Any, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ThreadError("thread 必须是 JSON 对象")
    title = _text(data.get("title") or data.get("thread"), 240)
    if not title:
        raise ThreadError("thread.title 不能为空")
    status = _text(data.get("status"), 20).lower() or "open"
    if status not in STATUSES:
        status = "open"
    moment = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        importance = min(1.0, max(0.0, float(data.get("importance", 0.5))))
    except (TypeError, ValueError):
        importance = 0.5
    # 保留既有扩展字段与历史，避免加载后再保存时静默丢失。
    normalized = dict(data)
    normalized.update({
        "schema_version": SCHEMA_VERSION,
        "thread_id": _text(data.get("thread_id"), 80) or "th-" + _fingerprint(title),
        "title": title,
        "status": status,
        "importance": importance,
        "characters": _list(data.get("characters"), limit=8, item_limit=60),
        "topics": _list(data.get("topics"), limit=8, item_limit=120),
        "evidence": _list(data.get("evidence"), limit=10),
        "current_theory": _text(data.get("current_theory"), 400),
        "first_seen_ms": int(data.get("first_seen_ms") if data.get("first_seen_ms") is not None else moment),
        "last_referenced_ms": int(data.get("last_referenced_ms") if data.get("last_referenced_ms") is not None else moment),
        "fingerprint": _text(data.get("fingerprint"), 40) or _fingerprint(title),
    })
    return normalized


class ThreadStore:
    """`state/narrative_threads.json` 的原子存储。"""

    def __init__(self, path: Path | str, *, clock: Optional[Callable[[], int]] = None, logger: Any = None):
        self.path = Path(path)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger

    def _empty(self) -> Dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "updated_at": "", "threads": []}

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            self._recover()
            return self._empty()
        if not isinstance(data, dict):
            self._recover()
            return self._empty()
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            self._recover()
            return self._empty()
        if version > SCHEMA_VERSION:
            raise ThreadError("thread 版本过新：{0}".format(version))
        threads = []
        for item in data.get("threads") or []:
            try:
                threads.append(normalize_thread(item, now_ms=self._clock()))
            except ThreadError:
                continue
        result = dict(data)
        result.update({"schema_version": SCHEMA_VERSION, "updated_at": _text(data.get("updated_at"), 40), "threads": threads})
        return result

    def apply(self, updates: Any) -> List[Dict[str, Any]]:
        if not isinstance(updates, (list, tuple)):
            return []
        data = self.load()
        threads = list(data.get("threads") or [])
        changed: List[Dict[str, Any]] = []
        for update in updates[:8]:
            try:
                normalized = normalize_thread(update, now_ms=self._clock())
            except ThreadError:
                continue
            normalized["last_referenced_ms"] = self._clock()
            existing = next(
                (item for item in threads if item["fingerprint"] == normalized["fingerprint"]),
                None,
            )
            if existing is None:
                threads.append(normalized)
                changed.append(normalized)
                continue
            if "status" in update and existing["status"] != normalized["status"]:
                _record_status(existing, existing["status"], normalized["status"], normalized["last_referenced_ms"], reason="manual_update")
                existing["status"] = normalized["status"]
            if "importance" in update:
                existing["importance"] = normalized["importance"]
            if "current_theory" in update:
                existing["current_theory"] = normalized["current_theory"]
            existing["characters"] = _list(
                list(existing.get("characters") or []) + normalized["characters"], limit=8, item_limit=60
            )
            existing["topics"] = _list(
                list(existing.get("topics") or []) + normalized["topics"], limit=8, item_limit=120
            )
            existing["evidence"] = _list(
                list(existing.get("evidence") or []) + normalized["evidence"], limit=10
            )
            existing["last_referenced_ms"] = normalized["last_referenced_ms"]
            changed.append(existing)
        if changed:
            self._save(threads)
        return changed

    def activate(self, thread_ids: Any, *, now_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        """检索命中线索时更新 last_referenced，避免伏笔被遗忘。"""
        if not isinstance(thread_ids, (list, tuple)):
            return []
        now = int(now_ms if now_ms is not None else self._clock())
        data = self.load()
        threads = list(data.get("threads") or [])
        changed = []
        for thread in threads:
            if thread.get("thread_id") in thread_ids and thread.get("status") == "dormant":
                _record_status(thread, "dormant", "open", now, reason="manual_activation")
                thread["status"] = "open"
                thread["last_referenced_ms"] = now
                changed.append(thread)
        if changed:
            self._save(threads)
        return changed

    def activate_from_event(
        self, event_id: str, *, episodic_store: Any, now_ms: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """按已保存的玩家可见 episode 激活最多八条休眠线索。

        调用方须在确认真实观察、保存 episode 后传入其原始 ID；此方法不证明
        观察本身的真实性，也不接收世界知识或自由生成的事件文本。只读取
        what_happened 和明确的 characters，不使用猜测、理论或语义相似度。
        同一事件成功处理后不会再次产生变化，包括线索后来重新进入休眠。
        """
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            return []
        event = episodic_store.get(event_id)
        if not isinstance(event, dict) or event.get("event_id") != event_id or event.get("visibility") != "player_knowledge":
            return []
        happened = _text(event.get("what_happened"), 1200).lower()
        try:
            event_ms = int(event.get("time_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            return []
        if not happened or event_ms <= 0:
            return []
        entities = {_specific(name) for name in _list(event.get("characters"), limit=12, item_limit=60)} - {""}
        data = self.load()
        processed = data.get("activation_event_ids")
        processed = processed if isinstance(processed, list) else []
        if event_id in processed:
            return []
        now = int(now_ms if now_ms is not None else self._clock())
        if event_ms > now:
            return []
        changed = []
        for thread in data["threads"]:
            if thread.get("status") != "dormant" or event_ms <= int(thread.get("last_referenced_ms") or 0):
                continue
            matches = sorted(entities & {_specific(name) for name in thread.get("characters", [])})
            topics = [_specific(value, topic=True) for value in [thread["title"], *thread.get("topics", [])]]
            matched_topic = next((topic for topic in topics if _contains_topic(happened, topic)), "")
            if not matches and not matched_topic:
                continue
            reason = "entity:" + matches[0] if matches else "topic:" + matched_topic
            _record_status(thread, "dormant", "open", now, reason=reason, event_id=event_id)
            thread["status"] = "open"
            thread["last_referenced_ms"] = now
            thread["last_activation"] = {"event_id": event_id, "reason": reason, "time_ms": now}
            changed.append(thread)
            if len(changed) >= _MAX_EVENT_ACTIVATIONS:
                break
        if changed:
            data["activation_event_ids"] = processed + [event_id]
            self._save(data["threads"], data=data)
        return changed

    def _save(self, threads: List[Dict[str, Any]], *, data: Optional[Dict[str, Any]] = None) -> None:
        """保留顶层扩展字段，原子写入线索与事件去重记录。"""
        payload = dict(self.load() if data is None else data)
        payload.update({
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(self._clock()),
            "threads": threads,
        })
        atomic_write_json(self.path, payload)

    def list(self, *, status: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        threads = self.load().get("threads") or []
        if status:
            threads = [item for item in threads if item.get("status") == status]
        threads.sort(key=lambda item: int(item.get("last_referenced_ms") or 0), reverse=True)
        return threads[:limit] if limit is not None else threads

    def summary(self, limit: int = 5) -> List[str]:
        lines = []
        for item in self.list(limit=limit):
            if item.get("status") in ("open", "dormant"):
                text = "我还惦记着：{0}".format(item["title"])
                if item.get("current_theory"):
                    text += "（我猜：{0}）".format(item["current_theory"])
                lines.append(text)
        return lines

    def _recover(self) -> None:
        try:
            backup_corrupt_file(self.path, self._clock())
        except OSError:
            pass
