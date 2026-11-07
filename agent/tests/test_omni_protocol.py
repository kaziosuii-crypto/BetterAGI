"""快系统协议回归：事件不越轮、每轮先音频后图像。"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import omni  # noqa: E402


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 10, 10)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _client(script):
    cfg = omni.OmniConfig(api_key="k", model="m")
    transport = omni.ScriptedTransport(script)
    return omni.OmniClient(cfg, transport=transport), transport


def test_collect_does_not_stop_at_text_done() -> None:
    """text.done 之后必须继续读到 response.done，否则事件会串到下一轮。"""

    script = [
        {"type": "response.text.delta", "delta": "你好"},
        {"type": "response.text.done", "text": "你好"},
        {"type": "response.output_item.done"},
        {"type": "response.done", "response": {"output": []}},
    ]
    client, transport = _client(script)
    reply = asyncio.run(client.collect())
    assert reply.text == "你好"
    # 4 个事件全被这一轮吃掉，下一个响应体不会被污染
    assert reply.raw_events == 4
    assert transport.sent == []


def test_ask_seeds_audio_before_image_every_turn() -> None:
    """commit 清空缓冲，所以每轮都要重发音频；否则第二轮报 append image before audio。"""

    script = [
        {"type": "response.text.done", "text": "1"},
        {"type": "response.done", "response": {"output": []}},
    ] * 2
    client, transport = _client(script)

    asyncio.run(client.ask(_jpeg(), instructions="a"))
    asyncio.run(client.ask(_jpeg(), instructions="b"))

    kinds = [event["type"] for event in transport.sent]
    audio = [i for i, k in enumerate(kinds) if k == "input_audio_buffer.append"]
    image = [i for i, k in enumerate(kinds) if k == "input_image_buffer.append"]
    assert len(audio) == 2 and len(image) == 2
    # 每一对里，音频必须排在图像之前
    for a, i in zip(audio, image):
        assert a < i, kinds


def test_url_requires_model_query_param() -> None:
    """公共入口不带 ?model= 会被服务端 1009 拒绝（实测）。"""

    cfg = omni.OmniConfig(api_key="k", model="qwen3.8-omni-flash-realtime")
    url = cfg.resolve_url()
    assert "model=" in url
    assert "qianwenaiapi.com/api-ws/v1/realtime" in url


def test_session_update_carries_model() -> None:
    cfg = omni.OmniConfig(api_key="k", model="qwen3.8-omni-flash-realtime")
    body = cfg.session_update()["session"]
    assert body["model"] == "qwen3.8-omni-flash-realtime"
    assert body["modalities"] == ["text"]
