"""帧差分中继（跑在 GPU 机上）：客户端只发「变化了的那部分」，中继拼回整帧再转给本机 vLLM。

为什么需要它：vLLM 的 chat/completions 只吃完整图片，没法「只发变化部分」。
所以中继替我们保存历史帧，把客户端发来的 patch 拼回整帧，再把完整图片塞进请求转发给 127.0.0.1:8000。

协议（客户端在 body 里加一个顶层字段，其余完全照常）：
    {"frame_delta": {"sid": "<会话id>", "seq": <序号>, "refs": {"cur": "<b64 patch>"}}, ...}

messages 里的占位符：
    "delta://cur"   当前帧，按 refs["cur"] 的 patch 应用在上一帧上（seq=0 时是整帧）
    "frame://-1"    上一帧（中继里已经存着，零字节）
    "frame://-2"    上上帧 …… 以此类推

seq 的作用：中继靠它确认「我手里的上一帧就是他这条链的参考帧」。对不上就 409 让客户端发整帧，
绝不拿别的帧顶上——那会让模型看到一张根本不是当时的画面，而且不报错。

性能取舍（2026-11-05 实测，640x480）：
    PNG 编码 23.4ms / JPEG 1.6ms / WEBP 11.7ms → 发给 vLLM 用 JPEG q85；
    帧本身以 PIL 图像常驻内存，拼帧不再做任何编解码，一个 delta 请求的 CPU 从 ~74ms 降到 ~8ms。
"""

import asyncio
import base64
import io
import json
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

import frame_delta as fd

UPSTREAM = os.environ.get("FRAME_RELAY_UPSTREAM", "http://127.0.0.1:8000")
MAX_SIDS = int(os.environ.get("FRAME_RELAY_MAX_SIDS", "8"))
MAX_HISTORY = int(os.environ.get("FRAME_RELAY_MAX_HISTORY", "12"))
FORWARD_QUALITY = int(os.environ.get("FRAME_RELAY_QUALITY", "85"))

app = FastAPI()

_imgs: Dict[str, List[Any]] = {}    # 每个 sid 的帧（PIL 图像，直接能拼）
_urls: Dict[str, List[str]] = {}    # 与 _imgs 平行的 data URL 缓存（历史帧复用，零编码）
_seq: Dict[str, int] = {}           # 期望的下一个序号
_order: List[str] = []
_stats = {
    "requests": 0, "delta": 0, "full": 0, "none": 0, "errors": 0,
    "bytes_in": 0, "bytes_patch": 0, "refs_served": 0, "refs_missing": 0,
    "ms_resolve": 0.0, "ms_upstream": 0.0, "ms_total": 0.0,
}
_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "upstream": UPSTREAM, "ts": time.time(), "sids": len(_imgs)}


@app.get("/frame/stats")
async def stats() -> Dict[str, Any]:
    out = dict(_stats)
    n = max(1, int(out["requests"]))
    for key in ("ms_resolve", "ms_upstream", "ms_total"):
        out[key.replace("ms_", "ms_avg_")] = round(out[key] / n, 1)
    out["sids"] = {k: len(v) for k, v in _imgs.items()}
    out["upstream"] = UPSTREAM
    return out


@app.post("/frame/reset")
async def reset_all() -> Dict[str, Any]:
    _imgs.clear(); _urls.clear(); _seq.clear(); _order.clear()
    return {"ok": True}


def _ring(sid: str) -> List[Any]:
    if sid not in _imgs:
        _imgs[sid] = []
        _urls[sid] = []
        _order.append(sid)
        while len(_order) > MAX_SIDS:
            old = _order.pop(0)
            _imgs.pop(old, None); _urls.pop(old, None); _seq.pop(old, None)
    _urls.setdefault(sid, [])
    return _imgs[sid]


def _img_url(img: Any) -> str:
    """整帧 -> data URL。JPEG q85（官方推荐的帧格式），编码只要 1.6ms。"""
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=FORWARD_QUALITY, subsampling=0)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _store(sid: str, img: Any) -> str:
    ring = _ring(sid)
    urls = _urls[sid]
    url = _img_url(img)
    ring.append(img)
    urls.append(url)
    if len(ring) > MAX_HISTORY:
        del ring[0: len(ring) - MAX_HISTORY]
        del urls[0: len(urls) - MAX_HISTORY]
    return url


