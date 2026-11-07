#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BetterAGI 极简 TUI：看得见思考、看得见工具调用，还能随时插话。

设计要点（按用户要求）：

- 动态界面：底部一行状态栏持续刷新（时钟、已玩多久、第几轮、当前动作、
  工具次数、上下文条数、插话队列），窗口标题也带着时间；流式输出照常打印；
- 每一行都带系统时间，模型自己也拿到"现在几点、这次玩多久了"；
- 上下文保留最近 30 条**文字**消息（图片不占名额，只留最近 3 张，
  更早的图替换成一行说明）；
- 跑着的时候可以直接打字插话，插话在下一次模型调用前进入上下文；
- 暴露一个本机 HTTP API（默认 127.0.0.1:8791），方便外部调试：
  ``GET /state``、``GET /events``、``POST /say``、``POST /quit``，
  全部要求 ``Authorization: Bearer <token>``，token 与地址写到
  ``_diag/tui-endpoint.json``。

用法::

    python tui.py --goal "在蒙德城里随便走走"
    python tui.py --goal-file goal.txt --port 8791

与 run_stream.py 的关系（2026-11-04 定）：

    本文件是**旧交互入口（legacy）**，但慢系统规划已经接到
    ``slow_planner.SlowPlanner``：每轮先要一份三段式任务（慢系统 2s 预算；超时/报错
    沿用上一轮并在状态栏/提示里说明），再把 ``plan.goal`` 作为这一轮任务交给
    ``lithos.stream_complete`` 执行（流式说话 + 工具调用）。``stream_complete``
    只负责执行，不再负责规划。
    真正按设计开播请用 ``run_stream.py``（装配 -> 预检 -> 回合 -> 下播）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import lithos
import trace_log
from agent import SYSTEM_PROMPT, TOOL_SPECS, BetterGITools, Session, ToolResult, resolve_system_prompt
from memory_wire import episode_from_tool, format_recall, recall_for, writeback
from profiles import open_profile
from slow_planner import (
    LithosSlowClient,
    SlowPlan,
    SlowPlanner,
    SlowPlannerError,
    SlowPlannerTimeout,
    build_slow_planner,
)

KEEP_TEXT_MESSAGES = 30
KEEP_IMAGES = 3
#: 模型连续失败到这个次数就停止本次会话，避免网络/配额问题把额度打成水漂。
MAX_CONSECUTIVE_LLM_FAILURES = 12
#: 思考最多显示多少字。太长会把窗口刷成草稿本（用户 2026-11-06 投诉）。
REASONING_DISPLAY_LIMIT = 420
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: 慢系统（规划腿）的硬预算；卡关开 thinking 时 SlowPlanner 用它的 30s 预算（用户 2026-11-04 拍板）。
SLOW_BUDGET_MS: Optional[int] = None  # None = 不设截止（用户 2026-11-04）
#: 每轮送给慢系统的最近弹幕条数。
RECENT_DANMAKU_LIMIT = 10
#: 慢系统彻底不可用/没给出任务时的兜底任务，保证这一轮不开天窗。
SLOW_TASK_FALLBACK = "先看画面，确认现在在哪、能做什么；从最稳的一步开始，做完再看结果。"

DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
#: 快系统专用：洋红。慢系统是青/绿，一眼就能分出哪条腿在动。
MAGENTA = "\033[35m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _as_datetime(when: Optional[datetime | float]) -> datetime:
    """时间戳既可能是 datetime，也可能是 time.time() 的浮点秒。"""
    if when is None:
        return datetime.now()
    if isinstance(when, datetime):
        return when
    return datetime.fromtimestamp(float(when))


def is_reasoning_400(error: BaseException) -> bool:
    """是不是"thinking 模式没回传 reasoning_content"的 400（不可靠重试自愈的那类）。"""
    status = int(getattr(error, "status", 0) or 0)
    text = f"{getattr(error, 'body', '')} {error}"
    return status == 400 and "reasoning_content" in text


def stamp(when: Optional[datetime | float] = None) -> str:
    return _as_datetime(when).strftime("%H:%M:%S")


def full_stamp(when: Optional[datetime | float] = None) -> str:
    return _as_datetime(when).strftime("%Y-%m-%d %H:%M:%S")


def elapsed_text(started: float) -> str:
    seconds = int(time.time() - started)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def probe_legs(tools: Any) -> Dict[str, Any]:
    """问一句 lumine 那头通不通；探测失败按"不可用"处理，不能把直播掐了。"""
    try:
        return tools.lumine_availability()
    except Exception as error:  # noqa: BLE001 - 探测只是提示，炸了也要能播
        return {"available": False, "reason": f"探测失败：{error}", "detail": {}}


def legs_note(legs: Dict[str, Any]) -> str:
    """把 lumine 的状态写成一句给模型看的话（只在原神通道用）。"""
    if not legs.get("available"):
        return (
            f"lumine 现在不可用：{legs.get('reason')}。"
            "这一轮不要调用 lumine；只用 scan/look 看，并把情况告诉玩家。"
        )
    kind = (legs.get("motor") or {}).get("kind")
    live_active = (legs.get("detail") or {}).get("liveActive") is True
    if kind == "local-stub":
        return (
            "lumine 这条腿通着，但接的是本机替身 Motor（只会按住 W），不是真 Lumine 策略模型；"
            "可以用它验证动作通路，但别把它的动作当成策略判断。"
        )
    if not live_active:
        return (
            "lumine 这条腿是通的（Motor 在线），但 Live 会话还没开；"
            "调用 lumine 时它会自己 stop→激活→live/start，前提是游戏在前台且识别到主界面。"
        )
    return "lumine 这条腿通着（真 Lumine 模型），可以用 lumine 让角色动。"


#: 这些结束原因说明「还没做完」，不是错误：步数/时间用完、连续空动作。
UNFINISHED_ENDINGS = ("max_decisions", "budget", "no_action")


def _tool_content(result: Any) -> str:
    """给慢系统看工具结果：没做完要说「未完成」，不能一律打成失败。

    用户 2026-11-05 投诉「明明还需要操作一会，就直接说任务失败」——手跑到步数上限
    交回来是正常交接，不是故障；只有真正跑挂（截帧失败/手模型报错）才算失败。
    """
    text = result.text
    if result.ok:
        return text
    try:
        payload = json.loads(text)
    except Exception:  # noqa: BLE001 - 不是 JSON 就按原文报失败
        return f"失败：{text}"
    if isinstance(payload, dict) and str(payload.get("ended") or "") in UNFINISHED_ENDINGS:
        return ("未完成（这一轮只做了一部分，还没到完成条件，需要接着往下做；"
                "同样的问题别重复问）：" + text)
    return f"失败：{text}"


def _small_image(data_url: str) -> str:
    """慢系统看图的帧也压一道（默认 webp q72 / 最长边 640）。

    640x480 的 JPEG q85 是 24–43KB，webp q72 约 10–14KB；慢系统一次 look 要带两帧，
    压完一轮省 30–60KB。q72 比手模型那条链路的 q65 高一点，因为慢系统要读屏幕上的字。
    压缩失败/没有 Pillow 就原样返回，不拦链路。
    """
    url = str(data_url or "")
    if not url.startswith("data:image/"):
        return url
    try:
        import frame_codec

        out, meta = frame_codec.compress_data_url(url, quality=72, max_side=640)
        return out if meta.get("ok") else url
    except Exception:  # noqa: BLE001 - 压缩只是优化，压坏了不该让这一轮发不出图
        return url


def wait_for_game_window(tools: Any, console: Any, stop_event: Any = None,
                         interjections: Any = None, profile: Any = None,
                         interval: float = 2.0) -> bool:
    """游戏窗口不在就一直等，等的时候**一次模型都不请求**。

    用户 2026-11-05 明确要求：agent 循环要等到游戏窗口出现了再开始跑。
    原来窗口没起也照常调慢系统，模型看不见画面就会编故事、乱按键盘。
    返回 True=窗口已在（可以开始这一轮）；False=用户退出或没有 profile。
    """
    if profile is None:
        return True
    announced = False
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        state: Dict[str, Any]
        try:
            state = tools.window_state()
        except Exception as error:  # noqa: BLE001 - 探针炸了也按「还没窗口」处理
            state = {"ok": False, "reason": f"{type(error).__name__}: {error}"}
        if state.get("ok"):
            if announced:
                console.note("游戏窗口出现了，开始跑")
            return True
        if not announced:
            console.note(
                f"等待游戏窗口（{state.get('reason') or profile.game_id}）…"
                "窗口出现前不会请求模型，也不消耗 token"
            )
            announced = True
        # 等的时候照样要能 /quit、/time：其它输入原样放回队列，不丢弹幕。
        if interjections is not None:
            for text in interjections.drain():
                if text == "/quit":
                    if stop_event is not None:
                        stop_event.set()
                    return False
                if text == "/time":
                    console.note("还在等游戏窗口")
                    continue
                interjections.push(text)
        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)


