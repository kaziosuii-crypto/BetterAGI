#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显式、一次性的旧玩家 JSON 数据导入；不读取、修改或删除源文件。

可信调用方提供每个来源的完整、未经有损规范化的已解码 JSON 对象及路径，
并为每个来源声明 player_scope。路径和所有权是调用方的声明，不是认证或
文件内容证明；调用方必须拒绝重复 JSON 键和解析失败，不能用旧 Store 自动
恢复得到的空默认值。归档摘要针对传入 JSON 的规范化 UTF-8 表示，而不是
源文件的原始字节。新归档只存已知字段及摘要，不存来源路径；不能安全保留的
扩展和历史会被拒绝。旧归档只读兼容，不自动清除其中原有数据。自由文本的
密钥检测并不完备，调用方仍须确保输入不含凭据。事件仅在日志 API 层追加，
直接改写数据库不受此保护。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from copy import deepcopy
from typing import Any

try:
    from . import player_beliefs as beliefs, player_emotion as emotion
    from . import player_relationships as relationships, player_threads as threads
    from . import player_working_memory as working
    from .player_cognitive_transaction import (
        CognitiveTransactionError, PlayerCognitiveTransaction, PROJECTION, SESSION_BINDING,
        VERIFICATION, VISIBILITY, _empty, _required, _state_valid,
    )
    from .player_event_log import EventLogConflict, _SECRET_TEXT, validate_projection_scope
    from .player_io import clean_text, utc_now_iso
    from .player_profile import is_secret_key
    from .shadow_recorder import redact_secrets
except ImportError:
    import player_beliefs as beliefs
    import player_emotion as emotion
    import player_relationships as relationships
    import player_threads as threads
    import player_working_memory as working
    from player_cognitive_transaction import (
        CognitiveTransactionError, PlayerCognitiveTransaction, PROJECTION, SESSION_BINDING,
        VERIFICATION, VISIBILITY, _empty, _required, _state_valid,
    )
    from player_event_log import EventLogConflict, _SECRET_TEXT, validate_projection_scope
    from player_io import clean_text, utc_now_iso
    from player_profile import is_secret_key
    from shadow_recorder import redact_secrets


_RECEIPT = "cognitive.legacy_import.v1"
_NAMES = {"working", "beliefs", "relationships", "emotion", "threads"}
_FORBIDDEN_PARTS = {"world", "secret", "secrets", "canonical", "objective", "groundtruth"}
_IDENTITY_KEYS = {"player_scope", "player_id", "owner", "owner_id", "owner_scope"}
# 在现有敏感键规则之外覆盖常见缩写；未知扩展也递归检查，拒绝而不回显键名。
_SENSITIVE_KEY_PARTS = {"pwd", "passwd", "password", "passphrase", "credential", "credentials",
                        "authorization", "bearer", "token", "secret"}
_SENSITIVE_KEY_COMPACT = ("apikey", "accesskey", "privatekey", "secretkey", "clientsecret",
                          "accesstoken", "refreshtoken", "password", "passwd", "passphrase",
                          "credential", "authorization", "pwd")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?<![a-z0-9_])(?:pwd|passwd|passphrase|credentials?|password|token|api[_-]?key|secret)"
    r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+"
    r"|\b(?:https?|wss?|ftp)://[^/\s:@]+:[^/\s@]+@"
)
_ABSOLUTE_PATH = re.compile(r"(?i)(?:\b[a-z]:[\\/]|\\\\[^\\\s]+\\|file://|/(?:home|users|root|etc|var|tmp|mnt|opt)/)")

