"""Streaming frame relay (SSE passthrough).

Difference from the original svc/frame_relay.py: the original did

    resp = await _client.post(...)          # reads the WHOLE body
    return Response(content=resp.content)   # ...then sends it at once

which makes every token arrive at the client at the same moment. That is the
root cause of "streaming works on the server, not over the network".

This version keeps the frame-delta logic identical and only changes transport:
when the client asks for stream=true the upstream response is forwarded chunk
by chunk with StreamingResponse, and the upstream request is cancelled when
the client goes away.
"""

import asyncio
import base64
import io
import json
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import frame_delta as fd

UPSTREAM = os.environ.get("FRAME_RELAY_UPSTREAM", "http://127.0.0.1:6006")
MAX_SIDS = int(os.environ.get("FRAME_RELAY_MAX_SIDS", "8"))
MAX_HISTORY = int(os.environ.get("FRAME_RELAY_MAX_HISTORY", "12"))
FORWARD_QUALITY = int(os.environ.get("FRAME_RELAY_QUALITY", "85"))

app = FastAPI()

_imgs: Dict[str, List[Any]] = {}
_urls: Dict[str, List[str]] = {}
_seq: Dict[str, int] = {}
_order: List[str] = []
_stats = {
    "requests": 0, "delta": 0, "full": 0, "none": 0, "errors": 0,
    "bytes_in": 0, "bytes_patch": 0, "refs_served": 0, "refs_missing": 0,
    "ms_resolve": 0.0, "ms_upstream": 0.0, "ms_total": 0.0,
    "ms_first_byte": 0.0, "stream_requests": 0, "chunks_forwarded": 0,
}
_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0), trust_env=False)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "upstream": UPSTREAM, "ts": time.time(), "sids": len(_imgs)}


@app.get("/frame/stats")
async def stats() -> Dict[str, Any]:
    out = dict(_stats)
    n = max(1, int(out["requests"]))
    for key in ("ms_resolve", "ms_upstream", "ms_total", "ms_first_byte"):
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
        _imgs[sid] = []
        _urls[sid] = []
        _seq[sid] = 1
        if sid not in _order:
            _order.append(sid)
    elif _seq.get(sid) != seq:
        raise fd.DeltaError("seq desync (expected %s, got %s)" % (_seq.get(sid), seq))
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
                raise fd.DeltaError("history has %d frames, cannot resolve %s" % (len(snapshot_urls), url))

    payload_b64 = refs.get("cur")
    if payload_b64:
        payload = fd.from_b64(payload_b64)
        _stats["bytes_patch"] += len(payload)
        raw_mode = payload[4] if len(payload) > 4 else -1
        if raw_mode == fd.MODE_NONE:
            if not urls:
                raise fd.DeltaError("empty delta but no reference frame")
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


NO_BUFFER_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


async def _prepare(request: Request) -> tuple[Optional[Dict[str, Any]], Optional[Response]]:
    t_start = time.perf_counter()
    raw = await request.body()
    _stats["requests"] += 1
    _stats["bytes_in"] += len(raw)
    _stats["_t_start"] = t_start  # type: ignore
    try:
        body = json.loads(raw)
    except Exception:
        _stats["errors"] += 1
        return None, JSONResponse({"error": {"message": "body is not JSON"}}, status_code=400)

    deltas = body.pop("frame_delta", None)
    t_resolve = time.perf_counter()
    if deltas:
        try:
            if await _resolve(body, deltas) == 0:
                _stats["errors"] += 1
                return None, JSONResponse({"error": {"message": "frame_delta without delta:// / frame:// placeholders"}}, status_code=400)
        except fd.DeltaError as exc:
            _stats["errors"] += 1
            _stats["ms_resolve"] += (time.perf_counter() - t_resolve) * 1000.0
            return None, JSONResponse({"error": {"message": "need_full: %s" % exc}}, status_code=409)
        except Exception as exc:
            _stats["errors"] += 1
            _stats["ms_resolve"] += (time.perf_counter() - t_resolve) * 1000.0
            return None, JSONResponse({"error": {"message": "need_full: %s" % exc}}, status_code=409)
    _stats["ms_resolve"] += (time.perf_counter() - t_resolve) * 1000.0
    return body, None


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    body, err = await _prepare(request)
    if err is not None:
        return err
    assert body is not None and _client is not None
    t_start = float(_stats.get("_t_start") or time.perf_counter())
    payload = json.dumps(body).encode("utf-8")
    streaming = bool(body.get("stream"))

    if not streaming:
        t_up = time.perf_counter()
        try:
            resp = await _client.post(UPSTREAM + "/v1/chat/completions",
                                      content=payload,
                                      headers={"Content-Type": "application/json"})
        except Exception as exc:
            _stats["errors"] += 1
            return JSONResponse({"error": {"message": "upstream failed: %s" % exc}}, status_code=502)
        _stats["ms_upstream"] += (time.perf_counter() - t_up) * 1000.0
        _stats["ms_total"] += (time.perf_counter() - t_start) * 1000.0
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/json"))

    # --- real streaming passthrough -------------------------------------
    _stats["stream_requests"] += 1
    req = _client.build_request("POST", UPSTREAM + "/v1/chat/completions",
                                content=payload,
                                headers={"Content-Type": "application/json",
                                         "Accept": "text/event-stream"})
    t_up = time.perf_counter()
    try:
        upstream = await _client.send(req, stream=True)
    except Exception as exc:
        _stats["errors"] += 1
        return JSONResponse({"error": {"message": "upstream failed: %s" % exc}}, status_code=502)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    if upstream.headers.get("content-type"):
        headers["Content-Type"] = upstream.headers["content-type"]

    async def _pipe() -> AsyncIterator[bytes]:
        first = True
        try:
            async for chunk in upstream.aiter_raw():
                if not chunk:
                    continue
                if first:
                    _stats["ms_first_byte"] += (time.perf_counter() - t_up) * 1000.0
                    _stats["ms_upstream"] += (time.perf_counter() - t_up) * 1000.0
                    first = False
                _stats["chunks_forwarded"] += 1
                yield chunk
            _stats["ms_total"] += (time.perf_counter() - t_start) * 1000.0
        except (asyncio.CancelledError, GeneratorExit):
            # client went away: drop the upstream connection with it
            raise
        finally:
            await upstream.aclose()

    return StreamingResponse(_pipe(), status_code=upstream.status_code, headers=headers,
                             media_type=None)


@app.get("/v1/models")
async def models() -> Response:
    assert _client is not None
    resp = await _client.get(UPSTREAM + "/v1/models")
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))