def lane_trace(console: Any, trace: Any = None) -> Any:
    """把快系统执行器的事件翻译成 TUI 的"快"车道行。

    2026-11-06：同一个 handler 顺手把事件原文写进追踪日志（界面只画一部分，
    日志要能复盘手模型每一步的原文、解析结果和耗时）。

    双系统在界面上必须分得开：慢系统的"想/说/调/果"是整行；快系统是**两只手同时干活**
    ——手（gamescaling）每 33ms 吐操作数据、嘴（Omni）同一条流上给带时间码的解说。
    所以这里只画"决策"和"解说"两类事实，不画"选中了哪个键位"这种内部节拍：
    按键是手的输出，不是决策。
    """
    beat = {"n": 0}

    def handler(event: str, data: Dict[str, Any]) -> None:
        if trace is not None:
            try:
                trace.event("fast_" + event, **dict(data or {}))
            except Exception:  # noqa: BLE001 - 写日志失败不能影响界面
                pass
        if event == "operator_start":
            console.set_status(lane="快")
            beat["n"] = 0
            intent = str(data.get("intent") or "").replace("\n", " ")[:60]
            note = "操作器接手：" + (intent or "（无意图文本）")
            if data.get("text"):
                note += f"；要打的字：{data['text']}"
            if data.get("done_when"):
                note += f"；达成条件：{str(data['done_when'])[:40]}"
            note += "；嘴" + ("在线" if data.get("narrator") else "缺席（只动手不解说）")
            console.fast(note)
            return
        if event == "operator_action":
            beat["n"] += 1
            n = beat["n"]
            keys = [str(key) for key in (data.get("keys") or []) if str(key)]
            # 每 5 次决策报一次心跳；凡是真按下了键就报——不刷屏，也不瞒操作。
            if keys or n == 1 or n % 5 == 0:
                line = f"手 #{n} · 决策 {int(data.get('decide_ms') or 0)}ms"
                if keys:
                    line += " · 键 " + "、".join(dict.fromkeys(keys))
                else:
                    line += " · 无动作"
                if data.get("skipped"):
                    line += " · 跳过 " + "、".join(str(k) for k in data["skipped"][:3])
                console.fast(line)
            return
        if event == "operator_note":
            t_ms = int(data.get("t_ms") or 0)
            text = str(data.get("text") or "").strip()[:90]
            line = f"嘴 [{t_ms / 1000.0:.1f}s] {text or '（无内容）'}"
            if data.get("done"):
                line += " · 判定达成"
            console.fast(line)
            return
        if event == "operator_end":
            console.set_status(lane="慢")
            line = (f"收手：{data.get('ended')} · {int(data.get('steps') or 0)} 次决策 · "
                    f"{int(data.get('total_ms') or 0)}ms")
            if data.get("reached"):
                line += " · 达成条件成立"
            errors = data.get("errors") or []
            if errors:
                line += f" · 报错 {str(errors[0])[:60]}"
            console.fast(line)
            return

    return handler


def slow_delta_handler(console: Any, aborted: Dict[str, Any]) -> Any:
    '''流式回调：思考过程照常滚动；超预算时记一笔并把「收手」提示打到界面上。

    ``aborted`` 是调用方持有的状态字典（``{'hit': bool, 'why': str}``），
    回调返回后调用方据此判断本轮回复是不是「预算内已收到的部分」。
    '''

    def _on_delta(kind: str, piece: str) -> None:
        if kind == "reasoning":
            console.reasoning(piece)
        elif kind == "text":
            # 2026-11-06：正文也要边写边落。之前只有 reasoning 分支，luna 的正文
            # 全被丢掉，窗口静默 4-6 秒才一次性蹦出整句。
            console.say_delta(piece)
        elif kind == "abort":
            aborted["hit"] = True
            aborted["why"] = str(piece or '')
            console.note(f"慢系统超出预算，已收手：{aborted['why']}")

    return _on_delta


def build_tui_slow_planner(client: Any, *, budget_ms: int = SLOW_BUDGET_MS,
                           trace: Any = None) -> SlowPlanner:
    """把 TUI 已经在用的 lithos client 包成慢系统规划器。

    build_slow_planner(client=...) 会原样接一个 client；这里显式套
    LithosSlowClient，因为 SlowPlanner 要的是 callable(messages, thinking)，
    而 LithosClient 本身是 complete(...) 形状。
    """
    wrapped = LithosSlowClient(client) if client is not None else None
    if wrapped is not None:
        wrapped.trace = trace
    return build_slow_planner(client=wrapped, budget_ms=budget_ms)




#: 只有「要把这一段写成脚本」时，才额外发一次开思考的请求：思考只花在脚本这一次。
SCRIPT_RE = re.compile(r"<script>\s*(.+?)\s*</script>", re.S)
SCRIPT_THINK_PROMPT = (
    "上面就是你刚才的决定。现在输出脚本：把接下来这一段要按的键写成一条脚本命令，不要解释、"
    "不要调用工具、不要写其它段落。\n"
    "只输出一行 <script>命令</script>。命令用这套 DSL：\n"
    "- Z*10  连按 Z 十次\n"
    "- Down*3,Z  下三次再按 Z\n"
    "- Right+Z  同时按\n"
    "- hold:Right:1500  按住 1500ms\n"
    "- wait:2500  等 2500ms\n"
    "- gap:250  后续按键间隔 250ms\n"
    "最多 40 步、总时长不超过 60 秒。只用于不看画面也能确定的流程：推进对话/过场、"
    "确认已知位置的菜单项、取消/返回、字母网格按固定次数挪光标。"
)


def think_script(client: Any, messages: List[Dict[str, Any]], reply: Any,
                 goal: str, on_delta: Any = None, trace: Any = None, turn: int = 0) -> Any:
    """把「上一次请求 + 上一次请求的输出 + 这次的自然语言要求」整段重发，开思考，要它输出脚本。

    用户 2026-11-05 明确：
    - run_script 的参数不再是脚本内容，而是一句自然语言要求（goal）；
    - 生成脚本的这次请求也要记进上下文（调用方负责写 history）；
    - 脚本只在这一处、只这一次开思考生成；其它慢系统请求一律不开。
    返回 (脚本, 这次回复的原文)；拿不到脚本时脚本为空串。
    """
    said = str(getattr(reply, "text", "") or "").strip()
    calls: List[str] = []
    for call in list(getattr(reply, "tool_calls", None) or []):
        fn = (call or {}).get("function") or {}
        name = str(fn.get("name") or "").strip()
        raw = str(fn.get("arguments") or "{}")
        if name:
            calls.append("我调用了工具 " + name + "，参数是 " + raw)
    assistant = "\n".join([said] + calls).strip() or "（这一轮我只调用了工具，没有说话）"
    prompt = SCRIPT_THINK_PROMPT
    if str(goal or "").strip():
        prompt = prompt + chr(10) + "我的要求：" + str(goal).strip()
    msgs = list(messages) + [
        {"role": "assistant", "content": assistant},
        {"role": "user", "content": prompt},
    ]
    started = time.monotonic()
    if trace is not None:
        try:
            trace.event("slow_request", channel="script", turn=turn,
                        messages=trace_log.describe_messages(msgs),
                        prompt_chars=sum(len(str(m.get("content") or "")) for m in msgs),
                        thinking=True, omit_max_tokens=True)
        except Exception:  # noqa: BLE001
            pass
    answer = client.stream_complete(msgs, thinking=True, on_delta=on_delta, omit_max_tokens=True)
    text = str(getattr(answer, "text", "") or "")
    if trace is not None:
        try:
            trace.event("slow_response", channel="script", turn=turn, text=text,
                        reasoning=str(getattr(answer, "reasoning", "") or ""),
                        usage=dict(getattr(answer, "usage", {}) or {}),
                        total_ms=int((time.monotonic() - started) * 1000))
        except Exception:  # noqa: BLE001
            pass
    found = SCRIPT_RE.search(text)
    if found:
        return found.group(1).strip(), text
    for line in text.splitlines():
        body = line.strip().strip("`").strip()
        if body and any(token in body for token in ("*", ":", "+")):
            return body, text
    return "", text

#: 调用 lumine 时同样单开一次开思考的请求：把意图重写成更明确的目标，并写清完成条件。
#: 用户 2026-11-05：这次请求也只发一次、也要进上下文，和其它调用一致；目标不许指导按键。
LUMINE_GOAL_RE = re.compile(r"<goal>\s*(.+?)\s*</goal>", re.S)
LUMINE_DONE_RE = re.compile(r"<done_when>\s*(.+?)\s*</done_when>", re.S)
LUMINE_THINK_PROMPT = (
    "上面就是你刚才的决定。现在只做一件事：把要交给操作器（lumine）的意图重写清楚。"
    "规则：\n"
    "- 只写「要达成什么」，**不要指导怎么按键**（不写按哪个键、按几次、先上还是先下），按键由操作器自己决定；\n"
    "- 必须用英文写（操作模型只吃英文）；屏幕上出现的中文可以原样引用；\n"
    "- 要具体到能判定：在哪一屏、要把什么变成什么样。\n"
    # 2026-11-06 用户选方案 A：重写只做澄清，不许换目标。
    "- **必须保留我原来那条意图的方向和目的**：我写「向上走」就还是「向上走」，我写「按 Z」就还是「按 Z」，"
    "只是把说法写清楚、翻成英文；原意图已经具体就直接按原意写，不要扩大到别的目标，也不要缩小成「先确认状态」这类前置检查；\n"
    "- 原来的完成条件同样只做澄清、不换判定点；我只给了完成条件就保留它。\n"
    "只输出两行，不要解释、不要调用工具：\n"
    "<goal>英文意图</goal>\n"
    "<done_when>英文完成条件（画面上看到什么就算完成）</done_when>"
)


