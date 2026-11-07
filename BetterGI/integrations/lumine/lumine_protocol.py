#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lumine 动作文本与提示词协议。

只依赖 Python 标准库，供 AutoDL 推理服务器、Bridge 与测试共用。

动作格式（官方 zlc1004/Lumine 与同目录 PROTOCOL.md）：

    <|action_start|>dX dY dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>
    <|action_start|><use_absolute> X Y dZ ; K1 ; ... ; K6<|action_end|>

可选思考块：

    <|thought_start|>...<|thought_end|>

每个动作覆盖 200ms，由 6 个 33ms 键块组成；每个键块最多 4 个键。
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------
# 协议常量
# ----------------------------------------------------------------------

ACTION_START = "<|action_start|>"
ACTION_END = "<|action_end|>"
THOUGHT_START = "<|thought_start|>"
THOUGHT_END = "<|thought_end|>"
ABSOLUTE_TOKEN = "<use_absolute>"

ACTION_RE = re.compile(
    re.escape(ACTION_START) + r"(.*?)" + re.escape(ACTION_END), re.DOTALL
)
THOUGHT_RE = re.compile(
    re.escape(THOUGHT_START) + r"(.*?)" + re.escape(THOUGHT_END), re.DOTALL
)
_INT_RE = re.compile(r"^[+-]?\d+$")

MAX_CHUNKS = 6
MAX_KEYS_PER_CHUNK = 4

# 相对鼠标位移与滚轮的解析范围（PROTOCOL.md 与训练格式）。
MAX_RELATIVE_MOVE = 1000
MAX_SCROLL = 5

# 1280x720 输出帧中的绝对光标范围。
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
MAX_ABSOLUTE_X = FRAME_WIDTH - 1
MAX_ABSOLUTE_Y = FRAME_HEIGHT - 1

# 与官方 KeyRecorder / BetterGI LiveKeyTokenMap 一致的键 token。
LETTER_KEYS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
DIGIT_KEYS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
FUNCTION_KEYS = (
    "One",
    "Two",
    "Three",
    "Four",
    "Five",
    "Six",
    "Seven",
    "Eight",
    "Nine",
    "Ten",
    "Eleven",
    "Twelve",
)
NAMED_KEYS = ("Esc", "Tab", "Caps", "Shift", "Ctrl", "Alt", "Space")
MOUSE_KEYS = ("LB", "RB", "MB", "XB1", "XB2")

KEY_TOKENS = frozenset(LETTER_KEYS + DIGIT_KEYS + FUNCTION_KEYS + NAMED_KEYS + MOUSE_KEYS)
_NAMED_KEY_BY_LOWER = {name.lower(): name for name in NAMED_KEYS}
_MOUSE_KEY_BY_UPPER = {name.upper(): name for name in MOUSE_KEYS}
_DIGIT_SET = frozenset(DIGIT_KEYS)
_FUNCTION_SET = frozenset(FUNCTION_KEYS)

PROMPT_MODES = ("pretrain", "instruct", "motor")


def motor_attribution(message: Any) -> Optional[Dict[str, Any]]:
    """校验 v2 归属；generation 必须是非负 JSON 整数，不能是布尔值。"""
    if not isinstance(message, dict):
        return None
    intent_id = message.get("intentId")
    generation = message.get("motorGeneration")
    group = message.get("actionGroupId")
    if (not isinstance(intent_id, str) or not intent_id.strip()
            or intent_id != intent_id.strip() or len(intent_id) > 80
            or isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0):
        return None
    if "actionGroupId" in message and (not isinstance(group, str) or not group.strip()
                                      or group != group.strip() or len(group) > 128):
        return None
    result = {"intentId": intent_id, "motorGeneration": generation}
    if group is not None:
        result["actionGroupId"] = group
    return result

