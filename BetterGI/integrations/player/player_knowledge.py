#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来自屏幕文字的保守知识解锁，不把模型推理当作事实。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from player_io import atomic_write_json, read_json, utc_now_iso
from shadow_recorder import redact_secrets

_EVIDENCE_ID = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    """观察证据不可验证或与当前帧不匹配。"""


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _normalize_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


class EvidenceStore:
    """保存可信屏幕识别适配器提交的 OCR 摘要；不保存原始图片。"""

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    def record_screen_ocr(
        self, *, session_id: str, frame_id: str, frame_bytes: bytes,
        observed_at_ms: int, text: str, confidence: Optional[float],
    ) -> Dict[str, Any]:
        """此方法只供直接处理捕获帧的本地 OCR 适配器调用。"""
        if not isinstance(session_id, str) or not session_id.strip():
            raise EvidenceError("session_id 不能为空")
        if not isinstance(frame_id, str) or not frame_id.strip() or len(frame_id) > 128:
            raise EvidenceError("frame_id 无效")
        if not isinstance(frame_bytes, bytes) or not frame_bytes:
            raise EvidenceError("必须提供对应帧的原始图像字节")
        if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms < 0:
            raise EvidenceError("观察时间无效")
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
            raise EvidenceError("OCR 置信度无效")
        cleaned = _normalize_text(text)
        if not cleaned or len(cleaned) > 500 or redact_secrets(cleaned) != cleaned:
            raise EvidenceError("屏幕文字为空、过长或包含敏感信息")
        payload = {
            "schema_version": 1,
            "kind": "screen_ocr",
            "session_id": session_id.strip(),
            "frame_id": frame_id.strip(),
            "frame_sha256": hashlib.sha256(frame_bytes).hexdigest(),
            "observed_at_ms": observed_at_ms,
            "text": cleaned,
            "confidence": float(confidence) if confidence is not None else None,
        }
        evidence_id = hashlib.sha256(_canonical(payload)).hexdigest()
        target = self.directory / (evidence_id + ".json")
        record = {**payload, "evidence_id": evidence_id}
        if target.exists():
            if self.get(evidence_id) != record:
                raise EvidenceError("证据文件冲突或已损坏")
        else:
            atomic_write_json(target, record)
        return record

    def get(self, evidence_id: str) -> Optional[Dict[str, Any]]:
        """验证内容哈希后返回；文件不可信或损坏时返回空。"""
        if not isinstance(evidence_id, str) or not _EVIDENCE_ID.fullmatch(evidence_id):
            return None
        try:
            record = read_json(self.directory / (evidence_id + ".json"))
            if not isinstance(record, dict) or record.get("evidence_id") != evidence_id:
                return None
            payload = {key: value for key, value in record.items() if key != "evidence_id"}
            if hashlib.sha256(_canonical(payload)).hexdigest() != evidence_id:
                return None
            if payload.get("schema_version") != 1 or payload.get("kind") != "screen_ocr":
                return None
            return record
        except (OSError, ValueError, TypeError, UnicodeError):
            return None


class KnowledgeGate:
    """将精确的当帧 OCR 引文写入玩家知识；不推断其客观真假。"""

    def __init__(self, evidence_store: EvidenceStore, semantic_store: Any):
        self.evidence_store = evidence_store
        self.semantic_store = semantic_store

    def unlock_quote(
        self, *, evidence_id: str, quote: str, session_id: str,
        frame_id: str, frame_sha256: str, now_ms: int,
    ) -> Dict[str, Any]:
        evidence = self.evidence_store.get(evidence_id)
        if evidence is None:
            raise EvidenceError("证据不存在或校验失败")
        if evidence.get("session_id") != session_id or evidence.get("frame_id") != frame_id:
            raise EvidenceError("证据不属于当前会话和帧")
        if evidence.get("frame_sha256") != frame_sha256:
            raise EvidenceError("证据与当前帧内容不一致")
        observed_at = evidence.get("observed_at_ms")
        if not isinstance(observed_at, int) or not 0 <= now_ms - observed_at <= 180000:
            raise EvidenceError("证据已过期或来自未来")
        clean_quote = _normalize_text(quote)
        if len(clean_quote) < 4 or len(clean_quote) > 200 or clean_quote not in evidence.get("text", ""):
            raise EvidenceError("候选引文未在屏幕证据中逐字出现")
        fact, _created = self.semantic_store.add({
            "proposition": "屏幕OCR（未校对）：" + clean_quote,
            "source": "screen_ocr:" + evidence_id,
            "evidence_id": evidence_id,
            "evidence_quote": clean_quote,
            "learned_at": utc_now_iso(observed_at),
            "learned_ms": observed_at,
            "confidence": evidence["confidence"] if evidence["confidence"] is not None else 0.0,
            "visibility": "player_knowledge",
            "tags": ["屏幕文字"],
        })
        return fact

    def permits(self, fact: Dict[str, Any]) -> bool:
        """事实必须可回溯到原始 OCR 摘要，且只能表达逐字引文。"""
        if not isinstance(fact, dict) or fact.get("visibility") != "player_knowledge":
            return False
        evidence_id = fact.get("evidence_id")
        evidence = self.evidence_store.get(evidence_id)
        if evidence is None:
            return False
        quote = _normalize_text(fact.get("evidence_quote"))
        return bool(
            4 <= len(quote) <= 200
            and quote in evidence.get("text", "")
            and fact.get("source") == "screen_ocr:" + evidence_id
            and fact.get("proposition") == "屏幕OCR（未校对）：" + quote
        )