# 仅归档已定义的旧字段；完整原始数据及路径只参与摘要，绝不直接写入事件。
_RECORD_FIELDS = {
    "beliefs": {"schema_version", "proposition", "belief_id", "fingerprint", "status", "confidence",
                "evidence", "contradictions", "history", "first_formed_ms", "last_updated_ms"},
    "relationships": {"schema_version", "character", "name", "character_id", *relationships.DIMENSIONS,
                      "memorable_events", "impressions", "unresolved", "last_updated_ms"},
    "threads": {"schema_version", "title", "thread", "thread_id", "fingerprint", "status", "importance",
                "characters", "topics", "evidence", "current_theory", "first_seen_ms", "last_referenced_ms",
                "status_history", "source_event_ids", "last_activation"},
}
_HISTORY_FIELDS = {"from_status", "to_status", "time_ms", "reason", "event_id"}
_ACTIVATION_FIELDS = {"time_ms", "reason", "event_id"}


def _sensitive_key(key: str) -> bool:
    """对各层键名进行保守匹配，不依赖未知扩展的字段约定。"""
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", split).lower()
    parts = set(filter(None, re.split(r"[^a-z0-9]+", split)))
    compact = re.sub(r"[^a-z0-9]", "", split)
    return (is_secret_key(key) or bool(parts & _SENSITIVE_KEY_PARTS)
            or compact.endswith(("token", "secret"))
            or any(part in compact for part in _SENSITIVE_KEY_COMPACT))


def _safe_persisted_text(value: Any) -> None:
    """拒绝已知疑似密钥或绝对路径；自由文本不能保证穷尽所有秘密。"""
    if isinstance(value, str):
        cleaned = clean_text(value)
        if (_SECRET_TEXT.search(value) or _SENSITIVE_TEXT.search(value)
                or redact_secrets(cleaned) != cleaned or _ABSOLUTE_PATH.search(value)):
            raise CognitiveTransactionError("旧数据的已知字段含疑似密钥或绝对路径")
    elif isinstance(value, list):
        for item in value:
            _safe_persisted_text(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _safe_persisted_text(key)
            _safe_persisted_text(item)


def _canonical(value: Any) -> str:
    """用确定性 JSON 表示计算摘要，并拒绝非 JSON/非 UTF-8 数据。"""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        text.encode("utf-8")
        return text
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise CognitiveTransactionError("旧数据不是有效的 UTF-8 JSON") from exc


def _inspect(value: Any, player_scope: str) -> None:
    """递归拒绝结构性世界/秘密数据及不一致的玩家身份声明。"""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise CognitiveTransactionError("旧数据含非有限数字")
        return
    if isinstance(value, list):
        for item in value:
            _inspect(item, player_scope)
        return
    if not isinstance(value, dict):
        raise CognitiveTransactionError("旧数据必须是 JSON 类型")
    for key, item in value.items():
        if not isinstance(key, str):
            raise CognitiveTransactionError("旧数据字段名必须是字符串")
        if _sensitive_key(key):
            raise CognitiveTransactionError("旧数据包含疑似敏感字段")
        split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
        parts = set(filter(None, re.split(r"[^a-z0-9]+", split)))
        compact = re.sub(r"[^a-z0-9]", "", split)
        if parts & _FORBIDDEN_PARTS or any(x in compact for x in ("groundtruth", "worldtruth", "worldonly")):
            raise CognitiveTransactionError("旧数据包含 world/secret 字段")
        if split in _IDENTITY_KEYS and item != player_scope:
            raise CognitiveTransactionError("旧数据中的玩家身份与目标不一致")
        if split in {"verified", "is_verified"} and item is not False:
            raise CognitiveTransactionError("旧数据不能声明已核实")
        if split in {"visibility", "verification", "authority", "scope", "kind", "type", "status"} and isinstance(item, str):
            claim = re.sub(r"[^a-z0-9]", "", item.lower())
            if claim in {"world", "worldonly", "worldtruth", "groundtruth", "canonical", "objective", "verified", "secret"}:
                raise CognitiveTransactionError("旧数据不能声明世界真相或秘密")
        _inspect(item, player_scope)


def _version(obj: dict, label: str, *, working_store: bool = False) -> None:
    """只接受旧 store 实际支持的版本，缺失版本不可推定。"""
    version = obj.get("schema_version")
    if type(version) is not int or version not in ((0, 1) if working_store else (1,)):
        raise CognitiveTransactionError(f"{label} schema_version 无效或不受支持")


def _text_fields(obj: dict, fields: dict[str, int]) -> None:
    """拒绝会被旧规范化器截断或静默丢弃的文本。"""
    for name, limit in fields.items():
        if name in obj and (not isinstance(obj[name], str) or len(obj[name]) > limit):
            raise CognitiveTransactionError(f"{name} 必须是长度不超过 {limit} 的字符串")


def _lists(obj: dict, fields: dict[str, tuple[int, int]]) -> None:
    """检查列表元素和容量，避免旧规范化器静默过滤损坏数据。"""
    for name, (limit, size) in fields.items():
        if name not in obj:
            continue
        value = obj[name]
        if (not isinstance(value, list) or len(value) > limit
                or any(not isinstance(item, str) or not item.strip() or len(item) > size for item in value)):
            raise CognitiveTransactionError(f"{name} 不是有效字符串列表")
        cleaned = [" ".join(item.split()) for item in value]
        if len(set(cleaned)) != len(cleaned):
            raise CognitiveTransactionError(f"{name} 含重复项")


def _numbers(obj: dict, names: Any, *, ratios: bool = False) -> None:
    """不把坏数字、布尔值或超范围数值修正为默认值。"""
    for name in names:
        if name not in obj:
            continue
        value = obj[name]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or (ratios and not 0 <= value <= 1)):
            raise CognitiveTransactionError(f"{name} 数值无效")


