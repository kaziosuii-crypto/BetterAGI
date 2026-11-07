"""快系统输出 → 意图 → 总线裁决（FR-9 / M3）。

**控制通道只有一条：函数调用（tool calling）。** 自然语言不驱动动作。

理由：关键词匹配是脆弱的（「打开背包」被「打」吃掉、「等」与「等一会儿」不同义），
而且与慢系统一致——那边也是 TOOL_SPECS 描述的工具调用，MCP 体系同样如此。
所以这里把意图定义成一个正式工具 set_intent，用 JSON Schema 明确取值域；
快系统只能在 schema 内说话，越界就是 NO_OP，绝不靠猜。

文本回复仍然有用：它进 why（日志/复盘），但**不参与动作决策**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import intent as intent_mod


#: 快系统唯一可用的动作工具。schema 与慢系统 TOOL_SPECS 同构（OpenAI function calling）。
INTENT_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "set_intent",
            "description": (
                "声明接下来要做的一件事。不调用它等于不动。"
                "一次只声明一个意图，换目标就重新调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["NO_OP", "navigate", "explore", "combat", "interact", "wait"],
                        "description": "意图类型；NO_OP 表示立刻收手（安全方向，优先）",
                    },
                    "target": {
                        "type": "string",
                        "description": "目标对象（地点/NPC/敌人/物件）；NO_OP 与 wait 可为空",
                    },
                    "urgency": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "紧迫度；>=0.9 可抢断正在执行的意图",
                    },
                    "ttl": {
                        "type": "number",
                        "minimum": 0,
                        "description": "本条意图的有效秒数，过期自动回落 NO_OP",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "你对这个判断的把握；低于门槛会被总线拒绝",
                    },
                },
                "required": ["type"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    }
]


class ArbiterError_(RuntimeError):
    """意图参数非法（schema 之外的取值）。"""


@dataclass
class ArbiterResult:
    accepted: bool
    reason: str
    intent: intent_mod.Intent
    motor_mode: str = ""
    evidence: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "motor_mode": self.motor_mode,
            "evidence": self.evidence,
            **self.intent.as_dict(),
        }


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def intent_from_call(call: Optional[Dict[str, Any]], *, default_ttl: float = 15.0) -> intent_mod.Intent:
    """把 set_intent 的函数调用严格翻译成 Intent；不合 schema 就抛错，不做兜底猜测。"""

    if not call:
        raise ArbiterError_("没有函数调用：文本回复不作为指令")
    if str(call.get("name") or "") != "set_intent":
        raise ArbiterError_("未知工具 %r：快系统只允许 set_intent" % (call.get("name"),))
    args = call.get("arguments")
    if isinstance(args, str):
        raw = args.strip()
        if not raw:
            raise ArbiterError_("函数参数为空")
        import json

        try:
            args = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ArbiterError_("函数参数不是合法 JSON：%s" % error) from error
    if not isinstance(args, dict):
        raise ArbiterError_("函数参数必须是对象")

    kind = str(args.get("type") or "").strip()
    if kind not in intent_mod.INTENT_TYPES:
        raise ArbiterError_("意图类型 %r 不在枚举内" % (kind,))
    target = str(args.get("target") or "").strip()
    if kind not in ("NO_OP", "wait", "explore") and not target:
        raise ArbiterError_("意图 %s 必须给出 target" % kind)

    return intent_mod.Intent(
        type=kind,
        target=target,
        urgency=_clamp(args.get("urgency", 0.5), 0.0, 1.0, 0.5),
        ttl=_clamp(args.get("ttl", default_ttl), 0.0, 3600.0, default_ttl),
        why=str(args.get("why") or "")[:120],
        confidence=_clamp(args.get("confidence", 0.8), 0.0, 1.0, 0.8),
    )


def motor_mode_for(kind: str) -> str:
    """走哪条腿：需要连续控制的交给 lumine，单步/观察/等待快系统自己扛。"""

    return kind if kind in ("navigate", "combat", "interact") else ""


class IntentArbiter:
    """唯一裁决入口：函数调用 → 校验 → 总线。文本只做证据。"""

    def __init__(self, bus: intent_mod.IntentBus, *, min_confidence: float = 0.5):
        self.bus = bus
        self.min_confidence = float(min_confidence)

    def submit(
        self,
        text: str = "",
        *,
        function_call: Optional[Dict[str, Any]] = None,
        ttl: float = 15.0,
    ) -> ArbiterResult:
        try:
            parsed = intent_from_call(function_call, default_ttl=ttl)
        except ArbiterError_ as error:
            return ArbiterResult(
                False,
                str(error),
                intent_mod.Intent(type="NO_OP", why="无合法工具调用"),
                "",
                "text_only" if not function_call else "schema_violation",
            )

        if parsed.type != "NO_OP" and parsed.confidence < self.min_confidence:
            return ArbiterResult(
                False,
                "置信度 %.2f 低于 %.2f" % (parsed.confidence, self.min_confidence),
                intent_mod.Intent(type="NO_OP", why="置信度不足：" + (text or "")[:60]),
                "",
                "function_call",
            )

        accepted, reason = self.bus.accept(parsed)
        return ArbiterResult(accepted, reason, parsed, motor_mode_for(parsed.type), "function_call")
