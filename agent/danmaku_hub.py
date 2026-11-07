"""弹幕环形缓冲：给慢系统的 read_danmaku 与本地 HTTP 接口共用一份数据。

设计文档 02 §9 / 03 C.9 的两条硬要求都在这里落地：
1. 弹幕**不再直接塞进慢系统上下文**（旧 tui.py:852 的做法），而是先进这份缓冲，
   慢系统用 read_danmaku 按需取；
2. 接口只给「按游标取新弹幕」一种读法：``GET /danmaku?since=<ms>&limit=50`` 返 JSONL，
   ``GET /stats`` 返条数与最后一条时间。

时间口径：``t_ms`` 是本机 epoch 毫秒（跟 danmaku_feed 客户端的 since 参数同一把尺子），
不跟时间轴的单调毫秒混用——弹幕是外部事件，跨进程只能靠 epoch 对齐。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_CAPACITY = 500
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_TEXT_CHARS = 500


class DanmakuHub:
    """线程安全的环形缓冲：满了丢最旧的，游标只进不退。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, clock=time.time) -> None:
        self.capacity = max(1, int(capacity))
        self._clock = clock
        self._items: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.dropped = 0
        self._seq = 0

    # ----------------------------------------------------------- 写
    def add(self, text: Any, user: str = "", t_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """收一条弹幕；空文本直接丢（返回 None），不算错误。"""

        body = str(text or "").strip()
        if not body:
            return None
        if len(body) > MAX_TEXT_CHARS:
            body = body[:MAX_TEXT_CHARS]
        when = int(t_ms if t_ms is not None else self._clock() * 1000)
        with self._lock:
            self._seq += 1
            item = {"t_ms": when, "seq": self._seq, "text": body, "user": str(user or "")[:40]}
            self._items.append(item)
            if len(self._items) > self.capacity:
                overflow = len(self._items) - self.capacity
                del self._items[:overflow]
                self.dropped += overflow
            return dict(item)

    # ----------------------------------------------------------- 读
    def since(self, since_ms: int = 0, limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
        """取 ``t_ms > since_ms`` 的弹幕，最多 limit 条（保持原顺序）。"""

        cursor = int(since_ms or 0)
        size = int(limit or DEFAULT_LIMIT)
        size = max(1, min(MAX_LIMIT, size))
        with self._lock:
            picked = [dict(item) for item in self._items if item["t_ms"] > cursor]
        return picked[:size]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            last = self._items[-1]["t_ms"] if self._items else 0
            return {
                "ok": True,
                "items": len(self._items),
                "capacity": self.capacity,
                "dropped": self.dropped,
                "last_ms": last,
                "seq": self._seq,
            }

    def clip(self, items: Sequence[Any]) -> List[str]:
        """只要文本（给慢系统的 read_danmaku 用，别把 seq/user 塞进 prompt）。"""

        return [str(item.get("text") or "") for item in items if str(item.get("text") or "").strip()]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.dropped = 0


def build_hub(capacity: Optional[int] = None) -> DanmakuHub:
    return DanmakuHub(capacity or DEFAULT_CAPACITY)