#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：假 BetterGI 观察接口 → shadow 一轮思考 → 引文解锁。"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import player_knowledge as knowledge_module  # noqa: E402
import player_semantic_memory as semantic_module  # noqa: E402
from player_agent import PersistentPlayerAgent  # noqa: E402
from player_context_builder import ContextBuilder  # noqa: E402
from player_episodic_memory import EpisodicStore  # noqa: E402
from player_memory_consolidator import MemoryConsolidator  # noqa: E402
from player_profile import load_profile  # noqa: E402
from player_retrieval import MemoryRetriever  # noqa: E402
from player_state import StateStore  # noqa: E402
from player_vision import ScreenObserver  # noqa: E402
from player_working_memory import WorkingMemoryStore  # noqa: E402
from shadow_recorder import ShadowRecorder  # noqa: E402

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "lumi.json"
IMAGE = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class FakeLLM:
    """像真实模型一样，从提示词里的 OCR 证据块引用 evidence_id。"""

    def __init__(self):
        self.calls = []

    async def complete(self, system_prompt, user_text, image_b64=None, media_type="image/jpeg"):
        self.calls.append({"system": system_prompt, "user": user_text, "image": image_b64})
        evidence_id = ""
        for line in user_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("· ["):
                evidence_id = stripped[3:].split("]")[0]
                break
        if not evidence_id:
            return json.dumps({"thought": "没有可引用的屏幕文字。"}, ensure_ascii=False)
        return json.dumps({
            "thought": "骑士团在开会，讨论什么？",
            "memory_note": {"learned_facts": [
                {"evidence_id": evidence_id, "quote": "骑士团正在开会"}
            ]},
        }, ensure_ascii=False)


def test_observe_to_unlock_end_to_end(tmp_path: Path) -> None:
    seen = []

    async def scenario():
        async def observe(request):
            request_body = await request.json()
            assert request_body["includeOcr"] is True
            assert request_body["ocrRoi"] == {"x": 0, "y": 0, "width": 400, "height": 200}
            assert request.headers.get("Authorization") == "Bearer token"
            seen.append((request.method, request.path))
            return web.json_response({
                "sessionId": "player-shadow-ocr",
                "frameId": "unsaved-e2e",
                "capturedAtUtc": datetime.now(timezone.utc).isoformat(),
                "imageBase64": base64.b64encode(IMAGE).decode(),
                "image": {"mimeType": "image/jpeg", "width": 640, "height": 360},
                "vision": {"uiCategory": "Talk", "ocr": [{"text": "骑士团正在开会"}]},
            })

        async def forbidden(request):
            seen.append(("FORBIDDEN", request.path))
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/api/v1/agent/observe", observe)
        app.router.add_post("/api/v1/agent/runtime/start", forbidden)
        app.router.add_post("/api/v1/agent/live/guidance", forbidden)
        app.router.add_post("/api/v1/agent/live/commentary", forbidden)
        app.router.add_post("/api/v1/agent/live/actions", forbidden)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            profile = load_profile(DEFAULT_PROFILE)
            state_store = StateStore(tmp_path / "resume.json", profile.player_id)
            recorder = ShadowRecorder(tmp_path / "shadow", "e2e", profile.player_id, run_id="e2e")
            working_store = WorkingMemoryStore(tmp_path / "working.json")
            episodic_store = EpisodicStore(tmp_path / "episodes")
            semantic_store = semantic_module.SemanticStore(tmp_path / "facts.json")
            evidence_store = knowledge_module.EvidenceStore(tmp_path / "evidence")
            gate = knowledge_module.KnowledgeGate(evidence_store, semantic_store)
            observer = ScreenObserver(
                "http://127.0.0.1:{0}".format(port), "token", "player-shadow-ocr", evidence_store,
                ocr_roi={"x": 0, "y": 0, "width": 400, "height": 200},
            )
            llm = FakeLLM()
            agent = PersistentPlayerAgent(
                profile, state_store, recorder, llm=llm, session_id="e2e",
                working_store=working_store, episodic_store=episodic_store,
                semantic_store=semantic_store,
                retriever=MemoryRetriever(episodic_store, semantic_store, knowledge_gate=gate),
                context_builder=ContextBuilder(profile),
                consolidator=MemoryConsolidator(
                    working_store, episodic_store, semantic_store, session_id="e2e"),
                observation_provider=observer, knowledge_gate=gate,
            )
            agent.observe({"type": "frame", "seq": 1, "jpeg": base64.b64encode(b"live").decode(),
                           "timestampUtc": datetime.now(timezone.utc).isoformat()})
            record = await agent.tick(force=True)
            agent.close()
            return record, agent, semantic_store, recorder, llm
        finally:
            await runner.cleanup()

    record, agent, semantic_store, recorder, llm = asyncio.run(scenario())
    assert seen == [("POST", "/api/v1/agent/observe")]
    assert llm.calls[0]["image"] == base64.b64encode(IMAGE).decode()
    assert record["memory"]["ocr_unlocks"]
    facts = semantic_store.list()
    assert len(facts) == 1
    assert facts[0]["proposition"] == "屏幕OCR（未校对）：骑士团正在开会"
    assert facts[0]["source"].startswith("screen_ocr:")
    types = [
        json.loads(line)["type"]
        for line in recorder.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert types == ["shadow_start", "shadow_tick", "shadow_end"]
