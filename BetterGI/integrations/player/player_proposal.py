#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析 shadow 模式下玩家 agent 的提案 JSON。

提案只是「如果是我会怎么想/怎么做」的记录，不含任何可执行指令。
解析器对模型输出保持宽容（可能带 markdown、前后解释），但写出的字段
必须被裁剪和限长，避免日志膨胀或把随机文本带进后续阶段。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from player_io import clean_text

ALLOWED_ACTION_HINTS = {
    "walk",
    "run",
    "look",
    "interact",
    "attack",
    "jump",
    "open_map",
    "wait",
    "use_item",
    "talk",
    "other",
}

ALLOWED_EMOTIONS = {
    "calm",
    "curious",
    "excited",
    "happy",
    "worried",
    "nervous",
    "sad",
    "angry",
    "surprised",
    "thinking",
    "tired",
    "determined",
}


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(clean_text(value).split())[:limit]


def _clamp(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _strip_fence(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _first_balanced_object(text: str) -> Optional[str]:
    """从任意文本中截取第一个括号配平的 JSON 对象（忽略字符串内的括号）。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return None


def _unique_object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 存在重复字段")
        result[key] = value
    return result


def extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    """从模型输出中提取 JSON 对象；失败返回 None，不抛异常。"""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return None

    candidates = [text.strip(), _strip_fence(text)]
    balanced = _first_balanced_object(text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate, object_pairs_hook=_unique_object)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def normalize_dialogue_decision(value: Any) -> Dict[str, str]:
    """Validate the proposal without changing source or option identifiers."""
    if not isinstance(value, dict):
        raise ValueError("dialogue_decision 必须是对象")
    action = value.get("action")
    if not isinstance(action, str) or action not in {"wait", "advance", "select"}:
        raise ValueError("dialogue_decision.action 无效")
    fields = {"action", "frame_id", "option_id"} if action == "select" else {"action", "frame_id"}
    if set(value) != fields:
        raise ValueError("dialogue_decision 字段不匹配")
    for key in fields - {"action"}:
        identifier = value[key]
        if (not isinstance(identifier, str) or not identifier or _text(identifier, 128) != identifier
                or any(ord(char) < 32 or ord(char) == 127 for char in identifier)):
            raise ValueError("dialogue_decision 标识无效")
    return dict(value)


def normalize_proposal(data: Any) -> Optional[Dict[str, Any]]:
    """裁剪提案字段；如果没有任何实质内容则返回 None。"""
    if not isinstance(data, dict):
        return None
    # 容忍 {"proposal": {...}} 包装，但只在顶层没有实际字段时解包。
    if (
        isinstance(data.get("proposal"), dict)
        and not any(
            key in data
            for key in ("thought", "intention", "speech", "proposed_actions", "dialogue_decision")
        )
    ):
        data = data["proposal"]

    dialogue_decision = None
    if "dialogue_decision" in data:
        try:
            dialogue_decision = normalize_dialogue_decision(data["dialogue_decision"])
        except ValueError:
            return None

    observation = _text(data.get("observation"), 300)
    thought = _text(data.get("thought"), 800)

    intention_raw = data.get("intention")
    if isinstance(intention_raw, str):
        intention_raw = {"goal": intention_raw}
    intention: Optional[Dict[str, Any]] = None
    if isinstance(intention_raw, dict):
        goal = _text(intention_raw.get("goal"), 200)
        if goal:
            horizon = _text(intention_raw.get("horizon"), 20).lower()
            if horizon not in ("now", "soon", "session"):
                horizon = "soon"
            intention = {
                "goal": goal,
                "reason": _text(intention_raw.get("reason"), 300),
                "horizon": horizon,
            }
            # 可选 MotorIntent 字段沿用已有规范化规则；旧提案输出不变。
            from player_intent import normalize_intent

            motor = normalize_intent({
                key: intention_raw[key]
                for key in ("goal", "target", "constraints", "interrupt_conditions", "motor_mode", "deadline_ms")
                if key in intention_raw
            })
            for key in ("target", "constraints", "interrupt_conditions", "motor_mode", "deadline_ms"):
                if key in intention_raw:
                    intention[key] = motor[key]

    speech_raw = data.get("speech")
    if isinstance(speech_raw, str):
        speech_raw = {"text": speech_raw}
    speech: Optional[Dict[str, Any]] = None
    if isinstance(speech_raw, dict):
        text = _text(speech_raw.get("text"), 160)
        should_speak = speech_raw.get("should_speak")
        if should_speak is None:
            should_speak = bool(text)
        emotion = _text(speech_raw.get("emotion"), 20).lower()
        if emotion not in ALLOWED_EMOTIONS:
            emotion = "calm"
        speech = {
            "text": text,
            "emotion": emotion,
            "intensity": _clamp(speech_raw.get("intensity"), 0.5),
            "should_speak": bool(should_speak) and bool(text),
        }

    actions: List[Dict[str, Any]] = []
    raw_actions = data.get("proposed_actions")
    if isinstance(raw_actions, (list, tuple)):
        for item in raw_actions[:6]:
            if isinstance(item, str):
                item = {"hint": item}
            if not isinstance(item, dict):
                continue
            hint = _text(item.get("hint") or item.get("action"), 24).lower()
            if hint not in ALLOWED_ACTION_HINTS:
                hint = "other"
            detail = _text(item.get("detail"), 200)
            reason = _text(item.get("reason"), 200)
            if not (detail or reason):
                continue
            actions.append(
                {
                    "hint": hint,
                    "detail": detail,
                    "reason": reason,
                    # 永远标记为未执行；执行只能发生在未来的 MotorController。
                    "executed": False,
                }
            )

    note_raw = data.get("memory_note")
    memory_note: Dict[str, Any] = {}
    if isinstance(note_raw, dict):
        scene = _text(note_raw.get("scene") or note_raw.get("location"), 200)
        quest = _text(note_raw.get("quest"), 200)
        checks = note_raw.get("things_to_check")
        things: List[str] = []
        if isinstance(checks, (list, tuple)):
            for item in checks[:5]:
                text = _text(item, 100)
                if text:
                    things.append(text)
        if scene:
            memory_note["scene"] = scene
        if quest:
            memory_note["quest"] = quest
        if things:
            memory_note["things_to_check"] = things

        characters = note_raw.get("characters")
        if isinstance(characters, (list, tuple)):
            names: List[str] = []
            for item in characters[:12]:
                text = _text(item, 60)
                if text and text not in names:
                    names.append(text)
            if names:
                memory_note["characters"] = names

        for key, limit in (
            ("what_happened", 700),
            ("what_i_knew_before", 700),
            ("what_i_learned", 700),
            ("my_reaction", 400),
        ):
            text = _text(note_raw.get(key), limit)
            if text:
                memory_note[key] = text

        felt_raw = note_raw.get("what_i_felt")
        if isinstance(felt_raw, dict):
            felt: Dict[str, float] = {}
            for key, value in list(felt_raw.items())[:12]:
                name = _text(key, 40)
                if name:
                    felt[name] = _clamp(value, 0.0)
            if felt:
                memory_note["what_i_felt"] = felt
        try:
            importance = float(note_raw.get("importance"))
        except (TypeError, ValueError):
            importance = None
        if importance is not None:
            memory_note["importance"] = _clamp(importance, 0.5)

        learned = note_raw.get("learned_facts")
        if isinstance(learned, (list, tuple)):
            facts: List[Dict[str, Any]] = []
            for item in learned[:6]:
                if isinstance(item, str):
                    proposition = _text(item, 500)
                    confidence = 0.6
                    tags: List[str] = []
                elif isinstance(item, dict):
                    proposition = _text(
                        item.get("proposition") or item.get("text"), 500
                    )
                    confidence = _clamp(item.get("confidence"), 0.6)
                    tags = []
                    raw_tags = item.get("tags")
                    if isinstance(raw_tags, (list, tuple)):
                        for tag in raw_tags[:6]:
                            text = _text(tag, 40)
                            if text and text not in tags:
                                tags.append(text)
                else:
                    continue
                if isinstance(item, dict):
                    evidence_id = _text(item.get("evidence_id"), 64)
                    quote = _text(item.get("quote"), 200)
                else:
                    evidence_id = ""
                    quote = ""
                if proposition or (evidence_id and quote):
                    facts.append(
                        {
                            "proposition": proposition,
                            "confidence": confidence,
                            "tags": tags,
                            "evidence_id": evidence_id,
                            "quote": quote,
                        }
                    )
            if facts:
                memory_note["learned_facts"] = facts

    beliefs_raw = data.get("beliefs")
    beliefs: List[Dict[str, Any]] = []
    if isinstance(beliefs_raw, (list, tuple)):
        for item in beliefs_raw[:6]:
            if not isinstance(item, dict):
                continue
            proposition = _text(item.get("proposition"), 500)
            if not proposition:
                continue
            status = _text(item.get("status"), 30).lower()
            if status and status not in {
                "known_fact", "belief", "hypothesis", "suspicion", "unknown", "disproven"
            }:
                continue
            record = {"proposition": proposition}
            if "status" in item:
                record["status"] = status
            if "confidence" in item:
                record["confidence"] = _clamp(item.get("confidence"), 0.5)
            for field in ("evidence", "contradictions"):
                values = item.get(field)
                if isinstance(values, (list, tuple)):
                    record[field] = [_text(value, 200) for value in values[:8] if _text(value, 200)]
            reason = _text(item.get("reason"), 200)
            if reason:
                record["reason"] = reason
            beliefs.append(record)

    relationships_raw = data.get("relationships")
    relationships: List[Dict[str, Any]] = []
    if isinstance(relationships_raw, (list, tuple)):
        for item in relationships_raw[:6]:
            if not isinstance(item, dict):
                continue
            character = _text(item.get("character") or item.get("name"), 60)
            if not character:
                continue
            record = {"character": character}
            for key in (
                "familiarity", "trust", "affection", "suspicion", "respect", "concern"
            ):
                if key in item:
                    record[key] = _clamp(item.get(key), 0.0)
            record["memorable_events"] = [
                _text(value, 240)
                for value in (item.get("memorable_events") or [])[:6]
                if _text(value, 240)
            ]
            relationships.append(record)

    emotion_raw = data.get("emotion")
    emotion: Dict[str, float] = {}
    if isinstance(emotion_raw, dict):
        for key, value in list(emotion_raw.items())[:9]:
            name = _text(key, 30)
            if name:
                emotion[name] = _clamp(value, 0.0)

    threads_raw = data.get("threads")
    threads: List[Dict[str, Any]] = []
    if isinstance(threads_raw, (list, tuple)):
        for item in threads_raw[:6]:
            if not isinstance(item, dict):
                continue
            title = _text(item.get("title") or item.get("thread"), 240)
            if not title:
                continue
            status = _text(item.get("status"), 20).lower()
            if status and status not in {"open", "dormant", "resolved", "abandoned"}:
                continue
            record = {"title": title}
            if "status" in item:
                record["status"] = status
            if "importance" in item:
                record["importance"] = _clamp(item.get("importance"), 0.5)
            if "current_theory" in item:
                record["current_theory"] = _text(item.get("current_theory"), 400)
            for field in ("evidence", "characters", "topics"):
                values = item.get(field)
                if isinstance(values, (list, tuple)):
                    record[field] = [_text(value, 200) for value in values[:8] if _text(value, 200)]
            threads.append(record)

    experience_note = None
    raw_experience = data.get("experience_note")
    if isinstance(raw_experience, dict):
        content = _text(raw_experience.get("text"), 500)
        derivation = _text(raw_experience.get("derivation"), 300)
        if content and derivation:
            experience_note = {"text": content, "derivation": derivation,
                               "confidence": _clamp(raw_experience.get("confidence"), 0.5)}

    proposal = {
        "observation": observation,
        "thought": thought,
        "intention": intention,
        "speech": speech,
        "proposed_actions": actions,
        "memory_note": memory_note,
        "beliefs": beliefs,
        "relationships": relationships,
        "emotion": emotion,
        "threads": threads,
        "experience_note": experience_note,
        "confidence": _clamp(data.get("confidence"), 0.5),
    }
    if dialogue_decision is not None:
        proposal["dialogue_decision"] = dialogue_decision
    if not (observation or thought or intention or (speech and speech["text"]) or actions or dialogue_decision):
        return None
    return proposal


def parse_proposal(text: Any) -> Optional[Dict[str, Any]]:
    """解析并规范化一条模型输出；无法得到有效提案时返回 None。"""
    return normalize_proposal(extract_json_object(text))
