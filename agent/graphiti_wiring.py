"""Graphiti 本地化装配：LLM 走 DeepSeek，embedding 跑在本机（fastembed，bge-small-zh-v1.5）。

为什么不依赖 OPENAI_API_KEY：
- Graphiti 默认 LLM/embedder/reranker 全指向 OpenAI；本机只有 DeepSeek 的对话额度，
  没有 OpenAI key，也没有 embeddings 端点。
- 所以 embedding 用 fastembed 在进程内跑（512 维、离线下载一次后不再联网），
  LLM 用 DeepSeek 的 OpenAI 兼容端点；reranker 关掉走 RRF（见 memory._make_noop_cross_encoder）。

这些客户端都是惰性导入的：没装 graphiti-core / fastembed 时只影响 Graphiti 后端。
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, Sequence

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"


def _api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("GRAPHITI_LLM_API_KEY") or ""
    if key:
        return key
    # DSH 把密钥放在 credentials.yaml 的 refs 里，按需读一次，不写死到代码。
    try:
        import yaml  # type: ignore

        path = os.path.expanduser(os.environ.get("DSH_CREDENTIALS", "~/.dsh/.credentials.yaml"))
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return str((data.get("refs") or {}).get("DEEPSEEK_API_KEY") or "")
    except Exception:  # noqa: BLE001 - 读不到就让上层报清晰的缺少凭据错误
        return ""


try:  # Graphiti 用 pydantic 校验实例类型，必须真的继承它的基类
    from graphiti_core.embedder.client import EmbedderClient as _EmbedderBase  # type: ignore
except Exception:  # noqa: BLE001 - 没装 graphiti-core 时退化成普通类，仅用于本地测试
    _EmbedderBase = object  # type: ignore[assignment]


class FastEmbedEmbedder(_EmbedderBase):  # type: ignore[misc,valid-type]
    """Graphiti EmbedderClient 的本机实现（fastembed + ONNX，CPU 即可）。"""

    def __init__(self, model: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM, cache_dir: Optional[str] = None):
        self.model_name = model
        self.dim = int(dim)
        self.cache_dir = cache_dir
        self._model = None

    def _ensure(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding  # type: ignore
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("fastembed 未安装：uv pip install fastembed") from error
        kwargs = {}
        if self.cache_dir:
            kwargs["cache_dir"] = self.cache_dir
        self._model = TextEmbedding(self.model_name, **kwargs)
        return self._model

    def _embed_sync(self, texts: Sequence[str]) -> List[List[float]]:
        model = self._ensure()
        out: List[List[float]] = []
        for vector in model.embed([str(t) for t in texts]):
            values = [float(x) for x in vector]
            if len(values) != self.dim:
                raise RuntimeError("embedding 维度不一致：%d != %d" % (len(values), self.dim))
            out.append(values)
        return out

    async def create(self, input_data: Any) -> List[float]:
        texts = [input_data] if isinstance(input_data, str) else list(input_data)
        vectors = self._embed_sync(texts)
        return vectors[0]

    async def create_batch(self, input_data: List[str]) -> List[List[float]]:
        return self._embed_sync(input_data)


def build_llm_client(
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    small_model: str = "",
) -> Any:
    """DeepSeek（OpenAI 兼容）作为 Graphiti 的抽取/命名模型。"""

    from graphiti_core.llm_client import LLMConfig, OpenAIClient  # type: ignore

    key = api_key or _api_key()
    if not key:
        raise RuntimeError("缺少 LLM 凭据：设置 DEEPSEEK_API_KEY 或 GRAPHITI_LLM_API_KEY")
    config = LLMConfig(
        api_key=key,
        base_url=base_url or os.environ.get("GRAPHITI_LLM_BASE_URL") or DEEPSEEK_BASE_URL,
        model=model or os.environ.get("GRAPHITI_LLM_MODEL") or DEEPSEEK_MODEL,
        small_model=small_model or os.environ.get("GRAPHITI_LLM_SMALL_MODEL") or DEEPSEEK_MODEL,
    )
    return OpenAIClient(config=config)


def build_embedder(
    *,
    model: str = "",
    dim: Optional[int] = None,
    cache_dir: Optional[str] = None,
) -> FastEmbedEmbedder:
    return FastEmbedEmbedder(
        model=model or os.environ.get("GRAPHITI_EMBED_MODEL") or DEFAULT_MODEL,
        dim=int(dim or os.environ.get("GRAPHITI_EMBED_DIM") or DEFAULT_DIM),
        cache_dir=cache_dir or os.environ.get("GRAPHITI_EMBED_CACHE") or None,
    )


def build_clients(**kwargs: Any) -> tuple:
    """一次性拿到 (llm_client, embedder)，给 GraphitiMemory 用。"""

    return build_llm_client(**{k: v for k, v in kwargs.items() if k in {"api_key", "base_url", "model", "small_model"}}), build_embedder(
        **{k: v for k, v in kwargs.items() if k in {"model", "dim", "cache_dir"}}
    )