def _times(obj: dict, names: Any) -> None:
    """保留已有时间；缺失时只使用零作为未知时间。"""
    for name in names:
        if name in obj and (type(obj[name]) is not int or not 0 <= obj[name] <= 2**63 - 1):
            raise CognitiveTransactionError(f"{name} 时间无效")


def _working(data: dict) -> dict:
    """导入工作记忆已知字段；不可安全保留的扩展会被拒绝。"""
    _text_fields(data, {"updated_at": 40, "location": 200, "current_activity": 240, "why": 300})
    _lists(data, {"characters": (12, 40), "open_questions": (10, 160), "short_plan": (8, 160)})
    for name, limit in (("dialogue", 20), ("recent_events", 20)):
        if name not in data:
            continue
        rows = data[name]
        if not isinstance(rows, list) or len(rows) > limit:
            raise CognitiveTransactionError(f"{name} 列表无效")
        for row in rows:
            if isinstance(row, str):
                if not row.strip() or len(row) > (300 if name == "dialogue" else 240):
                    raise CognitiveTransactionError(f"{name} 内容无效")
                continue
            if not isinstance(row, dict):
                raise CognitiveTransactionError(f"{name} 条目无效")
            _text_fields(row, {"text": 300 if name == "dialogue" else 240,
                               "what": 240, "speaker": 60, "emotion": 30, "kind": 30})
            text = row.get("text") or (row.get("what") if name == "recent_events" else "")
            if not isinstance(text, str) or not text.strip():
                raise CognitiveTransactionError(f"{name} 内容为空")
            if (name == "recent_events" and row.get("text") and row.get("what")
                    and row["text"] != row["what"]):
                raise CognitiveTransactionError("recent_events 内容冲突")
            _times(row, ("t_ms",))
            _numbers(row, ("confidence",), ratios=True)
    if "emotion" in data:
        value = data["emotion"]
        if not isinstance(value, dict) or len(value) > 12 or any(not k.strip() or len(k) > 30 for k in value):
            raise CognitiveTransactionError("working.emotion 无效")
        _numbers(value, value, ratios=True)
    if "last_motor_feedback" in data and not isinstance(data["last_motor_feedback"], dict):
        raise CognitiveTransactionError("working.last_motor_feedback 无效")
    # 该字段允许任意嵌套扩展，无法保证安全无损地导入或归档。
    if data.get("last_motor_feedback"):
        raise CognitiveTransactionError("非空 motor feedback 无法安全迁移；原文件未改动")
    known = {key: deepcopy(data[key]) for key in working.default_working() if key in data}
    return working.normalize_working(known)


