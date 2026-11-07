"""Stage 6 network probe: identical official body through four links.

Links (all hit the same vLLM /v1/chat/completions):
  direct  = http://127.0.0.1:8000        (vLLM, server-local)
  relay   = http://127.0.0.1:8010        (frame_relay, buffers resp.content)
  fwd     = http://127.0.0.1:8080        (fwd8080 -> 8010 -> 8000, byte transparent)
  public  = http://i-2.gpushare.com:47960 (Windows client -> 8080 -> 8010 -> 8000)

Records per-SSE-chunk arrival times relative to a monotonic T0, then maps
action-protocol milestones onto the first chunk whose cumulative text contains
them. No model / weight / Output Format change; single request concurrency.
"""
import argparse, json, os, re, socket, time
import requests

ACTION_START = "<|action_start|>"
ACTION_END = "<|action_end|>"
THINK_END = "</think>"
HEADER_RE = re.compile(r"-?\d+\s+-?\d+\s+-?\d+\s*;")


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def dns_ms(host, port):
    t = time.perf_counter()
    try:
        socket.getaddrinfo(host, port)
        return (time.perf_counter() - t) * 1000.0
    except Exception:
        return None


def tcp_ms(host, port, n=5):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        s = socket.socket()
        s.settimeout(5)
        try:
            s.connect((host, port))
            out.append((time.perf_counter() - t) * 1000.0)
        except Exception:
            pass
        finally:
            s.close()
    return out


def milestone_times(per_chunk):
    """per_chunk: list of (t_ms, delta_text). Return first-arrival ms per marker."""
    out = {}
    cum = []
    acc = ""
    for t, txt in per_chunk:
        acc += txt
        cum.append((t, acc))
    def first(pred):
        for t, s in cum:
            if pred(s):
                return t
        return None
    out["t_think_end_ms"] = first(lambda s: THINK_END in s)
    out["t_action_start_ms"] = first(lambda s: ACTION_START in s)
    out["t_action_end_ms"] = first(lambda s: ACTION_END in s)
    out["t_header_ms"] = first(lambda s: ACTION_START in s and HEADER_RE.search(s.split(ACTION_START)[-1]))
    for k in range(1, 7):
        out["t_semi%d_ms" % k] = first(
            lambda s, kk=k: ACTION_START in s and s.split(ACTION_START)[-1].count(";") >= kk
        )
    return out


def probe(base, body, n, fp, label):
    parts = base.split("//", 1)[1]
    host = parts.split(":", 1)[0].split("/", 1)[0]
    port = int(parts.split(":", 1)[1].split("/", 1)[0]) if ":" in parts else 80
    dns = dns_ms(host, port)
    conn = tcp_ms(host, port)
    print("[%s] base=%s host=%s port=%s dns_ms=%s tcp_p50_ms=%s"
          % (label, base, host, port, round(dns, 2) if dns else None,
             round(pct(conn, 50), 2) if conn else None))
    body_bytes = len(json.dumps(body).encode("utf-8"))
    sess = requests.Session()
    for i in range(n):
        t0 = time.perf_counter()
        per_chunk = []
        meta = {}
        status = None
        err = None
        try:
            r = sess.post(base + "/v1/chat/completions", json=body,
                          stream=True, timeout=180)
            status = r.status_code
            for raw in r.iter_lines(decode_unicode=True):
                t = (time.perf_counter() - t0) * 1000.0
                if not raw:
                    continue
                if not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                txt = ""
                if data and data != "[DONE]":
                    try:
                        o = json.loads(data)
                        ch = (o.get("choices") or [{}])[0]
                        txt = (ch.get("delta") or {}).get("content") or ""
                        fr = ch.get("finish_reason")
                        if fr:
                            meta["finish_reason"] = fr
                            meta["t_finish_ms"] = t
                        if o.get("usage"):
                            meta["usage"] = o["usage"]
                    except Exception:
                        pass
                else:
                    meta["t_done_ms"] = t
                per_chunk.append((t, txt))
        except Exception as e:
            err = str(e)
        e2e = (time.perf_counter() - t0) * 1000.0
        times = [t for t, _ in per_chunk]
        full = "".join(txt for _, txt in per_chunk)
        ms = milestone_times(per_chunk)
        rec = {
            "label": label, "trial": i, "base": base,
            "body_bytes": body_bytes,
            "dns_ms": dns, "tcp_connect_ms_p50": pct(conn, 50),
            "http_status": status, "error": err,
            "e2e_ms": e2e,
            "n_sse_chunks": len(per_chunk),
            "t_first_chunk_ms": times[0] if times else None,
            "t_last_chunk_ms": times[-1] if times else None,
            "chunk_span_ms": (times[-1] - times[0]) if len(times) > 1 else 0.0,
            "chunk_intervals_ms": [round(times[j + 1] - times[j], 3)
                                   for j in range(len(times) - 1)],
            "finish_reason": meta.get("finish_reason"),
            "t_finish_ms": meta.get("t_finish_ms"),
            "t_done_ms": meta.get("t_done_ms"),
            "action_start_present": ACTION_START in full,
            "action_end_present": ACTION_END in full,
            "n_semicolons": full.split(ACTION_START)[-1].count(";") if ACTION_START in full else 0,
            "output_chars": len(full),
            "raw_text_tail": full[-200:],
        }
        rec.update(ms)
        fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fp.flush()
        print("[%s] %d/%d status=%s e2e=%.1f first=%.1f last=%.1f span=%.1f chunks=%d fin=%s err=%s"
              % (label, i + 1, n, status, e2e,
                 times[0] if times else -1, times[-1] if times else -1,
                 rec["chunk_span_ms"], len(per_chunk),
                 meta.get("finish_reason"), err))
    sess.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    body = json.load(open(a.body, encoding="utf-8"))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        probe(a.url.rstrip("/"), body, a.n, f, a.label)
    print("DONE", a.label, a.out)


if __name__ == "__main__":
    main()
