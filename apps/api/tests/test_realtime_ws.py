"""Realtime step 1/2: exchange WebSocket feed (mock servers on loopback only), bus, REST fallback, /api/v1/ws push.

No real exchange is contacted: every socket connect outside 127.0.0.1/::1 raises.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.marketdata import realtime as RT
from app.marketdata.realtime.bus import MarketBus, Subscriber
from app.marketdata.realtime.config import RtConfig, load_rt_config, rt_enabled
from app.marketdata.realtime.feeds import BinanceSpotFeed, OkxCandleFeed, OkxTickerFeed
from app.marketdata.realtime.manager import RealtimeManager
from tests.rt_mock_exchange import MockExchange
from tests.test_mainstream_data import H, NOW, _cfg, _NoNetwork
from tests.test_mainstream_markets import COINS, TickerExchange

_real_connect = socket.socket.connect


def _loopback_only(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise AssertionError(f"network access in test: {address!r}")
    return _real_connect(self, address)


def fast_cfg(**kw) -> RtConfig:
    base = dict(enabled=True, backoff_base_sec=0.02, backoff_max_sec=0.2, idle_sec=0.5, stale_ms=1000,
                stable_sec=0.3, xcheck_sec=0, open_timeout_sec=2, ws_ping_sec=5)
    base.update(kw)
    return RtConfig(**base)


class _Sink:
    def __init__(self, bus):
        self.bus, self.tickers, self.bars = bus, [], []

    def on_ticker(self, ex, base, row):
        self.bus.set_ticker(ex, base, row)
        self.tickers.append((ex, base, row))

    def on_bar(self, ex, base, tf, row, closed, event_ts, recv):
        self.bus.set_bar(ex, base, tf, row, closed, recv)
        self.bars.append((ex, base, tf, row, closed))


async def _until(pred, timeout=5.0, step=0.01):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(step)
    return False


class _Loopback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._p = patch.object(socket.socket, "connect", _loopback_only)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        RT.set_manager(None)


# ---- config / switch -------------------------------------------------------------------------
class ConfigTests(unittest.TestCase):
    def test_default_off_and_env_parsing(self):
        with patch.dict(os.environ, {}, clear=False):
            for k in [k for k in os.environ if k.startswith("AUU_RT_")]:
                os.environ.pop(k)
            self.assertFalse(rt_enabled())
            cfg = load_rt_config()
            self.assertFalse(cfg.enabled)
            self.assertEqual(cfg.exchanges, ("binance", "okx"))
            self.assertTrue(cfg.binance_urls[0].startswith("wss://stream.binance.com:443"))
            self.assertNotIn(":8443", cfg.okx_public_url)  # OKX closes 8443 on 2026-10-31
            self.assertTrue(cfg.okx_business_url.endswith("/ws/v5/business"))
        env = {"AUU_RT_WS": "on", "AUU_RT_WS_EXCHANGES": "okx,bogus", "AUU_RT_STALE_SEC": "1",
               "AUU_RT_BINANCE_WS_URLS": "ws://127.0.0.1:1, junk", "AUU_RT_XCHECK_SEC": "0"}
        with patch.dict(os.environ, env):
            cfg = load_rt_config()
            self.assertTrue(cfg.enabled)
            self.assertEqual(cfg.exchanges, ("okx",))
            self.assertEqual(cfg.stale_ms, 3000)  # clamped to >= 3 s
            self.assertEqual(cfg.binance_urls, ("ws://127.0.0.1:1",))
            self.assertEqual(cfg.xcheck_sec, 0)


# ---- bus ---------------------------------------------------------------------------------------
class BusTests(unittest.TestCase):
    def test_coalescing_keeps_latest_frame_per_key(self):
        async def go():
            bus = MarketBus()
            s = Subscriber()
            bus.add(s, ("ticker", "BTC"))
            bus.add(s, ("ticker", "ETH"))
            for i in range(100):
                bus.publish(("ticker", "BTC"), f"btc{i}")
            bus.publish(("ticker", "ETH"), "eth0")
            bus.publish(("ticker", "SOL"), "sol0")  # nobody subscribed
            self.assertEqual(sorted(s.drain()), ["btc99", "eth0"])
            self.assertEqual(s.coalesced, 99)
            bus.drop(s)
            self.assertEqual(bus.subscribed_keys(), [])
        asyncio.run(go())

    def test_snapshot_requires_fresh_feed_and_every_coin_since_connect(self):
        t = {"now": 1_000_000}
        bus = MarketBus(stale_ms=1000, clock=lambda: t["now"])
        st = bus.feed("binance", "binance", ("ticker", "kline"))
        self.assertIsNone(bus.ticker_snapshot("binance", ["BTC"]))
        st.connected, st.connected_at = True, t["now"]
        bus.set_ticker("binance", "BTC", {"last": 1.0, "change24h": 0.0, "quoteVolume": 1.0, "ts": 1, "at": t["now"]})
        st.last_data_ms = t["now"]
        self.assertIsNone(bus.ticker_snapshot("binance", ["BTC", "ETH"]))  # ETH not seen yet
        snap = bus.ticker_snapshot("binance", ["BTC"])
        self.assertEqual(snap["data"]["BTC"]["last"], 1.0)
        t["now"] += 1001
        self.assertIsNone(bus.ticker_snapshot("binance", ["BTC"]))  # stale -> REST
        st.connected_at = t["now"]  # reconnected: old values are not trusted until re-sent
        st.last_data_ms = t["now"]
        self.assertIsNone(bus.ticker_snapshot("binance", ["BTC"]))


# ---- feeds against mock servers --------------------------------------------------------------
class BinanceFeedTests(_Loopback):
    async def test_parse_stream_reconnect_with_exponential_backoff(self):
        mock = await MockExchange("binance").start()
        bus = MarketBus(stale_ms=1000)
        sink = _Sink(bus)
        cfg = fast_cfg(binance_urls=(mock.url,), tfs=("1h",))
        feed = BinanceSpotFeed(cfg, ["BTC", "ETH"], "USDT", bus, sink, rand=lambda: 1.0)
        task = asyncio.create_task(feed.run())
        try:
            self.assertTrue(await _until(lambda: mock.conns))
            self.assertEqual(sorted(mock.streams), sorted(["btcusdt@miniTicker", "btcusdt@kline_1h", "ethusdt@miniTicker",
                                                           "ethusdt@kline_1h"]))
            await mock.broadcast(mock.binance_ticker("BTC", 100.0, open_=80.0))
            await mock.broadcast(mock.binance_kline("BTC", "1h", NOW // H * H, 1, 2, 0.5, 1.5))
            await mock.broadcast("not json")  # bad frame: counted, feed keeps running
            await mock.broadcast(json.dumps({"result": None, "id": 1}))
            self.assertTrue(await _until(lambda: len(sink.bars) == 1 and feed.state.parse_errors == 1))
            ex, base, row = sink.tickers[0]
            self.assertEqual((ex, base, row["last"]), ("binance", "BTC", 100.0))
            self.assertAlmostEqual(row["change24h"], 0.25)
            self.assertTrue(bus.fresh("binance", "ticker"))
            # server refuses the next 3 connections -> 3 failed attempts with doubling delays, then recovery
            mock.refuse = 3
            await mock.drop_all()
            self.assertTrue(await _until(lambda: mock.attempts >= 5 and mock.conns, timeout=5))
            d = feed.delays
            self.assertGreaterEqual(len(d), 4)
            # rand=1.0 -> no jitter: base * 2^(n-1), n = consecutive failures (the first drop came < stable_sec)
            self.assertEqual([round(x, 3) for x in d[:4]], [0.02, 0.04, 0.08, 0.16])
            self.assertEqual(feed.state.connects, 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mock.stop()

    async def test_idle_watchdog_reconnects_and_backoff_caps(self):
        mock = await MockExchange("binance").start()
        bus = MarketBus(stale_ms=1000)
        cfg = fast_cfg(binance_urls=(mock.url,), idle_sec=0.2, backoff_max_sec=0.05)
        feed = BinanceSpotFeed(cfg, ["BTC"], "USDT", bus, _Sink(bus), rand=lambda: 1.0)
        task = asyncio.create_task(feed.run())
        try:
            # never sends data: every connection is closed by the watchdog after 0.2 s
            self.assertTrue(await _until(lambda: mock.attempts >= 4, timeout=5))
            self.assertIn("FeedIdle", feed.state.last_error or "")
            self.assertLessEqual(max(feed.delays), 0.05 + 1e-9)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mock.stop()

    async def test_connect_failure_rotates_gateways(self):
        mock = await MockExchange("binance").start()
        bus = MarketBus()
        dead = "ws://127.0.0.1:1"  # nothing listens
        cfg = fast_cfg(binance_urls=(dead, mock.url))
        feed = BinanceSpotFeed(cfg, ["BTC"], "USDT", bus, _Sink(bus))
        task = asyncio.create_task(feed.run())
        try:
            self.assertTrue(await _until(lambda: mock.conns, timeout=5))
            self.assertTrue(feed.state.url.startswith(mock.url))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mock.stop()


class OkxFeedTests(_Loopback):
    async def test_subscribe_heartbeat_ping_and_dead_pong_reconnect(self):
        mock = await MockExchange("okx").start()
        bus = MarketBus(stale_ms=1000)
        sink = _Sink(bus)
        cfg = fast_cfg(exchanges=("okx",), okx_public_url=mock.url, okx_business_url=mock.url, idle_sec=0.2)
        tick = OkxTickerFeed(cfg, ["BTC"], "USDT", bus, sink)
        candles = OkxCandleFeed(cfg, ["BTC"], "USDT", bus, sink)
        tasks = [asyncio.create_task(tick.run()), asyncio.create_task(candles.run())]
        try:
            self.assertTrue(await _until(lambda: len(mock.subscribed) >= 4))
            chans = sorted(a["channel"] for a in mock.subscribed)
            self.assertEqual(chans, sorted(["tickers", "candle1H", "candle4H", "candle1Dutc"]))
            await mock.broadcast(mock.okx_ticker("BTC", 50.0, open_=40.0))
            await mock.broadcast(mock.okx_candle("BTC", "candle1Dutc", NOW // 86_400_000 * 86_400_000, 1, 2, 0.5, 1.5, "1"))
            self.assertTrue(await _until(lambda: sink.tickers and sink.bars))
            self.assertEqual(sink.bars[0][2], "1d")
            self.assertTrue(sink.bars[0][4])  # confirm=1 -> closed
            # quiet -> client sends "ping", server answers "pong" -> connection kept
            self.assertTrue(await _until(lambda: mock.pings >= 2, timeout=3))
            self.assertEqual(tick.state.connects, 1)
            # server stops answering -> reconnect
            mock.silent = True
            self.assertTrue(await _until(lambda: tick.state.connects >= 2, timeout=3))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await mock.stop()


# ---- manager: selection, cross-check, non-blocking ---------------------------------------------
class ManagerTests(_Loopback):
    def _mgr(self, **kw):
        t = {"now": NOW}
        m = RealtimeManager(fast_cfg(stale_ms=10_000, xcheck_bps=50, xcheck_strikes=2, **kw), bases=["BTC"],
                            preferred=lambda: "binance", clock=lambda: t["now"])
        return m, t

    def _feed(self, m, fid, ex, carries, now):
        st = m.bus.feed(fid, ex, carries)
        st.connected, st.connected_at, st.last_data_ms = True, now - 1, now
        return st

    async def test_selection_prefers_rest_exchange_and_fails_over(self):
        m, t = self._mgr()
        b = self._feed(m, "binance", "binance", ("ticker", "kline"), NOW)
        self._feed(m, "okx:tickers", "okx", ("ticker",), NOW)
        sub = Subscriber()
        m.bus.add(sub, ("status",))
        m.reselect()
        self.assertEqual(m.selected, "binance")
        b.connected = False
        m.reselect()
        self.assertEqual(m.selected, "okx")
        frames = [json.loads(f) for f in sub.drain()]
        self.assertEqual(frames[-1]["payload"], {**frames[-1]["payload"], "live": True, "source": "okx"})
        t["now"] += 20_000
        m.reselect()
        self.assertIsNone(m.selected)
        self.assertFalse(json.loads(sub.drain()[-1])["payload"]["live"])

    async def test_cross_check_marks_stream_suspect_after_strikes(self):
        m, t = self._mgr()
        self._feed(m, "binance", "binance", ("ticker", "kline"), NOW)
        m.on_ticker("binance", "BTC", {"last": 100.0, "change24h": 0, "quoteVolume": 1, "ts": NOW, "at": NOW})
        self.assertAlmostEqual(m.compare("binance", {"BTC": 100.2})["maxDevBps"], 20.0, delta=0.1)  # within 50 bp
        self.assertTrue(m.bus.fresh("binance", "ticker"))
        m.compare("binance", {"BTC": 102.0})  # strike 1
        self.assertTrue(m.bus.fresh("binance", "ticker"))
        rep = m.compare("binance", {"BTC": 102.0})  # strike 2 -> suspect
        self.assertTrue(rep["suspect"])
        self.assertFalse(m.bus.fresh("binance", "ticker"))
        self.assertIsNone(m.bus.ticker_snapshot("binance", ["BTC"]))
        self.assertEqual(m.xcheck["binance"]["mismatches"], 2)

    async def test_xcheck_loop_runs_rest_in_a_thread(self):
        import threading

        seen = []
        m, _ = self._mgr()
        m.cfg = replace(m.cfg, xcheck_sec=0.05)
        m.rest_fn = lambda ex: seen.append(threading.current_thread() is threading.main_thread()) or {}
        m.bus.clock = lambda: int(time.time() * 1000)
        st = m.bus.feed("binance", "binance", ("ticker",))
        st.connected, st.last_data_ms = True, int(time.time() * 1000) + 60_000
        task = asyncio.create_task(m._xcheck_loop())
        await _until(lambda: len(seen) >= 2, timeout=3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(seen and not any(seen))  # never on the event-loop (main) thread

    async def test_burst_of_frames_does_not_block_event_loop(self):
        """The mock exchange runs in a child process (no GIL sharing), so a stall measured here is the feed's own
        work: 5000 frames as fast as loopback allows, parsed + published to a subscriber, with a 5 ms probe task."""
        import sys
        from pathlib import Path

        script = (
            "import asyncio, sys\n"
            "from tests.rt_mock_exchange import MockExchange\n"
            "COINS = sys.argv[1].split(',')\n"
            "async def main():\n"
            "    m = await MockExchange('binance').start()\n"
            "    print(m.port, flush=True)\n"
            "    while not m.conns: await asyncio.sleep(0.01)\n"
            "    await asyncio.sleep(0.2)\n"
            "    for i in range(5000):\n"
            "        await m.broadcast(m.binance_ticker(COINS[i % len(COINS)], 100 + i))\n"
            "    await asyncio.sleep(30)\n"
            "asyncio.run(main())\n")
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script, ",".join(COINS),
                                                    cwd=str(Path(__file__).resolve().parents[1]),
                                                    stdout=asyncio.subprocess.PIPE)
        port = int((await asyncio.wait_for(proc.stdout.readline(), 20)).strip())
        m = RealtimeManager(fast_cfg(binance_urls=(f"ws://127.0.0.1:{port}",), exchanges=("binance",)), bases=COINS,
                            preferred=lambda: "binance")
        sub = Subscriber()
        for b in COINS:
            m.bus.add(sub, ("ticker", b))
        run = asyncio.create_task(m.run())
        lags: list[float] = []

        async def probe():
            while True:
                t0 = time.perf_counter()
                await asyncio.sleep(0.005)
                lags.append(time.perf_counter() - t0 - 0.005)

        p = asyncio.create_task(probe())
        try:
            self.assertTrue(await _until(lambda: m.bus.feed_states()[0].messages >= 5000, timeout=20))
        finally:
            p.cancel()
            run.cancel()
            await asyncio.gather(p, run, return_exceptions=True)
            proc.kill()
            await proc.wait()
        self.assertGreater(len(lags), 10)
        lags.sort()
        print(f"loop lag during 5000-frame burst: n={len(lags)} p50={lags[len(lags)//2]*1000:.2f}ms "
              f"p99={lags[int(len(lags)*0.99)]*1000:.2f}ms max={lags[-1]*1000:.2f}ms")
        self.assertLess(lags[-1], 0.05, f"event loop stalled {lags[-1]:.3f}s")


# ---- REST read path integration -------------------------------------------------------------
class ServiceOverlayTests(_NoNetwork):
    def _svc(self, ex):
        t = {"now": NOW}
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC", "ETH", "SOL"], strategy_symbols=COINS))
        svc.now_ms = lambda: t["now"]
        svc.refresh_once()
        return svc, t

    def _wait(self, svc):
        for _ in range(200):
            if not svc._tk["busy"]:
                return
            time.sleep(0.01)

    def _mgr(self, t):
        m = RealtimeManager(fast_cfg(stale_ms=10_000), bases=COINS, preferred=lambda: "binance", clock=lambda: t["now"])
        m.running = True
        st = m.bus.feed("binance", "binance", ("ticker", "kline"))
        st.connected, st.connected_at, st.last_data_ms = True, t["now"] - 1, t["now"]
        for b in COINS:
            m.bus.set_ticker("binance", b, {"last": 321.0, "change24h": 0.01, "quoteVolume": 5.0, "ts": t["now"], "at": t["now"]})
        return m, st

    def tearDown(self):
        RT.set_manager(None)
        super().tearDown()

    def test_switch_off_is_the_old_rest_path(self):
        ex = TickerExchange()
        svc, _ = self._svc(ex)
        self.assertIsNone(RT.get_manager())
        svc.markets()
        self._wait(svc)
        self.assertEqual(len([c for c in ex.calls if c[0] == "fetch_tickers"]), 1)
        self.assertEqual(svc.markets()["items"][0]["price"], 200.0)

    def test_fresh_stream_feeds_markets_without_rest_then_falls_back_when_stale(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        m, st = self._mgr(t)
        RT.set_manager(m)
        out = svc.markets()
        self.assertEqual(out["tickers"]["source"], "ticker")
        self.assertEqual(out["items"][0]["price"], 321.0)
        self.assertEqual(out["items"][0]["priceAt"], t["now"])
        self.assertEqual([c for c in ex.calls if c[0] == "fetch_tickers"], [])  # no REST request at all
        # the price-alert checker path (sync=True) sees the same stream price
        self.assertEqual(svc.markets(sync=True)["items"][0]["price"], 321.0)
        # stream goes quiet: after stale_ms readers fall back to the REST ticker cache by themselves
        t["now"] += 10_001
        svc.markets()
        self._wait(svc)
        self.assertEqual(len([c for c in ex.calls if c[0] == "fetch_tickers"]), 1)
        self.assertEqual(svc.markets()["items"][0]["price"], 200.0)

    def test_disconnect_or_suspect_falls_back(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        m, st = self._mgr(t)
        RT.set_manager(m)
        st.connected = False
        svc.markets(sync=True)
        self.assertEqual(svc.markets()["items"][0]["price"], 200.0)
        st.connected = True
        m.bus.mark_suspect("binance", t["now"] + 60_000)
        t["now"] += 3_100
        st.last_data_ms = t["now"]
        svc.markets(sync=True)
        self.assertEqual(len([c for c in ex.calls if c[0] == "fetch_tickers"]), 2)

    def test_chart_tail_uses_stream_bar_without_rest(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        m, st = self._mgr(t)
        cur = NOW // H * H
        m.bus.set_bar("binance", "BTC", "1h", [cur - H, 1.0, 2.0, 0.5, 1.5, 9.0], True, t["now"])
        m.bus.set_bar("binance", "BTC", "1h", [cur, 300.0, 330.0, 290.0, 321.0, 7.0], False, t["now"])
        RT.set_manager(m)
        n0 = len(ex.calls)
        out = svc.chart_candles("BTC", "1h", limit=5)
        self.assertEqual(len(ex.calls), n0)  # no tail request, no ticker request
        last = out["candles"][-1]
        self.assertEqual((last["ts"], last["open"], last["close"], last["volume"]), (cur, 300.0, 321.0, 7.0))
        self.assertEqual(out["candles"][-2]["close"], 1.5)
        self.assertEqual(out["tailAt"], t["now"])
        # a bar from the previous period does not cover "now": REST tail is used
        t["now"] += H
        st.last_data_ms = t["now"]
        for b in COINS:
            m.bus.set_ticker("binance", b, {"last": 321.0, "change24h": 0.01, "quoteVolume": 5.0, "ts": t["now"], "at": t["now"]})
        svc.chart_candles("BTC", "1h", limit=5)
        self.assertTrue(any(c[0] == "fetch_ohlcv" for c in ex.calls[n0:]))


# ---- /api/v1/ws end to end (uvicorn on loopback, mock exchange, real websocket clients) ----------
class WsEndpointTests(_Loopback):
    async def _serve(self, cfg_kw=None):
        import uvicorn
        from websockets.asyncio.client import connect

        from app.main import create_app

        self.env = patch.dict(os.environ, {"AUU_AUTH": "off", "AUU_SHIELD": "off"})
        self.env.start()
        self.mock = await MockExchange("binance").start()
        cfg = fast_cfg(**{"binance_urls": (self.mock.url,), "exchanges": ("binance",), "stale_ms": 5000, **(cfg_kw or {})})
        self.m = RealtimeManager(cfg, bases=["BTC", "ETH"], preferred=lambda: "binance")
        RT.set_manager(self.m)
        self.run_task = asyncio.create_task(self.m.run())
        app = create_app(legacy=False)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", ws="websockets"))
        self.srv_task = asyncio.create_task(self.server.serve(sockets=[sock]))
        self.assertTrue(await _until(lambda: self.server.started and self.mock.conns))
        self.connect = lambda: connect(f"ws://127.0.0.1:{self.port}/api/v1/ws", open_timeout=3)

    async def asyncTearDown(self):
        if getattr(self, "server", None):
            self.server.should_exit = True
            await asyncio.gather(self.srv_task, return_exceptions=True)
            self.run_task.cancel()
            await asyncio.gather(self.run_task, return_exceptions=True)
            await self.mock.stop()
            self.env.stop()

    async def _recv_until(self, ws, pred, timeout=3.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if pred(msg):
                return msg
        raise AssertionError("frame not received")

    async def test_push_ticker_and_kline_from_exchange_to_client(self):
        await self._serve()
        await self.mock.broadcast(self.mock.binance_ticker("BTC", 100.0))
        self.assertTrue(await _until(lambda: self.m.selected == "binance", timeout=3))
        async with self.connect() as ws:
            hello = json.loads(await ws.recv())
            self.assertIn("ticker", hello["eventTypes"])
            self.assertEqual(hello["realtime"]["symbols"], ["BTC", "ETH"])
            st = json.loads(await ws.recv())
            self.assertEqual(st["type"], "rt_status")
            await ws.send(json.dumps({"type": "subscribe", "channel": "ticker", "symbols": ["BTC", "eth/usdt"]}))
            ack = await self._recv_until(ws, lambda m: m["type"] == "subscribed")
            self.assertEqual(ack["symbols"], ["BTC", "ETH"])
            snap = await self._recv_until(ws, lambda m: m["type"] == "ticker")
            self.assertEqual(snap["payload"]["last"], 100.0)  # latest value right after subscribing
            await ws.send(json.dumps({"type": "subscribe", "channel": "kline", "symbol": "BTC", "interval": "1h"}))
            await self._recv_until(ws, lambda m: m["type"] == "subscribed" and m["channel"] == "kline")
            sent = int(time.time() * 1000)
            await self.mock.broadcast(self.mock.binance_ticker("BTC", 101.5, event_ms=sent))
            got = await self._recv_until(ws, lambda m: m["type"] == "ticker" and m["payload"]["last"] == 101.5)
            self.assertEqual(got["payload"]["ts"], sent)
            self.assertLess(int(time.time() * 1000) - sent, 1000)
            await self.mock.broadcast(self.mock.binance_kline("BTC", "1h", int(time.time() * 1000) // H * H, 1, 2, 0.5, 1.7))
            k = await self._recv_until(ws, lambda m: m["type"] == "kline")
            self.assertEqual((k["payload"]["tf"], k["payload"]["close"]), ("1h", 1.7))
            # paper-hub ping/pong and errors keep working
            await ws.send(json.dumps({"type": "ping"}))
            await self._recv_until(ws, lambda m: m["type"] == "pong")
            await ws.send(json.dumps({"type": "subscribe", "channel": "ticker", "symbol": "NOPE"}))
            e = await self._recv_until(ws, lambda m: m["type"] == "error")
            self.assertEqual(e["payload"]["code"], "BAD_SUBSCRIBE")
            await ws.send(json.dumps({"type": "unsubscribe", "channel": "ticker", "symbols": ["BTC", "ETH"]}))
            await self._recv_until(ws, lambda m: m["type"] == "unsubscribed")
        self.assertTrue(await _until(lambda: self.m.clients == 0 and self.m.bus.subscriber_count() == 0))

    async def test_stream_stale_pushes_rt_status_not_live(self):
        await self._serve({"stale_ms": 400})

        async def keep_pushing():
            while True:
                await self.mock.broadcast(self.mock.binance_ticker("BTC", 100.0))
                await asyncio.sleep(0.05)

        pusher = asyncio.create_task(keep_pushing())
        self.assertTrue(await _until(lambda: self.m.selected == "binance", timeout=3))
        pusher.cancel()
        await asyncio.gather(pusher, return_exceptions=True)
        async with self.connect() as ws:
            await ws.recv()
            await ws.recv()
            await ws.send(json.dumps({"type": "subscribe", "channel": "ticker", "symbol": "BTC"}))
            st = await self._recv_until(ws, lambda m: m["type"] == "rt_status" and m["payload"]["live"] is False, timeout=4)
            self.assertIsNone(st["payload"]["source"])

    async def test_limits_per_client_subs_and_message_rate(self):
        await self._serve({"per_client_key": 1, "max_subs": 1, "msg_burst": 3})
        async with self.connect() as ws1:
            await ws1.recv()
            async with self.connect() as ws2:  # same IP, cap 1
                e = json.loads(await ws2.recv())
                self.assertEqual(e["payload"]["code"], "WS_PER_CLIENT")
            self.assertEqual(self.m.rejected, 1)
            await ws1.send(json.dumps({"type": "subscribe", "channel": "ticker", "symbols": ["BTC", "ETH"]}))
            e = await self._recv_until(ws1, lambda m: m["type"] == "error")
            self.assertEqual(e["payload"]["code"], "TOO_MANY_SUBS")
            for _ in range(5):
                await ws1.send(json.dumps({"type": "ping"}))
            e = await self._recv_until(ws1, lambda m: m["type"] == "error" and m["payload"]["code"] == "RATE_LIMIT")
            self.assertEqual(e["payload"]["code"], "RATE_LIMIT")

    async def test_status_route(self):
        await self._serve()
        import httpx

        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{self.port}/api/v1/mainstream/realtime", headers={"user-agent": "Mozilla/5.0"})
        d = r.json()["data"]
        self.assertTrue(d["enabled"])
        self.assertEqual(d["feeds"][0]["feed"], "binance")


class SlowClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_closes_client_that_cannot_keep_up(self):
        from app.marketdata.realtime.ws_session import RtSession

        class StuckWs:
            closed = None

            async def send_text(self, s):
                await asyncio.sleep(3600)

            async def close(self, code=1000, reason=""):
                self.closed = (code, reason)

        m = RealtimeManager(fast_cfg(send_timeout_sec=0.05), bases=["BTC"])
        ws = StuckWs()
        s = RtSession(ws, m, "ip:1")
        self.assertIsNone(s.admit())
        m.bus.add(s.sub, ("ticker", "BTC"))
        task = asyncio.create_task(s.sender())
        for i in range(1000):
            m.bus.publish(("ticker", "BTC"), f"f{i}")
        await asyncio.wait_for(task, 2)
        self.assertEqual(ws.closed, (1013, "SLOW_CLIENT"))
        self.assertEqual(m.slow_closed, 1)
        self.assertLessEqual(len(s.sub.pending), 1)  # coalesced, never a growing queue
        s.close()
        self.assertEqual(m.clients, 0)


class SwitchOffWsTests(unittest.TestCase):
    def test_ws_unchanged_when_switch_off(self):
        from fastapi.testclient import TestClient

        from app.main import create_app

        RT.set_manager(None)
        with patch.dict(os.environ, {"AUU_AUTH": "off", "AUU_SHIELD": "off"}):
            os.environ.pop("AUU_RT_WS", None)
            client = TestClient(create_app(legacy=False))
            with client.websocket_connect("/api/v1/ws") as ws:
                hello = ws.receive_json()
                self.assertEqual(hello, {"type": "hello", "version": 1, "providers": ["cex_public"], "orderMode": "paper",
                                         "venue": "CEX", "marketData": "real", "legacyPump": False,
                                         "eventTypes": ["signal", "risk", "fill", "reject", "trading_state"]})
                ws.send_text(json.dumps({"type": "subscribe", "channel": "ticker", "symbol": "BTC"}))
                self.assertEqual(ws.receive_json()["payload"]["code"], "LEGACY_OFF")
            r = client.get("/api/v1/mainstream/realtime", headers={"user-agent": "Mozilla/5.0"})
            self.assertEqual(r.json()["data"], {"enabled": False})


if __name__ == "__main__":
    unittest.main()
