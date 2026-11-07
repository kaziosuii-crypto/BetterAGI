#!/usr/bin/env python3
"""Aggregate GameScaling benchmark JSONL -> per-config statistics + charts.

Pure-stdlib + numpy + PIL only (no matplotlib/pandas needed on the server).
Run locally:  python analyze.py --indir <dir with *.jsonl> --out <dir>
"""
import argparse, json, math, os, statistics, sys
from pathlib import Path

# ---------- statistics ----------
def stats(vals):
    vals = [float(v) for v in vals if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    def pct(p):
        if n == 1: return s[0]
        k = (n - 1) * p
        f = math.floor(k); c = math.ceil(k)
        return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)
    return {
        "n": n, "mean": round(statistics.fmean(s), 3),
        "std": round(statistics.pstdev(s), 3) if n > 1 else 0.0,
        "min": round(s[0], 3), "max": round(s[-1], 3),
        "p50": round(pct(0.50), 3), "p90": round(pct(0.90), 3),
        "p95": round(pct(0.95), 3), "p99": round(pct(0.99), 3),
    }

# client-side primary latency metrics (ms)
CLIENT_METRICS = [
    "e2e_latency_ms", "client_wall_ms", "ttft_client_ms", "action_ttft_ms",
    "full_generation_latency_ms", "generation_tail_ms", "encode_ms",
    "T3_http_send_ms", "T7_first_token_ms", "T8_think_end_ms",
    "T9_action_start_ms", "T10_mouse_header_numbers_ms", "T10b_header_sep_ms",
    "T11_g1_ms", "T12_g2_ms", "T13_g3_ms", "T14_g4_ms", "T15_g5_ms",
    "T16_g6_ms", "T17_request_complete_ms", "t_finish_reason_ms",
    "server_approx_T5_prefill_start_ms", "server_approx_T6_prefill_done_ms",
    "server_approx_T7_first_token_ms",
]
# server Prometheus delta metrics (seconds -> ms) and token counters
SERVER_MS = ["prefill_s", "decode_s", "queue_s", "inference_s", "ttft_s", "itl_s", "e2e_s"]
SERVER_TOKENS = ["prompt_tokens", "generation_tokens", "prefill_kv_computed_tokens",
                 "prefix_queries", "prefix_hits", "mm_queries", "mm_hits",
                 "prompt_tokens_cached", "request_success", "preemptions",
                 "kv_cache_usage_perc", "num_requests_running"]
# derived per-trial quantities
DERIVED = {
    "prefill_ms": lambda r: (r["server_metrics"].get("prefill_s") or 0) * 1000,
    "decode_ms": lambda r: (r["server_metrics"].get("decode_s") or 0) * 1000,
    "queue_ms": lambda r: (r["server_metrics"].get("queue_s") or 0) * 1000,
    "itl_ms": lambda r: (r["server_metrics"].get("itl_s") or 0) * 1000,
    "server_ttft_ms": lambda r: (r["server_metrics"].get("ttft_s") or 0) * 1000,
    "prompt_tokens": lambda r: r["server_metrics"].get("prompt_tokens"),
    "generation_tokens": lambda r: r["server_metrics"].get("generation_tokens"),
    "prefix_hits": lambda r: r["server_metrics"].get("prefix_hits"),
    "mm_hits": lambda r: r["server_metrics"].get("mm_hits"),
    "prefill_kv_computed_tokens": lambda r: r["server_metrics"].get("prefill_kv_computed_tokens"),
    "output_tps": lambda r: ((r["usage"] or {}).get("completion_tokens") or 0) / ((r["e2e_latency_ms"] or 0)/1000) if r.get("e2e_latency_ms") else None,
    "think_chars": lambda r: r.get("think_chars"),
    "n_deltas_before_action_start": lambda r: r.get("n_deltas_before_action_start"),
    "n_content_chunks": lambda r: r.get("n_content_chunks"),
    "image_url_bytes": lambda r: r.get("image_url_bytes"),
    "n_images": lambda r: r.get("n_images"),
}

