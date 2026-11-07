#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BetterGI Live 参考导演 / VTuber 解说循环。

这是 ``dsh`` 的可替换参考实现：连接 BetterGI Live WebSocket，按固定间隔或
显著事件取最新画面，调用 LLM 生成一行指导与多维度解说，再通过 WebSocket /
HTTP 写回 BetterGI。会话记忆通过 BetterGI 的智能体会话 HTTP 接口维护。

只写三类数据：

- 指导（guidance）：``{"type": "guidance", "text": ...}``
- 解说（commentary）：``{"type": "commentary", "kind", "text", "emotion", "frameSeq"}``
- 会话记忆（memory）：``POST /api/v1/agent/sessions/{sessionId}/memory``

不会读写游戏内存、抓包、注入或执行任意代码。LLM 返回的 ``tool_calls`` 只允许
白名单中的五个工具，其余一律拒绝并记录日志。

用法示例::

    set BETTERGI_AGENT_TOKEN=...
    set ANTHROPIC_API_KEY=...
    python integrations/lumine/live_director.py --session-id vtuber
    python integrations/lumine/live_director.py --no-llm        # 只观察/记录
    python integrations/lumine/live_director.py --dry-run       # 打印提示词

依赖 ``aiohttp``；未安装时仍可导入纯函数（单元测试会跳过网络集成用例）。
"""

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from collections import deque

try:
    import aiohttp
except ImportError:  # pragma: no cover - 由运行环境决定
    aiohttp = None

try:
    from .llm_client import LLMClient, LLMError
except ImportError:  # 直接以脚本运行时没有包上下文
    from llm_client import LLMClient, LLMError

try:
    from .memory_bridge import MemoryBridge
except ImportError:  # 直接以脚本运行时没有包上下文
    from memory_bridge import MemoryBridge


DEFAULT_WS_URL = "ws://127.0.0.1:30648/ws/v1/agent/live"
DEFAULT_HTTP_URL = "http://127.0.0.1:30648"
DEFAULT_INTERVAL = 4.0
DEFAULT_SESSION_ID = "vtuber"
DEFAULT_RECONNECT_DELAY = 3.0
DEFAULT_MAX_COMMENTARY_PER_TICK = 4
DEFAULT_MEMORY_LIMIT = 4000
RECENT_COMMENTARY_LIMIT = 8
MAX_COMMENTARY_CHARS = 80
MAX_GUIDANCE_CHARS = 240

LOGGER = logging.getLogger("lumine.director")


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------


def configure_stdio():
    """尽量把标准输出/错误切换到 UTF-8，避免 Windows 控制台中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def truncate(text, limit):
    """按字符截断并附加省略标记。"""
    if not isinstance(text, str):
        text = str(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


def redact_url(url):
    """从日志用 URL 中移除令牌类查询参数。"""
    try:
        parts = urllib.parse.urlsplit(url)
        query = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parts.query, keep_blank_values=True
            )
            if key.lower() not in ("access_token", "token")
        ]
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urllib.parse.urlencode(query),
                parts.fragment,
            )
        )
    except ValueError:
        return "<invalid-url>"


def build_ws_url(ws_url, token):
    """把访问令牌作为 ``access_token`` 查询参数附加到 WS 地址。"""
    if not token:
        return ws_url
    try:
        parts = urllib.parse.urlsplit(ws_url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        if "access_token" not in query and "token" not in query:
            query["access_token"] = token
        return urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urllib.parse.urlencode(query),
                parts.fragment,
            )
        )
    except ValueError:
        separator = "&" if "?" in ws_url else "?"
        return ws_url + separator + urllib.parse.urlencode({"access_token": token})


