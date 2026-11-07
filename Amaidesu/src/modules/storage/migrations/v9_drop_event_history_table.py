"""v8 → v9：删除 event_history 事件副本表。

事件流持久观察由业务表承担（消息流落 live_chat 等），事件历史定位收敛为
"运行周期内存观察窗"（EventHistoryService 纯环形缓冲），副本表无消费者；
回放已改读 live_chat。直接 DROP 回收空间。新建库的 ``build_schema_sql()``
已不含该表 DDL，DROP IF EXISTS 仅处理存量库。
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """原地修改、幂等：DROP IF EXISTS 处理存量库。"""
    conn.execute("DROP TABLE IF EXISTS event_history")


__all__ = ["migrate"]
