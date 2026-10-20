"""Browser side of the step-3 check: screenshots + push-to-DOM latency against rt_frontend_mock_server.py.

System python3 (playwright + /usr/bin/google-chrome). Prints one JSON summary line.
    python3 ops/measure/rt_frontend_browser.py --mode on  --base http://127.0.0.1:5231 --ctl http://127.0.0.1:5232 --log /tmp/rt3/pub_on.jsonl --out /workspace/rt3_shots
    python3 ops/measure/rt_frontend_browser.py --mode off --base http://127.0.0.1:5233 --log /tmp/rt3/pub_off.jsonl --out /workspace/rt3_shots
Latency = time the header price text changed in the DOM (MutationObserver, Date.now()) minus the mock exchange
event time ``E`` of that exact price (same box, same clock). ``wsIn`` = when the ticker frame reached the page.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.request

from playwright.sync_api import sync_playwright

INIT = r"""
localStorage.setItem('auu.lang','zh-CN'); performance.setResourceTimingBufferSize(20000);
window.__wsIn = []; window.__dom = [];
const W = window.WebSocket;
window.WebSocket = class extends W { constructor(...a){ super(...a); this.addEventListener('message', ev => {
  try { const m = JSON.parse(ev.data); if (m.type==='ticker' && m.payload && m.payload.symbol==='BTC') window.__wsIn.push({px:m.payload.last, t:Date.now(), recvAt:m.payload.recvAt}); } catch(e){} }); } };
const arm = () => { const el = document.querySelector('[data-testid=tk-price]'); if (!el) return setTimeout(arm, 50);
  window.__dom.push({txt: el.textContent, t: Date.now()});
  new MutationObserver(() => window.__dom.push({txt: el.textContent, t: Date.now()})).observe(el, {subtree:true, characterData:true, childList:true}); };
