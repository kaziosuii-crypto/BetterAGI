#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""保守的剧情/对话感知，不对 Live 图像虚构视觉识别能力。

observe 接受同一次 BetterGI observe 的 UI 与 OCR；observe_live_frame 接受原始
Live frame。Live 可提供同帧 UI 分类或可选区域诊断，但不携带台词 OCR；
不能把诊断候选当成解除对话锁的授权。
对话/说话人/选项 ROI 是由调用方校准的归一化矩形 (left, top, right, bottom)，没有
默认值：不同分辨率、语言、界面可能需要不同区域。未配置或缺失框时不提取文字。
旧 events 类型维持兼容；dialogue_events 给出 dialogue_line_changed 和
 dialogue_choice_presented 等完整名称。结束须同时有非对话状态和覆盖两个区域的
明确空 OCR；未校准、未执行或结果不完整时保持锁定。
"""

from __future__ import annotations

import copy
import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from player_io import iso_to_epoch_ms

MAX_DIALOGUE_ITEMS = 8
LOCK_HOLD_MS = 4000  # 兼容旧调用方；不再用经过时间自动释放锁。
Rect = Tuple[float, float, float, float]


def _text(value: Any, limit: int = 500) -> str:
    """规范化显示文本，不推断其含义。"""
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _rect(value: Optional[Tuple[float, float, float, float]]) -> Optional[Rect]:
    """校验调用方配置的归一化区域。"""
    if value is None:
        return None
    if (not isinstance(value, (tuple, list)) or len(value) != 4
            or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in value)):
        raise ValueError("ROI 必须是四个归一化坐标")
    left, top, right, bottom = map(float, value)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("ROI 必须在画面内且有正面积")
    return left, top, right, bottom


def _inside(row: Dict[str, Any], roi: Optional[Rect]) -> bool:
    """只接纳完整位于 ROI 的 OCR 框；BetterGI 的归一化 x/y 是中心。"""
    box = row.get("normalized_bbox")
    if roi is None or not isinstance(box, dict):
        return False
    values = [box.get(k) for k in ("x", "y", "width", "height")]
    if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not math.isfinite(n) for n in values):
        return False
    x, y, width, height = values
    if width <= 0 or height <= 0:
        return False
    left, top, right, bottom = roi
    return left <= x - width / 2 and x + width / 2 <= right and top <= y - height / 2 and y + height / 2 <= bottom


def _empty_regions(visual: Dict[str, Any], dialogue_roi: Optional[Rect], choice_roi: Optional[Rect],
                   *, choice_only: bool = False) -> bool:
    """只用覆盖目标区域、明确完成且无原始检测的识别结果作阴性证据。"""
    targets = (("choice", choice_roi),) if choice_only else (("dialogue_text", dialogue_roi), ("choice", choice_roi))
    if any(target is None for _, target in targets) or visual.get("ocr_requested") is not True:
        return False
    rows = visual.get("ocr")
    named = visual.get("ocr_regions")
    roi = visual.get("ocr_roi")
    width, height = visual.get("original_width"), visual.get("original_height")
    if not isinstance(rows, list) or type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return False
    if isinstance(named, dict) and named:
        for name, target in targets:
            region = named.get(name)
            if (not isinstance(region, dict) or region.get("completed") is not True
                    or type(region.get("raw_detection_count")) is not int or region["raw_detection_count"] != 0):
                return False
            bounds = region.get("roi")
            if not isinstance(bounds, dict):
                return False
            x, y, w, h = (bounds.get(k) for k in ("x", "y", "width", "height"))
            if (any(type(n) is not int for n in (x, y, w, h)) or x < 0 or y < 0 or w <= 0 or h <= 0
                    or x + w > width or y + h > height):
                return False
            coverage = (x / width, y / height, (x + w) / width, (y + h) / height)
            if not (coverage[0] <= target[0] and coverage[1] <= target[1]
                    and coverage[2] >= target[2] and coverage[3] >= target[3]):
                return False
        target_names = {name for name, _ in targets}
        return not any(not isinstance(row, dict) or row.get("region") not in named
                       or row.get("region") in target_names for row in rows)
    # 旧 API 没有原始检测数，不能把经过过滤的空列表当作无检测。
    if visual.get("ocr_complete") is not True or visual.get("ocr_empty_verified") is False:
        return False
    if not isinstance(roi, dict):
        return False
    try:
        x, y, w, h = (roi[k] for k in ("x", "y", "width", "height"))
    except KeyError:
        return False
    if any(type(n) is not int for n in (x, y, w, h)) or x < 0 or y < 0 or w <= 0 or h <= 0:
        return False
    # 服务端可能裁剪超出画面的请求，故以实际帧边界为准。
    coverage = (x / width, y / height, min(x + w, width) / width, min(y + h, height) / height)
    if any(not (coverage[0] <= r[0] and coverage[1] <= r[1] and coverage[2] >= r[2] and coverage[3] >= r[3])
           for _, r in targets):
        return False
    # 无框或无文字的异常行不能用于证明区域为空。
    for row in rows:
        if not isinstance(row, dict) or not _text(row.get("text")) or not _inside(row, (0, 0, 1, 1)):
            return False
        if any(_inside(row, target) for _, target in targets):
            return False
        # 与目标区域部分相交的行也不能证明目标区域为空。
        box = row["normalized_bbox"]
        left, top = box["x"] - box["width"] / 2, box["y"] - box["height"] / 2
        right, bottom = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        if any(left < r[2] and right > r[0] and top < r[3] and bottom > r[1] for _, r in targets):
            return False
    return True


def _source(visual: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """保留服务实际提供的帧标识和采集时间，不以处理时间冒充采集时间。"""
    timestamp = visual.get("captured_at_ms")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        timestamp = iso_to_epoch_ms(visual.get("timestampUtc") or visual.get("timestamp"))
    return {
        "kind": kind,
        "session_id": visual.get("session_id") or visual.get("sessionId"),
        "frame_id": visual.get("frame_id"),
        "frame_sha256": visual.get("frame_sha256"),
        "frame_seq": visual.get("seq"),
        "timestamp_ms": timestamp,
    }


class DialogueTracker:
    """有界聚合当前对话和事件；未知/失帧不会按计时器释放锁。"""

    def __init__(self, *, clock: Any = None, dialogue_roi: Optional[Rect] = None,
                 choice_roi: Optional[Rect] = None, speaker_roi: Optional[Rect] = None,
                 non_dialogue_frames: int = 3):
        if isinstance(non_dialogue_frames, bool) or not isinstance(non_dialogue_frames, int) or non_dialogue_frames < 2:
            raise ValueError("退出对话至少需要两个有效非对话帧")
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.dialogue_roi = _rect(dialogue_roi)
        self.choice_roi = _rect(choice_roi)
        self.speaker_roi = _rect(speaker_roi)
        self.non_dialogue_frames = non_dialogue_frames
        self.locked = False
        self.lock_until_ms = 0
        self.recent: Deque[Dict[str, Any]] = deque(maxlen=MAX_DIALOGUE_ITEMS)
        self.started_ms = 0
        self.last_seen_ms = 0
        self.start_count = 0
        self.turns = 0
        self.conversation: Optional[Dict[str, Any]] = None
        self._negative_count = 0
        self._negative_evidence: List[Dict[str, Any]] = []
        self._seen: Deque[Tuple[Any, ...]] = deque(maxlen=256)
        self._last_timestamp: Dict[str, int] = {}
        self._last_talk_timestamp: Optional[int] = None
        self._talk_time_unknown = False
        self._unknown_talk_seen_ms: Optional[int] = None
        self._unknown_talk_session: Optional[str] = None
        self._last_live_seq: Optional[int] = None
        self._last_line: Tuple[str, Optional[str]] = ("", None)
        self._last_choices: Tuple[str, ...] = ()
        self._choice_snapshot: Optional[Dict[str, Any]] = None
        self._cutscene: Optional[bool] = None
        self._session_id: Optional[str] = None

    def _result(self, events: List[Dict[str, Any]], text: str = "", *, include_conversation: bool = True) -> Dict[str, Any]:
        """保留既有锁接口；实时路径可省略较大的聚合快照。"""
        types = {event["type"] for event in events}
        canonical_names = {"line_changed": "dialogue_line_changed", "choice_presented": "dialogue_choice_presented"}
        canonical_events = [{**event, "type": canonical_names.get(event["type"], event["type"])} for event in events]
        return {"in_talk": self.locked, "started": "dialogue_started" in types,
                "ended": "dialogue_ended" in types, "text": text,
                "recent": copy.deepcopy(list(self.recent)[-3:]),
                "events": copy.deepcopy(events), "dialogue_events": copy.deepcopy(canonical_events),
                "choice_snapshot": copy.deepcopy(self._choice_snapshot) if include_conversation else None,
                "conversation": copy.deepcopy(self.conversation) if include_conversation else None}

    def observe_live_frame(self, frame: Dict[str, Any], *, now_ms: Optional[int] = None,
                           include_conversation: bool = False) -> Dict[str, Any]:
        """轻量处理 Live 元数据；不解码图片、不调用 OCR 或网络。"""
        return self._observe(frame, "live", now_ms, include_conversation)

    def observe(self, visual: Dict[str, Any], *, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """处理一次独立截图的同帧 UI/OCR，不拼接其它 Live 帧的 OCR。"""
        return self._observe(visual, "observe", now_ms, True)

    def _observe(self, visual: Dict[str, Any], kind: str, now_ms: Optional[int],
                 include_conversation: bool) -> Dict[str, Any]:
        """按三态 UI 信号更新状态；只有连续、不同的有效非对话帧才能退出。"""
        visual = visual if isinstance(visual, dict) else {}
        if kind == "observe":
            self._choice_snapshot = None
        now = int(now_ms if now_ms is not None else self._clock())
        source = _source(visual, kind)
        frame_id, seq, timestamp = source["frame_id"], source["frame_seq"], source["timestamp_ms"]
        session = source["session_id"]
        if isinstance(session, str) and session:
            if self._session_id is not None and session != self._session_id:
                self._negative_count = 0
                return self._result([], include_conversation=include_conversation)
            self._session_id = session
        elif self._session_id is not None and kind == "observe":
            self._negative_count = 0
            return self._result([], include_conversation=include_conversation)
        # 观察帧由 frame_id 标识，Live 帧由序号标识；不可用附加序号让同帧绕过去重。
        valid_frame_id = frame_id if isinstance(frame_id, str) and frame_id else None
        valid_seq = seq if type(seq) is int and seq >= 0 else None
        identity = (kind, source["session_id"], valid_frame_id if kind == "observe" else None,
                    valid_seq if kind == "live" else None)
        has_identity = identity[2] is not None or identity[3] is not None
        if has_identity and identity in self._seen:
            return self._result([], include_conversation=include_conversation)
        last_timestamp = self._last_timestamp.get(kind)
        if timestamp is not None and last_timestamp is not None and timestamp < last_timestamp:
            self._negative_count = 0
            return self._result([], include_conversation=include_conversation)
        if has_identity:
            self._seen.append(identity)
        if kind == "live" and identity[3] is not None:
            # Live 不参与独立截图的阴性帧计数，序号缺口也不破坏该计数。
            self._last_live_seq = seq

        category = visual.get("ui_category", visual.get("uiCategory"))
        category = category.strip().lower() if isinstance(category, str) else ""
        flag = visual.get("is_in_talk_ui", visual.get("isInTalkUi"))
        # 互相矛盾的信号视为未知；Unknown 加 false 也不是肯定的非对话。
        if not has_identity:
            state = "unknown"
        elif (category == "talk" and flag is False) or (category in ("main", "bigmap") and flag is True):
            state = "unknown"
        elif category == "talk" or flag is True:
            state = "talk"
        elif category in ("main", "bigmap") and flag is not True:
            state = "non_dialogue"
        else:
            state = "unknown"
        # 仅已验证、同会话的新鲜截图且采集晚于无时间 Talk 的到达时刻，
        # 才能重建可比较的时间基线；未知来源必须显式 reset。
        if (self._talk_time_unknown and kind == "observe" and has_identity
                and state != "unknown" and visual.get("source_verified") is True
                and self._unknown_talk_session is not None and session == self._unknown_talk_session
                and timestamp is not None and self._unknown_talk_seen_ms is not None
                and self._unknown_talk_seen_ms <= timestamp <= now and now - timestamp <= 15000):
            self._talk_time_unknown = False
            self._last_talk_timestamp = max(self._last_talk_timestamp or 0, timestamp)
            self._unknown_talk_seen_ms = None
            self._unknown_talk_session = None
            self._negative_count = 0
        # 各来源各自排序：异步 observe 的同帧 OCR 不因后到的 Live 帧被丢弃。
        if state != "unknown" and timestamp is not None:
            self._last_timestamp[kind] = timestamp
        events: List[Dict[str, Any]] = []

        def emit(event_type: str, **fields: Any) -> None:
            events.append({"type": event_type, "source": dict(source), **fields})

        # 可由上游明确提供 cutscene 信号；BetterGI 本身当前不提供此信号。
        cutscene = visual.get("cutscene")
        if isinstance(cutscene, bool) and cutscene != self._cutscene:
            if cutscene or self._cutscene is True:
                emit("cutscene", active=cutscene)
            self._cutscene = cutscene

        text = ""
        if state == "talk":
            self._negative_count = 0
            if timestamp is None:
                # 无法比较无时间的 Talk 与之后收到的慢截图，只能保持锁定。
                if not self._talk_time_unknown:
                    self._unknown_talk_session = self._session_id
                self._unknown_talk_seen_ms = now
                self._talk_time_unknown = True
            elif self._last_talk_timestamp is None or timestamp > self._last_talk_timestamp:
                self._last_talk_timestamp = timestamp
            if not self.locked:
                self.locked = True
                self.started_ms = now
                self.start_count += 1
                self.recent.clear()
                self._last_line = ("", None)
                self._last_choices = ()
                self.conversation = {"started": dict(source), "ended": None, "lines": [], "choices": [], "entries": []}
                emit("dialogue_started")
            self.last_seen_ms = now
            self.lock_until_ms = now + LOCK_HOLD_MS
            # 仅独立 observe 响应允许携带 OCR；实时帧与截图不共享来源。
            rows = (visual.get("ocr") if kind == "observe" and isinstance(frame_id, str)
                    and frame_id and timestamp is not None and visual.get("ocr_requested") is not False
                    and isinstance(visual.get("ocr"), list) else [])
            named = visual.get("ocr_regions")

            def matches_region(row: Dict[str, Any], name: str, roi: Optional[Rect]) -> bool:
                """具名结果只能用于对应的区域，且该区域必须完成。"""
                if isinstance(named, dict) and named:
                    status = named.get(name)
                    if (row.get("region") != name or not isinstance(status, dict)
                            or status.get("completed") is not True):
                        return False
                    bounds = status.get("roi")
                    width, height = visual.get("original_width"), visual.get("original_height")
                    if (not isinstance(bounds, dict) or type(width) is not int or type(height) is not int
                            or width <= 0 or height <= 0):
                        return False
                    x, y, w, h = (bounds.get(key) for key in ("x", "y", "width", "height"))
                    if (any(type(n) is not int for n in (x, y, w, h)) or x < 0 or y < 0
                            or w <= 0 or h <= 0 or x + w > width or y + h > height
                            or not _inside(row, (x / width, y / height, (x + w) / width, (y + h) / height))):
                        return False
                return bool(_text(row.get("text"))) and _inside(row, roi)

            line_rows = [row for row in rows if isinstance(row, dict) and matches_region(row, "dialogue_text", self.dialogue_roi)]
            choice_rows = [row for row in rows if isinstance(row, dict) and matches_region(row, "choice", self.choice_roi)]
            speaker_rows = [row for row in rows if isinstance(row, dict) and matches_region(row, "speaker", self.speaker_roi)
                            and not _inside(row, self.dialogue_roi) and not _inside(row, self.choice_roi)]
            # OCR 返回顺序不保证为阅读顺序，按中心点从上到下、从左到右排列。
            for group in (line_rows, choice_rows, speaker_rows):
                group.sort(key=lambda row: (row["normalized_bbox"]["y"], row["normalized_bbox"]["x"]))
            text = " ".join(_text(row.get("text")) for row in line_rows)
            speaker = " ".join(_text(row.get("text")) for row in speaker_rows) or None
            if text and (text, speaker) != self._last_line:
                line = {"text": text, "speaker": speaker, "candidate": True,
                        "source": dict(source), "rows": copy.deepcopy(line_rows),
                        "dialogue_roi": self.dialogue_roi, "speaker_roi": self.speaker_roi,
                        "speaker_rows": copy.deepcopy(speaker_rows)}
                self.conversation["lines"].append(line)
                self.conversation["entries"].append({"type": "line", "value": line})
                self.recent.append({"text": text, "t_ms": now, "source": dict(source)})
                self._last_line = (text, speaker)
                self.turns += 1
                emit("line_changed", line=line)
            choices = tuple(_text(row.get("text")) for row in choice_rows)
            if kind == "observe" and choices:
                self._choice_snapshot = {"source": dict(source), "rows": copy.deepcopy(choice_rows)}
            if choices and choices != self._last_choices:
                entry = {"texts": list(choices), "candidate": True, "choice_roi": self.choice_roi,
                         "source": dict(source), "rows": copy.deepcopy(choice_rows)}
                self.conversation["choices"].append(entry)
                self.conversation["entries"].append({"type": "choices", "value": entry})
                self._last_choices = choices
                emit("choice_presented", choices=entry)
            elif not choices and kind == "observe" and _empty_regions(visual, None, self.choice_roi, choice_only=True):
                self._last_choices = ()
        elif (state == "non_dialogue" and self.locked and kind == "observe" and has_identity
              and timestamp is not None and not self._talk_time_unknown
              and (self._last_talk_timestamp is None or timestamp > self._last_talk_timestamp)
              and _empty_regions(visual, self.dialogue_roi, self.choice_roi)):
            if self._negative_count == 0:
                self._negative_evidence.clear()
            self._negative_evidence.append({"source": dict(source), "ui_category": category,
                                            "is_in_talk_ui": flag,
                                            "ocr_roi": dict(visual["ocr_roi"]) if isinstance(visual.get("ocr_roi"), dict) else None,
                                            "ocr_regions": copy.deepcopy(visual.get("ocr_regions")),
                                            "original_width": visual["original_width"],
                                            "original_height": visual["original_height"],
                                            "dialogue_roi": self.dialogue_roi, "choice_roi": self.choice_roi})
            self._negative_count += 1
            if self._negative_count >= self.non_dialogue_frames:
                self.locked = False
                self._negative_count = 0
                self.conversation["ended"] = dict(source)
                self.conversation["end_evidence"] = copy.deepcopy(self._negative_evidence)
                emit("dialogue_ended", evidence=copy.deepcopy(self._negative_evidence))
                self._negative_evidence.clear()
        elif not (kind == "live" and has_identity and
                  (state == "non_dialogue" or (state == "unknown" and category in ("", "unknown")
                                                and (flag is None or flag is False)))):
            # 普通 Live 的未知或非对话状态不提供空 OCR，也不打断独立截图流。
            # 截图失败、缺失标识和显式矛盾则中断连续阴性。
            self._negative_count = 0
        return self._result(events, text, include_conversation=include_conversation)

    def reset(self) -> None:
        """显式清理会话状态；配置与统计计数保留。"""
        self.locked = False
        self.lock_until_ms = 0
        self.started_ms = 0
        self.last_seen_ms = 0
        self.recent.clear()
        self.conversation = None
        self._negative_count = 0
        self._negative_evidence.clear()
        self._seen.clear()
        self._last_timestamp.clear()
        self._last_talk_timestamp = None
        self._talk_time_unknown = False
        self._unknown_talk_seen_ms = None
        self._unknown_talk_session = None
        self._last_live_seq = None
        self._last_line = ("", None)
        self._last_choices = ()
        self._choice_snapshot = None
        self._cutscene = None
        self._session_id = None

    def is_locked(self, *, now_ms: Optional[int] = None) -> bool:
        """时间流逝和缺失观察都不是对话结束的证据。"""
        return self.locked

    def context_lines(self, limit: int = 3) -> List[str]:
        """提供台词和已呈现选项候选；不将候选视为当前状态或已选择。"""
        if limit <= 0:
            return []
        lines = ["屏幕对话（OCR，可能不完整）：{0}".format(item["text"])
                 for item in list(self.recent)[-limit:]]
        if self.locked and self._last_choices and self.conversation:
            choice = self.conversation["choices"][-1]
            source = choice.get("source") or {}
            frame_id = _text(source.get("frame_id"), 80) or "未知"
            lines.append("最近观察到的选项（OCR 候选，可能已变化，尚未确认选择；帧 {0}）：{1}".format(
                frame_id, " / ".join(_text(text, 150) for text in self._last_choices[:6])))
        return lines
