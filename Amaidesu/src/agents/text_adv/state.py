"""文字冒险游戏 Agent 的内部状态（内存态，不持久化）

状态归属约定：
- 内容特有状态（当前屏文本 / 累积剧情环缓冲 / 最近选项）= Agent 包内部自由
- 框架不为此提供任何存储表；其它 Agent 经 ``game.*`` 事件或 ``text_adv_get_state``
  工具读快照，快照形状由 :meth:`TextAdvGameAgentState.to_dict` 唯一定义

识别文本的重复去重不在这里做——由同包 screen.py 的 text_key（文本指纹）承担，
状态只负责忠实地记录新屏。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import InitVar, dataclass, field
from typing import Any, Deque, Dict, List, Optional

from src.agents.text_adv.vlm import Option

# 环缓冲默认保留屏数（与 agents.toml 的 max_recent_screens 默认一致）
DEFAULT_MAX_RECENT_SCREENS = 10


@dataclass(slots=True)
class TextAdvGameAgentState:
    """文字冒险游戏 Agent 内部状态。

    Attributes:
        current_text: 最近一屏识别出的正文文本
        recent_screens: 累积剧情环缓冲（最近 N 屏文本，超限丢最旧）
        last_options: 最近一屏识别出的选项列表（复用 vlm.Option）
        updated_at_ms: 最近一次状态更新的 Unix epoch 毫秒时刻
    """

    # 构造参数：环缓冲容量；InitVar 只进构造，不落为实例字段
    max_recent_screens: InitVar[int] = DEFAULT_MAX_RECENT_SCREENS
    current_text: str = ""
    recent_screens: Deque[str] = field(init=False)
    last_options: List[Option] = field(default_factory=list)
    updated_at_ms: int = 0

    def __post_init__(self, max_recent_screens: int) -> None:
        self.recent_screens = deque(maxlen=max_recent_screens)

    def record_screen(self, text: str, options: Optional[List[Option]] = None) -> None:
        """记录一屏新内容：更新当前文本、追加环缓冲、刷新选项与时间戳。

        Args:
            text: 该屏识别出的正文文本
            options: 该屏识别出的选项列表；None 或空表示无选项屏
        """
        self.current_text = text
        self.recent_screens.append(text)
        self.last_options = list(options) if options else []
        self.updated_at_ms = time.time_ns() // 1_000_000

    def to_dict(self, *, auto: bool) -> Dict[str, Any]:
        """导出状态快照，键集恰为 ``{text, options, auto, updated_at_ms}``。

        ``auto`` 由调用方（Agent）注入——该标志由 Agent 持有，状态对象不自持。
        options 的 ``index`` 从 1 起编号，与 ``text_adv_choose`` 工具的 1-based
        序号语义一致。

        Args:
            auto: Agent 当前是否处于自动观察模式

        Returns:
            供 get_state 工具返回的快照字典
        """
        return {
            "text": self.current_text,
            "options": [
                {
                    "index": i + 1,
                    "label": option.label,
                    "clickable": option.clickable,
                }
                for i, option in enumerate(self.last_options)
            ],
            "auto": auto,
            "updated_at_ms": self.updated_at_ms,
        }


__all__ = ["DEFAULT_MAX_RECENT_SCREENS", "TextAdvGameAgentState"]
