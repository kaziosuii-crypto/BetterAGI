"""MP4 式帧间差分：只把变化的画面块发出去，服务端拼回整帧再喂模型。

设计要点（2026-11-05）：
- vLLM 只认完整图片，所以差分必须在服务端还原：客户端发 patch，中继（svc/frame_relay.py）
  保存上一帧、拼回整帧、再转给 vLLM。
- 参考帧两边必须逐像素一致，否则误差会累积（视频编码里的漂移）。做法：发送方发完 patch
  后用「自己按同一套规则还原出来的结果」覆盖参考帧，接收方做同样的事。
- 用块（tile）而不是外接矩形：实测 Undertale 一屏只有 2–3% 像素变，但分散在整片文字区，
  外接矩形会退化成 70% 全屏（见 _diag/measure_delta_mp4.py）。
- 变化块太多时自动退回整帧（keyframe），避免「差分比整帧还大」。
- 全静态帧发 MODE_NONE（几十字节），服务端复用上一帧。
"""

import base64
import io
import struct
from typing import Any, Dict, List, Optional, Tuple

MAGIC = b"FDL1"
MODE_FULL = 0
MODE_DELTA = 1
MODE_NONE = 2

HEADER = struct.Struct(">4sBHHBH")  # magic, mode, w, h, tile, count

DEFAULT_TILE = 16
DEFAULT_THRESH = 10
DEFAULT_MIN_TILE_CHANGE = 0.02
DEFAULT_MAX_RATIO = 0.75
DEFAULT_QUALITY = 65


class DeltaError(ValueError):
    """差分编解码失败（格式不对 / 尺寸不匹配 / 缺参考帧）。"""


def decode_image(raw: bytes) -> Any:
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        raise DeltaError("没有 Pillow，无法做帧间差分：%s" % exc)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def encode_image(img: Any, quality: int = DEFAULT_QUALITY, fmt: str = "WEBP") -> bytes:
    buf = io.BytesIO()
    if fmt.upper() == "WEBP":
        img.save(buf, "WEBP", quality=quality, method=4)
    else:
        img.save(buf, fmt.upper(), quality=quality)
    return buf.getvalue()


