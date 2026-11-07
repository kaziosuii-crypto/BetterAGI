#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Semantic Memory（PLAYER_KNOWLEDGE）：玩家已经通过游玩解锁的设定。

世界库/攻略默认 ``world_only``，不得进入这里；只有玩家从对话、UI、画面
或自己的推理中真正获得的事实才能写入。Phase 2 只做最小事实表，belief
仍留给 Phase 5。
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
    file_lock,
    read_json,
    utc_now_iso,
)

from shadow_recorder import _sanitize

SCHEMA_VERSION = 1
VISIBILITY_PLAYER = "player_knowledge"
_FORBIDDEN_VISIBILITY = {"world_only", "ground_truth", "world_database"}
_ALLOWED_RISK = {"low", "medium", "high"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class SemanticMemoryError(ValueError):
    """事实缺少命题、可见性非法或文件版本过新。"""


class SemanticMemorySchemaError(SemanticMemoryError):
    """文件版本高于当前程序，拒绝降级覆盖。"""


def _text(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _text_list(value: Any, limit: int = 8, item_limit: int = 60) -> List[str]:
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


def _safe_fact_id(value: Any) -> str:
    return _SAFE_ID.sub("-", str(value or "").strip()).strip(".-")[:80]


def new_fact_id(now_ms: Optional[int] = None) -> str:
    moment = time.gmtime((time.time() if now_ms is None else now_ms / 1000.0))
    return "k-" + time.strftime("%Y%m%d-%H%M%S", moment) + "-" + uuid.uuid4().hex[:6]


def normalize_fact(
    data: Any,
    *,
    now_ms: Optional[int] = None,
    generate_id: bool = True,
) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise SemanticMemoryError("fact 必须是 JSON 对象")
    data = _sanitize(data)
    proposition = _text(data.get("proposition"), 500)
    if not proposition:
        raise SemanticMemoryError("fact.proposition 不能为空")
    if not _text(data.get("source"), 200):
        raise SemanticMemoryError("fact.source 必须指向已验证的来源")
    visibility = _text(data.get("visibility"), 40)
    if visibility != VISIBILITY_PLAYER:
        raise SemanticMemoryError(
            "fact 只能使用 player_knowledge，实际为 {0!r}".format(visibility)
        )
    risk = _text(data.get("spoiler_risk"), 20).lower() or "low"
    if risk not in _ALLOWED_RISK:
        risk = "low"
    learned_ms = data.get("learned_ms")
    try:
        learned_ms = max(0, int(learned_ms))
    except (TypeError, ValueError):
        learned_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    learned_at = _text(data.get("learned_at"), 40) or utc_now_iso(learned_ms)
    confidence = _ratio(data.get("confidence"), 0.7)
    fact_id = _safe_fact_id(data.get("fact_id"))
    if not fact_id:
        if not generate_id:
            raise SemanticMemoryError("fact 缺少 fact_id")
        fact_id = new_fact_id(learned_ms)
    fingerprint = _text(data.get("fingerprint"), 40)
    if not fingerprint:
        fingerprint = hashlib.sha1(
            proposition.lower().encode("utf-8")
        ).hexdigest()[:20]
    return {
        "schema_version": SCHEMA_VERSION,
        "fact_id": fact_id,
        "proposition": proposition,
        "source": _text(data.get("source"), 200),
        "evidence_id": _text(data.get("evidence_id"), 64),
        "evidence_quote": _text(data.get("evidence_quote"), 200),
        "learned_at": learned_at,
        "learned_ms": learned_ms,
        "confidence": confidence,
        "spoiler_risk": risk,
        "tags": _text_list(data.get("tags"), limit=10),
        "visibility": VISIBILITY_PLAYER,
        "fingerprint": fingerprint,
    }


class SemanticStore:
    """`memory/semantic/facts.json` 的原子存储。"""

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
        self.invalid_items = 0

    def _empty(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": "",
            "facts": [],
        }

    def load(self) -> Dict[str, Any]:
        self.loaded_from_disk = False
        self.recovered_corrupt = None
        self.load_warning = ""
        self.invalid_items = 0
        if not self.path.exists():
            return self._empty()
        try:
            data = read_json(self.path)
        except ValueError as exc:
            return self._recover("JSON 解析失败：{0}".format(exc))
        except OSError as exc:
            raise SemanticMemoryError("读取 semantic memory 失败：{0}".format(exc))
        if not isinstance(data, dict):
            return self._recover("顶层不是 JSON 对象")
        version = data.get("schema_version", 0)
        if isinstance(version, bool) or not isinstance(version, int):
            return self._recover("schema_version 不是整数")
        if version > SCHEMA_VERSION:
            raise SemanticMemorySchemaError(
                "semantic memory 版本 {0} 高于当前支持的 {1}：{2}".format(
                    version, SCHEMA_VERSION, self.path
                )
            )
        facts: List[Dict[str, Any]] = []
        self.invalid_items = 0
        raw_facts = data.get("facts", [])
        if not isinstance(raw_facts, list):
            raise SemanticMemoryError("facts 必须是数组，拒绝覆盖原文件")
        for item in raw_facts:
            try:
                fact = normalize_fact(
                    item, now_ms=self._clock(), generate_id=False
                )
            except SemanticMemoryError as exc:
                if self.log is not None:
                    self.log.warning("跳过损坏 fact：%s", exc)
                self.invalid_items += 1
                continue
            facts.append(fact)
        self.loaded_from_disk = True
        if version < SCHEMA_VERSION:
            self.load_warning = "semantic memory 从 v{0} 迁移到 v{1}".format(
                version, SCHEMA_VERSION
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _text(data.get("updated_at"), 40),
            "facts": facts,
        }

    def add(self, fact: Any, *, dedupe: bool = True) -> Tuple[Dict[str, Any], bool]:
        """添加事实；同一命题合并来源/标签/置信度，返回 (fact, 是否新建)。

        读改写全程持锁，避免两个进程各自读旧列表后互相覆盖。
        """
        if isinstance(fact, dict) and not fact.get("visibility"):
            fact = dict(fact)
            fact["visibility"] = VISIBILITY_PLAYER
        normalized = normalize_fact(
            fact, now_ms=self._clock(), generate_id=True
        )
        with file_lock(self.path):
            data = self.load()
            if self.invalid_items:
                raise SemanticMemoryError("事实文件包含无法识别的条目，拒绝覆盖原文件")
            facts = list(data.get("facts") or [])
            if dedupe:
                for index, existing in enumerate(facts):
                    if existing["fingerprint"] != normalized["fingerprint"]:
                        continue
                    merged = dict(existing)
                    merged["confidence"] = max(
                        float(existing.get("confidence") or 0.0),
                        normalized["confidence"],
                    )
                    sources = _text_list(
                        str(existing.get("source") or "").split("；")
                        + str(normalized.get("source") or "").split("；"),
                        limit=4,
                        item_limit=200,
                    )
                    merged["source"] = "；".join(sources)[:200]
                    if normalized.get("evidence_id") and normalized.get("evidence_quote"):
                        merged["source"] = normalized["source"]
                        merged["evidence_id"] = normalized["evidence_id"]
                        merged["evidence_quote"] = normalized["evidence_quote"]
                        merged["learned_ms"] = normalized["learned_ms"]
                        merged["learned_at"] = normalized["learned_at"]
                    merged["tags"] = _text_list(
                        list(existing.get("tags") or []) + normalized["tags"],
                        limit=10,
                    )
                    if not normalized.get("evidence_id") and normalized["learned_ms"] < existing["learned_ms"]:
                        merged["learned_ms"] = normalized["learned_ms"]
                        merged["learned_at"] = normalized["learned_at"]
                    facts[index] = merged
                    self._save_facts(facts)
                    return merged, False
            if any(
                item["fact_id"].casefold() == normalized["fact_id"].casefold()
                for item in facts
            ):
                raise SemanticMemoryError("fact_id 已存在，拒绝覆盖或重复")
            facts.append(normalized)
            self._save_facts(facts)
        return normalized, True

    def _save_facts(self, facts: List[Dict[str, Any]]) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(self._clock()),
            "facts": facts,
        }
        atomic_write_json(self.path, data)

    def list(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        facts = list(self.load().get("facts") or [])
        facts.sort(key=lambda item: (item["learned_ms"], item["fact_id"]), reverse=True)
        if limit is not None:
            return facts[: max(0, int(limit))]
        return facts

    def count(self) -> int:
        return len(self.load().get("facts") or [])

    def _recover(self, reason: str) -> Dict[str, Any]:
        try:
            backup = backup_corrupt_file(self.path, self._clock())
        except OSError as exc:
            raise SemanticMemoryError(
                "semantic memory 损坏且无法备份：{0}（{1}；{2}）".format(
                    self.path, reason, exc
                )
            )
        self.recovered_corrupt = backup
        self.load_warning = "semantic memory 损坏（{0}），已备份到 {1}".format(
            reason, backup
        )
        if self.log is not None:
            self.log.warning(self.load_warning)
        return self._empty()