def _records(data: dict, name: str) -> list:
    """读取 store 记录；缺失列表为空，损坏列表不得默默跳过。"""
    value = data.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CognitiveTransactionError(f"{name} 必须是对象列表")
    return value


def _build(sources: dict, session_id: str, event_id: str) -> dict:
    """构建 v2 当前状态；所有旧历史仅作未核实内容，来源仍在归档。"""
    state = _empty()
    if "working" in sources:
        state["working"] = _working(sources["working"]["data"])
        state["working"].update(visibility=VISIBILITY, verification=VERIFICATION)
    if "emotion" in sources:
        data = sources["emotion"]["data"]
        _text_fields(data, {"updated_at": 40})
        _times(data, ("last_updated_ms",))
        _numbers(data, emotion.DIMENSIONS, ratios=True)
        state["emotion"].update({k: deepcopy(data[k]) for k in ("updated_at", "last_updated_ms", *emotion.DIMENSIONS) if k in data})
        state["emotion"].update(visibility=VISIBILITY, verification=VERIFICATION)

    seen = set()
    ids = set()
    for raw in _records(sources.get("beliefs", {}).get("data", {}), "beliefs"):
        _text_fields(raw, {"proposition": 500, "belief_id": 80, "fingerprint": 40, "status": 30})
        _required(raw.get("proposition"), "belief.proposition")
        if "schema_version" in raw:
            _version(raw, "belief")
        if "status" in raw and raw["status"] not in beliefs.STATUSES:
            raise CognitiveTransactionError("belief.status 无效")
        _numbers(raw, ("confidence",), ratios=True)
        _lists(raw, {"evidence": (8, 200), "contradictions": (8, 200)})
        if "history" in raw and (not isinstance(raw["history"], list) or
                                 any(not isinstance(x, str) or len(x) > 200 for x in raw["history"])):
            raise CognitiveTransactionError("belief.history 无效")
        _times(raw, ("first_formed_ms", "last_updated_ms"))
        item = beliefs.normalize_belief(raw, now_ms=0)
        fingerprint = beliefs._fingerprint(item["proposition"])
        if item["fingerprint"] != fingerprint or fingerprint in seen or item["belief_id"] in ids:
            raise CognitiveTransactionError("belief 身份冲突或指纹损坏")
        seen.add(fingerprint)
        ids.add(item["belief_id"])
        if item["status"] == "known_fact":
            item["status"] = "belief"
        item.update(visibility=VISIBILITY, verification=VERIFICATION, source_event_ids=[])
        state["beliefs"].append(item)

    seen = set()
    ids = set()
    for raw in _records(sources.get("relationships", {}).get("data", {}), "relationships"):
        _text_fields(raw, {"character": 500, "name": 500, "character_id": 60})
        if raw.get("character") and raw.get("name") and relationships._identity(raw["character"]) != relationships._identity(raw["name"]):
            raise CognitiveTransactionError("relationship 名称冲突")
        _required(raw.get("character") or raw.get("name"), "relationship.character")
        if "schema_version" in raw:
            _version(raw, "relationship")
        _numbers(raw, relationships.DIMENSIONS, ratios=True)
        _lists(raw, {"memorable_events": (12, 240), "impressions": (10, 200), "unresolved": (8, 200)})
        _times(raw, ("last_updated_ms",))
        item = relationships.normalize_relationship({**raw, "last_updated_ms": raw.get("last_updated_ms", 0)})
        identity = relationships._identity(item["character"])
        if "character_id" in raw:
            item["character_id"] = relationships._stored_id(raw["character_id"])
            if not item["character_id"]:
                raise CognitiveTransactionError("relationship.character_id 无效")
        if identity in seen or item["character_id"] in ids:
            raise CognitiveTransactionError("relationship 身份冲突")
        seen.add(identity)
        ids.add(item["character_id"])
        item.update(visibility=VISIBILITY, verification=VERIFICATION)
        state["relationships"].append(item)

    seen = set()
    ids = set()
    for raw in _records(sources.get("threads", {}).get("data", {}), "threads"):
        _text_fields(raw, {"title": 240, "thread": 240, "thread_id": 80, "fingerprint": 40,
                           "status": 20, "current_theory": 400})
        if raw.get("title") and raw.get("thread") and threads._text(raw["title"], 240) != threads._text(raw["thread"], 240):
            raise CognitiveTransactionError("thread 标题冲突")
        _required(raw.get("title") or raw.get("thread"), "thread.title")
        if "schema_version" in raw:
            _version(raw, "thread")
        if "status" in raw and raw["status"] not in threads.STATUSES:
            raise CognitiveTransactionError("thread.status 无效")
        _numbers(raw, ("importance",), ratios=True)
        _lists(raw, {"characters": (8, 60), "topics": (8, 120), "evidence": (10, 200)})
        _times(raw, ("first_seen_ms", "last_referenced_ms"))
        if "status_history" in raw and (not isinstance(raw["status_history"], list)
                                        or any(not isinstance(entry, dict) for entry in raw["status_history"])):
            raise CognitiveTransactionError("thread.status_history 无效")
        if "source_event_ids" in raw and (not isinstance(raw["source_event_ids"], list)
                                          or any(not isinstance(entry, str) for entry in raw["source_event_ids"])):
            raise CognitiveTransactionError("thread.source_event_ids 无效")
        # 旧扩展/历史中的 event ID 无法证明来源，只保存在不可变归档中。
        fields = {"schema_version", "title", "thread", "thread_id", "fingerprint", "status", "importance",
                  "characters", "topics", "evidence", "current_theory", "first_seen_ms", "last_referenced_ms"}
        item = threads.normalize_thread({k: deepcopy(v) for k, v in raw.items() if k in fields}, now_ms=0)
        fingerprint = threads._fingerprint(item["title"])
        if item["fingerprint"] != fingerprint or fingerprint in seen or item["thread_id"] in ids:
            raise CognitiveTransactionError("thread 身份冲突或指纹损坏")
        seen.add(fingerprint)
        ids.add(item["thread_id"])
        item.update(visibility=VISIBILITY, verification=VERIFICATION, source_event_ids=[])
        state["threads"].append(item)
        state["thread_last_events"][item["thread_id"]] = {"session_id": session_id, "event_id": event_id}
    return state