MOTOR_SYSTEM_PROMPT = """You are the MotorController for a persistent player agent. You do NOT choose goals, quests, routes, dialogue options, or what to explore. You only convert the supplied intent into the next 200 ms action for the character.

Rules:
- The supplied intent is authoritative. Do not replace it with your own plan.
- Guidance is only a constraint or hint for executing the intent; it is not a new goal.
- If the intent is already satisfied, cannot be executed safely, or the frame is unclear, output a safe idle action (all key chunks empty) and a short thought saying why. Never invent progress.
- Prefer small, continuous movements and keep camera control smooth.
- Do not narrate story, make lore claims, or decide what the player should do next.
- In wait or dialogue mode output only idle. Dialogue advance and option selection belong to the host's deterministic controller; never choose an option or emit keys or clicks for dialogue.

Output format:
<|action_start|>dX dY dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>
- dX/dY: relative mouse movement in the 1280x720 frame, integers in [-1000, 1000].
- dZ: wheel steps in [-5, 5].
- Menu/UI clicks: <|action_start|><use_absolute> X Y dZ ; ...<|action_end|>, X in [0,1279], Y in [0,719].
- Six 33 ms chunks, up to four keys each; repeated empty chunks are allowed.
- Key tokens: A-Z, zero-nine, One-Twelve, Esc, Tab, Caps, Shift, Ctrl, Alt, Space, LB, RB, MB, XB1, XB2.
- Optionally prefix a short <|thought_start|>...<|thought_end|> block.
"""


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------

PRETRAIN_SYSTEM_PROMPT = """You are playing Genshin Impact from the current game frame.
Predict the next 200 ms action (six 33 ms chunks) for the character.

Reply with exactly one action block:
<|action_start|>dX dY dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>

- dX dY: relative mouse movement in pixels, integers clamped to [-1000, 1000].
- dZ: mouse wheel steps, integer clamped to [-5, 5].
- For menus or UI, use absolute mode instead:
  <|action_start|><use_absolute> X Y dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>
  where X is in [0, 1279] and Y is in [0, 719].
- Each of the six chunks lasts 33 ms and holds up to four keys; leave unused chunks empty.
- Key tokens: A-Z, zero-nine, One-Twelve, Esc, Tab, Caps, Shift, Ctrl, Alt, Space, LB, RB, MB, XB1, XB2.
- You may put a short <|thought_start|>...<|thought_end|> block before the action.

Output only the optional thought and the action block."""

INSTRUCT_SYSTEM_PROMPT = """You are an expert Genshin Impact player controlling a character from a 1280x720 game frame.
Decide the best action for the next 200 ms and express it as a Lumine action block.

Output format:
<|action_start|>dX dY dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>

Movement:
- dX and dY are relative mouse movements in the 1280x720 frame, integers clamped to [-1000, 1000]. Use them to turn the camera smoothly: small values for fine aim, larger values for fast turns.
- dZ is the wheel amount, integer clamped to [-5, 5]; use it only when the interface expects scrolling.
- When interacting with a menu or clicking a point on screen, use absolute mode:
  <|action_start|><use_absolute> X Y dZ ; K1 ; K2 ; K3 ; K4 ; K5 ; K6<|action_end|>
  where X is in [0, 1279] and Y is in [0, 719].

Keys:
- Six key chunks cover six 33 ms slices of the 200 ms action. Each chunk may hold at most four keys. Leave a chunk empty when no key changes are needed.
- Tokens: A-Z, zero-nine (digits 0-9), One-Twelve (F1-F12), Esc, Tab, Caps, Shift, Ctrl, Alt, Space, LB, RB, MB, XB1, XB2.
- Hold keys across chunks when the action continues. Use W/A/S/D to move, Space to jump, LB to attack, E/Q for skills, and so on.

Guidance:
- Follow the provided guidance when it is consistent with what is visible.
- Do not invent game state. If the frame is unclear, prefer a safe idle action.
- You may include a short <|thought_start|>...<|thought_end|> block before the action.

Reply with the optional thought block and exactly one action block, and nothing else."""

_SYSTEM_PROMPTS = {
    "pretrain": PRETRAIN_SYSTEM_PROMPT,
    "instruct": INSTRUCT_SYSTEM_PROMPT,
    "motor": MOTOR_SYSTEM_PROMPT,
}


