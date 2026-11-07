#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件日志上的玩家认知事务，不读写旧 JSON stores。

每个 session 的 cognitive.v1 是兼容旧调用的快照；显式 player_scope 使用独立
玩家投影，跨真实 session 持续演化。调用方须先为每个 session 绑定玩家，绑定
一经建立不可由此 API 更改。调用方持有 PlayerEventLog 生命周期，先提交可见
raw_observation，再调用 commit；每轮仅一次 append，原子提交事件、状态及 CAS。
冲突不会重试，调用方应重新读取并重新决定。event_id 由调用方生成。

绑定由可信父进程分配，不是身份认证或访问控制；不能证明观察真实、推断正确
或讲话/动作已执行。此 API 不约束直接写数据库或绕过 API 的其他写入者。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

try:
    from .player_event_log import EventLogConflict, PlayerEventLog, validate_projection_scope
    from . import player_beliefs as beliefs, player_relationships as relationships
    from . import player_emotion as emotion, player_threads as threads
    from . import player_working_memory as working_memory, player_proposal
    from .player_io import utc_now_iso
except ImportError:
    from player_event_log import EventLogConflict, PlayerEventLog, validate_projection_scope
    import player_beliefs as beliefs
    import player_relationships as relationships
    import player_emotion as emotion
    import player_threads as threads
    import player_working_memory as working_memory
    import player_proposal
    from player_io import utc_now_iso

PROJECTION = "cognitive.v1"
SESSION_BINDING = "cognitive.session_owner.v1"
VISIBILITY = "player_knowledge"
VERIFICATION = "subjective_unverified"
MOTOR_PROJECTION = "cognitive.motor.v1"


def _receipt_name(kind: str, event_id: str) -> str:
    """收据按事件 ID 分片，避免投影携带无界事件列表。"""
    try:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise CognitiveTransactionError("event_id 必须是有效 UTF-8") from exc
    return "cognitive.receipt." + kind + "." + digest


def _migration_projection(migration: list) -> dict:
    """仅迁移时保留一份旧投影，后续 tick 不再复制或覆盖。"""
    if not migration:
        return {}
    archive = migration[0]
    return {"cognitive.legacy." + archive["event_id"]: deepcopy(archive["payload"]["legacy_state"])}


def _clear_field_sources(record: dict, fields: Any) -> None:
    sources = record.get("field_sources")
    if not isinstance(sources, dict):
        return
    for field in fields:
        sources.pop(field, None)
    if not sources:
        record.pop("field_sources", None)


def _compact_state(state: dict) -> None:
    """只保留每个实体的最新来源和变化；完整变更由同事务事件存档。"""
    for record in state["beliefs"]:
        for key in ("history", "transitions", "source_event_ids"):
            if isinstance(record.get(key), list):
                record[key] = record[key][-1:]
    for record in state["threads"]:
        for key in ("status_history", "source_event_ids"):
            if isinstance(record.get(key), list):
                record[key] = record[key][-1:]
    # 每个 proposition 的最新值是当前认知，不同轮次的事实仍保存在事件中。
    latest = {}
    for fact in state["facts"]:
        latest[fact["proposition"].casefold()] = fact
    state["facts"] = list(latest.values())


def _legacy_activation_matches(entry: dict, event: dict, source: dict, session_id: str) -> bool:
    """旧激活事件没有 changes；严格重放其来源和激活清单来校验投影。"""
    payload = event["payload"]
    activated = payload.get("activated")
    changes = entry.get("changes")
    now_ms = entry.get("time_ms")
    if (event["type"] != "cognitive.thread_activation" or "changes" in payload
            or not isinstance(activated, list) or len(activated) > threads._MAX_EVENT_ACTIVATIONS
            or not isinstance(changes, dict) or type(now_ms) is not int or now_ms < 0
            or source["payload"].get("channel") == "action_attempt"):
        return False
    if not activated:
        return changes == {}
    if set(changes) != {"threads"} or not isinstance(changes["threads"], dict):
        return False
    change = changes["threads"]
    if set(change) != {"before", "after"} or not isinstance(change["before"], list) or not isinstance(change["after"], list):
        return False
    before, after = change["before"], change["after"]
    if len(before) != len(after) or not all(isinstance(item, dict) for item in before + after):
        return False
    text = threads._text(source["payload"]["text"], len(source["payload"]["text"])).lower()
    expected = deepcopy(before)
    matched = []
    for old, new in zip(expected, after):
        if old.get("status") != "dormant":
            continue
        reason = ""
        for entity in old.get("characters", []):
            candidate = threads._specific(entity) if isinstance(entity, str) and len(entity) <= 120 else ""
            if threads._contains_topic(text, candidate):
                reason = "entity:" + candidate
                break
        if not reason:
            for topic in [old.get("title"), *old.get("topics", [])]:
                candidate = threads._specific(topic, topic=True) if isinstance(topic, str) and len(topic) <= 120 else ""
                if threads._contains_topic(text, candidate):
                    reason = "topic:" + candidate
                    break
        if not reason:
            continue
        thread_id = old.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            return False
        threads._record_status(old, "dormant", "open", now_ms, reason=reason, event_id=source["event_id"])
        old["status"] = "open"
        old["last_referenced_ms"] = now_ms
        old.setdefault("source_event_ids", []).append(source["event_id"])
        if "last_activation" in new:
            old["last_activation"] = {"event_id": source["event_id"], "session_id": session_id,
                                      "reason": reason, "time_ms": now_ms}
        matched.append({"thread_id": thread_id, "reason": reason})
        if len(matched) == threads._MAX_EVENT_ACTIVATIONS:
            break
    return matched == activated and expected == after


def _attribution(value: Any, *, require_generation: bool = False) -> dict:
    """验证桥归属；无动作组使用缺省值，不能把缺失代际当作匹配。"""
    if not isinstance(value, dict) or set(value) - {"motorGeneration", "actionGroupId"}:
        raise CognitiveTransactionError("attribution 字段无效")
    result = dict(value)
    generation = result.get("motorGeneration")
    if "motorGeneration" in result and (type(generation) is not int or not 0 <= generation <= 2**63 - 1):
        raise CognitiveTransactionError("motorGeneration 必须是非负 64 位整数")
    if require_generation and "motorGeneration" not in result:
        raise CognitiveTransactionError("缺少 motorGeneration")
    if "actionGroupId" in result:
        group = result["actionGroupId"]
        if not isinstance(group, str) or not group.strip() or group != group.strip() or len(group) > 128:
            raise CognitiveTransactionError("actionGroupId 无效")
    return result


def _intent_key(intent_id: str) -> str:
    """以原始 ID 的摘要作为稳定、全局唯一的事件及投影命名空间。"""
    return hashlib.sha256(intent_id.encode("utf-8")).hexdigest()


class CognitiveTransactionError(ValueError):
    """输入、来源或已存快照不符合认知事务契约。"""


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CognitiveTransactionError(f"{name} 必须是非空字符串")
    return value