def think_lumine(client: Any, messages: List[Dict[str, Any]], reply: Any,
                 goal: str, done_when: str = "", on_delta: Any = None,
                 trace: Any = None, turn: int = 0) -> Any:
    """把「上一次请求 + 它的输出 + 这次的要求」整段重发，开思考，要一份更好的意图。

    用户 2026-11-05：lumine 也要单开一次思考火花，让模型输出优化过的目标；目标不许指导按键。
    返回 (goal, done_when, 原文)；想不出来时 goal 为空串（调用方退回原 goal）。
    """
    said = str(getattr(reply, "text", "") or "").strip()
    calls: List[str] = []
    for call in list(getattr(reply, "tool_calls", None) or []):
        fn = (call or {}).get("function") or {}
        name = str(fn.get("name") or "").strip()
        raw = str(fn.get("arguments") or "{}")
        if name:
            calls.append("我调用了工具 " + name + "，参数是 " + raw)
    assistant = "\n".join([said] + calls).strip() or "（这一轮我只调用了工具，没有说话）"
    parts = [LUMINE_THINK_PROMPT]
    if str(goal or "").strip():
        parts.append("我原来的意图：" + str(goal).strip())
    if str(done_when or "").strip():
        parts.append("我原来的完成条件：" + str(done_when).strip())
    msgs = list(messages) + [
        {"role": "assistant", "content": assistant},
        {"role": "user", "content": "\n".join(parts)},
    ]
    started = time.monotonic()
    if trace is not None:
        try:
            trace.event("slow_request", channel="lumine_intent", turn=turn,
                        messages=trace_log.describe_messages(msgs),
                        prompt_chars=sum(len(str(m.get("content") or "")) for m in msgs),
                        thinking=True)
        except Exception:  # noqa: BLE001
            pass
    answer = client.stream_complete(msgs, thinking=True, on_delta=on_delta)
    text = str(getattr(answer, "text", "") or "")
    if trace is not None:
        try:
            trace.event("slow_response", channel="lumine_intent", turn=turn, text=text,
                        reasoning=str(getattr(answer, "reasoning", "") or ""),
                        usage=dict(getattr(answer, "usage", {}) or {}),
                        total_ms=int((time.monotonic() - started) * 1000))
        except Exception:  # noqa: BLE001
            pass
    found_goal = LUMINE_GOAL_RE.search(text)
    found_done = LUMINE_DONE_RE.search(text)
    new_goal = found_goal.group(1).strip() if found_goal else ""
    new_done = found_done.group(1).strip() if found_done else ""
    if not new_goal:
        for line in text.splitlines():
            body = line.strip().strip("`").strip()
            if body and not body.startswith("<") and len(body) > 12:
                new_goal = body
                break
    return new_goal, new_done, text

def slow_receipt(reply_text: str, tool_results: Optional[List[Dict[str, Any]]] = None) -> str:
    """把上一轮的「说 + 做」压成慢系统能读的回执。"""
    lines: List[str] = []
    text = (reply_text or "").strip()
    if text:
        lines.append("上一轮我说：" + text[:600])
    for item in tool_results or []:
        mark = "成功" if item.get("ok") else "失败"
        body = str(item.get("text") or "").strip()
        lines.append(f"- {item.get('name', '?')} {mark}：{body[:240]}")
    return "\n".join(lines)


def update_blocked_count(blocked_count: int, tool_results: List[Dict[str, Any]]) -> int:
    """连续卡关计数：这一轮有成功就清零；全失败才 +1；没动手不算卡关。"""
    if not tool_results:
        return int(blocked_count)
    if any(item.get("ok") for item in tool_results):
        return 0
    return int(blocked_count) + 1


class SlowTurnPlanner:
    """TUI 每轮的慢系统规划：出三段式任务；超时/报错不崩，沿用上一轮或兜底。"""

    def __init__(self, planner: Optional[SlowPlanner], console: Any = None,
                 *, fallback_task: str = SLOW_TASK_FALLBACK) -> None:
        self.planner = planner
        self.console = console
        self.fallback_task = fallback_task
        self.last_plan: Optional[SlowPlan] = None
        self.degraded = False
        self.degraded_kind = ""
        self.last_error = ""
        self.rounds = 0

    def plan(self, report_summary: str = "", danmaku: Optional[List[str]] = None,
             history: Optional[List[str]] = None, constraints: Optional[List[str]] = None,
             blocked_count: int = 0) -> SlowPlan:
        self.rounds += 1
        if self.planner is None:
            return self._fallback("unavailable", "慢系统不可用，沿用上一轮", "")
        try:
            plan = self.planner.plan(
                report_summary=report_summary,
                danmaku=danmaku,
                history=history,
                constraints=constraints,
                blocked_count=blocked_count,
                last_plan=(self.last_plan.goal if self.last_plan is not None else None),
            )
            if not plan.goal.strip():
                detail = "；".join(plan.warnings) or "空计划"
                return self._fallback("empty", "慢系统没给出任务，沿用上一轮", detail)
        except SlowPlannerTimeout as error:
            return self._fallback("timeout", "慢系统超时，沿用上一轮", str(error))
        except SlowPlannerError as error:
            return self._fallback("error", "慢系统报错，沿用上一轮", str(error))
        except Exception as error:  # noqa: BLE001 - 规划腿任何意外都不能掐直播
            return self._fallback("error", "慢系统异常，沿用上一轮", f"{type(error).__name__}: {error}")
        self.degraded = False
        self.degraded_kind = ""
        self.last_error = ""
        self.last_plan = plan
        return plan

    def prepare(self, messages: List[Dict[str, Any]], report_summary: str = "",
                danmaku: Optional[List[str]] = None, history: Optional[List[str]] = None,
                constraints: Optional[List[str]] = None,
                blocked_count: int = 0) -> SlowPlan:
        """要计划，并把 plan.goal 作为最后一条 user 任务塞进这一轮 messages。"""
        plan = self.plan(
            report_summary=report_summary,
            danmaku=danmaku,
            history=history,
            constraints=constraints,
            blocked_count=blocked_count,
        )
        messages.append({"role": "user", "content": self.task_text(plan)})
        return plan

    def task_text(self, plan: Optional[SlowPlan] = None) -> str:
        plan = plan or self.last_plan
        if plan is None:
            plan = SlowPlan(goal=self.fallback_task)
        lines = ["【慢系统规划｜这一轮任务】", plan.goal.strip() or self.fallback_task]
        if plan.success.strip():
            lines += ["", "## 就算成了", plan.success.strip()]
        if plan.avoid.strip():
            lines += ["", "## 别做", plan.avoid.strip()]
        if self.degraded:
            lines += ["", f"> {self.status_text()}：按上面这一轮任务继续，别停。"]
        return "\n".join(lines)

    def status_text(self) -> str:
        if not self.degraded:
            return ""
        suffix = "沿用上一轮" if self.last_plan is not None else "兜底"
        prefix = "超时" if self.degraded_kind == "timeout" else "降级"
        return f"{prefix}·{suffix}"

    def _fallback(self, kind: str, note: str, detail: str) -> SlowPlan:
        self.degraded = True
        self.degraded_kind = kind
        self.last_error = detail
        if self.console is not None:
            message = f"{note}（{detail}）" if detail else note
            self.console.note(message)
        if self.last_plan is not None:
            return self.last_plan
        return SlowPlan(goal=self.fallback_task, warnings=[note])


@dataclass
class Entry:
    """一条上下文记录；时间戳跟着记录走，模型才分得清先后与间隔。"""

    role: str
    content: Any
    ts: str
    tool_call_id: str = ""
    images: int = 0
    tool_calls: Optional[List[Dict[str, Any]]] = None
    #: 思考模式的原文：DeepSeek 要求下一轮把 assistant 的 reasoning_content 原样带回。
    reasoning: str = ""

    def text_of(self) -> str:
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            parts = [
                part.get("text", "")
                for part in self.content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return " ".join(item for item in parts if item)
        return ""


class History:
    """带时间戳的上下文，按"文字条数"裁剪，图片单独限流。"""

    def __init__(self, keep_text: int = KEEP_TEXT_MESSAGES, keep_images: int = KEEP_IMAGES):
        self.keep_text = keep_text
        self.keep_images = keep_images
        self.system_prompt = SYSTEM_PROMPT
        self.entries: List[Entry] = []

    def add(self, entry: Entry) -> None:
        self.entries.append(entry)

    def text_count(self) -> int:
        return sum(1 for item in self.entries if item.images == 0)

    def image_count(self) -> int:
        return sum(1 for item in self.entries if item.images > 0)

    def trim(self) -> None:
        """只保留最近 N 条文字消息；更早的图换成一行说明。

        裁剪点必须落在"不会拆散 tool_calls 与 tool 回包"的位置：如果切到一条
        ``role == 'tool'`` 的记录，就继续往前丢，直到丢掉它的 assistant 消息。
        """
        text_entries = [item for item in self.entries if item.images == 0]
        if len(text_entries) > self.keep_text:
            drop = len(text_entries) - self.keep_text
            seen = 0
            cut = 0
            for index, item in enumerate(self.entries):
                if item.images > 0:
                    continue
                seen += 1
                if seen > drop:
                    cut = index
                    break
            while cut < len(self.entries) and self.entries[cut].role == "tool":
                cut += 1
            self.entries = self.entries[cut:]
        self._trim_images()

    def _trim_images(self) -> None:
        image_entries = [item for item in self.entries if item.images > 0]
        excess = len(image_entries) - self.keep_images
        for item in image_entries[: max(0, excess)]:
            item.content = "[图片已省略：这是较早的画面，只保留文字结论]"
            item.images = 0

    def messages(self, now_line: str) -> List[Dict[str, Any]]:
        """拼成 API 需要的 messages：system 提示词 + 带时间的记录 + 当前时间。"""
        output: List[Dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if now_line:
            output.append({"role": "system", "content": now_line})
        for item in self.entries:
            if item.role == "assistant":
                message: Dict[str, Any] = {"role": "assistant", "content": item.text_of()}
                if item.tool_calls:
                    message["tool_calls"] = item.tool_calls
                    # thinking 模式硬要求：带 tool_calls 的 assistant 必须回传
                    # reasoning_content，缺字段直接 HTTP 400（实测 deepseek-flash
                    # 报 "must be passed back to the API"）；空串是合法值，所以
                    # 没有思考时也要发 ""，否则一次空思考会把整段历史钉死成永久 400。
                    message["reasoning_content"] = item.reasoning or ""
                elif item.reasoning:
                    # 纯文本 assistant 省略该字段不影响（实测 B 组 200）。
                    message["reasoning_content"] = item.reasoning
                output.append(message)
            elif item.role == "tool":
                # 工具结果也带时间：模型才能数清"刚才那步是几分钟前做的"。
                output.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.tool_call_id,
                        "content": f"[{item.ts}] {item.text_of()}",
                    }
                )
            else:
                content = item.content
                if isinstance(content, str):
                    content = f"[{item.ts}] {content}"
                output.append({"role": item.role, "content": content})
        return output


