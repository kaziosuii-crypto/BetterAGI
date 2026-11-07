#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线验收器：把 04_acceptance.md 的 A1-A9 不变量断言落到一次真实运行目录上。

用法::

    python -m verify <run_dir> [--session-id X]
    python agent/verify.py <run_dir> [--session-id X]

设计约束（04_acceptance.md §七B）：

* **离线**：只读磁盘上已经写好的时间轴 / 索引 / 交棒 / 分片 / 便利贴，不联网、
  不启动模型、不依赖 agent 包里的运行期对象；
* **不静默通过**：时间轴、索引、交棒、segments.csv 或它引用的分片文件缺了，
  对应断言必须 ok=False 并在 detail 里点名缺哪个路径；
* **可复核**：每条断言带 evidence（行号 / seq / t_ms / 文件路径），方便按
  04 §七B 的要求直接定位反例。

为了能独立运行，本文件不 import 仓库里的其它模块，长度阈值与字段名直接对齐
timeline.py / recorder.py / handoff.py / incident.py / sticky.py 的落盘格式。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ---- 与设计 / 现有模块对齐的常量 -----------------------------------------
TIMELINE_NAME = "timeline.jsonl"
INDEX_NAME = "session_index.json"
SEGMENTS_CSV = "segments.csv"

CONCLUSION_MIN = 8
CONCLUSION_MAX = 60
OBSERVATIONS_MIN = 120
OBSERVATIONS_MAX = 2000

STICKY_BLOCK_LIMIT = 1024
STICKY_TOTAL_LIMIT = 2048

#: 时间轴里代表 realtime 会话生命周期的 kind。当前主线还没写这类事件，
#: 所以 A1 在大多数真实运行目录上会明确 skipped；以后接了会话打点就能直接判。
SESSION_START_KINDS = frozenset({
    "session_start", "session_open", "session_connect",
    "omni_session_start", "omni_connect", "omni_start",
    "realtime_session_start", "realtime_connect", "realtime_start",
})
SESSION_END_KINDS = frozenset({
    "session_end", "session_close", "session_disconnect",
    "omni_session_end", "omni_disconnect", "omni_stop",
    "realtime_session_end", "realtime_disconnect", "realtime_stop",
})
SESSION_GENERIC_KINDS = frozenset({
    "session", "omni", "realtime", "omni_session", "realtime_session",
    "session_state", "omni_state",
})
_SESSION_START_WORDS = frozenset({"start", "started", "open", "opened", "connect",
                                  "connected", "active", "on"})
_SESSION_END_WORDS = frozenset({"end", "ended", "close", "closed", "disconnect",
                                "disconnected", "inactive", "off", "stop", "stopped"})

ACTION_KINDS = ("game_action",)
CHECK_NAMES = (
    "A1 活跃 realtime 会话不超过 1 个",
    "A2 每个动作都落在某条意图的生死区间内",
    "A3 时间轴单调（t_ms 不倒退、seq 严格递增）",
    "A4 每条意图都有合规的交棒报告",
    "A5 分片与时间轴同源且分片文件齐全",
    "A6 便利贴单块与合计不超限",
    "A7 事故时间轴事件都有带 README 的留证目录",
    "A8 事故之后不再出现动作或新意图",
    "A9 便利贴正文没有混进快系统的 system/messages",
)

_SAFE_ID_RE = re.compile(r"[^0-9A-Za-z_\-]+")
_STICKY_HEADING_RE = re.compile(r"^\s*##\s+(?P<name>.+?)\s*$")


# ---- 基础结果结构 ---------------------------------------------------------
def _check(name: str, ok: bool, detail: str,
           evidence: Optional[Sequence[str]] = None,
           skipped: bool = False) -> Dict[str, Any]:
    """一条断言的结果。skipped=True 时 ok 仍为 True，单独计数。"""

    return {
        "name": name,
        "ok": bool(ok),
        "skipped": bool(skipped),
        "detail": str(detail),
        "evidence": [str(item) for item in (evidence or []) if str(item).strip()],
    }


def _read_text(path: Path) -> str:
    # 读取失败要冒泡到调用方，由具体断言决定怎么报；不要用 errors='ignore'
    # 吞掉坏字节，否则验收器自己就变成了静默通过。
    return path.read_text(encoding="utf-8")


def _read_timeline(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """逐行读 timeline.jsonl。返回 (events, errors)；每行保留 _line/_index。"""

    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        text = _read_text(path)
    except OSError as error:
        return [], ["读不了 %s：%s" % (path, error)]
    for lineno, line in enumerate(text.splitlines(), 1):
        raw = line.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError) as error:
            errors.append("%s:%d JSON 解析失败：%s" % (path.name, lineno, error))
            continue
        if not isinstance(obj, dict):
            errors.append("%s:%d 不是 JSON 对象，而是 %s"
                          % (path.name, lineno, type(obj).__name__))
            continue
        event = dict(obj)
        event["_line"] = lineno
        event["_index"] = len(events)
        events.append(event)
    return events, errors


