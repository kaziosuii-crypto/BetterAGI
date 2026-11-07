#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Session Recap：一场结束时给下一场的最小自我回顾。

只从结构化 episode/fact/working 生成，不把整场 transcript 塞进下一场，
也不在这里做新的叙事判断。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from player_io import atomic_write_json, backup_corrupt_file, clean_text, read_json, utc_now_iso

SCHEMA_VERSION = 1


class RecapError(ValueError):
    """recap 文件不可安全读取。"""


class RecapSchemaError(RecapError):
    """recap 版本过新。"""


def _text(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _text_list(value: Any, limit: int = 12, item_limit: int = 200) -> List[str]:
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


def build_recap(
    *,
    session_id: str,
    state: Dict[str, Any],
    working: Dict[str, Any],
    episodes: Iterable[Dict[str, Any]],
    facts: Iterable[Dict[str, Any]] = (),
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """确定性生成 recap；同一输入得到同一结果（update 时间除外）。"""
    episode_list = [item for item in episodes if isinstance(item, dict)]
    fact_list = [item for item in facts if isinstance(item, dict)]
    episode_list.sort(key=lambda item: int(item.get("time_ms") or 0), reverse=True)
    fact_list.sort(key=lambda item: int(item.get("learned_ms") or 0), reverse=True)

    recent = episode_list[:3]
    happened = [item.get("what_happened") for item in recent]
    people: List[str] = []
    unresolved: List[str] = []
    for item in episode_list[:8]:
        for name in item.get("characters") or []:
            if isinstance(name, str) and name and name not in people:
                people.append(name)
        for question in item.get("unresolved_questions") or []:
            if isinstance(question, str) and question and question not in unresolved:
                unresolved.append(question)
    for question in working.get("open_questions") or []:
        if isinstance(question, str) and question and question not in unresolved:
            unresolved.append(question)

    learned = [
        str(item.get("proposition") or "")
        for item in fact_list[:4]
        if item.get("proposition")
    ]
    emotions = working.get("emotion") if isinstance(working.get("emotion"), dict) else {}
    next_intentions = []
    for value in (
        state.get("current_intention"),
        working.get("current_activity"),
    ):
        text = _text(value, 240)
        if text and text not in next_intentions:
            next_intentions.append(text)
    for item in working.get("short_plan") or []:
        text = _text(item, 160)
        if text and text not in next_intentions:
            next_intentions.append(text)

    moment = int(now_ms if now_ms is not None else time.time() * 1000)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": _text(session_id, 120),
        "created_ms": moment,
        "created_at": utc_now_iso(moment),
        "last_location": _text(state.get("last_location") or working.get("location"), 200),
        "current_quest": _text(state.get("current_quest"), 240),
        "what_happened": _text_list(happened, limit=3, item_limit=300),
        "people": _text_list(people, limit=10, item_limit=60),
        "learned": _text_list(learned, limit=4, item_limit=240),
        "unresolved": _text_list(unresolved, limit=8, item_limit=200),
        "emotional_aftertaste": list(emotions)[:6],
        "last_thought": _text(state.get("last_internal_thought"), 400),
        "next_intentions": _text_list(next_intentions, limit=4, item_limit=200),
        "episode_count": len(episode_list),
    }


def recap_prompt_text(recap: Optional[Dict[str, Any]]) -> str:
    """渲染成下一场 system/user 可用的自然语言；不含裸分数与路径。"""
    if not isinstance(recap, dict) or not recap:
        return ""
    lines: List[str] = []
    if recap.get("last_location"):
        lines.append("- 上次停在：{0}".format(recap["last_location"]))
    if recap.get("current_quest"):
        lines.append("- 上次的任务：{0}".format(recap["current_quest"]))
    for item in recap.get("what_happened") or []:
        lines.append("- 过去的推测记录（未经核实）：{0}".format(item))
    if recap.get("people"):
        lines.append("- 旧记录中的角色候选（未经核实）：{0}".format("、".join(recap["people"])))
    for item in recap.get("learned") or []:
        lines.append("- 旧事实记录（未重新验明来源）：{0}".format(item))
    for item in recap.get("unresolved") or []:
        lines.append("- 还惦记着：{0}".format(item))
    if recap.get("last_thought"):
        lines.append("- 上次最后在想：{0}".format(recap["last_thought"]))
    for item in recap.get("next_intentions") or []:
        lines.append("- 下次想做：{0}".format(item))
    return "\n".join(lines)


def read_journey(home: Path | str, *, profile_path: Path | str | None = None,
                 limit: int = 30) -> Dict[str, Any]:
    """从已有只读 SQLite 索引提取经过玩家绑定与来源核验的主观回顾。"""
    from player_event_log import PlayerEventLog
    from player_experience import PlayerExperience
    from player_profile import load_profile
    from player_reflection import _unique_object

    if type(limit) is not int or not 1 <= limit <= 100:
        raise RecapError("旅程回顾上限必须在 1 到 100 之间")
    root = Path(home).expanduser().resolve()
    profile_file = Path(profile_path).expanduser().resolve() if profile_path else root / "profile" / "profile.json"
    if not profile_file.is_relative_to(root):
        raise RecapError("玩家档案必须位于指定的玩家目录内")
    profile = load_profile(profile_file)
    scope = "player-" + hashlib.sha256(profile.player_id.encode("utf-8")).hexdigest()
    db_path = root / "state" / "cognitive_events.sqlite"
    if not db_path.is_file():
        raise RecapError("玩家经历数据库不存在，不能创建空数据库作为回顾")
    items: List[Dict[str, Any]] = []
    with PlayerEventLog(db_path, read_only=True) as log:
        experience = PlayerExperience(log)
        reflections = experience.recent_reflections(profile.player_id, limit=limit, player_scope=scope)
        for row in reflections:
            sid, eid = row.get("session_id"), row.get("event_id")
            evidence = row.get("evidence")
            if not isinstance(sid, str) or not isinstance(eid, str) or not isinstance(evidence, list):
                continue
            refs = {ref.get("event_id"): ref for ref in evidence if isinstance(ref, dict)
                    and ref.get("session_id") == sid and isinstance(ref.get("event_id"), str)}
            event = log.get_event(sid, eid)
            if event is None:
                continue
            try:
                document = json.loads(row.get("text", ""), object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                document = None
            candidates = document.get("items") if isinstance(document, dict) else None
            if candidates is None and document is None and isinstance(row.get("text"), str):
                candidates = [{"kind": "reflection", "text": row["text"],
                               "uncertainty": row.get("uncertainty"), "source_event_ids": list(refs)}]
            if not isinstance(candidates, list) or len(candidates) > 12:
                continue
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                kind, text, uncertainty = item.get("kind"), item.get("text"), item.get("uncertainty")
                source_ids = item.get("source_event_ids")
                if (not isinstance(kind, str) or not isinstance(text, str) or not text.strip()
                        or not isinstance(uncertainty, str) or not uncertainty.strip()
                        or not isinstance(source_ids, list) or not source_ids
                        or any(not isinstance(value, str) or value not in refs for value in source_ids)):
                    continue
                items.append({"kind": kind, "classification": "主观候选，未经核实",
                              "timestamp": row.get("timestamp"), "text": text,
                              "uncertainty": uncertainty, "session_id": sid, "event_id": eid,
                              "sources": [dict(refs[source]) for source in source_ids], "_seq": event["seq"]})
        for row in experience.recent_dialogue(profile.player_id, limit=limit, player_scope=scope):
            sid, eid = row.get("session_id"), row.get("event_id")
            event = log.get_event(sid, eid)
            if event is None:
                continue
            items.append({"kind": "dialogue_ocr", "classification": "画面 OCR 候选，可能识别错误",
                          "timestamp": row.get("timestamp"), "text": row.get("text"),
                          "uncertainty": "文字和说话人未经独立验证", "speaker": row.get("speaker"),
                          "session_id": sid, "event_id": eid,
                          "sources": [{"session_id": sid, "event_id": eid,
                                       "source_id": row.get("source_id"), "seq": event["seq"]}],
                          "_seq": event["seq"]})
    items.sort(key=lambda item: item["_seq"])
    items = items[-limit:]
    for item in items:
        del item["_seq"]
    return {"player_id": profile.player_id, "player_scope": scope,
            "classification": "个人经历与主观候选，不代表世界真相",
            "items": items, "status": "available" if items else "unknown"}


class RecapStore:
    """`state/session_recap.json` 的原子存储。"""

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

    def load(self) -> Optional[Dict[str, Any]]:
        self.loaded_from_disk = False
        self.recovered_corrupt = None
        self.load_warning = ""
        if not self.path.exists():
            return None
        try:
            data = read_json(self.path)
        except ValueError as exc:
            self._recover("JSON 解析失败：{0}".format(exc))
            return None
        except OSError as exc:
            raise RecapError("读取 recap 失败：{0}".format(exc))
        if not isinstance(data, dict):
            self._recover("顶层不是对象")
            return None
        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            self._recover("schema_version 非法")
            return None
        if version > SCHEMA_VERSION:
            raise RecapSchemaError("recap 版本过新：{0}".format(version))
        self.loaded_from_disk = True
        return data

    def save(self, recap: Dict[str, Any]) -> Path:
        data = dict(recap)
        data["schema_version"] = SCHEMA_VERSION
        data["updated_at"] = utc_now_iso(self._clock())
        atomic_write_json(self.path, data)
        return self.path

    def _recover(self, reason: str) -> None:
        try:
            backup = backup_corrupt_file(self.path, self._clock())
        except OSError:
            return
        self.recovered_corrupt = backup
        self.load_warning = "recap 损坏（{0}），已备份到 {1}".format(reason, backup)
        if self.log is not None:
            self.log.warning(self.load_warning)
