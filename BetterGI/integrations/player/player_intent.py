#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家意图（MotorIntent）与执行反馈的状态机。

意图表达「我想做什么」，不包含具体按键；Lumine 只负责把意图转成
200ms 动作。同一时刻只允许一个主意图；新意图会取消旧意图。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

ACTIVE_STATUSES = {"created", "sent", "running", "blocked", "awaiting_observation"}
TERMINAL_STATUSES = {"done", "failed", "cancelled", "expired"}
FEEDBACK_STATUSES = {"progress", "done", "blocked", "failed", "cancelled"}
MOTOR_MODES = {"navigate", "interact", "combat", "explore", "ui", "wait", "dialogue"}
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class IntentError(ValueError):
    """意图/反馈结构非法。"""


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = CONTROL_CHARS.sub("", value)
    return " ".join(value.split())[:limit]


def _list(value: Any, limit: int, item_limit: int) -> List[str]:
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


def new_intent_id(now_ms: Optional[int] = None) -> str:
    moment = time.gmtime((time.time() if now_ms is None else now_ms / 1000.0))
    return "i-" + time.strftime("%Y%m%d-%H%M%S", moment) + "-" + uuid.uuid4().hex


def _attribution(data: Dict[str, Any]) -> Dict[str, Any]:
    """保留已验证的 Motor 代际及可选动作组；不接受布尔或不完整代际。"""
    result: Dict[str, Any] = {}
    if "motorGeneration" in data:
        generation = data["motorGeneration"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise IntentError("motorGeneration 必须是非负整数")
        result["motorGeneration"] = generation
    if "actionGroupId" in data:
        group = data["actionGroupId"]
        if not isinstance(group, str) or not group.strip() or group != group.strip() or len(group) > 128:
            raise IntentError("actionGroupId 非法")
        result["actionGroupId"] = group
    return result


def normalize_intent(data: Any, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise IntentError("intent 必须是 JSON 对象")
    goal = _text(data.get("goal"), 300)
    if not goal:
        raise IntentError("intent.goal 不能为空")
    intent_id = _text(data.get("intent_id"), 80)
    if not intent_id:
        intent_id = new_intent_id(now_ms)
    if _SAFE_ID.sub("-", intent_id) != intent_id:
        raise IntentError("intent_id 只能包含字母、数字、点、下划线和连字符")
    motor_mode = _text(data.get("motor_mode"), 20).lower() if "motor_mode" in data else "navigate"
    if motor_mode not in MOTOR_MODES:
        raise IntentError("intent.motor_mode 不受支持")
    try:
        deadline = int(data.get("deadline_ms") or 15000)
    except (TypeError, ValueError):
        deadline = 15000
    deadline = min(60000, max(1000, deadline))
    target = data.get("target")
    target = dict(target) if isinstance(target, dict) else {}
    if "completion_condition" in target:
        condition = target["completion_condition"]
        if (not isinstance(condition, dict) or set(condition) != {"kind", "region", "text"}
                or condition.get("kind") != "ocr_text_visible" or condition.get("region") not in {"quest", "hud"}
                or not isinstance(condition.get("text"), str) or not condition["text"]
                or condition["text"] != _text(condition["text"], 200)):
            raise IntentError("屏幕完成条件必须是 quest/hud 区域的明确 OCR 文本")
        target["completion_condition"] = dict(condition)
    version = data.get("version")
    if version is None:
        version = 2
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise IntentError("intent.version 必须是正整数")
    return {
        "type": "intent",
        "version": version,
        "intent_id": intent_id,
        "goal": goal,
        "reason": _text(data.get("reason"), 400),
        "constraints": _list(data.get("constraints"), 8, 200),
        "motor_mode": motor_mode,
        "target": target,
        "interrupt_conditions": _list(data.get("interrupt_conditions"), 8, 60),
        "deadline_ms": deadline,
        "created_ms": int(now_ms if now_ms is not None else time.time() * 1000),
        **_attribution(data),
    }


def make_dialogue_intent(*, action: str, event_session_id: str, capture_session_id: str,
                         frame_id: str, frame_sha256: str, captured_at_ms: int,
                         source_event_id: str, thought_id: str, player_generation: int,
                         frame_seq: Optional[int] = None, option_id: Optional[str] = None, option_index: Optional[int] = None,
                         option_text: Optional[str] = None) -> Dict[str, Any]:
    """构建绑定原始帧的对话决策；执行只由宿主确定性控制器授权。"""
    if action not in {"advance", "select"}:
        raise IntentError("对话意图动作无效")
    for value in (event_session_id, capture_session_id, frame_id, source_event_id, thought_id):
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise IntentError("对话意图来源标识无效")
    if (not isinstance(frame_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", frame_sha256)
            or type(captured_at_ms) is not int or captured_at_ms <= 0
            or (frame_seq is not None and (type(frame_seq) is not int or frame_seq <= 0))
            or type(player_generation) is not int or player_generation < 0):
        raise IntentError("对话意图帧来源无效")
    if ((action == "select" and (not isinstance(option_id, str)
            or not re.fullmatch(r"choice-[0-9a-f]{64}", option_id)
            or type(option_index) is not int or not 0 <= option_index < 8
            or not isinstance(option_text, str) or not option_text.strip()
            or option_text != " ".join(option_text.split()) or len(option_text) > 500))
            or (action == "advance" and (option_id is not None or option_index is not None or option_text is not None))):
        raise IntentError("对话意图选项来源无效")
    source = {"event_session_id": event_session_id, "capture_session_id": capture_session_id,
              "frame_id": frame_id, "frame_seq": frame_seq, "frame_sha256": frame_sha256, "captured_at_ms": captured_at_ms,
              "source_event_id": source_event_id, "thought_id": thought_id,
              "decision_id": thought_id, "player_generation": player_generation}
    identity = json.dumps([action, source, option_id, option_index, option_text], ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    return {"type": "dialogue_intent", "intent_id": "di-" + hashlib.sha256(identity.encode("ascii")).hexdigest(),
            "action": action, "source": source, "option_id": option_id, "option_index": option_index,
            "option_text": option_text, "execution": "not_executed"}


def normalize_feedback(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise IntentError("motor_feedback 必须是 JSON 对象")
    intent_id = _text(data.get("intent_id"), 80)
    if not intent_id:
        raise IntentError("motor_feedback.intent_id 不能为空")
    if "motorGeneration" in data and data.get("intent_id") != intent_id:
        raise IntentError("motor_feedback.intent_id 必须与原始 ID 完全一致")
    status = _text(data.get("status"), 20).lower()
    if status not in FEEDBACK_STATUSES:
        raise IntentError("motor_feedback.status 非法：{0!r}".format(status))
    reason = _text(data.get("reason"), 200)
    observation = _text(data.get("observation"), 400)
    if status == "done":
        status = "progress"
        reason = "motor_reported_done_unconfirmed"
        observation = "Motor 自报完成；游戏响应和目标结果尚未由独立世界观察确认。"
    result = {
        "type": "motor_feedback",
        "intent_id": intent_id,
        "status": status,
        "reason": reason,
        "observation": observation,
        "t": int(data.get("t") or time.time() * 1000),
        **_attribution(data),
    }
    if "seq" in data:
        if type(data["seq"]) is not int or data["seq"] <= 0:
            raise IntentError("motor_feedback.seq 必须是正整数")
        result["seq"] = data["seq"]
    return result


@dataclass
class IntentTracker:
    """单写者的意图生命周期；供 PlayerAgent/桥共用的纯状态机。"""

    max_failures: int = 3
    current: Optional[Dict[str, Any]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    feedback: List[Dict[str, Any]] = field(default_factory=list)
    consecutive_failures: int = 0
    needs_replan: bool = False

    def create(self, intent: Any, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
        normalized = normalize_intent(intent, now_ms=now_ms)
        if self.current is not None:
            previous = dict(self.current)
            if previous.get("status") in ACTIVE_STATUSES:
                previous["status"] = "cancelled"
                previous["reason"] = "replaced_by_new_intent"
            self.history.append(previous)
        normalized["status"] = "created"
        normalized["updated_ms"] = normalized["created_ms"]
        self.current = normalized
        self.needs_replan = False
        return dict(normalized)

    def mark_sent(self, intent_id: str, *, now_ms: Optional[int] = None) -> bool:
        if not self._matches(intent_id) or self.current.get("status") in TERMINAL_STATUSES:
            return False
        self.current["status"] = "sent"
        self.current["updated_ms"] = int(now_ms or time.time() * 1000)
        return True

    def apply_feedback(self, data: Any, *, now_ms: Optional[int] = None,
                       live_session_id: Optional[str] = None,
                       receipt_event_id: Optional[str] = None) -> bool:
        feedback = normalize_feedback(data)
        if not self._matches(feedback["intent_id"]) or self.current.get("status") in TERMINAL_STATUSES:
            return False
        # v2 反馈必须与发布时桥返回的完整归属一致；不能仅凭同名 ID 推断代际。
        keys = ("motorGeneration", "actionGroupId")
        if any(self.current.get(key) != feedback.get(key) for key in keys):
            return False
        if "motorGeneration" not in self.current and any(
            item.get("intent_id") == feedback["intent_id"] for item in self.history
        ):
            # 旧协议没有代际时，同 ID 重用无法安全区分旧反馈。
            return False
        status = feedback["status"]
        self.feedback.append(feedback)
        self.feedback = self.feedback[-20:]
        if status == "progress":
            self.current["status"] = ("awaiting_observation"
                                      if feedback["reason"] == "motor_reported_done_unconfirmed" else "running")
            if feedback["reason"] in {"input_system_accepted", "motor_reported_done_unconfirmed"}:
                self.current["game_response_confirmed"] = None
                self.needs_replan = True
                timestamp = feedback["t"]
                now = int(now_ms if now_ms is not None else time.time() * 1000)
                previous = self.current.get("last_system_input")
                if (feedback["reason"] == "input_system_accepted" and type(feedback.get("seq")) is int
                        and type(feedback.get("motorGeneration")) is int and 0 < timestamp <= now
                        and isinstance(live_session_id, str) and live_session_id
                        and isinstance(receipt_event_id, str) and receipt_event_id
                        and (not isinstance(previous, dict) or
                             (previous.get("live_session_id") == live_session_id
                              and (feedback["seq"], timestamp) >
                                  (previous.get("seq", -1), previous.get("received_ms", -1))))):
                    self.current["last_system_input"] = {
                        "intent_id": feedback["intent_id"], "seq": feedback["seq"],
                        "motorGeneration": feedback["motorGeneration"], "live_session_id": live_session_id,
                        "receipt_event_id": receipt_event_id, "received_ms": timestamp,
                        "game_response_confirmed": None,
                    }
                    if "actionGroupId" in feedback:
                        self.current["last_system_input"]["actionGroupId"] = feedback["actionGroupId"]
        elif status == "cancelled":
            self.current["status"] = "cancelled"
            self.current["reason"] = feedback["reason"]
        elif status in ("blocked", "failed"):
            self.current["status"] = status
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.max_failures:
                self.needs_replan = True
        self.current["updated_ms"] = int(now_ms or time.time() * 1000)
        return True

    def _mark_observed_completion(self, *, intent_id: str, motor_generation: int, action_seq: int,
                                  live_session_id: str, receipt_event_id: str,
                                  observation_event_id: str, completion_event_id: str,
                                  observed_ms: int) -> bool:
        """仅供 Player 在持久化并核验同场正向观察后调用。"""
        if (not self._matches(intent_id) or self.current.get("status") not in ACTIVE_STATUSES
                or type(motor_generation) is not int or type(action_seq) is not int or action_seq <= 0
                or type(observed_ms) is not int or observed_ms <= 0
                or not all(isinstance(item, str) and item for item in
                           (live_session_id, receipt_event_id, observation_event_id, completion_event_id))):
            return False
        receipt = self.current.get("last_system_input")
        if (not isinstance(receipt, dict) or self.current.get("motorGeneration") != motor_generation
                or receipt.get("intent_id") != intent_id or receipt.get("motorGeneration") != motor_generation
                or receipt.get("seq") != action_seq or receipt.get("live_session_id") != live_session_id
                or receipt.get("receipt_event_id") != receipt_event_id
                or type(receipt.get("received_ms")) is not int or observed_ms <= receipt["received_ms"]):
            return False
        self.current["status"] = "done"
        self.current["completion"] = {
            "kind": "screen_observation_matched", "verification": "ocr_candidate",
            "event_id": completion_event_id, "observation_event_id": observation_event_id,
            "receipt_event_id": receipt_event_id, "live_session_id": live_session_id,
            "motorGeneration": motor_generation, "seq": action_seq,
            "observed_ms": observed_ms, "game_response_confirmed": None,
        }
        self.current["updated_ms"] = observed_ms
        self.consecutive_failures = 0
        self.needs_replan = False
        return True

    def cancel(self, intent_id: str, reason: str = "cancelled") -> bool:
        if not self._matches(intent_id):
            return False
        self.current["status"] = "cancelled"
        self.current["reason"] = _text(reason, 200)
        return True

    def expire(self, *, now_ms: Optional[int] = None) -> bool:
        if self.current is None or self.current.get("status") not in ACTIVE_STATUSES:
            return False
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if now - int(self.current.get("created_ms", now)) < int(
            self.current.get("deadline_ms") or 15000
        ):
            return False
        self.current["status"] = "expired"
        self.current["updated_ms"] = now
        self.consecutive_failures += 1
        self.needs_replan = True
        return True

    def export(self) -> Dict[str, Any]:
        return {
            "current": dict(self.current) if self.current else None,
            "history": self.history[-20:],
            "recent_feedback": self.feedback[-5:],
            "consecutive_failures": self.consecutive_failures,
            "needs_replan": self.needs_replan,
        }

    def _matches(self, intent_id: str) -> bool:
        return bool(
            self.current is not None
            and self.current.get("intent_id") == intent_id
        )


class IntentPublisher:
    """把主意图发给本机 Bridge 的 v2 意图总线；只发意图，不发按键。"""

    def __init__(self, url: str, token: str = "", timeout: float = 5.0):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise ValueError("意图总线必须是本机 loopback HTTP 地址")
        if parts.path not in ("", "/") or parts.query or parts.username or parts.password:
            raise ValueError("意图总线地址必须是服务根地址")
        if aiohttp is None:
            raise RuntimeError("需要 aiohttp")
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = float(timeout)
        # 保留 Bridge 最近一次撤销响应，便于暴露部分停止失败。
        self.last_cancel_result: Dict[str, Any] = {}

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    async def publish(self, intent: Any) -> bool:
        try:
            normalized = normalize_intent(intent)
            if normalized["motor_mode"] == "dialogue":
                return False
        except IntentError:
            return False
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.url + "/intent",
                    json={"intent": normalized},
                    headers=self._headers(),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        return False
                    data = await response.json()
                    if not isinstance(data, dict) or data.get("accepted") is not True or data.get("intent_id") != normalized["intent_id"]:
                        return False
                    attribution = _attribution(data)
                    if "motorGeneration" not in attribution or attribution.get("actionGroupId") != normalized.get("actionGroupId"):
                        return False
                    # PlayerAgent 在发布成功后基于同一字典创建 tracker；让其绑定桥的权威代际。
                    intent.update(attribution)
                    return True
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    async def publish_dialogue(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """只向宿主提交同帧决策；超时不重发且不能推断未执行。"""
        try:
            source = intent["source"]
            canonical = make_dialogue_intent(
                action=intent["action"], event_session_id=source["event_session_id"],
                capture_session_id=source["capture_session_id"], frame_id=source["frame_id"],
                frame_seq=source["frame_seq"], frame_sha256=source["frame_sha256"],
                captured_at_ms=source["captured_at_ms"], source_event_id=source["source_event_id"],
                thought_id=source["thought_id"], player_generation=source["player_generation"],
                option_id=intent.get("option_id"), option_index=intent.get("option_index"),
                option_text=intent.get("option_text"))
            if canonical != intent or type(source["frame_seq"]) is not int:
                raise IntentError("对话意图不完整")
        except (IntentError, KeyError, TypeError, ValueError):
            return {"status": "not_executed", "reason": "invalid_dialogue_intent"}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.post(self.url + "/dialogue/decision", json={"intent": canonical},
                                        headers=self._headers(), allow_redirects=False) as response:
                    if response.status != 200:
                        return {"status": "unknown", "reason": "dialogue_transport_unconfirmed"}
                    result = await response.json()
                    if (not isinstance(result, dict) or result.get("intentId") != canonical["intent_id"]
                            or type(result.get("executed")) not in (bool, type(None))
                            or type(result.get("systemInputAccepted")) not in (bool, type(None))
                            or result.get("gameResponseConfirmed") is not None
                            or (result.get("executed") is True and result.get("systemInputAccepted") is not True)
                            or not isinstance(result.get("reason"), str)):
                        return {"status": "unknown", "reason": "dialogue_receipt_unconfirmed"}
                    status = ("input_sent" if result.get("executed") is True else
                              "not_executed" if result.get("executed") is False else "unknown")
                    return {"status": status, "reason": result["reason"],
                            "system_input_accepted": result.get("systemInputAccepted"),
                            "game_response_confirmed": None}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {"status": "unknown", "reason": "dialogue_transport_unconfirmed"}

    async def fetch_feedback(self) -> Dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    self.url + "/feedback",
                    headers=self._headers(),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        return {}
                    data = await response.json()
                    return data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return {}

    async def cancel(self) -> bool:
        """请求 Bridge 清空当前意图（例如对话开始）。"""
        self.last_cancel_result = {}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.url + "/intent",
                    json={"cancel": True},
                    headers=self._headers(),
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        return False
                    data = await response.json()
                    if not isinstance(data, dict):
                        return False
                    self.last_cancel_result = dict(data)
                    # 本地取消不等于远端停止；缺失发送状态也不能宣称安全。
                    return (data.get("accepted") is True and data.get("cancelled") is True and
                            all(data.get(key) is True for key in ("remoteStopSent", "inputReleaseSent")))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False
