"""主播 Agent 运行时统计。

把散布在决策循环各分支的计数收成一个对象：Agent 与决策执行器共享同一
实例，字段即计数器本身，``as_dict()`` 是对外唯一导出口（key 名即
``get_statistics`` 的历史契约，dashboard 消费）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class StreamerStats:
    """运行时计数器（7 项；字段名即 ``get_statistics`` 返回 key）。

    决策路径直接 ``+=`` 增量；读走 ``as_dict()``。
    """

    total_messages: int = 0
    total_batches: int = 0
    total_replies: int = 0
    total_no_action: int = 0
    total_proactive: int = 0
    planner_failures: int = 0
    replyer_failures: int = 0

    def as_dict(self) -> Dict[str, Any]:
        """导出统计 dict（key 名与历史 ``get_statistics`` 契约一致）。"""
        return {
            "total_messages": self.total_messages,
            "total_batches": self.total_batches,
            "total_replies": self.total_replies,
            "total_no_action": self.total_no_action,
            "total_proactive": self.total_proactive,
            "planner_failures": self.planner_failures,
            "replyer_failures": self.replyer_failures,
        }
