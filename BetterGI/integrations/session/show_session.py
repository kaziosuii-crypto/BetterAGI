#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在终端里直读一份会话记录。

示例::

    python show_session.py sessions/20261027-153000-ab12cd
    python show_session.py sessions/<id> --thoughts --actions --limit 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from session_format import read_session


def format_time(ms: int) -> str:
    seconds, millis = divmod(max(0, ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis // 100:01d}"
    return f"{minutes:02d}:{seconds:02d}.{millis // 100:01d}"


def describe(event: Dict[str, Any]) -> Optional[str]:
    kind = event.get("type")
    if kind == "thought":
        return f"思考     {event.get('text', '')}"
    if kind == "speech":
        emotion = event.get("emotion") or "calm"
        duration = event.get("dur")
        return f"解说[{emotion}] {event.get('text', '')}  ({duration}ms)"
    if kind == "guidance":
        return f"指导     {event.get('text', '')}"
    if kind == "action":
        parts = []
        keys = event.get("keys") or []
        if keys:
            parts.append("+".join(keys))
        mouse = event.get("mouse") or {}
        if mouse.get("dx") or mouse.get("dy"):
            parts.append(f"鼠标({mouse.get('dx', 0)},{mouse.get('dy', 0)})")
        if event.get("scroll"):
            parts.append(f"滚轮{event['scroll']}")
        if event.get("cursor"):
            parts.append(f"光标({event['cursor'].get('x')},{event['cursor'].get('y')})")
        thought = event.get("thought")
        text = "动作     " + (" ".join(parts) or "无输入")
        if thought:
            text += f"  // {thought}"
        return text
    if kind == "avatar":
        op = event.get("op")
        if op == "blink":
            return "表情     眨眼"
        if op == "pose":
            return None  # 每秒一条，默认不刷屏
        expression = event.get("expression")
        return f"表情     {expression or op}"
    if kind == "note":
        return f"备注     {event.get('text', '')}"
    if kind == "error":
        return f"错误     [{event.get('code')}] {event.get('message', '')}"
    if kind == "video":
        return f"画面     开始录制（偏移 {event.get('t')}ms）"
    if kind == "audio":
        return f"音频     {event.get('path', '')}"
    if kind == "end":
        counts = event.get("counts") or {}
        return (
            f"结束     总时长 {format_time(int(event.get('duration_ms') or 0))}，"
            f"解说 {counts.get('speeches', 0)} 条，思考 {counts.get('thoughts', 0)} 条，"
            f"动作 {counts.get('actions', 0)} 个"
        )
    return None


def main(argv: Optional[list[str]] = None) -> int:
    # Windows 控制台默认 cp936，直接输出中文会话会乱码。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="在终端查看会话记录")
    parser.add_argument("session", help="会话目录或 session.jsonl")
    parser.add_argument("--thoughts", action="store_true", help="显示内部思考")
    parser.add_argument("--actions", action="store_true", help="显示模拟输入")
    parser.add_argument("--poses", action="store_true", help="显示每秒姿态")
    parser.add_argument("--limit", type=int, default=0, help="最多显示多少条；0 = 全部")
    options = parser.parse_args(argv)

    session = read_session(Path(options.session))
    meta = session.meta
    print(f"会话 {session.session_id}  开始于 {meta.get('created_at')}")
    print(f"目录 {session.directory}")
    print(f"时长 {format_time(session.duration_ms)}  事件 {len(session.events)}")
    print("-" * 72)

    shown = 0
    for event in session.events:
        kind = event.get("type")
        if kind == "thought" and not options.thoughts:
            continue
        if kind == "action" and not options.actions:
            continue
        if kind == "avatar" and event.get("op") == "pose" and not options.poses:
            continue
        text = describe(event)
        if not text:
            continue
        print(f"{format_time(int(event.get('t', 0)))}  {text}")
        shown += 1
        if options.limit and shown >= options.limit:
            print("……（已截断，用 --limit 0 查看全部）")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
