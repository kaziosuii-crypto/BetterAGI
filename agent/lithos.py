#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LithosAI 三档模型客户端（base / fast / ultra）。

设计目标只有一个：**按紧迫程度自动挑档位**，其余一律不猜。

- 三个档位是三条独立的模型 id + 参数，全部走 OpenAI 兼容的 `/chat/completions`；
- 调用方只说"这件事多急"（urgency 0–1）或"这是什么类型的事"（kind），
  由 `choose_tier()` 决定用哪一档，避免各调用点各写一套 if/else；
- 任何网络/协议错误都抛 `LithosError`，带上 HTTP 状态与截断后的响应体，
  绝不把失败伪装成空回复。

环境变量：

- ``LITHOS_API_KEY``   必填（也可以显式传进 ``LithosConfig``）
- ``LITHOS_BASE_URL``  默认 ``https://api.lithos.ai/v1``
- ``LITHOS_MODEL_BASE`` / ``LITHOS_MODEL_FAST`` / ``LITHOS_MODEL_ULTRA``
  三个档位的模型 id；没配就用 ``DEFAULT_MODELS``
- ``LITHOS_TIMEOUT_BASE`` / ``..._FAST`` / ``..._ULTRA`` 各档超时秒数

本模块只依赖 ``httpx``（仓库里已有）。没有 httpx 时 import 仍然成功，
但真正发请求会给出可读错误，方便在离线环境跑单测。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:  # pragma: no cover - 环境相关
    import httpx
except Exception:  # noqa: BLE001 - 离线环境允许缺失
    httpx = None  # type: ignore[assignment]


#: 生产（lithosAI 网关）三档模型 id：同一模型家族的三档出字速度。
LITHOS_MODELS = {
    "base": "deepseek-ai/DeepSeek-V4.1-Flash",
    "fast": "deepseek-ai/DeepSeek-V4.1-Flash-fast",
    "ultra": "deepseek-ai/DeepSeek-V4.1-Flash-ultra",
}

#: 测试（官方 DeepSeek 接口）只接受 `deepseek-flash` 与 `deepseek-v4-pro` 两个 id，
#: 没有速度分档。因此测试环境三个档位都用 `deepseek-flash`：档位的语义（职责与超时）
#: 仍然生效，但**速度差异无法在测试环境复现**，真实三档要等生产 key。
DEEPSEEK_MODELS = {"base": "deepseek-flash", "fast": "deepseek-flash", "ultra": "deepseek-flash"}

#: 2026-11-06 用户把慢系统（脑）切到 portixapi 网关的 gpt-6-luna。
#: 网关自报的模型列表里 luna 只有这一档，所以三个档位暂时同 id，
#: 档位语义（职责与超时）照旧生效，速度差异等网关真的分档再说。
PORTIX_MODELS = {"base": "gpt-6-luna", "fast": "gpt-6-luna", "ultra": "gpt-6-luna"}

#: 2026-11-06 用户给的第二条 luna 通道（origin.modelflare.dev）。portix 的 luna
#: 分组一直 429 饱和，这条实测 200 可用；同样只有 luna 一档，三档同 id。
MODELFLARE_MODELS = {"base": "gpt-6-luna", "fast": "gpt-6-luna", "ultra": "gpt-6-luna"}

#: 2026-11-06 20:09 用户指定的新慢系统：阿里 Qwen 官方 MaaS 的 qwen3.8-flash，
#: 明确要求「用和 omni 一个 key」→ 复用 DASHSCOPE_API_KEY（快系统 Omni 那把）。
#: 实测（走本机代理、同一个 key）：qwen3.8-flash 400~900ms 返回，流式首块同量级，
#: 比 luna 的 4~6s 网关地板快一个数量级；官方兼容端点与文档页给的 cn-beijing.maas 等价。
QWEN_MODELS = {"base": "qwen3.8-flash", "fast": "qwen3.8-flash", "ultra": "qwen3.8-flash"}