def _read_index(path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        text = _read_text(path)
    except OSError as error:
        return None, "读不了 %s：%s" % (path, error)
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as error:
        return None, "%s JSON 解析失败：%s" % (path, error)
    if not isinstance(obj, dict):
        return None, "%s 顶层不是 JSON 对象" % path
    return obj, ""


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _seq_of(event: Dict[str, Any]) -> str:
    """证据前缀：定位到 timeline 的行号与 seq。"""

    return "第 %s 行/seq=%s" % (event.get("_line", "?"), event.get("seq", "?"))


def _kind_of(event: Dict[str, Any]) -> str:
    return str(event.get("kind") or "").strip().lower()


# ---- 目录定位 -------------------------------------------------------------
def _locate_run(root: Path, session_id: Optional[str]) -> Tuple[
        Optional[Path], List[Path], Optional[Path], List[str]]:
    """定位运行目录。

    支持三种真实落盘形态：
    * <root>/timeline.jsonl + <root>/session_index.json；
    * <root>/<session_id>/timeline.jsonl（调用方给的是父目录）；
    * <root>/run/<session_id>/timeline.jsonl。

    返回 (run_dir, timeline_candidates, index_path, warnings)。run_dir 可能为
    None（连 timeline 都找不到），由断言逐条报缺文件。
    """

    warnings: List[str] = []
    index_candidates: List[Path] = []
    if root.is_file():
        return None, [root], None, ["运行目录是一个文件而不是目录：%s" % root]

    direct = root / INDEX_NAME
    if direct.is_file():
        index_candidates.append(direct)
    for base in (root, root / "run"):
        if session_id:
            candidate = base / str(session_id) / INDEX_NAME
            if candidate.is_file() and candidate not in index_candidates:
                index_candidates.insert(0, candidate)
    if not index_candidates:
        try:
            for candidate in root.rglob(INDEX_NAME):
                if candidate.is_file() and len(candidate.relative_to(root).parts) <= 4:
                    if candidate not in index_candidates:
                        index_candidates.append(candidate)
        except OSError as error:
            warnings.append("扫描 %s 下的 %s 失败：%s" % (root, INDEX_NAME, error))

    # 按 session_id 过滤，匹配不上再用目录里唯一的索引。
    chosen_index: Optional[Path] = None
    if index_candidates:
        if session_id:
            for candidate in index_candidates:
                data, _ = _read_index(candidate)
                if data and str(data.get("session_id") or "") == str(session_id):
                    chosen_index = candidate
                    break
        if chosen_index is None:
            for candidate in index_candidates:
                if (candidate.parent / TIMELINE_NAME).is_file():
                    chosen_index = candidate
                    break
        if chosen_index is None:
            chosen_index = index_candidates[0]
        if session_id and chosen_index is not None:
            data, _ = _read_index(chosen_index)
            actual = str((data or {}).get("session_id") or "")
            if actual and actual != str(session_id):
                warnings.append(
                    "目录里的 session_id=%r 与请求的 %r 不一致，仍按实际目录验收"
                    % (actual, session_id))
    else:
        warnings.append("没有找到 %s（尝试过 %s 及 depth<=4 的子目录）"
                        % (INDEX_NAME, root))

    if chosen_index is not None:
        run_dir: Optional[Path] = chosen_index.parent
        if not (run_dir / TIMELINE_NAME).is_file():
            warnings.append("索引所在目录 %s 里没有 %s" % (run_dir, TIMELINE_NAME))
    elif (root / TIMELINE_NAME).is_file():
        run_dir = root
    else:
        run_dir = None

    timeline_candidates: List[Path] = []
    if run_dir is not None:
        timeline_candidates.append(run_dir / TIMELINE_NAME)
    if root / TIMELINE_NAME not in timeline_candidates:
        timeline_candidates.append(root / TIMELINE_NAME)
    if run_dir is None:
        # 没有索引时把所有可能的时间轴都算进搜索范围。
        for candidate in root.rglob(TIMELINE_NAME):
            if candidate.is_file() and candidate not in timeline_candidates:
                timeline_candidates.append(candidate)
        if len(timeline_candidates) > 1:
            warnings.append("找到多个 %s：%s" % (TIMELINE_NAME, timeline_candidates))

    timeline_path = next((path for path in timeline_candidates if path.is_file()), None)
    return run_dir, timeline_candidates, chosen_index, warnings


def _safe_id(text: Any) -> str:
    cleaned = _SAFE_ID_RE.sub("_", str(text or "").strip()).strip("_")
    return cleaned or "intent"


def _find_handoff(intent_id: str, bases: Sequence[Path], session_id: str = "") -> Optional[Path]:
    """按 handoff.write_file 的真实落点找 <intent_id>.json。

    write_file 会写成 <root>/<session_id>/handoffs/<id>.json（session_id 非空）
    或 <root>/handoffs/<id>.json；历史运行目录两者都出现过，所以都查。
    """

    names = ["%s.json" % intent_id, "%s.json" % _safe_id(intent_id)]
    folders: List[Path] = []
    for base in bases:
        folders.append(base / "handoffs")
        if session_id:
            folders.append(base / str(session_id) / "handoffs")
    # 去重且只保留存在目录。
    seen: set = set()
    for folder in folders:
        if not folder.is_dir():
            continue
        key = str(folder.resolve())
        if key in seen:
            continue
        seen.add(key)
        for name in names:
            path = folder / name
            if path.is_file():
                return path

    # 兜底：运行目录里任何 handoffs/ 目录下的同名文件（最多 5 层）。
    for base in bases:
        if not base.is_dir():
            continue
        try:
            for path in base.rglob("*.json"):
                if path.parent.name != "handoffs":
                    continue
                if len(path.relative_to(base).parts) > 5:
                    continue
                if path.stem in (intent_id, _safe_id(intent_id)):
                    return path
        except OSError:
            continue
    return None


# ---- A1 -------------------------------------------------------------------
def _session_transitions(events: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[int, int, str, Dict[str, Any]]], List[str]]:
    """把 session/omni 事件变成 (t_ms, delta, sid, event) 的切换点。"""

    transitions: List[Tuple[int, int, str, Dict[str, Any]]] = []
    notes: List[str] = []
    for event in events:
        kind = _kind_of(event)
        direction = ""
        if kind in SESSION_START_KINDS:
            direction = "start"
        elif kind in SESSION_END_KINDS:
            direction = "end"
        elif kind in SESSION_GENERIC_KINDS:
            raw = (event.get("action") or event.get("state")
                   or event.get("event") or event.get("phase") or "")
            word = str(raw).strip().lower()
            if word in _SESSION_START_WORDS:
                direction = "start"
            elif word in _SESSION_END_WORDS:
                direction = "end"
            else:
                notes.append("%s 的 %s 事件缺 action/state，无法判断开始还是结束"
                             % (_seq_of(event), kind))
                continue
        else:
            continue
        t_ms = _as_int(event.get("t_ms"))
        if t_ms is None:
            notes.append("%s 的会话事件 t_ms=%r 不是整数"
                         % (_seq_of(event), event.get("t_ms")))
            continue
        sid = str(event.get("session_id") or event.get("sid")
                  or event.get("name") or "default")
        transitions.append((t_ms, 1 if direction == "start" else -1, sid, event))
    return transitions, notes


