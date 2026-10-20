"""Local mock backend for the step-3 frontend (screenshots + push-to-DOM latency). Loopback only.

Runs, in one process: a fake public exchange behind the real ``MainstreamService`` (REST candles / tickers /
funding / order book, no network), the loopback mock Binance combined stream (``tests/rt_mock_exchange.py``) fed
by a pusher (every coin 1 ticker/s, 1h/4h/1d forming bars every 2 s), the real FastAPI app (uvicorn) with
``AUU_RT_WS=on|off``, and the built frontend (``apps/web/dist``) served from the same origin.
Every BTC price that is published is logged as ``{"px": ..., "E": exchange_ms}`` (both modes) so the browser side
can match what the page shows to when it was published.

Control (plain HTTP on --ctl-port): ``/stall`` stops the exchange stream (connections dropped and refused, REST keeps
working) -> the server goes stale and tells clients ``rt_status live=false``; ``/resume`` restores it.

    cd apps/api && python ../../ops/measure/rt_frontend_mock_server.py --rt on --port 5231 --ctl-port 5232 \
        --dist ../web/dist --log /tmp/rt3/pub.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

COINS = "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split()
BASE_PX = dict(zip(COINS, [67000, 2600, 150, 0.52, 0.12, 590, 0.35, 27, 11.5, 66, 0.16, 4.3, 330, 18.5, 0.09, 4.4, 3.4, 7.1, 4.6]))
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "8h": 28_800_000}


def now_ms() -> int:
    return int(time.time() * 1000)


class State:
    """Shared truth for REST (fake ccxt) and the stream."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.px = {c: float(BASE_PX[c]) for c in COINS}
        self.open24 = {c: self.px[c] * (1 - random.uniform(-0.03, 0.03)) for c in COINS}
        self.at = now_ms()
        self.i = 0

    def step(self) -> int:
        with self.lock:
            self.i += 1
            for c in COINS:
                if c == "BTC":  # unique, readable values so the browser can match them
                    self.px[c] = round(67000 + (self.i % 2000) * 0.37 + 0.01, 2)
                else:
                    self.px[c] = self.px[c] * (1 + random.gauss(0, 0.0004))
            self.at = now_ms()
            return self.at


class FakeExchange:
    """Public ccxt-like client: deterministic history that ends at the live mock price."""

    def __init__(self, st: State) -> None:
        self.st = st
        self.name = "binance"
        self.apiKey = self.secret = ""

    def _bar(self, base: str, ts: int, step: int) -> list:
        k = ts // step
        ke = now_ms() // step  # anchored so the history ends at the base (= live) price

        def f(j):
            return BASE_PX[base] * (1 + 0.08 * (math.sin(j / 37.0) - math.sin(ke / 37.0)) + 0.03 * (math.sin(j / 7.3) - math.sin(ke / 7.3)))

        p0, p1 = f(k), f(k + 1)
        hi, lo = max(p0, p1) * 1.004, min(p0, p1) * 0.996
        return [ts, p0, hi, lo, p1, 1000.0 + (k % 17) * 40]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        base = symbol.split("/")[0]
        step = TF_MS[timeframe]
        end = now_ms() // step * step
        lim = int(limit or 500)
        start = end - (lim - 1) * step if since is None else (int(since) + step - 1) // step * step
        out, ts = [], start
        while ts <= end and len(out) < lim:
            out.append(self._bar(base, ts, step))
            ts += step
        if out and out[-1][0] == end:  # forming bar closes at the live price
            with self.st.lock:
                px = self.st.px.get(base, out[-1][4])
            out[-1][4] = px
            out[-1][2] = max(out[-1][2], px)
            out[-1][3] = min(out[-1][3], px)
        return out

    def fetch_tickers(self, symbols=None):
        with self.st.lock:
            return {s: {"symbol": s, "last": self.st.px[s.split("/")[0]], "percentage": (self.st.px[s.split("/")[0]] / self.st.open24[s.split("/")[0]] - 1) * 100,
                        "quoteVolume": 1.2e9, "timestamp": self.st.at} for s in (symbols or []) if s.split("/")[0] in self.st.px}

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        step = 8 * 3_600_000
        end = now_ms() // step * step
        ts = (int(since) + step - 1) // step * step if since else end - 90 * step
        out = []
        while ts <= end and len(out) < (limit or 100):
            out.append({"symbol": symbol, "timestamp": ts, "fundingRate": 0.0001 * math.sin(ts / step / 5.0)})
            ts += step
        return out

    def fetch_funding_rate(self, symbol):
        return {"symbol": symbol, "fundingRate": 0.0001, "fundingTimestamp": now_ms() + 3_600_000, "timestamp": now_ms()}

    def fetch_order_book(self, symbol, limit=None):
        base = symbol.split("/")[0]
        with self.st.lock:
            px = self.st.px.get(base, 1.0)
        n = int(limit or 20)
        return {"symbol": symbol, "timestamp": now_ms(), "bids": [[px * (1 - 0.0001 * (i + 1)), 0.5 + i * 0.1] for i in range(n)],
                "asks": [[px * (1 + 0.0001 * (i + 1)), 0.5 + i * 0.1] for i in range(n)]}

    def load_markets(self, *a, **kw):
        return {}


def miniticker(base: str, px: float, open_: float, e: int) -> str:
    s = f"{base}USDT"
    return json.dumps({"stream": f"{s.lower()}@miniTicker", "data": {"e": "24hrMiniTicker", "E": e, "s": s, "c": f"{px}", "o": f"{open_}",
                                                                    "h": f"{px}", "l": f"{px}", "v": "1000", "q": "1200000000"}})


