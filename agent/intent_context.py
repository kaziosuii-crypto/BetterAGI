#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手嘴共用的意图上下文（设计 03 §C.6 / D.2 的 P0 后半）。

问题（已核实）：executor.FastExecutor 的手（motor）和嘴（narrator）只共享
stop / done 两个 threading.Event。嘴每 narrate_every_s 抓一帧就问，手每步
200ms 按一次，于是「嘴看到的是当前帧而不是第 k 步的帧」，收手判断必然滞后
一个动作；嘴也完全不知道手刚按了什么、有没有按键被吞、这一步是不是解析失败。

本模块只做一件事：一个线程安全的小信箱。

- 手每步写一条动作：第几步、按了哪些键、决策耗时、发生时刻、备注；
- 嘴每说一句写一条解说：正文 + 时刻；
- 嘴在每次 ask 之前读 last_action()，把「上一动作」拼进 instructions 末尾
  （describe_action 负责压成一句中文）。

约定：
- 不做 IO、不做重试、不阻塞等待：内部只有一把 threading.Lock，临界区是纯内存操作；
- 空输入不抛：record_action() / record_commentary(None) 都安全（空解说直接不记）；
- 对外返回的 dict / list 都是拷贝，调用方改了不会污染内部状态；
- 不做跨意图清空：一个 IntentContext 属于一个意图，由调用方每意图新建一份
  （run() 不替你清，避免把「上一意图按了什么」串给下一意图）。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

#: 最近动作 / 解说最多保留的条数：这一段操作最长 max_decisions=60 步，
#: 256 足够，同时防止异常调用方把内存撑爆。
MAX_KEPT = 256

#: 备注字段上限：note 是给人看的短句，不是日志正文。
MAX_NOTE_CHARS = 200


def _as_int(value: Any, default: int = 0) -> int:
    """尽力转 int；坏输入退回 default（记账绝不因为一个脏字段抛异常）。"""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_keys(keys: Any) -> List[str]:
    """把一次决策的按键压成 str 列表；None / 空串 → []，单个字符串 → 一个键。"""
    if keys is None:
        return []
    if isinstance(keys, str):
        return [keys] if keys else []
    try:
        return [str(key) for key in keys]
    except TypeError:
        # 既不是字符串也不是可迭代对象（例如误传 int）：当作一个键名记下来，不抛。
        return [str(keys)]


def _copy_action(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """动作条目的深一层拷贝（keys 是唯一的可变字段）。"""
    if entry is None:
        return None
    copied = dict(entry)
    copied["keys"] = list(entry.get("keys") or [])
    return copied


def describe_action(action: Optional[Dict[str, Any]]) -> str:
    """把一条动作压成一句中文，供嘴的 instructions 末尾引用。

    例：「上一动作：第 3 步按键 W,A，耗时 41ms」；空动作写成「没有按键」，
    有 note 时追加在末尾（例如「解析失败，动作没有执行」）。没有动作返回 ""。
    """
    if not action:
        return ""
    step = action.get("step_index")
    if step is None:
        head = "上一动作："
    else:
        head = "上一动作：第 %s 步" % step
    keys = action.get("keys") or []
    if keys:
        text = head + "按键 " + ",".join(str(key) for key in keys)
    else:
        text = head + "没有按键"
    decide_ms = action.get("decide_ms")
    if decide_ms is not None:
        text += "，耗时 %sms" % decide_ms
    note = str(action.get("note") or "").strip()
    if note:
        text += "，" + note
    return text


class IntentContext:
    """一个意图内手工嘴共享的内存信箱（线程安全，无 IO，无阻塞等待）。

    max_kept：最多保留多少条历史（默认 MAX_KEPT）；只影响保留量，
    snapshot()["action_count"] / ["commentary_count"] 记的是累计条数。
    """

    def __init__(self, max_kept: int = MAX_KEPT) -> None:
        self._lock = threading.Lock()
        self._actions: List[Dict[str, Any]] = []
        self._commentary: List[Dict[str, Any]] = []
        self._max_kept = max(1, _as_int(max_kept, MAX_KEPT))
        self._action_total = 0
        self._commentary_total = 0

    # ---------------------------------------------------------------- 手写
    def record_action(self, step_index: Any = 0, keys: Any = None,
                      decide_ms: Any = 0, t_ms: Any = 0,
                      note: str = "") -> Dict[str, Any]:
        """记下手的第 step_index 步；返回写入的条目拷贝（seq 是累计序号，1 起）。"""
        key_list = _as_keys(keys)
        index = _as_int(step_index, 0)
        spent = _as_int(decide_ms, 0)
        at = _as_int(t_ms, 0)
        remark = str(note or "")[:MAX_NOTE_CHARS]
        with self._lock:
            self._action_total += 1
            entry: Dict[str, Any] = {
                "seq": self._action_total,
                "step_index": index,
                "keys": key_list,
                "decide_ms": spent,
                "t_ms": at,
                "note": remark,
            }
            self._actions.append(entry)
            if len(self._actions) > self._max_kept:
                del self._actions[: len(self._actions) - self._max_kept]
        return _copy_action(entry) or {}

    def last_action(self) -> Optional[Dict[str, Any]]:
        """最近一步动作的拷贝；一步都还没走时返回 None。"""
        with self._lock:
            return _copy_action(self._actions[-1]) if self._actions else None

    # ---------------------------------------------------------------- 嘴写
    def record_commentary(self, text: Any = "", t_ms: Any = 0) -> Optional[Dict[str, Any]]:
        """记下嘴刚说的一句；空文本返回 None（不记空气，也不抛）。"""
        body = str(text or "").strip()
        if not body:
            return None
        at = _as_int(t_ms, 0)
        with self._lock:
            self._commentary_total += 1
            entry: Dict[str, Any] = {
                "seq": self._commentary_total,
                "text": body,
                "t_ms": at,
            }
            self._commentary.append(entry)
            if len(self._commentary) > self._max_kept:
                del self._commentary[: len(self._commentary) - self._max_kept]
        return dict(entry)

    def commentary(self, limit: int = 8) -> List[Dict[str, Any]]:
        """最近的 limit 句解说（按时间顺序，老的在前）；limit <= 0 返回 []。"""
        want = _as_int(limit, 8)
        if want <= 0:
            return []
        with self._lock:
            return [dict(item) for item in self._commentary[-want:]]

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> Dict[str, Any]:
        """给编排层 / 测试看的快照：累计条数 + 最近动作 + 最近解说（全是拷贝）。"""
        with self._lock:
            return {
                "action_count": self._action_total,
                "kept_actions": len(self._actions),
                "last_action": _copy_action(self._actions[-1]) if self._actions else None,
                "recent_actions": [_copy_action(item) or {} for item in self._actions[-8:]],
                "commentary_count": self._commentary_total,
                "last_commentary": dict(self._commentary[-1]) if self._commentary else None,
                "recent_commentary": [dict(item) for item in self._commentary[-8:]],
            }
