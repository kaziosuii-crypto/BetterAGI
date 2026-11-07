#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Episodic Memory：我经历过什么，以及我当时怎么想/怎么感觉。

每个 episode 是一个独立 JSON 文件，写入原子；文件内容是玩家的认知记录，
不是世界真相。所有 episode 默认 ``visibility=player_knowledge``。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from player_io import (
    atomic_write_json,
    backup_corrupt_file,
    clean_text,
    iso_to_epoch_ms,
    read_json,
    utc_now_iso,
)

from shadow_recorder import _sanitize

SCHEMA_VERSION = 1
VISIBILITY_PLAYER = "player_knowledge"
_FORBIDDEN_VISIBILITY = {"world_only", "ground_truth", "world_database"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_TEXT = 1200


class EpisodeError(ValueError):
    """Episode 缺少必填认知字段或包含不允许的可见性。"""


class EpisodeSchemaError(EpisodeError):
    """Episode 文件版本高于当前程序。"""


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _text_list(value: Any, limit: int = 20, item_limit: int = 200) -> List[str]:
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


def _safe_event_id(value: Any) -> str:
    text = _SAFE_ID.sub("-", str(value or "").strip()).strip(".-")
    return text[:80]


def new_episode_id(now_ms: Optional[int] = None) -> str:
    moment = time.gmtime((time.time() if now_ms is None else now_ms / 1000.0))
    return "ep-" + time.strftime("%Y%m%d-%H%M%S", moment) + "-" + uuid.uuid4().hex[:6]


def _felt_map(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    felt: Dict[str, float] = {}
    for key, item in list(value.items())[:20]:
        name = _text(key, 40)
        if name:
            felt[name] = _ratio(item)
    return felt


def episode_fingerprint(data: Dict[str, Any]) -> str:
    """稳定指纹：同一会话、地点、事件与想法视为同一条（不含时间）。"""
    parts = [
        str(data.get("source_session") or ""),
        str(data.get("location") or ""),
        str(data.get("what_happened") or ""),
        str(data.get("what_i_thought") or ""),
    ]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:20]


def normalize_episode(
    data: Any,
    *,
    now_ms: Optional[int] = None,
    generate_id: bool = True,
) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise EpisodeError("episode 必须是 JSON 对象")
    data = _sanitize(data)

    visibility = _text(data.get("visibility"), 40)
    # 读取/检索一律 fail-closed：缺失或未知可见性都不视为玩家知识。
    if visibility != VISIBILITY_PLAYER:
        raise EpisodeError(
            "episode 只能使用 player_knowledge，实际为 {0!r}".format(visibility)
        )

    what_i_knew_before = _text(data.get("what_i_knew_before"))
    what_i_thought = _text(data.get("what_i_thought"))
    what_happened = _text(data.get("what_happened"))
    felt = _felt_map(data.get("what_i_felt"))
    missing = []
    if not what_i_knew_before:
        missing.append("what_i_knew_before")
    if not what_i_thought:
        missing.append("what_i_thought")
    if not what_happened:
        missing.append("what_happened")
    if not felt:
        missing.append("what_i_felt")
    if missing:
        raise EpisodeError("episode 缺少必填字段：{0}".format("、".join(missing)))

    time_ms = data.get("time_ms")
    if time_ms is None:
        time_ms = iso_to_epoch_ms(data.get("time"))
    if time_ms is None:
        time_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        time_ms = max(0, int(time_ms))
    except (TypeError, ValueError):
        time_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    game_range = data.get("game_time_range")
    if isinstance(game_range, (list, tuple)) and len(game_range) >= 2:
        try:
            start = max(0, int(game_range[0]))
            end = max(start, int(game_range[1]))
            game_range = [start, end]
        except (TypeError, ValueError):
            game_range = None
    else:
        game_range = None

    felt_values = list(felt.values())
    emotional = data.get("emotional_intensity")
    if emotional is None:
        emotional = max(felt_values) if felt_values else 0.0

    event_id = _safe_event_id(data.get("event_id"))
    if not event_id:
        if not generate_id:
            raise EpisodeError("episode 缺少 event_id")
        event_id = new_episode_id(time_ms)
    time_text = _text(data.get("time"), 40)
    if not time_text or iso_to_epoch_ms(time_text) is None:
        time_text = utc_now_iso(time_ms)
    normalized: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "time": time_text,
        "time_ms": time_ms,
        "game_time_range": game_range,
        "location": _text(data.get("location"), 200),
        "characters": _text_list(data.get("characters"), limit=12, item_limit=60),
        "what_happened": what_happened,
        "what_i_knew_before": what_i_knew_before,
        "what_i_learned": _text(data.get("what_i_learned")),
        "what_i_thought": what_i_thought,
        "what_i_felt": felt,
        "my_reaction": _text(data.get("my_reaction"), 600),
        "proposed_intention": _text(data.get("proposed_intention"), 240),
        "choices_i_made": _text_list(
            data.get("choices_i_made"), limit=10, item_limit=200
        ),
        "unresolved_questions": _text_list(
            data.get("unresolved_questions"), limit=10, item_limit=200
        ),
        "importance": _ratio(data.get("importance"), 0.5),
        "emotional_intensity": _ratio(emotional, 0.0),
        "related_memories": _text_list(
            data.get("related_memories"), limit=12, item_limit=80
        ),
        "source_session": _text(data.get("source_session"), 120),
        "visibility": VISIBILITY_PLAYER,
    }
    normalized["fingerprint"] = _text(data.get("fingerprint"), 40) or episode_fingerprint(
        normalized
    )
    return normalized


