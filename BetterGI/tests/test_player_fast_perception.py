"""仅使用合成 JPEG 测试帧区域变化，不启动游戏或调用网络。"""

from __future__ import annotations

import base64
import io
import unittest
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from integrations.player.player_fast_perception import FastPerception


LEFT = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}
RIGHT = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}


class Clock:
    """提供无需等待的可控单调秒时钟。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds=0.11):
        self.now += seconds


def jpeg(left=0, right=0, *, size=(128, 64), image_format="JPEG"):
    """生成左右纯色矩形；JPEG 边界与 ROI 对齐避免测试色彩混叠。"""
    image = Image.new("RGB", size, (left, left, left))
    ImageDraw.Draw(image).rectangle((size[0] // 2, 0, size[0] - 1, size[1] - 1), fill=(right, right, right))
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, quality=95, subsampling=0)
    image.close()
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def frame(seq, image, timestamp=None):
    """构造 Live 帧并保留实际来源序号和 UTC 时间。"""
    if timestamp is None:
        timestamp = (datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=seq * 100)).isoformat()
    return {"type": "frame", "seq": seq, "timestampUtc": timestamp, "jpeg": image}


class FastPerceptionTests(unittest.TestCase):
    """覆盖变化、节流、会话隔离与不可信的 JPEG 输入。"""

    def setUp(self):
        self.clock = Clock()
        self.detector = FastPerception({"dialogue_text": LEFT, "hud": RIGHT}, clock=self.clock)
        self.black = jpeg()
        self.white_left = jpeg(left=255)

    def test_region_changes_are_not_dialogue_or_ocr(self):
        self.assertEqual(self.detector.observe(frame(1, self.black)), [])
        self.clock.advance()
        timestamp = "2025-01-01T12:00:01.1234567Z"
        events = self.detector.observe(frame(2, self.white_left, timestamp))
        self.assertEqual(len(events), 1)
        self.assertEqual({k: events[0][k] for k in ("type", "frame_seq", "timestamp", "region")},
                         {"type": "region_changed", "frame_seq": 2, "timestamp": timestamp, "region": "dialogue_text"})
        self.assertGreater(events[0]["change"], 0.9)
        self.assertEqual(set(events[0]), {"type", "frame_seq", "timestamp", "region", "change"})
        self.clock.advance()
        self.assertEqual(self.detector.observe(frame(3, self.white_left)), [])
        self.clock.advance()
        self.assertEqual([e["region"] for e in self.detector.observe(frame(4, jpeg(255, 255)))], ["hud"])

    def test_all_supported_rois_and_cumulative_changes(self):
        detector = FastPerception({name: LEFT for name in ("dialogue_text", "speaker", "choice", "quest", "hud")},
                                  threshold=0.1, clock=self.clock)
        self.assertEqual(detector.observe(frame(1, jpeg())), [])
        self.clock.advance()
        self.assertEqual(detector.observe(frame(2, jpeg(15))), [])
        self.clock.advance()
        events = detector.observe(frame(3, jpeg(30)))
        self.assertEqual({e["region"] for e in events}, {"dialogue_text", "speaker", "choice", "quest", "hud"})
        self.clock.advance()
        self.assertEqual(detector.observe(frame(4, jpeg(30))), [])

    def test_duplicate_out_of_order_and_dropped_seq_never_advance_baseline(self):
        self.assertEqual(self.detector.observe(frame(10, self.black)), [])
        self.clock.advance()
        self.assertEqual(self.detector.observe(frame(10, self.white_left)), [])
        self.assertEqual(self.detector.observe(frame(9, self.white_left)), [])
        self.assertEqual(len(self.detector.observe(frame(11, self.white_left))), 1)
        self.assertEqual(self.detector.observe(frame(12, self.black)), [])  # 节流丢帧
        self.clock.advance()
        self.assertEqual(self.detector.observe(frame(12, self.black)), [])  # 丢帧不可重放
        self.assertEqual(self.detector.observe(frame(11, self.black)), [])
        self.assertEqual(len(self.detector.observe(frame(13, self.black))), 1)

    def test_session_reset_and_resolution_change_rebaseline(self):
        self.detector.observe({"type": "hello", "sessionId": "one"})
        self.detector.observe(frame(100, self.black))
        self.detector.observe({"type": "hello", "sessionId": "one"})
        self.clock.advance()
        self.assertEqual(len(self.detector.observe(frame(101, self.white_left))), 1)
        self.detector.observe({"type": "hello", "sessionId": "two"})
        self.assertEqual(self.detector.observe(frame(1, self.black)), [])
        self.clock.advance()
        self.assertEqual(self.detector.observe(frame(2, jpeg(255, size=(64, 32)))), [])
        self.clock.advance()
        self.assertEqual(len(self.detector.observe(frame(3, jpeg(size=(64, 32))))), 1)
        self.detector.reset()
        self.assertEqual(self.detector.observe(frame(1, self.black)), [])
        self.detector.observe({"type": "live.stopped"})
        self.assertEqual(self.detector.observe(frame(1, self.white_left)), [])
        self.detector.observe({"type": "live.started"})
        self.assertEqual(self.detector.observe(frame(1, self.black)), [])

    def test_empty_config_never_decodes_or_produces_facts(self):
        detector = FastPerception(clock=self.clock)
        detector._decode_features = lambda _: self.fail("无 ROI 不应解码")
        self.assertEqual(detector.observe(frame(1, self.black)), [])
        self.clock.advance()
        self.assertEqual(detector.observe(frame(2, self.white_left)), [])

    def test_limits_and_malformed_frames_do_not_poison_baseline(self):
        detector = FastPerception({"speaker": LEFT}, clock=self.clock, max_jpeg_bytes=10000, max_pixels=8192)
        self.assertEqual(detector.observe(frame(1, self.black)), [])
        bad = ["not-base64!", base64.b64encode(b"not jpeg").decode(),
               base64.b64encode(b"\xff\xd8garbage\xff\xd9").decode(),
               self.white_left[:-4], jpeg(image_format="PNG"), "A" * 14000,
               jpeg(size=(129, 64)), "\udcff"]
        for image in bad:
            self.clock.advance()
            self.assertEqual(detector.observe(frame(500, image)), [])
        self.clock.advance()
        # 序号 500 的无效帧不应覆盖有效基线和序号。
        self.assertEqual(len(detector.observe(frame(2, self.white_left))), 1)

    def test_failed_decode_also_uses_rate_budget(self):
        detector = FastPerception({"choice": LEFT}, clock=self.clock)
        self.assertEqual(detector.observe(frame(1, "invalid!")), [])
        self.assertEqual(detector.observe(frame(2, self.black)), [])
        self.clock.advance()
        self.assertEqual(detector.observe(frame(3, self.black)), [])
        self.clock.advance()
        self.assertEqual(len(detector.observe(frame(4, self.white_left))), 1)

    def test_rate_limits_5_to_15_hz(self):
        for hz in (5, 10, 15):
            clock = Clock()
            detector = FastPerception({"quest": LEFT}, hz=hz, clock=clock)
            detector.observe(frame(1, self.black))
            clock.advance(1 / hz - 0.00001)
            self.assertEqual(detector.observe(frame(2, self.white_left)), [])
            clock.advance(0.00002)
            events = detector.observe(frame(3, self.white_left))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["frame_seq"], 3)

    def test_bad_metadata_and_configuration(self):
        for seq, timestamp in ((True, "2025-01-01T00:00:00Z"), (-1, "2025-01-01T00:00:00Z"),
                               (1, None), (1, "bad"), (1, "2025-01-01T00:00:00")):
            self.assertEqual(self.detector.observe({"type": "frame", "seq": seq, "timestampUtc": timestamp, "jpeg": self.black}), [])
        invalid_configs = [
            {"rois": {"unknown": LEFT}}, {"rois": {"hud": {"x": 0, "y": 0, "width": 0, "height": 1}}},
            {"rois": {"hud": {"x": 0.8, "y": 0, "width": 0.3, "height": 1}}},
            {"rois": {"hud": {"x": float("nan"), "y": 0, "width": 0.2, "height": 1}}},
            {"hz": 4}, {"hz": 16}, {"threshold": 0}, {"max_pixels": 0}, {"sample_size": 0},
        ]
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                FastPerception(**config)


if __name__ == "__main__":
    unittest.main()
