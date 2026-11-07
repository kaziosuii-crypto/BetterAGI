#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BetterGI 的可选屏幕观察；OCR 与返回图像始终来自同一次截图。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import time
import urllib.parse
from typing import Any, Dict, Mapping, Optional

from player_io import iso_to_epoch_ms
from player_knowledge import EvidenceError, EvidenceStore

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None


def validate_local_pair(ws_url: str, http_url: str) -> None:
    """OCR 与 Live 必须指向同一台 loopback 服务和端口。"""
    ws = urllib.parse.urlsplit(ws_url)
    http = urllib.parse.urlsplit(http_url)
    loopback = {"127.0.0.1", "localhost", "::1"}
    if ws.hostname not in loopback or http.hostname not in loopback:
        raise ValueError("OCR 和 Live 必须都连接本机 BetterGI")
    if (ws.scheme, http.scheme) not in (("ws", "http"), ("wss", "https")):
        raise ValueError("OCR 和 Live 的协议不匹配")
    if (ws.port or (443 if ws.scheme == "wss" else 80)) != (http.port or (443 if http.scheme == "https" else 80)):
        raise ValueError("OCR 和 Live 的端口不一致")


def _pixel_roi(roi: Any) -> Dict[str, int]:
    """校验原始截图像素矩形，避免调用方后续修改配置。"""
    if not isinstance(roi, dict) or set(roi) != {"x", "y", "width", "height"}:
        raise ValueError("OCR ROI 必须恰有 x、y、width、height")
    if any(type(roi[key]) is not int for key in ("x", "y", "width", "height")):
        raise ValueError("OCR ROI 坐标必须为整数像素")
    x, y, width, height = (roi[key] for key in ("x", "y", "width", "height"))
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 2147483647 or y + height > 2147483647:
        raise ValueError("OCR ROI 的坐标或尺寸无效")
    return dict(roi)


