"""Display-only order book: cached, bounded, never blocks a request, no network in tests."""
from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from app.marketdata.mainstream.orderbook import BookCache
from tests.test_mainstream_data import NOW, FakeExchange, _cfg, _NoNetwork


class BookExchange(FakeExchange):
    def __init__(self, *a, book_fail=False, **kw):
        super().__init__(*a, **kw)
        self.book_fail = book_fail

    def fetch_order_book(self, symbol, limit=None):
        self._gate("fetch_order_book", symbol, limit)
        if self.book_fail:
            raise RuntimeError("book endpoint down")
        bids = [[100.0 - i, 1.0 + i] for i in range(limit or 20)]
        asks = [[101.0 + i, 2.0 + i] for i in range(limit or 20)]
        return {"symbol": symbol, "bids": bids, "asks": asks, "timestamp": NOW}


class SlowBookExchange(BookExchange):
    def __init__(self, *a, delay=0.5, **kw):
        super().__init__(*a, **kw)
        self.delay = delay

    def fetch_order_book(self, symbol, limit=None):
        time.sleep(self.delay)
        return super().fetch_order_book(symbol, limit)


class OrderBookTests(_NoNetwork):
    def _book(self, ex):
        t = {"now": NOW}
        svc = self.svc({"binance": ex}, cfg=_cfg(symbols=["BTC", "ETH"], strategy_symbols=["BTC", "ETH", "SOL"]))
        svc.now_ms = lambda: t["now"]
        svc.refresh_once()
        return BookCache(svc), t

    def _wait(self, bc, base="BTC"):
        for _ in range(300):
            b = bc._books.get(base)
            if b is None or not b["busy"]:
                return
            time.sleep(0.01)

    @staticmethod
    def _n(ex):
        return sum(1 for c in ex.calls if c[0] == "fetch_order_book")

    def test_first_call_pending_then_cached_and_trimmed(self):
        ex = BookExchange()
        bc, t = self._book(ex)
        with patch.dict(os.environ, {"AUU_MAINSTREAM_BOOK_WAIT_MS": "0"}):  # never-wait mode
            first = bc.get("BTC", 12)
        self.assertTrue(first["pending"])
        self.assertEqual(first["bids"], [])
        self._wait(bc)
        out = bc.get("BTC", 12)
        self.assertEqual(len(out["bids"]), 12)
        self.assertEqual(len(out["asks"]), 12)
        self.assertEqual(out["bids"][0], [100.0, 1.0])
        self.assertEqual(out["asks"][0], [101.0, 2.0])
        self.assertAlmostEqual(out["mid"], 100.5)
        self.assertAlmostEqual(out["spreadBp"], 1 / 100.5 * 1e4)
        self.assertEqual(out["note"], "display_only")
        self.assertEqual(out["pair"], "BTC/USDT")
        self.assertEqual(self._n(ex), 1)
        self.assertEqual(ex.calls[-1][1], ("BTC/USDT", 20))
        # within TTL: many requests, no new outbound call
        for _ in range(20):
            bc.get("BTC", 12)
        self.assertEqual(self._n(ex), 1)
        self.assertLessEqual(len(bc._books["BTC"]["bids"]), 20)

    def test_stale_serves_cache_and_refreshes_once(self):
        ex = BookExchange()
        bc, t = self._book(ex)
        bc.get("BTC"); self._wait(bc)
        t["now"] += 10_000
        out = bc.get("BTC")
        self.assertEqual(len(out["bids"]), 12)  # old snapshot returned immediately
        self.assertFalse(out["pending"])
        self._wait(bc)
        self.assertEqual(self._n(ex), 2)

    def test_stale_snapshot_waits_for_the_refresh(self):
        """Back on a coin after 2 min (the ~126 s case): the answer is the fresh book, not the old one."""
        ex = BookExchange()
        bc, t = self._book(ex)
        first = bc.get("BTC")  # empty cache: waits for the first fetch instead of answering 'pending'
        self.assertEqual(len(first["bids"]), 12)
        self.assertFalse(first["pending"])
        self.assertIsNotNone(first["waitedMs"])
        t["now"] += 126_000
        out = bc.get("BTC")
        self.assertEqual(out["ageMs"], 0)
        self.assertFalse(out["stale"])
        self.assertFalse(out["refreshing"])
        self.assertEqual(self._n(ex), 2)

    def test_wait_is_bounded_when_the_exchange_is_slow(self):
        ex = SlowBookExchange(delay=0.6)
        bc, t = self._book(ex)
        with patch.dict(os.environ, {"AUU_MAINSTREAM_BOOK_WAIT_MS": "100"}):
            t0 = time.monotonic()
            out = bc.get("BTC")
            took = time.monotonic() - t0
        self.assertLess(took, 0.45)
        self.assertTrue(out["pending"])
        self.assertTrue(out["refreshing"])
        self.assertEqual(out["bids"], [])
        self._wait(bc)
        self.assertEqual(len(bc.get("BTC")["bids"]), 12)

    def test_fresh_snapshot_never_waits(self):
        ex = SlowBookExchange(delay=0.3)
        bc, t = self._book(ex)
        bc.get("BTC"); self._wait(bc)
        t["now"] += 5_000  # older than the TTL (refresh due) but not stale
        t0 = time.monotonic()
        out = bc.get("BTC")
        self.assertLess(time.monotonic() - t0, 0.2)
        self.assertIsNone(out["waitedMs"])
        self.assertTrue(out["refreshing"])
        self.assertEqual(out["ageMs"], 5_000)
        self._wait(bc)

    def test_failed_refresh_releases_the_waiter(self):
        ex = BookExchange(book_fail=True)
        bc, _ = self._book(ex)
        t0 = time.monotonic()
        out = bc.get("BTC")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("book endpoint down", out["error"])
        self.assertFalse(out["refreshing"])

    def test_unknown_symbol_never_fetched(self):
        ex = BookExchange()
        bc, _ = self._book(ex)
        with self.assertRaises(KeyError):
            bc.get("PEPE")
        self.assertEqual(self._n(ex), 0)
        self.assertNotIn("PEPE", bc._books)

    def test_failure_backs_off(self):
        ex = BookExchange(book_fail=True)
        bc, t = self._book(ex)
        bc.get("BTC"); self._wait(bc)
        out = bc.get("BTC")
        self.assertIn("book endpoint down", out["error"])
        self.assertEqual(out["bids"], [])
        t["now"] += 30_000
        bc.get("BTC"); self._wait(bc)
        self.assertEqual(self._n(ex), 1)  # still backing off
        t["now"] += 31_000
        bc.get("BTC"); self._wait(bc)
        self.assertEqual(self._n(ex), 2)

    def test_global_budget_and_off_switch(self):
        ex = BookExchange()
        bc, t = self._book(ex)
        with patch.dict(os.environ, {"AUU_MAINSTREAM_BOOK_PER_MIN": "2"}):
            for base in ("BTC", "ETH", "SOL"):
                bc.get(base); self._wait(bc, base)
        self.assertEqual(self._n(ex), 2)
        with patch.dict(os.environ, {"AUU_MAINSTREAM_BOOK": "off"}):
            t["now"] += 120_000
            out = bc.get("BTC")
            self.assertFalse(out["enabled"])
        self.assertEqual(self._n(ex), 2)


if __name__ == "__main__":
    unittest.main()