#: provider 档案：决定 base_url、三档模型 id、以及 key 从哪里来。
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "lithos": {
        "base_url": "https://api.lithos.ai/v1",
        "models": LITHOS_MODELS,
        "key_env": "LITHOS_API_KEY",
        "credential_ref": "",
    },
    "portix": {
        "base_url": "https://www.portixapi.com/v1",
        "models": PORTIX_MODELS,
        "key_env": "PORTIX_API_KEY",
        "credential_ref": "",
        # key 落盘在仓库内的 .secrets/（不入库、不打日志），省得每次开播现问。
        "key_file": ".secrets/portix_api_key.txt",
    },
    "modelflare": {
        "base_url": "https://origin.modelflare.dev/v1",
        "models": MODELFLARE_MODELS,
        "key_env": "MODELFLARE_API_KEY",
        "credential_ref": "",
        # 网关自带 ~4.4k token 的固定系统提示，TTFT 8~18s，只适合当脑。
        "key_file": ".secrets/modelflare_api_key.txt",
    },
    "qianwen": {
        # 用户 2026-11-06 给的文档页（www.qianwenai.com/models/qwen3.8-flash）里写的就是这个
        # 兼容端点；dashscope.aliyuncs.com/compatible-mode/v1 用同一把 key 也通。
        "base_url": "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        "models": QWEN_MODELS,
        "key_env": "DASHSCOPE_API_KEY",
        "credential_ref": "DASHSCOPE_API_KEY",
        "key_file": "keys/dashscope.key",
        # 必须走系统代理（verge-mihomo 127.0.0.1:7897）：直连 dashscope 实测 42~45s 才回
        # （mihomo TUN 把域名解析成 198.18.x fake-ip，直连几乎等于超时），走代理 88ms。
        "trust_env": True,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "models": DEEPSEEK_MODELS,
        "key_env": "DEEPSEEK_API_KEY",
        "credential_ref": "DEEPSEEK_API_KEY",
    },
}

#: 2026-11-06 20:09 用户把慢系统切到阿里 Qwen 官方的 qwen3.8-flash（和 omni 同一把
#: DASHSCOPE_API_KEY）。此前依次用过 DeepSeek 官方（19:30 换回，702ms 但对中文小作文
#: 偏啰嗦）、modelflare / portix 的 gpt-6-luna（TTFT 4~6s 太慢、分组常 429）。
#: 这些档案全部保留为可回退通道：设 LITHOS_PROVIDER=deepseek / modelflare / portix 即可切回。
DEFAULT_PROVIDER = "qianwen"
DEFAULT_MODELS = QWEN_MODELS
DEFAULT_BASE_URL = PROVIDERS[DEFAULT_PROVIDER]["base_url"]

#: DSH 的凭据文件（只读，绝不落盘、绝不日志输出）。
DEFAULT_CREDENTIALS_PATH = r"C:\Users\Tsuba\.dsh\.credentials.yaml"


def _read_key_file(relative: str) -> str:
    """从仓库根目录下的小文件读一个 key（只读，绝不写回、绝不打印）。

    只给没有凭据文件可借的第三方网关用；路径相对本文件上两级（仓库根）。
    """
    if not relative:
        return ""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, relative), "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:  # noqa: BLE001 - 读不到就当没配 key
        return ""


def _read_credential(name: str) -> str:
    """从 DSH 凭据文件的 ``refs`` 段读一个 key；任何异常都返回空串。

    测试环境需要直接用官方 DeepSeek key，而 key 不应复制进仓库或环境变量文件，
    所以这里只读不写，也不打印。
    """
    if not name:
        return ""
    path = os.environ.get("DSH_CREDENTIALS", DEFAULT_CREDENTIALS_PATH)
    try:
        import yaml  # 仓库依赖里已有；缺了就当作没有 key
    except Exception:  # noqa: BLE001
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        value = (data.get("refs") or {}).get(name) or ""
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""

#: 各档默认超时（秒）。base 最宽松，ultra 最紧——它就是用来看"现在马上要一个决定"。
DEFAULT_TIMEOUTS = {"base": 60.0, "fast": 30.0, "ultra": 15.0}

#: 事件类型 → 档位。规划/反思用便宜档，实时反应直接上 ultra。
KIND_TO_TIER = {
    "reflect": "base",
    "summarize": "base",
    "memory": "base",
    "plan": "fast",
    "decide": "fast",
    "react": "ultra",
    "urgent": "ultra",
    "interrupt": "ultra",
}


#: 日志脱敏用的模式：`Bearer xxx` 与 `sk-xxx` 两类常见形态。
_SECRET_PATTERN = re.compile(r"(Bearer\s+|sk-)([A-Za-z0-9_\-\.]{4,})")


class LithosError(RuntimeError):
    """调用 LithosAI 失败：配置缺失、网络错误、非 2xx、响应结构不合法。"""

    def __init__(self, message: str, *, status: int = 0, body: str = "", code: str = "lithos_error"):
        super().__init__(message)
        self.status = int(status or 0)
        self.body = body or ""
        self.code = code