def _check_input(value: Any) -> None:
    """拒绝非有限数字和任何结构性世界真相声明，不审查自然语言真假。"""
    forbidden = {"canonical", "canonical_world_truth", "world", "world_only",
                 "ground_truth", "verified", "objective", "world_truth"}
    if isinstance(value, float) and not math.isfinite(value):
        raise CognitiveTransactionError("输入不能包含非有限数字")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CognitiveTransactionError("字段名必须是字符串")
            if key.lower() in forbidden:
                raise CognitiveTransactionError("禁止 canonical/world 字段")
            if key.lower() in {"visibility", "verification", "status", "kind", "type", "scope", "authority"} and isinstance(item, str) and item.strip().lower() in forbidden:
                raise CognitiveTransactionError("禁止 canonical/world 声明")
            _check_input(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_input(item)


def _fields(obj: Any, allowed: set[str], name: str) -> dict:
    if not isinstance(obj, dict) or set(obj) - allowed:
        raise CognitiveTransactionError(f"{name} 字段无效")
    return obj


def _items(value: Any, name: str) -> list:
    if not isinstance(value, list) or len(value) > 8:
        raise CognitiveTransactionError(f"{name} 必须是最多八条的列表")
    return value


def _number_fields(obj: dict, fields: Any) -> None:
    """数值字段必须是实际有限数字，避免字符串/布尔值被静默转为默认值。"""
    for field in fields:
        if field in obj:
            value = obj[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise CognitiveTransactionError(f"{field} 必须是有限数字")


def _list_fields(obj: dict, fields: Any) -> None:
    for field in fields:
        if field in obj and (not isinstance(obj[field], list) or not all(isinstance(x, str) for x in obj[field])):
            raise CognitiveTransactionError(f"{field} 必须是字符串列表")


def _merge_patch(base: dict, patch: dict) -> dict:
    """未提供的键保持原值；字典递归合并，列表/标量显式替换。"""
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_patch(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _empty() -> dict:
    """新投影只保留当前状态和线程最近变更索引；审计在事件中。"""
    return {"version": 2, "working": working_memory.default_working(),
            "beliefs": [], "relationships": [], "emotion": emotion._empty(),
            "threads": [], "facts": [], "thread_last_events": {}, "legacy_archive": None}


def _state_valid(state: Any) -> bool:
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] not in (1, 2):
        return False
    common = {"version", "working", "beliefs", "relationships", "emotion", "threads", "facts"}
    if set(state) != common | ({"history"} if state["version"] == 1 else {"thread_last_events", "legacy_archive"}):
        return False
    if state["version"] == 1 and (not isinstance(state["history"], list) or not all(isinstance(x, dict) for x in state["history"])):
        return False
    if state["version"] == 2 and not isinstance(state["thread_last_events"], dict):
        return False
    if not isinstance(state["working"], dict) or not isinstance(state["emotion"], dict):
        return False
    if any(not isinstance(state[k], list) or not all(isinstance(x, dict) for x in state[k])
           for k in ("beliefs", "relationships", "threads", "facts")):
        return False
    return all("fingerprint" in x and "status" in x for x in state["beliefs"] + state["threads"]) and all(
        "character" in x and "character_id" in x for x in state["relationships"])


class PlayerCognitiveTransaction:
    """持久化主动模式一轮主观认知。连接由调用方关闭。"""

    def __init__(self, event_log: PlayerEventLog):
        self._log = event_log
        self._live_speech_reservations: dict[tuple[str, str, str], Any] = {}
        self._live_speech_fields: dict[tuple[str, str, str], dict] = {}

    def bind_session(self, *, session_id: str, player_scope: str, event_id: str,
                     timestamp: str | None = None) -> dict:
        """由可信父进程声明真实 session 的唯一玩家；冲突时不得重新绑定。"""
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        event_id = _required(event_id, "event_id")
        current = self._log.get_projection(session_id, SESSION_BINDING)
        if current is not None:
            return self._binding(session_id, player_scope)
        if self._log.get_projection(session_id, PROJECTION) is not None:
            raise CognitiveTransactionError("旧会话已有本地认知状态，不能隐式迁移或覆盖")
        timestamp = _required(timestamp if timestamp is not None else utc_now_iso(int(time.time() * 1000)), "timestamp")
        state = {"version": 1, "session_id": session_id, "player_scope": player_scope, "event_id": event_id}
        event = {"event_id": event_id, "session_id": session_id, "timestamp": timestamp,
                 "type": "cognitive.session_binding", "payload": state,
                 "source": "player_cognitive_transaction"}
        seq = self._log.append([event], projections={SESSION_BINDING: state},
                               expected_revisions={SESSION_BINDING: 0},
                               expected_session_revisions={PROJECTION: 0})[0]
        return {"state": deepcopy(state), "revision": seq}

    def _binding(self, session_id: str, player_scope: str) -> dict:
        """校验持久绑定及其原始事件，不能凭调用方传入的 scope 认领会话。"""
        binding = self._log.get_projection(session_id, SESSION_BINDING)
        state = binding.get("state") if binding else None
        if (not isinstance(state, dict) or set(state) != {"version", "session_id", "player_scope", "event_id"}
                or type(state.get("version")) is not int or state["version"] != 1
                or state.get("session_id") != session_id or state.get("player_scope") != player_scope
                or not isinstance(state.get("event_id"), str) or not state["event_id"].strip()):
            raise CognitiveTransactionError("会话未绑定该玩家或绑定无效")
        event = self._log.get_event(session_id, state["event_id"])
        if (not event or event["seq"] != binding["revision"] or event["type"] != "cognitive.session_binding"
                or event["source"] != "player_cognitive_transaction" or event["payload"] != state):
            raise CognitiveTransactionError("会话玩家绑定事件无效")
        return binding

    def read(self, session_id: str, *, player_scope: str | None = None) -> dict:
        """返回 {state, revision} 的副本；scope 模式要求 session 已绑定该玩家。"""
        session_id = _required(session_id, "session_id")
        if player_scope is not None:
            player_scope = validate_projection_scope(player_scope)
            self._binding(session_id, player_scope)
        elif self._log.get_projection(session_id, SESSION_BINDING) is not None:
            raise CognitiveTransactionError("已绑定会话必须显式提供 player_scope")
        projection = self._log.get_projection(session_id, PROJECTION, projection_scope=player_scope)
        if projection is None:
            return {"state": _empty(), "revision": 0}
        if not _state_valid(projection["state"]):
            raise CognitiveTransactionError("认知投影损坏或版本不兼容")
        return deepcopy(projection)

    def _prepare_state(self, snapshot: dict, session_id: str, player_scope: str | None,
                       timestamp: str) -> tuple[dict, list]:
        """保守迁移 v1：校验事件链并将完整原始快照同事务归档，失败不覆盖。"""
        state = snapshot["state"]
        if state["version"] == 2:
            return state, []
        legacy = deepcopy(state)
        indices = {}
        last_seq = 0
        # 每个分量均从初始状态追踪，不能只验证线索而将其他篡改状态迁入 v2。
        initial = _empty()
        components = ("working", "beliefs", "relationships", "emotion", "threads", "facts")
        previous = {name: deepcopy(initial[name]) for name in components}
        for entry in legacy["history"]:
            sid = entry.get("session_id", session_id)
            eid = entry.get("event_id")
            if not isinstance(sid, str) or not isinstance(eid, str):
                raise CognitiveTransactionError("旧历史缺少事件身份，拒绝迁移")
            if player_scope is not None:
                self._binding(sid, player_scope)
            elif sid != session_id:
                raise CognitiveTransactionError("旧历史引用了其他会话，拒绝迁移")
            event = self._log.get_event(sid, eid)
            if (not event or event["seq"] > snapshot["revision"]
                    or event["source"] != "player_cognitive_transaction"
                    or event["type"] not in {"cognitive.turn", "cognitive.thread_activation"}
                    or not isinstance(event["payload"], dict)
                    or event["payload"].get("player_scope") != player_scope
                    or event["payload"].get("source_event_id") != entry.get("source_event_id")
                    or event["caused_by"] != entry.get("source_event_id")):
                raise CognitiveTransactionError("旧历史与原始事件不一致，拒绝迁移")
            if event["seq"] <= last_seq:
                raise CognitiveTransactionError("旧历史事件顺序无效，拒绝迁移")
            last_seq = event["seq"]
            source = self._source(sid, entry["source_event_id"])
            if source["seq"] >= event["seq"] or event["payload"].get("source_seq") != source["seq"]:
                raise CognitiveTransactionError("旧历史来源无效，拒绝迁移")
            changes = entry.get("changes")
            if not isinstance(changes, dict):
                raise CognitiveTransactionError("旧历史 changes 无效，拒绝迁移")
            if (event["payload"].get("changes") != changes
                    and not _legacy_activation_matches(entry, event, source, sid)):
                raise CognitiveTransactionError("旧认知事件和历史不一致，拒绝迁移")
            if set(changes) - set(components):
                raise CognitiveTransactionError("旧历史包含未知状态分量，拒绝迁移")
            for name, change in changes.items():
                if (not isinstance(change, dict) or set(change) != {"before", "after"}
                        or change["before"] != previous[name]):
                    raise CognitiveTransactionError("旧认知状态变化链不连续，拒绝迁移")
                previous[name] = change["after"]
            tc = changes.get("threads")
            if tc is not None:
                if not isinstance(tc["before"], list) or not isinstance(tc["after"], list):
                    raise CognitiveTransactionError("旧线索历史无效，拒绝迁移")
                before = {item.get("thread_id"): item for item in tc["before"] if isinstance(item, dict)}
                after = {item.get("thread_id"): item for item in tc["after"] if isinstance(item, dict)}
                for tid in set(before) | set(after):
                    if not isinstance(tid, str) or not tid:
                        raise CognitiveTransactionError("旧线索缺少身份，拒绝迁移")
                    if before.get(tid) != after.get(tid):
                        prior_ref = indices.get(tid)
                        if event["type"] == "cognitive.thread_activation" and prior_ref:
                            prior = self._log.get_event(prior_ref["session_id"], prior_ref["event_id"])
                            if not prior or source["seq"] <= prior["seq"]:
                                raise CognitiveTransactionError("旧线索激活来源早于最后变更，拒绝迁移")
                        indices[tid] = {"session_id": sid, "event_id": eid}
        if last_seq != snapshot["revision"] or any(previous[name] != state[name] for name in components):
            raise CognitiveTransactionError("旧投影与事件历史不一致，拒绝迁移")
        if any(item.get("thread_id") not in indices for item in state["threads"]):
            raise CognitiveTransactionError("旧线索缺少精确变更事件，拒绝迁移")
        key = json.dumps([player_scope, session_id, snapshot["revision"]], ensure_ascii=True, separators=(",", ":"))
        archive_id = "cognitive-migration-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        archive = {"event_id": archive_id, "session_id": session_id, "timestamp": timestamp,
                   "type": "cognitive.migration_archive", "source": "player_cognitive_transaction",
                   "payload": {"player_scope": player_scope, "previous_revision": snapshot["revision"], "legacy_state": legacy}}
        state = deepcopy(state)
        del state["history"]
        state["version"] = 2
        state["thread_last_events"] = indices
        state["legacy_archive"] = {"session_id": session_id, "event_id": archive_id}
        _compact_state(state)
        return state, [archive]

    def _archive_history(self, state: dict, player_scope: str | None) -> list:
        """读取并校验迁移归档中的原始 history，供旧因果链接使用。"""
        ref = state.get("legacy_archive")
        if ref is None:
            return []
        if not isinstance(ref, dict) or set(ref) != {"session_id", "event_id"}:
            raise CognitiveTransactionError("迁移归档引用无效")
        event = self._log.get_event(ref["session_id"], ref["event_id"])
        if (not event or event["type"] != "cognitive.migration_archive"
                or event["source"] != "player_cognitive_transaction" or not isinstance(event["payload"], dict)
                or event["payload"].get("player_scope") != player_scope
                or not _state_valid(event["payload"].get("legacy_state"))
                or event["payload"]["legacy_state"].get("version") != 1):
            raise CognitiveTransactionError("迁移归档无效")
        archive = self._log.get_projection(ref["session_id"], "cognitive.legacy." + ref["event_id"],
                                           projection_scope=player_scope)
        if (archive is None or archive["state"] != event["payload"]["legacy_state"]
                or archive["revision"] <= event["seq"]
                or type(event["payload"].get("previous_revision")) is not int
                or event["payload"]["previous_revision"] >= event["seq"]):
            raise CognitiveTransactionError("迁移归档未与投影同事务提交")
        return event["payload"]["legacy_state"]["history"]

    def _source(self, session_id: str, event_id: str) -> dict:
        event = self._log.get_event(session_id, event_id)
        payload = event.get("payload") if event else None
        if (not event or event.get("type") != "experience.raw_observation"
                or event.get("source") != "player_experience" or not isinstance(payload, dict)
                or payload.get("kind") != "raw_observation" or payload.get("visibility") != "player"
                or payload.get("session_id") != session_id or payload.get("event_id") != event_id
                or not isinstance(payload.get("source_id"), str) or not payload["source_id"].strip()
                or not isinstance(payload.get("text"), str) or not payload["text"].strip()
                or payload.get("channel") not in {"visual", "audio", "dialogue", "choice", "feedback", "action_attempt"}):
            raise CognitiveTransactionError("来源必须是同一会话中已提交的玩家可见原始观察")
        # 普通 append 也能写同名事件；只有经历索引登记过的观察才是来源。
        registered, _ = self._log.experience_entries(session_id, event_id=event_id)
        if len(registered) != 1 or registered[0] != payload:
            raise CognitiveTransactionError("来源观察未登记或与经历索引不一致")
        return event

    def _committed_turn(self, session_id: str, player_scope: str | None, thought_id: str) -> tuple[dict | None, dict, dict]:
        """核验绑定、来源及同事务收据；旧版仅认可与原事件一致的历史。"""
        binding = self._binding(session_id, player_scope) if player_scope is not None else None
        snapshot = self.read(session_id, player_scope=player_scope)
        turn = self._log.get_event(session_id, thought_id)
        payload = turn.get("payload") if turn else None
        if (not turn or turn["type"] != "cognitive.turn" or turn["source"] != "player_cognitive_transaction"
                or not isinstance(payload, dict) or payload.get("player_scope") != player_scope
                or payload.get("visibility") != VISIBILITY or payload.get("verification") != VERIFICATION
                or not isinstance(payload.get("thought"), str) or not payload["thought"].strip()
                or not isinstance(payload.get("source_event_id"), str)
                or turn.get("caused_by") != payload["source_event_id"]
                or turn["seq"] > snapshot["revision"]
                or (binding is not None and binding["revision"] >= turn["seq"])):
            raise CognitiveTransactionError("thought 必须是该玩家会话已提交的认知轮次")
        source = self._source(session_id, payload["source_event_id"])
        state = snapshot["state"]
        if state["version"] == 1:
            committed = any(entry.get("event_id") == thought_id and entry.get("session_id", session_id) == session_id
                            and entry.get("source_event_id") == source["event_id"]
                            and entry.get("changes") == payload.get("changes") for entry in state["history"])
        else:
            receipt = self._log.get_projection(session_id, _receipt_name("turn", thought_id), projection_scope=player_scope)
            committed = bool(receipt and receipt["revision"] == turn["seq"] and receipt["state"] == {
                "event_id": thought_id, "session_id": session_id, "player_scope": player_scope,
                "source_event_id": source["event_id"]})
            if not committed:
                committed = any(entry.get("event_id") == thought_id and entry.get("session_id", session_id) == session_id
                                and entry.get("source_event_id") == source["event_id"]
                                and entry.get("changes") == payload.get("changes")
                                for entry in self._archive_history(state, player_scope))
        if source["seq"] != payload.get("source_seq") or source["seq"] >= turn["seq"] or not committed:
            raise CognitiveTransactionError("thought 的原始观察或提交历史无效")
        return binding, snapshot, turn

    def _intent_source(self, session_id: str, player_scope: str, thought_id: str) -> tuple[dict, dict, dict]:
        """意图只接受已绑定玩家的成功认知事务。"""
        binding, snapshot, turn = self._committed_turn(session_id, player_scope, thought_id)
        if binding is None:
            raise CognitiveTransactionError("意图必须属于已绑定的玩家")
        return binding, snapshot, turn

    def _speech_lock(self, player_scope: str, thought_id: str):
        """取得进程级独占锁；持有者退出时由操作系统释放。"""
        with self._log._lock:
            db_path = self._log._db.execute("PRAGMA database_list").fetchone()[2]
        identity = hashlib.sha256((player_scope + "\0" + thought_id).encode("utf-8")).hexdigest()
        path = Path(db_path).with_name(Path(db_path).name + ".speech-" + identity + ".lock")
        handle = open(path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
            return None

    def _release_speech_lock(self, key: tuple[str, str, str]) -> None:
        """释放本事务持有的投递锁。"""
        handle = self._live_speech_reservations.pop(key, None)
        self._live_speech_fields.pop(key, None)
        if handle is not None:
            handle.close()

    def begin_speech_delivery(self, *, session_id: str, player_scope: str, thought_id: str,
                              decision_id: str, binding: dict, speech: dict,
                              timestamp: str | None = None) -> dict:
        """Write one irreversible reservation before submitting a thought to the recorder."""
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        thought_id = _required(thought_id, "thought_id")
        decision_id = _required(decision_id, "decision_id")
        if len(decision_id) > 128 or len(thought_id) > 512:
            raise CognitiveTransactionError("speech 标识过长")
        if (not isinstance(binding, dict) or set(binding) != {"session_id", "instance"}
                or any(not isinstance(binding[key], str) or not binding[key].strip()
                       or len(binding[key]) > 512 for key in binding)):
            raise CognitiveTransactionError("录制会话绑定无效")
        allowed = {"type", "source", "text", "generated_at_ms", "thought_id", "intent_id", "frame_seq",
                   "emotion", "intensity", "pace", "volume", "pause_before_ms", "interruptible"}
        _fields(speech, allowed, "speech")
        if (speech.get("type") != "speech" or speech.get("source") != "persistent_player"
                or speech.get("thought_id") != thought_id or not isinstance(speech.get("text"), str)
                or not speech["text"].strip() or type(speech.get("generated_at_ms")) is not int
                or speech["generated_at_ms"] <= 0):
            raise CognitiveTransactionError("speech 事件无效")
        _check_input({"binding": binding, "speech": speech, "decision_id": decision_id})
        owner, cognitive, turn = self._intent_source(session_id, player_scope, thought_id)
        original = turn["payload"].get("speech")
        if (not isinstance(original, dict) or original.get("text") != speech["text"]
                or original.get("should_speak") is not True):
            raise CognitiveTransactionError("speech 文本与已提交的思考不一致")
        key = hashlib.sha256(thought_id.encode("utf-8")).hexdigest()
        name = "cognitive.speech." + key
        lock_key = (session_id, player_scope, thought_id)
        handle = self._speech_lock(player_scope, thought_id)
        try:
            current = self._log.get_projection(session_id, name, projection_scope=player_scope)
        except BaseException:
            if handle is not None:
                handle.close()
            raise
        fields = {"session_id": session_id, "player_scope": player_scope, "thought_id": thought_id,
                  "decision_id": decision_id, "binding": deepcopy(binding), "speech": deepcopy(speech)}
        if current is not None:
            try:
                state = current["state"]
                if not isinstance(state, dict) or any(state.get(k) != v for k, v in fields.items()):
                    raise CognitiveTransactionError("speech 已绑定其他内容或录制会话")
                created = self._log.get_event(session_id, state.get("created_event_id"))
                if (not created or created.get("type") != "cognitive.speech"
                        or created.get("source") != "player_cognitive_transaction"
                        or created.get("caused_by") != thought_id
                        or created.get("payload") != {**fields, "status": "created"}):
                    raise CognitiveTransactionError("speech 创建事件无效")
                status = state.get("status")
                if status == "created":
                    if current["revision"] != created["seq"]:
                        raise CognitiveTransactionError("speech 创建投影不一致")
                    if handle is not None:
                        self._live_speech_reservations[lock_key] = handle
                        self._live_speech_fields[lock_key] = deepcopy(fields)
                        handle = None
                        self.finish_speech_delivery(session_id=session_id, player_scope=player_scope,
                                                    thought_id=thought_id, decision_id=decision_id,
                                                    binding=binding, speech=speech, status="unknown")
                    return {"already_recorded": True, "status": "unknown"}
                result = self._log.get_event(session_id, state.get("result_event_id"))
                payload = result.get("payload") if result else None
                if (status not in {"timeline_written", "unknown"} or not result
                        or result.get("type") != "cognitive.speech"
                        or result.get("source") != "player_cognitive_transaction"
                        or result.get("caused_by") != created["event_id"] or result.get("seq") != current["revision"]
                        or not isinstance(payload, dict) or any(payload.get(k) != v for k, v in fields.items())
                        or payload.get("status") != status):
                    raise CognitiveTransactionError("speech 终态事件无效")
                receipt = payload.get("receipt")
                if ((status == "unknown" and receipt is not None)
                        or (status == "timeline_written" and (
                            not isinstance(receipt, dict) or type(receipt.get("t")) is not int or receipt["t"] < 0
                            or type(receipt.get("dur")) is not int or receipt["dur"] <= 0
                            or any(receipt.get(k) != speech[k] for k in
                                   ("type", "source", "text", "thought_id", "generated_at_ms", "intent_id", "frame_seq")
                                   if k in speech)))):
                    raise CognitiveTransactionError("speech 终态回执无效")
                return {"already_recorded": True, "status": status}
            finally:
                if handle is not None:
                    handle.close()
        if handle is None:
            return {"already_recorded": True, "status": "unknown"}
        self._live_speech_reservations[lock_key] = handle
        self._live_speech_fields[lock_key] = deepcopy(fields)
        created_id = "cognitive-speech-" + key + "-created"
        try:
            decision_name = "cognitive.speech-decision." + hashlib.sha256(decision_id.encode("utf-8")).hexdigest()
            if self._log.get_projection(session_id, decision_name, projection_scope=player_scope) is not None:
                raise CognitiveTransactionError("speech decision 已绑定其他思考或会话")
            event = {"event_id": created_id, "session_id": session_id,
                     "timestamp": timestamp or utc_now_iso(int(time.time() * 1000)),
                     "type": "cognitive.speech", "source": "player_cognitive_transaction",
                     "caused_by": thought_id, "payload": {**fields, "status": "created"}}
            state = {"version": 1, **fields, "status": "created", "created_event_id": created_id}
            seq = self._log.append([event], projections={name: state, decision_name: {
                                       "session_id": session_id, "thought_id": thought_id,
                                       "decision_id": decision_id, "created_event_id": created_id}},
                                   expected_revisions={name: 0, decision_name: 0},
                                   projection_scope=player_scope,
                                   expected_session_revisions={SESSION_BINDING: owner["revision"]},
                                   checked_scope=player_scope, expected_scoped_revisions={PROJECTION: cognitive["revision"]})[0]
        except BaseException:
            self._release_speech_lock(lock_key)
            raise
        return {"already_recorded": False, "status": "created", "event_id": created_id, "seq": seq}

    def finish_speech_delivery(self, *, session_id: str, player_scope: str, thought_id: str,
                               decision_id: str, binding: dict, speech: dict, status: str,
                               receipt: dict | None = None, timestamp: str | None = None) -> dict:
        """完成持有的投递；所有结果不确定的异常路径均释放独占锁。"""
        lock_key = (session_id, player_scope, thought_id)
        fields = {"session_id": session_id, "player_scope": player_scope, "thought_id": thought_id,
                  "decision_id": decision_id, "binding": binding, "speech": speech}
        owns = lock_key in self._live_speech_reservations and self._live_speech_fields.get(lock_key) == fields
        try:
            return self._finish_speech_delivery(session_id=session_id, player_scope=player_scope,
                thought_id=thought_id, decision_id=decision_id, binding=binding, speech=speech,
                status=status, receipt=receipt, timestamp=timestamp)
        finally:
            if owns:
                self._release_speech_lock(lock_key)

    def _finish_speech_delivery(self, *, session_id: str, player_scope: str, thought_id: str,
                                decision_id: str, binding: dict, speech: dict, status: str,
                                receipt: dict | None = None, timestamp: str | None = None) -> dict:
        """核验并原子追加投递终态；未知结果绝不触发重发。"""
        if status not in {"timeline_written", "unknown"}:
            raise CognitiveTransactionError("speech 状态无效")
        owner, cognitive, _ = self._intent_source(session_id, player_scope, thought_id)
        key = hashlib.sha256(thought_id.encode("utf-8")).hexdigest()
        name = "cognitive.speech." + key
        current = self._log.get_projection(session_id, name, projection_scope=player_scope)
        state = deepcopy(current["state"]) if current else None
        fields = {"session_id": session_id, "player_scope": player_scope, "thought_id": thought_id,
                  "decision_id": decision_id, "binding": binding, "speech": speech}
        if not isinstance(state, dict) or any(state.get(k) != v for k, v in fields.items()):
            raise CognitiveTransactionError("speech 创建记录或绑定不匹配")
        created = self._log.get_event(session_id, state.get("created_event_id"))
        if (not created or created.get("type") != "cognitive.speech"
                or created.get("source") != "player_cognitive_transaction" or created.get("caused_by") != thought_id
                or created.get("payload") != {**fields, "status": "created"}):
            raise CognitiveTransactionError("speech 创建事件无效")
        if state.get("status") != "created":
            prior = self._log.get_event(session_id, state.get("result_event_id"))
            payload = prior.get("payload") if prior else None
            if (not prior or prior.get("type") != "cognitive.speech"
                    or prior.get("source") != "player_cognitive_transaction"
                    or prior.get("caused_by") != created["event_id"] or prior.get("seq") != current["revision"]
                    or not isinstance(payload, dict) or any(payload.get(k) != v for k, v in fields.items())
                    or payload.get("status") != state.get("status")):
                raise CognitiveTransactionError("speech 终态事件无效")
            if state.get("status") == status:
                if status == "timeline_written" and (not isinstance(receipt, dict)
                        or type(receipt.get("t")) is not int or receipt["t"] < 0
                        or type(receipt.get("dur")) is not int or receipt["dur"] <= 0
                        or any(receipt.get(k) != value for k, value in speech.items())
                        or not isinstance(payload.get("receipt"), dict)
                        or any(receipt.get(k) != v for k, v in payload["receipt"].items())):
                    raise CognitiveTransactionError("speech 回执与原始事件不一致")
                return {"already_recorded": True, "status": status}
            raise CognitiveTransactionError("speech 状态已终结，不能重试或改写")
        if current["revision"] != created["seq"]:
            raise CognitiveTransactionError("speech 创建投影不一致")
        lock_key = (session_id, player_scope, thought_id)
        if lock_key not in self._live_speech_reservations:
            raise CognitiveTransactionError("speech 创建结果已过期或跨进程，非持有者不能终结")
        if status == "timeline_written":
            if (not isinstance(receipt, dict) or type(receipt.get("t")) is not int or receipt["t"] < 0
                    or type(receipt.get("dur")) is not int or receipt["dur"] <= 0
                    or any(receipt.get(k) != value for k, value in speech.items())
                    or receipt.get("type") != "speech" or receipt.get("source") != "persistent_player"):
                raise CognitiveTransactionError("speech 回执与原始事件不一致")
            saved_receipt = {k: receipt[k] for k in ("type", "source", "text", "thought_id", "intent_id",
                            "generated_at_ms", "frame_seq", "t", "dur") if k in receipt}
        else:
            if receipt is not None:
                raise CognitiveTransactionError("未知结果不能带成功回执")
            saved_receipt = None
        event_id = "cognitive-speech-" + key + "-" + status
        payload = {**fields, "status": status, "receipt": saved_receipt}
        event = {"event_id": event_id, "session_id": session_id,
                 "timestamp": timestamp or utc_now_iso(int(time.time() * 1000)),
                 "type": "cognitive.speech", "source": "player_cognitive_transaction",
                 "caused_by": created["event_id"], "payload": payload}
        state.update(status=status, result_event_id=event_id)
        seq = self._log.append([event], projections={name: state}, expected_revisions={name: current["revision"]},
                               projection_scope=player_scope,
                               expected_session_revisions={SESSION_BINDING: owner["revision"]},
                               checked_scope=player_scope, expected_scoped_revisions={PROJECTION: cognitive["revision"]})[0]
        return {"already_recorded": False, "status": status, "event_id": event_id, "seq": seq}

    def recover_speech_deliveries(self, *, session_id: str, player_scope: str) -> dict:
        """逐条尝试独占恢复；锁被占用的投递仍可能在另一进程执行。"""
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        recovered, active = 0, 0
        after = 0
        while True:
            rows = self._log.list_events(session_id, after_seq=after, limit=1000)
            if not rows:
                break
            for row in rows:
                after = row["seq"]
                payload = row.get("payload")
                if (row.get("type") != "cognitive.speech" or row.get("source") != "player_cognitive_transaction"
                        or not isinstance(payload, dict) or payload.get("status") != "created"
                        or payload.get("player_scope") != player_scope):
                    continue
                args = {key: payload[key] for key in ("session_id", "player_scope", "thought_id", "decision_id",
                                                       "binding", "speech")}
                result = self.begin_speech_delivery(**args)
                projection = self._log.get_projection(session_id,
                    "cognitive.speech." + hashlib.sha256(payload["thought_id"].encode("utf-8")).hexdigest(),
                    projection_scope=player_scope)
                if result["status"] == "unknown" and projection["state"]["status"] == "unknown":
                    recovered += 1
                elif projection["state"]["status"] == "created":
                    active += 1
        return {"unknown": recovered, "active": active}

    def recover_player_speech_deliveries(self, *, session_prefix: str, player_scope: str) -> dict:
        """恢复该玩家所有旧会话的孤儿投递，不影响仍持锁的活跃投递。"""
        player_scope = validate_projection_scope(player_scope)
        sessions, after = set(), 0
        while True:
            rows = self._log.query_events(session_prefix=session_prefix, event_types=["cognitive.speech"],
                                          after_seq=after, limit=200)
            if not rows:
                break
            for row in rows:
                after = row["seq"]
                if (row.get("source") == "player_cognitive_transaction"
                        and row.get("payload", {}).get("player_scope") == player_scope
                        and row["payload"].get("status") == "created"):
                    sessions.add(row["session_id"])
        result = {"unknown": 0, "active": 0}
        for session_id in sessions:
            counts = self.recover_speech_deliveries(session_id=session_id, player_scope=player_scope)
            result = {key: result[key] + counts[key] for key in result}
        return result

    def append_intent(self, *, session_id: str, player_scope: str, thought_id: str,
                      intent_id: str, attribution: dict, status: str,
                      timestamp: str | None = None) -> dict:
        """记录意图生命周期事实；重复调用返回现有状态，绝不回退状态。

        created 可无代际，sent 必须含桥返回的 motorGeneration；动作组如有
        必须与创建时一致。后续 cancelled/failed 使用当前归属。sent 表示
        发布被接受，不表示游戏执行。CAS 冲突需由调用者重读并重试。
        """
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        thought_id = _required(thought_id, "thought_id")
        intent_id = _required(intent_id, "intent_id")
        if not isinstance(status, str) or status not in {"created", "sent", "cancelled", "failed"}:
            raise CognitiveTransactionError("intent status 无效")
        attribution = _attribution(attribution, require_generation=status == "sent")
        binding, cognitive, turn = self._intent_source(session_id, player_scope, thought_id)
        try:
            key = _intent_key(intent_id)
        except UnicodeError as exc:
            raise CognitiveTransactionError("intent_id 必须是有效 UTF-8") from exc
        name = "cognitive.intent." + key
        current = self._log.get_projection(session_id, name)
        state = deepcopy(current["state"]) if current else None
        if state is not None:
            if (not isinstance(state, dict) or state.get("intent_id") != intent_id
                    or state.get("thought_id") != thought_id or state.get("player_scope") != player_scope
                    or state.get("session_id") != session_id or not isinstance(state.get("facts"), dict)):
                raise CognitiveTransactionError("意图归属冲突或投影无效")
            if status in state["facts"]:
                fact = state["facts"][status]
                if not isinstance(fact, dict) or fact.get("attribution") != attribution:
                    raise CognitiveTransactionError("重复意图状态的归属不一致")
                prior = self._log.get_event(session_id, fact.get("event_id"))
                if (not prior or prior["type"] != "cognitive.intent" or prior["source"] != "player_cognitive_transaction"
                        or prior["caused_by"] != thought_id or prior["intent_id"] != intent_id
                        or prior["payload"].get("status") != status
                        or prior["payload"].get("attribution") != attribution):
                    raise CognitiveTransactionError("已记录意图事件无效")
                return {"event_id": fact["event_id"], "seq": prior["seq"],
                        "revision": current["revision"], "state": state, "already_recorded": True}
            if status not in ({"sent", "cancelled", "failed"} if state.get("status") == "created"
                              else {"cancelled", "failed"} if state.get("status") == "sent" else set()):
                raise CognitiveTransactionError("意图状态转换无效")
            previous = state["attribution"]
            if status == "sent":
                if (previous.get("actionGroupId") != attribution.get("actionGroupId")
                        or ("motorGeneration" in previous and previous["motorGeneration"] != attribution["motorGeneration"])):
                    raise CognitiveTransactionError("意图发布归属与创建时不一致")
            elif previous != attribution:
                raise CognitiveTransactionError("意图状态归属不一致")
        else:
            if status != "created":
                raise CognitiveTransactionError("意图必须先创建")
            state = {"version": 1, "session_id": session_id, "player_scope": player_scope,
                     "thought_id": thought_id, "intent_id": intent_id, "facts": {}}

        event_id = "cognitive-intent-" + key + "-" + status
        timestamp = _required(timestamp if timestamp is not None else utc_now_iso(int(time.time() * 1000)), "timestamp")
        payload = {"player_scope": player_scope, "thought_id": thought_id,
                   "source_event_id": turn["payload"]["source_event_id"], "intent_id": intent_id,
                   "attribution": attribution, "status": status, "game_execution": "unknown"}
        event = {"event_id": event_id, "session_id": session_id, "timestamp": timestamp,
                 "type": "cognitive.intent", "source": "player_cognitive_transaction", "payload": payload,
                 "caused_by": thought_id, "intent_id": intent_id}
        state.update(status=status, attribution=deepcopy(attribution))
        # seq 由 SQLite 分配；以事件 ID 查回，不在投影中预猜序号。
        state["facts"][status] = {"event_id": event_id, "attribution": deepcopy(attribution)}
        projections = {name: state}
        expected = {name: current["revision"] if current else 0}
        if status == "sent":
            motor = self._log.get_projection(session_id, MOTOR_PROJECTION)
            if motor and (not isinstance(motor["state"], dict) or type(motor["state"].get("motorGeneration")) is not int):
                raise CognitiveTransactionError("motor 代际投影无效")
            if motor and attribution["motorGeneration"] <= motor["state"]["motorGeneration"]:
                raise CognitiveTransactionError("motor 代际已过期")
            projections[MOTOR_PROJECTION] = {"intent_id": intent_id, **attribution}
            expected[MOTOR_PROJECTION] = motor["revision"] if motor else 0
        seq = self._log.append([event], projections=projections, expected_revisions=expected,
                               expected_session_revisions={SESSION_BINDING: binding["revision"]},
                               checked_scope=player_scope, expected_scoped_revisions={PROJECTION: cognitive["revision"]})[0]
        return {"event_id": event_id, "seq": seq, "revision": seq, "state": deepcopy(state), "already_recorded": False}

    def append_motor_feedback(self, *, session_id: str, player_scope: str, intent_id: str,
                              feedback: dict, event_id: str, timestamp: str | None = None) -> dict:
        """记录已匹配当前已发送意图的反馈；调用方为每条反馈提供稳定 event_id。"""
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        intent_id = _required(intent_id, "intent_id")
        event_id = _required(event_id, "event_id")
        if not isinstance(feedback, dict) or set(feedback) - {"type", "intent_id", "status", "reason", "observation", "t", "motorGeneration", "actionGroupId"}:
            raise CognitiveTransactionError("motor feedback 字段无效")
        if (feedback.get("intent_id") != intent_id or feedback.get("type", "motor_feedback") != "motor_feedback"
                or not isinstance(feedback.get("status"), str)
                or feedback["status"] not in {"progress", "done", "blocked", "failed", "cancelled"}):
            raise CognitiveTransactionError("motor feedback 意图或状态无效")
        attribution = _attribution({k: feedback[k] for k in ("motorGeneration", "actionGroupId") if k in feedback}, require_generation=True)
        name = "cognitive.intent." + _intent_key(intent_id)
        current = self._log.get_projection(session_id, name)
        state = current["state"] if current else None
        if (not isinstance(state, dict) or state.get("intent_id") != intent_id
                or state.get("player_scope") != player_scope or state.get("session_id") != session_id
                or not isinstance(state.get("thought_id"), str)):
            raise CognitiveTransactionError("motor feedback 没有匹配的意图来源")
        binding, cognitive, turn = self._intent_source(session_id, player_scope, state["thought_id"])
        sent = state.get("facts", {}).get("sent")
        if not isinstance(sent, dict) or sent.get("attribution") != attribution:
            raise CognitiveTransactionError("motor feedback 与已发送意图归属不匹配")
        sent_event = self._log.get_event(session_id, sent.get("event_id"))
        if (not sent_event or sent_event["type"] != "cognitive.intent" or sent_event["intent_id"] != intent_id
                or sent_event["caused_by"] != state["thought_id"] or sent_event["payload"].get("status") != "sent"
                or sent_event["payload"].get("attribution") != attribution):
            raise CognitiveTransactionError("已发送意图事件无效")
        payload = {"player_scope": player_scope, "thought_id": state["thought_id"],
                   "source_event_id": turn["payload"]["source_event_id"], "intent_id": intent_id,
                   "attribution": attribution, "feedback": deepcopy(feedback), "game_execution": "unknown"}
        motor = self._log.get_projection(session_id, MOTOR_PROJECTION)
        if not motor or motor["state"] != {"intent_id": intent_id, **attribution}:
            raise CognitiveTransactionError("motor feedback 属于旧代际或未匹配的意图")
        existing = self._log.get_event(session_id, event_id)
        if existing:
            if (existing["type"] != "cognitive.motor_feedback" or existing["source"] != "player_cognitive_transaction"
                    or existing["caused_by"] != sent["event_id"] or existing["payload"] != payload):
                raise CognitiveTransactionError("feedback event_id 已用于不同事件")
            return {"event_id": event_id, "seq": existing["seq"], "already_recorded": True}
        if state.get("status") != "sent":
            raise CognitiveTransactionError("motor feedback 属于已结束的意图")
        timestamp = _required(timestamp if timestamp is not None else utc_now_iso(int(time.time() * 1000)), "timestamp")
        event = {"event_id": event_id, "session_id": session_id, "timestamp": timestamp,
                 "type": "cognitive.motor_feedback", "source": "player_cognitive_transaction", "payload": payload,
                 "caused_by": sent["event_id"], "intent_id": intent_id}
        seq = self._log.append([event], expected_session_revisions={SESSION_BINDING: binding["revision"],
                               name: current["revision"], MOTOR_PROJECTION: motor["revision"]},
                               checked_scope=player_scope, expected_scoped_revisions={PROJECTION: cognitive["revision"]})[0]
        return {"event_id": event_id, "seq": seq, "already_recorded": False}

    def _thread_last_change_seq(self, state: dict, thread_id: str, session_id: str,
                                player_scope: str | None = None) -> int:
        """按已提交历史确定线索最后改变的序号，避免旧观察激活新近休眠线索。"""
        if state["version"] == 2:
            ref = state["thread_last_events"].get(thread_id)
            if not isinstance(ref, dict) or set(ref) != {"session_id", "event_id"}:
                raise CognitiveTransactionError("线索缺少变更事件索引")
            event = self._log.get_event(ref["session_id"], ref["event_id"])
            if event and event["type"] == "cognitive.legacy_import":
                # 旧 JSON 没有可核验的变更事件；迁移事务本身是唯一可信时间边界。
                payload = event.get("payload")
                receipt = self._log.get_projection(ref["session_id"], "cognitive.legacy_import.v1",
                                                   projection_scope=player_scope) if player_scope else None
                if (not player_scope or not isinstance(payload, dict)
                        or event["source"] != "player_legacy_migration"
                        or payload.get("player_scope") != player_scope
                        or not isinstance(payload.get("thread_ids"), list)
                        or thread_id not in payload["thread_ids"]
                        or not receipt or receipt["revision"] != event["seq"]
                        or receipt["state"] != {"event_id": event["event_id"],
                                                "session_id": ref["session_id"],
                                                "player_scope": player_scope,
                                                "digest": payload.get("digest")}):
                    raise CognitiveTransactionError("旧线索迁移事件无效")
                self._binding(ref["session_id"], player_scope)
            elif not event or event["type"] not in {"cognitive.turn", "cognitive.thread_activation", "cognitive.reflection_applied"} or event["source"] != "player_cognitive_transaction":
                raise CognitiveTransactionError("线索变更事件无效")
            if event["type"] == "cognitive.reflection_applied":
                receipt = self._log.get_projection(ref["session_id"], _receipt_name("reflection", event["event_id"]),
                                                   projection_scope=player_scope)
                if (not isinstance(event.get("payload"), dict) or not receipt or receipt["revision"] != event["seq"]
                        or receipt["state"] != {"event_id": event["event_id"], "session_id": ref["session_id"],
                                               "player_scope": player_scope,
                                               "reflection_id": event["payload"].get("reflection_id")}):
                    raise CognitiveTransactionError("线索反思提交收据无效")
            return event["seq"]
        for entry in reversed(state["history"]):
            changes = entry.get("changes", {})
            if not isinstance(changes, dict):
                raise CognitiveTransactionError("认知状态历史无效")
            change = changes.get("threads")
            if change is None:
                continue
            if (not isinstance(change, dict) or not isinstance(change.get("before"), list)
                    or not isinstance(change.get("after"), list)):
                raise CognitiveTransactionError("线索状态历史无效")
            def select(items: list) -> Any:
                return next((item for item in items if isinstance(item, dict) and item.get("thread_id") == thread_id), None)
            if select(change["before"]) == select(change["after"]):
                continue
            sid = entry.get("session_id", session_id)  # 旧 session 快照无此字段。
            eid = entry.get("event_id")
            if not isinstance(sid, str) or not isinstance(eid, str):
                raise CognitiveTransactionError("线索状态历史缺少事件来源")
            event = self._log.get_event(sid, eid)
            if not event:
                raise CognitiveTransactionError("线索状态历史的事件不存在")
            return event["seq"]
        return 0

    def activate_dormant_threads(self, *, session_id: str, player_scope: str,
                                 source_event_id: str, timestamp: str | None = None,
                                 now_ms: int | None = None,
                                 expected_revision: int | None = None) -> dict:
        """用已提交的可见原始观察激活休眠线索，返回状态、修订和激活结果。

        session 须先绑定 player_scope。仅匹配观察原文中完整的非通用实体名、
        标题或主题；不使用推测、语义相似度或 action_attempt。每次最多激活
        八条；即使零匹配也记录该来源已处理，之后重复调用不会重新激活。
        检查来源序号晚于各线索最后变化。CAS 冲突由调用方重新读取并重试；
        相同来源在冲突后重试返回 already_processed=True 而不再次写入。
        """
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        source_event_id = _required(source_event_id, "source_event_id")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise CognitiveTransactionError("expected_revision 必须是非负整数")
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if type(now_ms) is not int or now_ms < 0:
            raise CognitiveTransactionError("now_ms 必须是非负整数")
        timestamp = _required(timestamp if timestamp is not None else utc_now_iso(now_ms), "timestamp")
        binding = self._binding(session_id, player_scope)
        source = self._source(session_id, source_event_id)
        if source["payload"]["channel"] == "action_attempt":
            raise CognitiveTransactionError("动作尝试不能作为观察证据")
        snap = self.read(session_id, player_scope=player_scope)
        if expected_revision is not None and expected_revision != snap["revision"]:
            raise EventLogConflict("状态投影版本冲突")
        state = snap["state"]
        # 派生固定 ID，重放时无需调用方提供额外的去重键。
        key = json.dumps([player_scope, session_id, source_event_id], ensure_ascii=True, separators=(",", ":"))
        event_id = "cognitive-activation-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        receipt_name = _receipt_name("activation", event_id)
        receipt = self._log.get_projection(session_id, receipt_name, projection_scope=player_scope)
        prior = self._log.get_event(session_id, event_id)
        legacy_history = (state["history"] if state["version"] == 1 else self._archive_history(state, player_scope)) if prior and not receipt else []
        recorded = bool(receipt) or any(entry.get("event_id") == event_id
                                        and entry.get("session_id", session_id) == session_id
                                        and entry.get("source_event_id") == source_event_id
                                        and prior and isinstance(prior.get("payload"), dict)
                                        and (entry.get("changes") == prior["payload"].get("changes")
                                             or _legacy_activation_matches(entry, prior, source, session_id))
                                        for entry in legacy_history)
        if recorded:
            if state["version"] == 1 and prior and "changes" not in prior["payload"]:
                self._prepare_state(snap, session_id, player_scope, timestamp)
            if (not prior or prior["type"] != "cognitive.thread_activation"
                    or prior["source"] != "player_cognitive_transaction" or not isinstance(prior["payload"], dict)
                    or prior["payload"].get("player_scope") != player_scope
                    or prior["payload"].get("source_event_id") != source_event_id
                    or prior.get("caused_by") != source_event_id
                    or prior["payload"].get("source_seq") != source["seq"]
                    or not source["seq"] < prior["seq"] <= snap["revision"]
                    or binding["revision"] >= prior["seq"]
                    or (receipt and (receipt["revision"] != prior["seq"] or receipt["state"] != {
                        "event_id": event_id, "session_id": session_id, "player_scope": player_scope,
                        "source_event_id": source_event_id}))):
                raise CognitiveTransactionError("已处理观察的激活事件无效")
            return {"event_id": event_id, "seq": prior["seq"], "revision": snap["revision"],
                    "state": deepcopy(state), "activated": deepcopy(prior["payload"]["activated"]),
                    "already_processed": True}
        state, migration = self._prepare_state(snap, session_id, player_scope, timestamp)
        text = threads._text(source["payload"]["text"], len(source["payload"]["text"])).lower()
        before = deepcopy(state["threads"])
        activated = []
        for thread in state["threads"]:
            if thread.get("status") != "dormant":
                continue
            thread_id = _required(thread.get("thread_id"), "thread_id")
            if source["seq"] <= self._thread_last_change_seq(state, thread_id, session_id, player_scope):
                continue
            reason = ""
            for entity in thread.get("characters", []):
                candidate = threads._specific(entity) if isinstance(entity, str) and len(entity) <= 120 else ""
                if threads._contains_topic(text, candidate):
                    reason = "entity:" + candidate
                    break
            if not reason:
                for topic in [thread.get("title"), *thread.get("topics", [])]:
                    candidate = threads._specific(topic, topic=True) if isinstance(topic, str) and len(topic) <= 120 else ""
                    if threads._contains_topic(text, candidate):
                        reason = "topic:" + candidate
                        break
            if not reason:
                continue
            _clear_field_sources(thread, ("status",))
            threads._record_status(thread, "dormant", "open", now_ms, reason=reason, event_id=source_event_id)
            thread["status"] = "open"
            thread["last_referenced_ms"] = now_ms
            thread["last_activation"] = {"event_id": source_event_id, "session_id": session_id,
                                         "reason": reason, "time_ms": now_ms}
            thread.setdefault("source_event_ids", []).append(source_event_id)
            activated.append({"thread_id": thread_id, "reason": reason})
            if len(activated) == threads._MAX_EVENT_ACTIVATIONS:
                break
        changes = {"threads": {"before": before, "after": deepcopy(state["threads"])}} if activated else {}
        for item in activated:
            state["thread_last_events"][item["thread_id"]] = {"session_id": session_id, "event_id": event_id}
        _compact_state(state)
        payload = {"visibility": VISIBILITY, "verification": VERIFICATION,
                   "player_scope": player_scope, "source_event_id": source_event_id,
                   "source_seq": source["seq"], "activated": activated, "changes": changes}
        event = {"event_id": event_id, "session_id": session_id, "timestamp": timestamp,
                 "frame_seq": source.get("frame_seq"), "type": "cognitive.thread_activation",
                 "payload": payload, "source": "player_cognitive_transaction", "caused_by": source_event_id}
        receipt_state = {"event_id": event_id, "session_id": session_id, "player_scope": player_scope,
                         "source_event_id": source_event_id}
        legacy_projection = _migration_projection(migration)
        seq = self._log.append([*migration, event], projections={PROJECTION: state, receipt_name: receipt_state, **legacy_projection},
                               projection_scope=player_scope, expected_revisions={PROJECTION: snap["revision"], receipt_name: 0,
                                                                                **{name: 0 for name in legacy_projection}},
                               expected_session_revisions={SESSION_BINDING: binding["revision"]})[-1]
        return {"event_id": event_id, "seq": seq, "revision": seq, "state": deepcopy(state),
                "activated": deepcopy(activated), "already_processed": False}

    def apply_reflection(self, *, session_id: str, player_scope: str, reflection_id: str,
                         now_ms: int | None = None) -> dict:
        """将同一玩家已提交的有来源反思提案一次性并入主观状态。"""
        try:
            from .player_experience import PlayerExperience
            from .player_reflection import _MAX_SOURCES, _completion_name, _parse, _reflection_id, _unique_object
        except ImportError:
            from player_experience import PlayerExperience
            from player_reflection import _MAX_SOURCES, _completion_name, _parse, _reflection_id, _unique_object
        session_id = _required(session_id, "session_id")
        player_scope = validate_projection_scope(player_scope)
        reflection_id = _required(reflection_id, "reflection_id")
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if type(now_ms) is not int or now_ms < 0:
            raise CognitiveTransactionError("now_ms 必须是非负整数")
        binding = self._binding(session_id, player_scope)
        snapshot = self.read(session_id, player_scope=player_scope)
        key = json.dumps([player_scope, session_id, reflection_id], ensure_ascii=True, separators=(",", ":"))
        event_id = "cognitive-reflection-" + hashlib.sha256(key.encode("ascii")).hexdigest()
        receipt_name = _receipt_name("reflection", event_id)
        existing = self._log.get_projection(session_id, receipt_name, projection_scope=player_scope)
        if existing is not None:
            prior = self._log.get_event(session_id, event_id)
            expected = {"event_id": event_id, "session_id": session_id, "player_scope": player_scope,
                        "reflection_id": reflection_id}
            if (existing["state"] != expected or prior is None or existing["revision"] != prior["seq"]
                    or prior["type"] != "cognitive.reflection_applied"
                    or prior["source"] != "player_cognitive_transaction" or prior["caused_by"] != reflection_id
                    or not isinstance(prior.get("payload"), dict)
                    or prior["payload"].get("reflection_id") != reflection_id
                    or prior["payload"].get("player_scope") != player_scope
                    or prior["seq"] > snapshot["revision"]):
                raise CognitiveTransactionError("反思提交收据无效")
            original = self._log.get_event(session_id, reflection_id)
            try:
                original_doc = json.loads(original["payload"]["text"], object_pairs_hook=_unique_object)
            except (ValueError, TypeError, KeyError) as exc:
                raise CognitiveTransactionError("原反思内容无效") from exc
            if not isinstance(original_doc, dict):
                raise CognitiveTransactionError("原反思内容无效")
            if (original_doc.get("version") == 2 and not PlayerExperience(self._log)._reflection_completed(
                    session_id, prior["payload"].get("conversation_event_id"), player_scope)):
                raise CognitiveTransactionError("反思完成收据缺失")
            return {"status": "already_applied", "event_id": event_id, "state": snapshot["state"]}
        reflection = self._log.get_event(session_id, reflection_id)
        if (not reflection or reflection["type"] != "experience.reflection"
                or reflection["source"] != "player_experience" or not isinstance(reflection.get("payload"), dict)):
            raise CognitiveTransactionError("反思事件不存在")
        try:
            document = json.loads(reflection["payload"]["text"], object_pairs_hook=_unique_object)
            conversation_id = document["conversation_event_id"]
            versioned = document.get("version") == 2
            revision = document.get("cognitive_revision") if versioned else None
            if versioned and (document.get("player_scope") != player_scope
                              or type(revision) is not int or revision < 0):
                raise CognitiveTransactionError("反思版本或玩家归属无效")
            conversation = self._log.get_event(session_id, conversation_id)
            experience = PlayerExperience(self._log)
            sources = experience._pending_conversation_sources(conversation)
            ordered = sorted(sources.values(), key=lambda source: source["seq"])
            total = (len(ordered) + _MAX_SOURCES - 1) // _MAX_SOURCES
            if reflection_id != _reflection_id(session_id, conversation_id, 0, total,
                                                player_scope=player_scope if versioned else None,
                                                cognitive_revision=revision):
                raise CognitiveTransactionError("反思身份与对话首批不一致")
            if not experience._has_conversation_reflection(conversation, sources,
                    player_scope=player_scope if versioned else None, cognitive_revision=revision):
                raise CognitiveTransactionError("反思与对话来源不一致")
            documents = []
            items = []
            for index in range(total):
                batch_id = _reflection_id(session_id, conversation_id, index, total,
                                          player_scope=player_scope if versioned else None,
                                          cognitive_revision=revision)
                batch = self._log.get_event(session_id, batch_id)
                batch_document = json.loads(batch["payload"]["text"], object_pairs_hook=_unique_object)
                selected = {row["event_id"] for row in ordered[index * _MAX_SOURCES:(index + 1) * _MAX_SOURCES]}
                parsed = _parse(json.dumps({"status": "ok", "items": batch_document["items"]}, ensure_ascii=False), selected)
                if parsed is None:
                    raise CognitiveTransactionError("反思批次提案无效")
                documents.append(batch_document)
                items.extend(parsed)
            if any(batch.get("cognitive_revision") != document.get("cognitive_revision") for batch in documents):
                raise CognitiveTransactionError("反思批次认知版本不一致")
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CognitiveTransactionError("反思来源或内容无效") from exc
        if items is None:
            raise CognitiveTransactionError("反思提案格式无效")
        proposals = [item for item in items if "update" in item]
        completion_name = _completion_name(session_id, conversation_id)
        if self._log.get_projection(session_id, completion_name, projection_scope=player_scope) is not None:
            if not experience._reflection_completed(session_id, conversation_id, player_scope):
                raise CognitiveTransactionError("反思完成收据无效")
            return {"status": "already_applied", "state": snapshot["state"]}
        if versioned and experience._has_conversation_reflection(conversation, sources):
            if experience._legacy_reflection_completed(conversation, sources, player_scope):
                return {"status": "already_applied", "state": snapshot["state"]}
        if not proposals and not versioned:
            return {"status": "no_updates", "state": snapshot["state"]}
        expected_revision = document.get("cognitive_revision")
        if type(expected_revision) is not int or expected_revision < 0:
            raise CognitiveTransactionError("反思缺少生成时的认知版本")
        if snapshot["revision"] != expected_revision:
            raise EventLogConflict("反思生成期间认知状态已变化")
        state, migration = self._prepare_state(snapshot, session_id, player_scope, utc_now_iso(now_ms))
        before = deepcopy(state)
        combined = {}
        for item in proposals:
            kind, update = item["kind"], item["update"]
            if kind == "belief_proposal":
                identity = (kind, beliefs.normalize_belief(update, now_ms=now_ms)["fingerprint"])
                patch = {"beliefs": [update]}
            elif kind == "relationship_proposal":
                identity = (kind, relationships._identity(update["character"]))
                observed = [sources[ref]["payload"] for ref in item["source_event_ids"]]
                if not any(identity[1] in relationships._identity(str(row.get(field) or ""))
                           for row in observed for field in ("speaker", "text")):
                    raise CognitiveTransactionError("关系角色未出现在引用的观察中")
                patch = {"relationships": [update]}
            elif kind == "emotion_proposal":
                identity = (kind, "emotion")
                patch = {"emotion": update}
            elif kind == "thread_proposal":
                identity = (kind, threads.normalize_thread(update, now_ms=now_ms)["fingerprint"])
                patch = {"threads": [update]}
            else:
                raise CognitiveTransactionError("反思更新类型无效")
            component, values = next(iter(patch.items()))
            values = values[0] if isinstance(values, list) else values
            previous, _, field_sources = combined.get(identity, ({}, "", {}))
            field_sources.update({field: [{"session_id": session_id, "event_id": source_id}
                                           for source_id in item["source_event_ids"]]
                                  for field in values})
            combined[identity] = ({**previous, **values}, item["source_event_ids"][0], field_sources)
        for identity, (update, source_id, field_sources) in combined.items():
            component = {"belief_proposal": "beliefs", "relationship_proposal": "relationships",
                         "emotion_proposal": "emotion", "thread_proposal": "threads"}[identity[0]]
            self._apply(state, {component: update if component == "emotion" else [update]}, source_id, now_ms)
            if component == "emotion":
                record = state[component]
            elif component == "relationships":
                record = next(row for row in state[component] if relationships._identity(row["character"]) == identity[1])
            else:
                record = next(row for row in state[component] if row["fingerprint"] == identity[1])
            record.setdefault("field_sources", {}).update(field_sources)
        changes = {name: {"before": before[name], "after": deepcopy(state[name])}
                   for name in ("working", "beliefs", "relationships", "emotion", "threads", "facts")
                   if before[name] != state[name]}
        old_threads = {item["thread_id"]: item for item in before["threads"]}
        for thread in state["threads"]:
            if old_threads.get(thread["thread_id"]) != thread:
                state["thread_last_events"][thread["thread_id"]] = {"session_id": session_id, "event_id": event_id}
        _compact_state(state)
        payload = {"visibility": VISIBILITY, "verification": VERIFICATION, "player_scope": player_scope,
                   "reflection_id": reflection_id, "conversation_event_id": conversation_id,
                   "reflection_ids": [_reflection_id(session_id, conversation_id, index, total,
                       player_scope=player_scope if versioned else None, cognitive_revision=revision) for index in range(total)],
                   "proposals": proposals, "changes": changes}
        event = {"event_id": event_id, "session_id": session_id, "timestamp": utc_now_iso(now_ms),
                 "type": "cognitive.reflection_applied", "source": "player_cognitive_transaction",
                 "caused_by": reflection_id, "payload": payload}
        receipt = {"event_id": event_id, "session_id": session_id, "player_scope": player_scope,
                   "reflection_id": reflection_id}
        completion = {completion_name: {**receipt, "conversation_event_id": conversation_id}}
        legacy_projection = _migration_projection(migration)
        seq = self._log.append([*migration, event], projections={PROJECTION: state, receipt_name: receipt,
                                                               **legacy_projection, **completion},
                               expected_revisions={PROJECTION: snapshot["revision"], receipt_name: 0,
                                                   **{name: 0 for name in legacy_projection},
                                                   **{name: 0 for name in completion}},
                               projection_scope=player_scope,
                               expected_session_revisions={SESSION_BINDING: binding["revision"]})[-1]
        return {"status": "applied" if proposals else "no_updates", "event_id": event_id, "seq": seq,
                "state": deepcopy(state)}

    def commit(self, *, session_id: str, source_event_id: str, event_id: str,
               thought: str, intention: Any, speech: Any, updates: dict | None = None,
               timestamp: str | None = None, now_ms: int | None = None,
               expected_revision: int | None = None, player_scope: str | None = None,
               dialogue_decision: dict | None = None,
               expected_dialogue_experience_revision: int | None = None) -> dict:
        """原子提交并返回 {event_id, seq, revision, state}。

        updates 支持 working（递归 patch）、beliefs、relationships、emotion、
        threads、facts。列表最多各八项，列表实体的身份分别为 proposition、
        character、title；同一次不得重复。事实不自动提升为世界知识。情绪和
        关系值是目标值，按现有单轮 MAX_DELTA 限幅。thread 状态由显式 patch
        更新；dormant -> open 时在 status_history 记录当前原始观察 ID。
        expected_revision 可指定调用方此前 read 得到的版本；省略也会使用本次
        读取的版本执行 CAS。显式 player_scope 前须 bind_session；省略仅兼容
        未绑定的旧 session。失败或冲突不会写入任何事件或投影。
        """
        session_id = _required(session_id, "session_id")
        source_event_id = _required(source_event_id, "source_event_id")
        event_id = _required(event_id, "event_id")
        _required(thought, "thought")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise CognitiveTransactionError("expected_revision 必须是非负整数")
        if (expected_dialogue_experience_revision is not None and
                (dialogue_decision is None or type(expected_dialogue_experience_revision) is not int
                 or expected_dialogue_experience_revision < 0)):
            raise CognitiveTransactionError("对话经历版本无效")
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        if type(now_ms) is not int or now_ms < 0:
            raise CognitiveTransactionError("now_ms 必须是非负整数")
        timestamp = _required(timestamp if timestamp is not None else utc_now_iso(now_ms), "timestamp")
        updates = {} if updates is None else updates
        _fields(updates, {"working", "beliefs", "relationships", "emotion", "threads", "facts"}, "updates")
        _check_input({"thought": thought, "intention": intention, "speech": speech, "updates": updates,
                      "dialogue_decision": dialogue_decision})
        source = self._source(session_id, source_event_id)
        experience_revision = None
        if dialogue_decision is not None:
            try:
                dialogue_decision = player_proposal.normalize_dialogue_decision(dialogue_decision)
            except ValueError as exc:
                raise CognitiveTransactionError("对话决策格式无效") from exc
            frame_id = dialogue_decision["frame_id"]
            source_id = source["payload"].get("source_id", "")
            prefix = "observe:" + frame_id + ":sha256:"
            digest = source_id[len(prefix):] if source_id.startswith(prefix) else ""
            if (source["payload"].get("channel") != "visual" or len(digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in digest)):
                raise CognitiveTransactionError("对话决策必须引用同帧视觉观察")
            if dialogue_decision["action"] == "select":
                choice = self._source(session_id, dialogue_decision["option_id"])
                item = choice["payload"]
                index, bounds = item.get("choice_index"), item.get("choice_bounds")
                if (item.get("channel") != "choice"
                        or item.get("source_id") != "frame:" + frame_id + ":sha256:" + digest
                        or choice["timestamp"] != source["timestamp"] or choice["seq"] >= source["seq"]
                        or type(index) is not int or not 0 <= index < 8 or not isinstance(bounds, dict)
                        or set(bounds) != {"x", "y", "width", "height"}):
                    raise CognitiveTransactionError("选项不属于该帧的已登记观察")
                values = [bounds[key] for key in ("x", "y", "width", "height")]
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in values):
                    raise CognitiveTransactionError("选项区域无效")
                x, y, width, height = values
                if (width <= 0 or height <= 0 or x - width / 2 < 0 or x + width / 2 > 1
                        or y - height / 2 < 0 or y + height / 2 > 1):
                    raise CognitiveTransactionError("选项区域不在画面内")
                try:
                    identity = json.dumps([session_id, frame_id, index, item["text"], bounds], ensure_ascii=True,
                                          sort_keys=True, separators=(",", ":"), allow_nan=False)
                    expected = "choice-" + hashlib.sha256(identity.encode("ascii")).hexdigest()
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise CognitiveTransactionError("选项来源标识无效") from exc
                if choice["event_id"] != expected:
                    raise CognitiveTransactionError("选项来源标识不匹配")
            _, experience_revision = self._log.experience_entries(session_id, event_id=source_event_id)
            if (expected_dialogue_experience_revision is not None
                    and experience_revision != expected_dialogue_experience_revision):
                raise EventLogConflict("对话观察已变化")
        binding_revision = 0
        if player_scope is not None:
            player_scope = validate_projection_scope(player_scope)
            binding_revision = self._binding(session_id, player_scope)["revision"]
        snap = self.read(session_id, player_scope=player_scope)
        if expected_revision is not None and expected_revision != snap["revision"]:
            raise EventLogConflict("状态投影版本冲突")
        proposal = player_proposal.normalize_proposal({"thought": thought, "intention": intention, "speech": speech})
        if not proposal or not proposal["thought"]:
            raise CognitiveTransactionError("thought 规范化后不能为空")
        if intention is not None and proposal["intention"] is None:
            raise CognitiveTransactionError("intention 缺少有效 goal")
        if speech is not None and proposal["speech"] is None:
            raise CognitiveTransactionError("speech 必须是字符串或对象")
        state, migration = self._prepare_state(snap, session_id, player_scope, timestamp)
        before = deepcopy(state)
        self._apply(state, updates, source_event_id, now_ms)
        # 完整变化保存在不可变事件；投影只保留当前状态。
        changes = {k: {"before": before[k], "after": deepcopy(state[k])}
                   for k in ("working", "beliefs", "relationships", "emotion", "threads", "facts")
                   if before[k] != state[k]}
        old_threads = {item["thread_id"]: item for item in before["threads"]}
        for item in state["threads"]:
            tid = item["thread_id"]
            if old_threads.get(tid) != item:
                state["thread_last_events"][tid] = {"session_id": session_id, "event_id": event_id}
        _compact_state(state)
        payload = {"visibility": VISIBILITY, "verification": VERIFICATION,
                   "source_event_id": source_event_id, "source_seq": source["seq"],
                   "player_scope": player_scope,
                   "thought": proposal["thought"], "intention": proposal["intention"],
                   "speech": proposal["speech"], "changes": changes}
        if dialogue_decision is not None:
            payload["dialogue_decision"] = {**dialogue_decision, "status": "proposed_not_executed"}
        event = {"event_id": event_id, "session_id": session_id, "timestamp": timestamp,
                 "frame_seq": source.get("frame_seq"), "type": "cognitive.turn",
                 "payload": payload, "source": "player_cognitive_transaction", "caused_by": source_event_id}
        receipt_name = _receipt_name("turn", event_id)
        receipt = {"event_id": event_id, "session_id": session_id, "player_scope": player_scope,
                   "source_event_id": source_event_id}
        legacy_projection = _migration_projection(migration)
        seq = self._log.append([*migration, event], projections={PROJECTION: state, receipt_name: receipt, **legacy_projection},
                               expected_revisions={PROJECTION: snap["revision"], receipt_name: 0,
                                                   **{name: 0 for name in legacy_projection}},
                               projection_scope=player_scope,
                               expected_session_revisions={SESSION_BINDING: binding_revision,
                                   **({"experience.v1": experience_revision} if experience_revision is not None else {})})[-1]
        return {"event_id": event_id, "seq": seq, "revision": seq, "state": deepcopy(state)}

    @staticmethod
    def _apply(state: dict, updates: dict, source_id: str, now: int) -> None:
        if "working" in updates:
            patch = _fields(updates["working"], set(working_memory.default_working()) - {"schema_version", "updated_at"}, "working")
            state["working"] = working_memory.normalize_working(_merge_patch(state["working"], patch))
            state["working"]["updated_at"] = utc_now_iso(now)

        seen = set()
        for update in _items(updates.get("beliefs", []), "beliefs"):
            _fields(update, {"proposition", "status", "confidence", "evidence", "contradictions", "reason"}, "belief")
            _required(update.get("proposition"), "belief.proposition")
            _number_fields(update, ("confidence",))
            _list_fields(update, ("evidence", "contradictions"))
            if "status" in update and update["status"] not in beliefs.STATUSES:
                raise CognitiveTransactionError("belief.status 无效")
            item = beliefs.normalize_belief(update, now_ms=now)
            key = item["fingerprint"]
            if key in seen:
                raise CognitiveTransactionError("同一事务不能重复更新 belief")
            seen.add(key)
            old = next ( (x for x in state["beliefs"] if x["fingerprint"] == key), None)
            if item["status"] == "known_fact":
                item["status"] = "belief"
            if old is None:
                item.update(visibility=VISIBILITY, verification=VERIFICATION, source_event_ids=[source_id])
                state["beliefs"].append(item)
                continue
            _clear_field_sources(old, update)
            new_status = item["status"] if "status" in update else old["status"]
            if new_status != old["status"]:
                old.setdefault("history", []).append(f'{old["status"]} -> {new_status}')
                old.setdefault("transitions", []).append({
                    "from": old["status"], "to": new_status,
                    "reason": str(update.get("reason") or ""),
                    "evidence": list(item.get("evidence") or []),
                    "source_event_id": source_id, "timestamp_ms": now,
                })
                if new_status == "disproven":
                    old["contradictions"] = beliefs._list(old.get("contradictions", []) + item["evidence"])
                old["status"] = new_status
            if "confidence" in update:
                old["confidence"] = item["confidence"]
            for field in ("evidence", "contradictions"):
                old[field] = beliefs._list(old.get(field, []) + item[field])
            old["last_updated_ms"] = now
            old.setdefault("source_event_ids", []).append(source_id)

        seen = set()
        for update in _items(updates.get("relationships", []), "relationships"):
            _fields(update, {"character", "name", *relationships.DIMENSIONS, "memorable_events", "impressions", "unresolved"}, "relationship")
            _required(update.get("character") or update.get("name"), "relationship.character")
            _number_fields(update, relationships.DIMENSIONS)
            _list_fields(update, ("memorable_events", "impressions", "unresolved"))
            item = relationships.normalize_relationship(update)
            key = relationships._identity(item["character"])
            if key in seen:
                raise CognitiveTransactionError("同一事务不能重复更新 relationship")
            seen.add(key)
            old = next((x for x in state["relationships"] if relationships._identity(x["character"]) == key), None)
            if old is None:
                item["character_id"] = relationships._available_id(item["character"], item["character_id"], {x["character_id"] for x in state["relationships"]})
                for dim in relationships.DIMENSIONS:
                    item[dim] = round(min(relationships.MAX_DELTA, item[dim]), 4)
                item.update(last_updated_ms=now, visibility=VISIBILITY, verification=VERIFICATION)
                state["relationships"].append(item)
                continue
            _clear_field_sources(old, update)
            for dim in relationships.DIMENSIONS:
                if dim in update:
                    current = old[dim]
                    old[dim] = round(current + max(-relationships.MAX_DELTA, min(relationships.MAX_DELTA, item[dim] - current)), 4)
            for field, limit, size in (("memorable_events", 12, 240), ("impressions", 10, 200), ("unresolved", 8, 200)):
                old[field] = relationships._list(old.get(field, []) + item[field], limit=limit, item_limit=size)
            old["last_updated_ms"] = now

        if "emotion" in updates:
            patch = _fields(updates["emotion"], set(emotion.DIMENSIONS), "emotion")
            _number_fields(patch, emotion.DIMENSIONS)
            old = state["emotion"]
            _clear_field_sources(old, patch)
            current = emotion._decayed(old, now - (old.get("last_updated_ms") or now))
            for dim, target in patch.items():
                target = emotion._ratio(target)
                current[dim] = round(current[dim] + max(-emotion.MAX_DELTA, min(emotion.MAX_DELTA, target - current[dim])), 4)
            old.update(current, last_updated_ms=now, updated_at=utc_now_iso(now))

        seen = set()
        for update in _items(updates.get("threads", []), "threads"):
            _fields(update, {"title", "thread", "status", "importance", "characters", "topics", "evidence", "current_theory"}, "thread")
            _required(update.get("title") or update.get("thread"), "thread.title")
            _number_fields(update, ("importance",))
            _list_fields(update, ("characters", "topics", "evidence"))
            if "status" in update and update["status"] not in threads.STATUSES:
                raise CognitiveTransactionError("thread.status 无效")
            item = threads.normalize_thread(update, now_ms=now)
            key = item["fingerprint"]
            if key in seen:
                raise CognitiveTransactionError("同一事务不能重复更新 thread")
            seen.add(key)
            old = next((x for x in state["threads"] if x["fingerprint"] == key), None)
            if old is None:
                item.update(visibility=VISIBILITY, verification=VERIFICATION, source_event_ids=[source_id])
                state["threads"].append(item)
                continue
            _clear_field_sources(old, update)
            if "status" in update and item["status"] != old["status"]:
                reason = "reactivation" if old["status"] == "dormant" and item["status"] == "open" else "manual_update"
                threads._record_status(old, old["status"], item["status"], now, reason=reason, event_id=source_id)
                if reason == "reactivation":
                    old["last_activation"] = {"event_id": source_id, "reason": reason, "time_ms": now}
                old["status"] = item["status"]
            for field in ("importance", "current_theory"):
                if field in update:
                    old[field] = item[field]
            for field, limit, size in (("characters", 8, 60), ("topics", 8, 120), ("evidence", 10, 200)):
                old[field] = threads._list(old.get(field, []) + item[field], limit=limit, item_limit=size)
            old["last_referenced_ms"] = now
            old.setdefault("source_event_ids", []).append(source_id)

        for fact in _items(updates.get("facts", []), "facts"):
            if isinstance(fact, str):
                fact = {"proposition": fact}
            _fields(fact, {"proposition", "confidence", "tags", "derivation"}, "fact")
            proposition = _required(fact.get("proposition"), "fact.proposition")
            _number_fields(fact, ("confidence",))
            _list_fields(fact, ("tags",))
            normalized = beliefs.normalize_belief({"proposition": proposition, "confidence": fact.get("confidence", 0.5)}, now_ms=now)
            tags = beliefs._list(fact.get("tags", []), limit=8, item_limit=40)
            derivation = fact.get("derivation", "")
            if not isinstance(derivation, str):
                raise CognitiveTransactionError("fact.derivation 必须为字符串")
            state["facts"].append({"proposition": normalized["proposition"], "confidence": normalized["confidence"],
                                   "tags": tags, "derivation": derivation[:300], "visibility": VISIBILITY,
                                   "verification": VERIFICATION, "source_event_id": source_id, "time_ms": now})