if (location.search.includes('symbol=')) arm();
"""


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def stats(xs):
    return {"n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "max": max(xs) if xs else None, "mean": round(statistics.mean(xs), 1) if xs else None}


def load_pub(path):
    return [(round(r["px"], 2), r["E"]) for r in map(json.loads, open(path))]


def e_of(pub, px, t):
    """Latest publication of ``px`` at or before ``t`` (BTC values repeat every 2000 steps)."""
    best = None
    for p, e in pub:
        if p == px and e <= t:
            best = e
    return best


def num(txt):
    s = re.sub(r"[^0-9.]", "", txt or "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def lat(page, pub, since, until, pages=()):
    """dom: header text change minus E of the shown price. visible: for every published BTC price, time until
    the header first shows that price or a newer one (= what a user waits; equals ``dom`` under push, ~half the
    poll period under polling). wsIn: ticker frame reaching the leader tab minus E."""
    dom = [d for d in page.evaluate("window.__dom") if since <= d["t"] <= until]
    ws = [w for p in (page, *pages) for w in p.evaluate("window.__wsIn") if since <= w["t"] <= until]
    d_l, w_l, shown = [], [], []
    for d in dom:
        e = e_of(pub, num(d["txt"]), d["t"])
        if e is not None:
            shown.append((d["t"], e))
            if d is not dom[0]:
                d_l.append(d["t"] - e)
    for w in ws:
        e = e_of(pub, round(float(w["px"]), 2), w["t"])
        if e is not None:
            w_l.append(w["t"] - e)
    vis = []
    for _, e in pub:
        if since + 2000 <= e <= until - 8000:
            hit = next((t for t, e2 in shown if t >= e and e2 >= e), None)
            if hit is not None:
                vis.append(hit - e)
    return {"dom": stats(d_l), "visible": stats(vis), "wsIn": stats(w_l), "domChanges": max(0, len(dom) - 1)}


def markets_gaps(page, since):
    ts = page.evaluate("""(s) => performance.getEntriesByType('resource').filter(e => e.name.includes('/mainstream/markets'))
        .map(e => performance.timeOrigin + e.startTime).filter(t => t >= s)""", since)
    return gaps(ts)


def badge(page):
    return page.evaluate("""() => [...document.querySelectorAll('[data-testid=rt-badge]')].map(b => ({text:b.textContent, state:b.dataset.state, reason:b.dataset.reason}))""")


def skip(page):
    page.evaluate("()=>{const b=[...document.querySelectorAll('button,a')].find(e=>/跳过|skip/i.test(e.textContent));b&&b.click()}")


def gaps(ts):
    ts = sorted(ts)
    return [round((b - a) / 1000, 1) for a, b in zip(ts, ts[1:])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["on", "off"], required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--ctl", default="")
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", default="/workspace/rt3_shots")
    ap.add_argument("--measure", type=int, default=60)
    a = ap.parse_args()
    res = {"mode": a.mode}
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        ctx.add_init_script(INIT)
        wss = []

        def hook(p, *_):
            p.on("websocket", lambda w: wss.append({"url": w.url, "t": time.time()}))

        lp = ctx.new_page(); hook(lp)
        lp.goto(a.base + "/mainstream", wait_until="networkidle"); skip(lp)
        lp.wait_for_timeout(6000)
        lp.screenshot(path=f"{a.out}/rt_{a.mode}_list.png", full_page=True)
        res["listBadge"] = badge(lp)
        dp = ctx.new_page(); hook(dp, True)
        dp.goto(a.base + "/mainstream?symbol=BTC", wait_until="networkidle"); skip(dp)
        dp.wait_for_timeout(8000)
        dp.screenshot(path=f"{a.out}/rt_{a.mode}_detail.png")
        res["detailBadge"] = badge(dp)
        res["listBadgeWith2Tabs"] = badge(lp)
        res["websocketsOpened2Tabs"] = len(wss)
        t0 = int(time.time() * 1000)
        dp.wait_for_timeout(a.measure * 1000)
        res["latency"] = lat(dp, load_pub(a.log), t0, int(time.time() * 1000), (lp,))
        res["marketsReqGapsS"] = markets_gaps(dp, t0)
        if a.mode == "on" and a.ctl:
            urllib.request.urlopen(a.ctl + "/stall").read()
            ts = time.time()
            fb = None
            while time.time() - ts < 30:
                bb = badge(dp)
                if bb and bb[0]["state"] != "live":
                    fb = round(time.time() - ts, 1); break
                dp.wait_for_timeout(200)
            res["fallbackAfterS"] = fb
            res["fallbackBadge"] = badge(dp)
            t1 = int(time.time() * 1000)
            dp.wait_for_timeout(30000)
            dp.screenshot(path=f"{a.out}/rt_fallback_polling_detail.png")
            lp.screenshot(path=f"{a.out}/rt_fallback_polling_list.png", full_page=True)
            res["fallbackListBadge"] = badge(lp)
            res["fallbackLatency"] = lat(dp, load_pub(a.log), t1, int(time.time() * 1000), (lp,))
            res["fallbackMarketsGapsS"] = markets_gaps(dp, t1)
            urllib.request.urlopen(a.ctl + "/resume").read()
            ts = time.time(); rc = None
            while time.time() - ts < 60:
                bb = badge(dp)
                if bb and bb[0]["state"] == "live":
                    rc = round(time.time() - ts, 1); break
                dp.wait_for_timeout(200)
            res["recoveredAfterS"] = rc
            t2 = int(time.time() * 1000)
            dp.wait_for_timeout(65000)
            res["recoveredMarketsGapsS"] = markets_gaps(dp, t2)
            res["recoveredLatency"] = lat(dp, load_pub(a.log), t2, int(time.time() * 1000), (lp,))
            dp.screenshot(path=f"{a.out}/rt_recovered_detail.png")
            res["recoveredBadge"] = badge(dp)
        res["websocketsTotal"] = len(wss)
        b.close()
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