def group_intervals(r):
    """T_g(i+1)-T_gi for i=1..5, plus T_header_sep->T_g1, T_g6->T17."""
    keys = ["T11_g1_ms", "T12_g2_ms", "T13_g3_ms", "T14_g4_ms", "T15_g5_ms", "T16_g6_ms"]
    v = [r.get(k) for k in keys]
    out = {}
    for i in range(5):
        out[f"g{i+1}_to_g{i+2}_ms"] = (v[i+1] - v[i]) if (v[i] is not None and v[i+1] is not None) else None
    if r.get("T9_action_start_ms") is not None and v[0] is not None:
        out["action_start_to_g1_ms"] = v[0] - r["T9_action_start_ms"]
    if r.get("T10_mouse_header_numbers_ms") is not None and r.get("T9_action_start_ms") is not None:
        out["action_start_to_header_ms"] = r["T10_mouse_header_numbers_ms"] - r["T9_action_start_ms"]
    if r.get("T10b_header_sep_ms") is not None and v[0] is not None:
        out["header_to_g1_ms"] = v[0] - r["T10b_header_sep_ms"]
    if v[5] is not None and r.get("T17_request_complete_ms") is not None:
        out["g6_to_complete_ms"] = r["T17_request_complete_ms"] - v[5]
    return out

GROUP_INTERVAL_KEYS = ["action_start_to_header_ms", "header_to_g1_ms",
    "g1_to_g2_ms", "g2_to_g3_ms", "g3_to_g4_ms", "g4_to_g5_ms",
    "g5_to_g6_ms", "g6_to_complete_ms"]

def load_configs(indir):
    by = {}
    for p in sorted(Path(indir).glob("*.jsonl")):
        recs = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line: continue
            try: r = json.loads(line)
            except Exception: continue
            if r.get("phase") != "measure": continue
            recs.append(r)
        if recs: by[p.stem] = recs
    return by

def analyze_config(recs):
    out = {"n_trials": len(recs),
           "n_errors": sum(1 for r in recs if r.get("error")),
           "n_parse_fail": sum(1 for r in recs if not r.get("parsed")),
           "n_truncated": sum(1 for r in recs if r.get("truncated")),
           "n_dropped": sum(1 for r in recs if r.get("dropped")),
           "n_noop": sum(1 for r in recs if r.get("is_noop")),
           "finish_reasons": {}, "g6_confirmed_by": {},
           "mode": recs[0].get("mode") if recs else None,
           "config_spec": recs[0].get("config_spec") if recs else None}
    for r in recs:
        fr = r.get("finish_reason") or "None"
        out["finish_reasons"][fr] = out["finish_reasons"].get(fr, 0) + 1
        g = r.get("g6_confirmed_by") or "None"
        out["g6_confirmed_by"][g] = out["g6_confirmed_by"].get(g, 0) + 1
    out["client"] = {k: stats([r.get(k) for r in recs]) for k in CLIENT_METRICS}
    out["server"] = {k: stats([(r["server_metrics"].get(k) or 0)*1000 for r in recs]) for k in SERVER_MS}
    out["server_tokens"] = {k: stats([r["server_metrics"].get(k) for r in recs]) for k in SERVER_TOKENS}
    out["derived"] = {k: stats([fn(r) for r in recs]) for k, fn in DERIVED.items()}
    gis = [group_intervals(r) for r in recs]
    out["group_intervals"] = {k: stats([g.get(k) for g in gis]) for k in GROUP_INTERVAL_KEYS}
    # stage-2 q3/q4: P95 of each subsequent group interval < 33.33ms? and which is slowest
    q3 = {}; q4 = {}
    for k in ["g1_to_g2_ms","g2_to_g3_ms","g3_to_g4_ms","g4_to_g5_ms","g5_to_g6_ms"]:
        s = out["group_intervals"][k]
        q3[k] = None if s is None else {"p95": s["p95"], "under_33ms": s["p95"] < 33.33, "p50": s["p50"]}
    out["group_interval_p95_under_33ms"] = q3
    # token count from usage
    out["usage"] = {
        "prompt_tokens": stats([(r.get("usage") or {}).get("prompt_tokens") for r in recs]),
        "completion_tokens": stats([(r.get("usage") or {}).get("completion_tokens") for r in recs]),
        "total_tokens": stats([(r.get("usage") or {}).get("total_tokens") for r in recs]),
    }
    # SSE evidence: content chunk intervals (is streaming really incremental?)
    cci = []
    for r in recs:
        cci.extend(r.get("content_chunk_intervals_ms") or [])
    out["content_chunk_intervals_ms"] = stats(cci)
    return out

