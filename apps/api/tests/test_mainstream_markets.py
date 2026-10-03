"""Market list: all strategy coins in one batched read, cached tickers, 4h from 1h, no network."""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_mainstream_data import NOW, H, FakeExchange, _cfg, _NoNetwork

COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


class TickerExchange(FakeExchange):
    def __init__(self, *a, ticker_fail=False, **kw):
        super().__init__(*a, **kw)
        self.ticker_fail = ticker_fail

    def fetch_tickers(self, symbols=None):
        self._gate("fetch_tickers", symbols)
        if self.ticker_fail:
            raise RuntimeError("ticker endpoint down")
        return {s: {"symbol": s, "last": 200.0, "percentage": 2.5, "quoteVolume": 1.5e9, "timestamp": NOW} for s in symbols}


class MarketsTests(_NoNetwork):
    def _svc(self, ex, now=None):
        t = {"now": NOW if now is None else now}
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC", "ETH", "SOL"], strategy_symbols=COINS))
        svc.now_ms = lambda: t["now"]
        svc.refresh_once()
        return svc, t

    def _wait(self, svc):
        for _ in range(200):
            if not svc._tk["busy"]:
                return
            time.sleep(0.01)

    def test_all_coins_one_batched_read_with_holdings(self):
        ex = TickerExchange()
        with patch.dict(os.environ, {"AUU_MAINSTREAM_TICKERS": "off"}):
            svc, _ = self._svc(ex)
            n_calls = len(ex.calls)
            out = svc.markets(holdings={"XRP": -0.12, "BTC": 0.2})
        self.assertEqual([i["symbol"] for i in out["items"]], COINS)
        self.assertEqual(len(ex.calls), n_calls)  # served from the store: no exchange request
        xrp = next(i for i in out["items"] if i["symbol"] == "XRP")
        self.assertTrue(xrp["held"])
        self.assertAlmostEqual(xrp["weight"], -0.12)
        self.assertFalse(xrp["display"])
        self.assertIsNotNone(xrp["price"])
        self.assertIsNotNone(xrp["quoteVolume24h"])
        self.assertIsNotNone(xrp["funding"])
        self.assertGreaterEqual(len(xrp["spark30"]), 8)
        self.assertEqual(out["tickers"]["source"], "store")
        self.assertFalse(next(i for i in out["items"] if i["symbol"] == "ETH")["held"])

    def test_tickers_one_request_cached_and_overlay(self):
        ex = TickerExchange()
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        calls = [c for c in ex.calls if c[0] == "fetch_tickers"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0][1][0]), len(COINS))  # one batched request for every coin
        out = svc.markets()
        self.assertEqual(out["tickers"]["source"], "ticker")
        btc = out["items"][0]
        self.assertEqual(btc["price"], 200.0)
        self.assertAlmostEqual(btc["change24h"], 0.025)
        self.assertEqual(btc["quoteVolume24h"], 1.5e9)
        for _ in range(5):
            svc.markets()
        self._wait(svc)
        self.assertEqual(len([c for c in ex.calls if c[0] == "fetch_tickers"]), 1)  # within the TTL
        t["now"] += 31_000
        svc.markets()
        self._wait(svc)
        self.assertEqual(len([c for c in ex.calls if c[0] == "fetch_tickers"]), 2)

    def test_ticker_failure_backs_off_and_falls_back_to_store(self):
        ex = TickerExchange(ticker_fail=True)
        svc, t = self._svc(ex)
        svc.markets()
        self._wait(svc)
        out = svc.markets()
        self.assertEqual(out["tickers"]["source"], "store")
        self.assertIn("RuntimeError", out["tickers"]["error"])
        n = len([c for c in ex.calls if c[0] == "fetch_tickers"])
        t["now"] += 60_000
        svc.markets()
        self._wait(svc)
        self.assertEqual(len([c for c in ex.calls if c[0] == "fetch_tickers"]), n)  # still in back-off
        self.assertIsNotNone(out["items"][0]["price"])

    def test_4h_for_strategy_only_coin_is_built_from_1h(self):
        ex = TickerExchange()
        with patch.dict(os.environ, {"AUU_MAINSTREAM_TICKERS": "off"}):
            svc, _ = self._svc(ex)
        out = svc.chart_candles("XRP", "4h", limit=6)
        rows = out["candles"]
        self.assertEqual(out.get("derived"), "4h from stored 1h")
        self.assertTrue(rows)
        self.assertTrue(all(r["ts"] % (4 * H) == 0 for r in rows))
        h = svc.store.candles("binance", "XRP", "1h", since=rows[-2]["ts"], until=rows[-2]["ts"] + 4 * H - 1)
        self.assertEqual(len(h), 4)
        self.assertEqual(rows[-2]["high"], max(x["high"] for x in h))
        self.assertEqual(rows[-2]["close"], h[-1]["close"])
        self.assertAlmostEqual(rows[-2]["volume"], sum(x["volume"] for x in h))
        # display coins keep their stored 4h
        self.assertNotIn("derived", svc.chart_candles("BTC", "4h", limit=6))


if __name__ == "__main__":
    unittest.main()