class EpisodicStore:
    """`memory/episodes/*.json` 的目录存储。"""

    def __init__(
        self,
        directory: Path | str,
        *,
        clock: Optional[Callable[[], int]] = None,
        logger: Any = None,
        dedupe_window_ms: int = 600000,
    ):
        self.directory = Path(directory)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger
        self.dedupe_window_ms = max(0, int(dedupe_window_ms))

    def add(
        self, episode: Any, *, dedupe: bool = True
    ) -> Tuple[Dict[str, Any], bool]:
        """写入 episode；按 fingerprint 去重，返回 (episode, 是否新建)。"""
        if isinstance(episode, dict) and not episode.get("visibility"):
            episode = dict(episode)
            episode["visibility"] = VISIBILITY_PLAYER
        normalized = normalize_episode(
            episode, now_ms=self._clock(), generate_id=True
        )
        if dedupe:
            for existing in self.list():
                if existing.get("fingerprint") != normalized["fingerprint"]:
                    continue
                if (
                    self.dedupe_window_ms
                    and abs(
                        int(existing.get("time_ms") or 0)
                        - int(normalized.get("time_ms") or 0)
                    )
                    > self.dedupe_window_ms
                ):
                    continue
                return existing, False
        path = self.path_for(normalized["event_id"])
        if path.exists():
            raise EpisodeError("episode ID 已存在，拒绝覆盖：{0}".format(normalized["event_id"]))
        atomic_write_json(path, normalized)
        return normalized, True

    def path_for(self, event_id: str) -> Path:
        safe = _safe_event_id(event_id) or new_episode_id(self._clock())
        return self.directory / (safe + ".json")

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(event_id)
        if not path.exists():
            return None
        try:
            data = read_json(path)
        except (OSError, ValueError):
            return None
        try:
            if not isinstance(data, dict):
                return None
            version = data.get("schema_version")
            if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
                return None
            return normalize_episode(
                data, now_ms=self._clock(), generate_id=False
            )
        except EpisodeError:
            return None

    def list(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """按时间倒序返回所有可读 episode；损坏/过新文件跳过并告警。"""
        if not self.directory.exists():
            return []
        episodes: List[Dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = read_json(path)
                if not isinstance(data, dict):
                    raise EpisodeError("episode 顶层必须是对象")
                version = data.get("schema_version")
                if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
                    raise EpisodeError("不支持的 episode schema_version")
                episodes.append(
                    normalize_episode(
                        data, now_ms=self._clock(), generate_id=False
                    )
                )
            except (OSError, ValueError) as exc:
                if self.log is not None:
                    self.log.warning("跳过损坏的 episode %s：%s", path, exc)
        episodes.sort(key=lambda item: (item["time_ms"], item["event_id"]), reverse=True)
        if limit is not None:
            return episodes[: max(0, int(limit))]
        return episodes

    def count(self) -> int:
        return len(list(self.directory.glob("*.json"))) if self.directory.exists() else 0

    def backup_corrupt(self, event_id: str) -> Optional[Path]:
        path = self.path_for(event_id)
        if not path.exists():
            return None
        return backup_corrupt_file(path, self._clock())