def _score(value: Any) -> Optional[float]:
    """保留有限原始分数；缺失或非法分数为未知。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        score = float(value)
    except OverflowError:
        return None
    return score if math.isfinite(score) else None


def _valid_named_candidate(row: Any, roi: Dict[str, int], width: int, height: int) -> bool:
    """核验原帧像素框、归一化框和其声称的具名区域一致。"""
    if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"].strip():
        return False
    x, y, w, h = (row.get(key) for key in ("x", "y", "width", "height"))
    if (any(type(n) is not int for n in (x, y, w, h)) or w <= 0 or h <= 0
            or x < roi["x"] or y < roi["y"]
            or x + w > roi["x"] + roi["width"] or y + h > roi["y"] + roi["height"]):
        return False
    expected = ((x + w / 2) / width, (y + h / 2) / height, w / width, h / height)
    actual = (row.get(key) for key in ("normalizedX", "normalizedY", "normalizedWidth", "normalizedHeight"))
    return all((number := _score(value)) is not None
               and math.isclose(number, target, rel_tol=1e-7, abs_tol=1e-7)
               for value, target in zip(actual, expected))


class ScreenObserver:
    """只调用观察接口，不调用游戏输入、指导或解说接口。"""

    def __init__(self, base_url: str, token: str, session_id: str, evidence: EvidenceStore,
                 *, interval: float = 30.0, timeout: float = 15.0,
                 ocr_roi: Optional[Dict[str, int]] = None,
                 ocr_regions: Optional[Mapping[str, Dict[str, int]]] = None,
                 named_regions: Optional[Mapping[str, Dict[str, int]]] = None):
        """区域是原始截图像素；多区域取包围矩形，在一次请求中保持同帧。"""
        if sum(value is not None for value in (ocr_roi, ocr_regions, named_regions)) > 1:
            raise ValueError("单一区域、包围区域和具名区域不能同时配置")
        self._named_regions = None
        if named_regions is not None:
            if (not isinstance(named_regions, Mapping) or not named_regions
                    or not set(named_regions) <= {"dialogue_text", "speaker", "choice", "quest", "hud"}):
                raise ValueError("具名 OCR 区域无效")
            self._named_regions = {name: _pixel_roi(roi) for name, roi in named_regions.items()}
            rectangles = list(self._named_regions.values())
            for index, roi in enumerate(rectangles):
                for other in rectangles[:index]:
                    if (roi["x"] < other["x"] + other["width"] and other["x"] < roi["x"] + roi["width"]
                            and roi["y"] < other["y"] + other["height"] and other["y"] < roi["y"] + roi["height"]):
                        raise ValueError("具名 OCR 区域不能重叠")
        if ocr_regions is not None:
            if not isinstance(ocr_regions, Mapping) or not ocr_regions or not set(ocr_regions) <= {"dialogue_text", "speaker", "choice"}:
                raise ValueError("OCR 区域名称无效或为空")
            regions = [_pixel_roi(roi) for roi in ocr_regions.values()]
            x = min(roi["x"] for roi in regions)
            y = min(roi["y"] for roi in regions)
            right = max(roi["x"] + roi["width"] for roi in regions)
            bottom = max(roi["y"] + roi["height"] for roi in regions)
            self._ocr_roi = _pixel_roi({"x": x, "y": y, "width": right - x, "height": bottom - y})
        else:
            self._ocr_roi = _pixel_roi(ocr_roi) if ocr_roi is not None else None
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or parts.hostname not in ("127.0.0.1", "localhost", "::1") or parts.username or parts.password:
            raise ValueError("观察接口必须使用本机 loopback 地址，且不能含 URL 凭据")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("观察地址必须是服务根地址")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.evidence = evidence
        self.interval = max(0.5, float(interval))
        self.timeout = max(1.0, float(timeout))
        self._last = 0.0

    async def capture(self) -> Optional[Dict[str, Any]]:
        """限频获取视觉结果；失败向调用方返回异常，由调用方降级。"""
        if aiohttp is None:
            raise RuntimeError("需要 aiohttp 才能请求屏幕观察")
        now = time.monotonic()
        if self._last and now - self._last < self.interval:
            return None
        self._last = now
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        payload = {
            "sessionId": self.session_id,
            "includeUiState": True,
            "includeOcr": self._ocr_roi is not None or self._named_regions is not None,
            "includeImageBase64": True,
            "saveFrame": False,
            "format": "jpg",
            "maxWidth": 1280,
        }
        if self._ocr_roi is not None:
            payload["ocrRoi"] = dict(self._ocr_roi)
        if self._named_regions is not None:
            payload["ocrRegions"] = [{"name": name, "roi": dict(roi)} for name, roi in self._named_regions.items()]
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request("POST", self.base_url + "/api/v1/agent/observe", json=payload, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError("屏幕观察失败，HTTP {0}".format(response.status))
                data = await response.json()
        if not isinstance(data, dict) or data.get("sessionId") != self.session_id:
            raise ValueError("观察会话不匹配")
        frame_id = data.get("frameId")
        captured = iso_to_epoch_ms(data.get("capturedAtUtc"))
        image = data.get("imageBase64")
        if not isinstance(frame_id, str) or not frame_id or captured is None or not isinstance(image, str) or len(image) > 10_000_000:
            raise ValueError("观察响应缺少截图或时间")
        now_ms = int(time.time() * 1000)
        if not 0 <= now_ms - captured <= 15000:
            raise ValueError("观察截图已过期或来自未来")
        try:
            image_bytes = base64.b64decode(image, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("观察截图 Base64 无效") from exc
        if not image_bytes:
            raise ValueError("观察截图为空")
        vision = data.get("vision") if isinstance(data.get("vision"), dict) else {}
        # 未请求 OCR 的响应即使包含文字也不能作为证据。
        ocr = vision.get("ocr") if self._ocr_roi is not None and isinstance(vision.get("ocr"), list) else None
        image_info = data.get("image") if isinstance(data.get("image"), dict) else {}
        region_status = {}
        if self._named_regions is not None:
            width, height = image_info.get("originalWidth"), image_info.get("originalHeight")
            if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
                raise ValueError("具名 OCR 响应缺少原帧尺寸")
            results = data.get("ocrRegions")
            if not isinstance(results, list):
                raise ValueError("具名 OCR 响应缺失")
            by_name = {}
            for item in results:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                        or item["name"] not in self._named_regions or item["name"] in by_name
                        or item.get("frameId") != frame_id):
                    raise ValueError("具名 OCR 响应区域或帧不匹配")
                try:
                    roi = _pixel_roi(item.get("roi"))
                except ValueError as exc:
                    raise ValueError("具名 OCR 响应区域或帧不匹配") from exc
                if (roi != self._named_regions[item["name"]] or roi["x"] + roi["width"] > width
                        or roi["y"] + roi["height"] > height):
                    raise ValueError("具名 OCR 响应区域或帧不匹配")
                by_name[item["name"]] = item
            if set(by_name) != set(self._named_regions):
                raise ValueError("具名 OCR 响应不完整")
            ocr = []
            for name, item in by_name.items():
                candidates = item.get("candidates")
                raw_count = item.get("rawDetectionCount")
                completed = (item.get("completed") is True and item.get("status") == "Completed"
                             and type(raw_count) is int and raw_count >= 0
                             and isinstance(candidates, list) and len(candidates) <= raw_count
                             and all(_valid_named_candidate(row, self._named_regions[name], width, height)
                                     for row in candidates))
                region_status[name] = {"completed": completed, "status": item.get("status"),
                                       "raw_detection_count": raw_count,
                                       "roi": dict(self._named_regions[name])}
                if completed:
                    ocr.extend({**row, "region": name} for row in candidates)
        records = []
        # 任一行无法记录时不能把过滤后的空列表当成明确空 OCR。
        ocr_complete = ocr is not None and (self._named_regions is None or all(
            item["completed"] for item in region_status.values()
        ))
        for row in ocr or []:
            if not isinstance(row, dict):
                ocr_complete = False
                continue
            recognition_score = _score(row.get("recognitionScore"))
            detection_score = _score(row.get("detectionScore"))
            # EvidenceStore 的 confidence 约定为 0–1，不把缺失或越界值当置信度。
            confidence = recognition_score if recognition_score is not None and 0 <= recognition_score <= 1 else None
            try:
                evidence = self.evidence.record_screen_ocr(
                    session_id=self.session_id, frame_id=frame_id, frame_bytes=image_bytes,
                    observed_at_ms=captured, text=row.get("text"), confidence=confidence,
                )
            except (EvidenceError, OSError):
                ocr_complete = False
                if row.get("region") in region_status:
                    region_status[row["region"]]["completed"] = False
                continue
            records.append({
                "evidence_id": evidence["evidence_id"], "text": evidence["text"],
                "confidence": confidence,
                "recognition_score": recognition_score, "detection_score": detection_score,
                **({"region": row["region"]} if isinstance(row.get("region"), str) else {}),
                # 像素框属于原始识别帧，并非可能缩放后的返回图；归一化值便于 ROI 匹配。
                "bbox": {key: row.get(key) for key in ("x", "y", "width", "height")},
                "normalized_bbox": {key: row.get("normalized" + key.capitalize())
                                    for key in ("x", "y", "width", "height")},
            })
        return {
            "session_id": self.session_id,
            "frame_id": frame_id,
            "frame_sha256": hashlib.sha256(image_bytes).hexdigest(),
            "captured_at_ms": captured,
            "source_verified": True,
            "image_b64": image,
            "mime_type": image_info.get("mimeType") or "image/jpeg",
            "width": image_info.get("width"),
            "height": image_info.get("height"),
            "original_width": image_info.get("originalWidth"),
            "original_height": image_info.get("originalHeight"),
            "ui_category": vision.get("uiCategory"),
            "is_in_talk_ui": vision.get("isInTalkUi") if isinstance(vision.get("isInTalkUi"), bool) else None,
            "ocr_requested": self._ocr_roi is not None or self._named_regions is not None,
            "ocr_complete": ocr_complete,
            # 旧 OCR API 没有原始检测数量，空列表不能证明区域没有检测。
            "ocr_empty_verified": self._named_regions is not None and ocr_complete,
            "ocr_regions": region_status,
            "ocr_roi": dict(self._ocr_roi) if self._ocr_roi is not None else None,
            # None 表示未提供 OCR，[] 表示已执行但未返回有效行。
            "ocr": records if ocr is not None else None,
        }
