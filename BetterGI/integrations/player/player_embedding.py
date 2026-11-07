#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""显式注入的本地或预计算向量接口；本模块不加载模型、不访问网络。

适配器是受信任的调用方代码，调用方须确保其同步、本地执行且不上传数据。
文档向量按 (source, 截断后的文档原文) 查找，避免把旧文本的向量当作新文本。
此模块不持久化向量、不推断模型身份，也不授予任何知识访问权限。
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence, Tuple


class EmbeddingAdapter(Protocol):
    """只接收查询和通过检索屏障的文档；返回真实向量或 None。"""

    def embed_query(self, text: str) -> Optional[Sequence[float]]:
        """同步返回查询向量；None 表示无法计算。"""
        ...

    def embed_document(self, source: str, text: str) -> Optional[Sequence[float]]:
        """同步返回与文档文本精确对应的向量；None 表示无法计算。"""
        ...


class LocalSentenceTransformerAdapter:
    """Load a user-selected local sentence-transformer without remote code or downloads."""

    def __init__(self, directory: str | Path):
        path = Path(directory).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("embedding 模型必须是本地目录")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("使用本地 embedding 需要安装 sentence-transformers") from exc
        self._model = SentenceTransformer(str(path), local_files_only=True, trust_remote_code=False)
        self._lock = threading.Lock()
        self._document_cache: OrderedDict[tuple[str, str], tuple[float, ...]] = OrderedDict()

    def embed_query(self, text: str) -> Optional[Sequence[float]]:
        with self._lock:
            vector = self._model.encode(text, show_progress_bar=False, normalize_embeddings=True)
        return vector.tolist()

    def embed_document(self, source: str, text: str) -> Optional[Sequence[float]]:
        with self._lock:
            key = (source, text)
            cached = self._document_cache.get(key)
            if cached is not None:
                self._document_cache.move_to_end(key)
                return list(cached)
            vector = tuple(self._model.encode(text, show_progress_bar=False, normalize_embeddings=True).tolist())
            _unit_vector(vector, 2048)
            self._document_cache[key] = vector
            if len(self._document_cache) > 256:
                self._document_cache.popitem(last=False)
            return list(vector)


class PrecomputedEmbeddingAdapter:
    """使用调用方提供的有界内存映射，不读取文件、不计算或伪造向量。"""

    def __init__(self, queries: Mapping[str, Sequence[float]],
                 documents: Mapping[Tuple[str, str], Sequence[float]]):
        """复制映射并限制条目数；向量校验在检索时统一执行。"""
        if len(queries) > 2048 or len(documents) > 2048:
            raise ValueError("预计算向量映射不能超过 2048 条")
        self._queries = dict(queries)
        self._documents = dict(documents)

    def embed_query(self, text: str) -> Optional[Sequence[float]]:
        """查找精确查询，不存在时安全回退。"""
        return self._queries.get(text)

    def embed_document(self, source: str, text: str) -> Optional[Sequence[float]]:
        """以来源和实际检索文本共同定位向量。"""
        return self._documents.get((source, text))


@dataclass(frozen=True)
class EmbeddingConfig:
    """显式启用时的权重、阈值和每次查询的资源上限。"""

    adapter: EmbeddingAdapter
    provenance: str
    weight: float = 0.15
    threshold: float = 0.4
    max_candidates: int = 128
    max_dimensions: int = 1024
    max_query_chars: int = 500
    max_document_chars: int = 4000

    def __post_init__(self) -> None:
        """配置错误应尽早暴露，运行时向量错误则回退到旧检索。"""
        if not isinstance(self.provenance, str) or not self.provenance.strip() or len(self.provenance) > 100:
            raise ValueError("provenance 必须是 1–100 字符的调用方来源标签")
        for name, lower, upper in (("weight", 0.0, 0.3), ("threshold", 0.0, 1.0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower < value <= upper:
                raise ValueError(name + " 超出允许范围")
        for name, upper in (("max_candidates", 512), ("max_dimensions", 2048),
                            ("max_query_chars", 4000), ("max_document_chars", 16000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(name + " 超出允许范围")


def _unit_vector(vector: Optional[Sequence[float]], max_dimensions: int) -> Tuple[float, ...]:
    """验证有限维数、有限实数及非零范数，再稳定地归一化。"""
    if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
        raise ValueError("向量必须是有界序列")
    if not 1 <= len(vector) <= max_dimensions:
        raise ValueError("向量维数无效")
    values = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("向量分量必须是实数")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("向量分量必须有限")
        values.append(number)
    scale = max(abs(value) for value in values)
    if not scale:
        raise ValueError("零向量不能归一化")
    scaled = [value / scale for value in values]
    norm = math.sqrt(math.fsum(value * value for value in scaled))
    return tuple(value / norm for value in scaled)


class EmbeddingSession:
    """每次检索独立的查询向量和候选预算，不缓存未授权文档。"""

    def __init__(self, config: EmbeddingConfig, query: str):
        """预先验证一次查询向量；适配器异常不影响原有检索。"""
        self.config = config
        self._remaining = config.max_candidates
        self._query = None
        try:
            self._query = _unit_vector(
                config.adapter.embed_query(query[:config.max_query_chars]), config.max_dimensions
            )
        except Exception:
            pass

    @property
    def available(self) -> bool:
        """指示本次查询是否具有有效向量。"""
        return self._query is not None

    def score(self, source: str, text: str) -> dict:
        """对单个已授权候选计算正余弦；任何异常只产生零增益。"""
        info = {"provenance": self.config.provenance, "status": "query_unavailable",
                "similarity": None, "threshold": self.config.threshold,
                "weight": self.config.weight, "contribution": 0.0}
        if self._query is None:
            return info
        if self._remaining <= 0:
            info["status"] = "candidate_limit"
            return info
        self._remaining -= 1
        try:
            vector = _unit_vector(
                self.config.adapter.embed_document(source, text[:self.config.max_document_chars]),
                self.config.max_dimensions,
            )
            if len(vector) != len(self._query):
                raise ValueError("查询和文档维数不一致")
            similarity = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(self._query, vector))))
            info["status"] = "matched" if similarity >= self.config.threshold else "below_threshold"
            info["similarity"] = similarity
            if similarity >= self.config.threshold:
                info["contribution"] = similarity * self.config.weight
        except Exception:
            info["status"] = "document_unavailable"
        return info
