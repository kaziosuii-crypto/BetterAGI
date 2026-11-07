"""v7 → v8：llm_usage 与 llm_requests 建立真连接键并补齐缓存记账列。

- ``llm_usage`` 增加 ``request_id``（TEXT 可空）：与 ``llm_requests.request_id``
  构成连接键，聚合账与请求明细可按次对齐。
- ``llm_requests`` 增加 ``cache_hit_tokens`` / ``cache_miss_tokens``
  （NOT NULL DEFAULT 0，与 ``llm_usage`` 同形状）：请求明细自带缓存记账。
- 存量行不回填：旧数据没有对应的请求 ID 与缓存上报，保持空/零即可。

新建库的 DDL 已含新列，回调经 PRAGMA 列存在性检查保证幂等。
"""

from __future__ import annotations

import sqlite3

from src.modules.storage.migrations._common import column_exists


def migrate(conn: sqlite3.Connection) -> None:
    """原地修改、幂等：列存在性检查保证对新建库与已迁移库安全。"""
    if not column_exists(conn, "llm_usage", "request_id"):
        conn.execute("ALTER TABLE llm_usage ADD COLUMN request_id TEXT")
    if not column_exists(conn, "llm_requests", "cache_hit_tokens"):
        conn.execute("ALTER TABLE llm_requests ADD COLUMN cache_hit_tokens INTEGER NOT NULL DEFAULT 0")
    if not column_exists(conn, "llm_requests", "cache_miss_tokens"):
        conn.execute("ALTER TABLE llm_requests ADD COLUMN cache_miss_tokens INTEGER NOT NULL DEFAULT 0")


__all__ = ["migrate"]