def _history_entry(value: Any, fields: set[str]) -> dict:
    """完整保留已知历史项；未知或非标量历史拒绝，不能静默删改。"""
    if not isinstance(value, dict) or set(value) - fields:
        raise CognitiveTransactionError("旧历史包含无法安全归档的扩展；原文件未改动")
    for key, item in value.items():
        if key == "time_ms":
            if type(item) is not int or not 0 <= item <= 2**63 - 1:
                raise CognitiveTransactionError("旧历史时间无效")
        elif not isinstance(item, str):
            raise CognitiveTransactionError("旧历史字段类型无效")
    return deepcopy(value)


def _known_fields(value: dict, fields: set[str]) -> None:
    """未知扩展不能安全归档或投影，拒绝导入以免静默丢失用户数据。"""
    if set(value) - fields:
        raise CognitiveTransactionError("旧数据含无法安全迁移的扩展；原文件未改动")


def _archive(sources: dict) -> dict:
    """仅保留已知字段和完整历史；遇到未知扩展时保守拒绝。"""
    result = {}
    for name, source in sources.items():
        data = source["data"]
        if name == "working":
            fields = set(working.default_working())
        elif name == "emotion":
            fields = {"schema_version", "updated_at", "last_updated_ms", *emotion.DIMENSIONS}
        else:
            fields = {"schema_version", "updated_at", name}
        _known_fields(data, fields)
        _text_fields(data, {"updated_at": 40})
        saved = {key: deepcopy(value) for key, value in data.items() if key in fields}
        if name == "working":
            row_fields = {"text", "what", "speaker", "emotion", "kind", "t_ms", "confidence"}
            for field in ("dialogue", "recent_events"):
                for row in saved.get(field, []):
                    if isinstance(row, dict):
                        _known_fields(row, row_fields)
        elif name in _RECORD_FIELDS:
            saved[name] = []
            for raw in _records(data, name):
                _known_fields(raw, _RECORD_FIELDS[name])
                record = {key: deepcopy(value) for key, value in raw.items() if key in _RECORD_FIELDS[name]}
                if name == "threads":
                    if "status_history" in record:
                        record["status_history"] = [_history_entry(entry, _HISTORY_FIELDS)
                                                    for entry in record["status_history"]]
                    if "last_activation" in record:
                        record["last_activation"] = _history_entry(record["last_activation"], _ACTIVATION_FIELDS)
                saved[name].append(record)
        result[name] = saved
    _safe_persisted_text(result)
    return result