def _to_png(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def _changed_tiles_numpy(prev: Any, cur: Any, tile: int, thresh: int, min_change: float) -> List[int]:
    import numpy as np

    a = np.asarray(prev, dtype=np.int16)
    b = np.asarray(cur, dtype=np.int16)
    changed = (np.abs(a - b) > thresh).any(axis=2)
    h, w = changed.shape
    pad_h = (-h) % tile
    pad_w = (-w) % tile
    if pad_h or pad_w:
        changed = np.pad(changed, ((0, pad_h), (0, pad_w)), constant_values=False)
    rows = changed.shape[0] // tile
    cols = changed.shape[1] // tile
    blocks = changed.reshape(rows, tile, cols, tile).transpose(0, 2, 1, 3).reshape(rows, cols, tile * tile)
    frac = blocks.mean(axis=2)
    ys, xs = np.nonzero(frac >= min_change)
    return [int(y) * cols + int(x) for y, x in zip(ys, xs)]


def _changed_tiles_pil(prev: Any, cur: Any, tile: int, thresh: int, min_change: float) -> List[int]:
    from PIL import ImageChops

    diff = ImageChops.difference(prev, cur).convert("L")
    width, height = cur.size
    cols = (width + tile - 1) // tile
    rows = (height + tile - 1) // tile
    out: List[int] = []
    for ty in range(rows):
        y0, y1 = ty * tile, min(ty * tile + tile, height)
        for tx in range(cols):
            x0, x1 = tx * tile, min(tx * tile + tile, width)
            hist = diff.crop((x0, y0, x1, y1)).histogram()
            total = sum(hist)
            if total and sum(hist[thresh + 1:]) / total >= min_change:
                out.append(ty * cols + tx)
    return out


def _changed_tiles(prev: Any, cur: Any, tile: int, thresh: int, min_change: float) -> List[int]:
    try:
        return _changed_tiles_numpy(prev, cur, tile, thresh, min_change)
    except ImportError:
        return _changed_tiles_pil(prev, cur, tile, thresh, min_change)


def encode(
    prev_raw: Optional[bytes],
    cur_raw: bytes,
    *,
    tile: int = DEFAULT_TILE,
    thresh: int = DEFAULT_THRESH,
    quality: int = DEFAULT_QUALITY,
    max_ratio: float = DEFAULT_MAX_RATIO,
) -> Tuple[bytes, Dict[str, Any]]:
    """编码一帧。prev_raw 为空（第一帧 / 重连 / 换场）时直接发整帧。"""
    cur = decode_image(cur_raw)
    width, height = cur.size
    quality = int(max(1, min(100, quality)))
    meta: Dict[str, Any] = {"w": width, "h": height, "tile": tile, "quality": quality}

    full_bytes = encode_image(cur, quality)
    meta["full_bytes"] = len(full_bytes)

    def _full(reason: str) -> Tuple[bytes, Dict[str, Any]]:
        payload = HEADER.pack(MAGIC, MODE_FULL, width, height, tile, 0) + full_bytes
        meta.update(mode="full", bytes=len(payload), reason=reason)
        return payload, meta

    if not prev_raw:
        return _full("no_ref")

    try:
        prev = decode_image(prev_raw)
    except Exception as exc:
        return _full("bad_ref: %s" % exc)

    if prev.size != cur.size:
        return _full("size_changed")

    idx = _changed_tiles(prev, cur, tile, thresh, DEFAULT_MIN_TILE_CHANGE)
    cols = (width + tile - 1) // tile
    meta["changed_tiles"] = len(idx)
    meta["total_tiles"] = cols * ((height + tile - 1) // tile)

    if not idx:
        payload = HEADER.pack(MAGIC, MODE_NONE, width, height, tile, 0)
        meta.update(mode="none", bytes=len(payload), ratio=round(len(payload) / max(1, len(full_bytes)), 3))
        return payload, meta

    from PIL import Image

    layout_cols = max(1, min(16, len(idx)))
    layout_rows = (len(idx) + layout_cols - 1) // layout_cols
    atlas = Image.new("RGB", (layout_cols * tile, layout_rows * tile), (0, 0, 0))
    for i, ti in enumerate(idx):
        ty, tx = divmod(ti, cols)
        x0, y0 = tx * tile, ty * tile
        atlas.paste(cur.crop((x0, y0, min(x0 + tile, width), min(y0 + tile, height))),
                    ((i % layout_cols) * tile, (i // layout_cols) * tile))

    atlas_bytes = encode_image(atlas, quality)
    mask = b"".join(struct.pack(">H", i) for i in idx)
    payload = HEADER.pack(MAGIC, MODE_DELTA, width, height, tile, len(idx)) + mask + atlas_bytes
    meta.update(
        mode="delta",
        bytes=len(payload),
        atlas_bytes=len(atlas_bytes),
        mask_bytes=len(mask),
        ratio=round(len(payload) / max(1, len(full_bytes)), 3),
    )
    if len(payload) > max_ratio * len(full_bytes):
        return _full("delta_too_big")
    return payload, meta


def apply(prev_raw: Optional[bytes], payload: bytes) -> Tuple[bytes, Dict[str, Any]]:
    """把 patch 拼回整帧，返回 (整帧 PNG 字节, meta)。

    中继里不要用它 —— 它会额外做一次 PNG 编码（实测 640x480 要 23ms），
    中继请用 apply_image() 直接拿 PIL 图像，省掉这次编码与解码。"""
    img, meta = apply_image(decode_image(prev_raw) if prev_raw else None, payload)
    return _to_png(img), meta


def apply_image(prev_img: Any, payload: bytes) -> Tuple[Any, Dict[str, Any]]:
    """同 apply，但参考帧和产物都是 PIL 图像（中继用这个，省掉两次编解码）。"""
    if len(payload) < HEADER.size:
        raise DeltaError("payload 太短：%d 字节" % len(payload))
    magic, mode, width, height, tile, count = HEADER.unpack(payload[: HEADER.size])
    if magic != MAGIC:
        raise DeltaError("不是帧差分负载：%r" % magic)
    body = payload[HEADER.size:]

    if mode == MODE_FULL:
        img = decode_image(body)
        return img, {"mode": "full", "w": img.width, "h": img.height}

    if mode == MODE_NONE:
        if prev_img is None:
            raise DeltaError("收到空差分但没有参考帧")
        return prev_img, {"mode": "none", "w": width, "h": height}

    if mode != MODE_DELTA:
        raise DeltaError("未知模式 %d" % mode)
    if prev_img is None:
        raise DeltaError("收到差分但没有参考帧")

    prev = prev_img
    if prev.size != (width, height):
        raise DeltaError("参考帧尺寸 %s 与负载 %dx%d 不符" % (prev.size, width, height))

    mask_len = count * 2
    if len(body) < mask_len:
        raise DeltaError("mask 长度不够：%d < %d" % (len(body), mask_len))
    idx = [struct.unpack(">H", body[i * 2: i * 2 + 2])[0] for i in range(count)]
    atlas = decode_image(body[mask_len:])

    layout_cols = max(1, min(16, count))
    cols = (width + tile - 1) // tile
    out = prev.copy()
    for i, ti in enumerate(idx):
        ty, tx = divmod(ti, cols)
        ax, ay = (i % layout_cols) * tile, (i // layout_cols) * tile
        out.paste(atlas.crop((ax, ay, ax + tile, ay + tile)), (tx * tile, ty * tile))
    return out, {"mode": "delta", "w": width, "h": height, "tiles": count}


def to_b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def from_b64(text: str) -> bytes:
    return base64.b64decode(text)


class DeltaChain:
    """发送侧差分链：每次 feed 一帧，返回要发的 patch，并把参考帧更新成「接收方会还原出的那一帧」。"""

    def __init__(self, *, tile: int = DEFAULT_TILE, quality: int = DEFAULT_QUALITY,
                 max_ratio: float = DEFAULT_MAX_RATIO):
        self.tile = tile
        self.quality = quality
        self.max_ratio = max_ratio
        self.ref_raw: Optional[bytes] = None
        self.frames = 0
        self.full_frames = 0
        self.delta_frames = 0
        self.none_frames = 0
        self.bytes_sent = 0
        self.bytes_full = 0
        self.last_meta: Dict[str, Any] = {}

    def reset(self) -> None:
        self.ref_raw = None

    def feed(self, raw: bytes) -> Tuple[bytes, Dict[str, Any]]:
# 参考帧用「自己这一帧的原图」，不是还原结果：块是绝对像素不是残差，
        # 所以两边基准略有量化差也不会漂移；反过来若用还原结果当基准，
        # 静止画面会被自己的量化噪声判成「有变化」，每次都退回整帧（实测过）。
        payload, meta = encode(self.ref_raw, raw, tile=self.tile, quality=self.quality, max_ratio=self.max_ratio)
        self.ref_raw = raw
        self.frames += 1
        self.bytes_sent += len(payload)
        self.bytes_full += int(meta.get("full_bytes", 0) or 0)
        mode = meta.get("mode")
        if mode == "full":
            self.full_frames += 1
        elif mode == "delta":
            self.delta_frames += 1
        elif mode == "none":
            self.none_frames += 1
        self.last_meta = meta
        return payload, meta

    def stats(self) -> Dict[str, Any]:
        saved = self.bytes_full - self.bytes_sent
        return {
            "frames": self.frames,
            "full": self.full_frames,
            "delta": self.delta_frames,
            "none": self.none_frames,
            "bytes_sent": self.bytes_sent,
            "bytes_full": self.bytes_full,
            "saved_bytes": saved,
            "saved_ratio": round(saved / self.bytes_full, 3) if self.bytes_full else 0.0,
        }
