"""低成本 Live 帧区域变化检测，不提供 OCR 或 UI 语义识别。

接线示例：创建 FastPerception(rois={"dialogue_text": {"x": 0.1, "y": 0.7,
"width": 0.8, "height": 0.2}}, hz=10)，把每条 Live 消息传给 observe。
返回的事件只表示指定矩形的像素外观变化；区域名称不证明对话、说话人
或选项存在。Live 可另行提供同帧 UI 状态及可选对话区域诊断，
本检测器不使用这些信号，也不授权解除对话锁。

首个有效帧仅建立基线。只有同一尺寸、同一会话的后续有效采样才会比较；
低于阈值的变化累积到下一次事件，事件后该区域的基线才更新。只保存
每个区域的小型 RGB 特征，不保存 JPEG、原图或完整帧。对象为单调用线程设计。

Live 的 frame 不携带 sessionId，且序号回退不能区分乱序与重启。
调用方必须在已知会话边界调用 reset()，或转发 hello/live.started/
live.stopped；如果重启没有边界消息，则必须由连接管理器显式 reset。
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import time
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from PIL import Image, ImageFile, UnidentifiedImageError


_REGION_NAMES = frozenset({"dialogue_text", "speaker", "choice", "quest", "hud"})


class FastPerception:
    """从配置区域生成 region_changed 事件；空 ROI 时不处理图像。"""

    def __init__(
        self,
        rois: Optional[Mapping[str, Mapping[str, float]]] = None,
        *,
        hz: float = 10.0,
        threshold: float = 0.08,
        sample_size: int = 32,
        max_jpeg_bytes: int = 2 * 1024 * 1024,
        max_pixels: int = 16_000_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """ROI 的 x/y/width/height 均为相对整帧的 0–1 坐标。

        threshold 是 RGB 平均绝对差除以 255 的阈值，非置信度。
        hz 限制图像实际解码频率；clock 必须为单调秒时钟。
        """
        if not isinstance(rois, (Mapping, type(None))):
            raise ValueError("rois 必须是区域名称到归一化矩形的映射")
        if isinstance(hz, bool) or not isinstance(hz, (int, float)) or not math.isfinite(hz) or not 5 <= hz <= 15:
            raise ValueError("hz 必须在 5–15 之间")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= 1:
            raise ValueError("threshold 必须大于 0 且不大于 1")
        if type(sample_size) is not int or not 1 <= sample_size <= 128:
            raise ValueError("sample_size 必须是 1–128 的整数")
        if type(max_jpeg_bytes) is not int or not 1 <= max_jpeg_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_jpeg_bytes 必须是 1–16777216 的整数")
        if type(max_pixels) is not int or not 1 <= max_pixels <= 40_000_000:
            raise ValueError("max_pixels 必须是 1–40000000 的整数")
        if not callable(clock):
            raise ValueError("clock 必须可调用")

        self._rois: dict[str, tuple[float, float, float, float]] = {}
        for name, roi in (rois or {}).items():
            if name not in _REGION_NAMES or not isinstance(roi, Mapping) or set(roi) != {"x", "y", "width", "height"}:
                raise ValueError("区域名称或 ROI 字段无效")
            values = tuple(roi[key] for key in ("x", "y", "width", "height"))
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
                raise ValueError("ROI 坐标必须为有限数字")
            x, y, width, height = values
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                raise ValueError("ROI 必须是整帧内的非空归一化矩形")
            self._rois[name] = (float(x), float(y), float(width), float(height))
        self._interval = 1.0 / hz
        self._threshold = float(threshold)
        self._sample_size = sample_size
        self._max_jpeg_bytes = max_jpeg_bytes
        self._max_pixels = max_pixels
        self._clock = clock
        self._session_id: Optional[str] = None
        self.reset()

    def reset(self) -> None:
        """清除序号、节流时间和全部区域基线，调用方在确认换会话时使用。"""
        self._last_seen_seq: Optional[int] = None
        self._last_sample_at: Optional[float] = None
        self._frame_size: Optional[tuple[int, int]] = None
        self._features: dict[str, bytes] = {}

    def observe(self, message: Mapping[str, Any]) -> list[dict[str, Any]]:
        """接收原始 Live 消息，返回零个或多个不含图像的事件。

        frame 必须提供非负整数 seq、带时区的 timestampUtc、JPEG base64。
        事件携带原始 frame_seq、timestamp、区域名和变化幅度。无效、重复、
        乱序或被节流的帧静默跳过；被节流的序号仍记入去重高水位。
        不会自动把序号回退解释为会话重启。
        """
        if not isinstance(message, Mapping):
            return []
        kind = message.get("type")
        if kind == "hello":
            session = message.get("sessionId")
            if isinstance(session, str) and session:
                if self._session_id is not None and session != self._session_id:
                    self.reset()
                self._session_id = session
            return []
        if kind in ("live.started", "live.stopped"):
            self.reset()
            self._session_id = None
            return []
        if kind != "frame" or not self._rois:
            return []
        seq = message.get("seq")
        timestamp = message.get("timestampUtc")
        jpeg = message.get("jpeg")
        if type(seq) is not int or not 0 <= seq <= 2**63 - 1 or not self._valid_timestamp(timestamp):
            return []
        if self._last_seen_seq is not None and seq <= self._last_seen_seq:
            return []
        if not isinstance(jpeg, str) or not jpeg or len(jpeg) > 4 * ((self._max_jpeg_bytes + 2) // 3):
            return []

        now = self._clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            return []
        if self._last_sample_at is not None and now - self._last_sample_at < self._interval:
            # 不解码被跳过的帧，避免稍后重放同一帧绕过节流。
            self._last_seen_seq = seq
            return []
        # 失败的解码也占用一次配额，防止异常输入绕过节流反复解码。
        self._last_sample_at = now
        features = self._decode_features(jpeg)
        if features is None:
            return []
        size, current = features
        self._last_seen_seq = seq
        if size != self._frame_size:
            self._frame_size, self._features = size, current
            return []

        events = []
        for name, feature in current.items():
            previous = self._features[name]
            change = sum(abs(a - b) for a, b in zip(feature, previous)) / (255 * len(feature))
            if change >= self._threshold:
                events.append({"type": "region_changed", "frame_seq": seq,
                               "timestamp": timestamp, "region": name, "change": change})
                self._features[name] = feature
        return events

    @staticmethod
    def _valid_timestamp(value: Any) -> bool:
        """只接受带时区的源时间，并原样保留在事件中。"""
        if not isinstance(value, str) or not value or len(value) > 64:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.utcoffset() is not None
        except (ValueError, OverflowError):
            return False

    def _decode_features(self, jpeg: str) -> Optional[tuple[tuple[int, int], dict[str, bytes]]]:
        """限制编码、尺寸和格式后提取小样本；异常帧不会改变检测状态。"""
        try:
            raw = base64.b64decode(jpeg, validate=True)
            if len(raw) > self._max_jpeg_bytes or not raw.startswith(b"\xff\xd8"):
                return None
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if image.format != "JPEG" or width <= 0 or height <= 0 or width * height > self._max_pixels:
                    return None
                # 禁止把截断图片当作成功帧，即使外部更改 Pillow 全局选项。
                if not raw.endswith(b"\xff\xd9") or ImageFile.LOAD_TRUNCATED_IMAGES:
                    return None
                image.load()
                result = {}
                for name, (x, y, w, h) in self._rois.items():
                    box = (int(x * width), int(y * height),
                           min(width, math.ceil((x + w) * width)),
                           min(height, math.ceil((y + h) * height)))
                    with image.crop(box) as region:
                        with region.convert("RGB") as rgb:
                            with rgb.resize((self._sample_size, self._sample_size), Image.Resampling.BILINEAR) as small:
                                result[name] = small.tobytes()
                return (width, height), result
        except (binascii.Error, ValueError, TypeError, OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
            return None
