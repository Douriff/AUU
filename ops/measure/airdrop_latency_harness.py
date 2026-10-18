#!/usr/bin/env python3
"""空投雷达 end-to-end latency harness. Local only: a mock origin plays OKX / CoinDesk / Lido / Optimism
(redirect chain) so nothing here touches any real service, key, server or database.

  mock-origin PORT SCHEDULE.json OUT.jsonl   serve the mock sources and publish items on a schedule
  api PORT DATA_DIR ORIGIN_PORT              run the real FastAPI app (real run_loop, real HTTP ingest) with the
                                             FEED_SOURCES urls pointed at the mock (no production code change)
  sim OUT.json [DAYS]                        fake-clock replay of the ingest cadence logic: OLD (900 s tick, 1800 s
                                             floors) vs NEW (240 s tick, per-source floors) on the same publish times

The browser side (page visibility of each item with the real 60 s poll) is measured by
airdrop_latency_browser.py. usage example: see docs/airdrop-radar.md ("实时性").
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
API_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "apps", "api"))

WORDS = ["Zephyrion", "Quantalis", "Orbitrex", "Nimbusfi", "Vortelle", "Kaldrith", "Sylvanex", "Pyrovane",
         "Lumeris", "Tessaro", "Brightmoor", "Cindrel", "Dravonix", "Elyndra", "Fornaxis", "Galdoria"]
SOURCES = ["okx", "coindesk", "lido", "optimism"]


# --------------------------------------------------------------------------------------------- mock origin
class Origin:
    def __init__(self, out_path: str):
        self.items: dict[str, list[dict]] = {k: [] for k in SOURCES}
        self.lock = threading.Lock()
        self.out = open(out_path, "a", buffering=1)
        self.hits: dict[str, int] = {}
        self.not_modified: dict[str, int] = {}

    def publish(self, n: int, source: str) -> None:
        w = WORDS[n % len(WORDS)]
        now = time.time()
        title = f"{w}{n}: airdrop eligibility snapshot announced"
        it = {"n": n, "title": title, "path": f"{w.lower()}-{n}", "t": now}
        with self.lock:
            self.items[source].insert(0, it)
        self.out.write(json.dumps({"n": n, "source": source, "title": title, "t_pub": now}) + "\n")

    def body(self, source: str) -> tuple[bytes, str]:
        with self.lock:
            items = list(self.items[source])
        if source == "okx":
            det = [{"title": i["title"], "url": f"https://www.okx.com/help/{i['path']}", "pTime": str(int(i["t"] * 1000)),
                    "annType": "announcements-latest-announcements"} for i in items]
            return json.dumps({"code": "0", "msg": "", "data": [{"totalPage": "1", "details": det}]}).encode(), "application/json"
        host = {"coindesk": "www.coindesk.com/markets", "lido": "blog.lido.fi", "optimism": "paragraph.com/@the-optimism-collective"}[source]
        rows = "".join(
            f"<item><title>{i['title']}</title><link>https://{host}/{i['path']}</link><guid>g-{i['path']}</guid>"
            f"<pubDate>{formatdate(i['t'], usegmt=True)}</pubDate><description>Eligibility and claim details.</description></item>"
            for i in items
        )
        return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Mock</title>{rows}</channel></rss>'.encode(), "text/xml"


def serve_origin(port: int, schedule_path: str, out_path: str) -> None:
    sched = json.load(open(schedule_path))  # [{"at": seconds, "source": "okx"}, ...]
    org = Origin(out_path)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, headers, body=b""):
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            org.hits[path] = org.hits.get(path, 0) + 1
            if path == "/__stats":
                return self._send(200, {"Content-Type": "application/json"}, json.dumps({"hits": org.hits, "304": org.not_modified}).encode())
            if path == "/optimism/feed/atom":  # the Mirror -> Paragraph chain: 301 then 308 then the feed
                return self._send(301, {"Location": "/optimism/api/blogs/rss/@opt"})
            if path == "/optimism/api/blogs/rss/@opt":
                return self._send(308, {"Location": "/optimism/real/@opt"})
            src = {"/okx": "okx", "/coindesk": "coindesk", "/lido": "lido", "/optimism/real/@opt": "optimism"}.get(path)
            if not src:
                return self._send(404, {})
            body, ctype = org.body(src)
            hdr = {"Content-Type": ctype}
            if src != "okx":  # RSS feeds are conditional; the OKX-style API is not
                etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
                hdr["ETag"] = etag
                if self.headers.get("If-None-Match") == etag:
                    org.not_modified[path] = org.not_modified.get(path, 0) + 1
                    return self._send(304, hdr)
            self._send(200, hdr, body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    t0 = time.time()

    def publisher():
        for n, ev in enumerate(sorted(sched, key=lambda e: e["at"])):
            time.sleep(max(0.0, t0 + ev["at"] - time.time()))
            org.publish(n, ev["source"])
            print(f"[{time.time() - t0:7.1f}s] published #{n} on {ev['source']}", flush=True)

    threading.Thread(target=publisher, daemon=True).start()
    print(f"mock origin on :{port}, {len(sched)} events", flush=True)
    srv.serve_forever()


# --------------------------------------------------------------------------------------------------- api
def run_api(port: int, data_dir: str, origin_port: int) -> None:
    os.makedirs(data_dir, exist_ok=True)
    os.environ.update(
        AUU_SKIP_DOTENV="1", AUU_DATA_DIR=data_dir, AUU_AUTH="on", AUU_ALLOW_SIGNUP="on",
        AUU_USER_STORE=os.path.join(data_dir, "users.json"), AUU_USER_JOURNAL_DIR=os.path.join(data_dir, "journals"),
        AUU_AUTH_RATE_MAX="1000", AUU_LEGACY_PUMP="off", AUU_STRATEGY_RUNNER="off", AUU_SHADOW_H2="off", AUU_SHADOW_S3="off",
        AUU_ALERTS="off", AUU_RECON="off", AUU_UPTIME="off", AUU_EXEC_SHADOW="off", AUU_SIGNAL_SHADOW="off", AUU_NEWS="off",
        AUU_MAINSTREAM_REFRESH="off", AUU_AIRDROPS_INGEST="on", AUU_ROLE="primary", DATA_PROVIDER="cex_public",
        AUU_AIRDROPS_FEEDS="okx,coindesk,lido,optimism", CORS_ORIGINS="http://127.0.0.1:5225,http://localhost:5225",
    )
    # (AUU_AIRDROPS_INTERVAL_SEC / *_MIN_*_SEC deliberately NOT set: this measures the shipped defaults.)
    sys.path.insert(0, API_DIR)
    from app.airdrops import ingest as ing

    base = f"http://127.0.0.1:{origin_port}"
    ing.FEED_SOURCES["okx"]["url"] = base + "/okx"
    ing.FEED_SOURCES["coindesk"]["url"] = base + "/coindesk"
    ing.FEED_SOURCES["lido"]["url"] = base + "/lido"
    ing.FEED_SOURCES["optimism"]["url"] = base + "/optimism/feed/atom"  # exercises 301 -> 308 -> 200
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="warning")


# ------------------------------------------------------------------------------------------------- sim
def simulate(out_path: str, days: float = 2.0) -> None:
    """Replay the *real* ``AirdropFetcher._due`` / ``run_loop`` cadence on a fake clock. Publishing is a Poisson-like
    process (random uniform times); latency = time until the next tick that actually requests the source.
    Browser poll (0-60 s after ingest, uniform) is added analytically for the NEW page; OLD page = 120 s news-style
    poll is not applicable (old airdrop page had no auto refresh: latency = until the user reloads)."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    sys.path.insert(0, API_DIR)
    os.environ.update(AUU_TEST="1", AUU_ROLE="primary", AUU_AIRDROPS_FEEDS="okx,bybit,coindesk,cointelegraph,lido,optimism,layerzero")
    from app import airdrops
    from app.airdrops import ingest as ing

    rng = random.Random(20261018)
    horizon = int(days * 86400)

    def run(tick: int, floors: dict[str, int] | None) -> dict[str, list[float]]:
        with tempfile.TemporaryDirectory() as tmp:
            st = airdrops.AirdropStore(Path(tmp) / "a.sqlite")
            clock = [1_800_000_000_000]
            f = ing.AirdropFetcher(st, transport=lambda m, v: ing.FetchResult(304), now_ms=lambda: clock[0])
            reqs: dict[str, list[int]] = {k: [] for k in ing.enabled_feeds()}
            patcher = patch.object(ing, "min_interval_for", (lambda meta: floors.get(next(k for k, m in ing.FEED_SOURCES.items() if m is meta), 0)) if floors else ing.min_interval_for)
            with patcher:
                t = 35  # run_loop's first tick
                while t < horizon:
                    clock[0] = 1_800_000_000_000 + t * 1000
                    out = f.tick()
                    for sid, r in out.items():
                        if isinstance(r, dict) and r.get("ok") and not r.get("skipped"):
                            reqs.setdefault(sid, []).append(t)
                    t += tick + 3  # + a few seconds of tick duration
            st.close()
        lat: dict[str, list[float]] = {}
        for sid, ts in reqs.items():
            pub = [rng.uniform(100, horizon - 4000) for _ in range(400)]
            vals = []
            for p in pub:
                nxt = next((x for x in ts if x >= p), None)
                if nxt is not None:
                    vals.append(nxt - p)
            lat[sid] = vals
        return lat

    old_floors = {sid: (0 if m.get("tier") == "media" else 1800) for sid, m in ing.FEED_SOURCES.items()}
    res = {}
    for label, tick, floors in (("old_900s_tick_1800s_floor", 900, old_floors), ("new_240s_tick_default_floors", 240, None)):
        lat = run(tick, floors)
        allv = sorted(v for vs in lat.values() for v in vs)
        pct = lambda p: allv[int(p / 100 * (len(allv) - 1))]
        res[label] = {
            "samples": len(allv), "mean_s": sum(allv) / len(allv), "p50_s": pct(50), "p90_s": pct(90), "p99_s": pct(99), "max_s": allv[-1],
            "by_source_mean_s": {k: round(sum(v) / len(v), 1) for k, v in lat.items() if v},
            "by_source_max_s": {k: round(max(v), 1) for k, v in lat.items() if v},
        }
    json.dump(res, open(out_path, "w"), indent=2)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mock-origin":
        serve_origin(int(sys.argv[2]), sys.argv[3], sys.argv[4])
    elif cmd == "api":
        run_api(int(sys.argv[2]), sys.argv[3], int(sys.argv[4]))
    elif cmd == "sim":
        simulate(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 2.0)
    else:
        print(__doc__)
        sys.exit(2)
