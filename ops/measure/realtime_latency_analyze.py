import json, sys, statistics, collections
def st(a):
    a=[x for x in a if x is not None]
    if not a: return None
    s=sorted(a); n=len(s)
    return {"n":n,"mean":round(statistics.mean(s),2),"median":round(s[n//2],2),"p95":round(s[min(n-1,int(n*.95))],2),"max":round(s[-1],2)}
def load(p): return json.load(open(p))
def summarize(d):
    W=d["warm"]; L=d["log"]; out={}
    pp=[x for x in L["page_polls"] if x["t"]>=W]
    out["page_at_poll_age_s"]=st([x["age_s"] for x in pp]); out["page_at_poll_absbps"]=st([abs(x["diff_bps"]) if x["diff_bps"] is not None else None for x in pp])
    out["page_src_ticker_frac"]=round(sum(1 for x in pp if x["src"]=="ticker")/max(1,len(pp)),3)
    out["page_markets_call_ms"]=st([x["ms"] for x in pp])
    po=[x for x in L["page_onscreen"] if x["t"]>=W]
    out["page_onscreen_age_s"]=st([x["age_s"] for x in po]); out["page_onscreen_absbps"]=st([abs(x["diff_bps"]) if x["diff_bps"] is not None else None for x in po])
    ch={}
    for tf in ("1m","5m","15m","1h","4h","1d"):
        cp=[x for x in L["chart_polls"] if x["tf"]==tf and x["t"]>=W]; co=[x for x in L["chart_onscreen"] if x["tf"]==tf and x["t"]>=W]
        ch[tf]={"polls":len(cp),"at_poll_age_s":st([x["age_s"] for x in cp]),"onscreen_age_s":st([x["age_s"] for x in co]),
                "onscreen_absbps_vs_truth":st([abs(x["diff_bps"]) if x["diff_bps"] is not None else None for x in co]),
                "bar_current_frac":round(sum(1 for x in cp if x["bar_current"])/max(1,len(cp)),3),"chart_call_ms":st([x["ms"] for x in cp])}
    out["chart"]=ch
    al=L["alerts"]; fired=[x for x in al if x["fired"]]; lat=[x["lat_s"] for x in fired if x["lat_s"] is not None]
    out["alerts"]={"created":len(al),"fired":len(fired),"with_cross_time":len(lat),"lat_s":st(lat),"neg_lat":sum(1 for x in lat if x<0),
                   "unfired_but_crossed":sum(1 for x in al if (not x["fired"]) and x["cross_t"] is not None)}
    req=[r for r in d["req"] if r[0]>=W]; span=(d["dur"]-W)/60
    c=collections.Counter()
    for t,h,p,i in req: c[(p if i is None else p+"?"+i)+("" if "fapi" not in h else " [fapi]")]+=1
    out["req_per_min"]={k:round(v/span,2) for k,v in sorted(c.items())}
    wt={"/api/v3/ticker/24hr":2,"/api/v3/klines":2,"/api/v3/depth":5}
    w=sum(wt.get(p,0) for t,h,p,i in req if "fapi" not in h)/span
    out["spot_weight_per_min_est"]=round(w,1); out["hosts_in_use"]=d["hosts_in_use"]; out["errors"]=len(L["errors"]); out["errors_sample"]=L["errors"][:5]
    out["config"]={k:d[k] for k in ("page_poll","chart_period","check_interval","max_price_age_ms","ticker_ttl_ms")}
    return out
if __name__=="__main__":
    for p in sys.argv[1:]:
        print("=====",p); print(json.dumps(summarize(load(p)),indent=1))
