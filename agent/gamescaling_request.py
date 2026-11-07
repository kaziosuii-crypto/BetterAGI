# -*- coding: utf-8 -*-
'''GameScaling 请求体（README 第 4 节）：多轮历史 + 前置填充，跟训练格式逐字对齐。

README 把这条格式写死了，任何一处走样都会让模型退化：

    system     <系统提示词>
    user       "current game screen" + 帧 t-k
    assistant  <t-k 的动作，无思考>
    ...        （最近 history_len 轮，默认 5）
    user       "current game screen" + 当前帧
    assistant  前置填充：开口 think / 空 think 块

硬约束（README §1 Things to keep as shipped，逐条对过官方 policy.py:129-144）：
* skip_special_tokens=false —— 否则 action_start / action_end 会被剥掉；
* 必须给 assistant 前置填充 —— 不给就等于让模型从空处续写；
* continue_final_message=true + add_generation_prompt=false —— 前置填充是「续写」不是
  「新一轮对话」，少了 add_generation_prompt=false 服务端会再插一个 assistant 头；
* temperature=1.0、max_tokens=2048、stop=[action_end]、帧 JPEG q90 原尺寸。

历史轮里失败的决策要存成显式 no-op，不能存原始坏输出，否则模型会照着学坏。
'''
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from gamescaling_action import (
    ACTION_END,
    ACTION_START,
    NOOP_ACTION_TEXT,
    THINK_CLOSE,
    THINK_OPEN,
)

#: 用户轮固定文本。README 原文，别翻译、别加料。
USER_TEXT = "current game screen"
#: 动作块起止标签（特殊 token，请求必须 skip_special_tokens=false）。
ACTION_OPEN = ACTION_START
ACTION_CLOSE = ACTION_END
#: 没解析出来时写进历史的显式 no-op。
NOOP_ACTION = NOOP_ACTION_TEXT

_NEWLINE = chr(10)
#: 默认前置填充：只开思考标签，让模型自己决定想不想、想多久。
PREFILL_THINK = THINK_OPEN
#: no-reasoning 前置填充：直接把思考块闭合掉，等于让它跳过思考直接出动作。
PREFILL_NO_REASONING = THINK_OPEN + _NEWLINE + _NEWLINE + THINK_CLOSE

#: vLLM 的 OpenAI 兼容端点不校验 key，但官方客户端仍然发 Bearer EMPTY（policy.py:58）。
#: 少这个头在某些网关后会 401，所以照发。
DEFAULT_API_KEY = "EMPTY"


def prefill(reasoning: bool = True) -> str:
    '''当前模式对应的前置填充串。'''
    return PREFILL_THINK if reasoning else PREFILL_NO_REASONING


def headers(api_key: Optional[str] = None) -> Dict[str, str]:
    '''请求头。官方客户端每一项都带 Authorization。'''
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (api_key or DEFAULT_API_KEY),
    }


def user_message(frame_url: str) -> Dict[str, Any]:
    '''一轮 user：固定文本 + 当前帧。'''
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": USER_TEXT},
            {"type": "image_url", "image_url": {"url": frame_url}},
        ],
    }


def assistant_turn(action_text: str, reasoning: str = "") -> Dict[str, Any]:
    '''一轮 assistant：官方 assistant_turn（policy.py:123）。

    历史轮的 history_think=0，所以只发动作串；只有显式要求保留思考时才把思考拼回去。
    '''
    text = (action_text or "").strip()
    if not text or ACTION_START not in text:
        text = NOOP_ACTION
    if reasoning:
        text = THINK_OPEN + _NEWLINE + reasoning.strip() + _NEWLINE + THINK_CLOSE + _NEWLINE + _NEWLINE + text
    return {"role": "assistant", "content": text}


def assistant_action(action: str) -> Dict[str, Any]:
    '''兼容旧调用名：历史轮只放动作，不带思考（history_think=0）。'''
    return assistant_turn(action, "")


def build_messages(
    system_prompt: Optional[str],
    history: Sequence[Dict[str, str]],
    frame_url: str,
    *,
    reasoning: bool = True,
) -> List[Dict[str, Any]]:
    '''组装完整 messages。history 是 [{frame_url, action}]，按时间从旧到新。'''
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for turn in history:
        url = str((turn or {}).get("frame_url") or "")
        if not url:
            continue
        messages.append(user_message(url))
        messages.append(assistant_action(str((turn or {}).get("action") or "")))
    messages.append(user_message(frame_url))
    # 前置填充：assistant 轮必须是最后一条，配合 continue_final_message=True 续写。
    messages.append({"role": "assistant", "content": prefill(reasoning)})
    return messages


def build_request(
    model: str,
    system_prompt: Optional[str],
    history: Sequence[Dict[str, str]],
    frame_url: str,
    *,
    reasoning: bool = True,
    max_tokens: int = 2048,
    temperature: float = 1.0,
) -> Dict[str, Any]:
    '''完整请求体。字段与官方 PolicyConfig / build_request 逐字一致。'''
    return {
        "model": model,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "messages": build_messages(system_prompt, history, frame_url, reasoning=reasoning),
        "stop": [ACTION_CLOSE],
        "skip_special_tokens": False,
        "continue_final_message": True,
        # 官方 policy.py:143 这一行不能少：前置填充是续写最后一条 assistant，
        # 缺了它服务端会认为该开新一轮，模板会多插一层 assistant 头。
        "add_generation_prompt": False,
    }


def history_turn(
    frame_url: str,
    action: str,
    *,
    pressed: Optional[bool] = None,
    step: Optional[int] = None,
    skipped_reason: str = "",
) -> Dict[str, str]:
    '''记一轮历史；action 为空或没解析出来就记显式 no-op。

    pressed 给定时在动作串行尾附一句英文短注（设计 03 §C.4 P1）：手模型需要知道
    "上一步到底生没生效"，才能对吞键自纠。例如：

        <action_start>...<action_end> # step3 skipped: no_window_focus

    不传 pressed 时输出与旧版逐字节一致——旧格式是训练分布的一部分，随便加字等于
    给模型喂噪声。step 是 1 起的步号（可省略），skipped_reason 来自
    play_steps 的 step_results[*].skipped_reason。
    '''
    text = (action or "").strip()
    if not text or ACTION_START not in text:
        text = NOOP_ACTION
    if pressed is not None:
        label = "step%d" % int(step) if step is not None else "step"
        if pressed:
            text = text + " # " + label + " pressed: ok"
        else:
            text = text + " # " + label + " skipped"
            reason = str(skipped_reason or "").strip()
            if reason:
                text = text + ": " + reason
    return {"frame_url": str(frame_url or ""), "action": text}
