"""意图总线（FR-9）：慢系统下意图，快系统/动作层消费。

ProAct 的「克制」落在三个机制上：

1. **默认 NO_OP**：没有任何意图就是"别动"，不是"随便动"。
2. **门槛**：新意图的 ``confidence`` 与 ``urgency`` 必须过阈值才允许覆盖当前意图。
3. **最小驻留（dwell）**：一条意图生效后至少存活 ``min_dwell`` 秒，防止慢系统两次决策之间
   把动作来回撕扯（抖动）。

意图带 TTL：过期自动失效 → 回到 NO_OP，快系统不会抱着一条过时命令一直跑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


#: 意图类型：移动、交互、战斗、看、等、无。
INTENT_TYPES = ("NO_OP", "navigate", "explore", "combat", "interact", "wait")
#: 谁拥有这条意图（03 §C.7）：慢系统只负责「出任务」，执行期归快系统。
INTENT_OWNERS = ("fast", "slow")
#: 交棒状态：idle 正常、pending 已请求、running 慢系统正在规划。
HANDOFF_STATES = ("idle", "pending", "running")


@dataclass
class Intent:
    """一条高层意图。慢系统产出，快系统与动作层消费。"""

    type: str = "NO_OP"
    target: str = ""
    urgency: float = 0.0
    ttl: float = 20.0
    why: str = ""
    confidence: float = 0.0
    created_at: float = field(default_factory=lambda: time.time())
    meta: Dict[str, Any] = field(default_factory=dict)
    #: 这条意图的编号：编排层发号（orchestrator 的 intent_id），不是模型编。
    intent_id: str = ""
    #: 归属：slow 只在规划期持有，执行期一律是 fast。
    owner: str = "fast"
    #: 交棒状态（见 HANDOFF_STATES）。
    handoff_state: str = "idle"
    #: 本场直播的 session_id（落盘/复盘要对得上）。
    session_id: str = ""

    def __post_init__(self) -> None:
        if self.type not in INTENT_TYPES:
            raise ValueError(f"未知意图类型 {self.type!r}，可用：{INTENT_TYPES}")
        self.urgency = max(0.0, min(1.0, float(self.urgency)))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.owner not in INTENT_OWNERS:
            raise ValueError(f"未知意图归属 {self.owner!r}，可用：{INTENT_OWNERS}")
        if self.handoff_state not in HANDOFF_STATES:
            raise ValueError(f"未知交棒状态 {self.handoff_state!r}，可用：{HANDOFF_STATES}")

    @property
    def expires_at(self) -> float:
        return self.created_at + max(0.0, self.ttl)

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


NO_OP = Intent(type="NO_OP", urgency=0.0, ttl=0.0, confidence=1.0, why="无意图=克制")


class IntentBus:
    """意图的唯一写者通道：accept() 决定要不要换，不是来一条换一条。"""

    def __init__(
        self,
        *,
        min_dwell: float = 1.0,
        min_confidence: float = 0.5,
        min_urgency: float = 0.0,
        clock=time.time,
        clock_ms=None,
    ):
        self.min_dwell = float(min_dwell)
        self.min_confidence = float(min_confidence)
        self.min_urgency = float(min_urgency)
        self._clock = clock
        #: 给了 clock_ms（一般就是 timeline.Clock.now_ms）就只用它：
        #: 全仓三套时钟（time.time / monotonic / 段内相对）统一到这一条，TTL 才可复算。
        self._clock_ms = clock_ms
        #: 交棒状态：慢系统规划期不接受新的快意图（快慢互斥，01 §6）。
        self.handoff_state: str = "idle"
        self._current: Intent = Intent(type="NO_OP", urgency=0.0, ttl=0.0, confidence=1.0, why="启动即克制")
        self._accepted_at: float = self._now()
        self._history: List[Dict[str, Any]] = []

    def _now(self) -> float:
        """统一取时：优先毫秒钟（timeline），退回秒钟。"""

        if self._clock_ms is not None:
            return float(self._clock_ms()) / 1000.0
        return float(self._clock())

    # ---- 交棒互斥 ----------------------------------------------------
    def begin_handoff(self, why: str = "") -> str:
        """进入交棒：此后只收 owner=slow 的意图（快系统不能插话）。"""

        self.handoff_state = "running"
        self._history.append({"ts": self._now(), "kind": "handoff_begin", "why": why})
        return self.handoff_state

    def end_handoff(self, why: str = "") -> str:
        self.handoff_state = "idle"
        self._history.append({"ts": self._now(), "kind": "handoff_end", "why": why})
        return self.handoff_state

    # ---- 读 ---------------------------------------------------------
    def current(self) -> Intent:
        """当前生效意图；过期就回落 NO_OP（不返回陈旧命令）。"""
        now = self._now()
        if self._current.type != "NO_OP" and self._current.expired(now):
            self._set(Intent(type="NO_OP", urgency=0.0, ttl=0.0, confidence=1.0, why="上一条意图过期"), now)
        return self._current

    def history(self, limit: int = 20) -> List[Dict[str, Any]]:
        return self._history[-limit:]

    # ---- 写 ---------------------------------------------------------
    def _set(self, intent: Intent, now: float) -> None:
        # TTL 以「总线接收时刻」为基准，而不是 Intent 对象的构造时刻：
        # 慢系统可能在队列里压了一小会儿才送达，过期判断必须从生效开始算。
        intent.created_at = now
        self._current = intent
        self._accepted_at = now
        self._history.append({"ts": now, **intent.as_dict()})

    def accept(self, intent: Intent) -> tuple[bool, str]:
        """尝试接收一条新意图。返回 (是否采纳, 理由)——理由要能进日志复盘。"""
        now = self._now()
        current = self.current()

        # 快慢互斥（01 §6）：交棒期间快系统不许改主意——它只能改「当下这一步怎么按」，
        # 改任务本身是慢系统的事。NO_OP（喊停）例外：安全优先。
        if (self.handoff_state != "idle" and intent.owner == "fast"
                and intent.type != "NO_OP"):
            return False, f"交棒中（{self.handoff_state}）：不收新的快意图"

        if intent.type == "NO_OP":
            # 主动要求停：立刻生效，不受 dwell 限制（安全优先）。
            if current.type == "NO_OP":
                return False, "已经是 NO_OP"
            self._set(intent, now)
            return True, "慢系统要求停下"

        if intent.confidence < self.min_confidence:
            return False, f"置信度 {intent.confidence:.2f} 低于门槛 {self.min_confidence:.2f}"
        if intent.urgency < self.min_urgency:
            return False, f"紧迫度 {intent.urgency:.2f} 低于门槛 {self.min_urgency:.2f}"

        if current.type == intent.type and current.target == intent.target:
            # 同一条意图重复下达：续期，不记为切换（避免日志刷屏与无谓抖动）。
            current.created_at = now
            current.ttl = intent.ttl
            current.urgency = intent.urgency
            return False, "同意图续期"

        elapsed = now - self._accepted_at
        if current.type != "NO_OP" and elapsed < self.min_dwell:
            # 高紧迫可以抢断，否则等 dwell 到期（防抖动）。
            if intent.urgency < 0.9:
                return False, f"最小驻留未到（{elapsed:.2f}s < {self.min_dwell:.2f}s）"
        self._set(intent, now)
        return True, f"采纳（urgency={intent.urgency:.2f} confidence={intent.confidence:.2f}）"

    def clear(self, why: str = "手工清空") -> Intent:
        now = self._now()
        stop = Intent(type="NO_OP", urgency=0.0, ttl=0.0, confidence=1.0, why=why)
        self._set(stop, now)
        return stop
