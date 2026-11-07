#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""玩家 resume 状态：版本化读取、校验与原子写入。

Phase 1 只落地 ``resume_state.json`` 这一份状态；emotion/belief/relationship/
thread 的完整存储属于 Phase 5，不在这里提前造空壳。未知字段会原样保留，
方便后续阶段只增不删地扩展 schema。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from player_io import (
    atomic_write_json,
    backup_corrupt_file,
    clean_text,
    read_json,
    utc_now_iso,
)
from player_profile import find_secret_fields, is_secret_key

SCHEMA_VERSION = 1

_LIST_FIELDS = (
    "unfinished_actions",
    "active_story_threads",
    "recent_important_memories",
    "recent_character_relationships",
    "current_beliefs",
    "things_i_want_to_check_next",
)


class StateError(ValueError):
    """状态文件属于其他玩家、读取失败等无法安全继续的情况。"""


class StateSchemaError(StateError):
    """状态文件版本高于当前程序，拒绝降级覆盖。"""


def default_state(player_id: str) -> Dict[str, Any]:
    """返回一份可以直接使用的空 resume 状态。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "player_id": player_id,
        "updated_at": "",
        "last_session_id": "",
        "last_run_id": "",
        "last_location": "",
        "current_quest": "",
        "current_intention": "",
        "unfinished_actions": [],
        "active_story_threads": [],
        "recent_important_memories": [],
        "current_emotion": {},
        "recent_character_relationships": [],
        "current_beliefs": [],
        "things_i_want_to_check_next": [],
        "last_internal_thought": "",
        # Phase 1 运行指标：帮助确认 shadow 真的在推进，不参与认知。
        "last_frame_seq": 0,
        "shadow_ticks": 0,
    }


def _text(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_text(value).split())[:limit]


def _text_list(value: Any, limit: int = 30, item_limit: int = 300) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: List[str] = []
    for item in value:
        if isinstance(item, str):
            text = " ".join(item.split())
            if text:
                items.append(text[:item_limit])
        if len(items) >= limit:
            break
    return items


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def normalize_state(data: Any, player_id: str) -> Dict[str, Any]:
    """把任意 JSON 对象规范化成当前 resume schema，保留未知字段。"""
    base = default_state(player_id)
    if not isinstance(data, dict):
        return base

    merged = dict(data)
    merged.update(
        {
            "schema_version": SCHEMA_VERSION,
            "player_id": player_id,
            "updated_at": _text(data.get("updated_at"), 40),
            "last_session_id": _text(data.get("last_session_id"), 128),
            "last_run_id": _text(data.get("last_run_id"), 128),
            "last_location": _text(data.get("last_location"), 200),
            "current_quest": _text(data.get("current_quest"), 300),
            "current_intention": _text(data.get("current_intention"), 300),
            "current_emotion": (
                dict(data.get("current_emotion"))
                if isinstance(data.get("current_emotion"), dict)
                else {}
            ),
            "last_internal_thought": _text(data.get("last_internal_thought"), 800),
            "last_frame_seq": _non_negative_int(data.get("last_frame_seq")),
            "shadow_ticks": _non_negative_int(data.get("shadow_ticks")),
        }
    )
    for key in _LIST_FIELDS:
        merged[key] = _text_list(data.get(key))
    return merged


def _strip_secret_fields(value: Any) -> Any:
    """递归丢弃令牌/密钥字段；列表与未知字段结构保持不变。"""
    if isinstance(value, dict):
        return {
            key: _strip_secret_fields(item)
            for key, item in value.items()
            if not is_secret_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_strip_secret_fields(item) for item in value]
    return value


class StateStore:
    """一个玩家的 resume 状态文件；负责损坏恢复与版本检查。"""

    def __init__(
        self,
        path: Path | str,
        player_id: str,
        *,
        clock: Optional[Callable[[], int]] = None,
        logger: Any = None,
    ):
        self.path = Path(path)
        self.player_id = player_id
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.log = logger
        self.loaded_from_disk = False
        self.recovered_corrupt: Optional[Path] = None
        self.load_warning = ""
        self.migrated_from: Optional[int] = None

    def load(self) -> Dict[str, Any]:
        """读取状态；缺失返回空状态，损坏则备份后重建。

        版本高于当前程序时抛出 ``StateSchemaError``，绝不覆盖新版本数据。
        """
        self.loaded_from_disk = False
        self.recovered_corrupt = None
        self.load_warning = ""
        self.migrated_from = None

        if not self.path.exists():
            return default_state(self.player_id)

        try:
            data = read_json(self.path)
        except ValueError as exc:
            return self._recover_corrupt("JSON 解析失败：{0}".format(exc))
        except OSError as exc:
            raise StateError("读取玩家状态失败：{0}（{1}）".format(self.path, exc))

        if not isinstance(data, dict):
            return self._recover_corrupt("状态文件顶层不是 JSON 对象")

        # 状态由程序生成，正常不会出现令牌；出现时只丢弃并告警，不把它
        # 原样写回磁盘。
        secrets = find_secret_fields(data)
        if secrets:
            self.load_warning = "状态包含敏感字段，已忽略：{0}".format(
                "、".join(secrets)
            )
            data = _strip_secret_fields(data)

        version = data.get("schema_version", 0)
        if isinstance(version, bool) or not isinstance(version, int):
            return self._recover_corrupt("schema_version 不是整数")
        if version > SCHEMA_VERSION:
            raise StateSchemaError(
                "玩家状态版本 {0} 高于当前支持的 {1}；请使用更新的程序，"
                "当前进程不会覆盖该文件：{2}".format(version, SCHEMA_VERSION, self.path)
            )

        existing_player = data.get("player_id")
        if isinstance(existing_player, str) and existing_player.strip():
            if existing_player.strip() != self.player_id:
                raise StateError(
                    "状态文件属于玩家 {0!r}，当前玩家是 {1!r}：{2}".format(
                        existing_player, self.player_id, self.path
                    )
                )

        self.loaded_from_disk = True
        if version < SCHEMA_VERSION:
            self.migrated_from = version
            message = "状态从 schema_version={0} 迁移到 {1}".format(
                version, SCHEMA_VERSION
            )
            self.load_warning = (
                self.load_warning + "；" + message if self.load_warning else message
            )
        return normalize_state(data, self.player_id)

    def save(self, state: Dict[str, Any], *, now_ms: Optional[int] = None) -> Path:
        """规范化后原子写入；失败抛 OSError，由调用方决定是否继续。"""
        self._backup_before_migration()
        merged = _strip_secret_fields(normalize_state(state, self.player_id))
        merged["schema_version"] = SCHEMA_VERSION
        merged["updated_at"] = utc_now_iso(
            self._clock() if now_ms is None else now_ms
        )
        atomic_write_json(self.path, merged)
        return self.path

    def _backup_before_migration(self) -> None:
        """首次把旧版本写回前保留一份原文件，迁移失败仍可人工恢复。"""
        if self.migrated_from is None or not self.path.exists():
            return
        backup = self.path.with_name(
            "{0}.v{1}.bak".format(self.path.name, self.migrated_from)
        )
        try:
            if not backup.exists():
                shutil.copy2(self.path, backup)
            if self.log is not None:
                self.log.info("迁移前状态已备份：%s", backup)
        except OSError as exc:
            if self.log is not None:
                self.log.warning("迁移前备份失败（继续保存）：%s", exc)
        self.migrated_from = None

    def _recover_corrupt(self, reason: str) -> Dict[str, Any]:
        """备份损坏文件并返回空状态，保证下一轮还能继续运行。"""
        try:
            backup = backup_corrupt_file(self.path, self._clock())
        except OSError as exc:
            raise StateError(
                "玩家状态损坏且无法备份：{0}（{1}；{2}）".format(
                    self.path, reason, exc
                )
            )
        self.recovered_corrupt = backup
        self.load_warning = "玩家状态损坏（{0}），已备份到 {1}，本次使用空状态".format(
            reason, backup
        )
        if self.log is not None:
            self.log.warning(self.load_warning)
        return default_state(self.player_id)