def _json_preview(value, limit=300):
    """把任意值转成便于日志输出的短 JSON。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return truncate(text, limit)


# ----------------------------------------------------------------------
# LLM 响应 JSON 提取
# ----------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_GENERIC_FENCE_RE = re.compile(r"```[^\n]*\r?\n(.*?)```", re.DOTALL)


def strip_code_fences(text):
    """去掉 Markdown 代码围栏；没有围栏时原样返回。"""
    if not isinstance(text, str):
        return ""
    matches = _FENCE_RE.findall(text)
    if not matches:
        matches = _GENERIC_FENCE_RE.findall(text)
    for candidate in matches:
        if "{" in candidate:
            return candidate.strip()
    if matches:
        return matches[0].strip()
    return text.strip()


def _match_balanced_object(text, start):
    """从 ``text[start]`` 附近的 ``{`` 开始，返回配平的 JSON 对象字符串。

    正确跳过字符串中的花括号和转义字符；对象未闭合时返回 ``None``。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _extract_balanced_object(text):
    """在文本中寻找第一个配平的 JSON 对象。"""
    start = text.find("{")
    while start >= 0:
        candidate = _match_balanced_object(text, start)
        if candidate is not None:
            return candidate
        start = text.find("{", start + 1)
    return None


def _iter_balanced_objects(text):
    """遍历文本中所有配平的 ``{...}`` 对象字符串。"""
    position = 0
    while True:
        start = text.find("{", position)
        if start < 0:
            return
        candidate = _match_balanced_object(text, start)
        if candidate is None:
            position = start + 1
            continue
        yield candidate
        position = start + len(candidate)


def _unescape_json_string(raw):
    """尽最大努力还原 JSON 字符串内容，兼容被截断的转义。"""
    if raw.endswith("\\") and not raw.endswith("\\\\"):
        raw = raw[:-1]
    try:
        return json.loads('"' + raw + '"')
    except (TypeError, ValueError):
        return (
            raw.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def _salvage_string(text, name):
    """用正则从残缺 JSON 中抢救一个字符串字段。"""
    pattern = r'"{0}"\s*:\s*("(?:\\.|[^"\\])*")'.format(re.escape(name))
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return _unescape_json_string(match.group(1)[1:-1])

    # 模型输出被截断在字符串中间：一直取到文本结尾。
    pattern = r'"{0}"\s*:\s*"((?:\\.|[^"\\])*)$'.format(re.escape(name))
    match = re.search(pattern, text, re.DOTALL)
    if match:
        value = _unescape_json_string(match.group(1)).strip()
        return value or None
    return None


def _salvage_object(text, name):
    """用正则从残缺 JSON 中抢救一个对象字段。"""
    match = re.search(r'"{0}"\s*:\s*'.format(re.escape(name)), text, re.DOTALL)
    if not match:
        return None
    start = text.find("{", match.end())
    if start < 0:
        return None
    candidate = _match_balanced_object(text, start)
    if candidate is None:
        return None
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _salvage_commentary(text):
    """从残缺 JSON 中抢救 commentary 数组。"""
    match = re.search(r'"commentary"\s*:\s*\[', text, re.DOTALL)
    if not match:
        return None
    tail = text[match.end() :]
    items = []
    for candidate in _iter_balanced_objects(tail):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and str(value.get("text") or "").strip():
            items.append(value)
        if len(items) >= RECENT_COMMENTARY_LIMIT:
            break
    if items:
        return items

    # 数组里只有一条且对象未闭合：抢救 text/kind/emotion。
    spoken = _salvage_string(tail, "text")
    if spoken:
        return [
            {
                "kind": _salvage_string(tail, "kind") or "reaction",
                "text": spoken,
                "emotion": _salvage_string(tail, "emotion") or "calm",
            }
        ]
    return None


def _salvage_partial(text):
    """从残缺 JSON 中逐字段抢救内容；完全无法解析时返回 ``None``。"""
    result = {}
    thought = _salvage_string(text, "thought")
    guidance = _salvage_string(text, "guidance")
    commentary = _salvage_commentary(text)
    memory_patch = _salvage_object(text, "memory_patch")
    if thought:
        result["thought"] = thought
    if guidance:
        result["guidance"] = guidance
    if commentary:
        result["commentary"] = commentary
    if memory_patch:
        result["memory_patch"] = memory_patch
    return result or None


def extract_director_json(text):
    """把 LLM 文本解析成导演响应字典。

    依次尝试：直接 JSON、代码围栏内容、文本中的第一个配平对象、残缺字段抢救。
    失败返回 ``None``，由调用方记录并跳过本轮。
    """
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return None

    cleaned = strip_code_fences(text)
    candidates = [cleaned]
    balanced = _extract_balanced_object(cleaned)
    if balanced and balanced != cleaned:
        candidates.append(balanced)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value

    return _salvage_partial(cleaned)


# ----------------------------------------------------------------------
# 人格与提示词
# ----------------------------------------------------------------------

DEFAULT_PERSONA = {
    "name": "小光",
    "personality": (
        "元气、好奇，对提瓦特的一切都感兴趣；遇到危险会紧张但不会慌乱，"
        "喜欢和观众一起分析 AI 队友的操作。"
    ),
    "tone": "轻松活泼，短句为主，偶尔善意吐槽",
    "language": "简体中文",
    "catchphrases": ["咦？", "等一下——", "这波可以！"],
    "content_dimensions": {
        "reaction": "对画面中突然出现的事件做即时反应",
        "plan": "用一句话点评当前路线或战斗策略",
        "emotion": "表达紧张、期待、开心等情绪",
        "lore": "补充一句不剧透的提瓦特世界观背景",
        "chat": "和观众互动，回应想象中的弹幕",
        "result": "复盘刚才操作的结果，指出成功或失误",
    },
}

RESPONSE_SCHEMA = (
    '{"thought":"内部推理，不会朗读",'
    '"guidance":"一行给下游玩家模型的可执行指导",'
    '"commentary":[{"kind":"reaction|plan|emotion|lore|chat|result",'
    '"text":"要朗读的台词","emotion":"calm|excited|surprised|worried|happy"}],'
    '"memory_patch":{"键":"值"},'
    '"tool_calls":[{"name":"bgi_agent_memory_set",'
    '"arguments":{"values":{"键":"值"}}}]}'
)

TOOL_SPECS = {
    "bgi_agent_memory_set": '{"values":{"键":"值"}}，合并写入 BetterGI 会话记忆。',
    "bgi_agent_memory_get": '{"key":"可选键名"}，读取会话记忆。',
    "bgi_agent_checkpoint_create": '{"name":"名称","note":"说明"}，创建会话检查点。',
    "bgi_live_status": "{}，读取 BetterGI Live 状态。",
    "bgi_live_set_guidance": '{"text":"一行指导"}，设置当前指导。',
}
ALLOWED_TOOL_NAMES = frozenset(TOOL_SPECS)

ALLOWED_COMMENTARY_KINDS = (
    "reaction",
    "plan",
    "emotion",
    "lore",
    "chat",
    "result",
)
ALLOWED_EMOTIONS = ("calm", "excited", "surprised", "worried", "happy")


def _persona_value(persona, key, default=None):
    """读取人格字段，空值时回退到默认人格。"""
    if isinstance(persona, dict):
        value = persona.get(key)
        if value not in (None, "", [], {}):
            return value
    return DEFAULT_PERSONA.get(key, default)


def _render_dimensions(persona):
    """把 content_dimensions 字典或列表渲染成提示词条目。"""
    dimensions = _persona_value(persona, "content_dimensions", {})
    if isinstance(dimensions, dict):
        lines = []
        for key, description in dimensions.items():
            lines.append("- {0}：{1}".format(key, description))
        return "\n".join(lines) or "- reaction：即时反应"
    if isinstance(dimensions, (list, tuple)):
        return "、".join(str(item) for item in dimensions)
    return str(dimensions)


def _render_catchphrases(persona):
    """渲染口头禅。"""
    phrases = _persona_value(persona, "catchphrases", [])
    if isinstance(phrases, (list, tuple)):
        return "、".join(str(item) for item in phrases if str(item).strip())
    return str(phrases)


def _format_recent_commentary(commentary):
    """把最近的解说事件格式化为提示词列表。"""
    lines = []
    for item in commentary:
        if isinstance(item, dict):
            kind = str(item.get("kind") or "reaction")
            text = str(item.get("text") or "").strip()
        else:
            kind = "reaction"
            text = str(item).strip()
        if text:
            lines.append("- [{0}] {1}".format(kind, text))
    return "\n".join(lines) if lines else "（暂无）"


def build_prompt(
    persona,
    guidance="",
    commentary=None,
    action_state=None,
    memory=None,
    frame_seq=None,
    memory_limit=DEFAULT_MEMORY_LIMIT,
    recent_limit=RECENT_COMMENTARY_LIMIT,
    short_term="",
    long_term="",
):
    """构建导演的系统提示与用户提示。

    返回 ``{"system": ..., "user": ...}``。图像不在这里拼接，由 LLM 客户端
    作为单独的多模态内容块附加。
    """
    limit = int(recent_limit)
    recent = list(commentary or [])
    if limit > 0:
        recent = recent[-limit:]
    else:
        recent = []
    name = str(_persona_value(persona, "name", "小光"))
    personality = str(_persona_value(persona, "personality", ""))
    tone = str(_persona_value(persona, "tone", ""))
    language = str(_persona_value(persona, "language", "简体中文"))

    tool_lines = "\n".join(
        "- {0}：{1}".format(tool_name, description)
        for tool_name, description in TOOL_SPECS.items()
    )

    system = """你是《原神》实况 VTuber「{name}」，正和观众一起观看一位 AI 玩家操作游戏。
人格：{personality}
语气：{tone}
语言：{language}
口头禅（偶尔自然使用，不要每句重复）：{catchphrases}

可以使用的解说维度：
{dimensions}

工作方式：
- 始终以第一人称、以 VTuber 身份对观众说话。
- 内部推理写在 thought；真正要朗读的内容写在 commentary[].text，两者严格分开。
- 每条朗读台词不超过 80 个中文字符，适合做字幕；一句话只表达一个意思。
- 解说要多维度轮换：即时反应、战术计划、情绪、世界观、观众互动、结果复盘，避免连续重复同一种。
- 可以引用已经出现的地区、角色、元素和事件，但不剧透尚未到达的剧情。
- 不提供作弊、外挂、读内存、抓包或绕过游戏限制的方法；不引导高风险或不可逆操作。
- guidance 是给下游玩家模型的一行可执行指导，必须具体、简短、能立刻执行。
- 记忆分两层：短期记忆是最近事件与场景卡，长期记忆是设定与往期经历；解说和指导要与两层记忆保持一致。
- memory_patch 中维护 scene（当前地点/任务/威胁，≤80字）、plot（最近剧情进展，≤120字）、objective（当前目标，≤60字）；每轮尽量更新，已经确认的设定不要删除。
- 如果没有值得说的内容，commentary 可以是空数组，不要为了说话而重复上一轮。

输出要求：
- 只输出一个 JSON 对象，不要 Markdown 代码围栏，不要额外解释。
- JSON 结构：{schema}
- guidance 变化时才需要给出新的指导；没有变化可以给空字符串。
- 可调用的工具（name 必须完全一致，其他名称会被拒绝）：
{tools}""".format(
        name=name,
        personality=personality,
        tone=tone,
        language=language,
        catchphrases=_render_catchphrases(persona),
        dimensions=_render_dimensions(persona),
        schema=RESPONSE_SCHEMA,
        tools=tool_lines,
    )

    memory_json = json.dumps(
        memory or {}, ensure_ascii=False, sort_keys=True, default=str
    )
    if memory_limit and len(memory_json) > int(memory_limit):
        memory_json = memory_json[: int(memory_limit)] + "…（已截断）"
    action_json = json.dumps(
        action_state or {}, ensure_ascii=False, sort_keys=True, default=str
    )

    user = """当前帧序号：{frame_seq}
当前指导：{guidance}
动作状态：{action_state}
短期记忆（最近事件与场景）：
{short_term}
长期记忆（设定/往事，仅作参考）：
{long_term}
最近解说（旧 → 新，最多 {recent_limit} 条）：
{recent_commentary}
关键记忆：{memory}

请结合随附的游戏画面，以 JSON 输出本轮的 thought、guidance、commentary、memory_patch 和 tool_calls。""".format(
        frame_seq="未知" if frame_seq is None else frame_seq,
        guidance=guidance.strip() or "（暂无）",
        action_state=action_json,
        recent_limit=int(recent_limit),
        recent_commentary=_format_recent_commentary(recent),
        short_term=short_term.strip() or "（暂无）",
        long_term=long_term.strip() or "（暂无）",
        memory=memory_json,
    )
    return {"system": system, "user": user}


# ----------------------------------------------------------------------
# 解说 / 指导规范化与事件检测
# ----------------------------------------------------------------------


def normalize_guidance(text, max_chars=MAX_GUIDANCE_CHARS):
    """把指导压成一行，去掉多余空白并限制长度。"""
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[: int(max_chars)].strip()


def normalize_commentary(
    items, frame_seq=None, max_items=DEFAULT_MAX_COMMENTARY_PER_TICK
):
    """校验并规范化 LLM 返回的解说条目。

    - 非法 kind / emotion 回退到 ``reaction`` / ``calm``；
    - 空文本丢弃；
    - 台词截断到 80 个字符，便于字幕；
    - 最多保留 ``max_items`` 条。
    """
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, (list, tuple)):
        return []

    result = []
    for raw in items:
        if isinstance(raw, str):
            raw = {"kind": "reaction", "text": raw}
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        kind = raw.get("kind")
        if kind not in ALLOWED_COMMENTARY_KINDS:
            kind = "reaction"
        emotion = raw.get("emotion")
        if emotion not in ALLOWED_EMOTIONS:
            emotion = "calm"
        if len(text) > MAX_COMMENTARY_CHARS:
            text = text[:MAX_COMMENTARY_CHARS]
        item = {
            "type": "commentary",
            "kind": kind,
            "text": text,
            "emotion": emotion,
        }
        if frame_seq is not None:
            try:
                item["frameSeq"] = int(frame_seq)
            except (TypeError, ValueError):
                pass
        result.append(item)
        if len(result) >= int(max_items):
            break
    return result


def detect_significant_change(
    previous_guidance, new_guidance, previous_keys=None, new_keys=None
):
    """检测值得提前触发导演轮询的廉价事件。

    指导文本变化，或按键状态在“空闲 / 有输入”之间切换（进入战斗、开始移动、
    停下等）都算显著变化。更细的语义判断留给 LLM。
    """
    if (
        isinstance(new_guidance, str)
        and new_guidance.strip()
        and new_guidance != (previous_guidance or "")
    ):
        return True
    return bool(previous_keys) != bool(new_keys)


def filter_tool_calls(tool_calls, logger=None):
    """只保留白名单内的工具调用，拒绝其他名称并记录日志。"""
    log = logger or LOGGER
    if not isinstance(tool_calls, list):
        return []
    accepted = []
    for raw in tool_calls:
        if not isinstance(raw, dict):
            log.warning("忽略格式错误的 tool_call：%r", raw)
            continue
        name = raw.get("name")
        if name not in ALLOWED_TOOL_NAMES:
            log.warning("拒绝未授权工具调用：%r", name)
            continue
        arguments = raw.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            log.warning("忽略参数不是对象的工具调用：%s", name)
            continue
        accepted.append({"name": name, "arguments": dict(arguments)})
    return accepted


def memory_values_from_arguments(arguments):
    """从工具参数中提取要写入的记忆字典。

    兼容三种写法：``{"values": {...}}``、``{"key": ..., "value": ...}`` 以及
    直接把键值放在参数顶层。
    """
    if not isinstance(arguments, dict):
        return {}
    values = arguments.get("values")
    if isinstance(values, dict):
        return {str(key): value for key, value in values.items()}
    if "key" in arguments:
        return {str(arguments["key"]): arguments.get("value")}
    return {
        str(key): value
        for key, value in arguments.items()
        if key not in ("sessionId", "session_id")
    }


def extract_memory_values(data):
    """从 BetterGI 的 memory 响应中取出键值。

    当前 C# 返回 ``{sessionId, updatedAtUtc, values}``；旧测试/兼容客户端
    可能返回 ``{"memory": {...}}`` 或直接返回键值对象。三种都接受。
    """
    if not isinstance(data, dict):
        return {}
    values = data.get("values")
    if isinstance(values, dict):
        return values
    memory = data.get("memory")
    if isinstance(memory, dict):
        return memory
    if any(key in data for key in ("sessionId", "updatedAtUtc", "values")):
        return {}
    return data


# ----------------------------------------------------------------------
# BetterGI HTTP 客户端
# ----------------------------------------------------------------------


class BetterGIError(Exception):
    """携带 HTTP 状态和稳定错误码的 BetterGI 调用错误。"""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = int(status or 0)
        self.code = code or "UNKNOWN"
        self.message = message or str(code)


class BetterGIClient:
    """仅覆盖导演需要的 BetterGI HTTP 接口。"""

    def __init__(self, session, base_url, token, session_id, timeout=30.0):
        self.session = session
        self.base_url = (base_url or DEFAULT_HTTP_URL).rstrip("/")
        self.token = token or ""
        self.session_id = session_id or DEFAULT_SESSION_ID
        self.timeout = float(timeout)

    @staticmethod
    def _session_path(session_id, suffix=""):
        """构造带转义的会话路径。"""
        sid = urllib.parse.quote(str(session_id or "default"), safe="")
        return "/api/v1/agent/sessions/{0}{1}".format(sid, suffix)

    def _build_error(self, status, body):
        """把错误响应转换为带排查提示的 BetterGIError。"""
        text = (body or "").strip()
        code = "HTTP_{0}".format(status)
        message = truncate(text, 500) or "HTTP {0}".format(status)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            code = data.get("code") or code
            message = data.get("message") or data.get("title") or message

        if status == 401:
            message = "{0}（令牌无效或缺失，请在 BetterGI 设置页重新复制令牌）".format(
                message
            )
        elif status == 404:
            message = (
                "{0}（接口未启用或路径不存在，请检查 HTTP API 与智能体开关）".format(
                    message
                )
            )
        elif status == 403 and code == "AGENT_CONTROL_DISABLED":
            message = "{0}（请在设置页启用智能体控制并重启 BetterGI）".format(message)
        return BetterGIError(status, code, message)

    async def _request(self, method, path, payload=None):
        """发送 JSON 请求并解析 JSON 响应。"""
        if aiohttp is None:
            raise BetterGIError(0, "NO_AIOHTTP", "需要 aiohttp：pip install aiohttp")
        if self.session is None:
            raise BetterGIError(0, "NO_SESSION", "HTTP 会话尚未建立。")

        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.token,
        }
        try:
            async with self.session.request(
                method,
                self.base_url + path,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise self._build_error(response.status, body)
                if not body:
                    return None
                try:
                    return json.loads(body)
                except (TypeError, ValueError):
                    raise BetterGIError(
                        response.status,
                        "INVALID_RESPONSE",
                        "BetterGI 返回的内容不是有效 JSON：{0}".format(
                            truncate(body, 300)
                        ),
                    ) from None
        except aiohttp.ClientError as exc:
            raise BetterGIError(
                0, "CONNECTION_FAILED", "无法连接 BetterGI：{0}".format(exc)
            ) from None
        except asyncio.TimeoutError:
            raise BetterGIError(0, "TIMEOUT", "BetterGI 请求超时。") from None

    async def ensure_session(self, name=None):
        """确保会话存在（尽力而为）。"""
        payload = {"sessionId": self.session_id}
        if name:
            payload["name"] = name
        return await self._request("POST", "/api/v1/agent/sessions", payload)

    async def memory_get(self):
        """读取会话记忆。"""
        return await self._request(
            "GET", self._session_path(self.session_id, "/memory")
        )

    async def memory_set(self, values):
        """合并写入会话记忆；值为 ``None`` 的键会被删除。"""
        if not isinstance(values, dict):
            raise BetterGIError(0, "INVALID_REQUEST", "内存内容必须是 JSON 对象。")
        return await self._request(
            "POST", self._session_path(self.session_id, "/memory"), values
        )

    async def checkpoint_create(self, name=None, note=None):
        """创建会话检查点。"""
        payload = {}
        if name:
            payload["name"] = name
        if note:
            payload["note"] = note
        return await self._request(
            "POST", self._session_path(self.session_id, "/checkpoints"), payload
        )

    async def live_status(self):
        """读取 Live 状态。"""
        return await self._request("GET", "/api/v1/agent/live/status")

    async def live_set_guidance(self, text, source="director"):
        """通过 HTTP 设置当前指导。"""
        return await self._request(
            "POST",
            "/api/v1/agent/live/guidance",
            {"text": text, "source": source},
        )

    async def execute_tool(self, name, arguments):
        """执行白名单工具；未知名称直接拒绝。"""
        if name not in ALLOWED_TOOL_NAMES:
            raise BetterGIError(0, "UNKNOWN_TOOL", "未授权工具调用：{0}".format(name))
        arguments = arguments if isinstance(arguments, dict) else {}

        if name == "bgi_agent_memory_set":
            values = memory_values_from_arguments(arguments)
            if not values:
                raise BetterGIError(
                    0, "EMPTY_MEMORY_PATCH", "memory_set 没有可写入的键值。"
                )
            return await self.memory_set(values)

        if name == "bgi_agent_memory_get":
            data = await self.memory_get()
            key = arguments.get("key")
            memory = extract_memory_values(data)
            if key is not None:
                return {"key": str(key), "value": memory.get(str(key))}
            return {"memory": memory}

        if name == "bgi_agent_checkpoint_create":
            name_value = str(arguments.get("name") or "director-checkpoint")[:80]
            note = str(arguments.get("note") or "由实况导演自动创建")[:512]
            return await self.checkpoint_create(name=name_value, note=note)

        if name == "bgi_live_status":
            return await self.live_status()

        if name == "bgi_live_set_guidance":
            text = normalize_guidance(arguments.get("text"))
            if not text:
                raise BetterGIError(
                    0, "EMPTY_GUIDANCE", "live_set_guidance 的 text 为空。"
                )
            await self.live_set_guidance(text)
            return {"ok": True, "guidance": text}

        raise BetterGIError(0, "UNKNOWN_TOOL", "未授权工具调用：{0}".format(name))


async def execute_tool_calls(client, tool_calls, logger=None):
    """按白名单执行工具调用，返回每个调用的结果摘要。

    未知工具会被过滤并记录日志，不会执行任意代码。
    """
    log = logger or LOGGER
    calls = filter_tool_calls(tool_calls, logger=log)
    results = []
    if client is None:
        if calls:
            log.warning("HTTP 客户端未连接，忽略 %d 个工具调用", len(calls))
        return results

    for call in calls:
        name = call["name"]
        arguments = call["arguments"]
        try:
            value = await client.execute_tool(name, arguments)
        except BetterGIError as exc:
            log.warning("工具 %s 执行失败：%s", name, exc)
            results.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "ok": False,
                    "error": str(exc),
                }
            )
        except Exception as exc:  # pragma: no cover - 防御性兜底
            log.exception("工具 %s 出现未预期错误", name)
            results.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "ok": False,
                    "error": str(exc),
                }
            )
        else:
            results.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "ok": True,
                    "result": value,
                }
            )
    return results


