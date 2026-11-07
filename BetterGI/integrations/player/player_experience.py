#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可追溯的玩家经历记忆，与事件日志同事务提交。

调用方负责从可信的截图/OCR、音频或动作反馈适配器提供真实输入；这里不联网、
不识别剧情，也无法证明传感器、说话人标签或自由文本的真实性。经历事件和事实
可以引用观察原文，也可以附来源、置信度和推导说明进行综合；综合结果标为未校验，
不是世界真相。主观推断须有不确定性说明。本模块不能验证自然语言的推导是否成立，
接入方必须检查证据是否真正支持内容，尤其不能将动作尝试冒充成功。

日志是唯一持久化源。旧 experience.v1 快照保留不改，按会话惰性索引其已提交
事件；新经历只增量追加事件和索引，兼容投影在读取时重建。对话依据已提交
观察及经历版本生成，写入时校验版本以避免漏掉并发观察。跨 session 查询必须由
调用方明确列出有权访问的 session；本模块不是访问控制系统。
"""
from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from typing import Any, Mapping, Sequence

try:
    from .player_event_log import EventLogConflict, EventLogError, EventLogSchemaError, PlayerEventLog, _text as _log_text
except ImportError:  # 兼容现有从 integrations/player 直接导入的运行方式。
    from player_event_log import EventLogConflict, EventLogError, EventLogSchemaError, PlayerEventLog, _text as _log_text

RAW_OBSERVATION = "raw_observation"
EXPERIENCED_EVENT = "experienced_event"
EXPERIENCED_FACT = "experienced_fact"
SUBJECTIVE_INTERPRETATION = "subjective_interpretation"
BELIEF = "belief"
HYPOTHESIS = "hypothesis"
CANONICAL_WORLD_TRUTH = "canonical_world_truth"
PROJECTION = "experience.v1"
THREAD_UPDATE = "thread_update"
REFLECTION = "reflection"
_THREAD_STATUSES = frozenset({"open", "dormant", "resolved", "abandoned"})

# action_attempt 仅记录执行请求，不能成为已发生事件或确定事实的证据。
_CHANNELS = frozenset({"visual", "audio", "dialogue", "choice", "feedback", "action_attempt"})
_CLAIM_KINDS = frozenset({EXPERIENCED_EVENT, EXPERIENCED_FACT,
                          SUBJECTIVE_INTERPRETATION, BELIEF, HYPOTHESIS})
_CERTAIN_KINDS = frozenset({EXPERIENCED_EVENT, EXPERIENCED_FACT})
_UNCERTAIN_KINDS = _CLAIM_KINDS - _CERTAIN_KINDS
_RETRIEVABLE_KINDS = frozenset({RAW_OBSERVATION, "conversation", THREAD_UPDATE, REFLECTION}) | _CLAIM_KINDS


class ExperienceError(ValueError):
    """经历记录缺少证据、可见性不合法或投影无法安全读取。"""


def _text(value: Any, name: str) -> str:
    """要求非空文本；不将输入回显到异常。"""
    if not isinstance(value, str) or not value.strip():
        raise ExperienceError(f"{name} 必须是非空文本")
    return value


def _sessions(value: Sequence[str]) -> list[str]:
    """保留显式的跨会话顺序，拒绝重复和隐式全库扫描。"""
    if not isinstance(value, (list, tuple)) or not value:
        raise ExperienceError("session_ids 必须是非空列表")
    result = [_text(item, "session_id") for item in value]
    if len(set(result)) != len(result):
        raise ExperienceError("session_ids 不允许重复")
    return result


class PlayerExperience:
    """以调用方提供的 PlayerEventLog 持久化经历，连接生命周期由调用方管理。"""

    def __init__(self, event_log: PlayerEventLog):
        """接入已打开的 SQLite EventLog，不创建其他存储或修改其 schema。"""
        self._log = event_log

    def _snapshot(self, session_id: str, *, conversation_id: str | None = None,
                  event_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
        """读取已登记事件；旧快照在首次访问时原子迁移到索引。"""
        try:
            return self._log.experience_entries(session_id, conversation_id=conversation_id, event_id=event_id)
        except EventLogSchemaError as exc:
            raise ExperienceError("经历投影格式无效或与事件不一致") from exc

    def _append(self, entry: dict[str, Any], timestamp: str, *, expected_revision: int | None = None) -> int:
        """事件、经历索引和版本同事务提交，不重写完整快照。"""
        sid = entry["session_id"]
        event = {"event_id": entry["event_id"], "session_id": sid,
                 "timestamp": _text(timestamp, "timestamp"),
                 "type": "experience." + entry["kind"], "payload": entry,
                 "frame_seq": entry.get("frame_seq"), "source": "player_experience"}
        try:
            return self._log.append([event], experience=True,
                                    expected_experience_revision=expected_revision)[0]
        except EventLogSchemaError as exc:
            raise ExperienceError("经历投影格式无效或与事件不一致") from exc

    def record_thought(self, *, session_id: str, event_id: str, timestamp: str,
                       text: str, frame_seq: int | None = None,
                       caused_by: str | None = None) -> int:
        """Persist a model-generated thought before any same-round speech is delivered."""
        session_id = _text(session_id, "session_id")
        caused_by = _text(caused_by, "caused_by")
        source = self._log.get_event(session_id, caused_by)
        source_entries, _ = self._snapshot(session_id, event_id=caused_by)
        if (source is None or source.get("type") != "experience.raw_observation"
                or source.get("source") != "player_experience"
                or len(source_entries) != 1 or source_entries[0] != source.get("payload")
                or not isinstance(source.get("payload"), dict)
                or source["payload"].get("visibility") != "player"):
            raise ExperienceError("思考必须引用同场已提交的玩家可见观察")
        event = {"event_id": _text(event_id, "event_id"),
                 "session_id": session_id,
                 "timestamp": _text(timestamp, "timestamp"),
                 "type": "thought", "payload": {"text": _text(text, "text"), "verification": "subjective"},
                 "source": "persistent_player", "frame_seq": frame_seq, "caused_by": caused_by}
        return self._log.append([event])[0]

    def record_observation(self, *, session_id: str, event_id: str, timestamp: str,
                           source_id: str, channel: str, text: str,
                           visibility: str, speaker: str | None = None,
                           conversation_id: str | None = None,
                           frame_seq: int | None = None, choice_index: int | None = None,
                           choice_bounds: Mapping[str, float] | None = None,
                           region: str | None = None) -> int:
        """记录来自观察或反馈的原文，source_id 指向调用方已有的原始证据。

        visibility 必须明确为 player 或 hidden；hidden 数据绝不参与玩家召回或
        推断。speaker/conversation_id 是上游标签，不推测未知说话人或对话归属。
        """
        if channel not in _CHANNELS:
            raise ExperienceError("不支持的观察来源类型")
        if visibility not in ("player", "hidden"):
            raise ExperienceError("visibility 必须明确为 player 或 hidden")
        if speaker is not None and channel != "dialogue":
            raise ExperienceError("说话人仅适用于 dialogue")
        if conversation_id is not None and channel not in ("dialogue", "choice"):
            raise ExperienceError("对话标识仅适用于 dialogue 或 choice")
        entry = {"kind": RAW_OBSERVATION, "session_id": _text(session_id, "session_id"),
                 "event_id": _text(event_id, "event_id"), "source_id": _text(source_id, "source_id"),
                 "timestamp": _text(timestamp, "timestamp"),
                 "channel": channel, "text": _text(text, "text"), "visibility": visibility}
        if frame_seq is not None:
            if type(frame_seq) is not int or not 0 <= frame_seq <= 2**63 - 1:
                raise ExperienceError("frame_seq 必须是非负整数")
            entry["frame_seq"] = frame_seq
        if speaker is not None:
            entry["speaker"] = _text(speaker, "speaker")
            entry["speaker_status"] = "candidate"
        if conversation_id is not None:
            entry["conversation_id"] = _text(conversation_id, "conversation_id")
        if region is not None:
            if channel != "visual" or not isinstance(region, str) or region not in {"quest", "hud"}:
                raise ExperienceError("屏幕区域必须是 quest 或 hud 的视觉观察")
            entry["region"] = region
        if choice_index is not None or choice_bounds is not None:
            if (channel != "choice" or type(choice_index) is not int or not 0 <= choice_index < 8
                    or not isinstance(choice_bounds, Mapping)
                    or set(choice_bounds) != {"x", "y", "width", "height"}):
                raise ExperienceError("选项序号及区域无效")
            values = [choice_bounds[key] for key in ("x", "y", "width", "height")]
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in values):
                raise ExperienceError("选项区域无效")
            x, y, width, height = values
            if (width <= 0 or height <= 0 or x - width / 2 < 0 or x + width / 2 > 1
                    or y - height / 2 < 0 or y + height / 2 > 1):
                raise ExperienceError("选项区域必须完整位于画面内")
            entry["choice_index"] = choice_index
            entry["choice_bounds"] = dict(choice_bounds)
        return self._append(entry, timestamp)

    def record_world_truth(self, *, session_id: str, event_id: str, timestamp: str,
                           source_id: str, text: str) -> int:
        """登记非玩家可召回的世界真相，仅供受信任的离线管理使用。"""
        entry = {"kind": CANONICAL_WORLD_TRUTH, "session_id": _text(session_id, "session_id"),
                 "event_id": _text(event_id, "event_id"), "source_id": _text(source_id, "source_id"),
                 "text": _text(text, "text"), "visibility": "hidden"}
        return self._append(entry, timestamp)

    def record_claim(self, *, session_id: str, event_id: str, timestamp: str,
                     kind: str, text: str, evidence: Sequence[Mapping[str, str]],
                     uncertainty: str | None = None, derivation: str | None = None,
                     confidence: float | None = None) -> int:
        """记录已观察原文或有不确定性说明的主观推断。

        evidence 每项为 {session_id, event_id}，必须引用已提交、玩家可见的原始
        观察；返回的引用还包含上游 source_id。非逐字的经历必须提供推导说明和
        置信度，并标为 derived_unverified。仅动作尝试不能支撑经历；其他观察
        与文本的语义一致性仍须由调用方验证。推断不可作为确定类证据。
        """
        sid = _text(session_id, "session_id")
        eid = _text(event_id, "event_id")
        content = _text(text, "text")
        if kind not in _CLAIM_KINDS:
            raise ExperienceError("不支持的经历类型")
        if not isinstance(evidence, (list, tuple)) or not evidence:
            raise ExperienceError("经历必须引用至少一条原始观察")
        if kind in _UNCERTAIN_KINDS:
            uncertainty = _text(uncertainty, "uncertainty")
        elif uncertainty is not None:
            raise ExperienceError("确定类经历不能带 uncertainty；不确定内容应使用推断类型")
        references = []
        observations = []
        seen = set()
        for ref in evidence:
            if not isinstance(ref, Mapping) or set(ref) != {"session_id", "event_id"}:
                raise ExperienceError("证据引用必须包含 session_id 和 event_id")
            source_session = _text(ref["session_id"], "evidence session_id")
            source_event = _text(ref["event_id"], "evidence event_id")
            key = (source_session, source_event)
            if key in seen:
                raise ExperienceError("证据引用重复")
            seen.add(key)
            matches, _ = self._snapshot(source_session, event_id=source_event)
            if len(matches) != 1 or matches[0].get("kind") != RAW_OBSERVATION or matches[0].get("visibility") != "player":
                raise ExperienceError("证据不存在或不可供玩家使用")
            observation = matches[0]
            observations.append(observation)
            references.append({"session_id": source_session, "event_id": source_event,
                               "source_id": _text(observation.get("source_id"), "source_id")})
        if kind in _CERTAIN_KINDS:
            exact = any(item.get("channel") != "action_attempt" and item.get("text") == content
                        for item in observations)
            if not exact:
                if not any(item.get("channel") != "action_attempt" for item in observations):
                    raise ExperienceError("不能仅凭动作尝试推断经历或事实")
                derivation = _text(derivation, "derivation")
                if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                        or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                    raise ExperienceError("推导经历需要 0–1 的有限 confidence")
        entry = {"kind": kind, "session_id": sid, "event_id": eid, "text": content,
                 "visibility": "player", "evidence": references}
        if kind in _CERTAIN_KINDS:
            entry["verification"] = "observed_quote" if exact else "derived_unverified"
            if not exact:
                entry["derivation"] = derivation
                entry["confidence"] = float(confidence)
        if kind in _UNCERTAIN_KINDS:
            entry["uncertainty"] = uncertainty
        return self._append(entry, timestamp)

    def end_conversation(self, *, session_id: str, event_id: str, timestamp: str,
                         conversation_id: str, source_id: str, incomplete: bool = False) -> int:
        """Commit a conversation assembled only from already persisted observations."""
        session_id = _text(session_id, "session_id")
        conversation_id = _text(conversation_id, "conversation_id")
        entries, revision = self._snapshot(session_id, conversation_id=conversation_id)
        rows = [item for item in entries if item.get("kind") == RAW_OBSERVATION
                and item.get("visibility") == "player"
                and item.get("conversation_id") == conversation_id]
        turns = [deepcopy(item) for item in rows if item.get("channel") == "dialogue"]
        choices = [deepcopy(item) for item in rows if item.get("channel") == "choice"]
        entry = {"kind": "conversation", "session_id": session_id,
                 "event_id": _text(event_id, "event_id"), "conversation_id": conversation_id,
                 "ended_at": _text(timestamp, "timestamp"), "source_id": _text(source_id, "source_id"),
                 "visibility": "player", "turns": turns, "choices_presented": choices,
                 "player_choices": [], "incomplete": bool(incomplete),
                 "interpretation_status": "not_reflected"}
        return self._append(entry, timestamp, expected_revision=revision)

    def end_conversation_idempotent(self, *, session_id: str, event_id: str, timestamp: str,
                                    conversation_id: str, source_id: str,
                                    incomplete: bool = False, max_attempts: int = 3) -> int:
        """以稳定事件 ID 重试结束对话，返回新提交或经验证的已有事件序号。

        每次冲突后重新读取索引；已占用 ID 仅在参数和结束时的全部来源快照
        均一致时视为成功。调用方重启后须重用完全相同的参数；重试耗尽仍抛
        EventLogConflict，不能将其他事件的 ID 冲突当作成功。
        """
        session_id = _text(session_id, "session_id")
        event_id = _text(event_id, "event_id")
        timestamp = _text(timestamp, "timestamp")
        conversation_id = _text(conversation_id, "conversation_id")
        source_id = _text(source_id, "source_id")
        if type(incomplete) is not bool or type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ExperienceError("incomplete 或 max_attempts 无效")
        for attempt in range(max_attempts):
            try:
                return self.end_conversation(session_id=session_id, event_id=event_id, timestamp=timestamp,
                                             conversation_id=conversation_id, source_id=source_id,
                                             incomplete=incomplete)
            except EventLogConflict:
                existing = self._log.get_event(session_id, event_id)
                if existing is not None:
                    # 必须重新核验持久化事件及其结束前所有可见观察，不能仅凭 ID 去重。
                    resolved = self._resolve_sources([{"session_id": session_id, "event_id": event_id}],
                                                     allowed_sessions=[session_id])[0]
                    entries, _ = self._snapshot(session_id, conversation_id=conversation_id)
                    turns, choices = [], []
                    for item in entries:
                        if (item.get("kind") != RAW_OBSERVATION or item.get("visibility") != "player"
                                or item.get("conversation_id") != conversation_id
                                or item.get("channel") not in ("dialogue", "choice")):
                            continue
                        source = self._log.get_event(session_id, item["event_id"])
                        if source is None:
                            raise ExperienceError("对话观察来源不存在")
                        if source["seq"] < resolved["seq"]:
                            verified = self._resolve_sources(
                                [{"session_id": session_id, "event_id": item["event_id"]}],
                                allowed_sessions=[session_id])[0]
                            if verified != source or verified["payload"] != item:
                                raise ExperienceError("对话观察与已提交来源不一致")
                            (turns if item["channel"] == "dialogue" else choices).append(item)
                    expected = {"kind": "conversation", "session_id": session_id, "event_id": event_id,
                                "conversation_id": conversation_id, "ended_at": timestamp,
                                "source_id": source_id, "visibility": "player", "turns": turns,
                                "choices_presented": choices, "player_choices": [], "incomplete": incomplete,
                                "interpretation_status": "not_reflected"}
                    if (resolved["type"] == "experience.conversation" and resolved["timestamp"] == timestamp
                            and resolved["payload"] == expected):
                        return resolved["seq"]
                    raise EventLogConflict("对话事件 ID 已被不匹配的事件占用")
                if attempt + 1 == max_attempts:
                    raise
        raise EventLogConflict("对话结束重试耗尽")

    @staticmethod
    def _end_intent_id(session_id: str, event_id: str) -> str:
        """对同一会话和目标事件生成重启后不变的待处理标识。"""
        import hashlib
        import json
        key = json.dumps([session_id, event_id], ensure_ascii=True, separators=(",", ":"))
        return "player-conversation-end-pending:" + hashlib.sha256(key.encode("ascii")).hexdigest()

    def stage_conversation_end(self, *, session_id: str, event_id: str, timestamp: str,
                               conversation_id: str, anchor_event_id: str) -> dict[str, Any]:
        """先持久化结束意图；观察只是锚点，不是对话确已结束的证明。

        锚点须是同会话、同对话、可见且已登记的帧来源观察。调用方须在丢弃
        内存状态之前等待成功；此接口不能证明帧真实存在或对话已经结束。
        """
        session_id = _text(session_id, "session_id")
        event_id = _text(event_id, "event_id")
        timestamp = _text(timestamp, "timestamp")
        conversation_id = _text(conversation_id, "conversation_id")
        anchor_event_id = _text(anchor_event_id, "anchor_event_id")
        if any(len(value) > 512 for value in (session_id, event_id, conversation_id, anchor_event_id)):
            raise ExperienceError("对话或来源标识过长")
        source = self._resolve_sources([{"session_id": session_id, "event_id": anchor_event_id}],
                                       allowed_sessions=[session_id])[0]
        observation = source["payload"]
        frame = observation.get("source_id")
        if (source["type"] != "experience.raw_observation"
                or observation.get("conversation_id") != conversation_id
                or observation.get("channel") not in ("dialogue", "choice")
                or not isinstance(frame, str) or not frame.startswith("frame:") or not frame[6:].strip()):
            raise ExperienceError("待处理对话需要同场已登记的帧观察锚点")
        pending_id = self._end_intent_id(session_id, event_id)
        payload = {"version": 1, "session_id": session_id, "conversation_event_id": event_id,
                   "conversation_id": conversation_id, "anchor_event_id": anchor_event_id,
                   "recorded_at": timestamp, "end_status": "unconfirmed", "incomplete": True}
        entry = {"event_id": pending_id, "session_id": session_id, "timestamp": timestamp,
                 "type": "experience.conversation_end_pending", "source": "player_experience",
                 "caused_by": anchor_event_id, "payload": payload}
        try:
            seq = self._log.append([entry])[0]
        except EventLogConflict:
            existing = self._log.get_event(session_id, pending_id)
            if existing is None or not all(existing.get(key) == value for key, value in entry.items()):
                raise
            seq = existing["seq"]
        return {"session_id": session_id, "pending_event_id": pending_id, "seq": seq,
                "end_status": "unconfirmed"}

    def _end_intent(self, session_id: str, pending_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取并重新验证持久化意图及其锚点，不信任标识或存储的可见性声明。"""
        try:
            _log_text(session_id, "session_id")
            _log_text(pending_id, "pending_event_id")
        except EventLogError as exc:
            raise ExperienceError("待处理意图标识无效") from exc
        event = self._log.get_event(session_id, pending_id)
        if event is None or event.get("type") != "experience.conversation_end_pending" or event.get("source") != "player_experience":
            raise ExperienceError("待处理意图不存在或来源无效")
        payload = event.get("payload")
        if (not isinstance(payload, dict) or set(payload) != {"version", "session_id", "conversation_event_id",
                "conversation_id", "anchor_event_id", "recorded_at", "end_status", "incomplete"}
                or type(payload.get("version")) is not int or payload["version"] != 1
                or payload.get("session_id") != session_id or payload.get("recorded_at") != event["timestamp"]
                or payload.get("end_status") != "unconfirmed" or payload.get("incomplete") is not True):
            raise ExperienceError("待处理意图格式无效")
        end_id = _text(payload.get("conversation_event_id"), "conversation_event_id")
        try:
            _log_text(end_id, "conversation_event_id")
        except EventLogError as exc:
            raise ExperienceError("对话事件标识无效") from exc
        cid = _text(payload.get("conversation_id"), "conversation_id")
        anchor = _text(payload.get("anchor_event_id"), "anchor_event_id")
        try:
            _log_text(cid, "conversation_id")
            _log_text(event["timestamp"], "timestamp")
        except EventLogError as exc:
            raise ExperienceError("待处理意图内容无效") from exc
        if any(len(value) > 512 for value in (session_id, end_id, cid, anchor)):
            raise ExperienceError("对话或来源标识过长")
        if pending_id != self._end_intent_id(session_id, end_id) or event.get("caused_by") != anchor:
            raise ExperienceError("待处理意图标识不一致")
        source = self._resolve_sources([{"session_id": session_id, "event_id": anchor}],
                                       allowed_sessions=[session_id])[0]
        observation = source["payload"]
        frame = observation.get("source_id")
        if (source["seq"] >= event["seq"] or source["type"] != "experience.raw_observation"
                or observation.get("conversation_id") != cid or observation.get("channel") not in ("dialogue", "choice")
                or not isinstance(frame, str) or not frame.startswith("frame:") or not frame[6:].strip()):
            raise ExperienceError("待处理意图的来源无效")
        return event, source

    def _unconfirmed_conversation(self, intent: Mapping[str, Any], anchor: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        """仅重建意图之前已验证的观察，不把记录时间伪装为对话结束时间。"""
        payload = intent["payload"]
        sid, cid = intent["session_id"], payload["conversation_id"]
        entries, revision = self._snapshot(sid, conversation_id=cid)
        turns, choices = [], []
        for item in entries:
            if (item.get("kind") != RAW_OBSERVATION or item.get("visibility") != "player"
                    or item.get("conversation_id") != cid or item.get("channel") not in ("dialogue", "choice")):
                continue
            source = self._log.get_event(sid, item["event_id"])
            if source is None:
                raise ExperienceError("对话观察来源不存在")
            if source["seq"] < intent["seq"]:
                verified = self._resolve_sources([{"session_id": sid, "event_id": item["event_id"]}],
                                                 allowed_sessions=[sid])[0]
                if (verified != source or source["payload"] != item
                        or len(_text(item.get("text"), "observation text")) > 1000000
                        or len(item["event_id"]) > 512):
                    raise ExperienceError("对话观察与来源不一致或过长")
                (turns if item["channel"] == "dialogue" else choices).append(deepcopy(item))
        if not any(item["event_id"] == anchor["event_id"] for item in turns + choices):
            raise ExperienceError("对话锚点不在已登记的观察中")
        if len(turns) + len(choices) > 1000:
            raise ExperienceError("对话观察超过恢复上限")
        entry = {"kind": "conversation", "session_id": sid, "event_id": payload["conversation_event_id"],
                 "conversation_id": cid, "ended_at": None, "recorded_at": payload["recorded_at"],
                 "end_status": "unconfirmed", "source_id": anchor["payload"]["source_id"],
                 "visibility": "player", "turns": turns, "choices_presented": choices,
                 "player_choices": [], "incomplete": True, "interpretation_status": "not_reflected"}
        return entry, revision

    def complete_pending_conversation_end(self, *, session_id: str, pending_event_id: str,
                                          max_attempts: int = 3) -> int:
        """幂等提交未确认结束的片段；它不是对话确已结束的证明。"""
        session_id = _text(session_id, "session_id")
        pending_event_id = _text(pending_event_id, "pending_event_id")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ExperienceError("max_attempts 必须为 1–10")
        for attempt in range(max_attempts):
            intent, anchor = self._end_intent(session_id, pending_event_id)
            entry, revision = self._unconfirmed_conversation(intent, anchor)
            try:
                return self._append(entry, intent["timestamp"], expected_revision=revision)
            except EventLogConflict:
                existing = self._log.get_event(session_id, entry["event_id"])
                if existing is not None:
                    verified = self._resolve_sources([{"session_id": session_id, "event_id": entry["event_id"]}],
                                                     allowed_sessions=[session_id])[0]
                    if (verified["type"] == "experience.conversation" and verified["seq"] > intent["seq"]
                            and verified["timestamp"] == intent["timestamp"] and verified["payload"] == entry):
                        return verified["seq"]
                    raise EventLogConflict("对话事件 ID 已被不匹配的事件占用")
                if attempt + 1 == max_attempts:
                    raise
        raise EventLogConflict("对话恢复重试耗尽")

    def pending_conversation_ends(self, player_id: str, *, before_seq: int | None = None,
                                  limit: int = 32, max_pages: int = 10) -> dict[str, Any]:
        """有界恢复扫描；游标排他，已完成项仅在完整核验后过滤。"""
        player_id = _text(player_id, "player_id")
        if (":" in player_id or type(limit) is not int or not 1 <= limit <= 100
                or type(max_pages) is not int or not 1 <= max_pages <= 20):
            raise ExperienceError("无效的恢复查询参数")
        cursor = before_seq
        items = []
        pages = 0
        while pages < max_pages and len(items) < limit:
            rows, next_cursor = self._log.conversation_event_page(player_id + ":", before_seq=cursor,
                                                                   pending_ends=True)
            pages += 1
            for index, row in enumerate(rows):
                cursor = row["seq"]
                try:
                    intent, anchor = self._end_intent(row["session_id"], row["event_id"])
                    if intent != row:
                        continue
                except (ExperienceError, EventLogSchemaError, TypeError, KeyError, RecursionError, UnicodeError):
                    continue
                status = "pending"
                try:
                    entry, _ = self._unconfirmed_conversation(intent, anchor)
                    existing = self._log.get_event(row["session_id"], entry["event_id"])
                    if existing is not None:
                        verified = self._resolve_sources([{"session_id": row["session_id"], "event_id": entry["event_id"]}],
                                                         allowed_sessions=[row["session_id"]])[0]
                        if (verified["type"] == "experience.conversation" and verified["seq"] > intent["seq"]
                                and verified["timestamp"] == intent["timestamp"] and verified["payload"] == entry):
                            continue
                        status = "blocked"
                except (ExperienceError, EventLogSchemaError, TypeError, KeyError, RecursionError, UnicodeError):
                    status = "blocked"
                items.append({"session_id": row["session_id"], "pending_event_id": row["event_id"],
                              "conversation_event_id": intent["payload"]["conversation_event_id"],
                              "end_status": "unconfirmed", "status": status})
                if len(items) == limit:
                    if index == len(rows) - 1 and next_cursor is None:
                        cursor = None
                    return {"items": items, "next_before_seq": cursor, "pages": pages}
            if next_cursor is None:
                return {"items": items, "next_before_seq": None, "pages": pages}
            cursor = next_cursor
        return {"items": items, "next_before_seq": cursor, "pages": pages}

    def recall(self, session_ids: Sequence[str]) -> list[dict[str, Any]]:
        """按给定会话顺序和各自提交顺序返回可见经历，不返回世界真相。"""
        result = []
        for sid in _sessions(session_ids):
            entries, _ = self._snapshot(sid)
            result.extend(entry for entry in entries if entry.get("visibility") == "player"
                          and entry.get("kind") in ({RAW_OBSERVATION} | _CLAIM_KINDS))
        return deepcopy(result)

    def _belongs_to_scope(self, session_id: str, player_scope: str | None) -> bool:
        """显式要求玩家归属时，只接受绑定事件可核验的会话。"""
        if player_scope is None:
            return True
        try:
            from .player_cognitive_transaction import PlayerCognitiveTransaction
        except ImportError:
            from player_cognitive_transaction import PlayerCognitiveTransaction
        try:
            PlayerCognitiveTransaction(self._log)._binding(session_id, player_scope)
            return True
        except (ValueError, TypeError, KeyError):
            return False

    def _iter_scoped_events(self, player_id: str, player_scope: str | None, **filters: Any):
        """按提交顺序翻页后再过滤归属，避免其他玩家事件占满结果限额。"""
        cursor = None
        while True:
            rows = self._log.query_events(session_prefix=player_id + ":", before_seq=cursor, limit=200, **filters)
            for row in rows:
                if self._belongs_to_scope(row["session_id"], player_scope):
                    yield row
            if len(rows) < 200:
                return
            cursor = rows[-1]["seq"]

    def recent_dialogue(self, player_id: str, *, limit: int = 8, player_scope: str | None = None) -> list[dict[str, Any]]:
        """返回玩家命名空间中的近期可见对话 OCR 候选，非访问控制。"""
        player_id = _text(player_id, "player_id")
        if ":" in player_id or type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("玩家标识或对话查询上限无效")
        events = self._iter_scoped_events(player_id, player_scope, event_types=["experience." + RAW_OBSERVATION],
                                          visibility="player", channel="dialogue")
        result = []
        for item in events:
            payload = item.get("payload")
            if (item.get("source") != "player_experience" or not isinstance(payload, dict)
                    or payload.get("kind") != RAW_OBSERVATION
                    or payload.get("session_id") != item["session_id"]
                    or payload.get("event_id") != item["event_id"]):
                continue
            registered, _ = self._snapshot(item["session_id"], event_id=item["event_id"])
            if len(registered) == 1 and registered[0] == payload:
                result.append(deepcopy(payload))
                if len(result) == limit:
                    break
        return list(reversed(result))

    def recent_region_observations(self, player_id: str, *, limit: int = 32,
                                   player_scope: str | None = None) -> list[dict[str, Any]]:
        """只召回已登记的任务/HUD 同帧 OCR 候选，不将文字视为任务事实。"""
        player_id = _text(player_id, "player_id")
        if ":" in player_id or type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("玩家标识或屏幕区域查询上限无效")
        events = []
        for region in ("quest", "hud"):
            count = 0
            for event in self._iter_scoped_events(player_id, player_scope,
                    event_types=["experience." + RAW_OBSERVATION], visibility="player", channel="visual", region=region):
                payload = event.get("payload")
                if (event.get("source") != "player_experience" or not isinstance(payload, dict)
                        or payload.get("kind") != RAW_OBSERVATION or payload.get("session_id") != event["session_id"]
                        or payload.get("event_id") != event["event_id"] or payload.get("region") != region):
                    continue
                registered, _ = self._snapshot(event["session_id"], event_id=event["event_id"])
                if len(registered) == 1 and registered[0] == payload:
                    events.append(event)
                    count += 1
                    if count >= limit:
                        break
        events.sort(key=lambda item: item["seq"], reverse=True)
        return [deepcopy(event["payload"]) for event in reversed(events[:limit])]

    def _pending_conversation_sources(self, row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """核验对话和每个快照的实际来源，拒绝不可见、伪造或过晚的观察。"""
        sid, eid = row["session_id"], row["event_id"]
        event = self._resolve_sources([{"session_id": sid, "event_id": eid}], allowed_sessions=[sid])[0]
        if event != row or event["type"] != "experience.conversation":
            raise ExperienceError("对话事件不一致")
        payload = event["payload"]
        if len(sid) > 512 or len(eid) > 512:
            raise ExperienceError("对话标识过长")
        cid = _text(payload.get("conversation_id"), "conversation_id")
        if len(cid) > 512:
            raise ExperienceError("对话标识过长")
        turns, choices = payload.get("turns"), payload.get("choices_presented")
        if not isinstance(turns, list) or not isinstance(choices, list) or not 1 <= len(turns) + len(choices) <= 1000:
            raise ExperienceError("对话观察列表无效")
        sources = {}
        for items, channel in ((turns, "dialogue"), (choices, "choice")):
            for item in items:
                if not isinstance(item, dict) or item.get("session_id") != sid:
                    raise ExperienceError("对话观察无效")
                source_id = _text(item.get("event_id"), "observation event_id")
                if len(source_id) > 512 or source_id in sources:
                    raise ExperienceError("对话观察标识无效或重复")
                source = self._resolve_sources([{"session_id": sid, "event_id": source_id}],
                                               allowed_sessions=[sid])[0]
                if (source["type"] != "experience.raw_observation" or source["seq"] >= event["seq"]
                        or source["payload"] != item or item.get("channel") != channel
                        or item.get("conversation_id") != cid):
                    raise ExperienceError("对话观察与已提交来源不一致")
                if len(_text(item.get("text"), "observation text")) > 1000000:
                    raise ExperienceError("对话观察过长")
                sources[source_id] = source
        return sources

    def _reflection_completed(self, session_id: str, conversation_id: str, player_scope: str) -> bool:
        """独立完成收据不随后续认知版本推进而失效。"""
        try:
            from .player_reflection import _completion_name
        except ImportError:
            from player_reflection import _completion_name
        receipt = self._log.get_projection(session_id, _completion_name(session_id, conversation_id),
                                           projection_scope=player_scope)
        if receipt is None or not isinstance(receipt.get("state"), dict):
            return False
        state = receipt["state"]
        if (set(state) != {"session_id", "conversation_event_id", "player_scope", "event_id", "reflection_id"}
                or state["session_id"] != session_id or state["conversation_event_id"] != conversation_id
                or state["player_scope"] != player_scope):
            return False
        event = self._log.get_event(session_id, state["event_id"])
        payload = event.get("payload") if event else None
        return bool(event and event["seq"] == receipt["revision"] and event["type"] == "cognitive.reflection_applied"
                    and event["source"] == "player_cognitive_transaction" and event.get("caused_by") == state["reflection_id"]
                    and isinstance(payload, dict) and payload.get("player_scope") == player_scope
                    and payload.get("conversation_event_id") == conversation_id
                    and payload.get("reflection_id") == state["reflection_id"])

    def _has_conversation_reflection(self, row: Mapping[str, Any],
                                     sources: Mapping[str, dict[str, Any]], *,
                                     player_scope: str | None = None, cognitive_revision: int | None = None) -> bool:
        """只有每一批均有可核验收据时，完整对话才算反思完毕。"""
        try:
            from .player_reflection import _MAX_SOURCES, _reflection_id
        except ImportError:
            from player_reflection import _MAX_SOURCES, _reflection_id
        import json
        ordered = sorted(sources.values(), key=lambda source: source["seq"])
        total = (len(ordered) + _MAX_SOURCES - 1) // _MAX_SOURCES
        unset = object()
        revision = unset
        for index in range(total):
            batch = ordered[index * _MAX_SOURCES:(index + 1) * _MAX_SOURCES]
            batch_sources = {source["event_id"]: source for source in batch}
            reflection_id = _reflection_id(row["session_id"], row["event_id"], index, total,
                                           player_scope=player_scope, cognitive_revision=cognitive_revision)
            if not self._valid_conversation_reflection(row, batch_sources, reflection_id, index, total,
                                                       player_scope=player_scope, cognitive_revision=cognitive_revision):
                return False
            if total > 1:
                try:
                    document = json.loads(self._log.get_event(row["session_id"], reflection_id)["payload"]["text"])
                    value = document.get("cognitive_revision")
                    if value is not None and (type(value) is not int or value < 0):
                        return False
                    if revision is not unset and revision != value:
                        return False
                    revision = value
                except (ValueError, TypeError, KeyError):
                    return False
        return total > 0

    def _valid_conversation_reflection(self, row: Mapping[str, Any], sources: Mapping[str, dict[str, Any]],
                                       reflection_id: str, index: int, total: int, *,
                                       player_scope: str | None = None, cognitive_revision: int | None = None) -> bool:
        """核验一个批次对应的对话、原始证据与反思收据。"""
        import json
        try:
            from .player_reflection import _parse, _unique_object
        except ImportError:
            from player_reflection import _parse, _unique_object
        sid, eid = row["session_id"], row["event_id"]
        try:
            reflection = self._log.get_event(sid, reflection_id)
            if (reflection is None or reflection.get("type") != "experience.reflection"
                    or reflection.get("source") != "player_experience" or reflection["seq"] <= row["seq"]):
                return False
            payload = reflection.get("payload")
            if (not isinstance(payload, dict) or payload.get("kind") != REFLECTION
                    or payload.get("session_id") != sid or payload.get("event_id") != reflection_id
                    or payload.get("visibility") != "player" or payload.get("verification") != "subjective_unverified"
                    or not isinstance(payload.get("uncertainty"), str) or not payload["uncertainty"].strip()):
                return False
            try:
                receipt = json.loads(payload.get("text", ""), object_pairs_hook=_unique_object)
            except (ValueError, TypeError, RecursionError):
                return False
            expected_version = 2 if player_scope is not None else 1
            if (not isinstance(receipt, dict) or type(receipt.get("version")) is not int or receipt["version"] != expected_version
                    or (player_scope is not None and (receipt.get("player_scope") != player_scope
                        or type(receipt.get("cognitive_revision")) is not int
                        or receipt["cognitive_revision"] != cognitive_revision))
                    or receipt.get("conversation_event_id") != eid
                    or (total > 1 and (type(receipt.get("batch_index")) is not int
                                       or receipt["batch_index"] != index
                                       or type(receipt.get("batch_total")) is not int
                                       or receipt["batch_total"] != total))
                    or not isinstance(receipt.get("items"), list) or not 1 <= len(receipt["items"]) <= 12):
                return False
            if _parse(json.dumps({"status": "ok", "items": receipt["items"]}, ensure_ascii=False), set(sources)) is None:
                return False
            referenced = set()
            kinds = {"question", "subjective_interpretation", "belief_proposal", "relationship_proposal",
                     "emotion_proposal", "thread_proposal", "experienced_fact_candidate",
                     "major_reveal_candidate", "turning_point_candidate", "self_change_candidate"}
            for item in receipt["items"]:
                if (not isinstance(item, dict) or not isinstance(item.get("kind"), str) or item["kind"] not in kinds
                        or not isinstance(item.get("text"), str) or not item["text"].strip()
                        or not isinstance(item.get("uncertainty"), str) or not item["uncertainty"].strip()
                        or not isinstance(item.get("source_event_ids"), list)
                        or not 1 <= len(item["source_event_ids"]) <= 16
                        or any(not isinstance(value, str) or value not in sources for value in item["source_event_ids"])
                        or len(set(item["source_event_ids"])) != len(item["source_event_ids"])):
                    return False
                referenced.update(item["source_event_ids"])
            refs = payload.get("evidence")
            if (not isinstance(refs, list) or any(not isinstance(ref, dict)
                    or set(ref) != {"session_id", "event_id", "source_id", "seq", "type"} for ref in refs)):
                return False
            evidence = self._resolve_sources(refs, allowed_sessions=[sid])
            return ({source["event_id"] for source in evidence} == referenced
                    and all(source["event_id"] in sources and source == sources[source["event_id"]]
                            for source in evidence))
        except (ExperienceError, EventLogSchemaError, json.JSONDecodeError, TypeError, KeyError, RecursionError, UnicodeError):
            # 损坏或冒名收据不能屏蔽待处理项，也不回显其内容。
            return False

    def _legacy_reflection_completed(self, row: Mapping[str, Any], sources: Mapping[str, dict[str, Any]],
                                     player_scope: str) -> bool:
        """旧版无状态更新可沿用；含更新必须有已验证的原应用收据。"""
        import json
        try:
            from .player_reflection import _MAX_SOURCES, _reflection_id
            from .player_cognitive_transaction import _receipt_name
        except ImportError:
            from player_reflection import _MAX_SOURCES, _reflection_id
            from player_cognitive_transaction import _receipt_name
        sid, cid = row["session_id"], row["event_id"]
        total = (len(sources) + _MAX_SOURCES - 1) // _MAX_SOURCES
        ids = [_reflection_id(sid, cid, index, total) for index in range(total)]
        try:
            documents = [json.loads(self._log.get_event(sid, eid)["payload"]["text"]) for eid in ids]
            if not any("update" in item for document in documents for item in document["items"]):
                return True
            key = json.dumps([player_scope, sid, ids[0]], ensure_ascii=True, separators=(",", ":"))
            event_id = "cognitive-reflection-" + hashlib.sha256(key.encode("ascii")).hexdigest()
            receipt = self._log.get_projection(sid, _receipt_name("reflection", event_id), projection_scope=player_scope)
            event = self._log.get_event(sid, event_id)
            expected = {"event_id": event_id, "session_id": sid, "player_scope": player_scope, "reflection_id": ids[0]}
            return bool(receipt and receipt["state"] == expected and event and receipt["revision"] == event["seq"]
                        and event["type"] == "cognitive.reflection_applied"
                        and event["source"] == "player_cognitive_transaction" and event["caused_by"] == ids[0]
                        and isinstance(event.get("payload"), dict)
                        and event["payload"].get("conversation_event_id") == cid
                        and event["payload"].get("player_scope") == player_scope)
        except (ValueError, TypeError, KeyError, RecursionError):
            return False

    def pending_conversation_page(self, player_id: str, *, after_seq: int = 0, limit: int = 8,
                                  player_scope: str | None = None) -> dict[str, Any]:
        """按原始事件顺序扫描一页，返回未完成项及下次扫描游标。"""
        player_id = _text(player_id, "player_id")
        if (":" in player_id or type(limit) is not int or not 1 <= limit <= 100
                or type(after_seq) is not int or after_seq < 0):
            raise ExperienceError("无效的分页恢复参数")
        rows, next_cursor = self._log.conversation_event_page(player_id + ":", after_seq=after_seq, limit=200)
        result = []
        for index, row in enumerate(rows):
            if not self._belongs_to_scope(row["session_id"], player_scope):
                continue
            try:
                sources = self._pending_conversation_sources(row)
                complete = self._has_conversation_reflection(row, sources)
                if player_scope is not None:
                    complete = (self._reflection_completed(row["session_id"], row["event_id"], player_scope)
                                or (complete and self._legacy_reflection_completed(row, sources, player_scope)))
                if not complete:
                    result.append({"session_id": row["session_id"], "conversation_event_id": row["event_id"]})
                    if len(result) == limit:
                        cursor = row["seq"] if index < len(rows) - 1 else next_cursor
                        return {"items": result, "next_after_seq": cursor}
            except (ExperienceError, EventLogSchemaError, TypeError, KeyError, RecursionError, UnicodeError):
                continue
        return {"items": result, "next_after_seq": next_cursor}

    def pending_conversations(self, player_id: str, *, limit: int = 32,
                              player_scope: str | None = None) -> list[dict[str, str]]:
        """跨玩家会话按最早待处理对话优先扫描。

        未确认片段保留 incomplete 标记，不代表对话确已结束。
        每页最多 200 条；仅返回标识，损坏来源不参与反思。
        """
        player_id = _text(player_id, "player_id")
        if ":" in player_id or type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("无效的玩家标识或反思恢复上限")
        result = []
        cursor = 0
        while len(result) < limit:
            rows, next_cursor = self._log.conversation_event_page(player_id + ":", after_seq=cursor)
            for row in rows:
                if not self._belongs_to_scope(row["session_id"], player_scope):
                    continue
                try:
                    sources = self._pending_conversation_sources(row)
                    complete = self._has_conversation_reflection(row, sources)
                    if player_scope is not None:
                        complete = self._reflection_completed(row["session_id"], row["event_id"], player_scope)
                        if not complete and self._has_conversation_reflection(row, sources):
                            complete = self._legacy_reflection_completed(row, sources, player_scope)
                    if not complete:
                        result.append({"session_id": row["session_id"], "conversation_event_id": row["event_id"]})
                        if len(result) == limit:
                            break
                except (ExperienceError, EventLogSchemaError, TypeError, KeyError, RecursionError, UnicodeError):
                    # 坏条目不能阻塞更早的合法对话，也不能暴露不可见数据。
                    continue
            if next_cursor is None:
                break
            cursor = next_cursor
        return result

    def recent_narrative(self, player_id: str, *, limit: int = 16) -> list[dict[str, Any]]:
        """Retrieve scoped cognitive turns with sources; callers choose relevance."""
        player_id = _text(player_id, "player_id")
        if ":" in player_id or type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("玩家标识或认知查询上限无效")
        try:
            from .player_cognitive_transaction import PlayerCognitiveTransaction
        except ImportError:
            from player_cognitive_transaction import PlayerCognitiveTransaction
        verifier = PlayerCognitiveTransaction(self._log)
        player_scope = "player-" + hashlib.sha256(player_id.encode("utf-8")).hexdigest()
        rows = self._iter_scoped_events(player_id, player_scope, event_types=["cognitive.turn"])
        result = []
        for row in rows:
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("player_scope") != player_scope:
                continue
            try:
                _, _, verified = verifier._committed_turn(row["session_id"], player_scope, row["event_id"])
                if verified == row:
                    result.append(deepcopy(row))
                    if len(result) == limit:
                        break
            except (EventLogError, ValueError, TypeError, KeyError, RecursionError, UnicodeError):
                # 无收据、来源或绑定的候选不能冒充成功的认知事务。
                continue
        return list(reversed(result))

    def recent_reflections(self, player_id: str, *, limit: int = 16,
                           player_scope: str | None = None) -> list[dict[str, Any]]:
        """Read bounded player-visible subjective reflections, never canonical truth."""
        player_id = _text(player_id, "player_id")
        if ":" in player_id or type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("玩家标识或反思查询上限无效")
        rows = self._iter_scoped_events(player_id, player_scope,
            event_types=["experience." + REFLECTION], visibility="player")
        result = []
        for row in rows:
            payload = row.get("payload")
            if (row.get("source") != "player_experience" or not isinstance(payload, dict)
                    or payload.get("verification") != "subjective_unverified"
                    or payload.get("session_id") != row["session_id"]
                    or payload.get("event_id") != row["event_id"]):
                continue
            try:
                self._resolve_sources(payload.get("evidence"), allowed_sessions=[row["session_id"]])
                import json
                try:
                    from .player_reflection import _unique_object
                except ImportError:
                    from player_reflection import _unique_object
                text = payload.get("text", "")
                try:
                    document = json.loads(text, object_pairs_hook=_unique_object)
                except (ValueError, TypeError, RecursionError):
                    if isinstance(text, str) and ('"version"' in text or '"conversation_event_id"' in text):
                        continue
                    document = None
                if isinstance(document, dict) and document.get("version") == 2:
                    scope = document.get("player_scope")
                    revision = document.get("cognitive_revision")
                    conversation_id = document.get("conversation_event_id")
                    if (not isinstance(scope, str) or not scope or (player_scope is not None and scope != player_scope)
                            or type(revision) is not int or revision < 0 or not isinstance(conversation_id, str)
                            or not self._belongs_to_scope(row["session_id"], scope)
                            or not self._reflection_completed(row["session_id"], conversation_id, scope)):
                        continue
                    try:
                        from .player_reflection import _MAX_SOURCES, _completion_name, _reflection_id
                    except ImportError:
                        from player_reflection import _MAX_SOURCES, _completion_name, _reflection_id
                    conversation = self._log.get_event(row["session_id"], conversation_id)
                    sources = self._pending_conversation_sources(conversation)
                    if not self._has_conversation_reflection(conversation, sources, player_scope=scope,
                                                             cognitive_revision=revision):
                        continue
                    total = (len(sources) + _MAX_SOURCES - 1) // _MAX_SOURCES
                    ids = [_reflection_id(row["session_id"], conversation_id, index, total,
                                          player_scope=scope, cognitive_revision=revision) for index in range(total)]
                    receipt = self._log.get_projection(row["session_id"], _completion_name(row["session_id"], conversation_id),
                                                       projection_scope=scope)
                    completed_event = self._log.get_event(row["session_id"], receipt["state"]["event_id"])
                    if (row["event_id"] not in ids or receipt["state"]["reflection_id"] != ids[0]
                            or completed_event["payload"].get("reflection_ids") != ids):
                        continue
            except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
                continue
            result.append(deepcopy(payload))
            if len(result) == limit:
                break
        return list(reversed(result))

    def _resolve_sources(self, evidence: Sequence[Mapping[str, str]],
                         allowed_sessions: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """解析已提交的可见原始观察或已结束对话，不把模型结论当作证据。"""
        if not isinstance(evidence, (list, tuple)) or not 1 <= len(evidence) <= 16:
            raise ExperienceError("evidence 必须包含 1–16 条来源")
        allowed = set(_sessions(allowed_sessions)) if allowed_sessions is not None else None
        result = []
        seen = set()
        for ref in evidence:
            if (not isinstance(ref, Mapping) or not {"session_id", "event_id"} <= set(ref)
                    or set(ref) - {"session_id", "event_id", "source_id", "seq", "type"}):
                raise ExperienceError("来源引用必须包含 session_id 和 event_id")
            sid = _text(ref["session_id"], "evidence session_id")
            eid = _text(ref["event_id"], "evidence event_id")
            # 将损坏标识与实际 SQLite 读取故障区分，避免忽略数据库错误。
            try:
                _log_text(sid, "session_id")
                _log_text(eid, "event_id")
            except EventLogError as exc:
                raise ExperienceError("来源标识无效") from exc
            if (sid, eid) in seen or (allowed is not None and sid not in allowed):
                raise ExperienceError("来源重复或不属于指定会话")
            seen.add((sid, eid))
            event = self._log.get_event(sid, eid)
            if event is None:
                raise ExperienceError("来源事件不存在")
            payload = event["payload"]
            registered, _ = self._snapshot(sid, event_id=eid)
            if (len(registered) != 1 or registered[0] != payload
                    or event["source"] != "player_experience" or not isinstance(payload, dict)
                    or event["type"] not in ("experience." + RAW_OBSERVATION, "experience.conversation")
                    or payload.get("kind") != event["type"].removeprefix("experience.")
                    or payload.get("session_id") != sid or payload.get("event_id") != eid
                    or payload.get("visibility") != "player"):
                raise ExperienceError("来源不是可见的原始观察或对话")
            source_id = _text(payload.get("source_id"), "source_id")
            if any(key in ref and ref[key] != expected for key, expected in
                   (("source_id", source_id), ("seq", event["seq"]), ("type", event["type"]))):
                raise ExperienceError("来源引用与已提交事件不一致")
            result.append(event)
        return result

    def resolve_evidence(self, evidence: Sequence[Mapping[str, str]], *,
                         session_ids: Sequence[str]) -> list[dict[str, Any]]:
        """按显式会话解析证据，原样返回事件、来源、时间和 OCR 候选。"""
        return deepcopy(self._resolve_sources(evidence, allowed_sessions=session_ids))

    def _record_reflected(self, *, session_id: str, event_id: str, timestamp: str,
                          kind: str, evidence: Sequence[Mapping[str, str]],
                          fields: Mapping[str, Any], player_scope: str | None = None,
                          cognitive_revision: int | None = None) -> int:
        """将主观解释和已提交证据标识作为一个事件提交，无独立存储。"""
        sources = self._resolve_sources(evidence)
        refs = [{"session_id": event["session_id"], "event_id": event["event_id"],
                 "source_id": event["payload"]["source_id"], "seq": event["seq"],
                 "type": event["type"]} for event in sources]
        entry = {"kind": kind, "session_id": _text(session_id, "session_id"),
                 "event_id": _text(event_id, "event_id"), "timestamp": _text(timestamp, "timestamp"),
                 "visibility": "player", "verification": "subjective_unverified",
                 "evidence": refs, **fields}
        checks = {}
        if player_scope is not None:
            try:
                from .player_cognitive_transaction import PlayerCognitiveTransaction, PROJECTION, SESSION_BINDING
            except ImportError:
                from player_cognitive_transaction import PlayerCognitiveTransaction, PROJECTION, SESSION_BINDING
            if type(cognitive_revision) is not int or cognitive_revision < 0:
                raise ExperienceError("反思认知版本无效")
            binding = PlayerCognitiveTransaction(self._log)._binding(entry["session_id"], player_scope)
            checks = {"expected_session_revisions": {SESSION_BINDING: binding["revision"]},
                      "checked_scope": player_scope, "expected_scoped_revisions": {PROJECTION: cognitive_revision}}
        elif cognitive_revision is not None:
            raise ExperienceError("反思版本需要玩家归属")
        return self._log.append([{"event_id": entry["event_id"], "session_id": entry["session_id"],
                                  "timestamp": entry["timestamp"], "type": "experience." + kind,
                                  "source": "player_experience", "payload": entry}], **checks)[0]

    def record_thread_update(self, *, session_id: str, event_id: str, timestamp: str,
                             thread_id: str, title: str, status: str,
                             uncertainty: str, evidence: Sequence[Mapping[str, str]],
                             current_theory: str | None = None) -> int:
        """追加一版线索观点；状态、标题及理论均是未经验证的主观判断。"""
        if not isinstance(status, str) or status not in _THREAD_STATUSES:
            raise ExperienceError("不支持的线索状态")
        fields = {"thread_id": _text(thread_id, "thread_id"), "title": _text(title, "title"),
                  "status": status, "uncertainty": _text(uncertainty, "uncertainty")}
        if current_theory is not None:
            fields["current_theory"] = _text(current_theory, "current_theory")
        return self._record_reflected(session_id=session_id, event_id=event_id, timestamp=timestamp,
                                      kind=THREAD_UPDATE, evidence=evidence, fields=fields)

    def record_reflection(self, *, session_id: str, event_id: str, timestamp: str,
                          text: str, uncertainty: str, evidence: Sequence[Mapping[str, str]],
                          thread_id: str | None = None, player_scope: str | None = None,
                          cognitive_revision: int | None = None) -> int:
        """追加来源可追溯的主观反思；不自动生成总结或确认事实。"""
        fields = {"text": _text(text, "text"), "uncertainty": _text(uncertainty, "uncertainty")}
        if thread_id is not None:
            fields["thread_id"] = _text(thread_id, "thread_id")
        return self._record_reflected(session_id=session_id, event_id=event_id, timestamp=timestamp,
                                      kind=REFLECTION, evidence=evidence, fields=fields,
                                      player_scope=player_scope, cognitive_revision=cognitive_revision)

    def retrieve(self, session_ids: Sequence[str], *, query: str = "", limit: int = 20,
                 before_seq: int | None = None, scan_limit: int = 200,
                 kinds: Sequence[str] | None = None) -> dict[str, Any]:
        """有界跨会话召回，返回完整源事件及排他续页游标，不推断或改写内容。

        查询在最多 scan_limit 条符合类型和可见性条件的事件中做简单子串匹配，
        非语义搜索；跨会话引用不完全属于指定会话的条目会被排除。游标不表示
        一定还有结果，调用方应继续读取直到它为 None 或达到自身预算。
        """
        sessions = _sessions(session_ids)
        if len(sessions) > 64:
            raise ExperienceError("最多查询 64 个会话")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("limit 必须为 1–100 的整数")
        if type(scan_limit) is not int or not 1 <= scan_limit <= 1000:
            raise ExperienceError("scan_limit 必须为 1–1000 的整数")
        if not isinstance(query, str) or len(query) > 500:
            raise ExperienceError("query 必须是不超过 500 字的字符串")
        if kinds is None:
            selected = sorted(_RETRIEVABLE_KINDS)
        elif (not isinstance(kinds, (list, tuple)) or not kinds
              or any(not isinstance(kind, str) or kind not in _RETRIEVABLE_KINDS for kind in kinds)):
            raise ExperienceError("kinds 包含不支持的类型")
        else:
            selected = list(dict.fromkeys(kinds))
        terms = query.casefold().split()
        allowed = set(sessions)
        matches: list[dict[str, Any]] = []
        cursor = before_seq
        scanned = 0
        exhausted = False
        while scanned < scan_limit and len(matches) < limit:
            size = min(200, scan_limit - scanned)
            rows = self._log.query_events(session_ids=sessions,
                                          event_types=["experience." + kind for kind in selected],
                                          visibility="player", before_seq=cursor, limit=size)
            if not rows:
                exhausted = True
                break
            consumed = 0
            for event in rows:
                consumed += 1
                scanned += 1
                cursor = event["seq"]
                entry = event["payload"]
                if (event["source"] != "player_experience" or not isinstance(entry, dict)
                        or entry.get("kind") != event["type"].removeprefix("experience.")
                        or entry.get("session_id") != event["session_id"]
                        or entry.get("event_id") != event["event_id"]):
                    continue
                if entry["kind"] in ({RAW_OBSERVATION, "conversation"} | _CLAIM_KINDS):
                    registered, _ = self._snapshot(event["session_id"], event_id=event["event_id"])
                    if len(registered) != 1 or registered[0] != entry:
                        continue
                refs = entry.get("evidence", [])
                if (not isinstance(refs, list) or any(not isinstance(ref, dict)
                        or ref.get("session_id") not in allowed for ref in refs)):
                    continue
                if entry["kind"] in (_CLAIM_KINDS | {THREAD_UPDATE, REFLECTION}):
                    try:
                        sources = self._resolve_sources(refs, allowed_sessions=sessions)
                        if (any(source["seq"] >= event["seq"] for source in sources)
                                or (entry["kind"] in _CLAIM_KINDS and any(
                                    source["type"] != "experience.raw_observation" for source in sources))):
                            continue
                    except (ExperienceError, EventLogSchemaError, TypeError, KeyError, RecursionError, UnicodeError):
                        continue
                if entry["kind"] == "conversation":
                    turns, choices = entry.get("turns"), entry.get("choices_presented")
                    if not isinstance(turns, list) or not isinstance(choices, list):
                        continue
                    if turns or choices:
                        try:
                            self._pending_conversation_sources(event)
                        except (ExperienceError, EventLogSchemaError, TypeError, KeyError, RecursionError, UnicodeError):
                            continue
                parts = [str(entry.get(key) or "") for key in
                         ("text", "speaker", "title", "current_theory")]
                if entry["kind"] == "conversation":
                    for key in ("turns", "choices_presented"):
                        items = entry.get(key, [])
                        if isinstance(items, list):
                            parts.extend(str(item.get("text") or "") for item in items
                                         if isinstance(item, dict) and item.get("visibility") == "player")
                searchable = " ".join(parts)
                if terms and not any(term in searchable.casefold() for term in terms):
                    continue
                matches.append(event)
                if len(matches) == limit:
                    break
            if consumed == len(rows) and len(rows) < size:
                exhausted = True
                break
        return {"events": deepcopy(matches), "scanned": scanned,
                "next_before_seq": None if exhausted else cursor}

    def recent_threads(self, session_ids: Sequence[str], *, limit: int = 20,
                       scan_limit: int = 200) -> dict[str, Any]:
        """合并有界近期窗口中每个 thread_id 的最新主观版本，不宣称全库完整。"""
        page = self.retrieve(session_ids, limit=100, scan_limit=scan_limit, kinds=[THREAD_UPDATE])
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ExperienceError("limit 必须为 1–100 的整数")
        latest: dict[str, dict[str, Any]] = {}
        for event in page["events"]:
            thread_id = event["payload"].get("thread_id")
            if isinstance(thread_id, str) and thread_id not in latest:
                latest[thread_id] = event
        return {"events": list(latest.values())[:limit], "scanned": page["scanned"],
                "truncated": page["next_before_seq"] is not None or len(latest) > limit}

    def conversations(self, session_ids: Sequence[str]) -> list[dict[str, Any]]:
        """聚合同一 session/conversation 的可见原始对话，不生成摘要或猜测。"""
        groups: list[dict[str, Any]] = []
        for sid in _sessions(session_ids):
            try:
                ids = self._log.experience_conversation_ids(sid)
            except EventLogSchemaError as exc:
                raise ExperienceError("经历投影格式无效或与事件不一致") from exc
            for conversation_id in ids:
                entries, _ = self._snapshot(sid, conversation_id=conversation_id)
                group = {"session_id": sid, "conversation_id": conversation_id, "turns": []}
                for entry in entries:
                    if (entry.get("kind") != RAW_OBSERVATION or entry.get("visibility") != "player"
                            or entry.get("channel") != "dialogue"):
                        continue
                    turn = {"text": entry["text"], "event_id": entry["event_id"],
                            "source_id": entry["source_id"], "session_id": sid,
                            "timestamp": entry.get("timestamp")}
                    if "speaker" in entry:
                        turn["speaker"] = entry["speaker"]
                        turn["speaker_status"] = entry.get("speaker_status", "candidate")
                    group["turns"].append(turn)
                if group["turns"]:
                    groups.append(group)
        return groups
