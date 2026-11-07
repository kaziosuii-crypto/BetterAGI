"""注入的小型手工向量只用于验证余弦计算，不代表训练模型。"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from player_embedding import EmbeddingConfig, LocalSentenceTransformerAdapter, PrecomputedEmbeddingAdapter  # noqa: E402
from player_knowledge import EvidenceStore, KnowledgeGate  # noqa: E402
from player_retrieval import BeliefLinks, CognitiveContext, MemoryRetriever, rank_experience_candidates, tokenize  # noqa: E402
from player_semantic_memory import SemanticStore  # noqa: E402


def test_experience_rank_recalls_semantics_and_preserves_fields():
    rows = [{"source": "turn:a", "text": "scarlet automobile", "session": "s", "evidence": ["a"]},
            {"source": "turn:b", "text": "unrelated ocean", "session": "s", "evidence": ["b"]}]
    assert rank_experience_candidates("crimson vehicle", rows) == []
    adapter = FakeVectors(documents={"turn:a": (1, 0), "turn:b": (0, 1)})
    result = rank_experience_candidates("crimson vehicle", rows, embedding=EmbeddingConfig(adapter, "fixture"))
    assert len(result) == 1 and result[0]["score"] == pytest.approx(0.15)
    assert {key: value for key, value in result[0].items() if key != "score"} == rows[0]
    assert result[0] is not rows[0] and result[0]["evidence"] == ["a"]
    assert [call[0] for call in adapter.calls] == ["query", "turn:a", "turn:b"]


@pytest.mark.parametrize("query_vector,document_vector", [
    (RuntimeError("offline"), (1, 0)), ((1, 0), RuntimeError("offline")),
    ((0, 0), (1, 0)), ((1, 0), (float("nan"), 0)), ((1, 0), (1, 0, 0)),
])
def test_experience_rank_bad_vectors_fall_back_to_lexical(query_vector, document_vector):
    rows = [{"source": "one", "text": "scarlet automobile"}, {"source": "two", "text": "unrelated"}]
    expected = rank_experience_candidates("scarlet", rows)
    adapter = FakeVectors(query=query_vector, documents={"one": document_vector, "two": document_vector})
    assert rank_experience_candidates("scarlet", rows, embedding=EmbeddingConfig(adapter, "fixture")) == expected
    assert rank_experience_candidates("crimson vehicle", rows, embedding=EmbeddingConfig(adapter, "fixture")) == []


def test_experience_rank_limits_semantic_calls_and_candidate_count():
    rows = [{"source": "one", "text": "other"}, {"source": "two", "text": "scarlet"},
            {"source": "three", "text": "different"}]
    adapter = FakeVectors(documents={"one": (0, 1), "two": (1, 0), "three": (1, 0)})
    config = EmbeddingConfig(adapter, "fixture", max_candidates=1)
    result = rank_experience_candidates("scarlet", rows, embedding=config)
    assert [item["source"] for item in result] == ["two"]
    assert result[0]["score"] > 0
    assert [call[0] for call in adapter.calls] == ["query", "one"]
    assert rank_experience_candidates("scarlet", rows, embedding=config, limit=0) == []
    with pytest.raises(ValueError, match="512"):
        rank_experience_candidates("query", itertools.repeat(rows[0], 513))
    with pytest.raises(ValueError, match="source 和 text"):
        rank_experience_candidates("query", [{"source": "bad"}])


def test_experience_rank_bounds_adapter_text_without_changing_output():
    row = {"source": "one", "text": "different text", "metadata": {"nested": True}}
    adapter = FakeVectors(documents={"one": (1, 0)})
    result = rank_experience_candidates("long query", [row],
        embedding=EmbeddingConfig(adapter, "fixture", max_query_chars=4, max_document_chars=5))
    assert adapter.calls == [("query", "long"), ("one", "diffe")]
    assert result[0]["text"] == row["text"]
    assert result[0]["metadata"] is row["metadata"]
    assert "score" not in row


def test_cognitive_state_reorders_only_already_relevant_experience():
    rows = [{"source": "a", "text": "遗迹", "source_event_ids": ["unrelated"]},
            {"source": "z", "text": "遗迹", "speaker": "安柏", "source_event_ids": ["seen"],
             "emotion": {"curiosity": 0.8}},
            {"source": "hidden-match", "text": "雪山", "speaker": "安柏", "source_event_ids": ["seen"]}]
    state = {"relationships": [{"character": "安柏", "affection": 0.8}],
             "beliefs": [{"status": "hypothesis", "source_event_ids": ["seen"]}],
             "threads": [{"status": "open", "source_event_ids": ["seen"]}],
             "emotion": {"curiosity": 0.8}}
    baseline = rank_experience_candidates("遗迹", rows)
    ranked = rank_experience_candidates("遗迹", rows, cognitive_state=state)
    assert [item["source"] for item in baseline] == ["a", "z"]
    assert [item["source"] for item in ranked] == ["z", "a"]
    assert 0 < ranked[0]["score"] - ranked[1]["score"] <= 0.2
    assert all(item["source"] != "hidden-match" for item in ranked)


def test_experience_rank_order_is_deterministic_and_bounded():
    rows = [{"source": "z", "text": "scarlet", "other": 3},
            {"source": "a", "text": "scarlet", "other": 1},
            {"source": "a", "text": "scarlet", "other": 2}]
    expected = [rows[1], rows[2]]
    result = rank_experience_candidates("scarlet", rows, limit=2)
    assert [{key: value for key, value in row.items() if key != "score"} for row in result] == expected
    assert result == rank_experience_candidates("scarlet", rows, limit=2)
    assert result[0]["score"] >= result[1]["score"]
    assert rows[0]["source"] == "z"


def test_local_model_adapter_disables_download_and_remote_code(tmp_path, monkeypatch):
    import types
    calls = []
    class Vector:
        def tolist(self): return [1.0, 0.0]
    class Model:
        def __init__(self, path, **kwargs):
            calls.append((path, kwargs))
        def encode(self, text, **kwargs):
            return Vector()
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Model))
    adapter = LocalSentenceTransformerAdapter(tmp_path)
    assert calls[0][1] == {"local_files_only": True, "trust_remote_code": False}
    assert adapter.embed_query("你好") == [1.0, 0.0]
    assert adapter.embed_document("source", "你好") == [1.0, 0.0]


def test_local_model_document_cache_is_source_and_content_bound(tmp_path, monkeypatch):
    import types
    calls = []
    class Vector:
        def tolist(self): return [1.0, 0.0]
    class Model:
        def __init__(self, path, **kwargs): pass
        def encode(self, text, **kwargs):
            calls.append(text)
            return Vector()
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Model))
    adapter = LocalSentenceTransformerAdapter(tmp_path)
    first = adapter.embed_document("a", "original")
    first[0] = 0.0
    assert adapter.embed_document("a", "original") == [1.0, 0.0]
    adapter.embed_document("a", "changed")
    adapter.embed_document("b", "original")
    assert calls == ["original", "changed", "original"]


class Rows:
    """提供可控的记忆候选。"""

    def __init__(self, rows):
        self.rows = rows

    def list(self):
        return self.rows


class FakeVectors:
    """显式注入的二维测试向量，不请求模型或网络。"""

    def __init__(self, query=(3.0, 0.0), documents=None):
        self.query = query
        self.documents = documents or {}
        self.calls = []

    def embed_query(self, text):
        self.calls.append(("query", text))
        if isinstance(self.query, Exception):
            raise self.query
        return self.query

    def embed_document(self, source, text):
        self.calls.append((source, text))
        value = self.documents.get(source)
        if isinstance(value, Exception):
            raise value
        return value


def episode(event_id, text, visibility="player_knowledge"):
    """构造简单 episode，不额外引入词面或角色匹配。"""
    return {"event_id": event_id, "visibility": visibility, "what_happened": text,
            "characters": [], "unresolved_questions": [], "importance": 0.5}


def test_semantic_only_recall_and_explainable_score():
    rows = Rows([episode("one", "scarlet automobile")])
    fake = FakeVectors(documents={"episode:one": (5.0, 0.0)})
    original = MemoryRetriever(rows, clock=lambda: 1)
    assert not tokenize("crimson vehicle") & tokenize("scarlet automobile")
    assert original.retrieve("crimson vehicle") == []
    retriever = MemoryRetriever(rows, embedding=EmbeddingConfig(fake, "fixture-vectors"), clock=lambda: 1)
    result = retriever.retrieve("crimson vehicle")[0]
    assert result.source == "episode:one"
    assert result.meta["embedding"] == {"provenance": "fixture-vectors", "status": "matched",
                                        "similarity": pytest.approx(1.0), "threshold": 0.4,
                                        "weight": 0.15, "contribution": pytest.approx(0.15)}
    scores = result.meta["score_breakdown"]
    assert scores["base"]["lexical"] == 0
    assert scores["base"]["embedding"] == pytest.approx(0.15)
    assert scores["total"] == pytest.approx(sum(scores["base"].values()) + sum(scores["cognitive"].values()))
    assert asyncio.run(retriever.retrieve_async("crimson vehicle"))[0].source == result.source


def test_visibility_and_real_knowledge_gate_before_adapter(tmp_path):
    evidence = EvidenceStore(tmp_path / "evidence")
    facts = SemanticStore(tmp_path / "facts.json", clock=lambda: 100000)
    gate = KnowledgeGate(evidence, facts)
    observed = evidence.record_screen_ocr(session_id="s", frame_id="f", frame_bytes=b"frame",
                                          observed_at_ms=100000, text="骑士正在集合", confidence=None)
    fact = gate.unlock_quote(evidence_id=observed["evidence_id"], quote="骑士正在集合", session_id="s",
                             frame_id="f", frame_sha256=hashlib.sha256(b"frame").hexdigest(), now_ms=100001)
    bad = dict(fact, fact_id="unverified", proposition="hidden spoiler")
    world = dict(fact, fact_id="world", visibility="world_only", proposition="secret ending")
    episodes = Rows([episode("visible", "scarlet automobile"), episode("hidden", "secret", "world_only")])
    adapter = FakeVectors(documents={"episode:visible": (1, 0), "episode:hidden": (1, 0),
                                     "fact:" + fact["fact_id"]: (1, 0),
                                     "fact:unverified": (1, 0), "fact:world": (1, 0)})
    retriever = MemoryRetriever(episodes, Rows([fact, bad, world]), knowledge_gate=gate,
                                embedding=EmbeddingConfig(adapter, "fixture"), clock=lambda: 100001)
    found = retriever.retrieve("crimson vehicle", limit=10)
    assert {x.source for x in found} == {"episode:visible", "fact:" + fact["fact_id"]}
    assert {x[0] for x in adapter.calls} == {"query", "episode:visible", "fact:" + fact["fact_id"]}
    assert MemoryRetriever(None, Rows([fact]), embedding=EmbeddingConfig(adapter, "fixture")).retrieve("crimson vehicle") == []
    evidence_path = tmp_path / "evidence" / (observed["evidence_id"] + ".json")
    evidence_path.unlink()
    assert not retriever.retrieve("crimson vehicle", limit=10)[0].source.startswith("fact:")


@pytest.mark.parametrize("query,document", [
    ((float("nan"), 0), (1, 0)), ((float("inf"), 0), (1, 0)),
    ((0, 0), (1, 0)), ((1, 0), (0, 0)), ((1, 0), (float("nan"), 0)),
    ((1, 0), (1, float("inf"))), ((1, 0), (1, 0, 0)),
    ((1, 0), (True, 0)), ((1, 0), RuntimeError("unavailable")),
    (RuntimeError("unavailable"), (1, 0)),
])
def test_bad_vectors_fall_back_to_original_retrieval(query, document):
    rows = Rows([episode("one", "scarlet automobile")])
    baseline = MemoryRetriever(rows, clock=lambda: 1).retrieve("scarlet")
    fake = FakeVectors(query=query, documents={"episode:one": document})
    retriever = MemoryRetriever(rows, embedding=EmbeddingConfig(fake, "fixture"), clock=lambda: 1)
    result = retriever.retrieve("scarlet")
    assert [r.memory_id for r in result] == [r.memory_id for r in baseline]
    assert result[0].score == baseline[0].score
    assert result[0].meta["embedding"]["status"] in {"query_unavailable", "document_unavailable"}
    assert retriever.retrieve("crimson vehicle") == []


def test_empty_token_query_falls_back_when_query_vector_fails():
    rows = Rows([episode("one", "something")])
    context = CognitiveContext(beliefs=[BeliefLinks("b", support_sources=["episode:one"])])
    fake = FakeVectors(query=(0, 0))
    retriever = MemoryRetriever(rows, embedding=EmbeddingConfig(fake, "fixture"))
    assert retriever.retrieve("!", cognitive_context=context) == []
    fake.query = (1, 0)
    fake.documents = {"episode:one": (1, 0)}
    assert retriever.retrieve("!")[0].memory_id == "one"


def test_candidate_bound_threshold_and_exact_precomputed_text():
    rows = Rows([episode("first", "scarlet"), episode("second", "automobile")])
    fake = FakeVectors(documents={"episode:first": (0, 1), "episode:second": (1, 0)})
    retriever = MemoryRetriever(rows, embedding=EmbeddingConfig(fake, "fixture", max_candidates=1), clock=lambda: 1)
    assert retriever.retrieve("crimson") == []
    assert [call[0] for call in fake.calls] == ["query", "episode:first"]
    first_doc = fake.calls[-1][1]
    vectors = PrecomputedEmbeddingAdapter({"crimson": (1, 0)}, {("episode:first", first_doc): (2, 0)})
    precomputed = MemoryRetriever(rows, embedding=EmbeddingConfig(vectors, "precomputed"), clock=lambda: 1)
    assert [item.memory_id for item in precomputed.retrieve("crimson")] == ["first"]
    rows.rows[0]["what_happened"] = "changed"
    assert precomputed.retrieve("crimson") == []


def test_limits_and_normalization():
    with pytest.raises(ValueError):
        EmbeddingConfig(FakeVectors(), "", max_candidates=1)
    for kwargs in ({"max_candidates": 513}, {"max_dimensions": 2049}, {"threshold": float("nan")}, {"weight": 0.31}):
        with pytest.raises(ValueError):
            EmbeddingConfig(FakeVectors(), "fixture", **kwargs)
    rows = Rows([episode("one", "different")])
    huge = FakeVectors(query=(1e308, 1e308), documents={"episode:one": (1e308, 1e308)})
    result = MemoryRetriever(rows, embedding=EmbeddingConfig(huge, "fixture"), clock=lambda: 1).retrieve("query")
    assert result[0].meta["embedding"]["similarity"] == pytest.approx(1.0)
    limited = FakeVectors(query=(1, 2, 3), documents={"episode:one": (1, 2, 3)})
    assert MemoryRetriever(rows, embedding=EmbeddingConfig(limited, "fixture", max_dimensions=2)).retrieve("query") == []