def format_intent(intent: Optional[Dict[str, Any]]) -> str:
    """把 MotorIntent 渲染成 Motor 提示词中的简短结构化文本。"""
    if not isinstance(intent, dict):
        return "No active intent. Output a safe idle action."
    lines = [
        "Intent ID: {0}".format(intent.get("intent_id") or "unknown"),
        "Goal: {0}".format(intent.get("goal") or ""),
        "Motor mode: {0}".format(intent.get("motor_mode") or "navigate"),
        "Deadline: {0} ms".format(intent.get("deadline_ms") or 15000),
    ]
    if intent.get("reason"):
        lines.append("Reason: {0}".format(intent["reason"]))
    constraints = intent.get("constraints")
    if isinstance(constraints, list) and constraints:
        lines.append("Constraints: " + "；".join(str(item) for item in constraints))
    permissions = intent.get("dialogue_permissions")
    if isinstance(permissions, dict):
        lines.append("Dialogue permissions: " + json.dumps(permissions, ensure_ascii=False, separators=(",", ":")))
    target = intent.get("target")
    if isinstance(target, dict) and target:
        lines.append("Target: " + json.dumps(target, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def build_messages(
    guidance: Optional[str] = "",
    mode: str = "pretrain",
    intent: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """构造 OpenAI Chat Completions 的 system/user 文本消息。

    返回的消息只含文本；服务器在发送 vLLM 前调用 :func:`append_image`
    把当前帧作为 ``image_url`` 追加到最后一条 user 消息。

    :param guidance: 可选指导文本（中文或英文均可）。
    :param mode: ``pretrain``/``instruct`` 让模型自行选择动作；``motor`` 只执行 intent。
    :param intent: ``motor`` 模式的 MotorIntent；缺失时输出安全 idle。
    :raises ValueError: mode 不是已知值。
    """
    if mode not in _SYSTEM_PROMPTS:
        raise ValueError(
            "unknown prompt mode {0!r}; expected one of {1}".format(mode, ", ".join(PROMPT_MODES))
        )

    text = (guidance or "").strip()
    if mode == "motor":
        user_lines = [
            "Execute only this intent for the attached game frame:",
            format_intent(intent),
        ]
        if text:
            user_lines.extend(["", "Guidance (constraint/hint only, not a new goal): " + text])
        user_lines.extend(
            ["", "Reply with the optional thought block and exactly one action block."]
        )
    elif mode == "instruct":
        user_lines = ["Observe the attached 1280x720 game frame and choose the next 200 ms action."]
    else:
        user_lines = ["Output the next 200 ms action for the attached game frame."]
    if text and mode != "motor":
        user_lines.extend(["", "Guidance: " + text])
    if mode == "instruct":
        user_lines.extend(
            [
                "",
                "Reply with the optional thought block and exactly one action block.",
            ]
        )

    return [
        {"role": "system", "content": _SYSTEM_PROMPTS[mode]},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def build_motor_messages(
    intent: Optional[Dict[str, Any]], guidance: str = ""
) -> List[Dict[str, Any]]:
    """MotorController 专用入口；无 intent 时仍返回安全 idle 提示。"""
    return build_messages(guidance, "motor", intent=intent)


def append_image(messages: List[Dict[str, Any]], jpeg_base64: str) -> List[Dict[str, Any]]:
    """把 JPEG（base64）作为 image_url 追加到最后一条 user 消息。

    直接修改并返回 ``messages``。若传入的是完整的 ``data:image/jpeg;base64,...``
    URL，则原样使用。
    """
    if not messages:
        raise ValueError("messages must not be empty")
    if not isinstance(jpeg_base64, str) or not jpeg_base64.strip():
        raise ValueError("jpeg_base64 must be a non-empty string")

    payload = jpeg_base64.strip()
    if not payload.startswith("data:"):
        payload = "data:image/jpeg;base64," + payload

    user = messages[-1]
    content = user.get("content")
    image_part = {"type": "image_url", "image_url": {"url": payload}}
    if isinstance(content, str):
        user["content"] = [{"type": "text", "text": content}, image_part]
    elif isinstance(content, list):
        content.append(image_part)
    else:
        raise ValueError("last message content must be a string or a list")
    return messages


def build_image_only_messages(image_url: str) -> List[Dict[str, Any]]:
    """构造 stage-1 训练格式的消息：只有一条纯图像 user 消息。

    官方 Runner（``Runner/client.go`` 的 ``Infer``）注释写明 stage-1 训练
    只有一条 image-only user 消息，不含 system、指令或历史；本函数就是那条
    消息，任何额外文本都会把输入推到训练分布之外。

    :param image_url: 形如 ``data:image/png;base64,...`` 的图像 URL。
    :raises ValueError: URL 为空。
    """
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("image_url must be a non-empty string")
    return [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url.strip()}}
            ],
        }
    ]


