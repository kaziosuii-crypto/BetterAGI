"""大帧降质重编测试：不联网、不用真 GPU，Pillow 只做本地编解码。

覆盖设计 03 §C.1 的 P2：encode_jpeg_frame 超过 MAX_IMAGE_BYTES 不再直接炸，
而是先降质量、再降尺寸，把 4K 截图救回来；救不回来 / 缺 Pillow 时仍然显式报错。
"""

import base64
import io
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import omni  # noqa: E402


def _encode_jpeg(image: Image.Image, quality: int = 95) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _noise_image(size: int) -> Image.Image:
    """确定性伪噪声：Pillow 的 effect_noise 用内部随机源，体积稳定偏大。"""

    return Image.effect_noise((size, size), 64).convert("RGB")


def _gradient_4k() -> Image.Image:
    """廉价的 4K 正方形：8x8 渐变放大，避免在测试里逐像素画 1470 万次。"""

    tiny = Image.new("RGB", (8, 8))
    px = tiny.load()
    for y in range(8):
        for x in range(8):
            px[x, y] = (x * 31, y * 31, (x * 7 + y * 11) % 256)
    return tiny.resize((3840, 3840), Image.BILINEAR)


def _assert_jpeg(blob: bytes) -> None:
    assert blob[:3] == b"\xff\xd8\xff", "重编结果必须仍是 JPEG（FFD8FF）"


def test_small_jpeg_passes_through_unchanged():
    small = _encode_jpeg(_gradient_4k().resize((64, 64), Image.BILINEAR))
    assert len(small) <= omni.MAX_IMAGE_BYTES

    b64, meta = omni.encode_jpeg_frame_ex(small)

    assert base64.b64decode(b64) == small
    assert meta["reencoded"] is False
    assert meta["attempts"] == []
    assert meta["bytes"] == len(small)
    assert omni.encode_jpeg_frame(small) == b64


def test_oversize_frame_is_downscaled_until_it_fits():
    frame = _encode_jpeg(_noise_image(1600))
    assert len(frame) > omni.MAX_IMAGE_BYTES, "夹具必须先超限"

    b64, meta = omni.encode_jpeg_frame_ex(frame)
    decoded = base64.b64decode(b64)

    _assert_jpeg(decoded)
    assert len(decoded) <= omni.MAX_IMAGE_BYTES
    assert meta["reencoded"] is True
    assert meta["original_bytes"] == len(frame)
    # 1600x1600 噪声单靠降质量到 40 仍超限，必须走到宽度档才可能落进 256KB。
    assert meta["scale"] < 1.0
    assert meta["attempts"], "尝试历史不能为空"
    assert meta["attempts"][-1]["bytes"] == meta["bytes"]


def test_4k_square_frame_is_accepted_after_reencode():
    frame = _encode_jpeg(_gradient_4k())
    assert frame[:3] == b"\xff\xd8\xff"
    assert len(frame) > omni.MAX_IMAGE_BYTES, "4K 原图必须先超限，否则没测到重编"

    started = time.time()
    b64, meta = omni.encode_jpeg_frame_ex(frame)
    elapsed = time.time() - started

    decoded = base64.b64decode(b64)
    _assert_jpeg(decoded)
    assert len(decoded) <= omni.MAX_IMAGE_BYTES
    assert meta["reencoded"] is True
    assert meta["width"] == meta["height"], "正方形输入的输出必须仍是正方形"
    assert meta["width"] <= 3840
    assert elapsed < 30.0, "本地重编不该慢到接近阻塞（且测试里也不许真联网/GPU）"


def test_missing_pillow_keeps_old_behaviour_with_clear_message():
    frame = _encode_jpeg(_noise_image(700))
    assert len(frame) > omni.MAX_IMAGE_BYTES

    with mock.patch.dict(sys.modules, {"PIL": None}):
        with pytest.raises(omni.OmniError_) as excinfo:
            omni.encode_jpeg_frame_ex(frame)

    message = str(excinfo.value)
    assert "Pillow" in message
    assert "无法降质重编" in message
    assert str(len(frame)) in message


def test_exhausted_ladder_raises_with_real_bytes_and_tiers():
    frame = _encode_jpeg(_noise_image(320))
    tiny_budget = 64  # 任何 JPEG 的头部都不止 64 B，必然耗尽所有档位

    with pytest.raises(omni.OmniError_) as excinfo:
        omni.encode_jpeg_frame_ex(frame, max_bytes=tiny_budget)

    message = str(excinfo.value)
    assert str(len(frame)) in message
    assert str(tiny_budget) in message
    assert "q85/" in message and "q40/" in message
    assert "55%" in message
    assert "最小仍为" in message


def test_allow_reencode_false_restores_strict_rejection():
    frame = _encode_jpeg(_noise_image(700))
    assert len(frame) > omni.MAX_IMAGE_BYTES

    with mock.patch.dict(sys.modules, {"PIL": None}):
        with pytest.raises(omni.OmniError_) as excinfo:
            omni.encode_jpeg_frame_ex(frame, allow_reencode=False)

    assert "请先降分辨率/质量" in str(excinfo.value)


def test_non_jpeg_is_still_rejected_before_any_reencode():
    png = b"\x89PNG\r\n\x1a\n" + bytes(1024)

    with pytest.raises(omni.OmniError_) as excinfo:
        omni.encode_jpeg_frame_ex(png)

    assert "FFD8FF" in str(excinfo.value)


class ClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """生产路径（OmniClient.send_frame）也要真的不再被大帧炸掉。"""

    async def test_send_frame_accepts_oversize_frame(self):
        frame = _encode_jpeg(_noise_image(700))
        assert len(frame) > omni.MAX_IMAGE_BYTES

        transport = omni.ScriptedTransport([])
        client = omni.OmniClient(
            omni.OmniConfig(api_key="k", workspace_id="ws1"), transport
        )
        await client.connect()
        await client.send_frame(frame)

        self.assertEqual(
            transport.types(),
            ["session.update", "input_audio_buffer.append", "input_image_buffer.append"],
        )
        image_event = transport.sent[-1]
        blob = base64.b64decode(image_event["image"])
        self.assertLessEqual(len(blob), omni.MAX_IMAGE_BYTES)
        self.assertEqual(blob[:3], b"\xff\xd8\xff")
