#!/usr/bin/env python3
"""Real-browser side of the 空投雷达 latency measurement (pairs with airdrop_latency_harness.py).
Opens /airdrops in headless Chrome (real 60 s auto refresh), and records for every published mock item
  t_api: first time GET /api/v1/airdrops (polled every 2 s from the same page session) lists it
  t_dom: first time its title appears in the page DOM (MutationObserver, no reload)
usage: airdrop_latency_browser.py BASE_URL PUBLISHED.jsonl DURATION_S OUT.json [SHOT_DIR]
(needs playwright + google-chrome; run with the system python3)."""
import json, sys, time, re
from playwright.sync_api import sync_playwright

base, pub_path, dur, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
shot_dir = sys.argv[5] if len(sys.argv) > 5 else None
OBS = """() => {
  window.__seen = {};
  const re = /([A-Z][a-z]+\\d+): airdrop eligibility snapshot announced/g;
  const scan = () => { const txt = document.body ? document.body.innerText : ''; let m;
    while ((m = re.exec(txt))) { if (!(m[1] in window.__seen)) window.__seen[m[1]] = performance.now(); } };
  new MutationObserver(scan).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
  scan();
}"""
with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    c = b.new_context(viewport={"width": 1440, "height": 900})
    p = c.new_page()
    p.goto(base + "/login"); p.wait_for_load_state("networkidle")
    p.evaluate("""async()=>{const h={'Content-Type':'application/json'};let r=await fetch('/api/v1/auth/register',{method:'POST',credentials:'include',headers:h,body:JSON.stringify({name:'latprobe',password:'paperPass123',password_confirm:'paperPass123'})});if(r.status>=400)await fetch('/api/v1/auth/login',{method:'POST',credentials:'include',headers:h,body:JSON.stringify({name:'latprobe',password:'paperPass123'})});}""")
    p.evaluate("localStorage.setItem('auu.lang','zh-CN')")
    p.goto(base + "/airdrops"); p.wait_for_load_state("networkidle"); time.sleep(2.5)
    p.evaluate("()=>{const b=[...document.querySelectorAll('button,a')].find(e=>/跳过|skip/i.test(e.textContent));b&&b.click()}")
    p.evaluate(OBS)
    api_seen = {}

    def mono_offset():  # python CLOCK_MONOTONIC (ms) - page performance.now() (ms); constant if both clocks agree
        m1 = time.monotonic(); pn = p.evaluate("performance.now()"); m2 = time.monotonic()
        return (m1 + m2) / 2 * 1000 - pn

    off0 = mono_offset()
    t_end = time.monotonic() + dur
    n_polls = 0
    while time.monotonic() < t_end:
        r = p.evaluate("""async()=>{const r=await fetch('/api/v1/airdrops?size=50&origin=ingested',{credentials:'include',cache:'no-store'});return r.ok?(await r.json()).data.items.map(i=>i.title):[]}""")
        now = time.monotonic() * 1000
        n_polls += 1
        for t in r:
            m = re.match(r"([A-Z][a-z]+\d+): airdrop", t)
            if m and m.group(1) not in api_seen:
                api_seen[m.group(1)] = now
        time.sleep(2)
    off1 = mono_offset()
    print(f"monotonic offset drift over the run: {off1 - off0:.1f} ms")
    dom = {k: v + off1 for k, v in p.evaluate("() => window.__seen").items()}
    if shot_dir:
        p.screenshot(path=f"{shot_dir}/latency-run-final.png")
    b.close()

pubs = [json.loads(l) for l in open(pub_path)]
rows = []
for e in pubs:
    key = e["title"].split(":")[0]
    tp = e["t_pub"] * 1000  # monotonic seconds -> ms
    ta, td = api_seen.get(key), dom.get(key)
    rows.append({"n": e["n"], "source": e["source"], "t_pub": tp, "api_s": (ta - tp) / 1000 if ta else None, "page_s": (td - tp) / 1000 if td else None})
def stats(vals):
    v = sorted(x for x in vals if x is not None)
    if not v: return None
    q = lambda p: v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]
    return {"n": len(v), "mean": sum(v) / len(v), "min": v[0], "p50": q(50), "p90": q(90), "max": v[-1]}
res = {"clock": "CLOCK_MONOTONIC", "offset_drift_ms": off1 - off0, "events": rows, "api_visible_s": stats([r["api_s"] for r in rows]), "page_visible_s": stats([r["page_s"] for r in rows]), "api_polls": n_polls}
json.dump(res, open(out, "w"), indent=1)
print(json.dumps({k: v for k, v in res.items() if k != "events"}, indent=1))
for r in rows: print(r)