def _check_a1(events: Sequence[Dict[str, Any]], timeline_missing: str) -> Dict[str, Any]:
    if timeline_missing:
        return _check(CHECK_NAMES[0], False, timeline_missing)
    transitions, notes = _session_transitions(events)
    if not transitions:
        return _check(
            CHECK_NAMES[0], True,
            "skipped：时间轴里没有 session/omni 会话事件（当前主线尚未落这类打点），"
            "无法判断并发，不能假装通过",
            evidence=notes, skipped=True,
        )

    # 同一毫秒先结束再开始：一个会话关了再开另一个，不算两个同时活跃。
    ordered = sorted(transitions, key=lambda item: (item[0], 0 if item[1] < 0 else 1,
                                                   item[3].get("_index", 0)))
    active: List[Tuple[str, Dict[str, Any]]] = []
    violations: List[str] = []
    peak = 0
    for t_ms, delta, sid, event in ordered:
        if delta > 0:
            active.append((sid, event))
        else:
            for index in range(len(active) - 1, -1, -1):
                if active[index][0] == sid:
                    active.pop(index)
                    break
            else:
                violations.append("%s 的会话结束事件 %s 没有对应的开始事件"
                                  % (_seq_of(event), sid))
                continue
        peak = max(peak, len(active))
        if len(active) > 1:
            names = "、".join("%s(%s)" % (item[0], _seq_of(item[1])) for item in active)
            violations.append("t_ms=%d 时同时活跃 %d 个 realtime 会话：%s"
                              % (t_ms, len(active), names))
    if violations:
        return _check(CHECK_NAMES[0], False,
                      "检测到 %d 处会话并发（峰值 %d，要求 ≤1）"
                      % (len(violations), peak), violations + notes)
    return _check(CHECK_NAMES[0], True,
                  "会话打点共 %d 个切换点，峰值活跃 %d（要求 ≤1）"
                  % (len(transitions), peak), notes)