async def _resolve(body: Dict[str, Any], deltas: Dict[str, Any]) -> int:
    sid = str(deltas.get("sid") or "")
    refs = deltas.get("refs") or {}
    try:
        seq = int(deltas.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq == 0:
        # seq=0：客户端从这一帧重新开始（整帧 keyframe），中继也清空重来
        _imgs[sid] = []
        _urls[sid] = []
        _seq[sid] = 1
        if sid not in _order:
            _order.append(sid)
    elif _seq.get(sid) != seq:
        raise fd.DeltaError("seq 不同步（期望 %s，收到 %s），参考帧不一致" % (_seq.get(sid), seq))
    ring = _ring(sid)
    urls = _urls[sid]
    snapshot_urls = list(urls)

    resolved: Dict[str, str] = {}
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            url = (part.get("image_url") or {}).get("url") or ""
            if not url.startswith("frame://"):
                continue
            try:
                k = int(url[len("frame://"):])
            except ValueError:
                continue
            idx = len(snapshot_urls) + k
            if 0 <= idx < len(snapshot_urls):
                resolved[url] = snapshot_urls[idx]
                _stats["refs_served"] += 1
            else:
                _stats["refs_missing"] += 1
                raise fd.DeltaError("历史只有 %d 帧，取不到 %s" % (len(snapshot_urls), url))

    payload_b64 = refs.get("cur")
    if payload_b64:
        payload = fd.from_b64(payload_b64)
        _stats["bytes_patch"] += len(payload)
        raw_mode = payload[4] if len(payload) > 4 else -1
        if raw_mode == fd.MODE_NONE:
            if not urls:
                raise fd.DeltaError("收到空差分但没有参考帧")
            ring.append(ring[-1])
            urls.append(urls[-1])
            if len(ring) > MAX_HISTORY:
                del ring[0: len(ring) - MAX_HISTORY]
                del urls[0: len(urls) - MAX_HISTORY]
            resolved["delta://cur"] = urls[-1]
            _stats["none"] += 1
        else:
            prev = ring[-1] if ring else None
            img, meta = await asyncio.to_thread(fd.apply_image, prev, payload)
            resolved["delta://cur"] = _store(sid, img)
            if meta.get("mode") == "delta":
                _stats["delta"] += 1
            else:
                _stats["full"] += 1
        _seq[sid] = seq + 1

    n = 0
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url") or ""
                    if url in resolved:
                        part["image_url"]["url"] = resolved[url]
                        n += 1
    return n


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    t_start = time.perf_counter()
    raw = await request.body()
    _stats["requests"] += 1
    _stats["bytes_in"] += len(raw)
    try:
        body = json.loads(raw)
    except Exception:
        return JSONResponse({"error": {"message": "body 不是 JSON"}}, status_code=400)

    deltas = body.pop("frame_delta", None)
    t_resolve = time.perf_counter()
    if deltas:
        try:
            if await _resolve(body, deltas) == 0:
                _stats["errors"] += 1
                return JSONResponse({"error": {"message": "有 frame_delta 但没有 delta:// / frame:// 占位"}}, status_code=400)
        except fd.DeltaError as exc:
            _stats["errors"] += 1
            _stats["ms_resolve"] += (time.perf_counter() - t_resolve) * 1000.0
            return JSONResponse({"error": {"message": "need_full: %s" % exc}}, status_code=409)
        except Exception as exc:
            _stats["errors"] += 1
            _stats["ms_resolve"] += (time.perf_counter() - t_resolve) * 1000.0
            return JSONResponse({"error": {"message": "need_full: %s" % exc}}, status_code=409)
    _stats["ms_resolve"] += (time.perf_counter() - t_resolve) * 1000.0

    assert _client is not None
    t_up = time.perf_counter()
    try:
        resp = await _client.post(UPSTREAM + "/v1/chat/completions",
                                  content=json.dumps(body).encode("utf-8"),
                                  headers={"Content-Type": "application/json"})
    except Exception as exc:
        _stats["errors"] += 1
        return JSONResponse({"error": {"message": "upstream failed: %s" % exc}}, status_code=502)
    _stats["ms_upstream"] += (time.perf_counter() - t_up) * 1000.0
    _stats["ms_total"] += (time.perf_counter() - t_start) * 1000.0
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))