#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MemoryConsolidator（Phase 2 骨架）。

把一轮 proposal/snapshot 变成：
- 更新后的 working memory；
- 一条结构化 episode（满足显著性与必填认知字段时）；
- 从 episode 中抽取的 PLAYER_KNOWLEDGE facts。

旧的 resume ``memory_note`` 双写仍由 PlayerAgent._apply_proposal 完成。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from player_episodic_memory import (
    EpisodeError,
    EpisodicStore,
    VISIBILITY_PLAYER,
    new_episode_id,
)
from player_io import clean_text, utc_now_iso
from player_semantic_memory import SemanticStore
from player_working_memory import (
    WorkingMemoryStore,
    WorkingMemoryError,
    append_recent_event,
    reset_working_session,
)
from shadow_recorder import _sanitize


def _text(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _text_list(value: Any, limit: int = 10, item_limit: int = 160) -> List[str]:
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


def _felt(value: Any) -> Dict[str, float]:
    if not isinstance(value, dict):
        return {}
    felt: Dict[str, float] = {}
    for key, item in list(value.items())[:12]:
        name = _text(key, 40)
        if not name:
            continue
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        felt[name] = min(1.0, max(0.0, number))
    return felt


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


@dataclass
class ConsolidationResult:
    working: Dict[str, Any]
    episode: Optional[Dict[str, Any]] = None
    episode_created: bool = False
    facts: List[Dict[str, Any]] = field(default_factory=list)
    facts_created: int = 0
    importance: float = 0.0
    error: str = ""
    state_updates: Dict[str, Any] = field(default_factory=dict)

    def record(self) -> Dict[str, Any]:
        return {
            "episode_id": (self.episode or {}).get("event_id", ""),
            "episode_created": self.episode_created,
            "facts": [fact.get("fact_id", "") for fact in self.facts],
            "facts_created": self.facts_created,
            "importance": round(self.importance, 3),
            "error": self.error,
            "state_updates": dict(self.state_updates),
        }


class MemoryConsolidator:
    """把一轮认知写入 working/episodic/semantic；失败不阻断 shadow。"""

    def __init__(
        self,
        working_store: WorkingMemoryStore,
        episodic_store: EpisodicStore,
        semantic_store: SemanticStore,
        *,
        session_id: str = "vtuber",
        clock: Optional[Callable[[], int]] = None,
        logger: Any = None,
        episode_threshold: float = 0.5,
        min_episode_interval_ms: int = 60000,
        belief_store: Any = None,
        relationship_store: Any = None,
        emotion_store: Any = None,
        thread_store: Any = None,
    ):
        self.working_store = working_store
        self.episodic_store = episodic_store
        self.semantic_store = semantic_store
        self.belief_store = belief_store
        self.relationship_store = relationship_store
        self.emotion_store = emotion_store
        self.thread_store = thread_store
        self.session_id = session_id
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger
        self.episode_threshold = _clamp(episode_threshold)
        self.min_episode_interval_ms = max(0, int(min_episode_interval_ms))
        self._working = working_store.load()
        self._last_episode_ms = 0

    @property
    def working(self) -> Dict[str, Any]:
        return dict(self._working)

    def reset_session(self, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """新 live 会话/重连时清空会话级 working，保留长期意图与疑问。"""
        self._working = reset_working_session(self._working)
        self._working["updated_at"] = utc_now_iso(self._clock() if now_ms is None else now_ms)
        self._last_episode_ms = 0
        self.save(now_ms=now_ms)
        return dict(self._working)

    def consolidate(
        self,
        proposal: Dict[str, Any],
        snapshot: Dict[str, Any],
        state: Dict[str, Any],
        *,
        now_ms: Optional[int] = None,
    ) -> ConsolidationResult:
        now = self._clock() if now_ms is None else int(now_ms)
        proposal = _sanitize(proposal) if isinstance(proposal, dict) else {}
        snapshot = _sanitize(snapshot) if isinstance(snapshot, dict) else {}
        state = _sanitize(state) if isinstance(state, dict) else {}
        note = proposal.get("memory_note")
        note = note if isinstance(note, dict) else {}
        intention = proposal.get("intention")
        intention = intention if isinstance(intention, dict) else {}
        speech = proposal.get("speech")
        speech = speech if isinstance(speech, dict) else {}

        working = self._update_working(self._working, proposal, note, intention, snapshot, state, now)
        working["updated_at"] = utc_now_iso(now)
        result = ConsolidationResult(working=dict(working))

        prior_working = self._working
        try:
            self.working_store.save(working, now_ms=now)
        except (OSError, WorkingMemoryError) as exc:
            if self.log is not None:
                self.log.warning("保存 working memory 失败：%s", exc)
            result.error = "working_save_failed"

        felt = _felt(note.get("what_i_felt"))
        if not felt and speech.get("emotion"):
            felt = {_text(speech.get("emotion"), 30): _clamp(speech.get("intensity") or 0.5)}
        thought = _text(proposal.get("thought"), 800)
        what_happened = _text(
            note.get("what_happened") or proposal.get("observation"), 800
        )
        # 没有独立观察证据时，模型自述不构成已学事实。
        learned = ""

        characters = _text_list(
            note.get("characters") or working.get("characters"),
            limit=12,
            item_limit=60,
        )
        location = _text(
            note.get("scene") or working.get("location") or state.get("last_location"),
            200,
        )
        why = _text(
            note.get("what_i_knew_before")
            or prior_working.get("why")
            or "之前还没想清楚这件事",
            600,
        )
        unresolved = _text_list(
            note.get("things_to_check") or working.get("open_questions"),
            limit=10,
            item_limit=200,
        )
        importance = self._importance(
            what_happened, learned, characters, felt, unresolved, note.get("importance")
        )
        result.importance = importance

        can_episode = bool(thought and what_happened and felt)
        interval_ok = (
            now - self._last_episode_ms >= self.min_episode_interval_ms
            or importance >= 0.75
        )
        if can_episode and interval_ok and importance >= self.episode_threshold:
            episode_data = {
                "event_id": new_episode_id(now),
                "time": utc_now_iso(now),
                "time_ms": now,
                "game_time_range": None,
                "location": location,
                "characters": characters,
                "what_happened": what_happened,
                "what_i_knew_before": why,
                "what_i_learned": learned,
                "what_i_thought": thought,
                "what_i_felt": felt,
                "my_reaction": _text(speech.get("text"), 600),
                "choices_i_made": [],
                "proposed_intention": _text(intention.get("goal"), 240),
                "unresolved_questions": unresolved,
                "importance": importance,
                "emotional_intensity": max(felt.values()) if felt else 0.0,
                "related_memories": [],
                "source_session": self.session_id,
                "visibility": VISIBILITY_PLAYER,
            }
            try:
                episode, created = self.episodic_store.add(episode_data)
                result.episode = episode
                result.episode_created = created
                if created:
                    self._last_episode_ms = now
                # 模型输出没有独立观察证据，不能自动晋升为已解锁事实。
                result.facts = []
                result.facts_created = 0
            except (EpisodeError, OSError) as exc:
                result.error = "episode_failed: {0}".format(exc)
                if self.log is not None:
                    self.log.warning("写入 episode 失败：%s", exc)

        # learned_facts 保留在 shadow proposal 中，等待有证据的知识解锁流程。
        # 它们不会在当前阶段直接进入 semantic store。

        self._working = working
        result.state_updates = self._apply_state_updates(proposal, now)
        return result

    def _apply_state_updates(
        self, proposal: Dict[str, Any], now: int
    ) -> Dict[str, Any]:
        """belief/relationship/emotion/thread 更新；失败只记录，不影响旧链路。"""
        updates: Dict[str, Any] = {}
        try:
            if self.belief_store is not None and proposal.get("beliefs"):
                changed = self.belief_store.apply(proposal.get("beliefs"))
                updates["beliefs"] = [item.get("belief_id") for item in changed]
        except Exception as exc:  # noqa: BLE001
            updates["belief_error"] = str(exc)[:200]
        try:
            if self.relationship_store is not None and proposal.get("relationships"):
                changed = self.relationship_store.apply(proposal.get("relationships"))
                updates["relationships"] = [item.get("character") for item in changed]
        except Exception as exc:  # noqa: BLE001
            updates["relationship_error"] = str(exc)[:200]
        try:
            if self.emotion_store is not None and proposal.get("emotion"):
                self.emotion_store.apply(proposal.get("emotion"), now_ms=now)
                updates["emotion"] = True
        except Exception as exc:  # noqa: BLE001
            updates["emotion_error"] = str(exc)[:200]
        try:
            if self.thread_store is not None and proposal.get("threads"):
                changed = self.thread_store.apply(proposal.get("threads"))
                updates["threads"] = [item.get("thread_id") for item in changed]
        except Exception as exc:  # noqa: BLE001
            updates["thread_error"] = str(exc)[:200]
        return updates

    def save(self, *, now_ms: Optional[int] = None) -> bool:
        try:
            self.working_store.save(
                self._working,
                now_ms=self._clock() if now_ms is None else now_ms,
            )
            return True
        except (OSError, WorkingMemoryError) as exc:
            if self.log is not None:
                self.log.warning("保存 working memory 失败：%s", exc)
            return False

    @staticmethod
    def _normalize_learned_facts(value: Any) -> List[Dict[str, Any]]:
        """规范化模型给出的事实候选；这些只是候选，不会自动进入已知事实。"""
        if not isinstance(value, (list, tuple)):
            return []
        facts: List[Dict[str, Any]] = []
        for item in value[:6]:
            if isinstance(item, str):
                proposition = _text(item, 500)
                confidence = 0.6
                tags: List[str] = []
            elif isinstance(item, dict):
                proposition = _text(item.get("proposition") or item.get("text"), 500)
                try:
                    confidence = _clamp(item.get("confidence") or 0.6)
                except (TypeError, ValueError):
                    confidence = 0.6
                tags = _text_list(item.get("tags"), limit=6, item_limit=40)
            else:
                continue
            if proposition:
                facts.append(
                    {
                        "proposition": proposition,
                        "confidence": confidence,
                        "tags": tags,
                    }
                )
        return facts

    @staticmethod
    def _importance(
        what_happened: str,
        learned: str,
        characters: Iterable[str],
        felt: Dict[str, float],
        unresolved: Iterable[str],
        explicit: Any,
    ) -> float:
        if explicit is not None:
            try:
                return _clamp(float(explicit))
            except (TypeError, ValueError):
                pass
        score = 0.25
        if what_happened:
            score += 0.20
        if learned:
            score += 0.15
        if list(characters):
            score += 0.10
        if felt:
            score += 0.15 * max(felt.values())
        if list(unresolved):
            score += 0.05
        return _clamp(score)

    @staticmethod
    def _update_working(
        working: Dict[str, Any],
        proposal: Dict[str, Any],
        note: Dict[str, Any],
        intention: Dict[str, Any],
        snapshot: Dict[str, Any],
        state: Dict[str, Any],
        now_ms: int,
    ) -> Dict[str, Any]:
        updated = dict(working)
        if note.get("scene"):
            updated["location"] = _text(note.get("scene"), 200)
        characters = list(updated.get("characters") or [])
        for name in _text_list(note.get("characters"), limit=12, item_limit=60):
            if name not in characters:
                characters.append(name)
        updated["characters"] = characters[:12]
        goal = _text(intention.get("goal"), 240)
        if goal:
            updated["current_activity"] = goal
            updated["short_plan"] = [goal]
        if intention.get("reason"):
            updated["why"] = _text(intention["reason"], 300)
        checks = _text_list(note.get("things_to_check"), limit=8, item_limit=160)
        if checks:
            updated["open_questions"] = checks
        felt = _felt(note.get("what_i_felt"))
        speech = proposal.get("speech") if isinstance(proposal.get("speech"), dict) else {}
        if not felt and speech.get("emotion"):
            felt = {
                _text(speech.get("emotion"), 30): _clamp(speech.get("intensity") or 0.5)
            }
        if felt:
            updated["emotion"] = felt
        action_state = snapshot.get("action_state")
        if isinstance(action_state, dict):
            updated["last_motor_feedback"] = dict(action_state)
        event_text = _text(
            proposal.get("observation")
            or note.get("what_happened")
            or proposal.get("thought"),
            240,
        )
        if event_text:
            append_recent_event(updated, event_text, now_ms, "observation")
        return updated
