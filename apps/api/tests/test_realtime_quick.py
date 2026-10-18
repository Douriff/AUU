"""Real-time quick wins (step 0): Binance host config + failover, shared ticker cache (TTL sets the exchange load),
10 s forming-bar tail for 1h/4h/1d without sqlite writes, live price overlay (header == last candle), price-alert
cadence/freshness. Everything is mocked: sockets are blocked, nothing hits the network."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.marketdata.mainstream import fetcher as F
from app.marketdata.mainstream.config import DEFAULT_BINANCE_HOSTS, TF_MS, binance_hosts, load_config
from tests.test_mainstream_data import H, NOW, FakeExchange, NetworkError, _cfg, _NoNetwork
from tests.test_mainstream_markets import COINS, TickerExchange

REPO = Path(__file__).resolve().parents[3]


# ---- 1. Binance host config + failover ---------------------------------------------------------------
class RateLimitExceeded(Exception):  # same class name as ccxt's (never fails over)
    pass


class ExchangeNotAvailable(Exception):  # same class name as ccxt's (403/451/5xx)
    pass


class HostConfigTests(unittest.TestCase):
    def test_default_order_prefers_alt_gateways_and_keeps_api_binance_last(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUU_BINANCE_REST_HOSTS", None)
            self.assertEqual(binance_hosts(), list(DEFAULT_BINANCE_HOSTS))
        self.assertEqual(DEFAULT_BINANCE_HOSTS[0], "api1.binance.com")
        self.assertEqual(DEFAULT_BINANCE_HOSTS[-1], "api.binance.com")
        self.assertIn("api3.binance.com", DEFAULT_BINANCE_HOSTS)
        self.assertIn("data-api.binance.vision", DEFAULT_BINANCE_HOSTS)

    def test_standby_default_keeps_api_binance_first_unless_overridden(self):
        from app.marketdata.mainstream.config import STANDBY_BINANCE_HOSTS

        with patch.dict(os.environ, {"AUU_ROLE": "standby"}):
            os.environ.pop("AUU_BINANCE_REST_HOSTS", None)
            self.assertEqual(binance_hosts(), list(STANDBY_BINANCE_HOSTS))
            self.assertEqual(binance_hosts()[0], "api.binance.com")
            os.environ["AUU_BINANCE_REST_HOSTS"] = "api3.binance.com,api.binance.com"
            self.assertEqual(binance_hosts(), ["api3.binance.com", "api.binance.com"])
        with patch.dict(os.environ, {"AUU_ROLE": "primary"}):
            os.environ.pop("AUU_BINANCE_REST_HOSTS", None)
            self.assertEqual(binance_hosts(), list(DEFAULT_BINANCE_HOSTS))

    def test_env_override_accepts_urls_dedupes_and_drops_junk(self):
        raw = "https://API3.binance.com/, api3.binance.com ,bad host!,data-api.binance.vision:443,,api.binance.com"
        with patch.dict(os.environ, {"AUU_BINANCE_REST_HOSTS": raw}):
            self.assertEqual(binance_hosts(), ["api3.binance.com", "data-api.binance.vision", "api.binance.com"])
            self.assertEqual(load_config().binance_hosts, ["api3.binance.com", "data-api.binance.vision", "api.binance.com"])
        with patch.dict(os.environ, {"AUU_BINANCE_REST_HOSTS": "!!!"}):
            self.assertEqual(binance_hosts(), list(DEFAULT_BINANCE_HOSTS))  # nothing valid: safe default
        with patch.dict(os.environ, {"AUU_BINANCE_REST_HOSTS": "api.binance.com"}):
            self.assertEqual(binance_hosts(), ["api.binance.com"])  # old behaviour, no fallback


class FakeClient:
    """ccxt-like client: ``fetch(url)`` is the only network hook; scripted per host."""

    def __init__(self, script):
        self.script = script  # host -> callable(url) -> result / raises
        self.urls: list[str] = []

    def fetch(self, url, method="GET", headers=None, body=None):
        self.urls.append(url)
        host = url.split("/")[2]
        return self.script[host](url)


class HostFailoverTests(unittest.TestCase):
    def _client(self, script, hosts=("api1.binance.com", "api3.binance.com", "api.binance.com"), clock=None):
        pool = F.HostPool(list(hosts), clock=clock or time.monotonic)
        c = FakeClient(script)
        F.install_host_failover(c, pool)
        return c, pool

    def test_spot_api_calls_use_the_first_host_others_untouched(self):
        ok = lambda u: "ok"
        c, pool = self._client({"api1.binance.com": ok, "fapi.binance.com": ok, "api.binance.com": ok})
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/ticker/24hr?x=1"), "ok")
        self.assertEqual(c.fetch("https://fapi.binance.com/fapi/v1/fundingRate"), "ok")
        self.assertEqual(c.fetch("https://api.binance.com/sapi/v1/capital/config"), "ok")  # keyed endpoint family: not rewritten
        self.assertEqual(c.urls, ["https://api1.binance.com/api/v3/ticker/24hr?x=1", "https://fapi.binance.com/fapi/v1/fundingRate",
                                  "https://api.binance.com/sapi/v1/capital/config"])

    def test_network_error_falls_back_to_next_host_and_sticks(self):
        def dead(u):
            raise NetworkError("RequestTimeout: read timed out")
        c, pool = self._client({"api1.binance.com": dead, "api3.binance.com": lambda u: "ok3", "api.binance.com": lambda u: "ok0"})
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/klines"), "ok3")
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/klines"), "ok3")  # sticky: api1 is not retried every call
        self.assertEqual([u.split("/")[2] for u in c.urls], ["api1.binance.com", "api3.binance.com", "api3.binance.com"])
        self.assertEqual(pool.switches, 1)

    def test_rate_limit_and_bad_symbol_never_fail_over(self):
        def limited(u):
            raise RateLimitExceeded("binance 429 Too Many Requests")
        c, pool = self._client({"api1.binance.com": limited, "api3.binance.com": lambda u: "ok", "api.binance.com": lambda u: "ok"})
        with self.assertRaises(RateLimitExceeded):
            c.fetch("https://api.binance.com/api/v3/ticker/24hr")
        self.assertEqual(len(c.urls), 1)  # the weight budget is per IP: another gateway cannot help
        self.assertEqual(pool.switches, 0)

    def test_a_gateway_that_answers_403_or_451_does_not_make_binance_look_blocked(self):
        def forbidden(u):
            raise ExchangeNotAvailable("binance GET https://x 403 Forbidden cloudfront")
        c, pool = self._client({"api1.binance.com": forbidden, "api3.binance.com": lambda u: "ok", "api.binance.com": lambda u: "ok"})
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/exchangeInfo"), "ok")

    def test_all_gateways_blocked_still_raises_so_okx_fallback_can_trigger(self):
        def blocked(u):
            raise ExchangeNotAvailable("binance GET https://x 451 Service unavailable from a restricted location")
        c, pool = self._client({h: blocked for h in ("api1.binance.com", "api3.binance.com", "api.binance.com")})
        with self.assertRaises(ExchangeNotAvailable) as cm:
            c.fetch("https://api.binance.com/api/v3/exchangeInfo")
        self.assertEqual(len(c.urls), 3)  # each gateway tried once per call
        fe = F.classify(cm.exception)
        self.assertTrue(fe.blocked and fe.fatal)  # unchanged classification -> service switches to okx

    def test_preferred_host_is_probed_again_after_the_retry_window(self):
        t = {"now": 1000.0}
        state = {"api1_up": False}

        def api1(u):
            if not state["api1_up"]:
                raise NetworkError("RequestTimeout")
            return "ok1"
        c, pool = self._client({"api1.binance.com": api1, "api3.binance.com": lambda u: "ok3", "api.binance.com": lambda u: "ok0"}, clock=lambda: t["now"])
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/x"), "ok3")
        t["now"] += F.HOST_PRIMARY_RETRY_SEC - 1
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/x"), "ok3")
        state["api1_up"] = True
        t["now"] += 2
        self.assertEqual(c.fetch("https://api.binance.com/api/v3/x"), "ok1")  # back on the preferred host

    def test_pool_is_shared_between_clients_and_a_single_host_never_rotates(self):
        pool = F.HostPool(["api.binance.com"])
        pool.fail("api.binance.com")
        self.assertEqual(pool.current(), "api.binance.com")
        self.assertIs(F.host_pool(["a.binance.com", "b.binance.com"]), F.host_pool(["a.binance.com", "b.binance.com"]))

    def test_make_exchange_installs_failover_for_binance_only_keyless(self):
        cfg = _cfg(binance_hosts=["api1.binance.com", "api.binance.com"])
        b = F.make_exchange("binance", cfg)
        self.assertTrue(hasattr(b, "_auu_host_pool"))
        self.assertFalse(b.apiKey)
        self.assertFalse(b.secret)
        o = F.make_exchange("okx", cfg)
        self.assertFalse(hasattr(o, "_auu_host_pool"))  # OKX path untouched

    def test_fetcher_once_is_a_single_classified_attempt(self):
        class C:
            n = 0

            def fetch_ohlcv(self, *a, **k):
                C.n += 1
                raise NetworkError("RequestTimeout")
        f = F.Fetcher("binance", C(), _cfg(), sleep=lambda s: self.fail("must not sleep"))
        with self.assertRaises(F.FetchError):
            f.once("fetch_ohlcv", "BTC/USDT", "1h")
        self.assertEqual(C.n, 1)


# ---- 2. ticker cache: TTL alone sets the exchange load ---------------------------------------------------
class TickerCadenceTests(_NoNetwork):
    def _svc(self, ex, now=None):
        t = {"now": NOW if now is None else now}
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC", "ETH", "SOL"], strategy_symbols=COINS))
        svc.now_ms = lambda: t["now"]
        svc.refresh_once()
        return svc, t

    def _wait(self, svc):
        for _ in range(300):
            if not svc._tk["busy"]:
                return
            time.sleep(0.01)

    def _n(self, ex):
        return len([c for c in ex.calls if c[0] == "fetch_tickers"])

    def test_ttl_default_3s_floor_2s_ceiling_600s(self):
        svc, _ = self._svc(TickerExchange())
        with patch.dict(os.environ, {}):
            os.environ.pop("AUU_MAINSTREAM_TICKER_SEC", None)
            self.assertEqual(svc._ticker_ttl_ms(), 3_000)
        for raw, want in (("1", 2_000), ("0", 2_000), ("5", 5_000), ("9999", 600_000), ("x", 3_000), ("30", 30_000)):
            with patch.dict(os.environ, {"AUU_MAINSTREAM_TICKER_SEC": raw}):
                self.assertEqual(svc._ticker_ttl_ms(), want, raw)

    def test_many_callers_one_request_per_ttl(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        for _ in range(200):  # 200 page polls / alert ticks inside the TTL
            svc.markets()
        self.assertEqual(self._n(ex), 1)
        for step in range(10):  # 30 s of wall clock with a request every 1 s from many users
            t["now"] += 1_000
            for _ in range(50):
                svc.markets()
            self._wait(svc)
        self.assertEqual(self._n(ex), 1 + 10 // 3)  # one fetch per 3 s TTL, independent of the 500 calls

    def test_price_carries_fetch_time_and_overlay_expires_into_store(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        row = svc.markets()["items"][0]
        self.assertEqual((row["priceSource"], row["priceAt"]), ("ticker", NOW))
        t["now"] += svc._ticker_fresh_ms() + 1
        svc._tk["try_at"] = t["now"] + 10**9  # the refresh is held off: the cache just gets too old
        row = svc.markets()["items"][0]
        self.assertEqual(row["priceSource"], "store")
        self.assertNotIn("priceAt", row)

    def test_sync_mode_refreshes_inline_and_waits_for_a_running_refresh(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        out = svc.markets(sync=True)  # first ever call: fetched before returning
        self.assertEqual(out["tickers"]["source"], "ticker")
        self.assertEqual(self._n(ex), 1)
        svc.markets(sync=True)
        self.assertEqual(self._n(ex), 1)  # inside the TTL: no second request
        t["now"] += 3_100
        gate = threading.Event()
        orig = ex.fetch_tickers

        def slow(symbols=None):
            gate.wait(2)
            return orig(symbols)
        ex.fetch_tickers = slow
        threading.Thread(target=svc.markets, daemon=True).start()  # a web request starts the background refresh
        for _ in range(100):
            if svc._tk["busy"]:
                break
            time.sleep(0.01)
        threading.Timer(0.1, gate.set).start()
        out = svc.markets(sync=True)  # the alert checker waits for it instead of reading the old cache
        self.assertEqual(self._n(ex), 2)
        self.assertEqual(out["tickers"]["at"], t["now"])

    def test_request_that_starts_a_refresh_gets_that_fresh_price_but_waits_at_most_a_moment(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        out = svc.markets()  # no explicit wait: the refresh it started is already in
        self.assertEqual((out["tickers"]["source"], out["tickers"]["at"]), ("ticker", NOW))
        t["now"] += 3_100
        gate = threading.Event()
        orig = ex.fetch_tickers
        ex.fetch_tickers = lambda symbols=None: (gate.wait(3), orig(symbols))[1]  # a hung exchange
        svc.TICKER_WAIT_S = 0.2
        t0 = time.time()
        out = svc.markets()
        self.assertLess(time.time() - t0, 1.5)  # bounded wait, then the cached rows
        self.assertEqual(out["tickers"]["at"], NOW)
        t1 = time.time()
        svc.markets()
        self.assertLess(time.time() - t1, 0.15)  # others do not wait behind the in-flight refresh
        gate.set()
        self._wait(svc)

    def test_failure_backoff_short_and_longer_for_rate_limits(self):
        ex = TickerExchange(ticker_fail=True)
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        self.assertEqual(svc._tk["try_at"], NOW + svc.TICKER_BACKOFF_MS)
        self.assertLess(svc.TICKER_BACKOFF_MS, 60_000)  # was 5 minutes: one blip no longer costs 5 min of stale prices

        class Limited(TickerExchange):
            def fetch_tickers(self, symbols=None):
                raise RateLimitExceeded("binance 429 Too Many Requests")
        svc2, _ = self._svc(Limited())
        svc2.markets()
        self._wait(svc2)
        self.assertEqual(svc2._tk["try_at"], NOW + svc2.TICKER_RATELIMIT_BACKOFF_MS)


# ---- 3. forming-bar tail (1h/4h/1d) + live overlay -----------------------------------------------------
class TailAndOverlayTests(_NoNetwork):
    def _svc(self, ex):
        t = {"now": NOW}
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC", "ETH", "SOL"], strategy_symbols=COINS))
        svc.now_ms = lambda: t["now"]
        svc.refresh_once()
        return svc, t

    def _ohlcv_calls(self, ex, tf=None):
        return [c for c in ex.calls if c[0] == "fetch_ohlcv" and (tf is None or c[1][1] == tf)]

    def _wait(self, svc):
        for _ in range(300):
            if not svc._tk["busy"]:
                return
            time.sleep(0.01)

    def _tail_exchange(self):
        class Ex(TickerExchange):
            def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
                rows = super().fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
                if limit == 2:  # the tail request: a fresher forming bar than the one the 5-min loop stored
                    rows = [list(r) for r in rows]
                    rows[-1][4] = 777.0
                    rows[-1][2] = 800.0
                return rows
        return Ex()

    def test_tail_is_incremental_throttled_and_never_written_to_sqlite(self):
        ex = self._tail_exchange()
        with patch.dict(os.environ, {"AUU_MAINSTREAM_TICKERS": "off"}):
            svc, t = self._svc(ex)
            n_rows = {tf: self.store.candle_bounds("binance", "BTC", tf)[2] for tf in ("1h", "4h", "1d")}
            before = len(self._ohlcv_calls(ex))
            out = svc.chart_candles("BTC", "1h", limit=5)
            calls = self._ohlcv_calls(ex)[before:]
            self.assertEqual(len(calls), 1)
            _, args, kw = calls[0]
            self.assertEqual((kw["limit"], kw["since"]), (2, NOW // H * H - H))  # previous + forming bar only
            self.assertEqual(out["candles"][-1]["close"], 777.0)  # fresher than the store
            self.assertEqual(out["tailAt"], NOW)
            for _ in range(20):  # a chart polling at 1 Hz inside the 10 s window
                svc.chart_candles("BTC", "1h", limit=5)
            self.assertEqual(len(self._ohlcv_calls(ex)) - before, 1)
            t["now"] += 10_000
            svc.chart_candles("BTC", "1h", limit=5)
            self.assertEqual(len(self._ohlcv_calls(ex)) - before, 2)
            for tf in ("4h", "1d"):
                svc.chart_candles("BTC", tf, limit=5)
            # sqlite: the store row is untouched (the 5-min loop owns every write)
            stored = self.store.candles("binance", "BTC", "1h", limit=1)[0]
            self.assertNotEqual(stored["close"], 777.0)
            self.assertEqual({tf: self.store.candle_bounds("binance", "BTC", tf)[2] for tf in ("1h", "4h", "1d")}, n_rows)

    def test_tail_failure_serves_the_store_without_raising_and_retries_next_interval(self):
        ex = self._tail_exchange()
        with patch.dict(os.environ, {"AUU_MAINSTREAM_TICKERS": "off"}):
            svc, t = self._svc(ex)
            ex.fail_times = 99
            sleeps = len(self.sleeps)
            out = svc.chart_candles("BTC", "1h", limit=5)
            self.assertEqual(len(self.sleeps), sleeps)  # one attempt, no retry sleeps on the request path
            self.assertTrue(out["candles"])
            self.assertNotIn("tailAt", out)
            t["now"] += 10_000
            ex.fail_times = 0
            self.assertEqual(svc.chart_candles("BTC", "1h", limit=5)["candles"][-1]["close"], 777.0)

    def test_old_pages_and_strategy_only_4h_do_not_fetch_a_tail(self):
        ex = self._tail_exchange()
        with patch.dict(os.environ, {"AUU_MAINSTREAM_TICKERS": "off"}):
            svc, _ = self._svc(ex)
            before = len(self._ohlcv_calls(ex))
            svc.chart_candles("BTC", "1h", limit=5, before=NOW - 10 * H)  # scrolling into history
            svc.chart_candles("XRP", "4h", limit=5)  # derived from stored 1h
            self.assertEqual(len(self._ohlcv_calls(ex)) - before, 0)

    def test_overlay_makes_last_candle_equal_header_price(self):
        ex = TickerExchange()  # ticker last = 200.0
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        header = next(i for i in svc.markets()["items"] if i["symbol"] == "BTC")["price"]
        for tf in ("1m", "5m", "1h", "4h", "1d"):
            out = svc.chart_candles("BTC", tf, limit=5)
            last = out["candles"][-1]
            self.assertEqual(last["close"], header, tf)
            self.assertGreaterEqual(last["high"], header)
            self.assertLessEqual(last["low"], header)
            self.assertEqual(out["priceAt"], NOW)
        # strategy-only coin: 4h derived from 1h gets the same overlay
        out = svc.chart_candles("XRP", "4h", limit=5)
        self.assertEqual(out["candles"][-1]["close"], 200.0)
        # history pages are never touched
        old = svc.chart_candles("BTC", "1h", limit=5, before=NOW - 5 * H)
        self.assertNotIn("priceAt", old)
        self.assertNotEqual(old["candles"][-1]["close"], 200.0)
        # and nothing was written to sqlite
        self.assertNotEqual(self.store.candles("binance", "BTC", "1h", limit=1)[0]["close"], 200.0)

    def test_overlay_skipped_when_ticker_is_stale_or_bar_is_closed(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        t["now"] += svc._ticker_fresh_ms() + 1_000
        svc._tk["try_at"] = t["now"] + 10**9  # no refresh
        out = svc.chart_candles("BTC", "1h", limit=5)
        self.assertNotIn("priceAt", out)
        self.assertNotEqual(out["candles"][-1]["close"], 200.0)
        # a closed (non-forming) last bar is never overwritten
        rows = [{"ts": NOW // H * H - 5 * H, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}]
        svc._tk.update(at=t["now"], ex="binance", data={"BTC": {"last": 999.0}})
        self.assertEqual(svc._live_overlay("binance", "BTC", "1h", rows, None), {})
        self.assertEqual(rows[-1]["close"], 1.5)


# ---- 4. price alerts: cadence, freshness, fallback, mail pacing ---------------------------------------------
class AlertCadenceTests(unittest.TestCase):
    def setUp(self):
        from app import price_alerts as pa
        self.pa = pa
        self.tmp = tempfile.TemporaryDirectory()
        self.store = pa.PriceAlertStore(Path(self.tmp.name) / "pa.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_constants(self):
        self.assertEqual(self.pa.CHECK_INTERVAL_SEC, 5)
        self.assertEqual(self.pa.MAX_PRICE_AGE_MS, 15_000)
        self.assertEqual(self.pa.MAIL_PER_TICK, 5)  # unchanged
        self.assertEqual(self.pa.MAIL_PER_USER_HOUR, 30)  # unchanged

    def test_fresh_prices_live_vs_store_fallback(self):
        pa = self.pa
        T = 1_800_000_000_000
        m = {"items": [
            {"symbol": "BTC", "price": 1.0, "priceSource": "ticker", "priceAt": T - 14_000, "priceTs": T - 60_000},  # fetched 14 s ago, thin-coin stat time older
            {"symbol": "ETH", "price": 2.0, "priceSource": "ticker", "priceAt": T - 16_000, "priceTs": T - 16_000},  # too old for a live feed
            {"symbol": "SOL", "price": 3.0, "priceSource": "store", "priceTs": T - 5 * 60_000},  # feed down: old rule (10 min) still applies
            {"symbol": "XRP", "price": 4.0, "priceSource": "store", "priceTs": T - 11 * 60_000},
        ]}
        self.assertEqual(pa.fresh_prices(m, T), {"BTC": 1.0, "SOL": 3.0})

    def test_checker_reads_markets_with_sync_refresh_in_production_wiring(self):
        seen = {}

        class Svc:
            def markets(self, holdings=None, *, sync=False):
                seen["sync"] = sync
                return {"exchange": "binance", "items": []}
        with patch("app.marketdata.mainstream.get_service", return_value=Svc()), patch.object(self.pa, "get_store", return_value=self.store):
            self.store.create("u1", "BTC", 100.0, "above", now_ms=1)
            self.pa.production_checker().check()
        self.assertTrue(seen["sync"])

    def test_fires_once_and_standby_never_runs_the_checker(self):
        pa = self.pa
        T = 1_800_000_000_000
        px = {"v": 99.0}
        mk = lambda: {"exchange": "binance", "items": [{"symbol": "BTC", "price": px["v"], "priceSource": "ticker", "priceAt": T, "priceTs": T}]}
        c = pa.AlertChecker(self.store, markets_fn=mk, user_fn=lambda u: None, send=lambda *a: None, configured=lambda: False, now_ms=lambda: T)
        self.store.create("u1", "BTC", 100.0, "above", now_ms=T)
        self.assertEqual(c.tick()["fired"], 0)  # records the side
        px["v"] = 101.0
        self.assertEqual(c.tick()["fired"], 1)
        self.assertEqual(c.tick()["fired"], 0)  # compare-and-set: only once
        with patch.dict(os.environ, {"AUU_ROLE": "standby"}):
            self.assertFalse(pa.price_alerts_enabled())

    def test_mail_pace_is_not_faster_with_the_5s_tick(self):
        pa = self.pa
        t = {"now": 1_800_000_000_000}
        sent: list[tuple] = []
        users = {f"u{i}": {"email": f"u{i}@example.com", "locale": "zh-CN"} for i in range(12)}
        c = pa.AlertChecker(self.store, markets_fn=lambda: {"exchange": "binance", "items": [
            {"symbol": "BTC", "price": c_px["v"], "priceSource": "ticker", "priceAt": t["now"], "priceTs": t["now"]}]},
            user_fn=lambda u: users.get(u), send=lambda to, *a: sent.append((t["now"], to)), configured=lambda: True, now_ms=lambda: t["now"])
        c_px = {"v": 99.0}
        for u in users:
            self.store.create(u, "BTC", 100.0, "above", now_ms=t["now"])
        c.tick()
        c_px["v"] = 101.0
        for _ in range(12):  # 12 ticks x 5 s = 60 s
            c.tick()
            t["now"] += pa.CHECK_INTERVAL_SEC * 1000
        per_window = [len([1 for ts, _ in sent if w <= ts < w + pa.MAIL_WINDOW_MS]) for w in range(1_800_000_000_000, 1_800_000_060_000, pa.MAIL_WINDOW_MS)]
        self.assertEqual(len(sent), 12)  # all delivered eventually...
        self.assertTrue(all(n <= pa.MAIL_PER_TICK for n in per_window), per_window)  # ...never more than 5 per 15 s

    def test_failed_mail_attempts_use_the_pacing_budget(self):
        pa = self.pa
        t = {"now": 1_800_000_000_000}
        calls = []

        def boom(*a):
            calls.append(t["now"])
            raise RuntimeError("smtp down")
        px = {"v": 99.0}
        c = pa.AlertChecker(self.store, markets_fn=lambda: {"exchange": "binance", "items": [
            {"symbol": "BTC", "price": px["v"], "priceSource": "ticker", "priceAt": t["now"], "priceTs": t["now"]}]},
            user_fn=lambda u: {"email": "a@example.com"}, send=boom, configured=lambda: True, now_ms=lambda: t["now"])
        for i in range(8):
            self.store.create("u1", "BTC", 100.0 + i, "above", now_ms=t["now"])
        c.tick()
        px["v"] = 200.0
        for _ in range(3):
            c.tick()
            t["now"] += 5_000
        self.assertLessEqual(len(calls), pa.MAIL_PER_TICK)


# ---- 5. sync / backup lists ---------------------------------------------------------------------------------
def _load_script(path: Path):
    loader = importlib.machinery.SourceFileLoader(path.name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class SyncListTests(unittest.TestCase):
    NEW = ("price_alerts.sqlite", "chart_drawings.sqlite", "airdrops.sqlite")

    def test_export_lists_new_dbs_staged_and_never_exchange_keys(self):
        m = _load_script(REPO / "ops/sync/auu-sync-export")
        for n in self.NEW:
            self.assertIn(n, m.DBS)
            self.assertIn(n, m.STAGED)  # the app keeps them 0600: read via the staged copy
        self.assertNotIn("exchange.sqlite", m.DBS)
        self.assertNotIn("exchange.sqlite", m.STAGED)
        self.assertTrue(all(n.endswith(".sqlite") for n in m.DBS))
        self.assertEqual(m.FILES, ("users.json",))
        self.assertEqual(len(set(m.DBS)), len(m.DBS))

    def test_backup_list_has_new_dbs_and_no_secrets(self):
        spec = importlib.util.spec_from_file_location("auu_backup_ops", REPO / "ops/backup/auu_backup.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for n in self.NEW:
            self.assertIn(n, m.FILES)
            self.assertNotIn(n, m.REQUIRED)  # optional: a missing file is not a failed backup
        self.assertNotIn("exchange.sqlite", m.FILES)
        self.assertNotIn("users.json", m.FILES)

    def test_stage_script_stages_all_readable_copies_and_skips_exchange(self):
        with tempfile.TemporaryDirectory() as d:
            data, stage = Path(d) / "data", Path(d) / "stage"
            data.mkdir()
            names = ("points.sqlite", *self.NEW, "exchange.sqlite")
            for n in names:
                db = sqlite3.connect(data / n)
                db.execute("CREATE TABLE t(x)")
                db.execute("INSERT INTO t VALUES (1)")
                db.commit()
                db.close()
                os.chmod(data / n, 0o600)
            os.remove(data / "airdrops.sqlite")  # one missing file is fine
            with patch.dict(os.environ, {"AUU_SYNC_DATA": str(data), "AUU_SYNC_STAGE": str(stage)}):
                m = _load_script(REPO / "ops/sync/auu-points-stage")
                self.assertEqual(m.main(), 0)
            out = sorted(p.name for p in stage.iterdir())
            self.assertEqual(out, sorted(["points.sqlite", "price_alerts.sqlite", "chart_drawings.sqlite"]))
            for n in out:
                self.assertEqual(os.stat(stage / n).st_mode & 0o777, 0o640)
                self.assertEqual(sqlite3.connect(f"file:{stage / n}?mode=ro", uri=True).execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_stage_failure_on_one_file_does_not_block_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            data, stage = Path(d) / "data", Path(d) / "stage"
            data.mkdir()
            (data / "price_alerts.sqlite").write_bytes(b"not a database" * 100)
            db = sqlite3.connect(data / "chart_drawings.sqlite")
            db.execute("CREATE TABLE t(x)")
            db.commit()
            db.close()
            with patch.dict(os.environ, {"AUU_SYNC_DATA": str(data), "AUU_SYNC_STAGE": str(stage)}):
                m = _load_script(REPO / "ops/sync/auu-points-stage")
                self.assertEqual(m.main(), 1)
            self.assertEqual([p.name for p in stage.iterdir()], ["chart_drawings.sqlite"])


if __name__ == "__main__":
    unittest.main()
