"""Latency harness (runs on Tokyo as user auu; never writes the live DB, never sends mail).
usage: harness.py CODE_DIR LABEL DURATION_S PAGE_POLL_S CHART_POLL_STORED_S CHART_POLL_INTRADAY_S
CODE_DIR contains apps/api (the code under test). Everything lands under /tmp/rt1/<label>/."""
import sys, os, time, json, threading, sqlite3, statistics, random, urllib.request, collections, asyncio, traceback
code, label, DUR, PAGE_P, CH_STORED_P, CH_INTRA_P = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6])
CHART_PERIOD = {"1m": CH_INTRA_P, "5m": CH_INTRA_P, "15m": CH_INTRA_P, "1h": CH_STORED_P, "4h": CH_STORED_P, "1d": CH_STORED_P}
if len(sys.argv) > 7:  # per-tf override like "1m=10,5m=15"
    for kv in sys.argv[7].split(","):
        k, v = kv.split("="); CHART_PERIOD[k] = float(v)
WARM = 100.0
work = f"/tmp/rt1/{label}"; os.makedirs(work + "/data", exist_ok=True)
for k in list(os.environ):
    if k.startswith("AUU_SMTP") or k.startswith("AUU_ALERT_TO"): os.environ.pop(k)
os.environ.update(AUU_DATA_DIR=work + "/data", PYTHONDONTWRITEBYTECODE="1", AUU_AUTH="off")
sys.dont_write_bytecode = True
sys.path.insert(0, code + "/apps/api")
src = sqlite3.connect("file:/var/lib/auu/data/mainstream.sqlite?mode=ro", uri=True, timeout=10)
dst = sqlite3.connect(work + "/data/mainstream.sqlite"); src.backup(dst); dst.close(); src.close()

# ---- count every HTTP request the app makes through ccxt
import ccxt, re
from ccxt.base.exchange import Exchange
REQ = []; _orig = Exchange.fetch
def _counted(self, url, *a, **k):
    REQ.append((time.time(), url.split("/")[2], re.sub(r"\?.*", "", url.split(".com")[-1].split(".vision")[-1]), re.search(r"interval=(\w+)", url).group(1) if "interval=" in url else None))
    return _orig(self, url, *a, **k)
Exchange.fetch = _counted

from app.marketdata.mainstream.service import MainstreamService, reset_service
from app.marketdata.mainstream.store import MarketStore
from app import price_alerts as pa
svc = MainstreamService(store=MarketStore(work + "/data/mainstream.sqlite"))
reset_service(svc)
store = pa.PriceAlertStore(work + "/data/price_alerts.sqlite"); pa.reset_store(store)
checker = pa.production_checker(); checker.user_fn = lambda u: None
checker.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no mail in harness"))

T0 = time.time(); STOP = threading.Event()
def tr(u):
    t = time.time(); d = json.loads(urllib.request.urlopen(u, timeout=5).read()); return d, (t + time.time()) / 2
TRUTH = []  # (t, price)
def truth_loop():
    while not STOP.is_set():
        try:
            d, tm = tr("https://api1.binance.com/api/v3/ticker/price?symbol=BTCUSDT"); TRUTH.append((tm, float(d["price"])))
        except Exception: pass
        STOP.wait(1.0)
def truth_at(t):
    if not TRUTH: return None
    return min(TRUTH[-400:], key=lambda x: abs(x[0] - t))[1]
def bps(a, b): return None if not a or not b else (a - b) / b * 1e4

th = [threading.Thread(target=truth_loop, daemon=True)]
th[0].start()
threading.Thread(target=lambda: asyncio.run(svc.run_loop()), daemon=True).start()

DISP = {"page": None, "chart": {}}  # what the "browser" currently shows
LOG = {"page_polls": [], "page_onscreen": [], "chart_polls": [], "chart_onscreen": [], "alerts": [], "errors": []}
def page_loop():
    while not STOP.is_set():
        try:
            now = time.time(); r = svc.markets(); row = next(x for x in r["items"] if x["symbol"] == "BTC")
            at = (row.get("priceAt") or row.get("priceTs") or 0) / 1000
            DISP["page"] = {"price": row["price"], "data_t": at, "src": r["tickers"]["source"]}
            LOG["page_polls"].append({"t": now - T0, "src": r["tickers"]["source"], "age_s": now - at if at else None, "age_ts_s": (now - row["priceTs"] / 1000) if row.get("priceTs") else None,
                                      "diff_bps": bps(row["price"], truth_at(now)), "ms": (time.time() - now) * 1000})
        except Exception as e: LOG["errors"].append(("page", repr(e)[:120]))
        STOP.wait(PAGE_P)