@dataclass(frozen=True)
class TierConfig:
    """单个档位的模型与采样参数。"""

    name: str
    model: str
    timeout: float
    temperature: float = 0.6
    max_tokens: int = 1024


@dataclass
class LithosConfig:
    """客户端配置；缺 key 时 `available()` 为 False，调用会给出明确报错。"""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    provider: str = DEFAULT_PROVIDER
    models: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    timeouts: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    extra_headers: Dict[str, str] = field(default_factory=dict)
    #: 是否让 httpx 读系统代理（HTTP_PROXY/HTTPS_PROXY 等）。只有必须翻墙的端点
    #: （qianwen 的阿里 MaaS）才开：本机直连要 42~45s，走 127.0.0.1:7897 只要 ~90ms。
    trust_env: bool = False

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "LithosConfig":
        """按 provider 档案组装配置。

        provider 选择顺序：显式 ``LITHOS_PROVIDER`` > 有 ``LITHOS_API_KEY`` 就用 lithos >
        否则用 deepseek（测试环境，key 从 DSH 凭据文件读）。
        """
        env = os.environ if env is None else env
        provider = (env.get("LITHOS_PROVIDER") or "").strip().lower()
        if not provider:
            # 2026-11-06 起默认 portix（gpt-6-luna）；DeepSeek 需显式 opt-in。
            provider = DEFAULT_PROVIDER
        if provider not in PROVIDERS:
            raise LithosError(
                f"未知 provider {provider!r}，只能是 {' / '.join(sorted(PROVIDERS))}",
                code="bad_provider",
            )
        spec = PROVIDERS[provider]
        models = dict(spec["models"])
        for tier in models:
            value = (env.get(f"LITHOS_MODEL_{tier.upper()}") or "").strip()
            if value:
                models[tier] = value
        timeouts = dict(DEFAULT_TIMEOUTS)
        for tier in timeouts:
            raw = (env.get(f"LITHOS_TIMEOUT_{tier.upper()}") or "").strip()
            if raw:
                try:
                    timeouts[tier] = max(3.0, float(raw))
                except ValueError:
                    pass
        api_key = (env.get("LITHOS_API_KEY") or "").strip()
        if not api_key and provider == "deepseek":
            # DeepSeek 官方 key 只给 deepseek 用；否则 portix 会拿着别人的 key 发请求。
            api_key = (env.get("DEEPSEEK_API_KEY") or "").strip()
        if not api_key and spec.get("key_env"):
            # 通用回退：档案声明的专用 key 环境变量（qianwen 用 DASHSCOPE_API_KEY，
            # 与快系统 Omni 共用同一把；用户 2026-11-06 明确要求「用和 omni 一个 key」）。
            api_key = (env.get(str(spec["key_env"])) or "").strip()
        if not api_key and spec.get("credential_ref"):
            api_key = _read_credential(str(spec["credential_ref"]))
        if not api_key and spec.get("key_file"):
            api_key = _read_key_file(str(spec["key_file"]))
        return cls(
            api_key=api_key,
            base_url=(env.get("LITHOS_BASE_URL") or spec["base_url"]).rstrip("/"),
            provider=provider,
            models=models,
            timeouts=timeouts,
            trust_env=bool(spec.get("trust_env", False)),
        )

    def available(self) -> bool:
        return bool(self.api_key)

    def tier(self, name: str) -> TierConfig:
        key = (name or "").strip().lower()
        if key not in self.models:
            raise LithosError(f"未知档位 {name!r}，只能是 base / fast / ultra", code="bad_tier")
        return TierConfig(name=key, model=self.models[key], timeout=self.timeouts[key])


def choose_tier(
    urgency: Optional[float] = None,
    *,
    kind: Optional[str] = None,
    forced: Optional[str] = None,
    live: bool = False,
) -> str:
    """按紧迫程度或事件类型选档。

    ``forced`` 优先（调试与回归用）；否则 ``kind`` 查表；再否则用 ``urgency``
    分三段：<=0.34 base、<=0.74 fast、其余 ultra。都没有时给 fast——
    "不知道多急"时用中档，比盲目用最贵或最慢都安全。

    ``live=True`` 是直播档：恒返回 fast，优先于 forced/kind/urgency。
    直播时慢系统只是"偶尔插一句话"，一轮口播拖到 ultra 的 15 秒预算不可接受；
    快慢互斥下本来也不该有"马上要一个决定"的慢调用。
    """
    if live:
        return "fast"
    if forced:
        key = forced.strip().lower()
        if key not in DEFAULT_MODELS:
            raise LithosError(f"未知档位 {forced!r}，只能是 base / fast / ultra", code="bad_tier")
        return key
    if kind:
        key = KIND_TO_TIER.get(kind.strip().lower())
        if key:
            return key
    if urgency is None:
        return "fast"
    value = max(0.0, min(1.0, float(urgency)))
    if value <= 0.34:
        return "base"
    if value <= 0.74:
        return "fast"
    return "ultra"


