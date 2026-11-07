"""快系统的"一个意图一个会话"：开局新建，交棒即断；断线静默重连，用预置台词撑场。

设计文档 01 §3 / 03 C.2 的口径：
    - realtime 会话不跨意图复用（旧会话的上下文会把上一轮的判断带进来）；
    - 冷启 1-3s 是可接受成本，但不能让画面和嘴一起哑掉；
    - 断线时先重连一次，仍不通就用预置台词占住 1-2 句，不冒充"我看到了"。

FastStream 已经做到"start() 新建 client、stop() 关连接"，本模块在其上加三件事：
    1. session_opens / reconnects / degraded 三个计数器（验收要能数出来）；
    2. ask() 失败 -> 先 stop+start 重连一次 -> 再失败才降级说预置台词；
    3. 预置台词轮转池（不重复同一句，冷场时观众听不出是故障）。
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence

from fast_stream import FastStream
from fast_system import INTENT_INSTRUCTIONS


#: 真断线时撑场用的台词。不带任何"我看到了什么"的具体内容——那是编造。
DEFAULT_PRESETS: tuple = (
    "嗯……我这边画面卡了一下，等我瞄一眼。",
    "稍等，我眼睛进沙子了，揉一揉。",
    "哎，信号有点抖，缓我半秒。",
)

#: 一整轮开口的总预算（秒）。collect 还有首包 12s / 单步 4s 兜底，这里是外层保险。
#: Omni 能用工具交棒/报警/求参考：函数名 -> 事件 kind（02_schemas.md §9 的工具表）。
TOOL_EVENT_KINDS = {
    "request_handoff": "handoff",
    "report_blocked": "blocked",
    "ask_slow_reference": "reference",
    #: 快系统自己按一下（omni.PRESS_KEY_TOOL）：默认不用，只有非按不可才发起。
    "press_key": "press",
}

#: 交棒没给理由时的兜底理由（执行器要求 DONE 后面至少有 4 个字）。
DEFAULT_HANDOFF_REASON = "这一轮我处理完了，交给你看下一步"

#: 事件只留最近这么多条：交棒证据够用，别把内存堆成日志。
MAX_EVENTS = 50

DEFAULT_ROUND_S = 25.0
ROUND_ENV = "BETTERAGI_OMNI_ROUND_S"


def round_budget_s(default: float = DEFAULT_ROUND_S) -> float:
    """整轮开口预算：env 覆盖，非法值回默认。"""

    raw = os.environ.get(ROUND_ENV, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class IntentFastSession(FastStream):
    """一个意图一个 Omni 会话，断了先重连、再不行就说预置台词。"""

    def __init__(self, config: Any = None, fps: float = 1.0, pump: bool = True,
                 presets: Optional[Sequence[str]] = None,
                 round_s: Optional[float] = None):
        super().__init__(config=config, fps=fps, pump=pump)
        self.presets: List[str] = [p for p in (presets or DEFAULT_PRESETS) if str(p).strip()]
        self.round_s = float(round_s) if round_s else round_budget_s()
        #: 建过几个会话（= 意图数 + 重连次数），验收里要能对上意图数。
        self.session_opens = 0
        self.reconnects = 0
        self.degraded = False
        self.last_error = ""
        self.presets_said = 0
        self._preset_i = 0
        #: Omni 的工具调用（交棒/卡住/求参考）按发生顺序留证：慢系统要据此判断这一轮怎么结束的。
        self.events: List[dict] = []
        self.handoff_requested = False
        self.handoff_reason = ""
        #: 快系统主动要按的键（press_key 工具），等执行器来取走；取走即清空。
        self.pending_press: List[str] = []

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        super().start()
        self.session_opens += 1
        self.handoff_requested = False
        self.handoff_reason = ""
        self.pending_press = []

    def close_intent(self) -> None:
        """意图结束：断开这个会话（下次 ask 会新建）。"""

        try:
            self.stop()
        except Exception as error:  # noqa: BLE001 - 断不干净也不能影响下一轮
            self.last_error = "%s: %s" % (type(error).__name__, error)

    # ------------------------------------------------------------ 开口
    def preset_line(self) -> str:
        """取一句预置台词（轮转，不连续重复）。"""

        if not self.presets:
            return "（画面卡了一下）"
        line = self.presets[self._preset_i % len(self.presets)]
        self._preset_i += 1
        self.presets_said += 1
        return line

    def ask(self, instructions: str = INTENT_INSTRUCTIONS, timeout: Optional[float] = None) -> str:
        """开口一次。失败先重连一次；仍失败返回预置台词并把 degraded 置真。

        文本之外还要处理 Omni 的工具调用：request_handoff / report_blocked /
        ask_slow_reference 这三件是"快系统真的有话要说"的通道。交棒那一句会被
        补上 [DONE] 理由，执行器据此收手、把控制权交回慢系统（不用模型自己记得写）。
        """

        budget = float(timeout) if timeout else self.round_s
        try:
            self.degraded = False
            return self._absorb(super().ask_reply(instructions=instructions, timeout=budget))
        except Exception as error:  # noqa: BLE001 - 第一跳：静默重连
            self.last_error = "%s: %s" % (type(error).__name__, error)

        try:
            self.close_intent()
            self.start()
            self.reconnects += 1
            text = self._absorb(super().ask_reply(instructions=instructions, timeout=budget))
            self.degraded = False
            return text
        except Exception as error:  # noqa: BLE001 - 第二跳：撑场
            self.last_error = "%s: %s" % (type(error).__name__, error)
            self.degraded = True
            return self.preset_line()

    # ------------------------------------------------------------ 工具调用
    def _absorb(self, reply: Any) -> str:
        """把这一轮 Reply 里的函数调用记下来，必要时把交棒补成执行器认得的收手标记。"""

        text = str(getattr(reply, "text", "") or "").strip()
        calls = list(getattr(reply, "function_calls", None) or [])
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            kind = TOOL_EVENT_KINDS.get(name)
            if not kind:
                continue
            event = {
                "kind": kind,
                "name": name,
                "arguments": call.get("arguments"),
                "call_id": call.get("call_id") or "",
                "text": text,
            }
            self.events.append(event)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            if kind == "handoff":
                self.handoff_requested = True
                self.handoff_reason = self._reason_from(call.get("arguments"))
            elif kind == "press":
                keys = self._keys_from(call.get("arguments"))
                if keys:
                    self.pending_press = keys
        if self.handoff_requested and "[DONE]" not in text:
            reason = self.handoff_reason or DEFAULT_HANDOFF_REASON
            text = (text + " [DONE] " + reason).strip() if text else "（交棒） [DONE] " + reason
        return text

    @staticmethod
    def _keys_from(arguments: Any) -> List[str]:
        """从 press_key 参数里取键（最多 6 个）；形状不对就返回空——不猜。"""

        raw: Any = arguments
        if isinstance(raw, str):
            try:
                import json

                raw = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001 - 解析不了就不按
                return []
        if not isinstance(raw, dict):
            return []
        keys = raw.get("keys", raw.get("key"))
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, (list, tuple)):
            return []
        return [str(key).strip() for key in keys if str(key).strip()][:6]

    def take_press(self) -> List[str]:
        """取走快系统要按的键（取一次就清空；没有就是空表，等于这一步不按）。"""

        keys = list(self.pending_press or [])
        self.pending_press = []
        return keys

    @staticmethod
    def _reason_from(arguments: Any) -> str:
        """从工具参数里取一句人话理由；取不到就空串（交给兜底）。"""

        if isinstance(arguments, dict):
            for key in ("reason", "why", "conclusion", "summary", "text"):
                value = str(arguments.get(key) or "").strip()
                if value:
                    return value
        if isinstance(arguments, str):
            return arguments.strip()[:200]
        return ""

    # ------------------------------------------------------------ 观测
    def stats(self) -> dict:
        return {
            "session_opens": self.session_opens,
            "reconnects": self.reconnects,
            "degraded": self.degraded,
            "presets_said": self.presets_said,
            "last_error": self.last_error,
            "frames_sent": self.frames_sent,
            "last_latency_ms": round(self.last_latency_ms, 1),
            "events": len(self.events),
            "handoff_requested": self.handoff_requested,
        }


def build_intent_session(fps: float = 1.0, pump: bool = True,
                         presets: Optional[Sequence[str]] = None) -> Optional[IntentFastSession]:
    """没 key 返回 None（慢系统单腿跑：只有手，没有嘴）。"""

    from fast_system import resolve_dashscope_key

    if not resolve_dashscope_key():
        return None
    session = IntentFastSession(fps=fps, pump=pump, presets=presets)
    if not session.config.api_key:
        return None
    return session

