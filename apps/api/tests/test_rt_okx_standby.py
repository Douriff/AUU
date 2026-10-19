"""OKX standby ("省流") mode: OKX only fully subscribed while Binance needs a backup (mock servers, loopback only).

Covers: config default/override, the warm /public probe connection, lazy /business, the manager's wake triggers
(disconnect, stale, suspect, cross-check strike, REST preference), recovery after the calm period, flap guard,
``AUU_RT_OKX_MODE=full`` = old behaviour, and an end-to-end failover Binance -> OKX -> Binance with an event-loop
stall probe.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

from app.marketdata.realtime.bus import FeedState, MarketBus, Subscriber
from app.marketdata.realtime.config import load_rt_config
from app.marketdata.realtime.feeds import OkxCandleFeed, OkxTickerFeed, build_feeds, okx_standby_applies
from app.marketdata.realtime.manager import RealtimeManager
from tests.rt_mock_exchange import MockExchange
from tests.test_mainstream_data import NOW
from tests.test_realtime_ws import _Loopback, _Sink, _until, fast_cfg

BOTH = ("binance", "okx")


class StandbyConfigTests(unittest.TestCase):
    def test_default_is_standby_and_full_restores_old_behaviour(self):
        with patch.dict(os.environ, {"AUU_RT_WS": "on"}, clear=False):
            for k in ("AUU_RT_OKX_MODE", "AUU_RT_OKX_PROBE", "AUU_RT_OKX_CALM_SEC", "AUU_RT_OKX_WAKE_SEC"):
                os.environ.pop(k, None)
            cfg = load_rt_config()
            self.assertEqual(cfg.okx_mode, "standby")
            self.assertEqual(cfg.okx_probe, ("funding-rate", "BTC-USDT-SWAP"))
            self.assertEqual((cfg.okx_wake_ms, cfg.okx_calm_sec), (3000, 300.0))
            self.assertTrue(okx_standby_applies(cfg))
        env = {"AUU_RT_WS": "on", "AUU_RT_OKX_MODE": "FULL", "AUU_RT_OKX_CALM_SEC": "1", "AUU_RT_OKX_PROBE": "status:x"}
        with patch.dict(os.environ, env):
            cfg = load_rt_config()
            self.assertEqual(cfg.okx_mode, "full")
            self.assertEqual(cfg.okx_calm_sec, 10.0)  # clamped
            self.assertEqual(cfg.okx_probe, ("status", "X"))
            self.assertFalse(okx_standby_applies(cfg))
        with patch.dict(os.environ, {"AUU_RT_OKX_MODE": "bogus"}):
            self.assertEqual(load_rt_config().okx_mode, "standby")

    def test_okx_alone_always_runs_full(self):
        bus = MarketBus()
        cfg = fast_cfg(exchanges=("okx",))
        self.assertFalse(okx_standby_applies(cfg))
        feeds = build_feeds(cfg, ["BTC"], "USDT", bus, _Sink(bus), connect=object())
        self.assertTrue(all(f.active and not f.lazy for f in feeds))
        m = RealtimeManager(cfg, bases=["BTC"], feed_kw={"connect": object()})
        self.assertFalse(m.okx_ctl)
        self.assertEqual(m.okx_view(NOW), {"mode": "full", "active": True})

    def test_standby_feeds_start_parked(self):
        bus = MarketBus()
        feeds = {f.feed_id: f for f in build_feeds(fast_cfg(exchanges=BOTH), ["BTC"], "USDT", bus, _Sink(bus), connect=object())}
        self.assertTrue(feeds["binance"].active)
        self.assertFalse(feeds["okx:tickers"].active or feeds["okx:tickers"].lazy)  # warm, not lazy
        self.assertTrue(feeds["okx:candles"].lazy and not feeds["okx:candles"].active)
        self.assertEqual(feeds["okx:tickers"].args(), [{"channel": "funding-rate", "instId": "BTC-USDT-SWAP"}])


class StandbyFeedTests(_Loopback):
    async def test_warm_public_connection_switches_tickers_on_live_connection(self):
        mock = await MockExchange("okx").start()
        bus = MarketBus(stale_ms=1000)
        sink = _Sink(bus)
        cfg = fast_cfg(exchanges=BOTH, okx_public_url=mock.url, idle_sec=0.2)
        feed = OkxTickerFeed(cfg, ["BTC", "ETH"], "USDT", bus, sink)
        task = asyncio.create_task(feed.run())
        try:
            self.assertTrue(await _until(lambda: ("funding-rate", "BTC-USDT-SWAP") in mock.subscribed_now()))
            self.assertEqual(mock.subscribed_now(), {("funding-rate", "BTC-USDT-SWAP")})
            # probe push = liveness only, never market data / freshness
            await mock.push("funding-rate", "BTC-USDT-SWAP", mock.okx_funding())
            self.assertTrue(await _until(lambda: feed.state.probe_ms > 0))
            self.assertFalse(bus.fresh("okx", "ticker"))
            self.assertEqual(sink.tickers, [])
            # tickers that leak to a standby connection are ignored by the server anyway (push() filters)
            self.assertEqual(await mock.push("tickers", "BTC-USDT", mock.okx_ticker("BTC", 1.0)), 0)
            # quiet: ping/pong keeps the warm connection
            self.assertTrue(await _until(lambda: mock.pings >= 2, timeout=3))
            self.assertEqual(feed.state.connects, 1)
            # wake: one subscribe op on the same connection, no reconnect
            await feed.set_active(True)
            self.assertTrue(await _until(lambda: ("tickers", "ETH-USDT") in mock.subscribed_now()))
            await mock.push("tickers", "BTC-USDT", mock.okx_ticker("BTC", 50.0))
            await mock.push("tickers", "ETH-USDT", mock.okx_ticker("ETH", 5.0))
            self.assertTrue(await _until(lambda: bus.ticker_snapshot("okx", ["BTC", "ETH"]) is not None))
            self.assertEqual(feed.state.connects, 1)
            # back to standby: unsubscribe tickers, probe stays
            await feed.set_active(False)
            self.assertTrue(await _until(lambda: mock.subscribed_now() == {("funding-rate", "BTC-USDT-SWAP")}))
            self.assertEqual(sorted(a["instId"] for a in mock.unsubscribed), ["BTC-USDT", "ETH-USDT"])
            # reconnect while active re-subscribes everything in one op
            await feed.set_active(True)
            await mock.drop_all()
            self.assertTrue(await _until(lambda: feed.state.connects == 2 and len(mock.subscribed_now()) == 3, timeout=3))
            # old (pre-standby) ticker rows no longer count after a re-activation until reconfirmed
            self.assertIsNone(bus.ticker_snapshot("okx", ["BTC", "ETH"]))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mock.stop()

    async def test_lazy_business_connection_only_while_active(self):
        mock = await MockExchange("okx").start()
        bus = MarketBus(stale_ms=1000)
        cfg = fast_cfg(exchanges=BOTH, okx_business_url=mock.url, tfs=("1h",))
        feed = OkxCandleFeed(cfg, ["BTC"], "USDT", bus, _Sink(bus))
        task = asyncio.create_task(feed.run())
        try:
            await asyncio.sleep(0.3)
            self.assertEqual(mock.attempts, 0)  # standby: no connection at all
            await feed.set_active(True)
            self.assertTrue(await _until(lambda: ("candle1H", "BTC-USDT") in mock.subscribed_now()))
            await feed.set_active(False)
            self.assertTrue(await _until(lambda: not mock.conns and not feed.state.connected))
            await asyncio.sleep(0.2)
            self.assertEqual((mock.attempts, feed.delays, feed.state.last_error), (1, [], None))  # no backoff, no error
            await feed.set_active(True)
            self.assertTrue(await _until(lambda: mock.attempts == 2 and mock.conns))
        finally:
            feed.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mock.stop()

    async def test_full_mode_subscribes_everything_like_before(self):
        mock = await MockExchange("okx").start()
        bus = MarketBus(stale_ms=1000)
        cfg = fast_cfg(exchanges=BOTH, okx_mode="full", okx_public_url=mock.url, okx_business_url=mock.url)
        feeds = [OkxTickerFeed(cfg, ["BTC"], "USDT", bus, _Sink(bus)), OkxCandleFeed(cfg, ["BTC"], "USDT", bus, _Sink(bus))]
        tasks = [asyncio.create_task(f.run()) for f in feeds]
        try:
            self.assertTrue(await _until(lambda: len(mock.subscribed) >= 4))
            self.assertEqual(sorted(a["channel"] for a in mock.subscribed),
                             sorted(["tickers", "candle1H", "candle4H", "candle1Dutc"]))  # no probe
            self.assertTrue(all(f.active and f.state.mode is None for f in feeds))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await mock.stop()


class OpsBudgetTests(_Loopback):
    async def test_subscribe_ops_capped_per_connection(self):
        mock = await MockExchange("okx").start()
        bus = MarketBus(stale_ms=1000)
        cfg = fast_cfg(exchanges=BOTH, okx_public_url=mock.url, okx_ops_per_hour=3)
        feed = OkxTickerFeed(cfg, ["BTC"], "USDT", bus, _Sink(bus))
        task = asyncio.create_task(feed.run())
        try:
            self.assertTrue(await _until(lambda: mock.subscribed_now()))  # op 1 (probe)
            await feed.set_active(True)  # op 2
            await feed.set_active(False)  # op 3
            await feed.set_active(True)  # refused -> reconnect: the limit is per connection, a new one subscribes all
            self.assertTrue(await _until(lambda: feed.state.connects == 2 and ("tickers", "BTC-USDT") in mock.subscribed_now(),
                                         timeout=3))
            self.assertEqual(feed.ops_refused, 1)
            await feed.set_active(False)  # a refused unsubscribe just keeps tickers (never loses data)
            await feed.set_active(True)
            await feed.set_active(False)
            await asyncio.sleep(0.1)
            self.assertEqual(feed.ops_refused, 2)
            self.assertIn(("tickers", "BTC-USDT"), mock.subscribed_now())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await mock.stop()


class _Rec:
    """Stand-in for a feed: records set_active calls."""

    def __init__(self, fid, ex):
        self.feed_id, self.ex, self.calls, self.on_down = fid, ex, [], None
        self.state = FeedState(fid, ex, ())

    async def set_active(self, on):
        self.calls.append(on)


class ControllerTests(_Loopback):
    def _mgr(self, **kw):
        t = {"now": NOW}
        pref = {"ex": "binance"}
        cfg = fast_cfg(exchanges=BOTH, stale_ms=10_000, okx_wake_ms=3000, okx_calm_sec=300, okx_calm_max_sec=3600,
                       okx_flap_window_sec=1800, okx_boot_grace_sec=20, **kw)
        m = RealtimeManager(cfg, bases=["BTC"], preferred=lambda: pref["ex"], clock=lambda: t["now"],
                            feed_kw={"connect": object()})
        m.feeds = [f for f in m.feeds if f.ex == "binance"] + [_Rec("okx:tickers", "okx"), _Rec("okx:candles", "okx")]
        m.started_at = NOW
        b = m.bus.feed("binance", "binance", ("ticker", "kline"))
        return m, t, pref, b

    @staticmethod
    def _healthy(b, now):
        b.connected, b.last_data_ms = True, now

    def _calls(self, m):
        return [f.calls for f in m.feeds if f.ex == "okx"]

    async def _tick(self, m):
        m.okx_control()
        await asyncio.sleep(0)  # let the set_active tasks run

    async def test_boot_grace_then_wake_when_binance_never_comes(self):
        m, t, _, b = self._mgr()
        self.assertTrue(m.okx_ctl)
        self.assertFalse(m.okx_active)
        t["now"] += 10_000
        await self._tick(m)
        self.assertFalse(m.okx_active)  # inside the grace period
        t["now"] += 11_000
        await self._tick(m)
        self.assertTrue(m.okx_active)
        self.assertEqual(m.okx_reason, "binance-disconnected")
        self.assertEqual(self._calls(m), [[True], [True]])

    async def test_each_wake_trigger(self):
        cases = {
            "binance-disconnected": lambda m, b, t, p: setattr(b, "connected", False),
            "binance-stale": lambda m, b, t, p: setattr(b, "last_data_ms", t["now"] - 3001),
            "binance-suspect": lambda m, b, t, p: setattr(b, "suspect_until_ms", t["now"] + 1),
            "binance-xcheck-strike": lambda m, b, t, p: m._strikes.__setitem__("binance", 1),
            "rest-prefers-okx": lambda m, b, t, p: p.__setitem__("ex", "okx"),
        }
        for want, breakit in cases.items():
            with self.subTest(want):
                m, t, p, b = self._mgr()
                self._healthy(b, t["now"])
                await self._tick(m)
                self.assertFalse(m.okx_active)
                t["now"] += 500
                b.last_data_ms = t["now"] - 2900  # still within okx_wake_ms
                await self._tick(m)
                self.assertFalse(m.okx_active)
                breakit(m, b, t, p)
                await self._tick(m)
                self.assertTrue(m.okx_active)
                self.assertEqual(m.okx_reason, want)

    async def test_recovery_after_calm_period_and_calm_resets_on_relapse(self):
        m, t, _, b = self._mgr()
        b.connected = False
        b.last_data_ms = NOW
        await self._tick(m)
        self.assertTrue(m.okx_active)
        # Binance back; OKX stays hot for 300 s of continuous health
        for _ in range(299):
            t["now"] += 1000
            self._healthy(b, t["now"])
            await self._tick(m)
        self.assertTrue(m.okx_active)
        self.assertEqual(m.okx_view(t["now"])["calmLeftSec"], 2)  # calm starts at the first healthy pass
        b.last_data_ms = t["now"] - 4000  # one stale blip resets the calm timer
        await self._tick(m)
        self.assertTrue(m.okx_active)
        self.assertEqual(m.okx_activations, 1)  # still the same activation (no toggling)
        for _ in range(300):
            t["now"] += 1000
            self._healthy(b, t["now"])
            await self._tick(m)
        self.assertTrue(m.okx_active)
        t["now"] += 1000
        self._healthy(b, t["now"])
        await self._tick(m)
        self.assertFalse(m.okx_active)
        self.assertIn("healthy for 300s", m.okx_reason)
        self.assertEqual(self._calls(m), [[True, False], [True, False]])

    async def test_flap_guard_doubles_calm_and_caps(self):
        m, t, _, b = self._mgr()
        self._healthy(b, t["now"])
        calms = []
        for _ in range(6):
            b.connected = False
            await self._tick(m)
            self.assertTrue(m.okx_active)
            calms.append(m.okx_calm_ms() // 1000)
            n = 0
            while m.okx_active:
                t["now"] += 1000
                self._healthy(b, t["now"])
                await self._tick(m)
                n += 1
                self.assertLess(n, 4000)
        # window is 1800 s: wakes older than that stop counting, so the calm grows then is capped/decays
        self.assertEqual(calms[:3], [300, 600, 1200])
        self.assertLessEqual(max(calms), 3600)
        # long quiet -> back to the base calm period
        t["now"] += 2 * 3600 * 1000
        self.assertEqual(m.okx_calm_ms() // 1000, 300)
        self.assertEqual(m.okx_activations, 6)

    async def test_planned_rotation_does_not_wake_unless_stale(self):
        m, t, _, b = self._mgr()
        self._healthy(b, t["now"])
        b.connected, b.rotating = False, True
        t["now"] += 1000
        await self._tick(m)
        self.assertFalse(m.okx_active)
        t["now"] += 2500  # reconnect did not deliver within okx_wake_ms
        await self._tick(m)
        self.assertEqual((m.okx_active, m.okx_reason), (True, "binance-stale"))

    async def test_disconnect_kicks_selector_immediately(self):
        m, t, _, b = self._mgr()
        m._kick = asyncio.Event()
        m.feeds[0].on_down("binance")
        self.assertTrue(m._kick.is_set())

    async def test_full_mode_has_no_controller(self):
        m = RealtimeManager(fast_cfg(exchanges=BOTH, okx_mode="full"), bases=["BTC"], feed_kw={"connect": object()})
        self.assertFalse(m.okx_ctl)
        self.assertTrue(m.okx_active)
        self.assertTrue(all(f.on_down is None for f in m.feeds))
        m.okx_control()  # no-op
        self.assertTrue(all(f.active for f in m.feeds))


class FailoverE2ETests(_Loopback):
    async def test_binance_down_okx_wakes_takes_over_then_returns_to_standby(self):
        bmock = await MockExchange("binance").start()
        omock = await MockExchange("okx").start()
        cfg = fast_cfg(exchanges=BOTH, binance_urls=(bmock.url,), okx_public_url=omock.url, okx_business_url=omock.url,
                       tfs=("1h",), stale_ms=1500, idle_sec=1.0, okx_wake_ms=600, okx_calm_sec=0.8,
                       okx_boot_grace_sec=5, backoff_base_sec=0.3, backoff_max_sec=0.3)
        m = RealtimeManager(cfg, bases=["BTC"], preferred=lambda: "binance")
        sub = Subscriber()
        m.bus.add(sub, ("status",))
        run = asyncio.create_task(m.run())
        stop = asyncio.Event()
        lag = {"max": 0.0}
        srcs: list = []

        async def collect():  # what a /api/v1/ws client sees as rt_status
            while not stop.is_set():
                await sub.event.wait()
                sub.event.clear()
                srcs.extend(json.loads(f)["payload"]["source"] for f in sub.drain())

        async def pump():  # Binance + OKX publish every 50 ms (OKX only to subscribed connections)
            i = 0
            while not stop.is_set():
                i += 1
                await bmock.broadcast(bmock.binance_ticker("BTC", 100.0 + i * 1e-3))
                await omock.push("tickers", "BTC-USDT", omock.okx_ticker("BTC", 100.5))
                if i % 10 == 0:
                    await omock.push("funding-rate", "BTC-USDT-SWAP", omock.okx_funding())
                await asyncio.sleep(0.05)

        async def probe():
            while not stop.is_set():
                t0 = time.perf_counter()
                await asyncio.sleep(0.005)
                lag["max"] = max(lag["max"], time.perf_counter() - t0 - 0.005)

        bg = [asyncio.create_task(pump()), asyncio.create_task(probe()), asyncio.create_task(collect())]
        try:
            self.assertTrue(await _until(lambda: m.selected == "binance", timeout=5))
            await asyncio.sleep(0.3)
            self.assertEqual(omock.subscribed_now(), {("funding-rate", "BTC-USDT-SWAP")})  # standby: probe only
            self.assertEqual(len(omock.conns), 1)  # /business not connected
            self.assertFalse(m.okx_active)
            # Binance goes away (refuses reconnects for a while)
            bmock.refuse = 1000
            t_down = time.perf_counter()
            await bmock.drop_all()
            self.assertTrue(await _until(lambda: m.selected == "okx", timeout=3))
            failover_ms = (time.perf_counter() - t_down) * 1000
            self.assertTrue(m.okx_active)
            self.assertEqual(m.okx_reason, "binance-disconnected")
            self.assertIn(("tickers", "BTC-USDT"), omock.subscribed_now())
            self.assertTrue(await _until(lambda: ("candle1H", "BTC-USDT") in omock.subscribed_now(), timeout=3))
            self.assertLess(failover_ms, 500)  # loopback: kick + one subscribe round trip, no 1 s tick wait
            # Binance back -> source returns at once, OKX stays hot for the calm period, then standby
            bmock.refuse = 0
            self.assertTrue(await _until(lambda: m.selected == "binance", timeout=3))
            self.assertTrue(m.okx_active)
            self.assertTrue(await _until(lambda: not m.okx_active, timeout=5))
            self.assertTrue(await _until(lambda: omock.subscribed_now() == {("funding-rate", "BTC-USDT-SWAP")}
                                         and len(omock.conns) == 1, timeout=3))
            st = m.status()["okx"]
            self.assertEqual((st["mode"], st["active"], st["activations"]), ("standby", False, 1))
            self.assertTrue(st["standbyHealthy"])
            self.assertEqual(srcs, ["binance", None, "okx", "binance"])
            self.assertLess(lag["max"], 0.05)  # mode switches never stall the event loop
        finally:
            stop.set()
            sub.event.set()
            await asyncio.gather(*bg, return_exceptions=True)
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            await bmock.stop()
            await omock.stop()


if __name__ == "__main__":
    unittest.main()