# ---- A2 -------------------------------------------------------------------
def _intent_intervals(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """由 intent_start/intent_end 还原每个意图的 [start, end] 区间。"""

    last_t = max((_as_int(event.get("t_ms")) or 0 for event in events), default=0)
    starts: List[Dict[str, Any]] = []
    ends: List[Dict[str, Any]] = []
    for event in events:
        kind = _kind_of(event)
        if kind == "intent_start":
            starts.append(event)
        elif kind == "intent_end":
            ends.append(event)
    intervals: List[Dict[str, Any]] = []
    for start in starts:
        intent_id = str(start.get("intent_id") or "")
        start_t = _as_int(start.get("t_ms"))
        if start_t is None:
            continue
        end_t: Optional[int] = None
        end_event: Optional[Dict[str, Any]] = None
        for end in ends:
            if end.get("_index", -1) <= start.get("_index", -1):
                continue
            if str(end.get("intent_id") or "") != intent_id:
                continue
            candidate = _as_int(end.get("t_ms"))
            if candidate is None:
                continue
            end_t = candidate
            end_event = end
            break
        if end_t is None:
            # 未闭合的意图：先按它之后的下一条 intent_start 之前结束，
            # 没有下一个开始就撑到时间轴末尾。
            later = [event for event in starts
                     if event.get("_index", -1) > start.get("_index", -1)]
            end_t = (_as_int(later[0].get("t_ms")) - 1) if later else last_t
            end_event = later[0] if later else None
        intervals.append({
            "intent_id": intent_id,
            "start_t": start_t,
            "end_t": int(end_t),
            "start_event": start,
            "end_event": end_event,
        })
    return intervals


def _check_a2(events: Sequence[Dict[str, Any]], timeline_missing: str) -> Dict[str, Any]:
    if timeline_missing:
        return _check(CHECK_NAMES[1], False, timeline_missing)
    intervals = _intent_intervals(events)
    actions = [event for event in events if _kind_of(event) in ACTION_KINDS]
    if not actions:
        return _check(CHECK_NAMES[1], True, "skipped：没有 game_action 事件，无从检查",
                      skipped=True)
    if not intervals:
        return _check(CHECK_NAMES[1], False,
                      "%d 条 game_action 没有任何 intent_start/intent_end 区间可归属"
                      % len(actions),
                      [_seq_of(event) for event in actions])
    violations: List[str] = []
    ambiguous = 0
    missing_id = 0
    for event in actions:
        t_ms = _as_int(event.get("t_ms"))
        if t_ms is None:
            violations.append("%s 的 t_ms=%r 不是整数"
                              % (_seq_of(event), event.get("t_ms")))
            continue
        intent_id = str(event.get("intent_id") or "")
        candidates = intervals
        if intent_id:
            candidates = [item for item in intervals
                          if item["intent_id"] == intent_id]
            if not candidates:
                violations.append("%s 的 intent_id=%r 找不到对应的 intent_start"
                                  % (_seq_of(event), intent_id))
                continue
        else:
            missing_id += 1
        hit = [item for item in candidates
               if item["start_t"] <= t_ms <= item["end_t"]]
        if not hit:
            nearest = min(candidates,
                          key=lambda item: min(abs(t_ms - item["start_t"]),
                                               abs(t_ms - item["end_t"])))
            violations.append(
                "%s t_ms=%d 不在意图 %r 的 [%d, %d] 内"
                % (_seq_of(event), t_ms, nearest["intent_id"],
                   nearest["start_t"], nearest["end_t"]))
            continue
        if not intent_id and len(hit) > 1:
            ambiguous += 1
            violations.append("%s 缺 intent_id，且同时落进 %d 个意图区间：%s"
                              % (_seq_of(event), len(hit),
                                 "、".join(item["intent_id"] for item in hit)))
    if violations:
        return _check(CHECK_NAMES[1], False,
                      "%d 条动作里有 %d 条无法归属（%d 条缺 intent_id）"
                      % (len(actions), len(violations), missing_id),
                      violations)
    return _check(CHECK_NAMES[1], True,
                  "%d 条 game_action 全部落在意图区间内（共 %d 个意图区间）"
                  % (len(actions), len(intervals)),
                  ["%s 缺 intent_id，按区间唯一归属放行" % _seq_of(event)
                   for event in actions if not event.get("intent_id")][:3])


# ---- A3 -------------------------------------------------------------------
def _check_a3(events: Sequence[Dict[str, Any]], errors: Sequence[str],
              timeline_missing: str) -> Dict[str, Any]:
    if timeline_missing:
        return _check(CHECK_NAMES[2], False, timeline_missing)
    if not events:
        return _check(CHECK_NAMES[2], False,
                      "时间轴没有可解析的事件行", errors)
    violations: List[str] = list(errors)
    previous_t: Optional[int] = None
    previous_seq: Optional[int] = None
    previous_event: Optional[Dict[str, Any]] = None
    for event in events:
        t_ms = _as_int(event.get("t_ms"))
        seq = _as_int(event.get("seq"))
        if t_ms is None:
            violations.append("%s 的 t_ms=%r 不是整数"
                              % (_seq_of(event), event.get("t_ms")))
        else:
            if previous_t is not None and t_ms < previous_t:
                violations.append(
                    "%s t_ms=%d 比上一条 %s 的 %d 倒退 %d ms"
                    % (_seq_of(event), t_ms,
                       ("第 %s 行" % previous_event.get("_line"))
                       if previous_event else "上一条", previous_t,
                       previous_t - t_ms))
            previous_t = t_ms if previous_t is None else max(previous_t, t_ms)
        if seq is None:
            violations.append("%s 的 seq=%r 不是整数"
                              % (_seq_of(event), event.get("seq")))
        else:
            if previous_seq is not None and seq <= previous_seq:
                violations.append("%s seq=%d 没有严格大于上一条 seq=%d"
                                  % (_seq_of(event), seq, previous_seq))
            previous_seq = seq if previous_seq is None else max(previous_seq, seq)
        previous_event = event
    if violations:
        return _check(CHECK_NAMES[2], False,
                      "时间轴 %d 行中发现 %d 处单调性违规"
                      % (len(events), len(violations)), violations[:50])
    return _check(CHECK_NAMES[2], True,
                  "时间轴 %d 行：t_ms 不倒退，seq 严格递增" % len(events),
                  [errors[0]] if errors else [])


# ---- A4 -------------------------------------------------------------------
def _normalize_conclusion(text: Any) -> str:
    return " ".join(str(text or "").split())


def _check_a4(index: Optional[Dict[str, Any]], index_path: Optional[Path],
              bases: Sequence[Path], session_id: str) -> Tuple[Dict[str, Any], int]:
    if index is None:
        return _check(CHECK_NAMES[3], False,
                      "缺 %s：没有它就无法确认哪些意图应该交棒"
                      % (index_path or INDEX_NAME)), 0
    intents = index.get("intents")
    if not isinstance(intents, list) or not intents:
        return _check(CHECK_NAMES[3], False,
                      "%s 里没有 intents 记录（缺数据，不能空过）"
                      % (index_path or INDEX_NAME)), 0
    violations: List[str] = []
    evidence: List[str] = []
    found_count = 0
    for position, intent in enumerate(intents, 1):
        if not isinstance(intent, dict):
            violations.append("intents[%d] 不是对象：%r" % (position, intent))
            continue
        intent_id = str(intent.get("intent_id") or "").strip()
        if not intent_id:
            violations.append("intents[%d] 缺 intent_id" % position)
            continue
        handoff_path = _find_handoff(intent_id, bases, session_id=session_id)
        if handoff_path is None:
            searched = [str(base / "handoffs") for base in bases]
            if session_id:
                searched.extend(str(base / str(session_id) / "handoffs")
                                for base in bases)
            violations.append("意图 %r 缺交棒报告（找过 %s）"
                              % (intent_id, "、".join(searched)))
            continue
        found_count += 1
        try:
            raw = _read_text(handoff_path)
            report = json.loads(raw)
        except (OSError, ValueError, TypeError) as error:
            violations.append("意图 %r 的交棒 %s 读不了/不是 JSON：%s"
                              % (intent_id, handoff_path, error))
            continue
        if not isinstance(report, dict):
            violations.append("意图 %r 的交棒 %s 顶层不是对象"
                              % (intent_id, handoff_path))
            continue
        if str(report.get("intent_id") or "") and str(report.get("intent_id")) != intent_id:
            violations.append("意图 %r 的交棒文件里 intent_id=%r，对不上"
                              % (intent_id, report.get("intent_id")))
        conclusion = _normalize_conclusion(report.get("conclusion"))
        observations = str(report.get("observations") or "").strip()
        conclusion_len = len(conclusion)
        observations_len = len(observations)
        if not conclusion_len:
            violations.append("意图 %r 的 conclusion 为空（要求 %d–%d 字）"
                              % (intent_id, CONCLUSION_MIN, CONCLUSION_MAX))
        elif not (CONCLUSION_MIN <= conclusion_len <= CONCLUSION_MAX):
            violations.append(
                "意图 %r 的 conclusion 实测 %d 字，超出 %d–%d 字：%r"
                % (intent_id, conclusion_len, CONCLUSION_MIN,
                   CONCLUSION_MAX, conclusion))
        if not (OBSERVATIONS_MIN <= observations_len <= OBSERVATIONS_MAX):
            violations.append(
                "意图 %r 的 observations 实测 %d 字，超出 %d–%d 字"
                % (intent_id, observations_len,
                   OBSERVATIONS_MIN, OBSERVATIONS_MAX))
        evidence.append("意图 %s：conclusion %d 字 / observations %d 字 / %s"
                        % (intent_id, conclusion_len, observations_len,
                           handoff_path))
    if violations:
        return _check(CHECK_NAMES[3], False,
                      "%d 个意图中有 %d 处交棒问题（找到 %d 份报告）"
                      % (len(intents), len(violations), found_count),
                      violations + evidence), found_count
    return _check(CHECK_NAMES[3], True,
                  "%d 个意图各有一份交棒报告；conclusion %d–%d 字、"
                  "observations %d–%d 字全部合规"
                  % (len(intents), CONCLUSION_MIN, CONCLUSION_MAX,
                     OBSERVATIONS_MIN, OBSERVATIONS_MAX), evidence), found_count


# ---- A5 -------------------------------------------------------------------
def _find_segments_csv(bases: Sequence[Path]) -> Optional[Path]:
    for base in bases:
        for candidate in (base / SEGMENTS_CSV, base / "segments" / SEGMENTS_CSV):
            if candidate.is_file():
                return candidate
    return None


def _resolve_segment_file(name: str, csv_path: Optional[Path],
                          bases: Sequence[Path]) -> Optional[Path]:
    raw = Path(str(name))
    if not raw.name:
        return None
    candidates: List[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        if csv_path is not None:
            candidates.append(csv_path.parent / raw)
            candidates.append(csv_path.parent / "segments" / raw)
        for base in bases:
            candidates.extend([base / raw, base / "segments" / raw,
                               base / "segments" / raw.name])
    seen: set = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    # 兜底：在 segments/ 下按 basename 找。
    for base in bases:
        folder = base / "segments"
        if folder.is_dir():
            try:
                for candidate in folder.rglob(raw.name):
                    if candidate.is_file():
                        return candidate
            except OSError:
                continue
    return None


def _check_a5(events: Sequence[Dict[str, Any]], timeline_missing: str,
              index: Optional[Dict[str, Any]],
              bases: Sequence[Path]) -> Tuple[Dict[str, Any], int]:
    csv_path = _find_segments_csv(bases)
    if csv_path is None:
        return _check(CHECK_NAMES[4], False,
                      "缺 %s（找过 %s）"
                      % (SEGMENTS_CSV, "、".join(
                          str(path) for base in bases
                          for path in (base / SEGMENTS_CSV,
                                       base / "segments" / SEGMENTS_CSV)))), 0
    try:
        text = _read_text(csv_path)
    except OSError as error:
        return _check(CHECK_NAMES[4], False,
                      "读不了 %s：%s" % (csv_path, error)), 0

    rows: List[Dict[str, Any]] = []
    violations: List[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        parts = [part.strip() for part in stripped.split(",")]
        if len(parts) < 2:
            violations.append("%s:%d 只有 %d 列（至少要 file,start_s）"
                              % (csv_path.name, lineno, len(parts)))
            continue
        try:
            start_s = float(parts[1])
        except ValueError:
            if lineno == 1 and not re.search(r"\d", parts[1]):
                continue  # 表头
            violations.append("%s:%d start_s=%r 不是数字"
                              % (csv_path.name, lineno, parts[1]))
            continue
        end_s: Optional[float] = None
        if len(parts) >= 3:
            try:
                end_s = float(parts[2])
            except ValueError:
                violations.append("%s:%d end_s=%r 不是数字"
                                  % (csv_path.name, lineno, parts[2]))
        rows.append({"file": parts[0], "start_ms": int(round(start_s * 1000)),
                     "end_ms": int(round(end_s * 1000)) if end_s is not None else None,
                     "line": lineno})
    if not rows:
        return _check(CHECK_NAMES[4], False,
                      "%s 没有数据行，无法核对分片" % csv_path), 0

    timeline_times = []
    for event in events:
        t_ms = _as_int(event.get("t_ms"))
        if t_ms is not None:
            timeline_times.append((t_ms, event))
    if not timeline_times:
        violations.append("时间轴没有带 t_ms 的事件，%d 个分片找不到锚点" % len(rows))

    for row in rows:
        segment = _resolve_segment_file(row["file"], csv_path, bases)
        if segment is None:
            violations.append("%s:%d 引用的分片 %r 不存在（找过 segments/ 及 %s/）"
                              % (csv_path.name, row["line"], row["file"],
                                 csv_path.parent))
        else:
            try:
                if segment.stat().st_size <= 0:
                    violations.append("%s:%d 的分片 %s 是空文件"
                                      % (csv_path.name, row["line"], segment))
            except OSError as error:
                violations.append("%s:%d 的分片 %s 读不了：%s"
                                  % (csv_path.name, row["line"], segment, error))
        if timeline_times:
            nearest_time, nearest_event = min(
                timeline_times, key=lambda item: abs(item[0] - row["start_ms"]))
            delta = abs(nearest_time - row["start_ms"])
            if delta > 1000:
                violations.append(
                    "%s:%d start_ms=%d 在时间轴 ±1000ms 内没有事件"
                    "（最近 t_ms=%d，差 %d ms）"
                    % (csv_path.name, row["line"], row["start_ms"],
                       nearest_time, delta))

    # 每片首帧锚点（timeline.py 落盘叫 segment_header；设计文档也写作
    # segment_start）的 t_ms 必须单调不减。
    anchor_kinds = ("segment_header", "segment_start")
    headers = [event for event in events if _kind_of(event) in anchor_kinds]
    header_times = [(_as_int(event.get("t_ms")), event) for event in headers]
    header_times = [(time, event) for time, event in header_times if time is not None]
    for (previous, previous_event), (current, event) in zip(header_times, header_times[1:]):
        if current < previous:
            violations.append("%s 的 %s t_ms=%d 比上一条 %s 的 %d 倒退"
                              % (_seq_of(event), _kind_of(event), current,
                                 _seq_of(previous_event), previous))

    # session_index.segments 里登记的分片也必须真实存在。
    if isinstance(index, dict) and isinstance(index.get("segments"), list):
        for position, item in enumerate(index["segments"], 1):
            if not isinstance(item, dict):
                continue
            filename = str(item.get("file") or "")
            if not filename:
                continue
            if _resolve_segment_file(filename, csv_path, bases) is None:
                violations.append("session_index.segments[%d] 的 %r 不存在"
                                  % (position, filename))

    evidence = ["%s:%d %s start_ms=%d"
                % (csv_path.name, row["line"], row["file"], row["start_ms"])
                for row in rows]
    if violations:
        return _check(CHECK_NAMES[4], False,
                      "%d 个分片中发现 %d 处问题（csv：%s）"
                      % (len(rows), len(violations), csv_path),
                      violations + evidence), len(rows)
    if headers:
        detail = ("%d 个分片都有实体文件，start_ms 在时间轴 ±1s 内有事件；"
                  "segment_header/segment_start 共 %d 条且 t_ms 单调"
                  % (len(rows), len(headers)))
    else:
        detail = ("%d 个分片都有实体文件，start_ms 在时间轴 ±1s 内有事件；"
                  "但时间轴没有 segment_header/segment_start 锚点，"
                  "首帧单调性无法核对" % len(rows))
    return _check(CHECK_NAMES[4], True, detail, evidence), len(rows)


# ---- A6 / A9 共用的便利贴解析 ---------------------------------------------
def _split_sticky(raw: str) -> Dict[str, str]:
    """按 sticky.py 的 _load/_split_sections 口径取两块正文。"""

    lines = raw.splitlines()
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            body_start = index + 1
            continue
        if stripped.startswith("<!--"):
            body_start = index + 1
        else:
            body_start = index
        break
    body = "\n".join(lines[body_start:])
    sections: Dict[str, str] = {}
    name: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        if name is not None and name not in sections:
            sections[name] = "\n".join(buffer).strip()

    for line in body.splitlines():
        matched = _STICKY_HEADING_RE.match(line)
        if matched:
            flush()
            name = matched.group("name").strip()
            buffer = []
            continue
        if name is not None:
            buffer.append(line)
    flush()
    return sections


def _find_sticky_files(bases: Sequence[Path]) -> List[Path]:
    found: List[Path] = []
    seen: set = set()
    for base in bases:
        if not base.is_dir():
            continue
        candidates: List[Path] = []
        for folder in (base / "sticky", base / "run" / "sticky"):
            if folder.is_dir():
                try:
                    candidates.extend(folder.glob("*/*.md"))
                    candidates.extend(folder.glob("*.md"))
                except OSError:
                    pass
        try:
            candidates.extend(base.glob("**/sticky/*/*.md"))
        except OSError:
            pass
        for path in candidates:
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return sorted(found)


def _load_sticky(path: Path) -> Tuple[Dict[str, str], str]:
    try:
        raw = _read_text(path)
    except OSError as error:
        return {}, "读不了 %s：%s" % (path, error)
    return _split_sticky(raw), ""


def _check_a6(sticky_files: Sequence[Path]) -> Dict[str, Any]:
    if not sticky_files:
        return _check(CHECK_NAMES[5], True,
                      "skipped：运行目录下没有 sticky/<game>/<save_id>.md",
                      skipped=True)
    violations: List[str] = []
    evidence: List[str] = []
    max_block = 0
    max_total = 0
    for path in sticky_files:
        blocks, error = _load_sticky(path)
        if error:
            violations.append(error)
            continue
        key_text = str(blocks.get("key_binding") or "")
        rules_text = str(blocks.get("game_rules") or "")
        key_len = len(key_text)
        rules_len = len(rules_text)
        total = key_len + rules_len
        max_block = max(max_block, key_len, rules_len)
        max_total = max(max_total, total)
        evidence.append("%s：key_binding=%d 字，game_rules=%d 字，合计=%d 字"
                        % (path, key_len, rules_len, total))
        if "key_binding" not in blocks:
            violations.append("%s 缺 ## key_binding 块" % path)
        if "game_rules" not in blocks:
            violations.append("%s 缺 ## game_rules 块" % path)
        if key_len > STICKY_BLOCK_LIMIT:
            violations.append("%s 的 key_binding %d 字，超过单块上限 %d"
                              % (path, key_len, STICKY_BLOCK_LIMIT))
        if rules_len > STICKY_BLOCK_LIMIT:
            violations.append("%s 的 game_rules %d 字，超过单块上限 %d"
                              % (path, rules_len, STICKY_BLOCK_LIMIT))
        if total > STICKY_TOTAL_LIMIT:
            violations.append("%s 合计 %d 字，超过总上限 %d"
                              % (path, total, STICKY_TOTAL_LIMIT))
    if violations:
        return _check(CHECK_NAMES[5], False,
                      "%d 个便利贴中超限/缺块 %d 处（最大单块 %d、最大合计 %d）"
                      % (len(sticky_files), len(violations), max_block, max_total),
                      violations + evidence)
    return _check(CHECK_NAMES[5], True,
                  "%d 个便利贴均合规：最大单块 %d 字（≤%d），最大合计 %d 字（≤%d）"
                  % (len(sticky_files), max_block, STICKY_BLOCK_LIMIT,
                     max_total, STICKY_TOTAL_LIMIT), evidence)


# ---- A7 -------------------------------------------------------------------
def _incident_dirs(bases: Sequence[Path]) -> List[Path]:
    found: List[Path] = []
    seen: set = set()
    for base in bases:
        folder = base / "incidents"
        if not folder.is_dir():
            continue
        try:
            children = sorted(folder.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            key = str(child.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(child)
    return found


def _resolve_incident_dir(event: Dict[str, Any], incident_dirs: Sequence[Path],
                          bases: Sequence[Path]) -> Optional[Path]:
    raw = event.get("dir") or event.get("path") or event.get("incident_dir")
    candidates: List[Path] = []
    if raw:
        path = Path(str(raw))
        if path.is_absolute():
            candidates.append(path)
        else:
            for base in bases:
                candidates.append(base / path)
            candidates.append(path)
        for folder in incident_dirs:
            if folder.name == path.name:
                candidates.append(folder)
    else:
        intent_id = str(event.get("intent_id") or "")
        for folder in incident_dirs:
            if intent_id and (folder.name.endswith("_" + intent_id)
                              or intent_id in folder.name):
                candidates.append(folder)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _find_readme(folder: Path) -> Optional[Path]:
    direct = folder / "README.md"
    if direct.is_file():
        return direct
    try:
        for child in folder.iterdir():
            if child.is_file() and child.name.lower() == "readme.md":
                return child
    except OSError:
        pass
    return None


def _check_a7(events: Sequence[Dict[str, Any]], timeline_missing: str,
              bases: Sequence[Path]) -> Tuple[Dict[str, Any], int]:
    if timeline_missing:
        return _check(CHECK_NAMES[6], False, timeline_missing), 0
    incidents = [event for event in events if _kind_of(event) == "incident"]
    if not incidents:
        return _check(CHECK_NAMES[6], True,
                      "skipped：时间轴没有 incident 事件", skipped=True), 0
    dirs = _incident_dirs(bases)
    violations: List[str] = []
    evidence: List[str] = []
    for event in incidents:
        folder = _resolve_incident_dir(event, dirs, bases)
        if folder is None:
            violations.append(
                "%s 的 incident 找不到留证目录（dir=%r；incidents 下现有 %s）"
                % (_seq_of(event), event.get("dir"),
                   "、".join(item.name for item in dirs) or "空"))
            continue
        readme = _find_readme(folder)
        if readme is None:
            violations.append("%s 的 incident 目录 %s 缺 README.md"
                              % (_seq_of(event), folder))
            continue
        evidence.append("%s -> %s（README.md 在）" % (_seq_of(event), folder))
    if violations:
        return _check(CHECK_NAMES[6], False,
                      "%d 条 incident 事件中有 %d 条留证不完整"
                      % (len(incidents), len(violations)), violations + evidence), len(incidents)
    return _check(CHECK_NAMES[6], True,
                  "%d 条 incident 事件都能定位到目录，且目录内有 README.md"
                  % len(incidents), evidence), len(incidents)


# ---- A8 -------------------------------------------------------------------
def _check_a8(events: Sequence[Dict[str, Any]], timeline_missing: str) -> Dict[str, Any]:
    if timeline_missing:
        return _check(CHECK_NAMES[7], False, timeline_missing)
    incident_indexes = [event.get("_index", -1) for event in events
                        if _kind_of(event) == "incident"]
    if not incident_indexes:
        return _check(CHECK_NAMES[7], True,
                      "skipped：时间轴没有 incident 事件", skipped=True)
    first_incident = min(incident_indexes)
    incident_event = events[first_incident]
    violations: List[str] = []
    for event in events:
        if event.get("_index", -1) <= first_incident:
            continue
        kind = _kind_of(event)
        if kind in ("game_action", "intent_start"):
            violations.append("%s 的 %s 发生在 %s 的 incident 之后"
                              % (_seq_of(event), kind, _seq_of(incident_event)))
    if violations:
        return _check(CHECK_NAMES[7], False,
                      "incident 之后仍有 %d 条动作/新意图" % len(violations),
                      violations)
    return _check(CHECK_NAMES[7], True,
                  "incident（%s）之后没有 game_action 或 intent_start"
                  % _seq_of(incident_event))


# ---- A9 -------------------------------------------------------------------
def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "system", "messages"):
            if key in value:
                return _flatten_text(value[key])
        return "\n".join(_flatten_text(item) for key, item in value.items()
                         if key not in ("role", "type", "name"))
    return ""


def _walk_records(value: Any, depth: int = 0) -> Iterable[Dict[str, Any]]:
    if depth > 5:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_records(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_records(item, depth + 1)


def _packet_prompt_text(record: Dict[str, Any]) -> Optional[str]:
    if "system" not in record and "messages" not in record:
        return None
    parts: List[str] = []
    if "system" in record:
        parts.append(_flatten_text(record.get("system")))
    if "messages" in record:
        parts.append(_flatten_text(record.get("messages")))
    text = "\n".join(part for part in parts if part)
    return text if text.strip() else None


def _find_packet_prompts(bases: Sequence[Path]) -> Tuple[List[Tuple[Path, str]], List[str]]:
    prompts: List[Tuple[Path, str]] = []
    warnings: List[str] = []
    seen: set = set()
    for base in bases:
        if not base.is_dir():
            continue
        try:
            files = sorted(base.rglob("*"))
        except OSError as error:
            warnings.append("扫描意图包失败：%s" % error)
            continue
        for path in files:
            if not path.is_file() or path.suffix.lower() not in (".json", ".jsonl"):
                continue
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    continue
            except OSError:
                continue
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                if path.suffix.lower() == ".jsonl":
                    records = []
                    for line in _read_text(path).splitlines():
                        stripped = line.strip()
                        if stripped:
                            records.append(json.loads(stripped))
                else:
                    records = [json.loads(_read_text(path))]
            except (OSError, ValueError, TypeError):
                continue
            for record in records:
                for item in _walk_records(record):
                    text = _packet_prompt_text(item)
                    if text:
                        prompts.append((path, text))
    return prompts, warnings


def _check_a9(sticky_files: Sequence[Path], bases: Sequence[Path]) -> Tuple[Dict[str, Any], int]:
    if not sticky_files:
        return _check(CHECK_NAMES[8], True,
                      "skipped：没有便利贴，无从谈正文泄漏", skipped=True), 0
    body_lines: List[Tuple[Path, str]] = []
    for path in sticky_files:
        blocks, _ = _load_sticky(path)
        for block_name in ("key_binding", "game_rules"):
            for line in str(blocks.get(block_name) or "").splitlines():
                stripped = line.strip()
                if len(stripped) < 4 or stripped.startswith("#"):
                    continue
                body_lines.append((path, stripped))
    if not body_lines:
        return _check(CHECK_NAMES[8], True,
                      "skipped：便利贴没有可做子串匹配的正文行", skipped=True), 0

    prompts, warnings = _find_packet_prompts(bases)
    if not prompts:
        return _check(CHECK_NAMES[8], True,
                      "skipped：没找到落盘的快系统意图包（含 system/messages 的 "
                      "json/jsonl）；找到后会做正文行子串匹配",
                      evidence=warnings, skipped=True), 0

    violations: List[str] = []
    for prompt_path, prompt_text in prompts:
        for sticky_path, line in body_lines:
            if line in prompt_text:
                violations.append("便利贴 %s 的正文行 %r 出现在快系统提示 %s 里"
                                  % (sticky_path, line, prompt_path))
    if violations:
        return _check(CHECK_NAMES[8], False,
                      "%d 个意图包里发现 %d 处便利贴正文泄漏"
                      % (len(prompts), len(violations)),
                      violations + warnings), len(prompts)
    return _check(CHECK_NAMES[8], True,
                  "检查 %d 个落盘意图包，未发现 %d 条便利贴正文行"
                  % (len(prompts), len(body_lines)), warnings), len(prompts)


# ---- 总入口 ---------------------------------------------------------------
def verify_run(root: Any, session_id: Optional[str] = None) -> Dict[str, Any]:
    """扫描一次运行的整个目录，逐条判 A1-A9。

    返回 {ok, checks, counts, warnings, run_dir, session_id}。缺少必需文件时
    对应断言 ok=False，并在 detail/evidence 里点名路径；不静默通过。
    """

    root_path = Path(str(root)).expanduser()
    try:
        root_path = root_path.resolve()
    except OSError:
        pass
    warnings: List[str] = []

    run_dir, timeline_candidates, index_path, locate_warnings = _locate_run(
        root_path, session_id)
    warnings.extend(locate_warnings)

    if run_dir is not None:
        bases = [run_dir]
        if root_path != run_dir and root_path.is_dir():
            bases.append(root_path)
    else:
        # 即使目录不存在也保留 root_path，缺失报告才能点名具体路径。
        bases = [root_path]

    # 时间轴
    timeline_path: Optional[Path] = next(
        (path for path in timeline_candidates if path.is_file()), None)
    if timeline_path is None:
        timeline_missing = ("缺 %s：找过 %s"
                            % (TIMELINE_NAME,
                               "、".join(str(path) for path in timeline_candidates)
                               or str(root_path)))
        events: List[Dict[str, Any]] = []
        timeline_errors: List[str] = []
    else:
        timeline_missing = ""
        events, timeline_errors = _read_timeline(timeline_path)
        if timeline_errors:
            warnings.extend(timeline_errors[:5])

    # 索引与 session_id
    index: Optional[Dict[str, Any]] = None
    index_error = ""
    if index_path is not None:
        index, index_error = _read_index(index_path)
        if index_error:
            warnings.append(index_error)
    else:
        index_error = "缺 %s" % INDEX_NAME
    effective_session = str(session_id or (index or {}).get("session_id") or "").strip()

    if not (root_path / "meta.json").exists():
        warnings.append("缺 meta.json（不阻断验收，但运行目录不完整）")

    checks: List[Dict[str, Any]] = []
    checks.append(_check_a1(events, timeline_missing))
    checks.append(_check_a2(events, timeline_missing))
    checks.append(_check_a3(events, timeline_errors, timeline_missing))

    a4_check, handoff_count = _check_a4(index, index_path, bases, effective_session)
    checks.append(a4_check)

    a5_check, segment_count = _check_a5(events, timeline_missing, index, bases)
    checks.append(a5_check)

    sticky_files = _find_sticky_files(bases)
    checks.append(_check_a6(sticky_files))

    a7_check, incident_count = _check_a7(events, timeline_missing, bases)
    checks.append(a7_check)
    checks.append(_check_a8(events, timeline_missing))

    a9_check, packet_count = _check_a9(sticky_files, bases)
    checks.append(a9_check)

    passed = sum(1 for check in checks if check["ok"] and not check["skipped"])
    failed = sum(1 for check in checks if not check["ok"])
    skipped = sum(1 for check in checks if check["skipped"])
    intents = index.get("intents") if isinstance(index, dict) else []
    counts = {
        "total": len(checks),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "timeline_events": len(events),
        "timeline_parse_errors": len(timeline_errors),
        "intents": len(intents) if isinstance(intents, list) else 0,
        "handoffs": handoff_count,
        "segments": segment_count,
        "incidents": incident_count,
        "sticky_files": len(sticky_files),
        "packets": packet_count,
    }
    return {
        "ok": failed == 0,
        "session_id": effective_session,
        "run_dir": str(run_dir or root_path),
        "timeline": str(timeline_path) if timeline_path else "",
        "checks": checks,
        "counts": counts,
        "warnings": warnings,
    }


def format_report(result: Dict[str, Any]) -> str:
    """把 verify_run 的结果写成给人看的中文摘要。"""

    counts = result.get("counts") or {}
    total = counts.get("total", len(result.get("checks") or []))
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    skipped = counts.get("skipped", 0)
    verdict = "通过" if result.get("ok") else "不通过"
    lines: List[str] = []
    lines.append("离线验收：%s（%d 项通过 / %d 项不通过 / %d 项跳过，共 %d 项）"
                 % (verdict, passed, failed, skipped, total))
    run_dir = result.get("run_dir") or ""
    if run_dir:
        lines.append("运行目录：%s" % run_dir)
    if result.get("session_id"):
        lines.append("session_id：%s" % result["session_id"])
    lines.append("")

    for check in result.get("checks") or []:
        if check.get("skipped"):
            tag = "SKIP"
        elif check.get("ok"):
            tag = "PASS"
        else:
            tag = "FAIL"
        lines.append("[%s] %s" % (tag, check.get("name", "")))
        lines.append("    %s" % check.get("detail", ""))
        evidence = check.get("evidence") or []
        limit = 8 if tag == "FAIL" else 3
        for item in evidence[:limit]:
            lines.append("    - %s" % item)
        if len(evidence) > limit:
            lines.append("    - ……另有 %d 条证据" % (len(evidence) - limit))

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("提示（不阻断）：")
        for item in warnings[:10]:
            lines.append("  - %s" % item)
        if len(warnings) > 10:
            lines.append("  - ……另有 %d 条" % (len(warnings) - 10))
    return "\n".join(lines)


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 cp936，管道里会变成乱码；有 reconfigure 就切 UTF-8。"""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (OSError, ValueError):
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="verify",
        description="离线验收一次运行目录是否满足 A1-A9 不变量断言",
    )
    parser.add_argument("run_dir", help="运行目录（含 timeline.jsonl / session_index.json）")
    parser.add_argument("--session-id", default=None,
                        help="运行目录里有多个会话时指定 session_id")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出机器可读的 JSON（默认输出中文摘要）")
    args = parser.parse_args(argv)

    result = verify_run(args.run_dir, session_id=args.session_id)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