def import_legacy_player(*, transaction: PlayerCognitiveTransaction, session_id: str,
                         player_scope: str, sources: dict, timestamp: str | None = None) -> dict:
    """将显式传入的旧数据一次性导入空玩家投影。

    sources 为 {store名: {player_scope, path, data}}；store名为 working、
    beliefs、relationships、emotion、threads，可省略不存在的来源。data 是
    旧 JSON 的完整已解码对象，path 是可信调用方声明的来源路径。每个来源
    必须包含支持的 schema_version；损坏、身份冲突或世界/秘密数据均拒绝。
    先 bind_session；不同输入或其他已有认知状态不得覆盖。相同输入重复调用
    返回原收据，包含后续认知更新的当前投影；并发竞争以 CAS 安全失败。
    """
    if not isinstance(transaction, PlayerCognitiveTransaction):
        raise CognitiveTransactionError("transaction 必须是 PlayerCognitiveTransaction")
    session_id = _required(session_id, "session_id")
    player_scope = validate_projection_scope(player_scope)
    if not isinstance(sources, dict) or not sources or set(sources) - _NAMES:
        raise CognitiveTransactionError("sources 必须包含有效的旧 store")
    # 在读取/写入数据库前完成所有输入检查及快照复制。
    sources = deepcopy(sources)
    _canonical(sources)  # 先拒绝循环引用，避免递归检查时无法安全终止。
    paths = set()
    for name, source in sources.items():
        if not isinstance(source, dict) or set(source) != {"player_scope", "path", "data"}:
            raise CognitiveTransactionError(f"{name} 来源必须包含 player_scope、path 和 data")
        if source["player_scope"] != player_scope:
            raise CognitiveTransactionError("来源玩家与目标玩家不一致")
        path = _required(source["path"], "source.path")
        path_key = path.replace("\\", "/").casefold()
        if path_key in paths:
            raise CognitiveTransactionError("多个 store 的来源路径冲突")
        paths.add(path_key)
        data = source["data"]
        if not isinstance(data, dict):
            raise CognitiveTransactionError(f"{name} 数据必须是 JSON 对象")
        _version(data, name, working_store=name == "working")
        _inspect(data, player_scope)
    source_digests = {name: hashlib.sha256(_canonical(source["data"]).encode("utf-8")).hexdigest()
                      for name, source in sources.items()}
    digest = hashlib.sha256(_canonical({"player_scope": player_scope, "sources": sources}).encode("utf-8")).hexdigest()
    event_id = "cognitive-legacy-import-" + hashlib.sha256(player_scope.encode("utf-8")).hexdigest()
    state = _build(sources, session_id, event_id)
    if not _state_valid(state):
        raise CognitiveTransactionError("生成的认知状态无效")
    _safe_persisted_text(state)
    binding = transaction._binding(session_id, player_scope)
    log = transaction._log
    receipt = log.get_projection(session_id, _RECEIPT, projection_scope=player_scope)
    if receipt is not None:
        ref = receipt["state"]
        if (not isinstance(ref, dict) or set(ref) != {"event_id", "session_id", "player_scope", "digest"}
                or ref.get("event_id") != event_id or ref.get("player_scope") != player_scope
                or ref.get("digest") != digest or not isinstance(ref.get("session_id"), str)):
            raise CognitiveTransactionError("该玩家已有不同或损坏的迁移收据")
        prior = log.get_event(ref["session_id"], event_id)
        payload = prior.get("payload") if prior else None
        if (not prior or prior["seq"] != receipt["revision"] or prior["type"] != "cognitive.legacy_import"
                or prior["source"] != "player_legacy_migration" or not isinstance(payload, dict)
                or payload.get("player_scope") != player_scope or payload.get("digest") != digest
                or payload.get("source_digests") != source_digests
                or payload.get("thread_ids") != [x["thread_id"] for x in state["threads"]]):
            raise CognitiveTransactionError("迁移归档或收据无效")
        # 老事件不可重写：仅在完整输入与旧归档一致时只读返回。新事件严格校验白名单归档。
        if "archive_version" not in payload:
            if payload.get("sources") != sources:
                raise CognitiveTransactionError("旧迁移归档与输入不一致")
        elif (payload.get("archive_version") != 2 or type(payload.get("archive_version")) is not int
              or payload.get("sources") != _archive(sources)
              or payload.get("visibility") != VISIBILITY or payload.get("verification") != VERIFICATION
              or set(payload) != {"archive_version", "player_scope", "visibility", "verification",
                                  "digest", "source_digests", "sources", "thread_ids"}):
            raise CognitiveTransactionError("迁移归档或收据无效")
        transaction._binding(ref["session_id"], player_scope)
        snapshot = transaction.read(session_id, player_scope=player_scope)
        if snapshot["revision"] < prior["seq"]:
            raise CognitiveTransactionError("迁移后的认知投影缺失或损坏")
        return {"event_id": event_id, "seq": prior["seq"], "revision": snapshot["revision"],
                "state": snapshot["state"], "already_imported": True}
    if log.get_projection(session_id, PROJECTION, projection_scope=player_scope) is not None:
        # 并发导入可能在第一次收据读取之后提交。
        if log.get_projection(session_id, _RECEIPT, projection_scope=player_scope) is not None:
            return import_legacy_player(transaction=transaction, session_id=session_id,
                                        player_scope=player_scope, sources=sources, timestamp=timestamp)
        raise EventLogConflict("玩家认知投影非空，拒绝导入")
    timestamp = _required(timestamp if timestamp is not None else utc_now_iso(int(time.time() * 1000)), "timestamp")
    payload = {"archive_version": 2, "player_scope": player_scope,
               "visibility": VISIBILITY, "verification": VERIFICATION,
               "digest": digest, "source_digests": source_digests, "sources": _archive(sources),
               "thread_ids": [x["thread_id"] for x in state["threads"]]}
    event = {"event_id": event_id, "session_id": session_id, "timestamp": timestamp,
             "type": "cognitive.legacy_import", "source": "player_legacy_migration", "payload": payload}
    _safe_persisted_text(event)
    receipt_state = {"event_id": event_id, "session_id": session_id, "player_scope": player_scope, "digest": digest}
    try:
        seq = log.append([event], projections={PROJECTION: state, _RECEIPT: receipt_state},
                         projection_scope=player_scope, expected_revisions={PROJECTION: 0, _RECEIPT: 0},
                         expected_session_revisions={SESSION_BINDING: binding["revision"]})[0]
    except EventLogConflict:
        # 相同导入的并发胜者可能已提交；只有收据出现时才走只读幂等校验。
        if log.get_projection(session_id, _RECEIPT, projection_scope=player_scope) is None:
            raise
        return import_legacy_player(transaction=transaction, session_id=session_id,
                                    player_scope=player_scope, sources=sources, timestamp=timestamp)
    return {"event_id": event_id, "seq": seq, "revision": seq, "state": deepcopy(state), "already_imported": False}