# ----------------------------------------------------------------------
# 导演主循环
# ----------------------------------------------------------------------


class LiveDirector:
    """连接 BetterGI Live，按间隔或显著事件驱动 LLM 导演。"""

    def __init__(
        self,
        ws_url,
        http_url,
        token,
        session_id=DEFAULT_SESSION_ID,
        persona=None,
        interval=DEFAULT_INTERVAL,
        no_llm=False,
        dry_run=False,
        llm=None,
        reconnect_delay=DEFAULT_RECONNECT_DELAY,
        max_commentary_per_tick=DEFAULT_MAX_COMMENTARY_PER_TICK,
        memory_limit=DEFAULT_MEMORY_LIMIT,
        logger=None,
        send_hook=None,
        memory_bridge=None,
    ):
        self.ws_url = build_ws_url(ws_url, token)
        self.display_ws_url = redact_url(self.ws_url)
        self.http_url = (http_url or DEFAULT_HTTP_URL).rstrip("/")
        self.token = token or ""
        self.session_id = session_id or DEFAULT_SESSION_ID
        self.persona = persona or dict(DEFAULT_PERSONA)
        self.interval = max(0.1, float(interval))
        self.min_tick_gap = max(0.5, self.interval / 2.0)
        self.no_llm = bool(no_llm)
        self.dry_run = bool(dry_run)
        self.llm = llm
        self.reconnect_delay = max(0.0, float(reconnect_delay))
        self.max_commentary_per_tick = max(1, int(max_commentary_per_tick))
        self.memory_limit = int(memory_limit)
        self.log = logger or LOGGER
        self.send_hook = send_hook

        self.http = None
        self._ws = None
        self._tick_event = None

        self._latest_frame = None
        self._frame_seq = 0
        self._latest_guidance = ""
        self._last_received_guidance = ""
        self._last_sent_guidance = ""
        self._pending_guidance = ""
        self._last_thought = ""
        self._recent_commentary = deque(maxlen=RECENT_COMMENTARY_LIMIT)
        self._short_events = deque(maxlen=40)
        self._action_state = {}
        self.memory = {}
        self.memory_bridge = (
            memory_bridge
            if memory_bridge is not None
            else MemoryBridge.from_env(logger=self.log)
        )
        self._memory_started = False
        self._memory_write_task = None
        self._last_memory_write = 0.0
        self._episode_log = []

    # -- 生命周期 -------------------------------------------------------

    def _safe_error(self, exc):
        """错误文本中隐去令牌，便于安全写日志。"""
        text = str(exc)
        if self.token:
            text = text.replace(self.token, "***")
        return text

    async def run(self):
        """持续运行；连接断开时按 ``reconnect_delay`` 重连。"""
        if aiohttp is None:
            raise RuntimeError(
                "需要 aiohttp 才能运行导演循环，请先执行：pip install aiohttp"
            )
        if not self.no_llm and not self.dry_run and self.llm is None:
            raise RuntimeError(
                "未配置 LLM 客户端；请设置 API 密钥，或使用 --no-llm / --dry-run。"
            )

        mode = "no-llm" if self.no_llm else ("dry-run" if self.dry_run else "llm")
        self.log.info(
            "导演启动：%s，模式=%s，会话=%s，间隔=%.1fs",
            self.display_ws_url,
            mode,
            self.session_id,
            self.interval,
        )

        self._memory_started = await self.memory_bridge.start()
        try:
            while True:
                try:
                    await self._run_connection()
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                    self.log.warning(
                        "Live 连接中断：%s；%.1f 秒后重连",
                        self._safe_error(exc),
                        self.reconnect_delay,
                    )
                except Exception:
                    self.log.exception(
                        "实况循环异常；%.1f 秒后重连", self.reconnect_delay
                    )
                await asyncio.sleep(self.reconnect_delay)
        finally:
            task = self._memory_write_task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(task, timeout=20)
                except Exception:  # noqa: BLE001 - 收尾失败不影响主流程
                    pass
            await self.memory_bridge.close()

    async def _run_connection(self):
        """建立一次 HTTP + WS 连接，并运行接收与轮询任务。"""
        timeout = aiohttp.ClientTimeout(total=None, connect=15.0, sock_connect=15.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.http = BetterGIClient(
                session, self.http_url, self.token, self.session_id
            )
            if not self.no_llm:
                # dry-run 只读取记忆构建提示词，不创建会话、不产生写入。
                await self._load_initial_memory(ensure=not self.dry_run)

            async with session.ws_connect(
                self.ws_url, heartbeat=30.0, max_msg_size=0
            ) as ws:
                self._ws = ws
                self._tick_event = asyncio.Event()
                self.log.info("已连接 BetterGI Live：%s", self.display_ws_url)

                receiver = asyncio.ensure_future(self._receive_loop(ws))
                ticker = asyncio.ensure_future(self._tick_loop())
                try:
                    done, _pending = await asyncio.wait(
                        [receiver, ticker],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        if task.cancelled():
                            continue
                        error = task.exception()
                        if error is not None:
                            raise error
                finally:
                    for task in (receiver, ticker):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(receiver, ticker, return_exceptions=True)
                    self._ws = None
                    self._tick_event = None
                    self.http = None
                    self.log.info("BetterGI Live 连接已关闭")

    async def _load_initial_memory(self, ensure=True):
        """尽力确保会话存在并读取已有记忆；失败不影响主流程。"""
        if self.http is None:
            return
        if ensure:
            try:
                await self.http.ensure_session(name="实况导演")
            except BetterGIError as exc:
                self.log.debug("确保会话存在失败（可忽略）：%s", exc)
        try:
            data = await self.http.memory_get()
        except BetterGIError as exc:
            self.log.info("读取会话记忆失败（可忽略）：%s", exc)
            return
        if isinstance(data, dict):
            memory = extract_memory_values(data)
            if memory:
                self.memory.update(memory)
                self.log.info("已载入会话记忆 %d 个键", len(memory))

    # -- WebSocket 接收 -------------------------------------------------

    async def _receive_loop(self, ws):
        """接收并分发 BetterGI Live 消息。"""
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except (TypeError, ValueError):
                    self.log.warning("收到无法解析的 WS 文本消息")
                    continue
                if isinstance(payload, dict):
                    self._handle_message(payload)
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise ConnectionError("WebSocket 错误：{0}".format(ws.exception()))
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                break

    def _handle_message(self, payload):
        """按 ``type`` 分发服务端消息。"""
        message_type = payload.get("type")
        if message_type == "hello":
            self.log.info(
                "Live Hub hello：session=%s fps=%s frame=%sx%s",
                payload.get("sessionId"),
                payload.get("fps"),
                payload.get("frameWidth"),
                payload.get("frameHeight"),
            )
        elif message_type == "frame":
            self._handle_frame(payload)
        elif message_type == "guidance":
            text = payload.get("text")
            if isinstance(text, str):
                self._apply_guidance(text, payload.get("source") or "server")
        elif message_type == "commentary":
            self._handle_commentary(payload)
        elif message_type == "action_state":
            self._handle_action_state(payload)
        elif message_type == "error":
            self.log.warning(
                "BetterGI Live 错误 [%s]：%s",
                payload.get("code"),
                payload.get("message"),
            )
        elif message_type == "pong":
            pass
        else:
            self.log.debug("忽略未知 WS 消息类型：%r", message_type)

    def _handle_frame(self, payload):
        """保存最新画面，并同步帧内附带的最新指导。"""
        try:
            self._frame_seq = int(payload.get("seq") or self._frame_seq)
        except (TypeError, ValueError):
            pass

        jpeg = payload.get("jpeg")
        if isinstance(jpeg, str) and jpeg:
            self._latest_frame = {
                "jpeg": jpeg,
                "seq": self._frame_seq,
                "ts": payload.get("ts"),
                "width": payload.get("width"),
                "height": payload.get("height"),
                "uiCategory": payload.get("uiCategory"),
            }

        guidance = payload.get("guidance")
        if isinstance(guidance, str):
            self._apply_guidance(guidance, "frame")

    def _record_short_event(self, kind, text):
        """记录一条短期事件，供下一轮提示词里的剧情上下文使用。"""
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        self._short_events.append(
            {
                "t": time.strftime("%H:%M:%S"),
                "kind": str(kind),
                "text": clean[:240],
            }
        )

    def _short_term_text(self):
        """把场景卡与最近事件压成一段短期记忆。"""
        lines = []
        for key, label in (
            ("scene", "当前场景"),
            ("objective", "当前目标"),
            ("plot", "剧情进展"),
        ):
            value = self.memory.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{label}：{value.strip()[:240]}")
        events = list(self._short_events)[-8:]
        if events:
            lines.append("最近事件：")
            lines.extend(
                f"- [{event['t']}] {event['kind']}：{event['text']}" for event in events
            )
        return "\n".join(lines)[:1400]

    def _apply_guidance(self, text, source):
        """更新当前指导；仅当外部指导真的变化时触发提前轮询。"""
        text = text.strip()
        if not text:
            return
        changed = text != self._last_received_guidance
        self._last_received_guidance = text
        self._latest_guidance = text
        if changed and text != self._last_sent_guidance:
            self.log.info("收到新指导（%s）：%s", source, text)
            self._record_short_event("指导", text)
            self._signal_tick()

    def _handle_commentary(self, payload):
        """把服务端解说加入最近上下文，并对自己的回显去重。"""
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        if str(payload.get("kind") or "") == "thought":
            # 内部思考只进会话文件，不参与“最近说过的话”上下文。
            return
        meta = payload.get("meta")
        if isinstance(meta, dict) and meta.get("internal"):
            return
        item = {
            "kind": str(payload.get("kind") or "reaction"),
            "text": text.strip(),
            "emotion": str(payload.get("emotion") or "calm"),
        }
        if payload.get("frameSeq") is not None:
            item["frameSeq"] = payload.get("frameSeq")
        self._remember_commentary(item)

    def _handle_action_state(self, payload):
        """更新动作状态；输入开始/停止时触发提前轮询。"""
        keys = payload.get("keys")
        if not isinstance(keys, list):
            keys = []
        previous_keys = self._action_state.get("keys")
        self._action_state = {
            "keys": keys,
            "queueDepth": payload.get("queueDepth"),
            "starved": payload.get("starved"),
            "applied": payload.get("applied"),
            "dropped": payload.get("dropped"),
            "seq": payload.get("seq"),
        }
        if detect_significant_change(None, None, previous_keys, keys):
            self.log.debug("动作状态显著变化：keys=%s", keys)
            self._record_short_event("动作", "按键：" + "+".join(map(str, keys)))
            self._signal_tick()

    def _signal_tick(self):
        """请求轮询任务尽快执行一轮。"""
        if self._tick_event is not None:
            self._tick_event.set()

    def _remember_commentary(self, item):
        """加入最近解说，并抑制最近窗口内的重复项；返回是否是新的。"""
        signature = (
            item.get("kind"),
            item.get("text"),
            item.get("frameSeq"),
        )
        for existing in self._recent_commentary:
            if (
                existing.get("kind"),
                existing.get("text"),
                existing.get("frameSeq"),
            ) == signature:
                return False
        self._recent_commentary.append(item)
        self._record_short_event("解说", item.get("text"))
        return True

    # -- 轮询 -----------------------------------------------------------

    async def _tick_loop(self):
        """按固定间隔轮询；显著事件可以提前触发，但受最小间隔限制。"""
        loop = asyncio.get_running_loop()
        event = self._tick_event
        last = 0.0
        while True:
            wait = max(0.05, self.interval - (loop.time() - last))
            triggered = False
            try:
                await asyncio.wait_for(event.wait(), timeout=wait)
                triggered = True
            except asyncio.TimeoutError:
                pass
            event.clear()

            now = loop.time()
            if triggered and (now - last) < self.min_tick_gap:
                await asyncio.sleep(self.min_tick_gap - (now - last))

            last = loop.time()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("导演轮询失败；本轮跳过")

    async def _tick(self):
        """取最新画面执行一轮导演。"""
        frame = self._latest_frame

        if self.no_llm:
            if frame is None and not self._action_state:
                self.log.debug("尚无画面/动作，跳过本轮")
                return
            self.log.info(
                "no-llm frame=%s ui=%s guidance=%r keys=%s queue=%s starved=%s",
                frame.get("seq") if frame else None,
                frame.get("uiCategory") if frame else None,
                truncate(self._latest_guidance, 80),
                self._action_state.get("keys"),
                self._action_state.get("queueDepth"),
                self._action_state.get("starved"),
            )
            return

        if frame is None:
            self.log.debug("尚无画面，跳过本轮")
            return

        short_term = self._short_term_text()
        long_term = ""
        if self.memory_bridge.available:
            query_parts = [
                self._latest_guidance,
                str(self.memory.get("scene") or ""),
                str(self.memory.get("objective") or ""),
                str(self.memory.get("plot") or ""),
            ]
            if self._recent_commentary:
                query_parts.append(str(self._recent_commentary[-1].get("text") or ""))
            query = " ".join(
                part.strip() for part in query_parts if part and part.strip()
            )
            if query:
                long_term = await self.memory_bridge.recall(query)
                if long_term:
                    self.log.debug("记忆检索：%s", truncate(long_term, 300))

        prompt = build_prompt(
            self.persona,
            guidance=self._latest_guidance,
            commentary=self._recent_commentary,
            action_state=self._action_state,
            memory=self.memory,
            frame_seq=frame.get("seq"),
            memory_limit=self.memory_limit,
            short_term=short_term,
            long_term=long_term,
        )

        if self.dry_run:
            self._print_dry_run(prompt, frame)
            return

        try:
            text = await self.llm.complete(
                prompt["system"],
                prompt["user"],
                image_b64=frame.get("jpeg"),
                media_type="image/jpeg",
            )
        except LLMError as exc:
            self.log.error("LLM 调用失败：%s", exc)
            return

        data = extract_director_json(text)
        if data is None:
            self.log.warning(
                "LLM 未返回可解析的 JSON，跳过本轮：%s", truncate(text, 300)
            )
            return

        await self._process_response(data, frame)

    def _print_dry_run(self, prompt, frame):
        """--dry-run：打印截断后的提示词，不调用 LLM、不发送。"""
        print(
            "[dry-run] frameSeq={0} size={1}x{2} jpeg_base64_chars={3}".format(
                frame.get("seq"),
                frame.get("width"),
                frame.get("height"),
                len(frame.get("jpeg") or ""),
            )
        )
        print("[dry-run] system prompt:\n" + truncate(prompt["system"], 2000))
        print("[dry-run] user prompt:\n" + truncate(prompt["user"], 2000))
        print("[dry-run] 未调用 LLM、未发送指导/解说")

    # -- 响应处理 -------------------------------------------------------

    async def _process_response(self, data, frame=None):
        """把一轮 LLM 响应写回 BetterGI。"""
        if not isinstance(data, dict):
            return
        frame_seq = None
        if isinstance(frame, dict):
            frame_seq = frame.get("seq")
        if frame_seq is None:
            frame_seq = self._frame_seq

        thought = data.get("thought")
        if isinstance(thought, str) and thought.strip():
            thought_text = thought.strip()[:480]
            self.log.info("内部思考：%s", truncate(thought_text, 240))
            # 思考不朗读，但必须进入同一份时间线：用 meta.internal 标记，
            # 录制器据此记成 thought 事件，叠加层与字幕忽略它。
            if thought_text != self._last_thought:
                self._last_thought = thought_text
                payload = {
                    "type": "commentary",
                    "kind": "plan",
                    "text": thought_text,
                    "emotion": "thinking",
                    "meta": {"internal": True, "thought": True},
                }
                if isinstance(frame_seq, int):
                    payload["frameSeq"] = frame_seq
                await self._send(payload)
                self._record_short_event("思考", thought_text)

        guidance = normalize_guidance(data.get("guidance"))
        if not guidance and self._pending_guidance:
            guidance = self._pending_guidance
        if guidance:
            retrying = (
                bool(self._pending_guidance) and guidance == self._pending_guidance
            )
            if guidance != self._last_sent_guidance and (
                guidance != self._latest_guidance or retrying
            ):
                sent = await self._send({"type": "guidance", "text": guidance})
                if sent:
                    self._pending_guidance = ""
                    self._last_sent_guidance = guidance
                    self._latest_guidance = guidance
                    self._last_received_guidance = guidance
                    self.log.info("发送指导：%s", guidance)
                    self._record_short_event("指导", guidance)
                else:
                    self._pending_guidance = guidance
                    self.log.warning(
                        "指导发送失败，保留待发状态，下一轮重试：%s", guidance
                    )
            elif guidance == self._last_sent_guidance:
                self.log.debug("指导未变化，不重复发送")
            else:
                self.log.debug("指导已由外部设置，不重复发送")

        commentary_items = normalize_commentary(
            data.get("commentary"),
            frame_seq=frame_seq,
            max_items=self.max_commentary_per_tick,
        )
        for item in commentary_items:
            if not self._remember_commentary(item):
                continue
            await self._send(item)
            self.log.info(
                "发送解说 [%s/%s] %s",
                item.get("kind"),
                item.get("emotion"),
                item.get("text"),
            )

        await self._apply_memory_patch(data.get("memory_patch"))
        await self._apply_tool_calls(data.get("tool_calls"))
        await self._maybe_write_episode()

    async def _maybe_write_episode(self):
        """按间隔把本场剧情摘要写进 OpenViking，形成长期记忆。"""
        bridge = self.memory_bridge
        if not bridge.available:
            return
        interval = float(bridge.settings.write_interval_s)
        now = time.time()
        if now - self._last_memory_write < interval:
            return
        task = self._memory_write_task
        if task is not None and not task.done():
            return

        scene = str(self.memory.get("scene") or "").strip()
        objective = str(self.memory.get("objective") or "").strip()
        plot = str(self.memory.get("plot") or "").strip()
        latest_speech = next(
            (
                str(event["text"])
                for event in reversed(self._short_events)
                if event.get("kind") == "解说"
            ),
            "",
        )
        line = "[{0}] 场景={1} 目标={2} 剧情={3} 最近说={4}".format(
            time.strftime("%H:%M:%S"),
            scene or "未知",
            objective or "未知",
            plot or "未知",
            latest_speech or "无",
        )
        self._episode_log.append(line)
        self._episode_log = self._episode_log[-60:]
        content = "# 实况记忆 {0}\n\n{1}\n".format(
            self.session_id, "\n".join(self._episode_log)
        )
        self._last_memory_write = now
        self._memory_write_task = asyncio.create_task(
            bridge.remember_session(self.session_id, content)
        )

    async def _apply_memory_patch(self, patch):
        """把 ``memory_patch`` 合并进本地记忆并写入 BetterGI。"""
        if patch is None:
            return
        if not isinstance(patch, dict):
            self.log.warning("memory_patch 不是对象，已忽略")
            return

        clean = {}
        for key, value in patch.items():
            if not isinstance(key, str) or not key:
                continue
            clean[key] = value
        if not clean:
            return
        try:
            json.dumps(clean, ensure_ascii=False)
        except (TypeError, ValueError):
            self.log.warning("memory_patch 不是可序列化 JSON，已忽略")
            return

        self.memory.update(clean)
        self.log.info("记忆更新：%s", _json_preview(clean, 240))
        if self.http is None:
            self.log.debug("HTTP 客户端未连接，未写入会话记忆")
            return
        try:
            await self.http.memory_set(clean)
        except BetterGIError as exc:
            self.log.warning("写入会话记忆失败：%s", exc)

    async def _apply_tool_calls(self, tool_calls):
        """执行白名单工具，并同步本地记忆/指导状态。"""
        if not isinstance(tool_calls, list) or not tool_calls:
            return
        if self.http is None:
            self.log.warning(
                "收到 %d 个工具调用，但 HTTP 客户端未连接，已忽略",
                len(tool_calls),
            )
            return

        results = await execute_tool_calls(self.http, tool_calls, logger=self.log)
        for result in results:
            name = result.get("name")
            if not result.get("ok"):
                self.log.warning("工具 %s 执行失败：%s", name, result.get("error"))
                continue

            if name == "bgi_live_set_guidance":
                value = result.get("result")
                text = None
                if isinstance(value, dict):
                    text = value.get("guidance")
                if not text:
                    arguments = result.get("arguments")
                    if isinstance(arguments, dict):
                        text = arguments.get("text")
                text = normalize_guidance(text)
                if text:
                    self._last_sent_guidance = text
                    self._latest_guidance = text
                    self._last_received_guidance = text
            elif name == "bgi_agent_memory_set":
                values = memory_values_from_arguments(result.get("arguments") or {})
                if values:
                    self.memory.update(values)

            self.log.info(
                "工具 %s 执行成功：%s",
                name,
                _json_preview(result.get("result"), 200),
            )

    async def _send(self, payload):
        """发送 WS 消息；返回是否真正发出。

        测试可注入 ``send_hook`` 取代真实连接。调用方必须检查返回值，
        不能在发送失败时把 guidance 标记为已发送。
        """
        if self.send_hook is not None:
            result = self.send_hook(payload)
            if inspect.isawaitable(result):
                await result
            return True
        if self._ws is None:
            self.log.warning("WebSocket 未连接，消息未发送（%s）", payload.get("type"))
            return False
        try:
            await self._ws.send_str(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:  # noqa: BLE001 - 连接断开不应击穿主循环
            self.log.warning("WebSocket 发送失败（%s）：%s", payload.get("type"), exc)
            return False


# ----------------------------------------------------------------------
# 人格 / 配置加载
# ----------------------------------------------------------------------


def load_persona(path):
    """读取人格 JSON；文件缺失或格式错误时回退到内置人格。"""
    if not path:
        return dict(DEFAULT_PERSONA)
    try:
        with open(path, "r", encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        LOGGER.warning("人格文件不存在，使用内置人格：%s", path)
        return dict(DEFAULT_PERSONA)
    except (OSError, ValueError) as exc:
        LOGGER.warning("读取人格文件失败，使用内置人格：%s", exc)
        return dict(DEFAULT_PERSONA)
    if not isinstance(data, dict):
        LOGGER.warning("人格文件必须是 JSON 对象，使用内置人格：%s", path)
        return dict(DEFAULT_PERSONA)
    persona = dict(DEFAULT_PERSONA)
    persona.update(data)
    return persona


def load_config(path):
    """读取导演 JSON 配置；未提供路径时返回空字典。"""
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象：{0}".format(path))
    return data


def _pick(cli_value, env_name, config, keys, default=None):
    """按 CLI > 环境变量 > 配置文件 > 默认值 的顺序取值。"""
    if cli_value is not None:
        return cli_value
    if env_name:
        env_value = os.environ.get(env_name)
        if env_value not in (None, ""):
            return env_value
    for key in keys:
        if key in config and config[key] is not None:
            return config[key]
    return default


def resolve_options(args):
    """把 CLI、环境变量和配置文件合并成最终运行参数。"""
    config = load_config(args.config)

    ws_url = _pick(
        args.bettergi_ws,
        "BETTERGI_LIVE_WS",
        config,
        ("bettergiWs", "ws"),
        default=DEFAULT_WS_URL,
    )
    http_url = _pick(
        args.bettergi_http,
        "BETTERGI_HTTP",
        config,
        ("bettergiHttp", "http"),
        default=DEFAULT_HTTP_URL,
    )
    token = args.token or os.environ.get("BETTERGI_AGENT_TOKEN") or ""
    session_id = _pick(
        args.session_id,
        "BETTERGI_SESSION_ID",
        config,
        ("sessionId", "session_id"),
        default=DEFAULT_SESSION_ID,
    )
    persona_path = _pick(
        args.persona,
        "LIVE_DIRECTOR_PERSONA",
        config,
        ("persona",),
        default="persona.json",
    )

    interval = float(
        _pick(args.interval, None, config, ("interval",), default=DEFAULT_INTERVAL)
    )
    if interval <= 0:
        raise ValueError("--interval 必须大于 0")

    max_commentary = int(
        _pick(
            args.max_commentary,
            None,
            config,
            ("maxCommentaryPerTick", "max_commentary"),
            default=DEFAULT_MAX_COMMENTARY_PER_TICK,
        )
    )
    memory_limit = int(
        config.get("promptMemoryLimit")
        or config.get("memoryLimit")
        or DEFAULT_MEMORY_LIMIT
    )
    reconnect_delay = float(
        _pick(
            args.reconnect_delay,
            None,
            config,
            ("reconnectDelay", "reconnect_delay"),
            default=DEFAULT_RECONNECT_DELAY,
        )
    )

    # 配置文件里的相对人格路径优先按配置文件目录解析。
    if persona_path and not os.path.isabs(persona_path) and args.config:
        candidate = os.path.join(
            os.path.dirname(os.path.abspath(args.config)), persona_path
        )
        if os.path.exists(candidate):
            persona_path = candidate

    return {
        "ws_url": ws_url,
        "http_url": http_url,
        "token": token,
        "session_id": session_id,
        "persona_path": persona_path,
        "interval": interval,
        "max_commentary": max_commentary,
        "memory_limit": max(memory_limit, 200),
        "reconnect_delay": max(reconnect_delay, 0.0),
        "no_llm": bool(args.no_llm),
        "dry_run": bool(args.dry_run),
    }


# ----------------------------------------------------------------------
# 命令行入口
# ----------------------------------------------------------------------


def build_parser():
    """构建命令行参数。"""
    parser = argparse.ArgumentParser(
        description="BetterGI Live 参考导演 / VTuber 解说循环"
    )
    parser.add_argument(
        "--config", default=None, help="JSON 配置文件（如 director_config.json）"
    )
    parser.add_argument(
        "--bettergi-ws",
        default=None,
        help="BetterGI Live WebSocket 地址，默认 {0}".format(DEFAULT_WS_URL),
    )
    parser.add_argument(
        "--bettergi-http",
        default=None,
        help="BetterGI HTTP 基地址，默认 {0}".format(DEFAULT_HTTP_URL),
    )
    parser.add_argument(
        "--token",
        default=None,
        help="访问令牌，默认读取环境变量 BETTERGI_AGENT_TOKEN",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="会话标识，默认 {0}".format(DEFAULT_SESSION_ID),
    )
    parser.add_argument(
        "--persona",
        default=None,
        help="人格 JSON 文件，默认 persona.json（缺失时使用内置人格）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="轮询间隔秒数，默认 {0}".format(DEFAULT_INTERVAL),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不调用 LLM，只记录画面/动作，由 dsh 驱动；不发送指导或解说",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印截断后的提示词，不调用 LLM、不发送",
    )
    parser.add_argument(
        "--max-commentary",
        type=int,
        default=None,
        help="每轮最多发送的解说条数，默认 {0}".format(DEFAULT_MAX_COMMENTARY_PER_TICK),
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=None,
        help="断线重连间隔秒数，默认 {0}".format(DEFAULT_RECONNECT_DELAY),
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


async def async_main(args):
    """异步入口。"""
    options = resolve_options(args)
    persona = load_persona(options["persona_path"])

    if aiohttp is None:
        LOGGER.error("需要 aiohttp：pip install aiohttp")
        return 2

    llm = None
    if not options["no_llm"] and not options["dry_run"]:
        try:
            llm = LLMClient.from_env()
        except LLMError as exc:
            LOGGER.error("%s", exc)
            return 2

    if not options["token"]:
        LOGGER.warning(
            "未提供令牌；若 BetterGI 启用了鉴权，连接会被拒绝。"
            "请设置 BETTERGI_AGENT_TOKEN 或传入 --token。"
        )

    director = LiveDirector(
        ws_url=options["ws_url"],
        http_url=options["http_url"],
        token=options["token"],
        session_id=options["session_id"],
        persona=persona,
        interval=options["interval"],
        no_llm=options["no_llm"],
        dry_run=options["dry_run"],
        llm=llm,
        reconnect_delay=options["reconnect_delay"],
        max_commentary_per_tick=options["max_commentary"],
        memory_limit=options["memory_limit"],
    )
    try:
        await director.run()
    finally:
        if llm is not None:
            await llm.close()
    return 0


def main(argv=None):
    """同步命令行入口。"""
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOGGER.info("已中断")
        return 130
    except (OSError, ValueError) as exc:
        LOGGER.error("配置错误：%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