# ---------- PIL charting ----------
try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

W, H = 1100, 620
BG = (17, 21, 28); FG = (220, 226, 235); GRID = (55, 63, 76)
COLORS = [(94,156,255),(255,159,67),(75,207,160),(214,112,218),(240,200,80),(110,220,235),(232,105,120),(150,170,255),(180,200,120)]

def font(sz, bold=False):
    if not HAVE_PIL: return None
    for c in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\segoeui.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try: return ImageFont.truetype(c, sz, index=(0 if bold else 0))
        except Exception: pass
    return ImageFont.load_default()

def new_canvas(title, subtitle=""):
    im = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(im)
    d.text((24, 16), title, fill=FG, font=font(22, True))
    if subtitle: d.text((24, 46), subtitle, fill=(150,162,178), font=font(13))
    d.line([(24, 68), (W-24, 68)], fill=GRID, width=1)
    return im, d

def bar_chart(path, title, subtitle, labels, values, errs=None, ylabel="ms", colors=None, hline=None):
    im, d = new_canvas(title, subtitle)
    n = len(labels); left, right, top, bot = 90, W-30, 90, H-70
    vmax = max([v for v in values if v is not None] + [1]) * 1.15
    if hline is not None: vmax = max(vmax, hline*1.15)
    d.text((12, top-22), ylabel, fill=(160,172,190), font=font(12))
    # y grid
    for i in range(6):
        y = bot - (bot-top)*i/5
        d.line([(left, y), (right, y)], fill=GRID, width=1)
        d.text((30, y-8), f"{vmax*i/5:.0f}", fill=(140,152,170), font=font(11))
    bw = (right-left)/max(n,1)*0.62
    for i, (lab, v) in enumerate(zip(labels, values)):
        cx = left + (right-left)*(i+0.5)/max(n,1)
        col = (colors[i] if colors else COLORS[i % len(COLORS)])
        if v is not None:
            y0 = bot - (bot-top)*(v/vmax)
            d.rectangle([cx-bw/2, y0, cx+bw/2, bot], fill=col)
            d.text((cx-14, y0-20), f"{v:.1f}", fill=FG, font=font(11, True))
            if errs and errs[i] is not None:
                e = errs[i]
                ye = bot - (bot-top)*((v+e)/vmax)
                d.line([(cx, ye), (cx, y0)], fill=(255,90,90), width=2)
                d.line([(cx-5, ye), (cx+5, ye)], fill=(255,90,90), width=2)
        d.text((cx-40, bot+8), lab, fill=(180,190,205), font=font(11))
        d.text((cx-40, bot+24), lab, fill=(180,190,205), font=font(11)) if False else None
    if hline is not None:
        yh = bot - (bot-top)*(hline/vmax)
        d.line([(left, yh), (right, yh)], fill=(255,90,90), width=2)
        d.text((right-150, yh-16), f"SLO {hline:.0f}ms", fill=(255,120,120), font=font(11, True))
    im.save(path)
    return path