@dataclass
class LLMReply:
    """一次补全的结果：文本、工具调用、实际用的档位与耗时。"""

    text: str
    tool_calls: List[Dict[str, Any]]
    tier: str
    model: str
    latency_ms: int
    usage: Dict[str, Any] = field(default_factory=dict)
    #: 思考模式的原文。DeepSeek 的 thinking 模式要求把上一轮的 reasoning_content
    #: 原样带回，否则下一轮直接 400（"must be passed back to the API"）。
    reasoning: str = ""
    #: 流式调用是否因 ``deadline_s`` 超预算而提前中止；此时 text/tool_calls 只含已到片段。
    aborted: bool = False

    def as_message(self) -> Dict[str, Any]:
        """转回 OpenAI 的 assistant message，便于直接续接对话。"""
        message: Dict[str, Any] = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
            # thinking 模式硬要求：带 tool_calls 时必须回传 reasoning_content，
            # 没有思考时也要给空串，否则下一轮请求直接 400（实测 deepseek-flash）。
            message["reasoning_content"] = self.reasoning or ""
        elif self.reasoning:
            message["reasoning_content"] = self.reasoning
        return message


def _image_part(data_url: str) -> Dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_url}}


def build_user_content(text: str, images: Sequence[str] = ()) -> Any:
    """拼一条 user content：没有图片时是纯字符串，有图片时是 OpenAI 的多段数组。"""
    if not images:
        return text
    parts: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        if not isinstance(image, str) or not image.startswith("data:image/"):
            raise LithosError("图片必须是 data:image/... 的 data URL", code="bad_image")
        parts.append(_image_part(image))
    return parts


