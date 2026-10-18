#!/usr/bin/env python3
"""Browser check of the 空投雷达 auto-refresh behaviour + screenshots (local stack only).
usage: airdrop_ui_check.py BASE_URL SHOT_DIR [degraded]
 1) visible: counts GET /api/v1/airdrops (list) requests over ~125 s  -> expect >= 2 (every 60 s)
 2) hidden (document.hidden=true + visibilitychange): ~130 s            -> expect 0 requests
 3) visible again                                                       -> expect a request within 3 s
 screenshots: desktop / mobile / English header.  Output: SHOT_DIR/ui-check.json
 mode "degraded" (after stopping the mock origin): waits for a failed ingest tick and shoots the gentle notice
 mode "degraded-only": same but skips the timing checks (output ui-check-degraded.json)"""
import json, sys, time
from playwright.sync_api import sync_playwright

base, shots = sys.argv[1], sys.argv[2]
LOGIN = """async()=>{const h={'Content-Type':'application/json'};let r=await fetch('/api/v1/auth/register',{method:'POST',credentials:'include',headers:h,body:JSON.stringify({name:'uicheck',password:'paperPass123',password_confirm:'paperPass123'})});if(r.status>=400)await fetch('/api/v1/auth/login',{method:'POST',credentials:'include',headers:h,body:JSON.stringify({name:'uicheck',password:'paperPass123'})});}"""
SKIP = "()=>{const b=[...document.querySelectorAll('button,a')].find(e=>/跳过|skip/i.test(e.textContent));b&&b.click()}"
SET_HIDDEN = "(v)=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>v});document.dispatchEvent(new Event('visibilitychange'))}"
res = {}
with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    c = b.new_context(viewport={"width": 1440, "height": 900})
    p = c.new_page()
    reqs = []
    p.on("request", lambda r: reqs.append(time.monotonic()) if r.method == "GET" and r.url.split("?")[0].endswith("/api/v1/airdrops") else None)
    p.goto(base + "/login"); p.wait_for_load_state("networkidle")
    p.evaluate(LOGIN); p.evaluate("(()=>{localStorage.setItem('auu.lang','zh-CN');localStorage.setItem('auu.lang.ts',String(Date.now()+60000))})()")
    p.goto(base + "/airdrops"); p.wait_for_load_state("networkidle"); time.sleep(2.5)
    p.evaluate(SKIP); time.sleep(0.6)
    only = len(sys.argv) > 3 and sys.argv[3] == "degraded-only"
    n0 = len(reqs); t0 = time.monotonic()
    p.wait_for_timeout(0 if only else 125_000)
    res["visible_125s_requests"] = len(reqs) - n0
    res["visible_gaps_s"] = [round(b_ - a_, 1) for a_, b_ in zip(reqs[n0:], reqs[n0 + 1:])]
    header = p.locator(".ad-upd").inner_text()
    res["header_text"] = header
    if not only:
        p.screenshot(path=f"{shots}/airdrops-live-desktop.png")
        p.evaluate(SET_HIDDEN, True); time.sleep(0.5)
        n1 = len(reqs)
        p.wait_for_timeout(130_000)
        res["hidden_130s_requests"] = len(reqs) - n1
        p.evaluate(SET_HIDDEN, False)
        t_show = time.monotonic(); p.wait_for_timeout(3000)
        res["after_show_requests_in_3s"] = len([t for t in reqs[n1:] if t >= t_show - 0.2])
    # mobile + english
    m = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    mp = m.new_page(); mp.goto(base + "/login"); mp.wait_for_load_state("networkidle")
    mp.evaluate(LOGIN); mp.evaluate("(()=>{localStorage.setItem('auu.lang','zh-CN');localStorage.setItem('auu.lang.ts',String(Date.now()+60000))})()")
    mp.goto(base + "/airdrops"); mp.wait_for_load_state("networkidle"); time.sleep(2.5); mp.evaluate(SKIP); time.sleep(0.8)
    mp.screenshot(path=f"{shots}/airdrops-live-mobile.png")
    e = b.new_context(viewport={"width": 1440, "height": 900})
    ep = e.new_page(); ep.goto(base + "/login"); ep.wait_for_load_state("networkidle")
    ep.evaluate(LOGIN); ep.evaluate("(()=>{localStorage.setItem('auu.lang','en');localStorage.setItem('auu.lang.ts',String(Date.now()+60000))})()")
    ep.goto(base + "/airdrops"); ep.wait_for_load_state("networkidle"); time.sleep(2.5); ep.evaluate("()=>{const b=[...document.querySelectorAll('button,a')].find(e=>/skip/i.test(e.textContent));b&&b.click()}"); time.sleep(0.8)
    ep.screenshot(path=f"{shots}/airdrops-live-desktop-en.png")
    res["header_text_en"] = ep.locator(".ad-upd").inner_text()
    if len(sys.argv) > 3 and sys.argv[3] in ("degraded", "degraded-only"):
        # the caller has stopped the mock origin: wait for a failed tick (<= 4 min), reload, shoot the gentle notice
        deadline = time.monotonic() + 330
        while time.monotonic() < deadline:
            p.wait_for_timeout(10_000)
            if p.locator(".pro-alert.is-warn").count():
                break
        time.sleep(1)
        res["degraded_notice"] = [x.inner_text() for x in p.locator(".pro-alert.is-warn").all()]
        p.screenshot(path=f"{shots}/airdrops-live-degraded.png")
        mp.reload(); mp.wait_for_load_state("networkidle"); time.sleep(2)
        mp.screenshot(path=f"{shots}/airdrops-live-degraded-mobile.png")
    b.close()
json.dump(res, open(f"{shots}/ui-check{'-degraded' if only else ''}.json", "w"), indent=1, ensure_ascii=False)
print(json.dumps(res, indent=1, ensure_ascii=False))
