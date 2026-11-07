#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地结构化记忆检索。

Phase 2 的默认后端是本地 JSON（episodes + semantic facts），不依赖向量库。
OpenViking 只是可选的 ``external`` 检索器：调用方必须自己确保它只暴露
PLAYER_KNOWLEDGE，当前 lore 库含 world_only 内容，因此默认不接线。
"""

from __future__ import annotations

import asyncio
import itertools
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

from player_embedding import EmbeddingConfig, EmbeddingSession
from player_episodic_memory import EpisodicStore, VISIBILITY_PLAYER as EP_VISIBILITY
from player_io import clean_text
from player_semantic_memory import SemanticStore, VISIBILITY_PLAYER as FACT_VISIBILITY

_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]+")

DEFAULT_WEIGHTS = {
    "semantic": 0.30,
    "importance": 0.20,
    "emotional": 0.15,
    "character": 0.10,
    "thread": 0.15,
    "recency": 0.10,
}
_FORBIDDEN_VISIBILITY = {"world_only", "ground_truth", "world_database"}
# 认知信息只改变注意力，不是事实证明；总增益不得超过 0.2。
_COGNITIVE_WEIGHTS = {"relationship": 0.05, "belief": 0.07, "emotion": 0.04, "thread": 0.04}
_MAX_EXPERIENCE_CANDIDATES = 512
_EXPERIENCE_LEXICAL_THRESHOLD = 0.05


class SemanticSimilarityAdapter(Protocol):
    """旧的预留接口；检索不调用 score，实际接线使用 EmbeddingConfig。"""

    def score(self, query: str, document: str) -> float:
        """返回有限的 [0,1] 相似度；实现方负责模型与数据安全。"""
        ...


@dataclass(frozen=True)
class BeliefLinks:
    """调用方选出的相关信念及其明确的记忆来源关联，不推断证据真假。

    来源使用 ``episode:<event_id>`` 或 ``fact:<fact_id>``，不是自由文本；
    支持与反证同等获得关注，disproven 不会被当作已证实的信念。
    """

    belief_id: str
    support_sources: Sequence[str] = ()
    counter_sources: Sequence[str] = ()
    relevance: float = 1.0


@dataclass(frozen=True)
class ThreadAttention:
    """调用方选出的活动线程；标题只做词面匹配，来源关联必须显式提供。"""

    thread_id: str
    title: str = ""
    memory_sources: Sequence[str] = ()
    relevance: float = 1.0


def thread_attention_from_records(records: Iterable[Mapping[str, Any]]) -> List[ThreadAttention]:
    """把调用方选出的 open 线索转成召回注意力，关联其激活事件。

    此助手不获取或授权记忆；episode 可见性和 semantic 证据门仍由检索器
    分别执行。只接受明确的 episode ID，不把自由文本 evidence 当作来源。
    """
    result: List[ThreadAttention] = []
    for item in records:
        if not isinstance(item, Mapping) or item.get("status") != "open":
            continue
        thread_id = item.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            continue
        activation = item.get("last_activation")
        event_id = activation.get("event_id") if isinstance(activation, Mapping) else None
        sources = ["episode:" + event_id] if isinstance(event_id, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,79}", event_id) else []
        result.append(ThreadAttention(thread_id=thread_id, title=str(item.get("title") or ""), memory_sources=sources))
        if len(result) >= 8:
            break
    return result


@dataclass(frozen=True)
class CognitiveContext:
    """一次召回的可选注意力输入；不读取 store，不提供知识授权。

    relationship_weights 是角色名到 [0,1] 注意力权重，不是信任概率；
    emotion 是情绪维度到 [0,1] 当前值。信念和线程由调用方先筛为当前相关项，
    不应将 store 中自由文本 evidence/contradictions 直接当作来源 ID。
    """

    relationship_weights: Mapping[str, float] = field(default_factory=dict)
    beliefs: Sequence[BeliefLinks] = ()
    emotion: Mapping[str, float] = field(default_factory=dict)
    threads: Sequence[ThreadAttention] = ()


@dataclass
class RetrievedMemory:
    """一条带来源/时间/分数的检索结果；分数只给系统，不直接进 prompt。"""

    memory_id: str
    kind: str
    text: str
    source: str
    time: str
    time_ms: int
    score: float
    visibility: str = ""
    importance: float = 0.0
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "kind": self.kind,
            "text": self.text,
            "source": self.source,
            "time": self.time,
            "time_ms": self.time_ms,
            "score": round(float(self.score), 4),
            "visibility": self.visibility,
            "importance": round(float(self.importance), 4),
            "meta": dict(self.meta),
        }


def tokenize(text: Any) -> set:
    """轻量中英混合分词：ASCII 词 + 中文二元组。"""
    cleaned = clean_text(text if isinstance(text, str) else str(text or "")).lower()
    tokens = set(_ASCII_WORD.findall(cleaned))
    for run in _CJK_RUN.findall(cleaned):
        if len(run) == 1:
            tokens.add(run)
        else:
            for index in range(len(run) - 1):
                tokens.add(run[index : index + 2])
    return tokens


def _semantic_score(query_tokens: set, doc_tokens: set) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    intersection = len(query_tokens & doc_tokens)
    if not intersection:
        return 0.0
    coverage = intersection / len(query_tokens)
    jaccard = intersection / len(query_tokens | doc_tokens)
    return min(1.0, 0.7 * coverage + 0.3 * jaccard)


def _experience_attention(row: Mapping[str, Any], state: Mapping[str, Any]) -> float:
    """只对已入围的可见经历加有界注意力，不把主观状态提升为事实。"""
    source_values = row.get("source_event_ids")
    sources = {value for value in source_values if isinstance(value, str)} if isinstance(source_values, (list, tuple)) else set()
    def matches_source(item: Mapping[str, Any]) -> bool:
        refs = item.get("source_event_ids")
        return isinstance(refs, (list, tuple)) and any(isinstance(ref, str) and ref in sources for ref in refs)
    boost = 0.0
    speaker = row.get("speaker")
    if isinstance(speaker, str) and speaker:
        for relation in state.get("relationships") or []:
            if isinstance(relation, Mapping) and relation.get("character") == speaker:
                boost += 0.05 * max(_attention_value(relation.get(key, 0))
                                    for key in ("affection", "familiarity", "concern"))
                break
    for belief in state.get("beliefs") or []:
        if isinstance(belief, Mapping) and matches_source(belief):
            boost += 0.07
            break
    for thread in state.get("threads") or []:
        if isinstance(thread, Mapping) and thread.get("status") == "open" and matches_source(thread):
            boost += 0.04
            break
    mood, remembered = state.get("emotion"), row.get("emotion")
    if isinstance(mood, Mapping) and isinstance(remembered, Mapping):
        dimensions = ("joy", "sadness", "fear", "anger", "curiosity", "confusion", "excitement", "nostalgia", "tension")
        boost += 0.04 * max(min(_attention_value(mood.get(key, 0)), _attention_value(remembered.get(key, 0)))
                            for key in dimensions)
    return min(0.2, boost)


def rank_experience_candidates(
    query: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    embedding: Optional[EmbeddingConfig] = None,
    cognitive_state: Optional[Mapping[str, Any]] = None,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Rank caller-authorized candidates, returning copies with a computed score."""
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("limit 必须在 0–100 之间")
    if limit == 0 or not query.strip():
        return []
    rows = list(itertools.islice(candidates, _MAX_EXPERIENCE_CANDIDATES + 1))
    if len(rows) > _MAX_EXPERIENCE_CANDIDATES:
        raise ValueError("经历候选不能超过 512 条")
    if not rows:
        return []
    for row in rows:
        if (not isinstance(row, Mapping) or not isinstance(row.get("source"), str)
                or not row["source"].strip() or not isinstance(row.get("text"), str)
                or not row["text"].strip()):
            raise ValueError("经历候选必须包含非空 source 和 text")
    query = query[:500]
    query_tokens = tokenize(query)
    session = EmbeddingSession(embedding, query) if embedding is not None else None
    ranked = []
    for index, row in enumerate(rows):
        text = row["text"]
        lexical = _semantic_score(query_tokens, tokenize(text[:4000]))
        semantic = session.score(row["source"], text) if session is not None else None
        matched = semantic is not None and semantic["status"] == "matched"
        if lexical < _EXPERIENCE_LEXICAL_THRESHOLD and not matched:
            continue
        score = lexical + (semantic["contribution"] if semantic else 0.0)
        if isinstance(cognitive_state, Mapping):
            score += _experience_attention(row, cognitive_state)
        result = dict(row)
        result["score"] = score
        ranked.append((score, row["source"], index, result))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in ranked[:limit]]