def kline(base: str, tf: str, bar: list, e: int) -> str:
    s = f"{base}USDT"
    return json.dumps({"stream": f"{s.lower()}@kline_{tf}", "data": {"e": "kline", "E": e, "s": s, "k": {
        "t": bar[0], "T": bar[0] + TF_MS[tf] - 1, "s": s, "i": tf, "o": f"{bar[1]}", "h": f"{bar[2]}", "l": f"{bar[3]}", "c": f"{bar[4]}",
        "v": f"{bar[5]}", "x": False}}})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rt", choices=["on", "off"], default="on")
    ap.add_argument("--port", type=int, default=5231)
    ap.add_argument("--ctl-port", type=int, default=5232)
    ap.add_argument("--dist", default=str(API_DIR.parent / "web" / "dist"))
    ap.add_argument("--log", default="/tmp/rt3_pub.jsonl")
    ap.add_argument("--stale-sec", default="10")
    a = ap.parse_args()

    from tests.rt_mock_exchange import MockExchange

    st = State()
    fake = FakeExchange(st)
    mock = await MockExchange("binance").start() if a.rt == "on" else None
    data_dir = tempfile.mkdtemp(prefix="rt3-")
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("AUU_", "SMTP"))}
    os.environ.clear()
    os.environ.update(keep)
    os.environ.update({
        "AUU_SKIP_DOTENV": "1", "AUU_DATA_DIR": data_dir, "AUU_AUTH": "off", "AUU_SHIELD": "off", "AUU_API_DOCS": "off",
        "AUU_MAINSTREAM_REFRESH": "off", "AUU_STRATEGY_RUNNER": "off", "AUU_SHADOW_S3": "off", "AUU_SHADOW_H2": "off",
        "AUU_SIGNAL_SHADOW": "off", "AUU_ALERTS": "off", "AUU_PRICE_ALERTS": "off", "AUU_RECON": "off", "AUU_UPTIME": "off",
        "AUU_NEWS": "off", "AUU_AIRDROPS_INGEST": "off", "AUU_TRC20_POLL": "off", "AUU_MAINSTREAM_EXCHANGE": "binance",
        "AUU_MAINSTREAM_SYMBOLS": "BTC,ETH,SOL", "AUU_MAINSTREAM_BACKFILL_1D_DAYS": "200", "AUU_MAINSTREAM_BACKFILL_4H_DAYS": "40",
        "AUU_MAINSTREAM_BACKFILL_1H_DAYS": "10", "AUU_MAINSTREAM_INTRADAY_DAYS": "1", "AUU_MAINSTREAM_FUNDING_DAYS": "10",
        "AUU_STRATEGY_FUNDING_DAYS": "10", "PYTHONDONTWRITEBYTECODE": "1",
    })
    if mock is not None:
        os.environ.update({"AUU_RT_WS": "on", "AUU_RT_WS_EXCHANGES": "binance", "AUU_RT_BINANCE_WS_URLS": mock.url,
                           "AUU_RT_XCHECK_SEC": "0", "AUU_RT_STALE_SEC": a.stale_sec})

    import app.marketdata.mainstream.service as S

    S.make_exchange = lambda name, cfg: fake
    svc = S.get_service()
    svc._factory = lambda name, cfg: fake
    t0 = time.time()
    await asyncio.to_thread(svc.refresh_once)
    print(f"store filled in {time.time() - t0:.1f}s", flush=True)

    import uvicorn
    from starlette.responses import FileResponse

    from app.main import create_app

    api = create_app()
    dist = Path(a.dist).resolve()

    async def asgi(scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith("/api"):
            p = (dist / scope["path"].lstrip("/")).resolve()
            if not str(p).startswith(str(dist)) or not p.is_file():
                p = dist / "index.html"
            return await FileResponse(p)(scope, receive, send)
        return await api(scope, receive, send)

    stall = {"on": False}
    log = open(a.log, "a", buffering=1)
    bars: dict = {}

    async def pusher():
        i = 0
        while True:
            await asyncio.sleep(1.0)
            e = st.step()
            with st.lock:
                px = dict(st.px)
            log.write(json.dumps({"px": px["BTC"], "E": e, "stream": bool(mock and not stall["on"])}) + "\n")
            if mock is None or stall["on"]:
                continue
            i += 1
            frames = [miniticker(c, px[c], st.open24[c], e) for c in COINS]
            if i % 2 == 0:
                for c in COINS:
                    for tf in ("1h", "4h", "1d"):
                        step = TF_MS[tf]
                        t = e // step * step
                        b = bars.get((c, tf))
                        if b is None or b[0] != t:
                            b = bars[(c, tf)] = [t, px[c], px[c], px[c], px[c], 0.0]
                        b[2], b[3], b[4], b[5] = max(b[2], px[c]), min(b[3], px[c]), px[c], b[5] + 1.5
                        frames.append(kline(c, tf, b, e))
            for f in frames:
                await mock.broadcast(f)

    async def ctl(reader, writer):
        line = (await reader.readline()).decode()
        path = line.split(" ")[1] if " " in line else "/"
        if path.startswith("/stall") and mock is not None:
            stall["on"] = True
            mock.refuse = 10 ** 6
            await mock.drop_all()
        elif path.startswith("/resume") and mock is not None:
            stall["on"] = False
            mock.refuse = 0
        body = json.dumps({"stall": stall["on"], "rt": a.rt}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n%s" % (len(body), body))
        await writer.drain()
        writer.close()

    await asyncio.start_server(ctl, "127.0.0.1", a.ctl_port)
    asyncio.create_task(pusher())
    server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=a.port, log_level="warning", ws="websockets", lifespan="on"))
    print(f"ready rt={a.rt} http://127.0.0.1:{a.port}", flush=True)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