class LithosClient:
    """最小可用的三档客户端；同步实现，异步侧自行用 to_thread 包一层。"""

    #: 连接复用（用户 2026-11-05 要求）：以前每轮都 `with httpx.Client(...)`，
    #: 慢系统每 3 秒一轮就白握手一次。这里缓存一个 keep-alive 的 Client。
    KEEPALIVE_CONNECTIONS = 8
    KEEPALIVE_EXPIRY_S = 30.0

    def __init__(self, config: Optional[LithosConfig] = None):
        self.config = config or LithosConfig.from_env()
        self._client: Any = None

    def _http(self) -> Any:
        """取（必要时新建）共享的 httpx.Client；timeout 由调用方按请求传。"""
        if self._client is None:
            # trust_env 默认 False：Windows 系统代理（verge-mihomo 127.0.0.1:7897）会让每个
            # 响应带上 `Connection: close`，keep-alive 失效、每轮重新握手（实测每步多 ~50ms）。
            # 例外：profile 里标了 trust_env 的端点（qianwen 阿里 MaaS）必须走代理，
            # 否则直连 fake-ip 要 42~45s——两害相权，这点握手开销可以接受。
            self._client = httpx.Client(
                trust_env=bool(getattr(self.config, "trust_env", False)),
                limits=httpx.Limits(
                    max_keepalive_connections=self.KEEPALIVE_CONNECTIONS,
                    max_connections=self.KEEPALIVE_CONNECTIONS * 2,
                    keepalive_expiry=self.KEEPALIVE_EXPIRY_S,
                ),
            )
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    # ---- 基础信息 -------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        return {
            "available": self.config.available(),
            "base_url": self.config.base_url,
            "models": dict(self.config.models),
            "timeouts": dict(self.config.timeouts),
        }

    def _headers(self) -> Dict[str, str]:
        if not self.config.available():
            raise LithosError(
                "缺少 LITHOS_API_KEY：先设置环境变量（或写进配置）再调用 LithosAI",
                code="missing_key",
            )
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self.config.extra_headers)
        return headers

    # ---- 主调用 ---------------------------------------------------
    def complete(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        tier: Optional[str] = None,
        urgency: Optional[float] = None,
        kind: Optional[str] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        thinking: Optional[bool] = None,
        max_retries: int = 2,
    ) -> LLMReply:
        """发一次补全。

        档位优先级：``tier`` > ``kind``/``urgency`` 推断。重试只针对网络错误与 5xx，
        429 退避后重试，4xx 直接抛——把参数错误反复重试只会浪费额度。
        """
        if httpx is None:
            raise LithosError("缺少 httpx，无法调用 LithosAI", code="missing_httpx")
        chosen = tier or choose_tier(urgency, kind=kind)
        cfg = self.config.tier(chosen)
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": list(messages),
            "temperature": cfg.temperature if temperature is None else float(temperature),
            "max_tokens": cfg.max_tokens if max_tokens is None else int(max_tokens),
            "stream": False,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = tool_choice or "auto"
        if thinking is not None:
            # DeepSeek thinking 模式开关。判定类调用必须显式关掉：否则模型只吐
            # reasoning_content、content 为空，拿不到那个单词（实测 deepseek-flash）。
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}

        url = f"{self.config.base_url}/chat/completions"
        headers = self._headers()
        last_error: Optional[LithosError] = None
        for attempt in range(max_retries + 1):
            started = time.monotonic()
            try:
                response = self._http().post(url, headers=headers, json=payload,
                                             timeout=cfg.timeout)
            except Exception as error:  # noqa: BLE001 - 网络层什么都可能抛
                last_error = LithosError(f"连接 LithosAI 失败：{error}", code="network")
                if attempt >= max_retries:
                    raise last_error
                time.sleep(0.6 * (attempt + 1))
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            text_body = response.text or ""
            if response.status_code == 429 or response.status_code >= 500:
                last_error = LithosError(
                    f"LithosAI 暂时不可用（HTTP {response.status_code}）",
                    status=response.status_code,
                    body=text_body[:500],
                    code="retryable",
                )
                if attempt >= max_retries:
                    raise last_error
                time.sleep(0.8 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise LithosError(
                    f"LithosAI 返回 HTTP {response.status_code}：{text_body[:300]}",
                    status=response.status_code,
                    body=text_body[:1000],
                    code="http_error",
                )

            try:
                data = response.json()
            except Exception as error:  # noqa: BLE001
                raise LithosError(f"LithosAI 返回的不是 JSON：{error}", body=text_body[:500], code="bad_json") from error

            try:
                message = data["choices"][0]["message"]
            except Exception as error:  # noqa: BLE001
                raise LithosError("LithosAI 响应缺少 choices[0].message", body=text_body[:500], code="bad_shape") from error

            return LLMReply(
                text=(message.get("content") or "").strip(),
                tool_calls=list(message.get("tool_calls") or []),
                tier=cfg.name,
                model=cfg.model,
                latency_ms=latency_ms,
                usage=dict(data.get("usage") or {}),
                # 非流式也可能带 thinking；同样要留着回传。
                reasoning=str(message.get("reasoning_content") or ""),
            )

        raise last_error or LithosError("LithosAI 调用失败但没有捕获到原因", code="unknown")

    # ---- 流式调用 -------------------------------------------------
    def stream_complete(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        tier: Optional[str] = None,
        urgency: Optional[float] = None,
        kind: Optional[str] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
        on_delta: Optional[Any] = None,
        deadline_s: Optional[float] = None,
        thinking: Optional[bool] = True,
        omit_max_tokens: bool = False,
    ) -> LLMReply:
        """流式补全：边收边回调，TUI 用来看"它正在想什么"。

        ``on_delta(kind, text)`` 会被反复调用，``kind`` 是 ``'reasoning'``（模型的
        思考过程）或 ``'text'``（最终要说的话）。工具调用在流里是分片的，这里按
        ``index`` 累积成完整调用，返回值与 :meth:`complete` 同形。

        ``deadline_s`` 是整次流式调用的墙钟预算（默认 ``None`` = 不关闭，永久等；用户 2026-11-04 拍板）。
        每读到一行先看累计耗时：超预算就用**同一套** ``on_delta(kind, text)``
        事件形状发一个 ``on_delta('abort', 说明)`` 中止事件，随即停止读取并返回
        已收到的部分（``LLMReply.aborted`` 为 True）。这条路径不依赖 httpx 超时——
        httpx 超时只管单次 socket 读，管不住"慢慢吐字"的总时长。
        """
        if httpx is None:
            raise LithosError("缺少 httpx，无法调用 LithosAI", code="missing_httpx")
        chosen = tier or choose_tier(urgency, kind=kind)
        cfg = self.config.tier(chosen)
        payload: Dict[str, Any] = {
            "model": cfg.model,
            "messages": list(messages),
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens if max_tokens is None else int(max_tokens),
            "stream": True,
            # 2026-11-04 用户拍板：慢系统**永久开思考**（原来是 disabled）。
            # 传 thinking=False 可以显式关掉（给「判定类」调用留口子）。
            "thinking": {"type": "enabled" if (thinking is None or thinking) else "disabled"},
        }
        if omit_max_tokens:
            payload.pop("max_tokens", None)
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = tool_choice or "auto"

        url = f"{self.config.base_url}/chat/completions"
        headers = self._headers()
        started = time.monotonic()
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: Dict[int, Dict[str, Any]] = {}
        usage: Dict[str, Any] = {}
        aborted = False
        deadline = None if deadline_s is None else float(deadline_s)
        # 复用连接但保持原来的缩进层级：外层用 nullcontext 顶替原来的 with httpx.Client(...)。
        with contextlib.nullcontext(self._http()) as client:
            with client.stream("POST", url, headers=headers, json=payload,
                               timeout=cfg.timeout) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", "replace")
                    raise LithosError(
                        f"LithosAI 返回 HTTP {response.status_code}：{body[:300]}",
                        status=response.status_code,
                        body=body[:1000],
                        code="http_error",
                    )
                for line in response.iter_lines():
                    if deadline is not None:
                        elapsed = time.monotonic() - started
                        if elapsed > deadline:
                            aborted = True
                            if on_delta is not None:
                                on_delta(
                                    "abort",
                                    f"streaming deadline {deadline:g}s exceeded after {elapsed:.2f}s",
                                )
                            break
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        data = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if data.get("usage"):
                        usage = dict(data["usage"])
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or ""
                        if reasoning:
                            # 必须留在返回值里：下一轮要原样回传，否则 thinking 模式直接 400。
                            reasoning_parts.append(reasoning)
                            if on_delta is not None:
                                on_delta("reasoning", reasoning)
                        text = delta.get("content") or ""
                        if text:
                            content_parts.append(text)
                            if on_delta is not None:
                                on_delta("text", text)
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index") or 0)
                            slot = tool_calls.setdefault(
                                index,
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["function"]["name"] += function["name"]
                            if function.get("arguments"):
                                slot["function"]["arguments"] += function["arguments"]
        ordered = [tool_calls[index] for index in sorted(tool_calls)]
        for position, call in enumerate(ordered):
            if not call.get("id"):
                call["id"] = f"call_{position}"
            if not call["function"].get("name"):
                ordered = [item for item in ordered if item["function"].get("name")]
                break
        return LLMReply(
            text="".join(content_parts).strip(),
            tool_calls=ordered,
            tier=cfg.name,
            model=cfg.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=usage,
            reasoning="".join(reasoning_parts),
            aborted=aborted,
        )

    # ---- 便捷方法 -------------------------------------------------
    def react(
        self,
        prompt: str,
        images: Sequence[str] = (),
        *,
        urgency: float = 0.9,
        system: Optional[str] = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMReply:
        """看一眼就要动：默认走 ultra。"""
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": build_user_content(prompt, images)})
        return self.complete(messages, urgency=urgency, tools=tools, max_tokens=max_tokens)

    def plan(self, prompt: str, *, system: Optional[str] = None, tools: Optional[Sequence[Dict[str, Any]]] = None) -> LLMReply:
        """想清楚再动：默认走 fast。"""
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.complete(messages, kind="plan", tools=tools)

    def reflect(self, prompt: str, *, system: Optional[str] = None) -> LLMReply:
        """事后总结：默认走 base。"""
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.complete(messages, kind="reflect")

    def probe(self) -> Dict[str, Any]:
        """探活：用最便宜的档位发一句最短的话，返回延迟与档位。"""
        reply = self.complete([{"role": "user", "content": "ping"}], tier="base", max_tokens=8, max_retries=0)
        return {"ok": True, "tier": reply.tier, "model": reply.model, "latency_ms": reply.latency_ms}


def redact(text: str, limit: int = 300) -> str:
    """把可能出现的 key 片段抹掉，便于写日志（单次扫描，避免替换后再次命中原位置）。"""
    if not text:
        return ""
    value = _SECRET_PATTERN.sub(lambda match: match.group(1) + "<redacted>", text)
    return value[:limit]


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODELS",
    "KIND_TO_TIER",
    "LithosClient",
    "LithosConfig",
    "LithosError",
    "LLMReply",
    "TierConfig",
    "build_user_content",
    "choose_tier",
    "redact",
]
