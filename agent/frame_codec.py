"""帧压缩：把屏幕上那一帧重编码得更小，再上传。

为什么要有这一层：手模型每步要传「当前帧 + 历史帧」，一帧 640x480 JPEG q85(4:4:4)
实测 17–43KB，base64 再 +33%。上行只有 ~1.2MB/s，光图片就吃掉几十毫秒到几百毫秒。

实测（640x480 Undertale 帧，客户端编码 + 服务端 PIL 解码）：
  jpeg q85 4:4:4   23.9 KB   解码 1.6 ms
  jpeg q70 4:2:0   17.5 KB   解码 1.1 ms
  webp q65          9.1 KB   解码 2.8 ms
  webp 480x360 q60  6.7 KB   解码 1.6 ms
  webp 320x240 q55  3.9 KB   解码 0.8 ms
服务端 128 核，解一帧 1–3ms，相对一次 600–1500ms 的推理可以忽略；
所以压缩只赚「上传字节」，不靠服务端省算力。默认 webp q65 + 不缩放
（保留 640x480，小字还能读；要更狠就设 BETTERAGI_FRAME_MAX_SIDE）。
"""

import base64
import io
import os
from typing import Any, Dict, Optional, Tuple

FORMAT_ENV = "BETTERAGI_FRAME_FORMAT"          # webp | jpeg
QUALITY_ENV = "BETTERAGI_FRAME_QUALITY"        # 默认 65（webp）/ 70（jpeg）
MAX_SIDE_ENV = "BETTERAGI_FRAME_MAX_SIDE"      # 默认 0 = 不缩放
DEFAULT_FORMAT = "webp"
DEFAULT_WEBP_QUALITY = 65
DEFAULT_JPEG_QUALITY = 70


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else 0


def target_format(fmt: Optional[str] = None) -> str:
    value = str(fmt or os.environ.get(FORMAT_ENV) or DEFAULT_FORMAT).strip().lower()
    return "jpeg" if value in ("jpeg", "jpg") else "webp"


def target_quality(fmt: str, quality: Optional[int] = None) -> int:
    if quality is not None:
        return max(1, min(100, int(quality)))
    default = DEFAULT_WEBP_QUALITY if fmt == "webp" else DEFAULT_JPEG_QUALITY
    return max(1, min(100, _int_env(QUALITY_ENV, default)))


def target_max_side(max_side: Optional[int] = None) -> int:
    if max_side is not None:
        return max(0, int(max_side))
    return _int_env(MAX_SIDE_ENV, 0)


def _pillow():
    try:
        from PIL import Image  # noqa: F401 - 只为探测依赖
        return Image
    except Exception:  # noqa: BLE001 - 没有 Pillow 就退化成原样上传
        return None


def encode_frame(data: bytes, *, fmt: Optional[str] = None, quality: Optional[int] = None,
                 max_side: Optional[int] = None) -> Tuple[bytes, Dict[str, Any]]:
    """把一帧重编码得更小。失败/没 Pillow 就原样返回（meta["ok"]=False）。"""
    raw = bytes(data or b"")
    meta: Dict[str, Any] = {"ok": False, "original_bytes": len(raw), "bytes": len(raw)}
    if not raw:
        meta["error"] = "empty"
        return raw, meta
    Image = _pillow()
    if Image is None:
        meta["error"] = "no_pillow"
        return raw, meta
    fmt_name = target_format(fmt)
    q = target_quality(fmt_name, quality)
    side = target_max_side(max_side)
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        if image.mode != "RGB":
            image = image.convert("RGB")
        if side and max(image.size) > side:
            ratio = side / float(max(image.size))
            size = (max(1, int(round(image.size[0] * ratio))),
                    max(1, int(round(image.size[1] * ratio))))
            image = image.resize(size, Image.LANCZOS)
        buf = io.BytesIO()
        if fmt_name == "webp":
            image.save(buf, "WEBP", quality=q, method=4)
        else:
            image.save(buf, "JPEG", quality=q, subsampling=2, optimize=True)
        out = buf.getvalue()
    except Exception as error:  # noqa: BLE001 - 压坏了就退回原图，别让链路断在这
        meta["error"] = "%s: %s" % (type(error).__name__, error)
        return raw, meta
    meta.update({"ok": True, "format": fmt_name, "quality": q, "max_side": side,
                 "bytes": len(out), "width": image.size[0], "height": image.size[1],
                 "ratio": round(len(out) / float(len(raw)), 3) if raw else None})
    return out, meta


def to_data_url(data: bytes, *, fmt: Optional[str] = None, quality: Optional[int] = None,
                max_side: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    """重编码成 data URL（给 OpenAI 兼容接口用）。"""
    out, meta = encode_frame(data, fmt=fmt, quality=quality, max_side=max_side)
    fmt_name = meta.get("format") or target_format(fmt)
    wav = "webp" if fmt_name == "webp" else "jpeg"
    return "data:image/%s;base64,%s" % (wav, base64.b64encode(out).decode("ascii")), meta


def _split_data_url(url: str) -> Optional[Tuple[str, bytes]]:
    text = str(url or "")
    head, sep, payload = text.partition(",")
    if not sep or not head.startswith("data:image/") or ";base64" not in head:
        return None
    try:
        return head, base64.b64decode(payload)
    except Exception:  # noqa: BLE001
        return None


def compress_data_url(url: str, *, fmt: Optional[str] = None, quality: Optional[int] = None,
                      max_side: Optional[int] = None, keep_if_smaller: bool = True) -> Tuple[str, Dict[str, Any]]:
    """把已有的 data URL 压小；不是 data URL / 压完反而更大就原样返回。"""
    parsed = _split_data_url(url)
    if parsed is None:
        return str(url or ""), {"ok": False, "error": "not_data_url", "bytes": 0}
    head, raw = parsed
    new_url, meta = to_data_url(raw, fmt=fmt, quality=quality, max_side=max_side)
    if not meta.get("ok"):
        return str(url or ""), meta
    if keep_if_smaller and len(new_url) >= len(str(url or "")):
        meta["kept_original"] = True
        return str(url or ""), meta
    meta["kept_original"] = False
    return new_url, meta