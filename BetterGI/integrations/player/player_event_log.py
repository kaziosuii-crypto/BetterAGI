#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 用户认知事件日志及调用方提供的状态投影。

这是独立基础设施，不读取、迁移或更新现有 JSON stores。普通 append 可在同一
事务写入事件和调用方计算的投影；经历事件另以增量索引和版本维护，读取
experience.v1 时重建兼容快照。v1 数据库先原子升级 schema，再于首次访问
各 session 时原子校验并索引旧投影；旧投影保留但迁移后禁止旧写者覆盖。
对共享数据库，SQLite 的 BEGIN IMMEDIATE 串行化跨实例/跨进程写入；同一
实例的连接由锁保护。需要防止基于旧快照覆盖时应传 expected_revisions。

秘密字段和常见密钥文本会被拒绝而非脱敏；任意自由文本无法可靠识别所有
秘密，接入方仍须确保输入不含凭据。请勿将数据库直接暴露给不可信写入方。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

SCHEMA_VERSION = 2
_EXPERIENCE_PROJECTION = "experience.v1"
_EXPERIENCE_KINDS = frozenset({"raw_observation", "experienced_event", "experienced_fact",
                               "subjective_interpretation", "belief", "hypothesis",
                               "canonical_world_truth", "conversation"})
_SECRET_NAMES = {
    "token", "access_token", "refresh_token", "api_key", "apikey", "access_key",
    "private_key", "secret_key", "key_id", "password", "secret", "client_secret",
    "authorization", "credential", "credentials",
}
_SECRET_SUFFIXES = ("_token", "_secret", "_password", "_api_key", "_access_key", "_private_key", "_secret_key", "_key_id")
_SECRET_TEXT = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|\b(?:access[_-]?token|token|password|api[_-]?key|secret)\s*[:=]\s*[^\s,;\"']+"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


class EventLogError(ValueError):
    """日志输入、数据库状态或持久化操作不可安全完成。"""


class EventLogSchemaError(EventLogError):
    """数据库损坏、不兼容或版本高于当前实现。"""


class EventLogConflict(EventLogError):
    """事件 ID 已存在或状态投影版本不匹配。"""


def _text(value: Any, field: str) -> str:
    """严格验证必填文本，不回显可能包含秘密的输入。"""
    if not isinstance(value, str) or not value.strip() or _SECRET_TEXT.search(value):
        raise EventLogError(f"{field} 必须是非空且不含密钥的文本")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise EventLogError(f"{field} 不是有效的 UTF-8 文本") from exc
    return value


def validate_projection_scope(value: Any) -> str:
    """校验调用方分配的稳定玩家命名空间；与 session ID 的表完全隔离。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise EventLogError("projection_scope 必须是 1–128 位 ASCII 标识符")
    return _text(value, "projection_scope")


def _json(value: Any, field: str) -> str:
    """只接受标准有限 JSON，递归拒绝秘密字段和已知密钥文本。"""
    active: set[int] = set()

    def check(item: Any) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if _SECRET_TEXT.search(item):
                raise EventLogError(f"{field} 包含疑似密钥文本")
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise EventLogError(f"{field} 包含无效 UTF-8 文本") from exc
            return
        if isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise EventLogError(f"{field} 包含非有限数值")
            return
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in active:
                raise EventLogError(f"{field} 包含循环引用")
            active.add(identity)
            try:
                if isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise EventLogError(f"{field} 的 JSON 键必须是字符串")
                        check(key)
                        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower().replace("-", "_")
                        if name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES):
                            raise EventLogError(f"{field} 包含密钥字段")
                        check(child)
                else:
                    for child in item:
                        check(child)
            finally:
                active.remove(identity)
            return
        raise EventLogError(f"{field} 必须是标准 JSON 值")

    try:
        check(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (RecursionError, OverflowError, ValueError) as exc:
        if isinstance(exc, EventLogError):
            raise
        raise EventLogError(f"{field} 无法编码为有限 JSON") from exc


def _decode(value: str, field: str) -> Any:
    """读取时也检查 JSON，避免将损坏数据当作正常状态。"""
    try:
        data = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _json(data, field)
        return data
    except (ValueError, TypeError, RecursionError) as exc:
        raise EventLogSchemaError(f"数据库中的 {field} 无效或损坏") from exc


class PlayerEventLog:
    """可关闭的日志连接；事件 append 顺序由数据库序号而非时间戳决定。"""

    def __init__(self, path: str | Path, *, timeout: float = 5.0, read_only: bool = False) -> None:
        """打开持久日志；只读模式不初始化或迁移数据库。"""
        if str(path) == ":memory:":
            raise EventLogError("事件日志需要文件数据库")
        self._lock = threading.RLock()
        self._closed = False
        self._read_only = read_only
        try:
            if read_only:
                uri = Path(path).resolve().as_uri() + "?mode=ro"
                self._db = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None, check_same_thread=False)
            else:
                self._db = sqlite3.connect(str(path), timeout=timeout, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            if read_only:
                self._db.execute("PRAGMA query_only=ON")
                if self._db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise EventLogSchemaError("只读回顾需要已迁移到当前版本的数据库")
            else:
                self._initialize()
        except (sqlite3.Error, EventLogError) as exc:
            if hasattr(self, "_db"):
                self._db.close()
            if isinstance(exc, EventLogError):
                raise
            raise EventLogSchemaError("无法打开事件数据库，数据库可能损坏或不可访问") from exc

    def _initialize(self) -> None:
        """在写锁中执行版本检查、初始化及迁移，拒绝未知的旧数据库。"""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise EventLogSchemaError(f"事件数据库版本 {version} 高于支持版本 {SCHEMA_VERSION}")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise EventLogSchemaError("事件数据库完整性检查失败")
            if version == 0:
                existing = db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
                if existing:
                    raise EventLogSchemaError("未版本化的非空数据库不能自动迁移")
                # 版本 0 的空数据库迁移到版本 1，整体事务提交。
                db.execute("""CREATE TABLE events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    frame_seq INTEGER,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source TEXT NOT NULL,
                    caused_by TEXT,
                    intent_id TEXT
                )""")
                db.execute("CREATE INDEX events_session_seq ON events(session_id, seq)")
                db.execute("""CREATE TABLE projections (
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL REFERENCES events(seq),
                    PRIMARY KEY (session_id, name)
                )""")
                db.execute("PRAGMA user_version = 1")
            self._validate_schema()
            # 与原 v1 表分离的向后兼容扩展；旧数据库在同一事务内补表。
            db.execute("""CREATE TABLE IF NOT EXISTS scoped_projections (
                scope TEXT NOT NULL,
                name TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL REFERENCES events(seq),
                PRIMARY KEY (scope, name)
            )""")
            self._validate_scoped_schema()
            # v1 数据库也可原子补建索引；不改变事件或投影的格式。
            db.execute("CREATE INDEX IF NOT EXISTS events_type_seq ON events(type, seq DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS events_session_type_seq ON events(session_id, type, seq DESC)")
            if version < 2:
                # v2 只新增索引及迁移状态，旧事件与完整投影原样保留。
                db.execute("""CREATE TABLE experience_sessions (
                    session_id TEXT PRIMARY KEY NOT NULL,
                    baseline_revision INTEGER NOT NULL,
                    revision INTEGER NOT NULL
                )""")
                db.execute("""CREATE TABLE experience_entries (
                    seq INTEGER PRIMARY KEY REFERENCES events(seq),
                    session_id TEXT NOT NULL,
                    conversation_id TEXT
                )""")
                db.execute("CREATE INDEX experience_conversation ON experience_entries(session_id, conversation_id, seq)")
                db.execute("""CREATE TRIGGER experience_projection_insert_guard
                    BEFORE INSERT ON projections WHEN NEW.name='experience.v1'
                    AND EXISTS (SELECT 1 FROM experience_sessions WHERE session_id=NEW.session_id)
                    BEGIN SELECT RAISE(ABORT, 'experience projection migrated'); END""")
                db.execute("""CREATE TRIGGER experience_projection_update_guard
                    BEFORE UPDATE ON projections WHEN (OLD.name='experience.v1' AND
                    EXISTS (SELECT 1 FROM experience_sessions WHERE session_id=OLD.session_id))
                    OR (NEW.name='experience.v1' AND
                    EXISTS (SELECT 1 FROM experience_sessions WHERE session_id=NEW.session_id))
                    BEGIN SELECT RAISE(ABORT, 'experience projection migrated'); END""")
                db.execute("""CREATE TRIGGER experience_projection_delete_guard
                    BEFORE DELETE ON projections WHEN OLD.name='experience.v1'
                    AND EXISTS (SELECT 1 FROM experience_sessions WHERE session_id=OLD.session_id)
                    BEGIN SELECT RAISE(ABORT, 'experience projection migrated'); END""")
                db.execute("PRAGMA user_version = 2")
            self._validate_experience_schema()
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise EventLogSchemaError("无法启用 SQLite WAL 模式")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")

    def _validate_schema(self) -> None:
        """检查关键列、约束和索引，防止把错误 schema 当成有效日志。"""
        db = self._db
        columns = {
            "events": ["seq", "event_id", "session_id", "timestamp", "frame_seq", "type", "payload", "source", "caused_by", "intent_id"],
            "projections": ["session_id", "name", "state", "revision"],
        }
        info = {}
        for table, expected in columns.items():
            # 表名为代码常量而非调用方输入。
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
            if [row[1] for row in rows] != expected:
                raise EventLogSchemaError(f"事件数据库表 {table} 的 schema 不兼容")
            info[table] = rows
        required = {"events": {"event_id", "session_id", "timestamp", "type", "payload", "source"},
                    "projections": {"session_id", "name", "state", "revision"}}
        for table, names in required.items():
            if any(not row[3] for row in info[table] if row[1] in names):
                raise EventLogSchemaError(f"事件数据库表 {table} 缺少非空约束")
        seq = info["events"][0]
        if seq[2].upper() != "INTEGER" or seq[5] != 1:
            raise EventLogSchemaError("事件数据库缺少序号主键")
        indices = db.execute("PRAGMA index_list(events)").fetchall()
        unique_event = any(row[2] and not row[4] and [col[2] for col in db.execute(f'PRAGMA index_info("{row[1].replace(chr(34), chr(34)*2)}")')] == ["event_id"] for row in indices)
        pk = [row[1] for row in sorted(info["projections"], key=lambda row: row[5]) if row[5]]
        foreign_keys = db.execute("PRAGMA foreign_key_list(projections)").fetchall()
        has_reference = any(row[2:5] == ("events", "revision", "seq") for row in foreign_keys)
        if not unique_event or pk != ["session_id", "name"] or not has_reference:
            raise EventLogSchemaError("事件数据库缺少必要的唯一约束或引用")

    def _validate_scoped_schema(self) -> None:
        """拒绝与玩家投影表重名但结构不兼容的旧数据库。"""
        rows = self._db.execute("PRAGMA table_info(scoped_projections)").fetchall()
        pk = [row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5]]
        refs = self._db.execute("PRAGMA foreign_key_list(scoped_projections)").fetchall()
        if ([row[1] for row in rows] != ["scope", "name", "state", "revision"]
                or any(not row[3] for row in rows) or pk != ["scope", "name"]
                or not any(row[2:5] == ("events", "revision", "seq") for row in refs)):
            raise EventLogSchemaError("玩家投影表 schema 不兼容")

    def _validate_experience_schema(self) -> None:
        """拒绝缺失迁移表、索引或旧写者防护的 v2 数据库。"""
        db = self._db
        tables = {}
        for table, columns in (("experience_sessions", ["session_id", "baseline_revision", "revision"]),
                               ("experience_entries", ["seq", "session_id", "conversation_id"])):
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
            if [row[1] for row in rows] != columns:
                raise EventLogSchemaError("经历索引 schema 不兼容")
            tables[table] = rows
        sessions, entries = tables["experience_sessions"], tables["experience_entries"]
        refs = db.execute("PRAGMA foreign_key_list(experience_entries)").fetchall()
        if (sessions[0][5] != 1 or any(not row[3] for row in sessions)
                or entries[0][2].upper() != "INTEGER" or entries[0][5] != 1 or not entries[1][3]
                or not any(row[2:5] == ("events", "seq", "seq") for row in refs)):
            raise EventLogSchemaError("经历索引约束不兼容")
        index = [row[2] for row in db.execute("PRAGMA index_info(experience_conversation)")]
        triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if index != ["session_id", "conversation_id", "seq"] or not {
                "experience_projection_insert_guard", "experience_projection_update_guard",
                "experience_projection_delete_guard"} <= triggers:
            raise EventLogSchemaError("经历索引或旧投影写入防护缺失")

    def _ensure_experience_session(self, session_id: str) -> None:
        """在写事务内一次性索引旧快照；异常时整个迁移回滚。"""
        db = self._db
        if db.execute("SELECT 1 FROM experience_sessions WHERE session_id=?", (session_id,)).fetchone():
            return
        old = db.execute("SELECT state,revision FROM projections WHERE session_id=? AND name=?",
                         (session_id, _EXPERIENCE_PROJECTION)).fetchone()
        baseline = old["revision"] if old else 0
        indexed = []
        if old:
            state = _decode(old["state"], "projection")
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("entries"), list)):
                raise EventLogSchemaError("经历投影格式无效或版本不兼容")
            last_seq = 0
            for entry in state["entries"]:
                if (not isinstance(entry, dict) or entry.get("session_id") != session_id
                        or not isinstance(entry.get("event_id"), str)
                        or not isinstance(entry.get("kind"), str)
                        or entry.get("kind") not in _EXPERIENCE_KINDS):
                    raise EventLogSchemaError("旧经历投影包含无效条目")
                event = db.execute("SELECT * FROM events WHERE event_id=? AND session_id=?",
                                   (entry["event_id"], session_id)).fetchone()
                if (event is None or event["source"] != "player_experience"
                        or event["type"] != "experience." + entry["kind"]
                        or not last_seq < event["seq"] <= baseline
                        or _decode(event["payload"], "payload") != entry):
                    raise EventLogSchemaError("旧经历投影与已提交事件不一致")
                conversation_id = entry.get("conversation_id") if entry["kind"] == "raw_observation" else None
                if conversation_id is not None and not isinstance(conversation_id, str):
                    raise EventLogSchemaError("旧经历的对话标识无效")
                indexed.append((event["seq"], session_id, conversation_id))
                last_seq = event["seq"]
        db.execute("INSERT INTO experience_sessions VALUES(?,?,?)", (session_id, baseline, baseline))
        db.executemany("INSERT INTO experience_entries VALUES(?,?,?)", indexed)

    def _begin_experience_read(self, session_id: str) -> None:
        """已迁移会话使用 WAL 读事务；首次访问才升级为独占写事务。"""
        self._db.execute("BEGIN")
        if self._db.execute("SELECT 1 FROM experience_sessions WHERE session_id=?", (session_id,)).fetchone():
            return
        self._db.execute("ROLLBACK")
        if self._read_only:
            raise EventLogSchemaError("只读回顾不能迁移未索引的经历")
        self._db.execute("BEGIN IMMEDIATE")
        self._ensure_experience_session(session_id)

    def close(self) -> None:
        """关闭连接；重复关闭安全。"""
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self) -> "PlayerEventLog":
        """支持 with 语句。"""
        return self

    def __exit__(self, *_: Any) -> None:
        """退出时关闭数据库。"""
        self.close()

    def _ensure_open(self) -> None:
        """拒绝使用已经关闭的连接。"""
        if self._closed:
            raise EventLogError("事件日志已关闭")

    def append(self, events: Sequence[Mapping[str, Any]], *, projections: Optional[Mapping[str, Any]] = None,
               expected_revisions: Optional[Mapping[str, int]] = None,
               projection_scope: Optional[str] = None,
               expected_session_revisions: Optional[Mapping[str, int]] = None,
               expected_scoped_revisions: Optional[Mapping[str, int]] = None,
               checked_scope: Optional[str] = None, experience: bool = False,
               expected_experience_revision: Optional[int] = None) -> list[int]:
        """原子写入同一会话的事件和完整投影，返回按输入顺序的序号。

        每个事件需 event_id/session_id/timestamp/type/payload/source；frame_seq、
        caused_by、intent_id 可为 None。timestamp 为调用方提供的非空字符串。
        expected_revisions 中 0 表示投影不存在，其余为上次读取的 revision；
        不提供时为无条件覆盖。revision 是本批最后一个事件的数据库序号。
        projection_scope 非 None 时只改变投影归属，事件仍属于真实 session；
        expected_session_revisions 在同一写事务中额外校验真实 session 的投影。
        expected_scoped_revisions 和 checked_scope 可同时只校验玩家投影，
        无需覆盖该投影。两个命名空间分别存表，不与同名 session 冲突。
        experience=True 为单个经历事件同时写入索引和版本；可用
        expected_experience_revision 在事务内保护基于已读经历生成的对话。
        """
        if not isinstance(events, (list, tuple)) or not events:
            raise EventLogError("events 必须是非空事件列表")
        rows = []
        session = None
        for event in events:
            if not isinstance(event, Mapping):
                raise EventLogError("事件必须是对象")
            allowed = {"event_id", "session_id", "timestamp", "frame_seq", "type", "payload", "source", "caused_by", "intent_id"}
            if set(event) - allowed or "payload" not in event:
                raise EventLogError("事件包含未知字段或缺少 payload")
            sid = _text(event.get("session_id"), "session_id")
            if session is not None and sid != session:
                raise EventLogError("同一批事件必须属于同一会话")
            session = sid
            frame = event.get("frame_seq")
            if frame is not None and (isinstance(frame, bool) or not isinstance(frame, int) or not 0 <= frame <= 2**63 - 1):
                raise EventLogError("frame_seq 必须是非负 64 位整数或 None")
            optional = [None if event.get(k) is None else _text(event[k], k) for k in ("caused_by", "intent_id")]
            rows.append((_text(event.get("event_id"), "event_id"), sid, _text(event.get("timestamp"), "timestamp"), frame,
                         _text(event.get("type"), "type"), _json(event["payload"], "payload"), _text(event.get("source"), "source"), *optional))
        if projections is None:
            projections = {}
        if expected_revisions is None:
            expected_revisions = {}
        if expected_session_revisions is None:
            expected_session_revisions = {}
        if expected_scoped_revisions is None:
            expected_scoped_revisions = {}
        if (not isinstance(projections, Mapping) or not isinstance(expected_revisions, Mapping)
                or not isinstance(expected_session_revisions, Mapping)
                or not isinstance(expected_scoped_revisions, Mapping)):
            raise EventLogError("projections 和 expected_revisions 必须是对象")
        scope = None if projection_scope is None else validate_projection_scope(projection_scope)
        check_scope = None if checked_scope is None else validate_projection_scope(checked_scope)
        if expected_scoped_revisions and check_scope is None:
            raise EventLogError("校验玩家投影时必须指定 checked_scope")
        states = {_text(name, "projection name"): _json(value, "projection") for name, value in projections.items()}
        for name, revision in expected_revisions.items():
            if name not in states or isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= 2**63 - 1:
                raise EventLogError("预期 revision 必须对应投影且为非负 64 位整数")
        for name, revision in expected_session_revisions.items():
            _text(name, "session projection name")
            if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
                raise EventLogError("预期 session revision 必须为非负 64 位整数")
        for name, revision in expected_scoped_revisions.items():
            _text(name, "scoped projection name")
            if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
                raise EventLogError("预期 scoped revision 必须为非负 64 位整数")
        if type(experience) is not bool or (expected_experience_revision is not None and
                (type(expected_experience_revision) is not int or not 0 <= expected_experience_revision <= 2**63 - 1)):
            raise EventLogError("无效的经历写入选项")
        if expected_experience_revision is not None and not experience:
            raise EventLogError("经历 revision 只能用于经历写入")
        if experience:
            if len(events) != 1 or projections:
                raise EventLogError("经历写入必须是单事件且不能覆盖投影")
            item = events[0]
            payload = item["payload"]
            if (not isinstance(payload, dict) or not isinstance(payload.get("kind"), str)
                    or payload.get("kind") not in _EXPERIENCE_KINDS
                    or item["type"] != "experience." + payload["kind"]
                    or item["source"] != "player_experience"
                    or payload.get("session_id") != session or payload.get("event_id") != item["event_id"]):
                raise EventLogError("经历事件来源或内容不一致")
            conversation_id = payload.get("conversation_id") if payload["kind"] == "raw_observation" else None
            if conversation_id is not None:
                _text(conversation_id, "conversation_id")
        with self._lock:
            self._ensure_open()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if experience:
                    self._ensure_experience_session(session)
                    current = self._db.execute("SELECT revision FROM experience_sessions WHERE session_id=?", (session,)).fetchone()[0]
                    if expected_experience_revision is not None and current != expected_experience_revision:
                        raise EventLogConflict("经历版本冲突")
                for name, expected in expected_scoped_revisions.items():
                    current = self._db.execute("SELECT revision FROM scoped_projections WHERE scope=? AND name=?", (check_scope, name)).fetchone()
                    if (current[0] if current else 0) != expected:
                        raise EventLogConflict("玩家投影版本冲突")
                for name, expected in expected_session_revisions.items():
                    current = None
                    if name == _EXPERIENCE_PROJECTION:
                        current = self._db.execute("SELECT revision FROM experience_sessions WHERE session_id=?", (session,)).fetchone()
                    if current is None:
                        current = self._db.execute("SELECT revision FROM projections WHERE session_id=? AND name=?", (session, name)).fetchone()
                    if (current[0] if current else 0) != expected:
                        raise EventLogConflict("会话投影版本冲突")
                for name, expected in expected_revisions.items():
                    if scope is None:
                        current = self._db.execute("SELECT revision FROM projections WHERE session_id=? AND name=?", (session, name)).fetchone()
                    else:
                        current = self._db.execute("SELECT revision FROM scoped_projections WHERE scope=? AND name=?", (scope, name)).fetchone()
                    if (current[0] if current else 0) != expected:
                        raise EventLogConflict("状态投影版本冲突")
                seqs = []
                for row in rows:
                    cursor = self._db.execute("INSERT INTO events(event_id,session_id,timestamp,frame_seq,type,payload,source,caused_by,intent_id) VALUES(?,?,?,?,?,?,?,?,?)", row)
                    seqs.append(cursor.lastrowid)
                if experience:
                    self._db.execute("INSERT INTO experience_entries VALUES(?,?,?)", (seqs[0], session, conversation_id))
                    self._db.execute("UPDATE experience_sessions SET revision=? WHERE session_id=?", (seqs[0], session))
                for name, state in states.items():
                    if scope is None:
                        self._db.execute("""INSERT INTO projections(session_id,name,state,revision) VALUES(?,?,?,?)
                            ON CONFLICT(session_id,name) DO UPDATE SET state=excluded.state, revision=excluded.revision""",
                            (session, name, state, seqs[-1]))
                    else:
                        self._db.execute("""INSERT INTO scoped_projections(scope,name,state,revision) VALUES(?,?,?,?)
                            ON CONFLICT(scope,name) DO UPDATE SET state=excluded.state, revision=excluded.revision""",
                            (scope, name, state, seqs[-1]))
                self._db.execute("COMMIT")
                return seqs
            except BaseException as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.IntegrityError):
                    raise EventLogConflict("事件 ID 重复或数据库约束冲突") from exc
                if isinstance(exc, sqlite3.Error):
                    raise EventLogError("事件事务写入失败") from exc
                raise

    def list_events(self, session_id: str, *, after_seq: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """按提交顺序读取指定会话事件；after_seq 为排他的游标。"""
        session_id = _text(session_id, "session_id")
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 2**63 - 1 for v in (after_seq, limit)) or limit == 0:
            raise EventLogError("after_seq 和 limit 必须是有效的非负整数，limit 必须大于零")
        with self._lock:
            self._ensure_open()
            try:
                rows = self._db.execute("SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?", (session_id, after_seq, limit)).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    item["payload"] = _decode(item["payload"], "payload")
                    result.append(item)
                return result
            except sqlite3.Error as exc:
                raise EventLogError("读取事件失败，数据库可能损坏") from exc

    def recent_events(self, session_prefix: str, event_type: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Read recent events in a caller-scoped session namespace; this is not access control."""
        session_prefix = _text(session_prefix, "session_prefix")
        event_type = _text(event_type, "event_type")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise EventLogError("limit 必须为 1–200 的整数")
        with self._lock:
            self._ensure_open()
            try:
                rows = self._db.execute(
                    "SELECT * FROM events WHERE substr(session_id,1,?)=? AND type=? "
                    "ORDER BY seq DESC LIMIT ?",
                    (len(session_prefix), session_prefix, event_type, limit),
                ).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    item["payload"] = _decode(item["payload"], "payload")
                    result.append(item)
                return result
            except sqlite3.Error as exc:
                raise EventLogError("读取近期事件失败") from exc

    def query_events(self, *, session_ids: Optional[Sequence[str]] = None,
                     session_prefix: Optional[str] = None,
                     event_types: Sequence[str], limit: int = 20,
                     before_seq: Optional[int] = None, after_seq: Optional[int] = None,
                     visibility: Optional[str] = None,
                     channel: Optional[str] = None, region: Optional[str] = None) -> list[dict[str, Any]]:
        """按全局提交序号倒序分页，先筛选再限制数量。

        必须明确列出最多 64 个会话，或指定调用方管理的命名空间前缀；
        前缀不构成访问控制。可见性和渠道是 payload 的精确筛选，
        不是对数据真实性的验证。before_seq 倒序，after_seq 正序，均为排他游标。
        """
        if (session_ids is None) == (session_prefix is None):
            raise EventLogError("必须指定 session_ids 或 session_prefix")
        params: list[Any] = []
        if session_ids is not None:
            if not isinstance(session_ids, (list, tuple)) or not 1 <= len(session_ids) <= 64:
                raise EventLogError("session_ids 必须包含 1–64 个会话")
            ids = [_text(sid, "session_id") for sid in session_ids]
            if len(set(ids)) != len(ids):
                raise EventLogError("session_ids 不允许重复")
            clauses = ["session_id IN (" + ",".join("?" for _ in ids) + ")"]
            params.extend(ids)
        else:
            prefix = _text(session_prefix, "session_prefix")
            clauses = ["substr(session_id,1,?)=?"]
            params.extend([len(prefix), prefix])
        if not isinstance(event_types, (list, tuple)) or not 1 <= len(event_types) <= 16:
            raise EventLogError("event_types 必须包含 1–16 个事件类型")
        types = [_text(value, "event_type") for value in event_types]
        clauses.append("type IN (" + ",".join("?" for _ in types) + ")")
        params.extend(types)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise EventLogError("limit 必须为 1–200 的整数")
        if before_seq is not None and after_seq is not None:
            raise EventLogError("不能同时指定 before_seq 与 after_seq")
        if before_seq is not None:
            if type(before_seq) is not int or not 1 <= before_seq <= 2**63 - 1:
                raise EventLogError("before_seq 必须为正 64 位整数")
            clauses.append("seq < ?")
            params.append(before_seq)
        if after_seq is not None:
            if type(after_seq) is not int or not 0 <= after_seq <= 2**63 - 1:
                raise EventLogError("after_seq 必须为非负 64 位整数")
            clauses.append("seq > ?")
            params.append(after_seq)
        for field, value in (("visibility", visibility), ("channel", channel), ("region", region)):
            if value is not None:
                clauses.append("json_extract(payload, '$." + field + "') = ?")
                params.append(_text(value, field))
        params.append(limit)
        with self._lock:
            self._ensure_open()
            try:
                order = "ASC" if after_seq is not None else "DESC"
                rows = self._db.execute("SELECT * FROM events WHERE " + " AND ".join(clauses)
                                        + " ORDER BY seq " + order + " LIMIT ?", params).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    item["payload"] = _decode(item["payload"], "payload")
                    result.append(item)
                return result
            except sqlite3.Error as exc:
                raise EventLogError("读取事件失败，数据库可能损坏") from exc

    def conversation_event_page(self, session_prefix: str, *, before_seq: Optional[int] = None,
                                after_seq: Optional[int] = None, limit: int = 200,
                                pending_ends: bool = False) -> tuple[list[dict[str, Any]], Optional[int]]:
        """按类型/序号索引分页读取原始候选；损坏行跳过，游标仍推进。

        前缀只是调用方命名空间而不是授权。返回的事件尚未校验可见性或来源；
        续页游标是排他的，None 表示已读到末尾。并发新增的较新行留给下次扫描。
        """
        prefix = _text(session_prefix, "session_prefix")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise EventLogError("limit 必须为 1–200 的整数")
        if before_seq is not None and (type(before_seq) is not int or not 1 <= before_seq <= 2**63 - 1):
            raise EventLogError("before_seq 必须为正 64 位整数")
        if after_seq is not None and (type(after_seq) is not int or not 0 <= after_seq <= 2**63 - 1
                                      or before_seq is not None):
            raise EventLogError("after_seq 无效或与 before_seq 冲突")
        if type(pending_ends) is not bool:
            raise EventLogError("pending_ends 必须是布尔值")
        clauses = ["type=?", "substr(session_id,1,?)=?"]
        event_type = "experience.conversation_end_pending" if pending_ends else "experience.conversation"
        params: list[Any] = [event_type, len(prefix), prefix]
        if before_seq is not None:
            clauses.append("seq<?")
            params.append(before_seq)
        if after_seq is not None:
            clauses.append("seq>?")
            params.append(after_seq)
        params.append(limit)
        with self._lock:
            self._ensure_open()
            try:
                order = "ASC" if after_seq is not None else "DESC"
                rows = self._db.execute(
                    "SELECT * FROM events INDEXED BY events_type_seq WHERE " + " AND ".join(clauses)
                    + " ORDER BY seq " + order + " LIMIT ?", params).fetchall()
                result = []
                for row in rows:
                    item = dict(row)
                    try:
                        item["payload"] = _decode(item["payload"], "payload")
                    except EventLogSchemaError:
                        continue
                    result.append(item)
                return result, rows[-1]["seq"] if len(rows) == limit else None
            except sqlite3.Error as exc:
                raise EventLogError("读取对话事件失败，数据库可能损坏") from exc

    def get_event(self, session_id: str, event_id: str) -> Optional[dict[str, Any]]:
        """按事件 ID 精确读取，并要求会话匹配以防错误引用。"""
        session_id = _text(session_id, "session_id")
        event_id = _text(event_id, "event_id")
        with self._lock:
            self._ensure_open()
            try:
                row = self._db.execute("SELECT * FROM events WHERE event_id=? AND session_id=?",
                                       (event_id, session_id)).fetchone()
                if row is None:
                    return None
                item = dict(row)
                item["payload"] = _decode(item["payload"], "payload")
                return item
            except sqlite3.Error as exc:
                raise EventLogError("读取事件失败，数据库可能损坏") from exc

    def experience_entries(self, session_id: str, *, conversation_id: Optional[str] = None,
                           event_id: Optional[str] = None) -> tuple[list[dict[str, Any]], int]:
        """按提交序读取已登记经历；对话查询使用 session/conversation 复合索引。"""
        session_id = _text(session_id, "session_id")
        if conversation_id is not None:
            conversation_id = _text(conversation_id, "conversation_id")
        if event_id is not None:
            event_id = _text(event_id, "event_id")
        with self._lock:
            self._ensure_open()
            try:
                self._begin_experience_read(session_id)
                revision = self._db.execute("SELECT revision FROM experience_sessions WHERE session_id=?", (session_id,)).fetchone()[0]
                sql = ("SELECT e.* FROM experience_entries x JOIN events e ON e.seq=x.seq "
                       "WHERE x.session_id=? AND e.session_id=x.session_id")
                params: list[Any] = [session_id]
                if conversation_id is not None:
                    sql += " AND x.conversation_id=?"
                    params.append(conversation_id)
                if event_id is not None:
                    sql += " AND e.event_id=?"
                    params.append(event_id)
                rows = self._db.execute(sql + " ORDER BY x.seq", params).fetchall()
                result = []
                for row in rows:
                    entry = _decode(row["payload"], "payload")
                    if (not isinstance(entry, dict) or not isinstance(entry.get("kind"), str)
                            or entry.get("kind") not in _EXPERIENCE_KINDS
                            or row["source"] != "player_experience" or row["type"] != "experience." + entry["kind"]
                            or entry.get("session_id") != session_id or entry.get("event_id") != row["event_id"]):
                        raise EventLogSchemaError("经历事件与索引不一致")
                    result.append(entry)
                self._db.execute("COMMIT")
                return result, revision
            except BaseException as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise EventLogError("读取经历索引失败") from exc
                raise

    def experience_conversation_ids(self, session_id: str) -> list[str]:
        """列出有玩家可见对话的会话内标识，按第一条观察的提交顺序排列。"""
        session_id = _text(session_id, "session_id")
        with self._lock:
            self._ensure_open()
            try:
                self._begin_experience_read(session_id)
                rows = self._db.execute("""SELECT x.conversation_id, MIN(x.seq) AS first_seq
                    FROM experience_entries x JOIN events e ON e.seq=x.seq
                    WHERE x.session_id=? AND x.conversation_id IS NOT NULL
                    AND e.session_id=x.session_id AND e.source='player_experience'
                    AND e.type='experience.raw_observation'
                    AND json_extract(e.payload, '$.visibility')='player'
                    AND json_extract(e.payload, '$.channel')='dialogue'
                    GROUP BY x.conversation_id ORDER BY first_seq""", (session_id,)).fetchall()
                self._db.execute("COMMIT")
                return [row["conversation_id"] for row in rows]
            except BaseException as exc:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise EventLogError("读取对话索引失败") from exc
                raise

    def get_projection(self, session_id: str, name: str, *, projection_scope: Optional[str] = None) -> Optional[dict[str, Any]]:
        """读取 session 投影或显式玩家 scope 投影；不存在时返回 None。"""
        session_id = _text(session_id, "session_id")
        name = _text(name, "projection name")
        scope = None if projection_scope is None else validate_projection_scope(projection_scope)
        with self._lock:
            self._ensure_open()
            try:
                if scope is None:
                    row = self._db.execute("SELECT state,revision FROM projections WHERE session_id=? AND name=?", (session_id, name)).fetchone()
                    if name == _EXPERIENCE_PROJECTION and self._db.execute(
                            "SELECT 1 FROM experience_sessions WHERE session_id=?", (session_id,)).fetchone():
                        entries, revision = self.experience_entries(session_id)
                        if revision == 0:
                            return None
                        # 首次迁移可能发生在上面的旧行读取之后；迁移后基线已冻结。
                        row = self._db.execute("SELECT state,revision FROM projections WHERE session_id=? AND name=?",
                                               (session_id, name)).fetchone()
                        state = _decode(row["state"], "projection") if row else {"version": 1}
                        if not isinstance(state, dict) or state.get("version") != 1:
                            raise EventLogSchemaError("经历投影格式无效或版本不兼容")
                        return {"state": {**state, "entries": entries}, "revision": revision}
                else:
                    row = self._db.execute("SELECT state,revision FROM scoped_projections WHERE scope=? AND name=?", (scope, name)).fetchone()
                return None if row is None else {"state": _decode(row["state"], "projection"), "revision": row["revision"]}
            except sqlite3.Error as exc:
                raise EventLogError("读取投影失败，数据库可能损坏") from exc