def build_chunk_example() -> str:
    """返回一个可直接解析的示例动作（向前走）。"""
    return (
        ACTION_START
        + "5 0 0 ; W ; W ; W ; W ; W ; W"
        + ACTION_END
    )


# ----------------------------------------------------------------------
# 动作解析
# ----------------------------------------------------------------------


def _clamp(value: int, lower: int, upper: int) -> int:
    """把整数限制在闭区间内。"""
    return max(lower, min(upper, value))


def _parse_int(token: str) -> int:
    """解析严格整数 token；非整数抛出 ValueError。"""
    if not _INT_RE.match(token):
        raise ValueError("not an integer: {0!r}".format(token))
    return int(token)


def _normalize_key(token: str) -> Optional[str]:
    """把模型输出的键 token 规范化为官方写法；未知 token 返回 None。"""
    if not token:
        return None
    upper = token.upper()
    if upper in _MOUSE_KEY_BY_UPPER:
        return _MOUSE_KEY_BY_UPPER[upper]
    # 单字母不区分大小写，统一输出大写。
    if len(token) == 1 and "A" <= upper <= "Z":
        return upper
    # 数字 token 必须小写，功能键必须首字母大写（否则 One 与 one 会混淆）。
    if token in _DIGIT_SET or token in _FUNCTION_SET:
        return token
    return _NAMED_KEY_BY_LOWER.get(token.lower())


def _parse_prefix(prefix: str) -> Tuple[bool, int, int, int]:
    """解析移动前缀，返回 (是否绝对, x, y, scroll)。

    相对模式：``dX dY dZ``；绝对模式：``<use_absolute> X Y dZ``。
    结构不合法时抛出 ValueError。
    """
    body = prefix.strip()
    absolute = False
    if body.startswith(ABSOLUTE_TOKEN):
        absolute = True
        body = body[len(ABSOLUTE_TOKEN):].strip()

    parts = body.split()
    # 模型偶尔整帧只给空键块，没有移动前缀；按原地不动处理。
    if not parts and not absolute:
        return False, 0, 0, 0
    # 有的 Lumine 输出会省略为 0 的滚轮值，只给 ``dX dY``；补 0 兼容。
    if len(parts) == 2 and not absolute:
        parts.append("0")
    if len(parts) != 3:
        raise ValueError(
            "{0} movement prefix must contain exactly three integers: {1!r}".format(
                "absolute" if absolute else "relative", prefix.strip()
            )
        )

    x, y, scroll = (_parse_int(part) for part in parts)
    scroll = _clamp(scroll, -MAX_SCROLL, MAX_SCROLL)
    if absolute:
        return True, _clamp(x, 0, MAX_ABSOLUTE_X), _clamp(y, 0, MAX_ABSOLUTE_Y), scroll
    return (
        False,
        _clamp(x, -MAX_RELATIVE_MOVE, MAX_RELATIVE_MOVE),
        _clamp(y, -MAX_RELATIVE_MOVE, MAX_RELATIVE_MOVE),
        scroll,
    )


def _parse_keys(segment: str) -> List[str]:
    """解析一个键块，忽略未知 token，去重并最多保留 4 个键。"""
    keys: List[str] = []
    seen = set()
    for token in segment.split():
        key = _normalize_key(token)
        if key is None:
            continue
        marker = key.lower()
        if marker in seen:
            continue
        seen.add(marker)
        keys.append(key)
        if len(keys) >= MAX_KEYS_PER_CHUNK:
            break
    return keys


def _find_action(text: str) -> "re.Match[str]":
    """优先在思考块之外查找最后一个动作块，找不到再查全文。"""
    without_thought = THOUGHT_RE.sub(" ", text)
    matches = list(ACTION_RE.finditer(without_thought))
    if not matches:
        matches = list(ACTION_RE.finditer(text))
    if not matches:
        raise ValueError("missing action block")
    return matches[-1]