def chart_loop(tf):
    while not STOP.is_set():
        try:
            now = time.time(); c = svc.chart_candles("BTC", tf, limit=3); ms = (time.time() - now) * 1000
            b = c["candles"][-1]; step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[tf] * 1000
            ex = c["exchange"]
            cands = [c.get("priceAt"), c.get("tailAt")]
            if tf in ("1m", "5m", "15m"): cands.append(svc._od_tail_at.get((ex, "BTC", tf)))
            else:
                fl = svc.store.fetch_log(ex).get(("BTC", tf)); cands.append(fl and fl.get("last_ok_ms"))
            data_t = max([x for x in cands if x] or [0]) / 1000
            DISP["chart"][tf] = {"close": b["close"], "bar_ts": b["ts"], "data_t": data_t}
            tp = truth_at(now)
            k, _ = tr(f"https://api1.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit=1")
            LOG["chart_polls"].append({"tf": tf, "t": now - T0, "age_s": now - data_t if data_t else None, "diff_bps_vs_truth": bps(b["close"], float(k[0][4])),
                                       "bar_current": b["ts"] == k[0][0], "ms": ms, "has_priceAt": bool(c.get("priceAt")), "has_tailAt": bool(c.get("tailAt"))})
        except Exception as e: LOG["errors"].append(("chart " + tf, repr(e)[:120]))
        STOP.wait(CHART_PERIOD[tf])
def onscreen_loop():
    while not STOP.is_set():
        now = time.time(); tp = truth_at(now)
        p = DISP["page"]
        if p and p["data_t"]: LOG["page_onscreen"].append({"t": now - T0, "age_s": now - p["data_t"], "diff_bps": bps(p["price"], tp), "src": p["src"]})
        for tf, d in list(DISP["chart"].items()):
            if d["data_t"]: LOG["chart_onscreen"].append({"tf": tf, "t": now - T0, "age_s": now - d["data_t"], "diff_bps": bps(d["close"], tp)})
        STOP.wait(1.0)
ALERTS = {}  # id -> dict
def alert_driver():
    STOP.wait(WARM)
    n = 0
    while not STOP.is_set() and time.time() - T0 < DUR - 90:
        tp = truth_at(time.time())
        for _ in range(3):
            off = random.uniform(0.5, 8) * random.choice((-1, 1)); tgt = float(f"{tp * (1 + off / 1e4):.2f}")
            n += 1; s = pa.side_of(tp, tgt)
            try:
                a = store.create(f"h{n}", "BTC", tgt, "cross", side=s, now_ms=int(time.time() * 1000))
                ALERTS[a["id"]] = {"target": tgt, "side0": s, "created": time.time(), "cross_t": None}
            except Exception as e: LOG["errors"].append(("alert create", repr(e)[:120]))
        STOP.wait(15)
def alert_cross_watch():  # first truth sample whose side differs from the creation side
    seen = 0
    while not STOP.is_set():
        for tm, px in TRUTH[seen:]:
            for a in ALERTS.values():
                if a["cross_t"] is None and tm >= a["created"] and pa.side_of(px, a["target"]) != a["side0"]: a["cross_t"] = tm
        seen = len(TRUTH); STOP.wait(0.5)
def checker_loop():
    STOP.wait(WARM - 10)
    while not STOP.is_set():
        try: checker.tick()
        except Exception as e: LOG["errors"].append(("tick", repr(e)[:120]))
        STOP.wait(pa.CHECK_INTERVAL_SEC)
for f, a in [(page_loop, ()), (onscreen_loop, ()), (alert_driver, ()), (alert_cross_watch, ()), (checker_loop, ())] + [(chart_loop, (tf,)) for tf in CHART_PERIOD]:
    t = threading.Thread(target=f, args=a, daemon=True); t.start(); th.append(t)
time.sleep(DUR); STOP.set(); time.sleep(2)
with store._lock:
    rows = store._db.execute("SELECT id, status, triggered_at, trigger_price, price FROM price_alerts").fetchall()
for r in rows:
    a = ALERTS.get(r["id"])
    if a: LOG["alerts"].append({"id": r["id"], "status": r["status"], "fired": r["status"] == "triggered", "cross_t": a["cross_t"] and a["cross_t"] - T0, "trig_t": r["triggered_at"] and r["triggered_at"] / 1000 - T0,
                                "lat_s": (r["triggered_at"] / 1000 - a["cross_t"]) if (r["triggered_at"] and a["cross_t"]) else None, "created_t": a["created"] - T0, "target": a["target"], "trigger_price": r["trigger_price"]})
out = {"label": label, "code": code, "dur": DUR, "warm": WARM, "page_poll": PAGE_P, "chart_period": CHART_PERIOD, "check_interval": pa.CHECK_INTERVAL_SEC, "max_price_age_ms": pa.MAX_PRICE_AGE_MS,
       "ticker_ttl_ms": svc._ticker_ttl_ms(), "hosts_in_use": sorted({h for _, h, _, _ in REQ}), "req": [(round(t - T0, 2), h, p, i) for t, h, p, i in REQ], "truth_n": len(TRUTH), "log": LOG}
json.dump(out, open(work + "/out.json", "w"))
print("done", label, len(REQ), "requests", len(LOG["errors"]), "errors")