def _recency_score(time_ms: int, now_ms: int, half_life_days: float = 7.0) -> float:
    if not time_ms:
        return 0.0
    age_days = max(0.0, (now_ms - time_ms) / 86_400_000.0)
    return 0.5 ** (age_days / max(0.5, half_life_days))


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _attention_value(value: Any) -> float:
    """将调用方的注意力输入约束在有限的 [0, 1]；非法值不加权。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return _clamp(number) if math.isfinite(number) else 0.0


def build_retrieval_query(
    snapshot: Dict[str, Any],
    state: Dict[str, Any],
    working: Optional[Dict[str, Any]] = None,
    *,
    max_chars: int = 500,
) -> str:
    """从当前观察/工作记忆/续场状态拼一个检索 query；保持确定性。"""
    working = working if isinstance(working, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    state = state if isinstance(state, dict) else {}
    parts: List[str] = []
    for value in (
        snapshot.get("guidance"),
        working.get("location"),
        working.get("current_activity"),
        state.get("current_intention"),
        state.get("current_quest"),
        state.get("last_location"),
    ):
        text = clean_text(str(value or "")).strip()
        if text:
            parts.append(text)
    characters = working.get("characters")
    if isinstance(characters, list):
        parts.extend(str(item) for item in characters if item)
    commentary = snapshot.get("commentary")
    if isinstance(commentary, list) and commentary:
        last = commentary[-1]
        if isinstance(last, dict) and last.get("text"):
            parts.append(str(last["text"]))
    query = " ".join(dict.fromkeys(parts))
    return query[:max_chars]


class MemoryRetriever:
    """本地 episode/fact 检索器，可选合并一个外部检索器。

    可通过 ``embedding=EmbeddingConfig(adapter, provenance=...)`` 显式接线。
    适配器收到的文档是 episode 的 location、what_happened、what_i_knew_before、
    what_i_learned、what_i_thought、my_reaction、characters、unresolved_questions
    以空格连接的结果，或 fact 的 proposition 加空格和空格连接的 tags；均按
    配置截断。所有文档调用均在可见性和事实知识门检查之后。默认不启用。
    """

    def __init__(
        self,
        episodic_store: Optional[EpisodicStore] = None,
        semantic_store: Optional[SemanticStore] = None,
        *,
        external: Any = None,
        knowledge_gate: Any = None,
        clock: Optional[Callable[[], int]] = None,
        weights: Optional[Dict[str, float]] = None,
        logger: Any = None,
        embedding: Optional[EmbeddingConfig] = None,
    ):
        self.episodic_store = episodic_store
        self.semantic_store = semantic_store
        self.external = external
        self.knowledge_gate = knowledge_gate
        self.embedding = embedding
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update({key: float(value) for key, value in weights.items()})
        self.log = logger

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        max_chars: int = 2400,
        active_characters: Optional[Iterable[str]] = None,
        active_threads: Optional[Iterable[str]] = None,
        now_ms: Optional[int] = None,
        cognitive_context: Optional[CognitiveContext] = None,
    ) -> List[RetrievedMemory]:
        now = self._clock() if now_ms is None else int(now_ms)
        query_tokens = tokenize(query)
        # 未启用 embedding 时完全沿用原来的空分词行为。
        if not query_tokens and (self.embedding is None or not isinstance(query, str) or not query.strip()):
            return []
        embedding_session = EmbeddingSession(self.embedding, query) if self.embedding is not None else None
        if not query_tokens and embedding_session is not None and not embedding_session.available:
            return []
        active_char_set = {
            clean_text(str(item)).lower()
            for item in (active_characters or [])
            if str(item).strip()
        }
        thread_tokens = tokenize(" ".join(str(item) for item in (active_threads or [])))

        results: List[RetrievedMemory] = []
        if self.episodic_store is not None:
            for episode in self.episodic_store.list():
                if episode.get("visibility") != EP_VISIBILITY:
                    continue
                result = self._score_episode(
                    episode, query, query_tokens, active_char_set, thread_tokens, now,
                    cognitive_context, embedding_session,
                )
                if result is not None:
                    results.append(result)
        if self.semantic_store is not None:
            for fact in self.semantic_store.list():
                if fact.get("visibility") != FACT_VISIBILITY:
                    continue
                if self.knowledge_gate is None or not self.knowledge_gate.permits(fact):
                    continue
                result = self._score_fact(
                    fact, query, query_tokens, active_char_set, thread_tokens, now,
                    cognitive_context, embedding_session,
                )
                if result is not None:
                    results.append(result)

        return self._select(results, limit=limit, max_chars=max_chars)

    async def retrieve_async(
        self,
        query: str,
        *,
        limit: int = 8,
        max_chars: int = 2400,
        active_characters: Optional[Iterable[str]] = None,
        active_threads: Optional[Iterable[str]] = None,
        now_ms: Optional[int] = None,
        cognitive_context: Optional[CognitiveContext] = None,
    ) -> List[RetrievedMemory]:
        results = await asyncio.to_thread(
            self.retrieve, query,
            limit=limit,
            max_chars=max_chars,
            active_characters=active_characters,
            active_threads=active_threads,
            now_ms=now_ms,
            cognitive_context=cognitive_context,
        )
        # 未经独立验证的外部文本不能证明其属于玩家知识。
        # 在有逐条来源与可见性校验前，禁止将它加入上下文。
        if self.external is not None and self.log is not None:
            self.log.warning("外部记忆来源尚未验证，已跳过外部检索")
        return results

    def _score_episode(
        self,
        episode: Dict[str, Any],
        query: str,
        query_tokens: set,
        active_characters: set,
        thread_tokens: set,
        now_ms: int,
        cognitive_context: Optional[CognitiveContext] = None,
        embedding_session: Optional[EmbeddingSession] = None,
    ) -> Optional[RetrievedMemory]:
        text = " ".join(
            str(episode.get(key) or "")
            for key in (
                "location",
                "what_happened",
                "what_i_knew_before",
                "what_i_learned",
                "what_i_thought",
                "my_reaction",
            )
        )
        text += " " + " ".join(episode.get("characters") or [])
        text += " " + " ".join(episode.get("unresolved_questions") or [])
        doc_tokens = tokenize(text)
        semantic = _semantic_score(query_tokens, doc_tokens)
        character = self._character_match(
            episode.get("characters") or [], query, active_characters
        )
        thread = self._thread_match(doc_tokens, thread_tokens)
        base = {
            "lexical": semantic * self.weights["semantic"],
            "importance": _clamp(episode.get("importance") or 0.0) * self.weights["importance"],
            "emotional_intensity": _clamp(episode.get("emotional_intensity") or 0.0)
            * self.weights["emotional"],
            "character": character * self.weights["character"],
            "thread_lexical": thread * self.weights["thread"],
            "recency": _recency_score(int(episode.get("time_ms") or 0), now_ms)
            * self.weights["recency"],
        }
        source = "episode:{0}".format(episode.get("event_id") or "")
        embedding_info = embedding_session.score(source, text) if embedding_session else None
        if embedding_info is not None:
            base["embedding"] = embedding_info["contribution"]
        bonus, details = self._cognitive_attention(
            cognitive_context, source, doc_tokens, episode.get("characters") or [],
            episode.get("what_i_felt") or {}, sum(base.values()),
        )
        if semantic <= 0 and character <= 0 and thread <= 0 and not details["context_relevance"] and not (embedding_info and embedding_info["contribution"] > 0):
            return None
        score = sum(base.values()) + sum(bonus.values())
        where = episode.get("location") or "某个地方"
        what = episode.get("what_happened") or ""
        thought = episode.get("what_i_thought") or ""
        text_view = "（影子模型的未验证记录）{0}：{1}".format(where, what)
        if thought:
            text_view += "；我当时想：{0}".format(thought)
        time_text = str(episode.get("time") or "")
        return RetrievedMemory(
            memory_id=str(episode.get("event_id") or ""),
            kind="episode",
            text=text_view[:600],
            source="episode:{0}".format(episode.get("event_id") or ""),
            time=time_text,
            time_ms=int(episode.get("time_ms") or 0),
            score=score,
            visibility=str(episode.get("visibility") or EP_VISIBILITY),
            importance=float(episode.get("importance") or 0.0),
            meta={
                "location": episode.get("location") or "",
                "characters": list(episode.get("characters") or []),
                "unresolved": list(episode.get("unresolved_questions") or []),
                "score_breakdown": {"base": base, "cognitive": bonus, "total": score},
                "attention_matches": details,
                **({"embedding": embedding_info} if embedding_info is not None else {}),
            },
        )

    def _score_fact(
        self,
        fact: Dict[str, Any],
        query: str,
        query_tokens: set,
        active_characters: set,
        thread_tokens: set,
        now_ms: int,
        cognitive_context: Optional[CognitiveContext] = None,
        embedding_session: Optional[EmbeddingSession] = None,
    ) -> Optional[RetrievedMemory]:
        proposition = str(fact.get("proposition") or "")
        document = proposition + " " + " ".join(fact.get("tags") or [])
        doc_tokens = tokenize(document)
        semantic = _semantic_score(query_tokens, doc_tokens)
        character = self._character_match(
            list(fact.get("tags") or []) + [proposition], query, active_characters
        )
        thread = self._thread_match(doc_tokens, thread_tokens)
        base = {
            "lexical": semantic * self.weights["semantic"],
            "importance": _clamp(fact.get("confidence") or 0.0) * self.weights["importance"],
            "character": character * self.weights["character"],
            "thread_lexical": thread * self.weights["thread"],
            "recency": _recency_score(int(fact.get("learned_ms") or 0), now_ms)
            * self.weights["recency"],
        }
        source = "fact:{0}".format(fact.get("fact_id") or "")
        embedding_info = embedding_session.score(source, document) if embedding_session else None
        if embedding_info is not None:
            base["embedding"] = embedding_info["contribution"]
        bonus, details = self._cognitive_attention(
            cognitive_context, source, doc_tokens,
            list(fact.get("tags") or []) + [proposition], {}, sum(base.values()),
        )
        if semantic <= 0 and character <= 0 and thread <= 0 and not details["context_relevance"] and not (embedding_info and embedding_info["contribution"] > 0):
            return None
        score = sum(base.values()) + sum(bonus.values())
        return RetrievedMemory(
            memory_id=str(fact.get("fact_id") or ""),
            kind="semantic",
            text=proposition[:500],
            source="fact:{0}".format(fact.get("fact_id") or ""),
            time=str(fact.get("learned_at") or ""),
            time_ms=int(fact.get("learned_ms") or 0),
            score=score,
            visibility=str(fact.get("visibility") or FACT_VISIBILITY),
            importance=float(fact.get("confidence") or 0.0),
            meta={
                "tags": list(fact.get("tags") or []),
                "evidence_source": fact.get("source", ""),
                "score_breakdown": {"base": base, "cognitive": bonus, "total": score},
                "attention_matches": details,
                **({"embedding": embedding_info} if embedding_info is not None else {}),
            },
        )

    @staticmethod
    def _cognitive_attention(
        context: Optional[CognitiveContext], source: str, doc_tokens: set,
        names: Iterable[str], felt: Mapping[str, Any], base_score: float,
    ) -> tuple[Dict[str, float], Dict[str, Any]]:
        """为已通过可见性/知识门的候选项计算有界增益，保留各项依据。"""
        values = {key: 0.0 for key in _COGNITIVE_WEIGHTS}
        details: Dict[str, Any] = {
            "relationships": [], "belief_links": [], "emotion_dimensions": [],
            "threads": [], "context_relevance": False,
        }
        if context is not None:
            candidate_names = [clean_text(str(name)).lower() for name in names]
            for name, weight in context.relationship_weights.items():
                normalized = clean_text(str(name)).lower().strip()
                strength = _attention_value(weight)
                if normalized and strength and any(normalized in item for item in candidate_names):
                    values["relationship"] = max(values["relationship"], strength)
                    details["relationships"].append(str(name))
            for belief in context.beliefs:
                relevance = _attention_value(belief.relevance)
                if not relevance:
                    continue
                for label, sources in (("support", belief.support_sources),
                                       ("counter", belief.counter_sources)):
                    if source in sources:
                        values["belief"] = max(values["belief"], relevance)
                        details["belief_links"].append({"belief_id": belief.belief_id, "role": label})
                        details["context_relevance"] = True
            for dimension, current in context.emotion.items():
                match = _attention_value(current) * _attention_value(felt.get(dimension, 0))
                if match:
                    values["emotion"] = max(values["emotion"], match)
                    details["emotion_dimensions"].append(str(dimension))
            for thread in context.threads:
                relevance = _attention_value(thread.relevance)
                if not relevance:
                    continue
                linked = source in thread.memory_sources
                lexical = _semantic_score(tokenize(thread.title), doc_tokens)
                match = max(1.0 if linked else 0.0, lexical) * relevance
                if match:
                    values["thread"] = max(values["thread"], match)
                    details["threads"].append({
                        "thread_id": thread.thread_id,
                        "match": "source" if linked else "lexical",
                    })
                    details["context_relevance"] = True
        bonus = {key: values[key] * weight for key, weight in _COGNITIVE_WEIGHTS.items()}
        total = sum(bonus.values())
        # 不改变自定义旧权重的基准得分；认知增益本身也不得使得分超过 1。
        allowed = min(0.2, max(0.0, 1.0 - base_score))
        if total > allowed and total:
            bonus = {key: value * allowed / total for key, value in bonus.items()}
        return bonus, details

    @staticmethod
    def _character_match(
        names: Iterable[str], query: str, active_characters: set
    ) -> float:
        query_lower = clean_text(query).lower()
        for name in names:
            text = clean_text(str(name)).lower()
            if not text:
                continue
            if text in active_characters or text in query_lower:
                return 1.0
            # fact 的 proposition 可能是“琴是代理团长”，需要看角色名是否
            # 出现在命题/标签里，而不是要求整句等于角色名。
            for active in active_characters:
                if active and active in text:
                    return 1.0
        return 0.0

    @staticmethod
    def _thread_match(doc_tokens: set, thread_tokens: set) -> float:
        if not thread_tokens:
            return 0.0
        return 1.0 if doc_tokens & thread_tokens else 0.0

    @staticmethod
    def _select(
        results: List[RetrievedMemory], *, limit: int, max_chars: int
    ) -> List[RetrievedMemory]:
        deduped: Dict[str, RetrievedMemory] = {}
        for result in results:
            key = result.memory_id or result.text
            existing = deduped.get(key)
            if existing is None or result.score > existing.score:
                deduped[key] = result
        ordered = sorted(
            deduped.values(),
            key=lambda item: (item.score, item.time_ms, item.memory_id),
            reverse=True,
        )
        selected: List[RetrievedMemory] = []
        used = 0
        for result in ordered:
            if len(selected) >= max(1, int(limit)):
                break
            size = len(result.text)
            if selected and used + size > max_chars:
                continue
            selected.append(result)
            used += size
        return selected
