#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 shadow 玩家构造提示词（Phase 1 的最小 ContextBuilder）。

Phase 2 会把它替换成带预算的 ContextBuilder；这里只保证：
- 人格摘要稳定、与时间无关；
- 只注入玩家已经看到/被告知的内容，不接入世界数据库或剧透资料；
- 不把裸数值、内部路径、令牌写进提示词。
"""

from __future__ import annotations

from typing import Any, Dict, List

from player_profile import PlayerProfile

# 这段约束是 shadow 模式的硬边界：只记录，不执行，不冒充执行者。
SHADOW_RULES = """此刻你正在亲自游玩《原神》，以第一人称体验；你不是解说员，也不是旁白。
当前运行模式是 shadow（影子）：你照常观察、思考、计划、产生想说的话，但你的动作不会被发送到游戏，画面不会因为你的决定而改变。
画面里的角色由当前执行器操作，不要把它当成你刚刚做出的决定；也不要假设自己的提议已经执行。
只记录你真正看到、想到、想做的事；不确定就保持不确定，不要编造未看到的信息。
不剧透自己还没在游戏里经历过的剧情；不要引用百科、世界数据库或任何「上帝视角」资料。
用简体中文，句子短；口头禅自然出现，不要每句都带。"""

PROPOSAL_SCHEMA = """{
  "observation": "一句话：我现在看到了什么",
  "thought": "内心想法，只给自己看",
  "intention": {"goal": "我接下来想做什么", "reason": "为什么", "horizon": "now|soon|session", "motor_mode": "navigate|combat|interact|explore|ui|wait|dialogue", "target": {}, "constraints": ["执行限制"], "interrupt_conditions": ["中断条件"], "deadline_ms": 15000},
  "dialogue_decision": {"action": "wait", "frame_id": "观察提供的原始帧标识"},
  "speech": {"text": "如果现在开口会说什么", "emotion": "calm|curious|excited|happy|worried|nervous|sad|angry|surprised|thinking|tired|determined", "intensity": 0.0, "should_speak": true},
  "proposed_actions": [{"hint": "walk|run|look|interact|attack|jump|open_map|wait|use_item|talk", "detail": "具体想怎么做", "reason": "为什么这样做"}],
  "memory_note": {"scene": "当前场景", "quest": "当前任务/目标", "characters": ["在场角色"], "what_happened": "刚发生了什么", "what_i_knew_before": "做这件事之前我知道什么", "what_i_learned": "这次的主观印象（未核实）", "what_i_felt": {"curiosity": 0.6}, "things_to_check": ["之后想确认的事"], "importance": 0.5, "learned_facts": [{"proposition": "候选印象，不是确定事实"}]},
  "beliefs": [{"proposition": "我关于剧情的猜测", "status": "hypothesis", "confidence": 0.5, "evidence": ["证据"]}],
  "relationships": [{"character": "角色名", "trust": 0.0, "affection": 0.0, "suspicion": 0.0, "memorable_events": ["事件"]}],
  "emotion": {"curiosity": 0.4, "tension": 0.2},
  "threads": [{"title": "未解决的线索", "status": "open", "importance": 0.5, "current_theory": "我的猜测"}],
  "experience_note": {"text": "本轮经历的主观归纳", "derivation": "如何从看到的内容推断", "confidence": 0.5},
  "confidence": 0.5
}
对话决策仅是提案，不代表执行或选项已选中；无可靠同帧观察时省略 dialogue_decision。
action 只能是 wait、advance 或 select；frame_id 必须原样引用当前观察提供的帧标识。
select 必须额外提供 option_id，且只能原样引用该帧提供的选项标识；wait/advance 不得提供 option_id。
不得添加坐标、额外字段或自行编造标识。"""


def _one_line(value: Any, limit: int = 300) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _format_commentary(items: Any) -> str:
    if not isinstance(items, (list, tuple)) or not items:
        return "（还没有）"
    lines: List[str] = []
    for item in list(items)[-3:]:
        if not isinstance(item, dict):
            continue
        kind = _one_line(item.get("kind"), 20) or "commentary"
        text = _one_line(item.get("text"), 160)
        if text:
            lines.append("- [{0}] {1}".format(kind, text))
    return "\n".join(lines) or "（还没有）"


def _format_actions(items: Any) -> str:
    if not isinstance(items, (list, tuple)) or not items:
        return "（还没有）"
    lines: List[str] = []
    for item in list(items)[-3:]:
        if not isinstance(item, dict):
            continue
        keys = item.get("keys")
        key_text = "、".join(str(key) for key in keys[:6]) if isinstance(keys, list) else ""
        status = _one_line(item.get("status"), 20)
        parts = [part for part in ("按键 " + key_text if key_text else "", status) if part]
        if parts:
            lines.append("- " + "；".join(parts))
    return "\n".join(lines) or "（还没有）"


def _format_action_state(state: Any) -> str:
    if not isinstance(state, dict) or not state:
        return "（还未知）"
    keys = state.get("keys")
    key_text = "、".join(str(key) for key in keys[:8]) if isinstance(keys, list) else "无"
    parts = ["当前按键：" + key_text]
    if state.get("queueDepth") is not None:
        parts.append("队列深度：{0}".format(state.get("queueDepth")))
    if state.get("starved") is not None:
        parts.append("是否饥饿：{0}".format("是" if state.get("starved") else "否"))
    return "；".join(parts)


def _resume_block(state: Any) -> str:
    """只摘取对「接着上次」有用的事实，避免把整份状态倒进 prompt。"""
    if not isinstance(state, dict):
        return ""
    fields = (
        ("last_internal_thought", "上次留下的想法"),
        ("current_intention", "上次想做的事"),
        ("last_location", "上次所在场景"),
        ("current_quest", "上次的任务"),
    )
    lines: List[str] = []
    for key, label in fields:
        text = _one_line(state.get(key), 200)
        if text:
            lines.append("- {0}：{1}".format(label, text))
    checks = state.get("things_i_want_to_check_next")
    if isinstance(checks, (list, tuple)):
        items = [_one_line(item, 100) for item in checks[:3]]
        items = [item for item in items if item]
        if items:
            lines.append("- 想确认的事：{0}".format("；".join(items)))
    return "\n".join(lines)


def build_shadow_prompt(
    profile: PlayerProfile,
    state: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Dict[str, str]:
    """构造 ``{system, user}``；相同输入必须得到相同文本。"""
    system = profile.stable_summary() + "\n\n" + SHADOW_RULES

    snapshot = snapshot if isinstance(snapshot, dict) else {}
    frame = snapshot.get("frame")
    frame = frame if isinstance(frame, dict) else {}
    guidance = _one_line(snapshot.get("guidance"), 240)
    guidance_source = _one_line(snapshot.get("guidance_source"), 40)
    ui_category = _one_line(
        frame.get("ui_category") or snapshot.get("ui_category"), 60
    )

    observation_lines = [
        "【当前观察】",
        "- 帧序号：{0}".format(frame.get("seq", "未知")),
        "- 画面尺寸：{0}×{1}".format(
            frame.get("width") or "未知", frame.get("height") or "未知"
        ),
        "- UI 类别：{0}".format(ui_category or "未知"),
        "- 实时会话：{0}".format("进行中" if snapshot.get("live_running") else "未运行"),
        "- 外部指导（来自当前导演，不是你的决定）：{0}".format(
            guidance or "（没有）"
        ),
    ]
    if guidance_source:
        observation_lines.append("- 指导来源：{0}".format(guidance_source))
    observation_lines.extend(
        [
            "",
            "【执行器状态】",
            "- {0}".format(_format_action_state(snapshot.get("action_state"))),
            "- 最近动作回显：",
            _format_actions(snapshot.get("recent_actions")),
            "",
            "【最近解说】",
            _format_commentary(snapshot.get("commentary")),
        ]
    )

    resume = _resume_block(state)
    if resume:
        observation_lines.extend(["", "【你上一次留下的状态】", resume])

    observation_lines.extend(
        [
            "",
            "【输出要求】",
            "只输出一个 JSON 对象，不要 markdown、不要额外解释。字段如下（没有内容的字段可以留空或省略）：",
            PROPOSAL_SCHEMA,
        ]
    )
    return {"system": system, "user": "\n".join(observation_lines)}
