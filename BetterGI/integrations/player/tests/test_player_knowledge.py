from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_knowledge import EvidenceError, EvidenceStore, KnowledgeGate  # noqa: E402
from player_legacy_import import quarantine_legacy_file  # noqa: E402
from player_retrieval import BeliefLinks, CognitiveContext, MemoryRetriever  # noqa: E402
from player_semantic_memory import SemanticStore  # noqa: E402
from player_vision import ScreenObserver  # noqa: E402


def test_legacy_import_is_idempotent_sanitized_and_isolated(tmp_path: Path) -> None:
    source = tmp_path / "old.json"
    source.write_text(json.dumps({"scene": "旧剧情", "apiKey": "secret-value", "plot": "token=secret123"}, ensure_ascii=False), encoding="utf-8")
    old_bytes = source.read_bytes()
    path, created = quarantine_legacy_file(source, tmp_path / "quarantine")
    assert created and source.read_bytes() == old_bytes
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["visibility"] == "quarantined"
    assert "secret-value" not in path.read_text(encoding="utf-8")
    assert "secret123" not in path.read_text(encoding="utf-8")
    assert quarantine_legacy_file(source, tmp_path / "quarantine") == (path, False)
    assert SemanticStore(tmp_path / "facts.json").list() == []


def test_evidence_gate_persists_quote_and_retrieves_after_restart(tmp_path: Path) -> None:
    evidence_store = EvidenceStore(tmp_path / "evidence")
    semantic = SemanticStore(tmp_path / "facts.json", clock=lambda: 100000)
    gate = KnowledgeGate(evidence_store, semantic)
    image = b"the actual observation image"
    evidence = evidence_store.record_screen_ocr(
        session_id="s-shadow", frame_id="unsaved-1", frame_bytes=image,
        observed_at_ms=100000, text="冒险家协会欢迎你", confidence=None,
    )
    fact = gate.unlock_quote(
        evidence_id=evidence["evidence_id"], quote="冒险家协会欢迎你", session_id="s-shadow",
        frame_id="unsaved-1", frame_sha256=hashlib.sha256(image).hexdigest(), now_ms=101000,
    )
    assert fact["proposition"] == "屏幕OCR（未校对）：冒险家协会欢迎你"
    assert fact["confidence"] == 0.0
    restarted_gate = KnowledgeGate(EvidenceStore(tmp_path / "evidence"), SemanticStore(tmp_path / "facts.json"))
    retriever = MemoryRetriever(semantic_store=restarted_gate.semantic_store, knowledge_gate=restarted_gate, clock=lambda: 101000)
    recalled = retriever.retrieve("冒险家协会", now_ms=101000)
    assert len(recalled) == 1
    assert recalled[0].source.startswith("fact:")
    assert recalled[0].time and recalled[0].score > 0
    assert "冒险家协会欢迎你" in recalled[0].text


def test_gate_rejects_wrong_frame_wrong_session_inference_and_expiry(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence")
    gate = KnowledgeGate(store, SemanticStore(tmp_path / "facts.json"))
    image = b"image"
    ev = store.record_screen_ocr(session_id="s", frame_id="f", frame_bytes=image, observed_at_ms=100000, text="骑士团正在开会", confidence=None)
    args = dict(evidence_id=ev["evidence_id"], quote="骑士团正在开会", session_id="s", frame_id="f", frame_sha256=hashlib.sha256(image).hexdigest(), now_ms=100001)
    for changes in ({"quote": "骑士团阴谋夺权"}, {"session_id": "other"}, {"frame_id": "another"}, {"frame_sha256": "0" * 64}, {"now_ms": 280001}, {"now_ms": 99999}):
        with pytest.raises(EvidenceError):
            gate.unlock_quote(**(args | changes))
    assert not (tmp_path / "facts.json").exists()


def test_tampered_evidence_and_unverified_legacy_fact_never_recalled(tmp_path: Path) -> None:
    evidence = EvidenceStore(tmp_path / "evidence")
    semantic = SemanticStore(tmp_path / "facts.json", clock=lambda: 100000)
    semantic.add({"proposition": "最终反派是某角色", "source": "director:legacy"})
    gate = KnowledgeGate(evidence, semantic)
    retriever = MemoryRetriever(semantic_store=semantic, knowledge_gate=gate, clock=lambda: 100000)
    assert retriever.retrieve("最终反派") == []
    ev = evidence.record_screen_ocr(session_id="s", frame_id="f", frame_bytes=b"x", observed_at_ms=100000, text="当前任务继续前进", confidence=None)
    fact = gate.unlock_quote(evidence_id=ev["evidence_id"], quote="当前任务继续前进", session_id="s", frame_id="f", frame_sha256=hashlib.sha256(b"x").hexdigest(), now_ms=100001)
    assert gate.permits(fact)
    context = CognitiveContext(beliefs=[BeliefLinks(
        "b", support_sources=["fact:" + fact["fact_id"]], relevance=1.0
    )])
    assert retriever.retrieve("不相干查询", cognitive_context=context)[0].memory_id == fact["fact_id"]
    path = tmp_path / "evidence" / (ev["evidence_id"] + ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["text"] = "恶意修改的内容"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert not gate.permits(fact)
    assert retriever.retrieve("当前任务") == []
    assert retriever.retrieve("不相干查询", cognitive_context=context) == []


def test_screen_observer_uses_matching_observation_image_and_ocr(tmp_path: Path) -> None:
    async def scenario():
        requests = []
        image = b"jpeg-bytes"
        async def observe(request):
            requests.append((request.method, request.path, await request.json()))
            from datetime import datetime, timezone
            return web.json_response({
                "sessionId": "vtuber-shadow", "frameId": "unsaved-test",
                "capturedAtUtc": datetime.now(timezone.utc).isoformat(),
                "imageBase64": base64.b64encode(image).decode(),
                "image": {"mimeType": "image/jpeg"},
                "vision": {"uiCategory": "Talk", "ocr": [{"text": "冒险家协会欢迎你"}]},
            })
        app = web.Application()
        app.router.add_route("*", "/api/v1/agent/observe", observe)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            store = EvidenceStore(tmp_path / "evidence")
            observer = ScreenObserver(f"http://127.0.0.1:{port}", "test", "vtuber-shadow", store,
                                      ocr_roi={"x": 0, "y": 0, "width": 400, "height": 200})
            result = await observer.capture()
            assert await observer.capture() is None
            assert result["image_b64"] == base64.b64encode(image).decode()
            assert result["frame_id"] == "unsaved-test"
            assert result["ocr"][0]["text"] == "冒险家协会欢迎你"
            stored = store.get(result["ocr"][0]["evidence_id"])
            assert stored["confidence"] is None
            assert stored["frame_sha256"] == hashlib.sha256(image).hexdigest()
            assert requests[0][0:2] == ("POST", "/api/v1/agent/observe")
            assert requests[0][2]["saveFrame"] is False
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_observer_refuses_nonlocal_or_credential_urls(tmp_path: Path) -> None:
    for url in ("http://example.com", "http://user:secret@localhost:30648", "http://127.0.0.1:30648/path"):
        with pytest.raises(ValueError):
            ScreenObserver(url, "token", "s", EvidenceStore(tmp_path))
