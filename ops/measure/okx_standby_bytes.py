"""OKX standby traffic + wake latency (AUU_RT_OKX_MODE=standby, see docs/realtime-ws-plan.md "OKX 省流待机").

``bytes``: the real ``OkxTickerFeed`` / ``OkxCandleFeed`` in standby against the loopback OKX mock, through a TCP
proxy that counts every byte and every read chunk in both directions. The mock behaves like OKX for an idle
standby connection: answers the text ``ping``, answers protocol pings (websockets), pushes one ``funding-rate``
frame (real shape, ~0.5 KB) every ``--funding`` seconds (OKX: 30-90 s). Time is compressed by ``--speed`` (idle
watchdog, protocol ping and funding interval all divided), so ``--seconds 60 --speed 10`` = 10 real minutes.
Wire bytes add, per inbound chunk, a TLS 1.3 record (22 B) + TCP/IP headers (52 B), plus one 52 B ACK per
outbound chunk — an upper-bound style estimate (real TCP coalesces).

``wake`` (optional, real network, public data only): connect to OKX /public, subscribe the probe, then time
(a) subscribe ``tickers`` for the 19 coins on that warm connection -> first / all tickers, and (b) a cold
connection to /business + candle subscribe -> first candle. This is what standby adds over full mode.

    cd apps/api && python ../../ops/measure/okx_standby_bytes.py bytes --seconds 60 --speed 10
    cd apps/api && python ../../ops/measure/okx_standby_bytes.py wake --rounds 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

COINS = "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split()
MONTH = 30 * 86400


class CountingProxy:
    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.down = self.up = self.down_chunks = self.up_chunks = 0
        self.server = None
        self.port = 0

    async def start(self) -> "CountingProxy":
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _pipe(self, r, w, down: bool) -> None:
        try:
            while True:
                b = await r.read(65536)
                if not b:
                    break
                if down:
                    self.down += len(b)
                    self.down_chunks += 1
                else:
                    self.up += len(b)
                    self.up_chunks += 1
                w.write(b)
                await w.drain()
        except Exception:
            pass
        finally:
            w.close()

    async def _client(self, cr, cw) -> None:
        sr, sw = await asyncio.open_connection("127.0.0.1", self.target_port)
        await asyncio.gather(self._pipe(cr, sw, False), self._pipe(sr, cw, True))

    def reset(self) -> None:
        self.down = self.up = self.down_chunks = self.up_chunks = 0


async def measure_bytes(seconds: float, speed: float, funding: float) -> dict:
    from app.marketdata.realtime.bus import MarketBus
    from app.marketdata.realtime.config import RtConfig
    from app.marketdata.realtime.feeds import OkxCandleFeed, OkxTickerFeed
    from tests.rt_mock_exchange import MockExchange

    class Sink:
        def __init__(self, bus):
            self.bus = bus

        def on_ticker(self, ex, base, row):
            self.bus.set_ticker(ex, base, row)

        def on_bar(self, *a):
            pass

    mock = await MockExchange("okx").start()
    proxy = await CountingProxy(mock.port).start()
    url = f"ws://127.0.0.1:{proxy.port}"
    cfg = RtConfig(enabled=True, exchanges=("binance", "okx"), okx_public_url=url, okx_business_url=url,
                   idle_sec=20 / speed, ws_ping_sec=20 / speed)
    bus = MarketBus()
    feeds = [OkxTickerFeed(cfg, COINS, "USDT", bus, Sink(bus)), OkxCandleFeed(cfg, COINS, "USDT", bus, Sink(bus))]
    tasks = [asyncio.create_task(f.run()) for f in feeds]
    stop = asyncio.Event()

    async def funding_pusher():
        while not stop.is_set():
            await asyncio.sleep(funding / speed)
            await mock.push("funding-rate", "BTC-USDT-SWAP", mock.okx_funding())

    pusher = asyncio.create_task(funding_pusher())
    t0 = time.monotonic()
    while not mock.subscribed_now() and time.monotonic() - t0 < 5:
        await asyncio.sleep(0.01)
    connect_down = proxy.down  # handshake + subscribe ack (once per connection; ~daily with 23.5 h rotation)
    proxy.reset()
    await asyncio.sleep(seconds)
    sim = seconds * speed
    down, up, dch, uch = proxy.down, proxy.up, proxy.down_chunks, proxy.up_chunks
    subscribed, conns = sorted(map(list, mock.subscribed_now())), len(mock.conns)
    stop.set()
    pusher.cancel()
    for f in feeds:
        f.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, pusher, return_exceptions=True)
    await mock.stop()
    wire_down = down + dch * (22 + 52) + uch * 52
    return {
        "simulatedSec": sim, "connections": conns, "subscribed": subscribed,
        "connectBytes": connect_down, "pings": mock.pings, "wsDownBytes": down, "downChunks": dch, "upChunks": uch,
        "wsDownBps": round(down / sim, 2), "wireDownBps": round(wire_down / sim, 2),
        "wsDownMBMonth": round(down / sim * MONTH / 1e6, 2), "wireDownMBMonth": round(wire_down / sim * MONTH / 1e6, 2),
        "candlesConnected": feeds[1].state.connects,
    }


async def measure_wake(rounds: int) -> dict:
    from websockets.asyncio.client import connect

    pub, biz = "wss://ws.okx.com/ws/v5/public", "wss://ws.okx.com/ws/v5/business"
    first, allt, cold, handshake = [], [], [], []
    for _ in range(rounds):
        async with connect(pub, open_timeout=10, compression=None) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}]}))
            await asyncio.sleep(2.0)
            t0 = time.perf_counter()
            await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "tickers", "instId": f"{c}-USDT"} for c in COINS]}))
            seen, t_first = set(), None
            while len(seen) < len(COINS) and time.perf_counter() - t0 < 10:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if (m.get("arg") or {}).get("channel") == "tickers" and m.get("data"):
                    t_first = t_first or time.perf_counter()
                    seen.add(m["arg"]["instId"])
            first.append((t_first - t0) * 1000)
            allt.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        async with connect(biz, open_timeout=10, compression=None) as ws:
            handshake.append((time.perf_counter() - t0) * 1000)
            await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "candle1H", "instId": "BTC-USDT"}]}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if m.get("data"):
                    break
            cold.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(1)
    r = lambda xs: {"p50": round(statistics.median(xs), 1), "max": round(max(xs), 1)}
    return {"rounds": rounds, "warmFirstTickerMs": r(first), "warmAll19Ms": r(allt), "coldHandshakeMs": r(handshake),
            "coldFirstCandleMs": r(cold)}


def main() -> None:
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("bytes")
    b.add_argument("--seconds", type=float, default=60)
    b.add_argument("--speed", type=float, default=10)
    b.add_argument("--funding", type=float, default=60, help="real seconds between funding-rate pushes")
    w = sp.add_parser("wake")
    w.add_argument("--rounds", type=int, default=5)
    a = ap.parse_args()
    out = asyncio.run(measure_bytes(a.seconds, a.speed, a.funding) if a.cmd == "bytes" else measure_wake(a.rounds))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
