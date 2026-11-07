#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Relationship Store：玩家对每个角色的连续态度。

数值只在内部用于更新；进入 prompt 的是自然语言摘要，不暴露裸分数。
单次事件对每个维度最多变化 0.15，避免一次剧情把关系拉满。
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import atomic_write_json, backup_corrupt_file, clean_text, read_json, utc_now_iso

SCHEMA_VERSION = 1
DIMENSIONS = ("familiarity", "trust", "affection", "suspicion", "respect", "concern")
MAX_DELTA = 0.15
# 保留 Unicode 字母和数字，避免中文名字都退化为同一个 ID。
_SAFE = re.compile(r"[^\w.-]+", re.UNICODE)


class RelationshipError(ValueError):
    """关系状态非法。"""


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


def _ratio(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _identity(name: str) -> str:
    """以 Unicode NFKC 规范化比较角色名称，不区分兼容字符形式。"""
    return unicodedata.normalize("NFKC", name)


def _hashed_id(name: str, suffix: str = "") -> str:
    """为有损清洗或 ID 冲突的名称生成稳定 ID。"""
    identity = _identity(name)
    slug = _SAFE.sub("-", identity).strip(".-") or "character"
    digest = hashlib.sha256((identity + suffix).encode("utf-8")).hexdigest()[:16]
    return slug[:43] + "-" + digest


def _character_id(name: str) -> str:
    """无损、安全的名称沿用原 ID；其他名称附加稳定哈希。"""
    identity = _identity(name)
    slug = _SAFE.sub("-", identity).strip(".-")
    if slug == identity and len(slug) <= 60 and slug != "unknown":
        return slug
    return _hashed_id(name)


def _available_id(name: str, preferred: str, used: set[str]) -> str:
    """冲突时使用稳定哈希及必要的序号，永不复用已有 ID。"""
    if preferred not in used:
        return preferred
    candidate = _hashed_id(name)
    index = 0
    while candidate in used:
        index += 1
        candidate = _hashed_id(name, "\0" + str(index))
    return candidate


def _stored_id(value: Any) -> str:
    """保留安全的历史 ID；旧版 unknown 不代表有效身份。"""
    if not isinstance(value, str) or value == "unknown" or not value or len(value) > 60:
        return ""
    if _SAFE.search(value) or value.strip(".-") != value:
        return ""
    return value


def normalize_relationship(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise RelationshipError("relationship 必须是 JSON 对象")
    # 名称不得截断后用于身份比较，否则不同长名称可能合并。
    raw_character = data.get("character") or data.get("name")
    character = _text(raw_character, len(raw_character) if isinstance(raw_character, str) else 0)
    if not character:
        raise RelationshipError("relationship.character 不能为空")
    dims = {}
    for key in DIMENSIONS:
        dims[key] = _ratio(data.get(key), 0.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "character": character,
        "character_id": _character_id(character),
        **dims,
        "memorable_events": _list(data.get("memorable_events"), limit=12, item_limit=240),
        "impressions": _list(data.get("impressions"), limit=10),
        "unresolved": _list(data.get("unresolved")),
        "last_updated_ms": int(data.get("last_updated_ms") if data.get("last_updated_ms") is not None else time.time() * 1000),
    }


def _summary_text(record: Dict[str, Any]) -> str:
    """把数值转成自然语言，绝不把 0.62 写进 prompt。"""
    parts: List[str] = []
    trust = record.get("trust", 0.0)
    affection = record.get("affection", 0.0)
    suspicion = record.get("suspicion", 0.0)
    respect = record.get("respect", 0.0)
    if suspicion >= 0.6:
        parts.append("我对 TA 仍有些戒备")
    elif trust >= 0.6:
        parts.append("我比较信任 TA")
    elif trust >= 0.3:
        parts.append("我对 TA 还算放心")
    else:
        parts.append("我和 TA 还不熟")
    if affection >= 0.6:
        parts.append("我挺喜欢和 TA 相处")
    if respect >= 0.6:
        parts.append("我尊重 TA 的判断")
    if record.get("concern", 0.0) >= 0.5:
        parts.append("我有点担心 TA")
    return "；".join(parts)


class RelationshipStore:
    """`state/relationships.json` 的原子存储。"""

    def __init__(self, path: Path | str, *, clock: Optional[Callable[[], int]] = None, logger: Any = None):
        self.path = Path(path)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger

    def _empty(self) -> Dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "updated_at": "", "relationships": []}

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
            raise RelationshipError("relationship 版本过新：{0}".format(version))
        records = []
        saved_ids = []
        for item in data.get("relationships") or []:
            try:
                records.append(normalize_relationship(item))
                saved_ids.append(_stored_id(item.get("character_id")))
            except RelationshipError:
                continue
        # 先保留各个不冲突的旧 ID，迁移 unknown 或重复 ID 时不得抢占它们。
        reserved = set(saved_ids) - {""}
        used: set[str] = set()
        for record, saved_id in zip(records, saved_ids):
            if saved_id and saved_id not in used:
                record["character_id"] = saved_id
            else:
                record["character_id"] = _available_id(
                    record["character"], record["character_id"], used | reserved
                )
            used.add(record["character_id"])
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _text(data.get("updated_at"), 40),
            "relationships": records,
        }

    def apply(self, updates: Any) -> List[Dict[str, Any]]:
        if not isinstance(updates, (list, tuple)):
            return []
        data = self.load()
        records = list(data.get("relationships") or [])
        changed: List[Dict[str, Any]] = []
        for update in updates[:8]:
            if not isinstance(update, dict):
                continue
            try:
                base = normalize_relationship(update)
            except RelationshipError:
                continue
            base["last_updated_ms"] = self._clock()
            # 用规范化名称匹配，不能仅凭清洗后的 ID 合并不同角色。
            existing = next(
                (item for item in records if _identity(item["character"]) == _identity(base["character"])),
                None,
            )
            if existing is None:
                base["character_id"] = _available_id(
                    base["character"], base["character_id"],
                    {item["character_id"] for item in records},
                )
                # 新角色从零开始，也受单次事件的变化限幅约束。
                for key in DIMENSIONS:
                    base[key] = round(min(MAX_DELTA, base[key]), 4)
                records.append(base)
                changed.append(base)
                continue
            for key in DIMENSIONS:
                if key not in update:
                    continue
                target = base[key]
                current = float(existing.get(key) or 0.0)
                delta = max(-MAX_DELTA, min(MAX_DELTA, target - current))
                existing[key] = round(min(1.0, max(0.0, current + delta)), 4)
            existing["memorable_events"] = _list(
                list(existing.get("memorable_events") or []) + base["memorable_events"],
                limit=12,
                item_limit=240,
            )
            existing["impressions"] = _list(
                list(existing.get("impressions") or []) + base["impressions"],
                limit=10,
            )
            existing["unresolved"] = _list(
                list(existing.get("unresolved") or []) + base["unresolved"],
                limit=8,
            )
            existing["last_updated_ms"] = base["last_updated_ms"]
            changed.append(existing)
        if changed:
            self._save(records)
        return changed

    def _save(self, records: List[Dict[str, Any]]) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_now_iso(self._clock()),
                "relationships": records,
            },
        )

    def list(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        records = self.load().get("relationships") or []
        records.sort(key=lambda item: int(item.get("last_updated_ms") or 0), reverse=True)
        return records[:limit] if limit is not None else records

    def summary(self, limit: int = 5) -> List[str]:
        return [
            "对{0}：{1}。".format(item["character"], _summary_text(item))
            for item in self.list(limit=limit)
        ]

    def _recover(self) -> None:
        try:
            backup_corrupt_file(self.path, self._clock())
        except OSError:
            pass
