#!/usr/bin/env python3
"""GameScaling real-time inference benchmark runner.

Runs SERVER-LOCAL, single-request-concurrency, sequential measurements against
the already-deployed vLLM 0.33.0 instance on 127.0.0.1:8000. It does NOT restart
vLLM, does NOT change the checkpoint, does NOT change the Output Format, and does
NOT send any input to a real game.

Two modes:
  sync   - faithful reproduction of gamescaling.policy.GameScalingPolicy.act()
           using requests.Session + non-streaming POST + r.json() + finalize()
           + parse_action(). Timestamps T0/T1/T2/T3/T17.
  stream - identical request body plus stream=True and per-chunk monotonic
           timestamps, giving T7..T16 (per-token / per-action-group timeline).

Every request is bracketed by a scrape of vLLM's real Prometheus endpoint so the
per-request prefill / decode / queue / inference times and prefix-cache hit rates
come from the server, not from guesswork.

Usage:
  python gs_bench.py --spec spec.json --out /hy-tmp/gs/audit/benchmark
  python gs_bench.py --preflight
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageEnhance

sys.path.insert(0, "/root/gs/GameScaling")
import gamescaling.policy as gp
from gamescaling.policy import (
    PolicyConfig, GameScalingPolicy, build_request, finalize,
    user_turn, assistant_turn, Turn,
)
from gamescaling.action import (
    parse_action, ACTION_START, ACTION_END, THINK_OPEN, THINK_CLOSE,
)

BASE = "http://127.0.0.1:8000"
V1 = BASE + "/v1/chat/completions"
METRICS = BASE + "/metrics"

# Verified by enumerating GET /metrics on this exact deployment (vLLM 0.33.0).
# Histogram _bucket / _created lines are deliberately skipped.
METRIC_NAMES = [
    "vllm:time_to_first_token_seconds_sum", "vllm:time_to_first_token_seconds_count",
    "vllm:inter_token_latency_seconds_sum", "vllm:inter_token_latency_seconds_count",
    "vllm:request_time_per_output_token_seconds_sum", "vllm:request_time_per_output_token_seconds_count",
    "vllm:request_prefill_time_seconds_sum", "vllm:request_prefill_time_seconds_count",
    "vllm:request_decode_time_seconds_sum", "vllm:request_decode_time_seconds_count",
    "vllm:request_queue_time_seconds_sum", "vllm:request_queue_time_seconds_count",
    "vllm:request_inference_time_seconds_sum", "vllm:request_inference_time_seconds_count",
    "vllm:e2e_request_latency_seconds_sum", "vllm:e2e_request_latency_seconds_count",
    "vllm:request_prefill_kv_computed_tokens_sum", "vllm:request_prefill_kv_computed_tokens_count",
    "vllm:request_prompt_tokens_sum", "vllm:request_prompt_tokens_count",
    "vllm:request_generation_tokens_sum", "vllm:request_generation_tokens_count",
    "vllm:request_success_total", "vllm:generation_tokens_total",
    "vllm:prompt_tokens_cached_total",
    "vllm:prefix_cache_queries_total", "vllm:prefix_cache_hits_total",
    "vllm:mm_cache_queries_total", "vllm:mm_cache_hits_total",
    "vllm:num_preemptions_total",
    "vllm:request_num_preemptions_sum", "vllm:request_num_preemptions_count",
    "vllm:kv_cache_usage_perc", "vllm:num_requests_running", "vllm:num_requests_waiting",
]
_LINE_RE = re.compile(r"^(vllm:[a-zA-Z0-9_]+)(?:\{[^}]*\})?\s+([-+0-9.eE]+)")


def scrape_metrics() -> dict:
    req = urllib.request.Request(METRICS, headers={"Accept": "text/plain"})
    with urllib.request.urlopen(req, timeout=10) as r:
        txt = r.read().decode("utf-8", "replace")
    out = {}
    for line in txt.splitlines():
        if not line.startswith("vllm:"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1)
        if name.endswith("_bucket") or name.endswith("_created"):
            continue
        try:
            val = float(m.group(2))
        except ValueError:
            continue
        out[name] = val  # single engine / single label set on this deployment
    return out


def metric_delta(before: dict, after: dict) -> dict:
    d = {}
    for k in METRIC_NAMES:
        if k in before and k in after:
            d[k] = round(after[k] - before[k], 9)
    return d


def per_request_avg(d: dict, prefix: str) -> float | None:
    s, c = d.get(prefix + "_sum"), d.get(prefix + "_count")
    if s is None or c is None or c <= 0:
        return None
    return s / c


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------
def make_frames(image_path: str, n_variants: int, mode: str) -> list[Image.Image]:
    base = Image.open(image_path).convert("RGB")
    if mode == "static" or n_variants <= 1:
        return [base]
    # Tiny brightness deltas change the image bytes (=> real MM-cache misses)
    # while keeping resolution, token count and semantic content unchanged.
    out = [base]
    for i in range(1, n_variants):
        out.append(ImageEnhance.Brightness(base).enhance(1.0 + i * 0.0025))
    return out


def timed_encode(image: Image.Image, cfg: PolicyConfig):
    """Byte-identical to gamescaling.policy.encode_image, with sub-timestamps."""
    t1 = time.perf_counter()
    img = image
    if cfg.image_size is not None and img.size != tuple(cfg.image_size):
        from gamescaling.policy import _RESAMPLE
        img = img.resize(tuple(cfg.image_size), _RESAMPLE[cfg.resample])
    buf = io.BytesIO()
    if cfg.image_format == "png":
        img.convert("RGB").save(buf, format="PNG")
        mime = "image/png"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=cfg.jpeg_quality)
        mime = "image/jpeg"
    url = f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    t2 = time.perf_counter()
    return url, t1, t2


# --------------------------------------------------------------------------
# Streaming timeline extraction
# --------------------------------------------------------------------------
SEMICOLON_COUNT_FOR_HEADER = 1  # 1st ';' terminates "X Y Z"
GROUP_SEMICOLONS = 5           # ';' #2..#6 terminate g1..g5


def empty_timeline():
    return {
        "t_first_byte_ms": None, "t_first_content_ms": None,
        "t_think_open_ms": None, "t_think_close_ms": None,
        "t_action_start_ms": None,
        "t_header_numbers_ms": None,
        "t_header_sep_ms": None,
        "t_g1_ms": None, "t_g2_ms": None, "t_g3_ms": None,
        "t_g4_ms": None, "t_g5_ms": None, "t_g6_ms": None,
        "t_action_end_ms": None, "t_finish_ms": None,
        "semicolon_times_ms": [], "chunk_times_ms": [],
        "delta_times_ms": [], "n_chunks": 0, "n_content_chunks": 0,
    }


def note_text(tl, text, t_ms):
    """Update timeline from the accumulated text at time t_ms."""
    if tl["t_think_open_ms"] is None and THINK_OPEN in text:
        tl["t_think_open_ms"] = t_ms
    if tl["t_think_close_ms"] is None and THINK_CLOSE in text:
        tl["t_think_close_ms"] = t_ms
    if tl["t_action_start_ms"] is None and ACTION_START in text:
        tl["t_action_start_ms"] = t_ms
    if tl["t_action_end_ms"] is None and ACTION_END in text:
        tl["t_action_end_ms"] = t_ms
    # Only count semicolons inside the action body (after ACTION_START).
    if ACTION_START in text:
        body = text[text.rfind(ACTION_START) + len(ACTION_START):]
        if ACTION_END in body:
            body = body[:body.find(ACTION_END)]
        n = body.count(";")
        while len(tl["semicolon_times_ms"]) < n:
            idx = len(tl["semicolon_times_ms"])
            tl["semicolon_times_ms"].append(t_ms)
            if idx == 0:
                tl["t_header_sep_ms"] = t_ms
            elif 1 <= idx <= 5:
                tl[f"t_g{idx}_ms"] = t_ms
        if tl["t_header_numbers_ms"] is None:
            head = body.split(";")[0].strip()
            toks = head.split()
            if len(toks) >= 3:
                ok = True
                for v in toks[:3]:
                    try:
                        float(v)
                    except ValueError:
                        ok = False
                if ok:
                    tl["t_header_numbers_ms"] = t_ms


# --------------------------------------------------------------------------
# One trial
# --------------------------------------------------------------------------
def run_trial(cfg: dict, trial: int, phase: str, image: Image.Image,
              policy: GameScalingPolicy, session: requests.Session,
              system_prompt: str, variant_urls: list | None = None) -> dict:
    pcfg: PolicyConfig = policy.cfg
    # The pre-request /metrics scrape is deliberately OUTSIDE the measured
    # decision window (T0..T17), so it cannot inflate client-side latency.
    before = scrape_metrics()
    t0 = time.perf_counter()
    url, t1, t2 = timed_encode(image, pcfg)
    t_encode_ms = (t2 - t1) * 1000.0

    cfg_local = dict(cfg)
    seed_history(policy, url, cfg_local, system_prompt, variant_urls, trial)
    messages = policy.messages(url)
    n_images = sum(1 for m in messages if m.get("role") == "user")
    body = build_request(messages, pcfg)

    # ---- per-request knobs (do not change deployment / checkpoint) ----
    salt_mode = cfg_local.get("cache_salt", "none")
    if salt_mode == "unique":
        body["cache_salt"] = f"gsbench-{cfg_local['name']}-{trial}-{uuid.uuid4().hex[:12]}"
    elif salt_mode == "fixed":
        body["cache_salt"] = f"gsbench-{cfg_local['name']}-fixed"
    if cfg_local.get("stream_interval"):
        body["stream_interval"] = int(cfg_local["stream_interval"])
    if cfg_local.get("structured_outputs"):
        body["structured_outputs"] = cfg_local["structured_outputs"]
    if cfg_local.get("extra_body"):
        body.update(cfg_local["extra_body"])

    is_stream = cfg_local["mode"] == "stream"
    if is_stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}

    tl = empty_timeline()
    raw_parts: list[str] = []
    finish_reason = None
    usage = None
    http_status = None
    error = None
    t3 = t17 = None

    try:
        t3 = time.perf_counter()
        r = session.post(V1, json=body, timeout=pcfg.timeout,
                         headers={"Authorization": f"Bearer {pcfg.api_key}"},
                         stream=is_stream)
        http_status = r.status_code
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
        if not is_stream:
            payload = r.json()
            t17 = time.perf_counter()
            choice = (payload.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            finish_reason = choice.get("finish_reason")
            usage = payload.get("usage") or None
            raw_parts.append(text)
            # Non-streaming: the only client-observable arrival is the WHOLE
            # JSON response. Client TTFT / per-group times are NOT measurable.
            # Use vLLM Prometheus ttft_s / prefill_s / decode_s instead.
            tl["t_first_byte_ms"] = (t17 - t0) * 1000.0
            tl["n_chunks"] = 1
            tl["sync_note"] = (
                "non-streaming: full response arrives at once; client TTFT and"
                " per-group times are not observable; all six groups become"
                " available simultaneously at T17"
            )
            for _gi in range(1, 7):
                tl[f"t_g{_gi}_ms"] = (t17 - t0) * 1000.0
            tl["t_finish_ms"] = (t17 - t0) * 1000.0
        else:
            accumulated = ""
            for line in r.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                t_arrive = time.perf_counter()
                tl["chunk_times_ms"].append((t_arrive - t0) * 1000.0)
                tl["n_chunks"] += 1
                if tl["t_first_byte_ms"] is None:
                    tl["t_first_byte_ms"] = (t_arrive - t0) * 1000.0
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not j.get("choices"):
                    usage = j.get("usage") or usage
                    continue
                ch = j["choices"][0]
                delta = (ch.get("delta") or {}).get("content") or ""
                fr = ch.get("finish_reason")
                if fr:
                    finish_reason = fr
                    if tl["t_finish_ms"] is None:
                        tl["t_finish_ms"] = (t_arrive - t0) * 1000.0
                if usage is None and j.get("usage"):
                    usage = j["usage"]
                if delta:
                    if tl["t_first_content_ms"] is None:
                        tl["t_first_content_ms"] = (t_arrive - t0) * 1000.0
                    tl["n_content_chunks"] += 1
                    tl["delta_times_ms"].append(((t_arrive - t0) * 1000.0, delta))
                    raw_parts.append(delta)
                    accumulated += delta
                    note_text(tl, accumulated, (t_arrive - t0) * 1000.0)
            t17 = time.perf_counter()
        r.close()
    except Exception as exc:  # noqa: BLE001 - record everything, never crash a run
        error = f"{type(exc).__name__}: {exc}"
        if t17 is None:
            t17 = time.perf_counter()
    after = scrape_metrics()

    raw_text = "".join(raw_parts)
    finalized = finalize(raw_text, finish_reason, pcfg) if error is None else ""
    action = parse_action(finalized, lenient=pcfg.lenient_parse)

    # g6 has no trailing ';' in the streamed text: it is confirmed by
    # ACTION_END (when present) or by finish_reason == "stop" (stop stripped).
    t_g6 = tl["t_g6_ms"]
    if t_g6 is None and finish_reason in ("stop", "length"):
        t_g6 = (tl["t_action_end_ms"] or tl["t_finish_ms"]
                or (t17 - t0) * 1000.0)
        tl["t_g6_ms"] = t_g6
        tl["g6_confirmed_by"] = "finish_reason" if tl["t_action_end_ms"] is None else "action_end"
    tl["t17_ms"] = (t17 - t0) * 1000.0

    think_chars = 0
    m = re.search(re.escape(THINK_OPEN) + r"(.*?)" + re.escape(THINK_CLOSE), finalized, re.DOTALL)
    if m:
        think_chars = len(m.group(1).strip())

    mdelta = metric_delta(before, after)
    server = {
        "prefill_s": per_request_avg(mdelta, "vllm:request_prefill_time_seconds"),
        "decode_s": per_request_avg(mdelta, "vllm:request_decode_time_seconds"),
        "queue_s": per_request_avg(mdelta, "vllm:request_queue_time_seconds"),
        "inference_s": per_request_avg(mdelta, "vllm:request_inference_time_seconds"),
        "ttft_s": per_request_avg(mdelta, "vllm:time_to_first_token_seconds"),
        "itl_s": per_request_avg(mdelta, "vllm:inter_token_latency_seconds"),
        "e2e_s": per_request_avg(mdelta, "vllm:e2e_request_latency_seconds"),
        "prompt_tokens": per_request_avg(mdelta, "vllm:request_prompt_tokens"),
        "generation_tokens": per_request_avg(mdelta, "vllm:request_generation_tokens"),
        "prefill_kv_computed_tokens": per_request_avg(mdelta, "vllm:request_prefill_kv_computed_tokens"),
        "prefix_queries": mdelta.get("vllm:prefix_cache_queries_total"),
        "prefix_hits": mdelta.get("vllm:prefix_cache_hits_total"),
        "mm_queries": mdelta.get("vllm:mm_cache_queries_total"),
        "mm_hits": mdelta.get("vllm:mm_cache_hits_total"),
        "prompt_tokens_cached": mdelta.get("vllm:prompt_tokens_cached_total"),
        "request_success": mdelta.get("vllm:request_success_total"),
        "preemptions": mdelta.get("vllm:num_preemptions_total"),
        "kv_cache_usage_perc": after.get("vllm:kv_cache_usage_perc"),
        "num_requests_running": after.get("vllm:num_requests_running"),
    }

    def ms(x):
        return None if x is None else round(x, 3)

    rec = {
        "run_id": cfg_local["run_id"],
        "config": cfg_local["name"],
        "phase": phase,
        "trial": trial,
        "mode": cfg_local["mode"],
        "server": "127.0.0.1:8000 (server-local, loopback)",
        "t0_epoch": t0,
        "config_spec": {k: v for k, v in cfg_local.items() if k not in ("run_id",)},
        "n_images": n_images,
        "image_url_bytes": len(url),
        "T0_ms": 0.0,
        "T1_preprocess_start_ms": ms((t1 - t0) * 1000.0),
        "T2_encode_done_ms": ms((t2 - t0) * 1000.0),
        "encode_ms": ms(t_encode_ms),
        "T3_http_send_ms": ms((t3 - t0) * 1000.0) if t3 else None,
        "T17_request_complete_ms": ms((t17 - t0) * 1000.0),
        "client_wall_ms": ms((t17 - t3) * 1000.0) if t3 else None,
        "ttft_client_ms": ms(tl["t_first_content_ms"]),
        "ttft_source": ("client_first_sse_content" if tl["t_first_content_ms"] is not None
                        else "server_prometheus_vllm:time_to_first_token_seconds"),
        "ttft_from_send_ms": ms((tl["t_first_content_ms"] - (t3 - t0) * 1000.0) if (tl["t_first_content_ms"] is not None and t3) else None),
        "action_ttft_ms": ms(tl["t_g1_ms"]),
        "full_generation_latency_ms": ms(((t17 - t0) * 1000.0 - tl["t_first_content_ms"]) if tl["t_first_content_ms"] is not None else None),
        "e2e_latency_ms": ms((t17 - t0) * 1000.0),
        "generation_tail_ms": ms(((t17 - t0) * 1000.0 - tl["t_g1_ms"]) if tl["t_g1_ms"] is not None else None),
        "T7_first_token_ms": ms(tl["t_first_content_ms"]),
        "T8_think_end_ms": ms(tl["t_think_close_ms"]),
        "T9_action_start_ms": ms(tl["t_action_start_ms"]),
        "T10_mouse_header_numbers_ms": ms(tl["t_header_numbers_ms"]),
        "T10b_header_sep_ms": ms(tl["t_header_sep_ms"]),
        "T11_g1_ms": ms(tl["t_g1_ms"]),
        "T12_g2_ms": ms(tl["t_g2_ms"]),
        "T13_g3_ms": ms(tl["t_g3_ms"]),
        "T14_g4_ms": ms(tl["t_g4_ms"]),
        "T15_g5_ms": ms(tl["t_g5_ms"]),
        "T16_g6_ms": ms(tl["t_g6_ms"]),
        "T17_action_end_ms": ms(tl["t_action_end_ms"] or tl["t_g6_ms"]),
        "t_finish_reason_ms": ms(tl["t_finish_ms"]),
        "g6_confirmed_by": tl.get("g6_confirmed_by") or (
            "action_end" if tl["t_action_end_ms"] else
            ("finish_reason" if finish_reason else None)),
        "sync_note": tl.get("sync_note"),
        "prefill_used": pcfg.prefill,
        "think_open_prefilled": True,
        "think_close_prefilled": (not pcfg.reasoning),
        # T4 (server received), T5 (prefill start), T6 (prefill done) are NOT
        # directly visible over HTTP. Approximate them from the per-request
        # vLLM Prometheus deltas relative to T3.
        "server_approx_T5_prefill_start_ms": ms(((t3 - t0) * 1000.0 + (server["queue_s"] or 0) * 1000.0) if t3 else None),
        "server_approx_T6_prefill_done_ms": ms(((t3 - t0) * 1000.0 + ((server["queue_s"] or 0) + (server["prefill_s"] or 0)) * 1000.0) if t3 else None),
        "server_approx_T7_first_token_ms": ms(((t3 - t0) * 1000.0 + (server["ttft_s"] or 0) * 1000.0) if t3 else None),
        "server_approx_note": "T4/T5/T6 not client-observable; derived from vLLM queue/prefill/ttft Prometheus deltas relative to T3",
        "n_deltas_before_action_start": sum(1 for t, d in tl["delta_times_ms"] if t <= (tl["t_action_start_ms"] or float("inf"))),
        "n_deltas_after_action_start": sum(1 for t, d in tl["delta_times_ms"] if t > (tl["t_action_start_ms"] or float("inf"))),
        "semicolon_times_ms": [round(v, 3) for v in tl["semicolon_times_ms"]],
        "n_sse_chunks": tl["n_chunks"],
        "n_content_chunks": tl["n_content_chunks"],
        "content_chunk_intervals_ms": [round(b[0] - a[0], 3) for a, b in zip(tl["delta_times_ms"], tl["delta_times_ms"][1:])],
        "delta_stream": [{"t_ms": round(t, 3), "d": d} for t, d in tl["delta_times_ms"]],
        "think_chars": think_chars,
        "finish_reason": finish_reason,
        "http_status": http_status,
        "error": error,
        "usage": usage,
        "raw_text": raw_text,
        "finalized_text": finalized,
        "parsed": action.parsed,
        "truncated": action.truncated,
        "dropped": action.dropped,
        "mouse": list(action.mouse),
        "steps": action.steps,
        "is_noop": action.is_noop,
        "action_body": action.body,
        "server_metrics": server,
        "metric_delta": mdelta,
    }
    return rec


def seed_history(policy: GameScalingPolicy, url: str, cfg: dict, system_prompt: str,
                 variant_urls: list | None = None, trial: int = 0):
    """Deterministic steady-state history: H past turns, all real 6-group actions.

    variant_urls (runner v2): when provided, each history turn gets a DIFFERENT
    pre-encoded frame URL, modelling the real deployment where every past
    screenshot differs. Default None keeps the batch1 behaviour (all H+1
    images byte-identical) so results stay comparable.
    """
    hist_action = cfg.get(
        "history_action",
        "<|action_start|>-37 -3 0 ; LB S ; LB ; LB ; LB ; LB ; LB<|action_end|>",
    )
    h = int(cfg.get("history_len", 5))
    policy._history.clear()
    for j in range(h):
        u = url
        if variant_urls and len(variant_urls) > 1:
            u = variant_urls[(trial + j * 3 + 1) % len(variant_urls)]
        policy._history.append(Turn(u, "", hist_action))


def make_policy(cfg: dict, system_prompt: str) -> GameScalingPolicy:
    pcfg = PolicyConfig(
        base_url="http://127.0.0.1:8000/v1",
        model="gamescaling", api_key="EMPTY",
        history_len=int(cfg.get("history_len", 5)),
        history_think=0,
        image_size=tuple(cfg["image_size"]) if cfg.get("image_size") else None,
        image_format=cfg.get("image_format", "jpeg"),
        jpeg_quality=int(cfg.get("jpeg_quality", 90)),
        resample=cfg.get("resample", "bilinear"),
        reasoning=bool(cfg.get("reasoning", True)),
        temperature=float(cfg.get("temperature", 1.0)),
        max_tokens=int(cfg.get("max_tokens", 2048)),
        stop=["<|action_end|>"],
        lenient_parse=False,
        timeout=float(cfg.get("timeout", 120.0)),
    )
    return GameScalingPolicy(system_prompt, pcfg)


def run_config(cfg: dict, out_dir: Path, system_prompt: str, image_path: str):
    name = cfg["name"]
    warmup = int(cfg.get("warmup", 10))
    trials = int(cfg.get("trials", 100))
    frames_mode = cfg.get("frames", "static")
    n_variants = int(cfg.get("frame_variants", 20))
    images = make_frames(image_path, n_variants, frames_mode)
    policy = make_policy(cfg, system_prompt)
    # runner v2: pre-encode distinct history frame URLs OUTSIDE the measured
    # decision window (encoding here would inflate T3_http_send_ms).
    variant_urls = None
    if cfg.get("history_variants"):
        variant_urls = [timed_encode(im, policy.cfg)[0] for im in images]
    session = requests.Session()  # persistent Keep-Alive, exactly like official client
    path = out_dir / (name + ".jsonl")
    n_ok = 0
    with path.open("w", encoding="utf-8") as f:
        for i in range(warmup + trials):
            phase = "warmup" if i < warmup else "measure"
            img = images[i % len(images)]
            rec = run_trial(cfg, i, phase, img, policy, session, system_prompt, variant_urls)
            if phase == "measure" and rec["error"] is None:
                n_ok += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if phase == "warmup" and (i + 1) % 5 == 0:
                print(f"[{name}] warmup {i+1}/{warmup}", flush=True)
            if phase == "measure" and (i - warmup + 1) % 25 == 0:
                print(f"[{name}] measure {i-warmup+1}/{trials}", flush=True)
    print(f"[{name}] DONE ok={n_ok}/{trials} -> {path}", flush=True)
    return path


DEFAULT_SPEC = {
    "image": "/root/gs/GameScaling/examples/doom_battle1.jpg",
    "system_prompt_file": "/root/gs/GameScaling/prompts/example_doom_battle1.txt",
    "out": "/hy-tmp/gs/audit/benchmark",
    "configs": [],
}


def preflight(system_prompt: str, image_path: str):
    variants = [
        {"name": "pf_sync", "mode": "sync", "trials": 1, "warmup": 0},
        {"name": "pf_stream", "mode": "stream", "trials": 1, "warmup": 0},
        {"name": "pf_salt_unique", "mode": "stream", "cache_salt": "unique", "trials": 1, "warmup": 0},
        {"name": "pf_salt_fixed", "mode": "stream", "cache_salt": "fixed", "trials": 1, "warmup": 0},
        {"name": "pf_si1", "mode": "stream", "stream_interval": 1, "trials": 1, "warmup": 0},
        {"name": "pf_si2", "mode": "stream", "stream_interval": 2, "trials": 1, "warmup": 0},
        {"name": "pf_guided", "mode": "stream", "trials": 1, "warmup": 0,
         "structured_outputs": {"regex": r"-?\d+ -?\d+ -?\d+( ; [ A-Za-z0-9]*){6}"}},
    ]
    out = Path("/hy-tmp/gs/audit/preflight")
    out.mkdir(parents=True, exist_ok=True)
    for v in variants:
        v["run_id"] = "preflight"
        try:
            p = run_config(v, out, system_prompt, image_path)
            rec = json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
            print("PREFLIGHT", v["name"], "status", rec["http_status"],
                  "err", rec["error"], "parsed", rec["parsed"],
                  "wall", rec["client_wall_ms"], "ttft", rec["ttft_client_ms"],
                  "prefix_hits", rec["server_metrics"]["prefix_hits"],
                  flush=True)
        except Exception as e:
            print("PREFLIGHT", v["name"], "EXC", type(e).__name__, str(e)[:300], flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec")
    ap.add_argument("--out")
    ap.add_argument("--preflight", action="store_true")
    args = ap.parse_args()
    DEFAULT_SPEC["system_prompt_file"] = "/root/gs/GameScaling/prompts/example_doom_battle1.txt"
    system_prompt = Path(DEFAULT_SPEC["system_prompt_file"]).read_text(encoding="utf-8")
    image_path = DEFAULT_SPEC["image"]
    if args.preflight:
        preflight(system_prompt, image_path)
        return
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else DEFAULT_SPEC
    out_dir = Path(args.out or spec.get("out") or DEFAULT_SPEC["out"])
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    print("RUN_ID", run_id, "configs", len(spec["configs"]), flush=True)
    for cfg in spec["configs"]:
        cfg["run_id"] = run_id
        cfg.setdefault("system_prompt_file", spec.get("system_prompt_file"))
        cfg.setdefault("image", spec.get("image"))
        cfg.setdefault("trials", 100)
        cfg.setdefault("warmup", 10)
        run_config(cfg, out_dir, system_prompt, image_path)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