class Console:
    """终端输出：思考流式打印 + 一行动态状态栏。

    状态栏只在"光标停在新行开头"时重画，避免把正在流式输出的半行截断。
    同时把去掉颜色码的文字抄进日志文件，窗口被关掉也能复盘。
    """

    ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]|\033\][^\007]*\007")

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self.plain = not sys.stdout.isatty()
        self._lock = threading.RLock()
        self._midline = False
        self._status_visible = False
        self._status: Dict[str, Any] = {}
        self._frame = 0
        self._reasoning_used = 0
        self._line_open = False
        #: 输入框里正在敲的字。底栏 = 状态 + 提示符 + 这段缓冲。
        self._buffer = ""
        #: 思考流的水位：攒够一行再落到滚动区，免得把底栏冲掉。
        self._stream = ""
        self._stream_open = False
        #: 正交流（say）的水位：慢系统边写边落时攒行用。
        self._say_buf = ""
        self._say_stream = ""
        self._say_open = False
        self._stop = threading.Event()
        self._log = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = open(log_path, "a", encoding="utf-8", buffering=1)

    # ---------- 基础写入 ----------

    def _write(self, text: str) -> None:
        with self._lock:
            sys.stdout.write(text)
            sys.stdout.flush()
            if self._log is not None:
                self._log.write(self.ANSI.sub("", text))

        self._buffer = ""
    # ---------- 底栏（状态 + 输入框） ----------

    def _bottom_text(self) -> str:
        return f"{self.status_line()}  {BOLD}▸{RESET} {self._buffer}"

    def _draw_bottom(self) -> None:
        if self.plain:
            return
        width = shutil.get_terminal_size((100, 30)).columns
        line = self._bottom_text()
        if len(line) > width - 1:
            # 输入优先保留：状态太长时砍掉开头，保证正在敲的字看得见。
            tail = f"{BOLD}▸{RESET} {self._buffer}"
            keep = max(8, width - 1 - len(tail))
            line = (self.status_line()[:keep] + " " + tail)[-(width - 1):]
        with self._lock:
            sys.stdout.write("\r\033[2K" + line)
            sys.stdout.flush()

    def _erase_bottom(self) -> None:
        if not self.plain:
            sys.stdout.write("\r\033[2K")

    def _block(self, text: str) -> None:
        """往滚动区打一整行，打完把底栏重新画回去。"""
        with self._lock:
            self._erase_bottom()
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
            if self._log is not None:
                self._log.write(self.ANSI.sub("", text + "\n"))
            self._draw_bottom()

    def set_status(self, **fields: Any) -> None:
        with self._lock:
            self._status.update(fields)
        self._draw_bottom()

    def status_line(self) -> str:
        data = self._status
        phase = data.get("phase", "空闲")
        frame = SPINNER[self._frame % len(SPINNER)] if phase in {"思考中", "调用工具"} else "·"
        short_phase = {"思考中": "想", "调用工具": "做", "准备": "备", "空闲": "闲"}.get(phase, phase)
        lane = data.get("lane", "慢")
        slow = str(data.get("slow") or "")
        line = (
            f"{DIM}{stamp()} · {data.get('elapsed', '00:00:00')}"
            f" · #{data.get('turn', 0)} · {short_phase}{frame} · 车道{lane}"
            f" · 工具{data.get('tools', 0)} · 上下文{data.get('ctx', '0/30')}"
            f" · 弹幕{data.get('queued', 0)}"
        )
        if slow:
            line += f"{YELLOW} · 慢{slow}"
        return line + RESET

    def _tick(self) -> None:
        while not self._stop.wait(0.35):
            self._frame += 1
            self._draw_bottom()
            if not self.plain:
                title = f"lumi · 已玩 {self._status.get('elapsed', '00:00:00')} · 第 {self._status.get('turn', 0)} 轮"
                sys.stdout.write(f"\033]0;{title}\007")
                sys.stdout.flush()

    def start(self) -> None:
        self._draw_bottom()
        if self.plain:
            return
        self._ticker = threading.Thread(target=self._tick, name="status", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._erase_bottom()
            sys.stdout.write("\n")
            sys.stdout.flush()

    # ---------- 键盘输入（真输入框） ----------

    def key(self, ch: str) -> Optional[str]:
        """吃一个按键；回车时返回整行（没有内容返回 None）。"""
        if ch in ("\r", "\n"):
            line = self._buffer.strip()
            self._buffer = ""
            self._draw_bottom()
            return line or None
        if ch == "\b":
            self._buffer = self._buffer[:-1]
        elif ch == "\x1b":
            self._buffer = ""
        elif ch and ch.isprintable():
            self._buffer += ch
        self._draw_bottom()
        return None

    # ---------- 内容输出 ----------

    def start_reasoning(self) -> None:
        self._reasoning_used = 0
        self._line_open = False
        self._stream = ""
        self._stream_open = False
        # 每轮开始清干净正交流水位，免得上一轮残留尾巴混进这一轮。
        self._say_buf = ""
        self._say_stream = ""
        self._say_open = False

    def reasoning(self, piece: str) -> None:
        """思考流：暗色、缩进、限量。观众要听主播说话，不是看小作文草稿。"""
        if self._reasoning_used >= REASONING_DISPLAY_LIMIT:
            return
        self._reasoning_used += len(piece)
        self._stream += piece.replace("\n", " ")
        while len(self._stream) >= 90:
            chunk, self._stream = self._stream[:90], self._stream[90:]
            if self._stream_open:
                self._block(f"{DIM}{self.SAY_INDENT}{chunk}{RESET}")
            else:
                self._block(f"{DIM}[{stamp()}] 想 {RESET}{DIM}{chunk}{RESET}")
                self._stream_open = True

    def end_reasoning(self) -> None:
        if self._stream:
            if self._stream_open:
                self._block(f"{DIM}{self.SAY_INDENT}{self._stream}{RESET}")
            else:
                self._block(f"{DIM}[{stamp()}] 想 {RESET}{DIM}{self._stream}{RESET}")
        elif self._reasoning_used >= REASONING_DISPLAY_LIMIT and not self._stream_open:
            self._block(f"{DIM}[{stamp()}] 想 …（思考过长，后面省略）{RESET}")
        self._stream = ""
        self._stream_open = False
        self._line_open = False
        self._midline = False

    #: 说：第一行带时间戳，后续行缩进对齐——看起来像一个人连着说完一段，
    #: 不是每 24 个字起一行「说」。（用户 2026-11-06：字全绿、一堆「说」不像主播。）
    SAY_INDENT = "      "

    def _say_line(self, chunk: str) -> None:
        piece = chunk.rstrip()
        if not piece:
            return
        if self._say_open:
            self._block(self.SAY_INDENT + piece)
        else:
            self._block(f"{BOLD}[{stamp()}] 说 {RESET}{piece}")
            self._say_open = True

    def say_delta(self, piece: str) -> None:
        """正交流式：慢系统每来一块 text 就落地，攒到一句就落滚动区。"""
        if not piece:
            return
        self._say_buf += piece
        self._say_stream += piece.replace("\n", " ")
        while len(self._say_stream) >= 40 or any(ch in "。！？!?…" for ch in self._say_stream):
            cut = len(self._say_stream)
            for i, ch in enumerate(self._say_stream):
                if ch in "。！？!?…":
                    cut = i + 1
                    break
            else:
                cut = 40
            chunk, self._say_stream = self._say_stream[:cut], self._say_stream[cut:]
            self._say_line(chunk)

    def say(self, text: str) -> None:
        """落一整句。若这一句已经在正交流里逐块显示过，只补剩余尾巴，不整段重打。"""
        body = text.strip()
        streamed = getattr(self, "_say_buf", "")
        tail = getattr(self, "_say_stream", "").strip()
        if tail:
            self._say_line(tail)
        if body and streamed.strip() != body:
            self._say_line(body)
        self._say_buf = ""
        self._say_stream = ""
        self._say_open = False

    def tool_call(self, name: str, arguments: str) -> None:
        self._block(f"{CYAN}[{stamp()}] 调 {RESET}{name} {DIM}{arguments[:300]}{RESET}")

    def tool_result(self, name: str, ok: bool, text: str) -> None:
        color = GREEN if ok else RED
        body = text if len(text) <= 600 else text[:600] + " …"
        self._block(f"{color}[{stamp()}] 果 {RESET}{name} {'ok' if ok else 'fail'} {DIM}{body}{RESET}")

    def fast(self, text: str) -> None:
        """快系统那一侧（操作器里的 Omni：选键、按键、看变化）。

        单独一条车道、洋红、带竖线缩进——慢系统的"说/调/果"是青绿色整行，
        肉眼看得出这一行是另一条腿在干活。
        """
        body = text.strip()
        if body:
            self._block(f"{MAGENTA}[{stamp()}] 快 │ {RESET}{MAGENTA}{body}{RESET}")

    def note(self, text: str) -> None:
        self._block(f"{YELLOW}[{stamp()}] {text}{RESET}")

    def viewer(self, text: str) -> None:
        """观众弹幕：不是命令，主播可以采纳也可以不理。"""
        self._block(f"{BOLD}[{stamp()}] 弹幕 {RESET}{text}")

    def user(self, text: str) -> None:
        self.viewer(text)

    def prompt(self) -> str:
        """画出底栏（状态 + 输入框），返回提示符字面量供老调用方使用。"""
        self._draw_bottom()
        return "▸ "


class Interjections:
    """后台读键盘：跑着的时候也能直接在底栏输入框里打字。

    Windows 上用 ``msvcrt`` 逐键读，自己维护一行缓冲（这样才能画出真正的输入框，
    而不是让 ``input()`` 抢走光标、把状态栏顶掉）；非交互终端回落到 ``input()``。
    """

    def __init__(self, console: Console, stop_event: Optional[threading.Event] = None) -> None:
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.closed = threading.Event()
        self.console = console
        self._stop = stop_event
        threading.Thread(target=self._loop, name="keys", daemon=True).start()

    def _loop(self) -> None:
        if os.name == "nt" and sys.stdin.isatty():
            self._loop_windows()
            return
        while not self.closed.is_set():
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                self.closed.set()
                return
            except Exception:  # noqa: BLE001 - 终端被切走时别把主循环带崩
                return
            text = line.strip()
            if text:
                self.queue.put(text)
            self.console.prompt()

    def _loop_windows(self) -> None:
        import msvcrt  # 仅 Windows；局部导入避免其他平台炸在 import 上

        while not self.closed.is_set():
            if self._stop is not None and self._stop.is_set():
                return
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            try:
                ch = msvcrt.getwch()
            except Exception:  # noqa: BLE001 - 控制台消失
                return
            if ch in ("\x00", "\xe0"):  # 方向键/功能键是两个字符，整对丢掉
                msvcrt.getwch()
                continue
            if ch == "\x03":  # Ctrl+C：当作请求停止
                if self._stop is not None:
                    self._stop.set()
                self.closed.set()
                return
            line = self.console.key(ch)
            if line:
                self.queue.put(line)

    def push(self, text: str) -> bool:
        """外部（HTTP API）也能往同一条队列里插话。"""
        text = (text or "").strip()
        if not text:
            return False
        self.queue.put(text)
        return True

    def drain(self) -> List[str]:
        items: List[str] = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                return items


@dataclass
class Runtime:
    """给 HTTP API 看的共享状态。"""

    started: float = field(default_factory=time.time)
    goal: str = ""
    turn: int = 0
    phase: str = "启动中"
    tier: str = "fast"
    model: str = ""
    tools: int = 0
    last_tool: str = ""
    last_error: str = ""
    last_reply: str = ""
    log_path: str = ""
    history_text: int = 0
    history_image: int = 0
    queued: int = 0
    legs: str = ""
    fast_intent: str = ""
    slow_goal: str = ""
    slow_degraded: bool = False
    finished: bool = False
    summary: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "ok": True,
                "elapsed": elapsed_text(self.started),
                "startedAt": full_stamp(self.started),
                "goal": self.goal,
                "turn": self.turn,
                "phase": self.phase,
                "tier": self.tier,
                "model": self.model,
                "tools": self.tools,
                "lastTool": self.last_tool,
                "lastError": self.last_error,
                "lastReply": self.last_reply[:2000],
                "log": self.log_path,
                "context": {"text": self.history_text, "images": self.history_image, "keepText": KEEP_TEXT_MESSAGES},
                "queued": self.queued,
                "legs": self.legs,
                "slowGoal": self.slow_goal,
                "slowDegraded": self.slow_degraded,
                "finished": self.finished,
                "summary": self.summary,
                "now": full_stamp(),
            }

    def event(self, kind: str, text: str) -> None:
        with self.lock:
            self.events.append({"ts": full_stamp(), "kind": kind, "text": text[:2000]})
            if len(self.events) > 200:
                del self.events[:-200]


