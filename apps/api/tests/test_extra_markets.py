"""大盘 coins outside the pool: allowlist from top-100 boards, bounded on-demand candles, no network."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.marketdata import extra as X

NOW = 1_790_000_000_000


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def fetch_ohlcv(self, symbol, tf, since=None, limit=None):
        self.calls.append(("ohlcv", symbol, tf, since, limit))
        if self.fail:
            raise RuntimeError("down")
        step = X.TF_MS[tf]
        end = NOW // step * step
        start = since if since is not None else end - (limit - 1) * step
        return [[t, 1.0, 2.0, 0.5, 1.5, 10.0] for t in range(start, min(end, start + (limit - 1) * step) + 1, step)]

    def fetch_order_book(self, symbol, limit=None):
        self.calls.append(("book", symbol, limit))
        return {"bids": [[1.0, 1.0]], "asks": [[1.1, 1.0]], "timestamp": NOW}


class FakeFetcher:
    def __init__(self, client):
        self.client = client


class FakeCfg:
    def all_symbols(self):
        return ["BTC", "ETH"]

    def spot(self, b):
        return f"{b}/USDT"


class FakeSvc:
    def __init__(self, reader="okx", fail=False):
        self.cfg = FakeCfg()
        self.reader = reader
        self.t = {"now": NOW}
        self.clients = {"okx": FakeClient(fail), "binance": FakeClient(fail)}

    def now_ms(self):
        return self.t["now"]

    def exchange_for_read(self):
        return self.reader

    def _od_fetcher(self, name):
        return FakeFetcher(self.clients[name])


def board(*bases):
    return {"status": "ok", "rows": [{"base": b, "symbol": b + "USDT", "last": 2.0, "change_24h": 0.1, "volume_24h": 1e6} for b in bases]}


class ExtraTests(unittest.TestCase):
    def _x(self, boards=None, held=(), **kw):
        svc = FakeSvc(**kw)
        boards = boards or {"okx": board("PEPE", "WIF"), "binance": board("PEPE", "FLOKI"), "bybit": board("ONLYBY"), "coinbase": board()}
        self.loads = []

        def load(v):
            self.loads.append(v)
            return boards.get(v, {"status": "unavailable", "rows": []})

        return X.ExtraMarkets(svc, board_fn=load, peek_fn=lambda v: None, held_fn=lambda: set(held)), svc

    def test_symbol_format(self):
        self.assertEqual(X.clean_symbol("pepe"), "PEPE")
        self.assertEqual(X.clean_symbol("PEPE/USDT"), "PEPE")
        self.assertEqual(X.clean_symbol("PEPEUSDT"), "PEPE")
        for bad in ("", "a", "../etc", "PE PE", "X" * 30, "BTC;rm"):
            self.assertIsNone(X.clean_symbol(bad), bad)

    def test_allowlist_and_resolve(self):
        x, svc = self._x()
        self.assertEqual(x.resolve("PEPE", "okx"), ("okx", None))
        self.assertEqual(x.resolve("PEPE", "binance"), ("binance", None))
        self.assertEqual(x.resolve("FLOKI", "okx"), ("binance", None))  # not on OKX top-100: binance data
        self.assertEqual(x.resolve("WIF", None), ("okx", None))  # reader first
        self.assertEqual(x.resolve("BTC", "binance"), ("binance", None))  # pool coin, other venue
        self.assertEqual(x.resolve("ONLYBY", "bybit"), (None, "VENUE_NO_DATA"))
        self.assertEqual(x.resolve("NOPE", "okx"), (None, "NOT_LISTED"))
        self.assertTrue(x.allowed_for_paper("PEPE"))
        self.assertFalse(x.allowed_for_paper("ONLYBY"))
        self.assertFalse(x.allowed_for_paper("NOPE"))
        syms = x.paper_symbols()
        self.assertEqual(syms[:2], ["BTC", "ETH"])
        self.assertIn("FLOKI", syms)
        self.assertNotIn("ONLYBY", syms)
        n = len(self.loads)
        for _ in range(20):
            x.listed("okx", "PEPE")
        self.assertEqual(len(self.loads), n)  # board refreshed at most every ALLOW_REFRESH_S

    def test_held_coin_stays_tradable_after_leaving_top100(self):
        x, _ = self._x(held=("OLD",))
        self.assertEqual(x.resolve("OLD", "okx"), ("okx", None))
        self.assertTrue(x.allowed_for_paper("OLD"))
        self.assertIn("OLD", x.paper_symbols())

    def test_allow_entries_expire_after_24h(self):
        boards = {"okx": board("PEPE"), "binance": board()}
        x, svc = self._x(boards=boards)
        self.assertTrue(x.listed("okx", "PEPE"))
        boards["okx"] = board("WIF")
        svc.t["now"] += 25 * 3600 * 1000
        x._allow_at["okx"] = -1e9
        x._allow_try["okx"] = -1e9
        self.assertTrue(x.listed("okx", "WIF"))
        self.assertFalse(x.listed("okx", "PEPE"))

    def test_candles_cached_ttl_and_tail_polls_free(self):
        x, svc = self._x()
        out = x.candles("okx", "PEPE", "1h", limit=500)
        self.assertEqual(len(out["candles"]), X.MAX_BARS["okx"])
        self.assertEqual(out["pair"], "PEPE/USDT")
        self.assertEqual(set(out["candles"][0]), {"ts", "open", "high", "low", "close", "volume"})
        for _ in range(30):
            self.assertEqual(len(x.candles("okx", "PEPE", "1h", limit=3)["candles"]), 3)
        self.assertEqual(len(svc.clients["okx"].calls), 1)
        svc.t["now"] += X.TTL_MS["1h"]
        x.candles("okx", "PEPE", "1h", limit=3)
        self.assertEqual(len(svc.clients["okx"].calls), 2)
        older = x.candles("okx", "PEPE", "1h", limit=50, before=out["candles"][10]["ts"])
        self.assertTrue(older["limited"])
        self.assertEqual(len(older["candles"]), 10)

    def test_global_budget_and_lru_bound(self):
        x, svc = self._x()
        with patch.dict(os.environ, {"AUU_EXTRA_FETCH_PER_MIN": "5"}):
            for i in range(8):
                x.candles("okx", f"C{i}X", "1d", limit=10)
        self.assertEqual(len(svc.clients["okx"].calls), 5)
        svc.t["now"] += 61_000
        for i in range(X.MAX_KEYS + 10):
            x.window("binance", f"K{i}Z", "1m")
            svc.t["now"] += 2_000
        self.assertLessEqual(x.stats()["windows"], X.MAX_KEYS)
        self.assertLessEqual(x.stats()["bars"], X.MAX_KEYS * max(X.MAX_BARS.values()))

    def test_failure_backoff_and_empty_message(self):
        x, svc = self._x(fail=True)
        out = x.candles("okx", "PEPE", "1h", limit=10)
        self.assertEqual(out["candles"], [])
        self.assertIn("fetchError", out)
        svc.t["now"] += 10_000
        x.candles("okx", "PEPE", "1h", limit=10)
        self.assertEqual(len(svc.clients["okx"].calls), 1)  # backing off
        svc.t["now"] += X.FAIL_BACKOFF_MS
        x.candles("okx", "PEPE", "1h", limit=10)
        self.assertEqual(len(svc.clients["okx"].calls), 2)

    def test_paper_price_and_bars_since(self):
        x, svc = self._x()
        px, ts = x.last_price("PEPE")
        self.assertEqual(px, 1.5)
        self.assertLessEqual(ts, NOW)
        recent = x.bars_since("PEPE", NOW - 10 * 60_000)
        self.assertTrue(recent and recent[0]["ts"] == (NOW - 10 * 60_000) // 60_000 * 60_000)
        ts_list = [r["ts"] for r in recent]
        self.assertEqual(ts_list, sorted(set(ts_list)))
        old = x.bars_since("PEPE", NOW - 8 * 3600_000)  # older than the okx window: paged, contiguous
        steps = {b["ts"] - a["ts"] for a, b in zip(old, old[1:])}
        self.assertEqual(steps, {60_000})
        self.assertEqual(old[0]["ts"], (NOW - 8 * 3600_000) // 60_000 * 60_000)
        self.assertIsNone(x.last_price("NOPE"))


class BookExtraTests(unittest.TestCase):
    def test_extra_book_key_and_bound(self):
        from app.marketdata.mainstream import orderbook as OB

        svc = FakeSvc()
        bc = OB.BookCache(svc)
        out = bc.get("PEPE", 12, ex="binance")
        self.assertTrue(out["pending"])
        self.assertEqual(out["pair"], "PEPE/USDT")
        with self.assertRaises(KeyError):
            bc.get("PEPE", 12)  # without a validated venue only pool coins are accepted
        for i in range(OB.MAX_SLOTS + 20):
            with bc._lock:
                bc._slot(f"okx:Z{i}")
        self.assertLessEqual(len(bc._books), OB.MAX_SLOTS)


if __name__ == "__main__":
    unittest.main()
