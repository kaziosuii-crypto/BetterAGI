"""Stage 6 aggregation: compare direct / relay8010 / fwd8080 / public47960 links.

Reads network/*.jsonl produced by network_probe.py, writes network/summary.json,
network/summary.md and network/charts/*.png (PIL only).
"""
import json, os, glob, math

HERE = os.path.dirname(os.path.abspath(__file__))

def pct(vals, p):
    vals = [v for v in vals if isinstance(v, (int, float)) and not (isinstance(v,float) and math.isnan(v))]
    if not vals: return None
    s = sorted(vals); k = (len(s)-1)*p/100.0; f = int(k); c = min(f+1, len(s)-1)
    return s[f] + (s[c]-s[f])*(k-f)

def stats(vals):
    vals = [v for v in vals if isinstance(v,(int,float)) and not (isinstance(v,float) and math.isnan(v))]
    if not vals: return None
    m = sum(vals)/len(vals)
    sd = (sum((v-m)**2 for v in vals)/len(vals))**0.5 if len(vals)>1 else 0.0
    return {"n":len(vals),"mean":round(m,2),"std":round(sd,2),"min":round(min(vals),2),
            "max":round(max(vals),2),"p50":round(pct(vals,50),2),"p90":round(pct(vals,90),2),
            "p95":round(pct(vals,95),2),"p99":round(pct(vals,99),2)}

FIELDS = ["e2e_ms","t_first_chunk_ms","t_last_chunk_ms","chunk_span_ms","n_sse_chunks",
          "t_think_end_ms","t_action_start_ms","t_action_end_ms","t_header_ms"]
SEMI = ["t_semi%d_ms" % k for k in range(1,7)]

def load(path):
    return [json.loads(l) for l in open(path,encoding="utf-8") if l.strip()]

def aggregate(rows):
    out = {"n":len(rows),
           "http_status": sorted({r.get("http_status") for r in rows}, key=str),
           "errors": sum(1 for r in rows if r.get("error")),
           "dns_ms_p50": pct([r.get("dns_ms") for r in rows],50),
           "tcp_connect_ms_p50": pct([r.get("tcp_connect_ms_p50") for r in rows],50),
           "body_bytes": rows[0].get("body_bytes") if rows else None}
    for f in FIELDS + SEMI:
        out[f] = stats([r.get(f) for r in rows])
    # chunk interval pool
    iv = []
    for r in rows:
        iv += [x for x in (r.get("chunk_intervals_ms") or []) if isinstance(x,(int,float))]
    out["chunk_intervals_ms"] = stats(iv)
    out["finish_reasons"] = {}
    for r in rows:
        k = str(r.get("finish_reason"))
        out["finish_reasons"][k] = out["finish_reasons"].get(k,0)+1
    out["action_start_present"] = sum(1 for r in rows if r.get("action_start_present"))
    out["action_end_present"] = sum(1 for r in rows if r.get("action_end_present"))
    return out

def main():
    labels = ["direct","relay8010","fwd8080","public47960"]
    agg = {}
    for lab in labels:
        p = os.path.join(HERE, lab+".jsonl")
        if not os.path.exists(p): continue
        rows = load(p)
        agg[lab] = aggregate(rows)
    os.makedirs(os.path.join(HERE,"charts"), exist_ok=True)
    json.dump(agg, open(os.path.join(HERE,"summary.json"),"w",encoding="utf-8"),
              indent=2, ensure_ascii=False)
    # markdown
    L = ["| link | n | err | E2E P50 | first chunk P50 | chunk span P50 | span P95 | chunks P50 | inter-chunk P50 | ActionStart P50 | g1(semi1) P50 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    def g(d,k,f="p50"):
        x=d.get(k); return ("%.1f"%x[f]) if x else "-"
    for lab in labels:
        if lab not in agg: continue
        d=agg[lab]
        L.append("| %s | %d | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            lab,d["n"],d["errors"],g(d,"e2e_ms"),g(d,"t_first_chunk_ms"),
            g(d,"chunk_span_ms"),g(d,"chunk_span_ms","p95"),g(d,"n_sse_chunks"),
            g(d,"chunk_intervals_ms"),g(d,"t_action_start_ms"),g(d,"t_semi1_ms")))
    open(os.path.join(HERE,"summary.md"),"w",encoding="utf-8").write("\n".join(L)+"\n")
    print("\n".join(L))
    # PIL bar chart: chunk span (P50) per link
    try:
        from PIL import Image, ImageDraw, ImageFont
        W,H=900,420; im=Image.new("RGB",(W,H),(250,250,252)); dr=ImageDraw.Draw(im)
        dr.text((20,14),"Stage 6: SSE chunk arrival span per link (P50, ms)",fill=(20,20,30))
        dr.text((20,34),"direct = true incremental; relay/fwd/public = buffered by frame_relay's Response(content=resp.content)",fill=(90,90,105))
        labs=[l for l in labels if l in agg]
        vals=[(agg[l]["chunk_span_ms"] or {}).get("p50",0) for l in labs]
        mx=max(vals+[1]); x0,y0,bw=70,70,110
        for i,(l,v) in enumerate(zip(labs,vals)):
            x=x0+i*(bw+70); h=int((v/mx)*260)
            dr.rectangle([x,y0+260-h,x+bw,y0+260],fill=(60,130,210) if l=="direct" else (215,95,95))
            dr.text((x,y0+266),l,fill=(30,30,40))
            dr.text((x,y0+282),"%.1f ms"%v,fill=(30,30,40))
        dr.line([(x0-10,y0+260),(W-20,y0+260)],fill=(0,0,0),width=1)
        p=os.path.join(HERE,"charts","network_chunk_span.png"); im.save(p)
        print("chart",p)
    except Exception as e:
        print("chart skipped",e)
    print("labels",labels,"present",list(agg.keys()))

if __name__=="__main__": main()