class QuietServer(ThreadingHTTPServer):
    """连不上/提前断开时不要往终端里打 traceback，会撕碎状态栏。"""

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: D401 - 覆盖只为静音
        return


class Api:
    """本机调试接口：读状态、插话、看事件、叫停。"""

    def __init__(self, host: str, port: int, token: str, runtime: Runtime, interjections: Interjections,
                 stop: threading.Event, danmaku: Any = None):
        self.runtime = runtime
        self.interjections = interjections
        self.stop = stop
        # 弹幕环形缓冲：None 表示这一场没接弹幕源（接口会如实说 not_configured）。
        self.danmaku = danmaku
        handler = self._make_handler(token)
        self.server = QuietServer((host, port), handler)
        self.host, self.port = host, self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="api", daemon=True).start()

    def _make_handler(self, token: str):
        runtime, interjections, stop_event = self.runtime, self.interjections, self.stop
        danmaku = self.danmaku

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # 静音，别把状态栏打乱
                return

            def _auth(self) -> bool:
                header = self.headers.get("Authorization", "")
                return header == f"Bearer {token}"

            def _send(self, payload: Dict[str, Any], code: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
                if not self._auth():
                    self._send({"ok": False, "error": "unauthorized"}, 401)
                    return
                if self.path.startswith("/state"):
                    self._send(runtime.snapshot())
                    return
                if self.path.startswith("/danmaku"):
                    if danmaku is None:
                        self._send({"ok": False, "error": "not_configured", "items": []}, 200)
                        return
                    since_ms, limit = 0, 50
                    query = self.path.split("?", 1)[1] if "?" in self.path else ""
                    for pair in query.split("&"):
                        if "=" not in pair:
                            continue
                        key, value = pair.split("=", 1)
                        try:
                            if key == "since":
                                since_ms = int(value)
                            elif key == "limit":
                                limit = int(value)
                        except ValueError:
                            self._send({"ok": False, "error": "bad_query", "query": query}, 400)
                            return
                    items = danmaku.since(since_ms, limit)
                    self._send({"ok": True, "since_ms": since_ms, "items": items,
                                "next_since": items[-1]["t_ms"] if items else since_ms})
                    return
                if self.path.startswith("/stats"):
                    stats = danmaku.stats() if danmaku is not None else {"ok": False, "error": "not_configured"}
                    payload = dict(stats)
                    payload.update({"phase": runtime.phase, "elapsed": elapsed_text(runtime.started),
                                    "queue": interjections.queue.qsize()})
                    self._send(payload)
                    return
                if self.path.startswith("/events"):
                    limit = 30
                    if "limit=" in self.path:
                        try:
                            limit = max(1, min(200, int(self.path.split("limit=")[1].split("&")[0])))
                        except ValueError:
                            pass
                    with runtime.lock:
                        items = runtime.events[-limit:]
                    self._send({"ok": True, "events": items})
                    return
                self._send({"ok": False, "error": "not_found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth():
                    self._send({"ok": False, "error": "unauthorized"}, 401)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._send({"ok": False, "error": "bad_json"}, 400)
                    return
                if self.path.startswith("/danmaku"):
                    if danmaku is None:
                        self._send({"ok": False, "error": "not_configured"}, 200)
                        return
                    item = danmaku.add(body.get("text") or "", str(body.get("user") or ""))
                    # 默认只进缓冲：慢系统要用 read_danmaku 主动取，不再直接塞进上下文。
                    # interject=true 才当插话推进队列（人工调试用）。
                    queued = False
                    if body.get("interject") and item is not None:
                        queued = interjections.push(item["text"])
                    self._send({"ok": item is not None, "item": item, "queued": queued},
                               200 if item is not None else 400)
                    return
                if self.path.startswith("/say"):
                    text = str(body.get("text") or "")
                    queued = interjections.push(text)
                    self._send({"ok": queued, "queued": queued, "text": text[:500]}, 200 if queued else 400)
                    return
                if self.path.startswith("/quit"):
                    stop_event.set()
                    self._send({"ok": True, "quitting": True})
                    return
                self._send({"ok": False, "error": "not_found"}, 404)

        return Handler

    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def run(
    goal: str,
    *,
    max_turns: int,
    urgency: float,
    log_root: Path,
    api_port: int,
    api_host: str = "127.0.0.1",
    log_file: Optional[Path] = None,
    allow_stub_motor: bool = False,
    once: bool = False,
    game: str = "",
    restart: bool = False,
) -> Dict[str, Any]:
    session = Session(root=log_root)
    profile = None
    system_prompt = SYSTEM_PROMPT
    prompt_meta: Dict[str, Any] = {"source": "static"}
    if game:
        profile = open_profile(game)
        system_prompt, prompt_meta = resolve_system_prompt(profile.game_id)
    tools = BetterGITools(session, allow_stub_motor=allow_stub_motor, profile=profile)
    tool_specs = tools.tool_specs()
    # 开播前清档重开：上次那局的中途状态不能带进这次直播，否则"从干净档开始"就是假的。
    restart_info: Dict[str, Any] = {}
    if restart and profile is not None:
        try:
            from adapter import ProfileAdapter

            restart_info = ProfileAdapter(profile).restart(clear_save=True)
        except Exception as error:  # noqa: BLE001 - 重开失败也要能播，但要留下原因
            restart_info = {"error": f"{type(error).__name__}: {error}"}
    client = lithos.LithosClient()
    console = Console(log_file)
    # 2026-11-06 用户要求：日志里要有除图片外的全部提示词、输出和时间。
    # 与 session 的 run-*.jsonl 同名前缀落一份 trace，逐轮 append。
    trace = trace_log.build(
        session.path().with_name(session.path().stem + ".trace.jsonl"),
        run_id=session.session_id,
    )
    if trace is None:
        console.note("追踪日志没建起来（不影响直播，只是复盘时没有提示词/原文）")
    # 慢系统规划腿：构造一次，逐轮复用；装配失败不拦直播，退化成兜底任务。
    try:
        slow_planner = SlowTurnPlanner(build_tui_slow_planner(client, trace=trace), console)
    except Exception as error:  # noqa: BLE001 - 慢系统装配失败不能掐直播
        console.note(f"慢系统规划不可用：{type(error).__name__}: {error}（改用兜底任务）")
        slow_planner = SlowTurnPlanner(None, console)
    history = History()
    # ---- 记忆接进回路：检索到的 episode 进上下文，行动结果写回 episode ----
    memory = None
    if profile is not None:
        try:
            from memory import build_memory

            memory = build_memory(profile, backend=os.environ.get("BETTERAGI_MEMORY", "local-json"))
        except Exception as error:  # noqa: BLE001 - 记忆坏了不能把直播掐了
            console.note(f"记忆后端不可用：{type(error).__name__}: {error}（本次直播不带记忆）")
            memory = None
    history.system_prompt = system_prompt
    runtime = Runtime(goal=goal, log_path=str(session.path()))
    interjections = Interjections(console)
    # 弹幕：先进环形缓冲，慢系统用 read_danmaku 按需取（设计 02 §9 的口径：
    # 不再每轮把弹幕塞进上下文）。tools 持有同一个 hub，因此 read_danmaku 同进程直读。
    try:
        from danmaku_hub import build_hub

        danmaku_hub = build_hub()
    except Exception as error:  # noqa: BLE001 - 弹幕坏了不影响播
        console.note(f"弹幕缓冲不可用：{type(error).__name__}: {error}")
        danmaku_hub = None
    if danmaku_hub is not None:
        tools.danmaku_hub = danmaku_hub
    # 快系统不在这里常驻：它是"手 + 嘴"，只在 lumine 操作器里启动（手=gamescaling 每 33ms
    # 出操作数据，嘴=Omni 在同一条画面流上解说并界定收手）。慢系统拿到的是 look 的真图 +
    # lumine 回传的解说时间轴与情况总结，而不是每轮一遍的"读屏文字"。
    last_frame: Optional[str] = None
    stop_event = threading.Event()
    started = time.time()

    # 开发环境才有 end_test：模型自己决定"这次测试做完了"就把运行收尾。
    # 生产（没有 BETTERAGI_DEV）什么都不挂，工具会如实拒绝，直播不会被模型关掉。
    dev_mode = (os.environ.get("BETTERAGI_DEV", "").strip().lower() in ("1", "true", "yes", "on"))
    # 快慢车道：操作器一开工就播报到界面（不挂钩子时零成本）。
    # trace 同时镜像进日志：手模型每一步的原文、解析、耗时全留。
    tools.trace = lane_trace(console, trace)
    tools.trace_log = trace
    if dev_mode:
        tools.dev_enabled = True
        tools.stop_hook = stop_event
        console.note("开发模式：end_test 可用（模型可以自己结束这次测试）")

    describe = client.describe()
    runtime.model = describe["models"]["fast"]
    if trace is not None:
        try:
            trace.event(
                "run_start",
                model=runtime.model,
                describe=describe,
                goal=goal,
                game=getattr(profile, "game_id", "") or "",
                system_prompt=history.system_prompt,
                prompt_meta=prompt_meta,
                keep_text=KEEP_TEXT_MESSAGES,
                keep_images=KEEP_IMAGES,
                urgency=urgency,
                dev_mode=dev_mode,
                restart=restart,
                restarted=restart_info,
                trace_path=str(trace.path),
            )
        except Exception:  # noqa: BLE001 - 日志坏了不能拦开播
            pass
    console.note(
        f"模型 {runtime.model}；上下文保留最近 {KEEP_TEXT_MESSAGES} 条文字、最近 {KEEP_IMAGES} 张图；"
        f"开始于 {full_stamp()}"
    )
    console.note("直接打字就是插话，输入 /quit 退出，/time 看玩了多久")

    api: Optional[Api] = None
    if api_port:
        token = secrets.token_urlsafe(24)
        api = Api(api_host, api_port, token, runtime, interjections, stop_event, danmaku=danmaku_hub)
        endpoint = Path("E:/BetterAGI/_diag/tui-endpoint.json")
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        endpoint.write_text(
            json.dumps({"url": api.url(), "token": token, "startedAt": full_stamp()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        console.note(f"调试接口 {api.url()}（token 在 {endpoint}）")

    console.set_status(phase="准备", elapsed="00:00:00", turn=0, tools=0, ctx="0/30", queued=0)
    console.start()

    prepare = {"activated": None}
    #: 通用通道（非原神）：先等游戏窗口出现再开跑。窗口没起就请求模型，
    #: 模型看不见画面只能编故事（用户 2026-11-05 明确要求改成先等窗口）。
    gated_channel = profile is not None and profile.game_id != "genshin-impact"
    if gated_channel:
        ready = wait_for_game_window(tools, console, stop_event, interjections, profile)
        if not ready:
            console.note("还没等到游戏窗口，先停在这里（按 /quit 或关窗口退出）")
            return {"ok": True, "summary": "用户退出"}
    try:
        # 有 profile 的游戏（Undertale 等）走通用适配层：前台由 adapter.prepare() 负责，
        # 不再调用 BetterGI 的 /api/v1/agent/actions（非原神宿主上那条通道必然 409）。
        prepare = (
            tools.generic_prepare()
            if gated_channel
            else tools.ensure_foreground()
        )
        console.note(f"前台准备：{json.dumps(prepare, ensure_ascii=False)[:200]}")
        runtime.event("foreground", json.dumps(prepare, ensure_ascii=False))
    except Exception as error:  # noqa: BLE001 - 宿主没起来也要把 TUI 留在窗口里
        console.note(f"前台准备失败：{type(error).__name__}: {error}（继续，等下一轮再试）")
        runtime.event("foreground_failed", str(error))
        runtime.last_error = str(error)[:300]

    history.add(Entry(role="user", content=f"目标：{goal}", ts=full_stamp()))
    session.append("start", goal=goal, urgency=urgency, tier=lithos.choose_tier(urgency))
    console.prompt()

    final: Dict[str, Any] = {"ok": False, "summary": None, "turns": 0}
    #: 连续模型失败计数：重试是留给网络抖动的，不是给永久 4xx 烧额度的。
    llm_failures = 0
    recent_danmaku: List[str] = []
    last_receipt = ""
    blocked_count = 0
    try:
        # 主播型 agent 不会"玩完"：永远播下去，没有轮次上限。
        # 停的条件只有三个：/quit、外部停止、连续模型失败。--turns 只给调试用，超过后继续播。
        turn_iter = itertools.count(1)
        for turn in turn_iter:
            round_tools: List[Dict[str, Any]] = []
            if stop_event.is_set():
                final.update({"ok": True, "summary": "外部请求停止"})
                break
            # 每轮开头再确认一次窗口还在：游戏被关掉/切走时，这一轮不该请求模型。
            if gated_channel and not wait_for_game_window(
                tools, console, stop_event, interjections, profile
            ):
                final.update({"ok": True, "summary": "用户退出"})
                break
            # 弹幕先入上下文，再请求模型：这样这一轮它就能看见观众说了什么。
            submitted = False
            for text in interjections.drain():
                if text == "/quit":
                    final.update({"ok": True, "summary": "用户退出"})
                    return final
                if text == "/time":
                    console.note(f"已经玩了 {elapsed_text(started)}（会话开始 {full_stamp(started)}）")
                    continue
                submitted = True
                console.viewer(text)
                if trace is not None:
                    try:
                        trace.event("interjection", turn=turn, text=text)
                    except Exception:  # noqa: BLE001
                        pass
                recent_danmaku.append(text)
                del recent_danmaku[:-RECENT_DANMAKU_LIMIT]
                runtime.event("interject", text)
                history.add(
                    Entry(
                        role="user",
                        content=f"（观众弹幕，可能在跟你说话，也可能只是在聊天；采纳与否你自己判断）{text}",
                        ts=full_stamp(),
                    )
                )

            if submitted:
                # 打弹幕会把键盘焦点抢到 TUI；严格安全要求游戏在前台，
                # 所以提交完立刻把游戏切回来，否则下一轮就动不了。
                try:
                    tools.ensure_foreground()
                except Exception as error:  # noqa: BLE001 - 切不回来也不该崩
                    console.note(f"切回游戏窗口失败：{type(error).__name__}: {error}")

            history.trim()
            runtime.turn = turn
            runtime.history_text, runtime.history_image = history.text_count(), history.image_count()
            # 每轮都把"腿通不通"写进上下文：没开 Lumine 时别让模型对着空气反复调用。
            # 但直控通道（非原神）压根没有 lumine 这条腿——把"腿断了"写进去，模型就会
            # 一直说"只能看不能动"，手里有键盘也不按。所以按通道分开写。
            if tools.uses_lumine():
                legs = probe_legs(tools)
                runtime.legs = "可用" if legs.get("available") else f"不可用（{legs.get('reason')}）"
                legs_line = legs_note(legs)
            else:
                runtime.legs = "直控键位"
                legs_line = "你手里有操作器：look 看，lumine 把意图交给它执行。没有需要等待的外部桥接。"
            console.set_status(legs=runtime.legs)
            now_line = (
                f"现在是 {full_stamp()}；本次会话已进行 {elapsed_text(started)}；你叫 lumi。"
                f"{legs_line}"
            )
            # 记忆检索：目标 + 上一轮看到的画面摘要当查询，召回自己写过的笔记。
            recall_line = ""
            if memory is not None:
                try:
                    recalled = recall_for(
                        memory,
                        goal=goal,
                        hint=f"{runtime.last_reply or ''} {runtime.last_tool or ''}",
                        limit=4,
                    )
                    recall_line = format_recall(recalled)
                    runtime.recalled = len(recalled)
                except Exception as error:  # noqa: BLE001 - 记忆坏了不能停播
                    console.note(f"记忆检索失败：{type(error).__name__}: {error}")
                    recall_line = ""
            if recall_line:
                now_line = now_line + "\n\n" + recall_line
            # 快系统不再是"每轮读屏一次"：它在 lumine 里跑（手按 33ms 决策、嘴同期解说并
            # 界定收手），回传的解说时间轴和情况总结会作为工具结果进上下文。这里不再注入
            # 逐帧转述——那既重复又会让慢系统把解说员当成眼睛。

            messages = history.messages(now_line)
            # 慢系统先出「这一轮做什么」：只给计划，不碰画面；2s 预算，超时沿用上一轮。
            plan = slow_planner.prepare(
                messages,
                report_summary=last_receipt,
                danmaku=recent_danmaku[-RECENT_DANMAKU_LIMIT:],
                blocked_count=blocked_count,
            )
            runtime.slow_goal = plan.goal[:400]
            runtime.slow_degraded = slow_planner.degraded
            runtime.phase = "思考中"
            console.set_status(
                phase="思考中",
                elapsed=elapsed_text(started),
                turn=turn,
                tools=runtime.tools,
                ctx=f"{runtime.history_text}/{KEEP_TEXT_MESSAGES}",
                queued=interjections.queue.qsize(),
                slow=slow_planner.status_text(),
            )
            runtime.event(
                "slow_plan_degraded" if slow_planner.degraded else "slow_plan",
                f"{slow_planner.status_text() or 'ok'}｜{plan.goal[:300]}",
            )
            if trace is not None:
                try:
                    trace.event("slow_plan", turn=turn, goal=plan.goal, success=plan.success,
                                avoid=plan.avoid, degraded=slow_planner.degraded,
                                degraded_kind=slow_planner.degraded_kind,
                                warnings=list(plan.warnings))
                except Exception:  # noqa: BLE001
                    pass
            session.append(
                "slow_plan",
                turn=turn,
                goal=lithos.redact(plan.goal, 400),
                success=lithos.redact(plan.success, 400),
                avoid=lithos.redact(plan.avoid, 400),
                degraded=slow_planner.degraded,
                degraded_kind=slow_planner.degraded_kind,
                warnings=list(plan.warnings),
            )

            console.start_reasoning()
            aborted = {"hit": False, "why": ""}
            # 慢系统超过墙钟预算（默认 2s）：收手比继续等更像人，界面要看得见。
            _on_delta = slow_delta_handler(console, aborted)
            # 2026-11-06：这一轮请求的完整提示词（图片只留元信息）+ 首字/总耗时落盘。
            _req_t = {"started": time.monotonic(), "first": None}

            def _on_delta_tracked(kind: str, piece: str) -> None:
                if _req_t["first"] is None and kind in ("reasoning", "text") and str(piece or ""):
                    _req_t["first"] = time.monotonic()
                _on_delta(kind, piece)

            if trace is not None:
                try:
                    trace.event(
                        "slow_request",
                        channel="main",
                        turn=turn,
                        model=runtime.model,
                        base_url=str(getattr(client, "base_url", "") or ""),
                        tool_names=[str((c.get("function") or {}).get("name")) for c in (tool_specs or [])],
                        tools=tool_specs,
                        system=history.system_prompt,
                        messages=trace_log.describe_messages(messages),
                        thinking=True,
                        urgency=urgency,
                        goal=goal,
                        slow_plan={
                            "goal": plan.goal,
                            "success": plan.success,
                            "avoid": plan.avoid,
                            "degraded": slow_planner.degraded,
                            "kind": slow_planner.degraded_kind,
                        },
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                reply = client.stream_complete(
                    messages,
                    urgency=urgency,
                    tools=tool_specs,
                    on_delta=_on_delta_tracked,
                    # 2026-11-06 用户推翻 11-05 的口径：「不允许慢系统不经思考」，
                    # 不过脑子就狂按按键是有病的疯子才干的事。所以主循环恢复开思考，
                    # 让每一轮动手前先有过一遍画面的判断（思考会走 reasoning 通道流式显示）。
                    thinking=True,
                )
            except Exception as error:  # noqa: BLE001 - 网络抖动不该把窗口关掉
                console.end_reasoning()
                runtime.phase = "模型异常"
                runtime.last_error = f"{type(error).__name__}: {error}"[:300]
                runtime.event("llm_error", runtime.last_error)
                if trace is not None:
                    try:
                        trace.event("llm_error", turn=turn, text=runtime.last_error)
                    except Exception:  # noqa: BLE001
                        pass
                if is_reasoning_400(error):
                    # 历史里带 tool_calls 的 assistant 已经强制补了 reasoning_content（空串合法）。
                    # 还复发说明服务端又加了新约束：停下来报清楚，别每 3 秒重试烧钱。
                    console.note(f"thinking 模式 400（reasoning_content）：{runtime.last_error}；停止重试")
                    final.update({"ok": False, "failed": True, "summary": f"模型 400：{runtime.last_error}"})
                    break
                llm_failures += 1
                if llm_failures >= MAX_CONSECUTIVE_LLM_FAILURES:
                    console.note(f"模型连续失败 {llm_failures} 次，停止本次会话：{runtime.last_error}")
                    final.update({"ok": False, "failed": True, "summary": f"模型连续失败 {llm_failures} 次"})
                    break
                console.note(f"模型调用失败：{runtime.last_error}；3 秒后重试")
                time.sleep(3)
                continue
            llm_failures = 0
            console.end_reasoning()
            if getattr(reply, 'aborted', False) or aborted["hit"]:
                # 落盘留痕：复盘时能分清「想完了才说」和「想不完先收手」。
                runtime.event('slow_aborted', aborted["why"] or 'streaming deadline exceeded')
                console.note('本轮回复是预算内已收到的部分（不是完整规划）')
            console.say(reply.text)
            runtime.tier = reply.tier
            runtime.last_reply = reply.text
            if trace is not None:
                try:
                    trace.event(
                        "slow_response",
                        channel="main",
                        turn=turn,
                        text=reply.text,
                        reasoning=reply.reasoning,
                        tool_calls=reply.tool_calls,
                        tier=reply.tier,
                        model=reply.model,
                        latency_ms=reply.latency_ms,
                        aborted=bool(getattr(reply, "aborted", False)),
                        usage=reply.usage,
                        ttft_ms=(
                            None
                            if _req_t["first"] is None
                            else int((_req_t["first"] - _req_t["started"]) * 1000)
                        ),
                        total_ms=int((time.monotonic() - _req_t["started"]) * 1000),
                    )
                except Exception:  # noqa: BLE001
                    pass
            runtime.event("assistant", reply.text)

            history.add(
                Entry(
                    role="assistant",
                    content=reply.text or "",
                    ts=full_stamp(),
                    tool_calls=reply.tool_calls or None,
                    reasoning=reply.reasoning or "",
                )
            )
            session.append(
                "assistant",
                turn=turn,
                tier=reply.tier,
                model=reply.model,
                latencyMs=reply.latency_ms,
                text=lithos.redact(reply.text, 2000),
                toolCalls=[call.get("function", {}).get("name") for call in reply.tool_calls],
            )
            final["turns"] = turn

            if not reply.tool_calls:
                last_receipt = slow_receipt(reply.text, [])
                if reply.text.strip():
                    console.note("这一轮没动手；按你自己的节奏继续，观众弹幕看看就好")
                continue

            pending_images: List[str] = []
            # 开思考补发的那几次请求（脚本 / lumine 意图）也要留进上下文（用户 2026-11-05）。
            pending_notes: List[Any] = []
            for call in reply.tool_calls:
                name = call["function"]["name"]
                raw_args = call["function"].get("arguments") or "{}"
                console.tool_call(name, raw_args)
                if trace is not None:
                    try:
                        trace.event("tool_call", turn=turn, name=name, arguments=raw_args)
                    except Exception:  # noqa: BLE001
                        pass
                runtime.phase = "调用工具"
                runtime.last_tool = name
                runtime.tools += 1
                console.set_status(phase="调用工具", tools=runtime.tools, lastTool=name)
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError:
                    arguments = {}
                if name == "run_script" and client is not None:
                    # 用户 2026-11-05：run_script 只收自然语言 goal；精确脚本在这一处、
                    # 只这一次开思考生成，并且这次请求与结果都要记进上下文。
                    goal = str(arguments.get("goal") or "").strip()
                    try:
                        script, raw = think_script(client, messages, reply, goal,
                                                   trace=trace, turn=turn)
                    except Exception as error:  # noqa: BLE001 - 想不出来就走空脚本分支
                        console.note(f"脚本思考失败：{type(error).__name__}: {error}")
                        script, raw = "", ""
                    if raw:
                        # 先记下来，等 tool 消息落进去之后再写：assistant(tool_calls) 之后必须是
                        # tool 消息，插在中间会让下一轮请求直接 400。
                        pending_notes.append(("【脚本请求】" + goal, raw))
                    if script:
                        console.note(f"思考后的脚本：{script[:160]}")
                    arguments = {"goal": goal, "script": script}
                elif name == "lumine" and client is not None:
                    # 用户 2026-11-05：调 lumine 时也单开一次思考，让模型输出优化过的目标；
                    # 目标只写「要做什么」，不许指导按键。这次请求同样记进上下文。
                    goal = str(arguments.get("goal") or "").strip()
                    done_when = str(arguments.get("done_when") or "").strip()
                    try:
                        better_goal, better_done, raw = think_lumine(
                            client, messages, reply, goal, done_when,
                            trace=trace, turn=turn)
                    except Exception as error:  # noqa: BLE001 - 想不出来就用原目标
                        console.note(f"lumine 意图思考失败：{type(error).__name__}: {error}")
                        better_goal, better_done, raw = "", "", ""
                    if raw:
                        pending_notes.append(("【lumine 意图请求】" + goal, raw))
                    if better_goal:
                        arguments["goal"] = better_goal
                        console.note(f"思考后的意图：{better_goal[:160]}")
                    if better_done and not done_when:
                        arguments["done_when"] = better_done
                try:
                    result: ToolResult = tools.dispatch(name, arguments)
                except Exception as error:  # noqa: BLE001 - 工具失败如实回给模型
                    result = ToolResult(name, False, f"{type(error).__name__}: {error}")
                console.tool_result(result.name, result.ok, result.text)
                if trace is not None:
                    try:
                        trace.event("tool_result", turn=turn, name=result.name, ok=result.ok,
                                    text=result.text)
                    except Exception:  # noqa: BLE001
                        pass
                round_tools.append({"name": result.name, "ok": result.ok, "text": result.text})
                runtime.event("tool", f"{result.name} {'ok' if result.ok else 'fail'} {result.text[:300]}")
                if result.name == "end_test" and result.ok:
                    # 模型自己判断测试做完了：把这一轮剩下的工具跑完就收尾，
                    # 别在"要退出了"之后还继续按键。
                    console.note("模型请求结束测试，本轮收尾后退出")
                    final.update({"ok": True, "summary": f"模型结束测试：{result.text[:160]}"})
                if not result.ok:
                    runtime.last_error = result.text[:300]
                session.append("tool", turn=turn, tool=result.name, ok=result.ok, text=result.text[:2000])
                if memory is not None:
                    try:
                        writeback(
                            memory,
                            episode_from_tool(
                                name,
                                arguments if isinstance(arguments, dict) else {},
                                result.text,
                                result.ok,
                                group_id=getattr(memory, "group_id", ""),
                            ),
                        )
                    except Exception as error:  # noqa: BLE001 - 写回失败不影响这一轮
                        console.note(f"记忆写回失败：{type(error).__name__}: {error}")
                if result.image:
                    if result.name == "look":
                        # 用户 2026-11-05：每次 look 都要同时带「上一张」和「这一张」，
                        # 只有两帧一起给它，它才能判断刚才的动作到底生效了没有。
                        if last_frame and last_frame != result.image:
                            pending_images.extend([last_frame, result.image])
                        else:
                            pending_images.append(result.image)
                    else:
                        pending_images.append(result.image)
                    last_frame = result.image
                history.add(
                    Entry(
                        role="tool",
                        content=_tool_content(result),
                        ts=full_stamp(),
                        tool_call_id=call["id"],
                    )
                )
                # 开思考补发的那几次请求也留在上下文里（用户 2026-11-05 要求）。
                while pending_notes:
                    note_label, note_raw = pending_notes.pop(0)
                    history.add(Entry(role="user", content=note_label, ts=full_stamp()))
                    history.add(Entry(role="assistant", content=note_raw, ts=full_stamp()))

            last_receipt = slow_receipt(reply.text, round_tools)
            blocked_count = update_blocked_count(blocked_count, round_tools)

            if pending_images:
                parts: List[Dict[str, Any]] = [{"type": "text", "text": f"[{full_stamp()}] 上面工具返回的画面："}]
                for data_url in pending_images:
                    parts.append({"type": "image_url", "image_url": {"url": _small_image(data_url)}})
                history.add(Entry(role="user", content=parts, ts=full_stamp(), images=len(pending_images)))
    finally:
        session.append("end", ok=final["ok"], summary=final.get("summary"), turns=final["turns"])
        if trace is not None:
            try:
                trace.event("run_end", ok=final.get("ok"), failed=final.get("failed"),
                            summary=final.get("summary"), turns=final.get("turns"),
                            elapsed_s=int(time.time() - started))
            except Exception:  # noqa: BLE001
                pass
            trace.close()
        runtime.finished = True
        runtime.summary = str(final.get("summary") or "")
        console.stop()
        if api is not None:
            api.server.shutdown()

    if not final["ok"] and not final.get("failed"):
        # TUI 是常驻交互：跑满轮数只是"这一程结束"，不是失败；真错误（failed）不洗白。
        final.update({"ok": True, "summary": final.get("summary") or "会话结束"})
    final["log"] = str(session.path())
    if trace is not None:
        final["trace"] = str(trace.path)
    return final


DEFAULT_HOST_GOAL = (
    "你是主播 lumi，正在直播玩这个游戏。每一轮先看画面，再决定动作；"
    "能交给策略模型（motor）决定的就交给它，只有它不可用时才自己手动按键。"
    "一边玩一边跟观众说话，节奏像真人直播。永远不说再见、不等指令，自己决定下一步。"
)


def main(argv: Optional[List[str]] = None) -> int:
    # 重定向到文件时 Python 默认用系统代码页（cp936）写，中文会变乱码；统一成 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老终端不支持就算了
            pass
    parser = argparse.ArgumentParser(description="BetterAGI 极简 TUI（看见思考、看见工具、可插话、带调试 API）")
    parser.add_argument("--goal", help="可选：想让它做什么。不给就按主播默认准则持续播")
    parser.add_argument("--goal-file", help="从 UTF-8 文件读目标")
    parser.add_argument("--turns", type=int, default=0, help="最多多少轮；0=不限（默认），仅作安全阀")
    parser.add_argument("--urgency", type=float, default=0.5, help="紧迫度 0–1，决定 base/fast/ultra")
    parser.add_argument("--port", type=int, default=8791, help="调试 API 端口，0 表示不开")
    parser.add_argument(
        "--restart",
        action="store_true",
        help="启动前清掉存档并重开游戏（每次开播都从干净档开始）",
    )
    parser.add_argument(
        "--game",
        default="",
        help="游戏 profile 名（如 undertale / 原神）。给了就按 profile 选工具面与提示词；不给走 BetterGI 原神历史路径",
    )
    parser.add_argument("--log-root", default="E:/BetterAGI/_diag/agent-runs")
    parser.add_argument("--log-file", default="E:/BetterAGI/_diag/tui.log", help="把终端内容（去颜色）抄一份到文件")
    parser.add_argument(
        "--allow-stub-motor",
        action="store_true",
        help="允许使用本机替身 Motor（只按 W）；默认拒绝，避免把替身当成真 Lumine 策略",
    )
    parser.add_argument(
        "--once",  # 兼容旧脚本：等价于 --turns 1
        action="store_true",
        help="模型宣布完成后就结束（默认不结束：报告完继续自己决定下一步，只有 /quit 或轮数上限才停）",
    )
    args = parser.parse_args(argv)
    if args.once:  # 旧脚本兼容：--once 就是只跑一轮
        args.turns = 1

    goal = (args.goal or "").strip()
    if args.goal_file:
        goal = Path(args.goal_file).read_text(encoding="utf-8").strip()
    if not goal:
        # 主播不是"接任务的执行器"：它没有终点目标，只有一个持续的行为准则。
        # 所以 goal 是可选的——不给就用主播默认准则，不该拿它当启动门槛。
        goal = DEFAULT_HOST_GOAL
    if not lithos.LithosConfig.from_env().available():
        print("缺少 API key：设置 LITHOS_API_KEY / DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    try:
        result = run(
            goal,
            max_turns=args.turns,
            urgency=args.urgency,
            log_root=Path(args.log_root),
            api_port=args.port,
            log_file=Path(args.log_file) if args.log_file else None,
            allow_stub_motor=args.allow_stub_motor,
            once=args.once,
            game=args.game,
            restart=args.restart,
        )
    except Exception:  # noqa: BLE001 - 崩溃也要把原因留在窗口与日志里
        traceback.print_exc()
        if args.log_file:
            with open(args.log_file, "a", encoding="utf-8") as handle:
                traceback.print_exc(file=handle)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