def parse_action(text: str) -> Dict[str, Any]:
    """解析 Lumine 模型输出。

    返回::

        {
            "action": "<|action_start|>...<|action_end|>",
            "chunks": [{"dx": int, "dy": int, "scroll": int, "keys": [...]}, ...],
            "absolute": {"x": int, "y": int} | None,
            "thought": str | None,
            "raw": 原始文本,
            "error": None,
        }

    ``chunks`` 固定补齐为 6 个键块。一个 200ms 动作只有一个移动/滚轮前缀，
    因此按协议把 ``dx``/``dy``/``scroll`` 放在第一个键块，其余键块位移为零；
    绝对模式时 ``dx``/``dy`` 为 0，坐标放在 ``absolute``。

    结构错误（缺少动作块、移动前缀不是三个整数）抛出 ValueError；
    未知键 token 会被忽略，超过 4 个的键会被截断。
    """
    if not isinstance(text, str):
        raise ValueError("action text must be a string")
    if not text.strip():
        raise ValueError("missing action block")

    original_text = text
    text = text.strip()
    # 模型偶尔会在动作体前输出对话角色名（"assistant"）或重复一个
    # <|action_end|>；两者都不是动作内容，先剥掉再解析。
    while True:
        text = text.lstrip()
        if text.startswith(ACTION_END):
            text = text[len(ACTION_END):]
            continue
        role = re.match(r"(?:assistant|model|ai)\b[ \t]*\n?", text, re.IGNORECASE)
        if role is not None and role.end() > 0:
            text = text[role.end():]
            continue
        break

    # vLLM 的 detokenizer 有时会把 <|action_start|> 当特殊 token 丢掉，
    # 只剩动作体 + <|action_end|>。补回起始标记后再解析，raw 仍保留原样。
    if ACTION_END in text and ACTION_START not in text:
        thought_end = text.rfind(THOUGHT_END)
        insert_at = thought_end + len(THOUGHT_END) if thought_end >= 0 else 0
        text = text[:insert_at] + ACTION_START + text[insert_at:]

    match = _find_action(text)
    action_text = match.group(0).strip()
    body = match.group(1).strip()
    if not body:
        raise ValueError("action block is empty")

    segments = body.split(";")
    absolute, x, y, scroll = _parse_prefix(segments[0])

    key_segments = segments[1:]
    if len(key_segments) > MAX_CHUNKS:
        key_segments = key_segments[:MAX_CHUNKS]

    def make_chunk(index: int, keys: List[str]) -> Dict[str, Any]:
        """只有第一个键块携带移动/滚轮前缀，后续键块位移为零。"""
        first = index == 0
        return {
            "dx": (0 if absolute else x) if first else 0,
            "dy": (0 if absolute else y) if first else 0,
            "scroll": scroll if first else 0,
            "keys": keys,
        }

    chunks: List[Dict[str, Any]] = []
    for index, segment in enumerate(key_segments):
        chunks.append(make_chunk(index, _parse_keys(segment)))
    while len(chunks) < MAX_CHUNKS:
        chunks.append(make_chunk(len(chunks), []))

    thought_match = THOUGHT_RE.search(text)
    thought = None
    if thought_match is not None:
        thought = thought_match.group(1).strip() or None

    return {
        "action": action_text,
        "chunks": chunks,
        "absolute": {"x": x, "y": y} if absolute else None,
        "thought": thought,
        "raw": original_text,
        "error": None,
    }


def format_chunks(
    chunks: Sequence[Dict[str, Any]],
    absolute: Optional[Dict[str, int]] = None,
) -> str:
    """把结构化键块渲染回 Lumine 动作文本（主要用于调试与测试）。

    取第一个键块的 ``dx``/``dy``/``scroll`` 作为移动前缀；与
    :func:`parse_action` 把前缀放在首块的行为对应。
    """
    first = chunks[0] if chunks else {"dx": 0, "dy": 0, "scroll": 0}
    scroll = int(first.get("scroll", 0) or 0)
    if absolute is not None:
        movement = "{0} {1} {2}".format(
            ABSOLUTE_TOKEN, int(absolute.get("x", 0)), int(absolute.get("y", 0))
        )
    else:
        movement = "{0} {1}".format(int(first.get("dx", 0) or 0), int(first.get("dy", 0) or 0))
    key_parts = []
    for chunk in list(chunks)[:MAX_CHUNKS]:
        keys = chunk.get("keys") or []
        key_parts.append(" ".join(str(key) for key in keys[:MAX_KEYS_PER_CHUNK]))
    return "{0}{1} {2} ; {3}{4}".format(
        ACTION_START, movement, scroll, " ; ".join(key_parts), ACTION_END
    )
