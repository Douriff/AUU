"""Spot screener: three-valued logic, daily indicators, and a read-only book.

The screener is additive. These tests also pin the public auth list, the root
catalog, and the existing navigation / premium paths.
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path

from app.auth.gate import PUBLIC_PATHS, gate_decision  # noqa: E402
from app.main import app, root  # noqa: E402
from app.routes import screener as screener_routes  # noqa: E402
from app.screener.logic import (  # noqa: E402
    DAY_MS,
    DEFAULT_QUERY,
    FALSE,
    TRUE,
    UNKNOWN,
    SchemaError,
    accept_query,
    combine_all,
    combine_any,
    compare,
    csv_cell,
    ema,
    export_csv,
    fails,
    negate,
    passes,
    rsi,
)
from app.screener.market import (  # noqa: E402
    DAY_MS as MARKET_DAY_MS,
    HttpResult,
    ScreenerBook,
    build_default_book,
    fixture_enabled,
    fixture_info,
)
from app.screener.market import FixtureTransport  # noqa: E402

NOW = 1_800_000_000_000
REPO = Path(__file__).resolve().parents[3]


def spot(base: str, **extra) -> dict:
    row = {
        "symbol": f"{base}USDT",
        "status": "TRADING",
        "baseAsset": base,
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "permissions": ["SPOT"],
    }
    row.update(extra)
    return row


def ticker(base: str, price, volume, source=NOW) -> dict:
    return {"symbol": f"{base}USDT", "lastPrice": price, "quoteVolume": volume, "closeTime": source}


def klines_from(closes, now=NOW, forming=None, shift_days=0) -> list:
    day = now // DAY_MS * DAY_MS
    last_open = day - DAY_MS * (1 + shift_days)
    first = last_open - DAY_MS * (len(closes) - 1)
    rows = []
    for index, close in enumerate(closes):
        open_time = first + index * DAY_MS
        rows.append([open_time, close, close, close, close, 1, open_time + DAY_MS - 1, 1, 0, 0, 0, 0])
    forming_open = day
    forming_close = closes[-1] if forming is None else forming
    rows.append([
        forming_open, forming_close, forming_close, forming_close, forming_close, 1,
        forming_open + DAY_MS - 1, 1, 0, 0, 0, 0,
    ])
    return rows


class Script:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.info = [spot("BTC")]
        self.tickers: list = []
        self.klines: dict = {}
        self.info_result = None
        self.ticker_result = None
        self.kline_result = None
        self.delay = 0.0

    def get(self, url: str) -> HttpResult:
        with self._lock:
            self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if "exchangeInfo" in url:
            if self.info_result is not None:
                return self.info_result() if callable(self.info_result) else self.info_result
            return HttpResult.success({"symbols": self.info})
        if "ticker/24hr" in url:
            if self.ticker_result is not None:
                return self.ticker_result() if callable(self.ticker_result) else self.ticker_result
            return HttpResult.success(self.tickers)
        if "klines" in url:
            if self.kline_result is not None:
                return self.kline_result(url) if callable(self.kline_result) else self.kline_result
            symbol = url.split("symbol=", 1)[1].split("&", 1)[0]
            return HttpResult.success(self.klines.get(symbol, []))
        return HttpResult.failure(404, "not_found")

    def kline_calls(self) -> list[str]:
        return [url for url in self.calls if "klines" in url]


def make(script: Script, clock=None, store=None, concurrency=4) -> tuple[ScreenerBook, dict]:
    clock = clock if clock is not None else {"t": NOW}
    book = ScreenerBook(
        transport=script,
        source_kind="binance-public-rest",
        store_reader=store,
        now_ms=lambda: clock["t"],
        concurrency=concurrency,
        root=lambda: "https://example.test",
    )
    return book, clock


def price_gt(threshold: float) -> dict:
    return {"cmp": "gt", "left": {"field": "price"}, "right": {"num": threshold}}


def vol_gt(threshold: float) -> dict:
    return {"cmp": "gt", "left": {"field": "quoteVolume24h"}, "right": {"num": threshold}}


def ema_field(period: int) -> dict:
    return {"field": "ema", "period": period, "tf": "1d"}


def rsi_field() -> dict:
    return {"field": "rsi", "period": 14, "tf": "1d"}


class LogicTests(unittest.TestCase):
    def test_all_does_not_drop_unknown(self):
        self.assertEqual(combine_all([TRUE, UNKNOWN]), UNKNOWN)
        self.assertEqual(combine_all([TRUE, TRUE]), TRUE)
        self.assertEqual(combine_all([FALSE, UNKNOWN]), FALSE)
        self.assertEqual(combine_all([]), UNKNOWN)
        self.assertFalse(passes(combine_all([TRUE, UNKNOWN])))
        self.assertFalse(fails(combine_all([TRUE, UNKNOWN])))

    def test_any_and_negation_keep_unknown(self):
        self.assertEqual(combine_any([FALSE, UNKNOWN]), UNKNOWN)
        self.assertEqual(combine_any([TRUE, UNKNOWN]), TRUE)
        self.assertEqual(combine_any([FALSE, FALSE]), FALSE)
        self.assertEqual(negate(UNKNOWN), UNKNOWN)
        self.assertEqual(negate(FALSE), TRUE)
        self.assertEqual(negate(TRUE), FALSE)
        self.assertTrue(passes(negate(FALSE)))
        self.assertTrue(fails(negate(TRUE)))
        self.assertFalse(passes(negate(UNKNOWN)))
        self.assertFalse(fails(negate(UNKNOWN)))
        self.assertFalse(fails(UNKNOWN))
        self.assertFalse(passes(UNKNOWN))

    def test_flat_short_and_nan_rsi(self):
        self.assertEqual(rsi([1.0] * 20, 14), 50.0)
        self.assertEqual(rsi(list(range(1, 30)), 14), 100.0)
        self.assertEqual(rsi(list(range(30, 1, -1)), 14), 0.0)
        self.assertIsNone(rsi([1.0] * 14, 14))
        self.assertIsNone(rsi([1.0] * 10 + [float("nan")] + [1.0] * 10, 14))
        self.assertNotEqual(rsi([1.0] * 20, 14), 0.0)

    def test_ema_short_nan_and_seed(self):
        self.assertIsNone(ema([1.0] * 9, 10))
        self.assertIsNone(ema([1.0] * 5 + [float("nan")] + [1.0] * 5, 10))
        self.assertEqual(ema([float(i) for i in range(1, 11)], 10), 5.5)
        nxt = ema([float(i) for i in range(1, 12)], 10)
        self.assertAlmostEqual(nxt, (11 - 5.5) * (2 / 11) + 5.5)

    def test_compare_uses_raw_precision_and_not_zero(self):
        self.assertEqual(compare("gt", 1.004, 1.001), TRUE)
        self.assertEqual(compare("gt", None, -1), UNKNOWN)
        self.assertNotEqual(compare("gt", None, -1), TRUE)
        self.assertEqual(compare("gt", float("nan"), 1), UNKNOWN)

    def test_schema_rejects_unknown_cross_period_and_junk(self):
        with self.assertRaises(SchemaError) as ctx:
            accept_query({"cmp": "gt", "left": {"field": "ema", "period": 50, "tf": "1d"}, "right": {"num": 1}})
        self.assertEqual(ctx.exception.code, "BAD_PERIOD")
        with self.assertRaises(SchemaError) as ctx:
            accept_query({"cmp": "crossed_above", "left": {"field": "price"}, "right": {"num": 1}})
        self.assertEqual(ctx.exception.code, "CROSS_UNSUPPORTED")
        with self.assertRaises(SchemaError) as ctx:
            accept_query({"op": "crossed_below", "of": []})
        self.assertEqual(ctx.exception.code, "CROSS_UNSUPPORTED")
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "ema", "period": 10, "tf": "1h"}, "right": {"num": 1}})
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "close"}, "right": {"num": 1}})
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "price"}, "right": {"num": 1}, "note": "x"})
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "price"}, "right": {"num": True}})
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "price"}, "right": {"num": "1"}})
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "price"}, "right": {"num": 1e20}})
        with self.assertRaises(SchemaError):
            accept_query({"cmp": "gt", "left": {"field": "price"}, "right": {"num": float("nan")}})
        leaves = [price_gt(i) for i in range(9)]
        with self.assertRaises(SchemaError) as ctx:
            accept_query({"op": "all", "of": leaves})
        self.assertEqual(ctx.exception.code, "TOO_COMPLEX")
        parsed = accept_query(DEFAULT_QUERY)
        self.assertEqual(parsed["type"], "group")
        self.assertEqual(parsed["op"], "all")
        blob = json.dumps(DEFAULT_QUERY)
        self.assertNotIn("50", blob)
        self.assertNotIn("ema50", blob)
        self.assertNotIn("cross", blob)

    def test_csv_quotes_and_neutralizes_formulas(self):
        text = export_csv(
            ["symbol", "note"],
            [{"symbol": "=HYPERLINK(\"http://evil\")", "note": 'say "hi"'}, {"symbol": " +cmd", "note": "-1+1"}, {"symbol": "@SUM(A1)", "note": "\t=1"}],
        )
        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn("\"'=HYPERLINK(\"\"http://evil\"\")\"", text)
        self.assertIn("\"say \"\"hi\"\"\"", text)
        self.assertIn("\"' +cmd\"", text)
        self.assertIn("\"'-1+1\"", text)
        self.assertIn("\"'@SUM(A1)\"", text)
        self.assertTrue(csv_cell("=1+1").startswith("\"'="))
        self.assertNotRegex(text, r'(^|,)=')


class BookTests(unittest.TestCase):
    def test_fixture_switch_defaults_off(self):
        os.environ.pop("AUU_SCREENER_FIXTURE", None)
        self.assertFalse(fixture_enabled())
        book = build_default_book()
        self.assertEqual(book.source_kind, "binance-public-rest")
        self.assertFalse(book.meta()["fixture"])
        self.assertFalse(book.meta()["liveEnabled"])
        self.assertIsNone(book.meta()["universe"]["topN"])
        self.assertFalse(book.meta()["universe"]["priceNearOneExcluded"])
        self.assertNotIn(50, [item.get("period") for item in book.meta()["fields"]])
        self.assertIn("crossed_above", book.meta()["unsupported"])

    def test_non_spot_rejected_and_near_one_kept(self):
        script = Script()
        script.info = fixture_info()
        script.tickers = [
            ticker("BTC", 100, 5_000_000),
            ticker("USDC", 0.9998, 9_000_000),
            ticker("FUT", 100, 9_000_000_000),
            ticker("HALT", 100, 9_000_000_000),
            ticker("MRGN", 100, 9_000_000_000),
        ]
        book, _clock = make(script)
        data = book.screen(price_gt(0))
        bases = [row["base"] for row in data["rows"]]
        self.assertIn("USDC", bases)
        self.assertNotIn("FUT", bases)
        self.assertNotIn("HALT", bases)
        self.assertNotIn("MRGN", bases)
        usdc = next(row for row in data["rows"] if row["base"] == "USDC")
        self.assertEqual(usdc["verdict"], "pass")
        self.assertEqual(usdc["values"]["price"]["value"], 0.9998)
        self.assertEqual(script.kline_calls(), [])

    def test_no_hidden_top_n(self):
        script = Script()
        script.info = [spot(f"C{index:02d}") for index in range(80)]
        script.tickers = [ticker(f"C{index:02d}", 1.0000001, 10) for index in range(80)]
        book, _clock = make(script)
        data = book.screen(price_gt(1))
        self.assertEqual(len(data["rows"]), 80)
        self.assertEqual(data["counts"]["passes"], 80)
        self.assertEqual(data["universe"]["topN"], None)
        self.assertEqual(script.kline_calls(), [])

    def test_precision_not_pre_rounded(self):
        script = Script()
        script.tickers = [ticker("BTC", 1.004, 10)]
        book, _clock = make(script)
        data = book.screen(price_gt(1.001))
        self.assertEqual(data["rows"][0]["verdict"], "pass")
        self.assertEqual(data["rows"][0]["values"]["price"]["value"], 1.004)

    def test_missing_volume_is_not_zero(self):
        script = Script()
        script.tickers = [{"symbol": "BTCUSDT", "lastPrice": "10", "closeTime": NOW}]
        book, _clock = make(script)
        data = book.screen(vol_gt(-1))
        row = data["rows"][0]
        self.assertEqual(row["verdict"], "unknown")
        self.assertIsNone(row["values"]["quoteVolume24h"]["value"])
        self.assertNotIn("display", row["values"]["quoteVolume24h"])
        self.assertNotEqual(row["values"]["quoteVolume24h"]["value"], 0)

    def test_forming_bar_excluded_and_price_is_ticker(self):
        script = Script()
        script.tickers = [ticker("BTC", 10, 5_000_000)]
        script.klines["BTCUSDT"] = klines_from([10.0] * 20, forming=1000)
        book, _clock = make(script)
        query = {"cmp": "gt", "left": ema_field(20), "right": {"num": 10}}
        data = book.screen(query)
        row = data["rows"][0]
        self.assertEqual(row["verdict"], "fail")
        self.assertTrue(row["formingExcluded"])
        self.assertEqual(row["values"]["ema20"]["value"], 10.0)
        priced = book.screen(price_gt(20))
        self.assertEqual(priced["rows"][0]["verdict"], "fail")
        above_close = book.screen({"cmp": "gt", "left": {"field": "price"}, "right": ema_field(20)})
        self.assertEqual(above_close["rows"][0]["verdict"], "fail")

    def test_short_history_keeps_ema10_and_unknown_ema200(self):
        script = Script()
        script.info = [spot("NEW")]
        script.tickers = [ticker("NEW", 5, 5_000_000)]
        script.klines["NEWUSDT"] = klines_from([float(i) for i in range(1, 13)])
        book, _clock = make(script)
        both = {
            "op": "all",
            "of": [
                {"cmp": "gt", "left": ema_field(10), "right": {"num": 0}},
                {"cmp": "gt", "left": ema_field(200), "right": {"num": 0}},
            ],
        }
        data = book.screen(both, budget=5)
        row = data["rows"][0]
        self.assertEqual(row["base"], "NEW")
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual(row["values"]["ema10"]["state"], "ok")
        self.assertIsNone(row["values"]["ema200"]["value"])
        self.assertEqual(row["values"]["ema200"]["state"], "short")
        only = book.screen({"cmp": "gt", "left": ema_field(10), "right": {"num": 0}})
        self.assertEqual(only["rows"][0]["verdict"], "pass")
        self.assertNotIn("ema200", only["rows"][0]["values"])

    def test_nan_is_unknown_not_zero(self):
        script = Script()
        script.info = [spot("NAN")]
        script.tickers = [ticker("NAN", 5, 5_000_000)]
        closes = [5.0] * 30
        closes[10] = float("nan")
        script.klines["NANUSDT"] = klines_from(closes)
        book, _clock = make(script)
        data = book.screen({"cmp": "gt", "left": ema_field(20), "right": {"num": 0}})
        row = data["rows"][0]
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual(row["values"]["ema20"]["state"], "nan")
        self.assertIsNone(row["values"]["ema20"]["value"])

    def test_flat_rsi_is_fifty_and_coin_stays(self):
        script = Script()
        script.info = [spot("USDC")]
        script.tickers = [ticker("USDC", 1.0, 5_000_000)]
        script.klines["USDCUSDT"] = klines_from([1.0] * 40)
        book, _clock = make(script)
        query = {
            "op": "all",
            "of": [
                {"cmp": "gt", "left": rsi_field(), "right": {"num": 40}},
                {"cmp": "lt", "left": rsi_field(), "right": {"num": 60}},
            ],
        }
        data = book.screen(query)
        row = data["rows"][0]
        self.assertEqual(row["values"]["rsi14"]["value"], 50.0)
        self.assertEqual(row["verdict"], "pass")
        below = book.screen({"cmp": "lt", "left": rsi_field(), "right": {"num": 30}})
        self.assertEqual(below["rows"][0]["verdict"], "fail")

    def test_true_plus_unknown_is_not_a_pass(self):
        script = Script()
        script.info = [spot("NEW")]
        script.tickers = [ticker("NEW", 5, 5_000_000)]
        script.klines["NEWUSDT"] = klines_from([float(i) for i in range(1, 13)])
        book, _clock = make(script)
        query = {"op": "all", "of": [vol_gt(1), {"cmp": "gt", "left": ema_field(200), "right": {"num": 0}}]}
        data = book.screen(query)
        self.assertEqual(data["rows"][0]["eval"], UNKNOWN)
        self.assertEqual(data["rows"][0]["verdict"], "unknown")
        self.assertEqual(data["counts"]["passes"], 0)

    def test_any_false_plus_unknown_is_not_a_fail(self):
        script = Script()
        script.info = [spot("NEW")]
        script.tickers = [ticker("NEW", 5, 5_000_000)]
        script.klines["NEWUSDT"] = klines_from([1.0] * 12)
        book, _clock = make(script)
        query = {"op": "any", "of": [vol_gt(1e12), {"cmp": "gt", "left": ema_field(20), "right": {"num": 0}}]}
        data = book.screen(query)
        self.assertEqual(data["rows"][0]["eval"], UNKNOWN)
        self.assertFalse(fails(data["rows"][0]["eval"]))
        self.assertEqual(data["counts"]["fails"], 0)

    def test_any_true_skips_klines(self):
        script = Script()
        script.tickers = [ticker("BTC", 10, 5_000_000)]
        script.klines["BTCUSDT"] = klines_from([10.0] * 30)
        book, _clock = make(script)
        query = {"op": "any", "of": [vol_gt(1), {"cmp": "gt", "left": ema_field(20), "right": {"num": 0}}]}
        data = book.screen(query)
        self.assertEqual(data["rows"][0]["verdict"], "pass")
        self.assertEqual(script.kline_calls(), [])

    def test_not_unknown_is_not_a_hit(self):
        script = Script()
        script.tickers = [ticker("BTC", 10, 5_000_000)]
        book, clock = make(script)
        book.screen(price_gt(0))
        script.ticker_result = HttpResult.failure(0, "network")
        clock["t"] = NOW + 60_000
        data = book.screen({"op": "not", "of": price_gt(0)})
        self.assertEqual(data["rows"][0]["eval"], UNKNOWN)
        self.assertEqual(data["rows"][0]["verdict"], "unknown")
        self.assertEqual(data["rows"][0]["values"]["price"]["display"], 10.0)
        self.assertIsNone(data["rows"][0]["values"]["price"]["value"])
        self.assertEqual(data["asOf"]["tickerState"], "stale")

    def test_failed_or_empty_fetch_keeps_last_snapshot(self):
        script = Script()
        script.tickers = [ticker("BTC", 10, 5_000_000)]
        script.klines["BTCUSDT"] = klines_from([float(i) for i in range(1, 40)])
        book, clock = make(script)
        query = {"op": "all", "of": [price_gt(0), {"cmp": "gt", "left": ema_field(10), "right": {"num": 0}}]}
        first = book.screen(query)
        self.assertEqual(first["rows"][0]["verdict"], "pass")
        script.ticker_result = HttpResult.success([])
        script.kline_result = HttpResult.success([])
        clock["t"] = NOW + 20 * 60 * 1000
        second = book.screen(query)
        row = second["rows"][0]
        self.assertEqual(row["values"]["price"]["display"], 10.0)
        self.assertIsNone(row["values"]["price"]["value"])
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual(second["asOf"]["tickerError"], "empty")
        self.assertNotEqual(row["verdict"], "pass")

    def test_network_failure_does_not_clear_ticker(self):
        script = Script()
        script.tickers = [ticker("BTC", 42, 5)]
        book, clock = make(script)
        book.screen(price_gt(0))
        script.ticker_result = HttpResult.failure(0, "network")
        clock["t"] = NOW + 60_000
        data = book.screen(price_gt(0))
        self.assertEqual(data["rows"][0]["values"]["price"]["display"], 42.0)
        self.assertEqual(data["rows"][0]["verdict"], "unknown")
        self.assertEqual(data["asOf"]["tickerError"], "network")

    def test_retry_after_stops_further_klines(self):
        script = Script()
        bases = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        script.info = [spot(base) for base in bases]
        script.tickers = [ticker(base, 10, 5_000_000) for base in bases]
        calls = {"n": 0}

        def kline(url: str) -> HttpResult:
            calls["n"] += 1
            return HttpResult.failure(429, "http_429", headers={"Retry-After": "30"})

        script.kline_result = kline
        book, _clock = make(script, concurrency=1)
        query = {"cmp": "gt", "left": ema_field(10), "right": {"num": 0}}
        data = book.screen(query, budget=5)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(data["counts"]["passes"], 0)
        states = {row["base"]: row["values"]["ema10"]["state"] for row in data["rows"]}
        self.assertIn("rate_limited", states.values())
        before = calls["n"]
        book.screen(query, budget=5)
        self.assertEqual(calls["n"], before)

    def test_requests_for_the_same_book_are_merged(self):
        script = Script()
        script.delay = 0.2
        script.tickers = [ticker("BTC", 10, 5)]
        book, _clock = make(script)
        results = []

        def run() -> None:
            results.append(book.screen(price_gt(0)))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for url in script.calls if "ticker" in url), 1)
        self.assertEqual(sum(1 for url in script.calls if "exchangeInfo" in url), 1)

    def test_budget_does_not_drop_the_rest_as_hits(self):
        script = Script()
        bases = ["AAA", "BBB", "CCC"]
        script.info = [spot(base) for base in bases]
        script.tickers = [ticker(base, 10, 5_000_000) for base in bases]
        for base in bases:
            script.klines[base + "USDT"] = klines_from([float(i) for i in range(1, 40)])
        book, _clock = make(script, concurrency=1)
        query = {"cmp": "gt", "left": ema_field(10), "right": {"num": 0}}
        first = book.screen(query, budget=1)
        self.assertEqual(len(script.kline_calls()), 1)
        self.assertEqual(first["counts"]["passes"], 1)
        self.assertEqual(first["counts"]["unknown"], 2)
        pending = [row for row in first["rows"] if row["verdict"] == "unknown"]
        self.assertTrue(all(row["values"]["ema10"]["state"] == "pending" for row in pending))
        second = book.screen(query, budget=1)
        self.assertEqual(len(script.kline_calls()), 2)
        self.assertGreaterEqual(second["counts"]["passes"], 2)

    def test_store_bars_are_preferred_and_forming_is_still_excluded(self):
        script = Script()
        script.tickers = [ticker("BTC", 10, 5)]

        def reader(base: str, now: int):
            if base != "BTC":
                return None
            bars = []
            day = now // MARKET_DAY_MS * MARKET_DAY_MS
            last_open = day - MARKET_DAY_MS
            for index in range(20):
                open_time = last_open - MARKET_DAY_MS * (19 - index)
                bars.append({"openTime": open_time, "closeTime": open_time + MARKET_DAY_MS - 1, "close": 10.0})
            bars.append({"openTime": day, "closeTime": day + MARKET_DAY_MS - 1, "close": 1000.0})
            return {"bars": bars, "fetchedAt": now, "source": "auu-mainstream-store:binance"}

        book, _clock = make(script, store=reader)
        data = book.screen({"cmp": "gt", "left": ema_field(20), "right": {"num": 10}})
        self.assertEqual(script.kline_calls(), [])
        self.assertEqual(data["rows"][0]["verdict"], "fail")
        self.assertEqual(data["rows"][0]["klineSource"], "auu-mainstream-store:binance")
        self.assertTrue(data["rows"][0]["formingExcluded"])

    def test_stale_universe_cannot_pass(self):
        script = Script()
        script.tickers = [ticker("BTC", 10, 5)]
        book, clock = make(script)
        self.assertEqual(book.screen(price_gt(0))["rows"][0]["verdict"], "pass")
        script.info_result = HttpResult.failure(0, "network")
        clock["t"] = NOW + 31 * 60 * 1000
        # Ticker would also be stale; keep it fresh by letting the ticker refresh succeed.
        data = book.screen(price_gt(0))
        self.assertEqual(data["rows"][0]["verdict"], "unknown")
        self.assertIn("universe_stale", data["rows"][0]["reasons"])
        self.assertTrue(data["universe"]["stale"])

    def test_old_closed_bar_is_not_fresh(self):
        script = Script()
        script.info = [spot("OLD")]
        script.tickers = [ticker("OLD", 12, 5_000_000)]
        script.klines["OLDUSDT"] = klines_from([12.0] * 30, shift_days=10)
        book, clock = make(script)
        query = {"cmp": "gt", "left": ema_field(10), "right": {"num": 0}}
        data = book.screen(query)
        row = data["rows"][0]
        self.assertEqual(row["verdict"], "unknown")
        self.assertEqual(row["values"]["ema10"]["state"], "stale")
        self.assertIsNone(row["values"]["ema10"]["value"])
        self.assertIsNotNone(row["values"]["ema10"].get("display"))
        self.assertEqual(data["status"], "degraded")
        self.assertEqual(len(script.kline_calls()), 1)
        book.screen(query)
        self.assertEqual(len(script.kline_calls()), 1)
        clock["t"] = NOW + 16 * 60 * 1000
        book.screen(query)
        self.assertEqual(len(script.kline_calls()), 2)
        self.assertEqual(book.screen(query)["rows"][0]["verdict"], "unknown")

    def test_fixture_default_query_shapes(self):
        book = ScreenerBook(transport=FixtureTransport(), source_kind="fixture", store_reader=lambda *_a, **_k: None, concurrency=2)
        data = book.screen(DEFAULT_QUERY, budget=20)
        self.assertTrue(data["fixture"])
        self.assertFalse(data["liveEnabled"])
        self.assertEqual(data["orderMode"], "paper")
        bases = {row["base"]: row["verdict"] for row in data["rows"]}
        self.assertEqual(bases["BTC"], "pass")
        self.assertEqual(bases["ETH"], "pass")
        self.assertEqual(bases["SOL"], "fail")
        self.assertEqual(bases["USDC"], "fail")
        self.assertEqual(bases["NEW"], "unknown")
        self.assertEqual(bases["NAN"], "unknown")
        self.assertEqual(bases["OLD"], "unknown")
        self.assertNotIn("FUT", bases)
        self.assertNotIn("HALT", bases)
        self.assertIn("BTCUP", bases)
        for row in data["rows"]:
            self.assertRegex(row["base"], r"^[A-Z0-9]{2,20}$")
            self.assertEqual(row["chartPath"], f"/?symbol={row['base']}&venue=binance")
            self.assertNotIn("<", row["symbol"])


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.book = ScreenerBook(
            transport=FixtureTransport(),
            source_kind="fixture",
            store_reader=lambda *_a, **_k: None,
            concurrency=2,
        )
        screener_routes.set_book(self.book)

    def tearDown(self) -> None:
        screener_routes.set_book(None)

    def test_anonymous_gets_no_book(self):
        prev = os.environ.get("AUU_AUTH")
        os.environ["AUU_AUTH"] = "on"
        from fastapi.testclient import TestClient

        try:
            with TestClient(app) as client:
                res = client.post("/api/v1/screener/spot", json=price_gt(0))
                self.assertEqual(res.status_code, 401)
                self.assertEqual(res.json()["error"]["code"], "AUTH_REQUIRED")
                self.assertNotIn("0.9997", res.text)
                self.assertNotIn("lastPrice", res.text)
                meta = client.get("/api/v1/screener/spot")
                self.assertEqual(meta.status_code, 401)
        finally:
            if prev is None:
                os.environ.pop("AUU_AUTH", None)
            else:
                os.environ["AUU_AUTH"] = prev

    def test_signed_in_can_read_and_live_stays_off(self):
        import tempfile

        from fastapi.testclient import TestClient

        from app.auth.accounts import reset_accounts

        tmp = tempfile.TemporaryDirectory()
        prev = {
            key: os.environ.get(key)
            for key in ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_INVITE_CODE", "AUU_LIVE_ENABLED", "AUU_EMAIL_VERIFY")
        }
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ["AUU_EMAIL_VERIFY"] = "off"
        os.environ["AUU_LIVE_ENABLED"] = "true"
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ["AUU_USER_STORE"] = str(Path(tmp.name) / "users.json")
        reset_accounts()
        try:
            with TestClient(app) as client:
                reg = client.post(
                    "/api/v1/auth/register",
                    json={"name": "screenuser", "password": "screenPass1", "password_confirm": "screenPass1"},
                )
                self.assertEqual(reg.status_code, 200, reg.text)
                meta = client.get("/api/v1/screener/spot")
                self.assertEqual(meta.status_code, 200, meta.text)
                # Injected fixture book is labeled fixture. The default book is covered
                # by BookTests.test_fixture_switch_defaults_off and stays off.
                self.assertTrue(meta.json()["data"]["fixture"])
                self.assertFalse(meta.json()["data"]["liveEnabled"])
                screened = client.post("/api/v1/screener/spot", json=DEFAULT_QUERY)
                self.assertEqual(screened.status_code, 200, screened.text)
                self.assertFalse(screened.json()["data"]["liveEnabled"])
                self.assertFalse(screened.json()["data"]["orders"])
                self.assertGreater(screened.json()["data"]["counts"]["passes"], 0)
        finally:
            for key, value in prev.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            reset_accounts()
            tmp.cleanup()

    def _accounts(self):
        import tempfile

        from app.auth.accounts import reset_accounts

        tmp = tempfile.TemporaryDirectory()
        keys = ("AUU_AUTH", "AUU_ALLOW_SIGNUP", "AUU_USER_STORE", "AUU_INVITE_CODE", "AUU_LIVE_ENABLED", "AUU_EMAIL_VERIFY", "AUU_SCREENER_FIXTURE")
        prev = {key: os.environ.get(key) for key in keys}
        os.environ["AUU_AUTH"] = "on"
        os.environ["AUU_ALLOW_SIGNUP"] = "on"
        os.environ["AUU_EMAIL_VERIFY"] = "off"
        os.environ["AUU_LIVE_ENABLED"] = "true"
        os.environ["AUU_SCREENER_FIXTURE"] = "1"
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ["AUU_USER_STORE"] = str(Path(tmp.name) / "users.json")
        reset_accounts()
        return tmp, prev

    def _restore(self, tmp, prev) -> None:
        from app.auth.accounts import reset_accounts

        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_accounts()
        tmp.cleanup()

    def _register(self, client, name: str) -> None:
        res = client.post(
            "/api/v1/auth/register",
            json={"name": name, "password": "screenPass1", "password_confirm": "screenPass1"},
        )
        self.assertEqual(res.status_code, 200, res.text)

    def test_member_headers_and_fixture_do_not_unlock(self):
        from fastapi.testclient import TestClient

        tmp, prev = self._accounts()
        try:
            with TestClient(app) as admin, TestClient(app) as member:
                self._register(admin, "screenadmin")
                self._register(member, "screenmember")
                self.assertTrue(admin.get("/api/v1/auth/me").json()["data"]["user"]["is_admin"])
                self.assertFalse(member.get("/api/v1/auth/me").json()["data"]["user"]["is_admin"])
                headers = {"X-Subscription": "active", "X-AUU-Admin": "1", "X-Preview": "on"}
                for method, path, body in (
                    ("GET", "/api/v1/screener/spot", None),
                    ("POST", "/api/v1/screener/spot", DEFAULT_QUERY),
                    ("POST", "/api/v1/screener/spot/csv", DEFAULT_QUERY),
                ):
                    res = member.request(method, path, json=body, headers=headers)
                    self.assertEqual(res.status_code, 403, res.text)
                    self.assertEqual(res.json()["error"]["code"], "SCREENER_ADMIN")
                    for leaked in ("0.9997", "BTCUSDT", "lastPrice", "quoteVolume"):
                        self.assertNotIn(leaked, res.text)
                self.assertEqual(member.get("/api/v1/live/status").status_code, 200)
                self.assertEqual(admin.get("/api/v1/screener/spot").status_code, 200)
                screened = admin.post("/api/v1/screener/spot", json=DEFAULT_QUERY)
                self.assertEqual(screened.status_code, 200, screened.text)
                self.assertGreater(screened.json()["data"]["counts"]["passes"], 0)
        finally:
            self._restore(tmp, prev)

    def test_auth_off_fixture_is_still_refused(self):
        from fastapi.testclient import TestClient

        prev_auth = os.environ.get("AUU_AUTH")
        prev_fix = os.environ.get("AUU_SCREENER_FIXTURE")
        os.environ["AUU_AUTH"] = "off"
        os.environ["AUU_SCREENER_FIXTURE"] = "1"
        try:
            with TestClient(app) as client:
                for method, path in (("GET", "/api/v1/screener/spot"), ("POST", "/api/v1/screener/spot"), ("POST", "/api/v1/screener/spot/csv")):
                    res = client.request(method, path, json=DEFAULT_QUERY)
                    self.assertEqual(res.status_code, 403, res.text)
                    self.assertEqual(res.json()["error"]["code"], "SCREENER_ADMIN")
                    self.assertNotIn("BTCUSDT", res.text)
                    self.assertNotIn("0.9997", res.text)
        finally:
            if prev_auth is None:
                os.environ.pop("AUU_AUTH", None)
            else:
                os.environ["AUU_AUTH"] = prev_auth
            if prev_fix is None:
                os.environ.pop("AUU_SCREENER_FIXTURE", None)
            else:
                os.environ["AUU_SCREENER_FIXTURE"] = prev_fix

    def test_bad_query_is_400_and_csv_is_quoted(self):
        from fastapi.testclient import TestClient

        tmp, prev = self._accounts()
        try:
            with TestClient(app) as client:
                self._register(client, "screenadmin")
                bad = client.post("/api/v1/screener/spot", json={"cmp": "gt", "left": {"field": "ema", "period": 50, "tf": "1d"}, "right": {"num": 1}})
                self.assertEqual(bad.status_code, 400)
                self.assertNotIn("0.9997", bad.text)
                cross = client.post("/api/v1/screener/spot", json={"cmp": "crossed_above", "left": {"field": "price"}, "right": {"num": 1}})
                self.assertEqual(cross.status_code, 400)
                huge = client.post("/api/v1/screener/spot", data=b"{" + b" " * 9000, headers={"Content-Type": "application/json"})
                self.assertEqual(huge.status_code, 400)
                ok = client.post("/api/v1/screener/spot", json=DEFAULT_QUERY)
                self.assertEqual(ok.status_code, 200, ok.text)
                body = ok.json()["data"]
                self.assertFalse(body["liveEnabled"])
                self.assertEqual(body["orderMode"], "paper")
                self.assertFalse(body["orders"])
                csv = client.post("/api/v1/screener/spot/csv", json=DEFAULT_QUERY)
                self.assertEqual(csv.status_code, 200, csv.text)
                self.assertIn("text/csv", csv.headers["content-type"])
                self.assertTrue(csv.content.startswith(b"\xef\xbb\xbf"))
                self.assertIn(b'"BTCUSDT"', csv.content)
                self.assertNotIn(b"<script", csv.content)
        finally:
            self._restore(tmp, prev)


class AccessTests(unittest.TestCase):
    def test_decide_trusts_only_admin_session(self):
        from app.points import catalog
        from app.screener.access import CODE, decide

        self.assertEqual(decide(None, auth_on=True)[:2], (401, "AUTH_REQUIRED"))
        self.assertEqual(decide(None, auth_on=False)[:2], (403, CODE))
        self.assertEqual(decide({"is_admin": False, "subscribed": True, "subscription": "active"}, auth_on=True)[:2], (403, CODE))
        self.assertIsNone(decide({"is_admin": True, "subscription": "expired"}, auth_on=True))
        self.assertEqual(list(catalog.ITEMS), ["example_badge"])
        self.assertTrue(catalog.ITEMS["example_badge"]["example"])
        source = "\n".join(path.read_text(encoding="utf-8") for path in (REPO / "apps/api/app/screener").glob("*.py"))
        route = (REPO / "apps/api/app/routes/screener.py").read_text(encoding="utf-8")
        self.assertNotIn("has_entitlement", source + route)
        self.assertNotIn("app.points", source + route)
        self.assertNotIn("request.headers", route)


class RegistrationTests(unittest.TestCase):
    def test_auth_and_root_catalog_unchanged(self):
        self.assertNotIn("/api/v1/screener/spot", PUBLIC_PATHS)
        self.assertIn("/api/v1/health", PUBLIC_PATHS)
        self.assertEqual(gate_decision("/api/v1/screener/spot", "GET", None)[0], 401)
        self.assertEqual(gate_decision("/api/v1/screener/spot", "POST", None)[0], 401)
        # The shared gate still only requires login. The screener router, not this gate, requires an admin.
        self.assertIsNone(gate_decision("/api/v1/screener/spot", "POST", {"is_admin": False}))
        self.assertIsNone(gate_decision("/api/v1/majors", "GET", {"is_admin": False}))
        self.assertIsNone(gate_decision("/api/v1/mainstream/overview", "GET", {"is_admin": False}))
        self.assertEqual(gate_decision("/api/v1/majors", "GET", None)[0], 401)
        catalog = root(False)["data"]["endpoints"]
        self.assertNotIn("screener", catalog)
        for key in ("mainstreamOverview", "majors", "news", "liveStatus", "paperOrder"):
            self.assertIn(key, catalog)
        paths = set()
        for route in app.routes:
            if getattr(route, "path", None):
                paths.add(route.path)
            original = getattr(route, "original_router", None)
            for sub in getattr(original, "routes", []) if original is not None else []:
                if getattr(sub, "path", None):
                    paths.add(sub.path)
        self.assertIn("/api/v1/majors", paths)
        self.assertIn("/api/v1/mainstream/overview", paths)
        self.assertIn("/api/v1/screener/spot", paths)
        self.assertIn("/api/v1/screener/spot/csv", paths)

    def test_web_registration_is_additive(self):
        router = (REPO / "apps/web/src/app/router.tsx").read_text(encoding="utf-8")
        shell = (REPO / "apps/web/src/components/layout/AppShell.tsx").read_text(encoding="utf-8")
        premium = (REPO / "apps/web/src/lib/subscription.ts").read_text(encoding="utf-8")
        self.assertIn('path: "screener"', router)
        for path in ("mainstream", "majors", "news", "pnl", "console", "settings", "subscribe"):
            self.assertIn(f'path: "{path}"', router)
        nav = shell.split("const mainstreamNav", 1)[1].split("const mobileTabs", 1)[0]
        self.assertNotIn("screener", nav)
        for path in ("/", "/pnl", "/console", "/performance", "/majors", "/news", "/airdrops", "/exchange"):
            self.assertIn(f'to: "{path}"' if path != "/" else 'to: "/"', nav)
        self.assertIn('to: "/?symbol=BTC"', nav)
        for path in ("/", "/mainstream", "/majors", "/pnl", "/console", "/performance", "/leaderboard", "/markets", "/market", "/board", "/strategy", "/review", "/positions", "/observe", "/alerts", "/backtest", "/watch", "/screener"):
            self.assertIn(f'"{path}"', premium)
        for path in ('"/news"', '"/settings"', '"/login"', '"/subscribe"', '"/airdrops"'):
            self.assertNotIn(path, premium.split("const PREMIUM", 1)[1].split(");", 1)[0])
        page = (REPO / "apps/web/src/pages/ScreenerPage").read_text(encoding="utf-8") if False else "\n".join(
            path.read_text(encoding="utf-8") for path in (REPO / "apps/web/src/pages/ScreenerPage").rglob("*") if path.is_file()
        )
        self.assertNotIn("dangerouslySetInnerHTML", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("localStorage", page)
        self.assertNotIn("WebSocket", page)
        self.assertNotIn("/api/v1/ws", page)
        self.assertNotIn("ema50", page.lower())
        self.assertNotIn("crossed", page.lower())


if __name__ == "__main__":
    unittest.main()
