#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow 记录器：只写本地 JSONL，不向游戏发送任何内容。

shadow 记录与正式 ``session.jsonl`` 分离存放：Phase 1 不改录制器的事件集合，
也不与录制器争抢同一文件的写权限。每行是一条完整 JSON，带 run_id，便于
区分同一次会话里的多次启动。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import clean_text, utc_now_iso
from player_profile import is_secret_key

SCHEMA_VERSION = 1

# 常见密钥形态；只用于日志/记录脱敏，不改变正常中文文本。
_SECRET_PATTERNS = (
    # 不用 \b：中文也是 Unicode 单词字符，"密钥sk-...已失效" 会被漏掉。
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)(access_token|token|password|api[_-]?key|secret)\s*[:=]\s*([^\s,;\"']+)"
    ),
)
_SAFE_SESSION = re.compile(r"[^A-Za-z0-9._-]+")


def redact_secrets(text: str) -> str:
    """遮蔽文本中的密钥/令牌，防止模型回显或异常信息写入记录。"""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    cleaned = clean_text(text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            cleaned = pattern.sub(lambda m: m.group(1) + "=***", cleaned)
        else:
            cleaned = pattern.sub("***", cleaned)
    return cleaned


def _sanitize(value: Any) -> Any:
    """递归脱敏；只处理 JSON 基本类型，避免把不可序列化对象写进日志。"""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            name = clean_text(str(key))
            if _is_sensitive_key(name):
                cleaned[name] = "***"
            else:
                cleaned[name] = _sanitize(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))


def _is_sensitive_key(key: str) -> bool:
    """键名本身是令牌/密钥时直接遮蔽；复用档案的单一实现。"""
    return is_secret_key(key)


def new_run_id(now: Optional[float] = None) -> str:
    """生成一次运行标识：时间戳 + 4 位随机后缀。"""
    moment = time.localtime(time.time() if now is None else now)
    return time.strftime("%Y%m%d-%H%M%S", moment) + "-" + uuid.uuid4().hex[:4]


def safe_session_name(session_id: str) -> str:
    """把会话名限制为安全文件名，避免路径穿越与非法字符。"""
    cleaned = _SAFE_SESSION.sub("-", str(session_id or "").strip())
    cleaned = cleaned.strip(".-")
    return cleaned[:80] or "session"


class ShadowRecorder:
    """线程安全的追加式 shadow JSONL 写入器。"""

    def __init__(
        self,
        directory: Path | str,
        session_id: str,
        player_id: str,
        *,
        run_id: Optional[str] = None,
        clock: Optional[Callable[[], int]] = None,
        logger: Any = None,
    ):
        self.directory = Path(directory)
        self.session_id = clean_text(str(session_id or "session"))
        self.player_id = clean_text(str(player_id or "player"))
        self.run_id = clean_text(run_id or new_run_id())
        self.path = self.directory / (safe_session_name(self.session_id) + ".jsonl")
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger
        self._lock = threading.Lock()
        self._closed = False
        self._seq = 0
        self._started_ms = self._clock()

    def write(self, record_type: str, **fields: Any) -> Dict[str, Any]:
        """追加一条记录并返回写入内容；关闭后继续写入属于调用方错误。"""
        if self._closed:
            raise RuntimeError("ShadowRecorder 已关闭，不能继续写入")
        record = self._build_record(record_type, fields)
        self._append(record)
        return record

    def close(self, **fields: Any) -> Optional[Dict[str, Any]]:
        """写入结束标记；重复调用是安全的。"""
        with self._lock:
            if self._closed:
                return None
            # 先标记关闭，避免其它线程在写结束标记时插入新记录。
            self._closed = True
        record = self._build_record("shadow_end", fields)
        self._append(record, sync=True)
        return record

    def _build_record(self, record_type: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(record_type, str) or not record_type:
            raise ValueError("shadow 记录必须带 type")
        now_ms = self._clock()
        self._seq += 1
        record: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": record_type,
            "mode": "shadow",
            "executed": False,
            # sent 永远为 false：shadow 记录绝不能成为对外发送的证据。
            "sent": False,
            "seq": self._seq,
            "player_id": self.player_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "t_unix_ms": now_ms,
            "run_t_ms": max(0, now_ms - self._started_ms),
            "timestamp_utc": utc_now_iso(now_ms),
        }
        for key, value in fields.items():
            if key in record:
                continue
            record[key] = _sanitize(value)
        return record

    def _append(self, record: Dict[str, Any], *, sync: bool = False) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())

    @property
    def closed(self) -> bool:
        return self._closed


def read_shadow_records(path: Path | str, *, strict: bool = True) -> List[Dict[str, Any]]:
    """读取 shadow JSONL；空行忽略。

    ``strict=False`` 时只容忍崩溃留下的最后一行半截 JSON，其余坏行仍报错。
    """
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig") as handle:
        lines = handle.readlines()
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError as exc:
            if not strict and number == len(lines) and not line.endswith("\n"):
                break
            raise ValueError(
                "shadow 记录第 {0} 行不是合法 JSON：{1}".format(number, exc)
            )
        if not isinstance(record, dict):
            raise ValueError("shadow 记录第 {0} 行不是 JSON 对象".format(number))
        records.append(record)
    return records
