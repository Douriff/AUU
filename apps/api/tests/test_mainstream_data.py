"""M1 mainstream market data: ccxt is mocked, sockets are blocked, nothing hits the network."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.marketdata.mainstream.config import TF_MS, MainstreamConfig, load_config
from app.marketdata.mainstream.fetcher import Fetcher
from app.marketdata.mainstream.service import MainstreamService, reset_service
from app.marketdata.mainstream.store import MarketStore

H = TF_MS["1h"]
D = TF_MS["1d"]
NOW = 1_790_000_000_000 // H * H + 30 * 60_000  # mid-hour, fixed clock
PUBLIC = {"fetch_ohlcv", "fetch_funding_rate_history", "fetch_funding_rate"}


class NetworkError(Exception):
    """Same class name as ccxt.NetworkError (retryable)."""


class ExchangeNotAvailable(Exception):
    pass


class FakeExchange:
    """Public-endpoint stand-in. Records every call; serves a deterministic tape."""

    def __init__(self, name="binance", *, skip=(), fail_times=0, block=False, page=None):
        self.name = name
        self.calls: list[tuple] = []
        self.skip = set(skip)  # candle ts the venue never returns
        self.fail_times = fail_times
        self.block = block
        self.page = page
        self.apiKey = ""
        self.secret = ""

    def __getattr__(self, item):  # any non-public method call is a test failure
        raise AssertionError(f"non-public exchange method used: {item}")

    def _gate(self, method, *a, **kw):
        self.calls.append((method, a, kw))
        if self.block:
            raise ExchangeNotAvailable(f"{self.name} GET https://x/api 451 Service unavailable from a restricted location")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise NetworkError("RequestTimeout: read timed out")

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self._gate("fetch_ohlcv", symbol, timeframe, since=since, limit=limit)
        step = TF_MS[timeframe]
        ts = (since + step - 1) // step * step
        out = []
        lim = min(limit or 500, self.page or 10_000)
        while ts <= NOW and len(out) < lim:
            if ts not in self.skip:
                px = 100.0 + (ts // step) % 50
                out.append([ts, px, px + 1, px - 1, px + 0.5, 10.0])
            ts += step
        return out

    def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        self._gate("fetch_funding_rate_history", symbol, since=since, limit=limit)
        step = 8 * H
        ts = (since + step - 1) // step * step
        out = []
        while ts <= NOW and len(out) < (limit or 100):
            out.append({"symbol": symbol, "timestamp": ts, "fundingRate": 0.0001})
            ts += step
        return out

    def fetch_funding_rate(self, symbol):
        self._gate("fetch_funding_rate", symbol)
        return {"symbol": symbol, "fundingRate": 0.00012, "fundingTimestamp": NOW + H, "timestamp": NOW}


def _cfg(**kw) -> MainstreamConfig:
    base = dict(symbols=["BTC"], exchanges=["binance", "okx"], backfill_days={"1d": 10, "1h": 2}, funding_backfill_days=2)
    base.update(kw)
    return MainstreamConfig(**base)


class _NoNetwork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MarketStore(Path(self.tmp.name) / "m.sqlite")
        self.sleeps: list[float] = []
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        self._factory = patch(
            "app.marketdata.mainstream.service.make_exchange",
            side_effect=AssertionError("real ccxt client constructed in test"),
        )
        self._factory.start()

    def tearDown(self):
        self._sock.stop()
        self._factory.stop()
        self.store.close()
        self.tmp.cleanup()

    def svc(self, exchanges: dict, cfg=None, now=NOW):
        return MainstreamService(
            cfg or _cfg(),
            self.store,
            exchange_factory=lambda name, _cfg: exchanges[name],
            sleep=self.sleeps.append,
            now_ms=lambda: now,
        )


class StoreAndRefreshTests(_NoNetwork):
    def test_strategy_funding_is_extended_backwards_once(self):
        ex = FakeExchange()
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC"], strategy_symbols=["BTC"], funding_backfill_days=2, strategy_funding_days=6))
        # Simulate a store first filled with the short display default (2 days).
        svc.cfg.strategy_symbols = []
        svc.refresh_once()
        first_short, _, n_short = self.store.funding_bounds("binance", "BTC")
        self.assertGreater(first_short, NOW - 3 * 86_400_000)
        svc.cfg.strategy_symbols = ["BTC"]
        svc.refresh_once()
        first, _, n = self.store.funding_bounds("binance", "BTC")
        self.assertLessEqual(first, NOW - 5 * 86_400_000)
        self.assertGreater(n, n_short)
        calls = len([c for c in ex.calls if c[0] == "fetch_funding_rate_history"])
        svc.refresh_once()  # already backfilled in this process: no extra history walk
        self.assertLessEqual(len([c for c in ex.calls if c[0] == "fetch_funding_rate_history"]) - calls, 1)

    def test_strategy_only_coins_fetch_daily_hourly_and_funding_only(self):
        ex = FakeExchange()
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC"], strategy_symbols=["BTC", "XRP"], strategy_funding_days=5))
        svc.refresh_once()
        xrp = [(m, a[1] if m == "fetch_ohlcv" else None) for m, a, _ in ex.calls if a and a[0].startswith("XRP")]
        self.assertTrue(xrp)
        self.assertEqual({tf for m, tf in xrp if m == "fetch_ohlcv"}, {"1d", "1h"})  # 1h = risk caps' hourly marks; no 4h
        self.assertNotIn("fetch_funding_rate", {m for m, _ in xrp})
        self.assertIn("fetch_funding_rate_history", {m for m, _ in xrp})
        self.assertGreater(self.store.candle_bounds("binance", "XRP", "1d")[2], 0)
        first, _, _ = self.store.funding_bounds("binance", "XRP")
        self.assertLessEqual(first, NOW - 4 * 86_400_000)  # strategy funding backfill (5 d) > display default (2 d)
        self.assertGreater(self.store.candle_bounds("binance", "XRP", "1h")[2], 0)
        self.assertEqual(self.store.candle_bounds("binance", "XRP", "4h")[2], 0)
        self.assertIn("1h", {a[1] for m, a, _ in ex.calls if m == "fetch_ohlcv" and a[0].startswith("BTC")})

    def test_backfill_then_incremental(self):
        ex = FakeExchange()
        svc = self.svc({"binance": ex})
        first = svc.refresh_once()
        self.assertEqual(first["exchange"], "binance")
        lo, hi, n = self.store.candle_bounds("binance", "BTC", "1h")
        self.assertEqual(hi, NOW // H * H)
        self.assertEqual(n, (hi - lo) // H + 1)
        self.assertGreaterEqual(n, 48)
        _, dhi, dn = self.store.candle_bounds("binance", "BTC", "1d")
        self.assertEqual(dhi, NOW // D * D)
        self.assertGreaterEqual(dn, 10)
        self.assertEqual(self.store.funding_bounds("binance", "BTC")[2], 6)
        self.assertTrue({c[0] for c in ex.calls} <= PUBLIC)
        self.assertEqual(ex.calls[0][1][0], "BTC/USDT")  # spot klines
        fr = [c for c in ex.calls if c[0] == "fetch_funding_rate_history"]
        self.assertEqual(fr[0][1][0], "BTC/USDT:USDT")  # perpetual swap

        ex.calls.clear()
        again = svc.refresh_once()
        self.assertTrue(all(v == 0 for v in again["added"].values()), again)
        h_calls = [c for c in ex.calls if c[0] == "fetch_ohlcv" and c[1][1] == "1h"]
        self.assertEqual(h_calls[0][2]["since"], hi)  # resumes at the last (forming) bar
        self.assertEqual(self.store.candle_bounds("binance", "BTC", "1h")[2], n)

    def test_paginates_small_pages(self):
        svc = self.svc({"binance": FakeExchange(page=7)})
        svc.refresh_once()
        lo, hi, n = self.store.candle_bounds("binance", "BTC", "1h")
        self.assertEqual(n, (hi - lo) // H + 1)
        self.assertEqual(self.store.find_gaps("binance", "BTC", "1h", H), [])

    def test_gap_detection_and_repair(self):
        base = NOW // H * H
        rows = [[base - i * H, 1, 1, 1, 1, 1] for i in range(10) if i not in (3, 4, 7)]
        self.store.upsert_candles("binance", "BTC", "1h", rows)
        self.assertEqual(
            self.store.find_gaps("binance", "BTC", "1h", H),
            [(base - 7 * H, base - 7 * H), (base - 4 * H, base - 3 * H)],
        )
        svc = self.svc({"binance": FakeExchange()})
        svc.refresh_once()
        self.assertEqual(self.store.find_gaps("binance", "BTC", "1h", H), [])
        self.assertEqual(svc.freshness()["gaps"], {})

    def test_unfillable_gap_is_reported(self):
        base = NOW // H * H
        hole = base - 5 * H
        self.store.upsert_candles("binance", "BTC", "1h", [[base - i * H, 1, 1, 1, 1, 1] for i in range(10) if i != 5])
        svc = self.svc({"binance": FakeExchange(skip={hole})})
        svc.refresh_once()
        self.assertEqual(self.store.find_gaps("binance", "BTC", "1h", H), [(hole, hole)])
        self.assertEqual(svc.freshness()["gaps"], {"binance:BTC:1h": 1})

    def test_retries_with_backoff(self):
        ex = FakeExchange(fail_times=2)
        svc = self.svc({"binance": ex})
        res = svc.refresh_once()
        self.assertEqual(res["exchange"], "binance")
        self.assertEqual(self.sleeps[:2], [1.0, 2.0])
        self.assertGreater(self.store.candle_bounds("binance", "BTC", "1d")[2], 0)

    def test_retry_exhaustion_logs_error(self):
        f = Fetcher("binance", FakeExchange(fail_times=99), _cfg(retries=3), sleep=self.sleeps.append, now_ms=lambda: NOW)
        with self.assertRaises(Exception) as ctx:
            f.ohlcv("BTC/USDT", "1h", NOW - 5 * H)
        self.assertIn("NetworkError", str(ctx.exception))
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_geo_block_falls_back_to_okx(self):
        binance, okx = FakeExchange("binance", block=True), FakeExchange("okx")
        svc = self.svc({"binance": binance, "okx": okx})
        res = svc.refresh_once()
        self.assertEqual(res["exchange"], "okx")
        self.assertIn("binance", res["skipped"])
        self.assertEqual(len(binance.calls), 1)  # blocked: no retry storm
        fresh = svc.freshness()
        self.assertEqual(fresh["exchange"], "okx")
        self.assertIn("binance", fresh["blocked"])
        self.assertFalse(fresh["stale"])
        # The working venue is tried first on the next pass.
        self.assertEqual(svc.candidates()[0], "okx")

    def test_all_venues_down(self):
        svc = self.svc({"binance": FakeExchange(block=True), "okx": FakeExchange("okx", block=True)})
        res = svc.refresh_once()
        self.assertIsNone(res["exchange"])
        fresh = svc.freshness()
        self.assertTrue(fresh["stale"])
        self.assertIsNone(fresh["exchange"])

    def test_freshness_turns_stale(self):
        self.svc({"binance": FakeExchange()}).refresh_once()
        fresh = self.svc({"binance": FakeExchange()}).freshness()
        self.assertFalse(fresh["stale"], fresh)
        later = self.svc({"binance": FakeExchange()}, now=NOW + 6 * H)
        stale = later.freshness()
        self.assertTrue(stale["stale"])
        self.assertIn("BTC:1h", stale["staleSeries"])
        self.assertNotIn("BTC:1d", stale["staleSeries"])

    def test_overview_fields(self):
        svc = self.svc({"binance": FakeExchange()})
        svc.refresh_once()
        ov = svc.overview()
        item = ov["items"][0]
        self.assertEqual(item["symbol"], "BTC")
        self.assertIsNotNone(item["price"])
        self.assertIsNotNone(item["change24h"])
        self.assertAlmostEqual(item["fundingLast"], 0.0001)
        self.assertAlmostEqual(item["fundingNow"], 0.00012)
        self.assertAlmostEqual(item["fundingAnnualized"], 0.00012 * 3 * 365)


M1 = TF_MS["1m"]


class IntradayOnDemandTests(_NoNetwork):
    """1m/5m/15m: fetched only when asked, recent window only, throttled."""

    def _svc(self, ex, now=NOW, **cfg):
        svc = self.svc({"binance": ex}, cfg=_cfg(**cfg), now=now)
        svc.active = "binance"
        return svc

    def _ohlcv(self, ex, tf):
        return [c for c in ex.calls if c[0] == "fetch_ohlcv" and c[1][1] == tf]

    def test_background_refresh_never_fetches_intraday(self):
        ex = FakeExchange()
        self.svc({"binance": ex}).refresh_once()
        tfs = {c[1][1] for c in ex.calls if c[0] == "fetch_ohlcv"}
        self.assertEqual(tfs, {"1d", "4h", "1h"})
        self.assertEqual(self.store.candle_bounds("binance", "BTC", "1m")[2], 0)

    def test_first_request_fetches_window_then_serves_from_cache(self):
        ex = FakeExchange()
        svc = self._svc(ex)
        out = svc.chart_candles("BTC", "1m", limit=120)
        self.assertEqual(len(out["candles"]), 120)
        self.assertEqual(out["candles"][-1]["ts"], NOW // M1 * M1)
        self.assertEqual(out["retentionDays"], 7)
        self.assertEqual(self._ohlcv(ex, "1m")[0][2]["since"], NOW // M1 * M1 - 119 * M1)
        ex.calls.clear()
        again = svc.chart_candles("BTC", "1m", limit=120)  # within the tail TTL: no exchange call
        self.assertEqual(len(again["candles"]), 120)
        self.assertEqual(ex.calls, [])

    def test_tail_refetch_after_ttl_only_from_last_bar(self):
        ex = FakeExchange()
        self._svc(ex).chart_candles("BTC", "5m", limit=50)
        ex.calls.clear()
        later = self._svc(ex, now=NOW + 11_000)
        later.chart_candles("BTC", "5m", limit=50)
        calls = self._ohlcv(ex, "5m")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["since"], NOW // TF_MS["5m"] * TF_MS["5m"])

    def test_paging_back_and_retention_limit(self):
        ex = FakeExchange()
        svc = self._svc(ex, intraday_days=1)
        first = svc.chart_candles("BTC", "15m", limit=40)
        oldest = first["candles"][0]["ts"]
        older = svc.chart_candles("BTC", "15m", limit=40, before=oldest)
        self.assertEqual(older["candles"][-1]["ts"], oldest - TF_MS["15m"])
        self.assertEqual(len(older["candles"]), 40)
        # Asking past the 1-day window returns only what's inside it and flags the limit.
        keep_from = NOW // TF_MS["15m"] * TF_MS["15m"] - D
        far = svc.chart_candles("BTC", "15m", limit=2000, before=older["candles"][0]["ts"])
        self.assertTrue(far["limited"])
        self.assertGreaterEqual(min(c["ts"] for c in far["candles"]), keep_from)
        ex.calls.clear()
        none = svc.chart_candles("BTC", "15m", limit=10, before=keep_from - D)
        self.assertEqual(none["candles"], [])
        self.assertTrue(none["limited"])
        self.assertEqual(ex.calls, [])

    def test_old_rows_are_pruned(self):
        old = NOW // M1 * M1 - 9 * D
        self.store.upsert_candles("binance", "BTC", "1m", [[old + i * M1, 1, 1, 1, 1, 1] for i in range(30)])
        self._svc(FakeExchange()).chart_candles("BTC", "1m", limit=10)
        lo, _, n = self.store.candle_bounds("binance", "BTC", "1m")
        self.assertGreaterEqual(lo, NOW // M1 * M1 - 7 * D)
        self.assertEqual(n, 10)

    def test_unfillable_hole_is_not_hammered(self):
        cur = NOW // M1 * M1
        ex = FakeExchange(skip={cur - 5 * M1})
        svc = self._svc(ex)
        svc.chart_candles("BTC", "1m", limit=20, before=cur - M1)
        n1 = len(self._ohlcv(ex, "1m"))
        svc.chart_candles("BTC", "1m", limit=20, before=cur - M1)
        self.assertEqual(len(self._ohlcv(ex, "1m")), n1)

    def test_fetch_error_serves_cache(self):
        ex = FakeExchange()
        svc = self._svc(ex)
        svc.chart_candles("BTC", "1m", limit=10)
        ex.fail_times = 99
        out = self._svc(ex, now=NOW + 60_000).chart_candles("BTC", "1m", limit=10)
        self.assertIn("fetchError", out)
        self.assertEqual(len(out["candles"]), 10)

    def test_stored_timeframes_page_back(self):
        svc = self.svc({"binance": FakeExchange()})
        svc.refresh_once()
        first = svc.chart_candles("BTC", "4h", limit=30)
        self.assertEqual(first["candles"][-1]["ts"], NOW // TF_MS["4h"] * TF_MS["4h"])
        older = svc.chart_candles("BTC", "4h", limit=30, before=first["candles"][0]["ts"])
        self.assertEqual(older["candles"][-1]["ts"], first["candles"][0]["ts"] - TF_MS["4h"])
        tail = svc.chart_candles("BTC", "1d", limit=2000, before=first["candles"][0]["ts"])
        self.assertTrue(tail["limited"])


class ConfigTests(unittest.TestCase):
    def test_legacy_toggle_defaults_off(self):
        from app.legacy import legacy_pump_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUU_LEGACY_PUMP", None)
            self.assertFalse(legacy_pump_enabled())
            os.environ["AUU_LEGACY_PUMP"] = "on"
            self.assertTrue(legacy_pump_enabled())
        from app.main import create_app

        paths = {getattr(r, "path", "") for r in create_app(legacy=True).routes}
        self.assertIn("/api/v1/board", paths)
        self.assertIn("/api/v1/mainstream/overview", paths)
        paths = {getattr(r, "path", "") for r in create_app(legacy=False).routes}
        self.assertNotIn("/api/v1/board", paths)
        self.assertNotIn("/api/v1/strategy/pump-paper-v1", paths)

    def test_symbols_and_exchange_env(self):
        with patch.dict(os.environ, {"AUU_MAINSTREAM_SYMBOLS": "btc, eth,doge,,ETH", "AUU_MAINSTREAM_EXCHANGE": "okx"}):
            cfg = load_config()
        self.assertEqual(cfg.symbols, ["BTC", "ETH", "DOGE"])
        self.assertEqual(cfg.exchanges, ["okx"])
        self.assertEqual(cfg.perp("DOGE"), "DOGE/USDT:USDT")
        with patch.dict(os.environ, {"AUU_MAINSTREAM_EXCHANGE": "auto"}):
            self.assertEqual(load_config().exchanges, ["binance", "okx"])

    def test_real_client_is_keyless(self):
        from app.marketdata.mainstream.fetcher import make_exchange

        with patch.object(socket.socket, "connect", side_effect=AssertionError("network")):
            for name in ("binance", "okx"):
                client = make_exchange(name, MainstreamConfig())
                self.assertFalse(client.apiKey)
                self.assertFalse(client.secret)

    def test_no_order_or_private_calls_in_source(self):
        root = Path(__file__).resolve().parents[1] / "app" / "marketdata" / "mainstream"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in ("create_order", "createOrder", "fetch_balance", "apiKey=", "secret=", "private_"):
                self.assertNotIn(needle, text, f"{path.name}: {needle}")


_ENV = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_INVITE_CODE", "AUU_ADMIN_USER", "AUU_USER_STORE", "AUU_USER_JOURNAL_DIR", "AUU_AUTH_RATE_MAX", "AUU_LEGACY_PUMP")
PW = "mainPass123"


class MainstreamApiTests(_NoNetwork):
    """App built in mainstream mode (AUU_LEGACY_PUMP off) with login on."""

    def setUp(self):
        super().setUp()
        from app.auth.accounts import reset_accounts

        self._prev = {k: os.environ.get(k) for k in _ENV}
        root = Path(self.tmp.name)
        os.environ.update(
            {
                "AUU_AUTH": "on",
                "AUU_ALLOW_SIGNUP": "on",
                "AUU_USER_STORE": str(root / "users.json"),
                "AUU_USER_JOURNAL_DIR": str(root / "journals"),
                "AUU_AUTH_RATE_MAX": "1000",
                "AUU_LEGACY_PUMP": "off",
            }
        )
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_accounts()
        svc = self.svc({"binance": FakeExchange()}, cfg=_cfg(symbols=["BTC", "ETH", "SOL"]))
        svc.refresh_once()
        reset_service(svc)
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.TestClient = TestClient
        self.app = create_app(legacy=False)

    def tearDown(self):
        from app.auth.accounts import reset_accounts

        reset_service(None)
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_accounts()
        super().tearDown()

    def _signed_in(self, name):
        client = self.TestClient(self.app)
        res = client.post("/api/v1/auth/register", json={"name": name, "password": PW, "password_confirm": PW})
        self.assertEqual(res.status_code, 200, res.text)
        return client

    PATHS = [
        "/api/v1/mainstream/overview",
        "/api/v1/mainstream/status",
        "/api/v1/mainstream/candles?symbol=BTC&tf=1d",
        "/api/v1/mainstream/candles?symbol=ETH&tf=1h",
        "/api/v1/mainstream/funding?symbol=SOL",
        "/api/v1/mainstream/markets",
    ]

    def test_new_api_requires_login(self):
        anon = self.TestClient(self.app)
        for path in self.PATHS:
            with self.subTest(path=path):
                res = anon.get(path)
                self.assertEqual(res.status_code, 401, res.text)
                self.assertEqual(res.json()["error"]["code"], "AUTH_REQUIRED")
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with anon.websocket_connect("/api/v1/ws") as ws:
                ws.receive_json()

    def test_logged_in_reads(self):
        user = self._signed_in("msuser")
        ov = user.get("/api/v1/mainstream/overview").json()["data"]
        self.assertEqual([i["symbol"] for i in ov["items"]], ["BTC", "ETH", "SOL"])
        self.assertTrue(all(i["price"] for i in ov["items"]))
        c = user.get("/api/v1/mainstream/candles?symbol=btc/usdt&tf=1h&limit=5").json()["data"]
        self.assertEqual(len(c["candles"]), 5)
        self.assertEqual(c["candles"][-1]["ts"], NOW // H * H)
        f = user.get("/api/v1/mainstream/funding?symbol=ETH").json()["data"]
        self.assertTrue(f["funding"])
        for tf in ("1m", "5m", "15m", "1h", "4h", "1d"):
            with self.subTest(tf=tf):
                r = user.get(f"/api/v1/mainstream/candles?symbol=BTC&tf={tf}&limit=20")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertTrue(r.json()["data"]["candles"], tf)
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=DOGE").status_code, 404)
        self.assertEqual(user.get("/api/v1/mainstream/candles?symbol=BTC&tf=3m").status_code, 400)
        mk = user.get("/api/v1/mainstream/markets").json()["data"]
        self.assertEqual([i["symbol"] for i in mk["items"]], ["BTC", "ETH", "SOL"])
        self.assertTrue(all(i["price"] and "held" in i for i in mk["items"]))
        # Read-only: no write verbs on the new API.
        self.assertEqual(user.post("/api/v1/mainstream/overview", json={}).status_code, 405)

    def test_health_mainstream_mode(self):
        anon = self.TestClient(self.app)
        h = anon.get("/api/v1/health").json()["data"]
        self.assertEqual(h["provider"], "cex_public")
        self.assertEqual(h["marketData"], "real")
        self.assertFalse(h["legacyPump"])
        self.assertFalse(h["liveEnabled"])
        self.assertFalse(h["autopaperStall"]["stalled"])
        # M3: the daily trend runner is the running strategy (not stalled right after start).
        self.assertEqual(h["autopaperStall"]["reason"], "")
        self.assertEqual(h["runningStrategies"], ["trend_tsmom_v1"])
        self.assertFalse(h["strategyRunner"]["stalled"])
        self.assertEqual(h["mainstream"]["exchange"], "binance")
        self.assertFalse(h["mainstream"]["stale"], h["mainstream"])
        self.assertIn("BTC:1h", h["mainstream"]["series"])

    def test_pump_routes_not_mounted(self):
        user = self._signed_in("msnopump")
        for path in ("/api/v1/board", "/api/v1/strategy/pump-paper-v1", "/api/v1/watch/traders", "/api/v1/symbols", "/api/v1/stats/postmortem"):
            with self.subTest(path=path):
                self.assertEqual(user.get(path).status_code, 404)
        self.assertEqual(user.get("/api/v1/stats/paper-performance").status_code, 200)  # ledger + Go/No-Go
        with user.websocket_connect("/api/v1/ws") as ws:
            hello = ws.receive_json()
            self.assertEqual(hello["venue"], "CEX")
            ws.send_json({"type": "subscribe", "channel": "candles", "symbol": "X"})
            self.assertEqual(ws.receive_json()["payload"]["code"], "LEGACY_OFF")

    def test_live_still_locked(self):
        from app.live.gate import evaluate, reset_live_state

        admin = self._signed_in("msadmin")  # first account is admin
        for method, path, body in [
            ("PUT", "/api/v1/live/enabled", {"liveEnabled": True, "confirmed": True}),
            ("PUT", "/api/v1/live/arm", {"armed": True}),
            ("PUT", "/api/v1/live/disabled", {"live_disabled": False}),
        ]:
            with self.subTest(path=path):
                res = admin.request(method, path, json=body)
                self.assertEqual(res.status_code, 403, res.text)
                self.assertIn("LIVE_API_LOCKED", res.json()["error"]["reasons"])
        self.assertFalse(evaluate().live_enabled)
        self.assertFalse(admin.get("/api/v1/health").json()["data"]["liveEnabled"])
        reset_live_state()

    def test_lifespan_starts_no_pump_tasks(self):
        boom = AssertionError("legacy task started in mainstream mode")
        with patch("app.legacy.pump.strategies.pump_paper_v1.PumpPaperEngine.run_loop", side_effect=boom), patch(
            "app.legacy.pump.discovery.DiscoveryRuntime.run_loop", side_effect=boom
        ), patch("app.providers.get_provider", side_effect=boom):
            with self.TestClient(self.app) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)

if __name__ == "__main__":
    unittest.main()
