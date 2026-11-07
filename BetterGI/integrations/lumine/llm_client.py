#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""参考导演使用的可选 LLM 客户端。

支持两类提供方：

- ``anthropic``：官方 Messages API，图像作为 base64 image block，system 单独传。
- ``openai``：OpenAI 兼容的 ``/chat/completions``，图像作为 ``image_url`` data URL。

只做一件事：把系统提示、用户文本和可选 JPEG 帧发给模型，返回纯文本。
提示词构建、JSON 解析和工具调用都在 ``live_director.py``。

环境变量：

- ``LLM_PROVIDER``：``anthropic`` 或 ``openai``；缺省时按已配置的密钥推断。
- ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_MODEL`` / ``ANTHROPIC_BASE_URL``
- ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` / ``OPENAI_BASE_URL``

本模块依赖 ``aiohttp``；未安装时仍可导入（便于运行不依赖网络的单元测试），
但调用 ``complete`` 会给出可读错误。
"""

import asyncio
import json
import os

try:
    import aiohttp
except ImportError:  # pragma: no cover - 由运行环境决定
    aiohttp = None


DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 60.0


class LLMError(Exception):
    """LLM 配置或 HTTP 调用错误，消息面向最终用户。"""

    def __init__(self, message, status=0, body=""):
        super().__init__(message)
        self.status = int(status or 0)
        self.body = body or ""


def resolve_provider(provider=None, env=None):
    """解析提供方名称；未指定时按环境变量中的密钥推断。"""
    env = os.environ if env is None else env
    value = (provider or env.get("LLM_PROVIDER") or "").strip().lower()
    if value:
        if value not in ("anthropic", "openai"):
            raise LLMError(
                "不支持的 LLM_PROVIDER={0!r}，只能是 anthropic 或 openai。".format(
                    value
                )
            )
        return value

    if env.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if env.get("OPENAI_API_KEY"):
        return "openai"
    raise LLMError(
        "未配置 LLM。请设置 LLM_PROVIDER 并配置对应的 ANTHROPIC_API_KEY "
        "或 OPENAI_API_KEY；也可以使用 --no-llm / --dry-run。"
    )


def build_anthropic_payload(
    model,
    system_prompt,
    user_text,
    image_b64=None,
    media_type="image/jpeg",
    max_tokens=DEFAULT_MAX_TOKENS,
):
    """构造 Anthropic Messages API 请求体（图像在前，文本在后）。"""
    content = []
    if image_b64:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                },
            }
        )
    content.append({"type": "text", "text": user_text})
    return {
        "model": model,
        "max_tokens": int(max_tokens),
        "system": system_prompt,
        "messages": [{"role": "user", "content": content}],
    }


def build_openai_payload(
    model,
    system_prompt,
    user_text,
    image_b64=None,
    media_type="image/jpeg",
    max_tokens=DEFAULT_MAX_TOKENS,
):
    """构造 OpenAI 兼容 chat/completions 请求体（图像使用 data URL）。"""
    user_content = []
    if image_b64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:{0};base64,{1}".format(media_type, image_b64)
                },
            }
        )
    user_content.append({"type": "text", "text": user_text})
    return {
        "model": model,
        "max_tokens": int(max_tokens),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }


def extract_anthropic_text(data):
    """从 Anthropic 响应中拼接所有 text block。"""
    parts = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_openai_text(data):
    """从 OpenAI 兼容响应中提取文本；兼容字符串与分段内容。"""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _error_message_from_body(body):
    """尽量从错误响应中提取可读信息。"""
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return text[:600]

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type")
            if message:
                return str(message)
        elif isinstance(error, str) and error:
            return error
        for key in ("message", "detail", "title"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return text[:600]


def make_http_error(provider, status, body):
    """把非 2xx 响应转换成可读的 LLMError。"""
    detail = _error_message_from_body(body)
    message = "{0} API 返回 HTTP {1}".format(provider, status)
    if detail:
        message += "：" + detail
    if status == 401:
        message += "（API 密钥无效或已过期）"
    elif status == 429:
        message += "（触发限流，请降低频率或稍后重试）"
    return LLMError(message, status=status, body=body)


class LLMClient:
    """极简异步 LLM 客户端。

    ``session`` 可以由调用方传入以复用连接；不传时每次 ``complete`` 自行创建
    并关闭会话。
    """

    def __init__(
        self,
        provider,
        api_key,
        model=None,
        base_url=None,
        timeout=DEFAULT_TIMEOUT,
        max_tokens=DEFAULT_MAX_TOKENS,
        session=None,
    ):
        self.provider = (provider or "").strip().lower()
        if self.provider not in ("anthropic", "openai"):
            raise LLMError(
                "LLM provider 必须是 anthropic 或 openai，实际为 {0!r}。".format(
                    provider
                )
            )
        if not api_key:
            raise LLMError(
                "缺少 API 密钥。请设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY。"
            )
        self.api_key = api_key
        self.base_url = (base_url or "").strip() or None
        self.timeout = float(timeout)
        self.max_tokens = int(max_tokens)
        self._session = session

        if self.provider == "anthropic":
            self.model = model or DEFAULT_ANTHROPIC_MODEL
        else:
            self.model = model or DEFAULT_OPENAI_MODEL
            if self.base_url is None:
                self.base_url = DEFAULT_OPENAI_BASE_URL

    @classmethod
    def from_env(cls, env=None, session=None):
        """按环境变量创建客户端。"""
        env = os.environ if env is None else env
        provider = resolve_provider(env=env)
        if provider == "anthropic":
            api_key = env.get("ANTHROPIC_API_KEY") or env.get("LLM_API_KEY") or ""
            model = env.get("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL
            base_url = env.get("ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_URL
        else:
            api_key = env.get("OPENAI_API_KEY") or env.get("LLM_API_KEY") or ""
            model = env.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
            base_url = env.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
        if not api_key:
            raise LLMError(
                "缺少 {0} 的 API 密钥，请设置 {1}。".format(
                    provider,
                    "ANTHROPIC_API_KEY"
                    if provider == "anthropic"
                    else "OPENAI_API_KEY",
                )
            )
        try:
            max_tokens = int(env.get("LLM_MAX_TOKENS") or DEFAULT_MAX_TOKENS)
        except (TypeError, ValueError):
            max_tokens = DEFAULT_MAX_TOKENS
        return cls(
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            max_tokens=max_tokens,
            session=session,
        )

    @property
    def endpoint(self):
        """计算最终请求地址；允许 base_url 已经是完整端点。"""
        if self.provider == "anthropic":
            base = (self.base_url or DEFAULT_ANTHROPIC_URL).rstrip("/")
            if base.endswith("/messages"):
                return base
            if base.endswith("/v1"):
                return base + "/messages"
            return base + "/v1/messages"

        base = (self.base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def build_payload(
        self, system_prompt, user_text, image_b64=None, media_type="image/jpeg"
    ):
        """按提供方构造请求体。"""
        if self.provider == "anthropic":
            return build_anthropic_payload(
                self.model,
                system_prompt,
                user_text,
                image_b64=image_b64,
                media_type=media_type,
                max_tokens=self.max_tokens,
            )
        payload = build_openai_payload(
            self.model,
            system_prompt,
            user_text,
            image_b64=image_b64,
            media_type=media_type,
            max_tokens=self.max_tokens,
        )
        # DeepSeek 等推理模型支持降低思考预算；未设置时不改请求体。
        effort = os.environ.get("OPENAI_REASONING_EFFORT")
        if effort:
            payload["reasoning_effort"] = effort
        return payload

    def headers(self):
        """构造鉴权与内容类型请求头。"""
        if self.provider == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
        return {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }

    async def complete(
        self, system_prompt, user_text, image_b64=None, media_type="image/jpeg"
    ):
        """调用模型并返回文本；非 2xx 抛出 ``LLMError``。"""
        if aiohttp is None:
            raise LLMError("需要 aiohttp 才能调用 LLM，请先执行：pip install aiohttp")

        payload = self.build_payload(
            system_prompt,
            user_text,
            image_b64=image_b64,
            media_type=media_type,
        )

        own_session = self._session is None
        if own_session:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        else:
            session = self._session

        try:
            async with session.post(
                self.endpoint, json=payload, headers=self.headers()
            ) as response:
                body = await response.text()
                if response.status < 200 or response.status >= 300:
                    raise make_http_error(self.provider, response.status, body)
                try:
                    data = json.loads(body)
                except (TypeError, ValueError):
                    raise LLMError(
                        "{0} API 返回的不是有效 JSON：{1}".format(
                            self.provider, body[:300]
                        ),
                        status=response.status,
                        body=body,
                    ) from None
        except aiohttp.ClientError as exc:
            raise LLMError("无法连接 {0} API：{1}".format(self.provider, exc)) from None
        except asyncio.TimeoutError:
            raise LLMError(
                "{0} API 请求超时（{1:.0f} 秒）。".format(self.provider, self.timeout)
            ) from None
        finally:
            if own_session:
                await session.close()

        if self.provider == "anthropic":
            text = extract_anthropic_text(data)
        else:
            text = extract_openai_text(data)
        if not text:
            raise LLMError("{0} API 未返回文本内容。".format(self.provider))
        return text

    async def close(self):
        """关闭调用方传入的会话（如果本客户端持有它）。"""
        if self._session is not None:
            await self._session.close()
            self._session = None