def line_chart(path, title, subtitle, series, xlabel="", ylabel="ms", hline=None):
    """series: list of (name, [(x,y),...]) """
    im, d = new_canvas(title, subtitle)
    left, right, top, bot = 90, W-30, 90, H-70
    allv = [y for _, pts in series for _, y in pts if y is not None]
    if not allv: allv = [1]
    vmin = 0; vmax = max(allv)*1.15
    if hline is not None: vmax = max(vmax, hline*1.15)
    maxx = max([x for _, pts in series for x, _ in pts] + [1])
    for i in range(6):
        y = bot - (bot-top)*i/5
        d.line([(left,y),(right,y)], fill=GRID, width=1)
        d.text((28,y-8), f"{vmax*i/5:.0f}", fill=(140,152,170), font=font(11))
    for si, (name, pts) in enumerate(series):
        col = COLORS[si % len(COLORS)]
        xy = [(left+(right-left)*(x/maxx), bot-(bot-top)*((y or 0)/vmax)) for x,y in pts]
        if len(xy) > 1: d.line(xy, fill=col, width=2)
        for (x,y) in xy: d.ellipse([x-3,y-3,x+3,y+3], fill=col)
        d.text((W-210, 78+si*20), name, fill=col, font=font(12, True))
    if hline is not None:
        yh = bot-(bot-top)*(hline/vmax)
        d.line([(left,yh),(right,yh)], fill=(255,90,90), width=2)
        d.text((right-150,yh-16), f"{hline:.0f}ms", fill=(255,120,120), font=font(11,True))
    d.text((W//2-40,H-28), xlabel, fill=(150,162,178), font=font(12))
    im.save(path)
    return path

def distribution_chart(path, title, subtitle, cfgs, key, src="client"):
    """Box-ish P50/P90/P95/P99 bars per config."""
    labels = list(cfgs.keys()); 
    p50 = [cfgs[c][src].get(key,{}).get("p50") if cfgs[c].get(src,{}).get(key) else None for c in labels]
    p90 = [cfgs[c][src].get(key,{}).get("p90") if cfgs[c].get(src,{}).get(key) else None for c in labels]
    p95 = [cfgs[c][src].get(key,{}).get("p95") if cfgs[c].get(src,{}).get(key) else None for c in labels]
    p99 = [cfgs[c][src].get(key,{}).get("p99") if cfgs[c].get(src,{}).get(key) else None for c in labels]
    return _multi_bar(path, title, subtitle, labels,
                       [("P50",p50),("P90",p90),("P95",p95),("P99",p99)])

def _multi_bar(path, title, subtitle, labels, groups):
    im, d = new_canvas(title, subtitle)
    left, right, top, bot = 90, W-30, 90, H-90
    flat = [v for _,vals in groups for v in vals if v is not None]
    vmax = (max(flat) if flat else 1)*1.15
    for i in range(6):
        y = bot-(bot-top)*i/5
        d.line([(left,y),(right,y)], fill=GRID, width=1)
        d.text((28,y-8), f"{vmax*i/5:.0f}", fill=(140,152,170), font=font(11))
    n = len(labels); g = len(groups)
    slot = (right-left)/max(n,1); bw = slot/(g+1)*0.8
    for i, lab in enumerate(labels):
        cx0 = left+slot*i
        for j,(gname,vals) in enumerate(groups):
            v = vals[i]; col = COLORS[j % len(COLORS)]
            x = cx0 + slot/(g+1)*(j+1)
            if v is not None:
                y0 = bot-(bot-top)*(v/vmax)
                d.rectangle([x-bw/2,y0,x+bw/2,bot], fill=col)
        d.text((cx0+2,bot+8), lab[:14], fill=(180,190,205), font=font(10))
    for j,(gname,_) in enumerate(groups):
        d.rectangle([W-150,78+j*20,W-138,90+j*20], fill=COLORS[j%len(COLORS)])
        d.text((W-132,76+j*20), gname, fill=FG, font=font(11))
    im.save(path)
    return path

def build_charts(cfgs, outdir):
    if not HAVE_PIL: 
        print("PIL unavailable, skipping charts"); return []
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    made = []
    def save(name, fn, *a, **k):
        try: made.append(fn(str(outdir/name), *a, **k))
        except Exception as e: print("chart", name, "failed:", e)
    def cs(c, key, field="p50", src="client"):
        d = cfgs[c].get(src,{}).get(key)
        return d.get(field) if d else None
    labels = list(cfgs.keys())
    # 1. TTFT distribution (client, stream configs)
    sc = [c for c in labels if cfgs[c].get("mode")=="stream"]
    sy = [c for c in labels if cfgs[c].get("mode")=="sync"]
    if sc: save("ttft_distribution.png", distribution_chart,
        "TTFT 分布 (client first SSE content)", "每配置 P50/P90/P95/P99, ms | P50 越低越好",
        {c:cfgs[c] for c in sc}, "ttft_client_ms")
    # 2. Action TTFT distribution
    if sc: save("action_ttft_distribution.png", distribution_chart,
        "Action TTFT 分布 (T_g1 - T0)", "第一组键盘动作可用时间, ms | 目标 < 200ms 窗口",
        {c:cfgs[c] for c in sc}, "action_ttft_ms")
    # 3. six-group generation timeline (baseline_stream g1..g6)
    for base in (["baseline_stream"] if "baseline_stream" in cfgs else sc[:1]):
        pts = [(i+1, cs(base, f"T{11+i}_g{i+1}_ms")) for i in range(6)]
        save("six_group_timeline.png", line_chart,
             "六组动作生成时间线 (P50)", f"{base}: T_g1..T_g6 相对 T0, ms | 33ms/组目标",
             [(base, pts)], xlabel="动作组 g1..g6", ylabel="ms", hline=200.0)
    # 4. Prefill/Decode 时间占比 (stacked bar via simple bars)
    if sc:
        pre = [cs(c,"prefill_ms",src="derived") or 0 for c in sc]
        dec = [cs(c,"decode_ms",src="derived") or 0 for c in sc]
        save("prefill_decode_ratio.png", _multi_bar,
             "Prefill / Decode 时间占比", "每配置 P50, ms (server Prometheus delta)",
             sc, [("Prefill",pre),("Decode",dec)])
    # 5. P50/P95 对比 (E2E)
    save("p50_p95_comparison.png", _multi_bar,
         "各方案 P50 / P95 对比 (E2E latency)", "client e2e_latency_ms, ms | 越低越好",
         labels, [("P50",[cs(c,"e2e_latency_ms","p50") for c in labels]),
                  ("P95",[cs(c,"e2e_latency_ms","p95") for c in labels])])
    # 6. Group intervals per stream config (are subsequent groups < 33ms?)
    gkeys = ["g1_to_g2_ms","g2_to_g3_ms","g3_to_g4_ms","g4_to_g5_ms","g5_to_g6_ms"]
    for c in sc[:4]:
        vals = [cfgs[c]["group_intervals"][k]["p95"] if cfgs[c]["group_intervals"].get(k) else None for k in gkeys]
        save(f"group_intervals_{c}.png", bar_chart,
             f"后续组间隔 P95 ({c})", "组间隔 P95, ms | 红线=33.33ms 理论步长",
             ["g1→2","g2→3","g3→4","g4→5","g5→6"], vals, ylabel="ms", hline=33.33)
    # 7. sync vs stream action TTFT notion
    if sc and sy:
        save("sync_vs_stream.png", _multi_bar,
             "同步 vs 流式: 首组可用时间", "Action TTFT (stream) vs 整响应 (sync), ms | 同步六组同时可用",
             labels, [("ActionTTFT",[cs(c,"action_ttft_ms") for c in labels]),
                      ("E2E",[cs(c,"e2e_latency_ms") for c in labels])])
    return made

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-charts", action="store_true")
    a = ap.parse_args()
    cfgs = load_configs(a.indir)
    if not cfgs:
        print("no measure records found in", a.indir); sys.exit(2)
    out = {c: analyze_config(r) for c, r in cfgs.items()}
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    (outdir/"summary.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    # compact markdown table
    lines = ["| config | mode | n | err | parse_fail | E2E P50 | E2E P95 | TTFT P50 | ActionTTFT P50 | Prefill P50 | Decode P50 | g1→2 P95 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c, o in out.items():
        cl = o["client"]; de = o["derived"]; gi = o["group_intervals"]
        def g(d,k,f="p50"):
            x = d.get(k); return f"{x[f]:.1f}" if x else "-"
        lines.append(f"| {c} | {o['mode']} | {o['n_trials']} | {o['n_errors']} | {o['n_parse_fail']} | "
                     f"{g(cl,'e2e_latency_ms')} | {g(cl,'e2e_latency_ms','p95')} | {g(cl,'ttft_client_ms')} | "
                     f"{g(cl,'action_ttft_ms')} | {g(de,'prefill_ms')} | {g(de,'decode_ms')} | {g(gi,'g1_to_g2_ms','p95')} |")
    (outdir/"summary.md").write_text(chr(10).join(lines)+chr(10), encoding="utf-8")
    charts = [] if a.no_charts else build_charts(out, outdir/"charts")
    print(f"configs={len(out)} trials={sum(o['n_trials'] for o in out.values())} charts={len(charts)}")
    print(chr(10).join(lines))
    for c in charts: print("CHART", c)

if __name__ == "__main__":
    main()
