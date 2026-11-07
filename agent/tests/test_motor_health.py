"""手模型 /health 探针：服务在、模型名对、能推理 是三件事（2026-11-04）。

只查 available 会把「--served-model-name 写错」误判成健康：vLLM 对 /v1/models
回 200，但所有 /v1/chat/completions 回 404。这里用假 httpx 把四种情形都钉住。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import motor


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    routes: Dict[str, Any] = {}
    seen: List[str] = []

    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> _FakeResponse:
        type(self).seen.append(url)
        path = url.split("127.0.0.1:18041", 1)[-1]
        value = type(self).routes.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return _FakeResponse(404)
        return value


def _models(*ids: str) -> _FakeResponse:
    return _FakeResponse(200, "{\"data\":[]}", {"data": [{"id": i} for i in ids]})


def _patch(monkeypatch, routes: Dict[str, Any]) -> None:
    _FakeClient.routes = routes
    _FakeClient.seen = []
    monkeypatch.setattr(motor.httpx, "Client", _FakeClient)


def test_health_ok_reports_model_and_ready(monkeypatch) -> None:
    _patch(monkeypatch, {"/v1/models": _models("gamescaling"), "/health": _FakeResponse(200)})
    out = motor.GamescalingClient(endpoint="http://127.0.0.1:18041").health()
    assert out["available"] is True
    assert out["model_ok"] is True
    assert out["ready"] is True
    assert out["model_ids"] == ["gamescaling"]
    assert "hint" not in out


def test_health_model_name_mismatch_is_not_healthy(monkeypatch) -> None:
    _patch(monkeypatch, {"/v1/models": _models("other-model"), "/health": _FakeResponse(200)})
    out = motor.GamescalingClient(endpoint="http://127.0.0.1:18041").health()
    assert out["available"] is True
    assert out["model_ok"] is False
    assert "served-model-name" in out["hint"]


def test_health_unreachable_explains_how_to_fix(monkeypatch) -> None:
    _patch(monkeypatch, {"/v1/models": ConnectionError("refused")})
    out = motor.GamescalingClient(endpoint="http://127.0.0.1:18041").health()
    assert out["available"] is False
    assert out["ready"] is False
    assert out["model_ids"] == []
    assert "2B-S3" in out["hint"]
    assert "ConnectionError" in out["error"]


def test_health_without_ready_probe(monkeypatch) -> None:
    _patch(monkeypatch, {"/v1/models": _models("gamescaling")})
    out = motor.GamescalingClient(endpoint="http://127.0.0.1:18041").health(probe_ready=False)
    assert out["ready"] is None
    assert not any(u.endswith("/health") for u in _FakeClient.seen)


def test_health_ready_probe_error_does_not_flip_available(monkeypatch) -> None:
    _patch(monkeypatch, {"/v1/models": _models("gamescaling"),
                          "/health": ConnectionError("boom")})
    out = motor.GamescalingClient(endpoint="http://127.0.0.1:18041").health()
    assert out["available"] is True
    assert out["ready"] is None
    assert "ready_error" in out
