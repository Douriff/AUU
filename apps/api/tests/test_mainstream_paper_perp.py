"""Paper USDT-M perpetuals: long/short, leverage cap, margin modes, liquidation, funding, close, migration."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.backtest.costs import CostModel
from app.paper.mainstream_account import Limits, OrderError, PaperAccounts
from app.paper.perp_account import FUNDING_MS, liq_price

NOW = 1_790_000_000_000


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.px = {"BTC": 100_000.0, "ETH": 4_000.0}
        self.bars: list[dict] = []
        self.path = Path(self.tmp.name) / "p.sqlite"
        self.acc = self._make()
        self.n = 0

    def _make(self, cost=None):
        return PaperAccounts(self.path, price_fn=lambda s: (self.px[s], self.clock()) if s in self.px else None,
                             candles_fn=lambda s, since: [b for b in self.bars if b["ts"] >= since // 60_000 * 60_000],
                             symbols=lambda: ["BTC", "ETH"], limits=Limits(), now_ms=self.clock,
                             cost=cost or CostModel(slippage={}, slippage_default=0.0), funding_fn=lambda s: 0.0001)

    def tearDown(self):
        self.acc.close()
        self.tmp.cleanup()

    def perp(self, **kw):
        self.n += 1
        self.clock.t += 5_000
        body = {"client_order_id": f"pcid-{self.n:06d}", "symbol": "BTC", "side": "buy"}
        body.update(kw)
        return self.acc.perp.place("u1", body)

    def snap(self):
        return self.acc.snapshot("u1", "BTC")


class TestPerp(Base):
    def test_open_long_locks_margin_and_fee(self):
        o = self.perp(notional=5000, leverage=5, margin_mode="isolated")
        self.assertEqual(o["kind"], "open")
        s = self.snap()
        p = s["perp"]["positions"][0]
        self.assertEqual((p["side"], p["leverage"], p["marginMode"]), ("long", 5, "isolated"))
        self.assertAlmostEqual(p["margin"], 1000)
        fee = 5000 * 0.0005
        self.assertAlmostEqual(s["cash"], 10_000 - 1000 - fee)
        self.assertAlmostEqual(s["equity"], 10_000 - fee)
        self.assertAlmostEqual(p["liqPrice"], liq_price("long", 100_000, 0.05, 1000, 0.005))
        self.assertLess(p["liqPrice"], 100_000 * 0.81)

    def test_short_profit_and_close(self):
        self.perp(side="sell", notional=2000, leverage=2)
        self.assertEqual(self.snap()["perp"]["positions"][0]["side"], "short")
        self.px["BTC"] = 90_000.0
        p = self.snap()["perp"]["positions"][0]
        self.assertAlmostEqual(p["unrealized"], 0.02 * 10_000)
        r = self.acc.perp.close_position("u1", "BTC", "close-000001")
        self.assertEqual(r["order"]["kind"], "close")
        s = self.snap()
        self.assertEqual(s["perp"]["positions"], [])
        fees = 2000 * 0.0005 + 0.02 * 90_000 * 0.0005
        self.assertAlmostEqual(s["cash"], 10_000 + 200 - fees, places=6)
        again = self.acc.perp.close_position("u1", "BTC", "close-000001")
        self.assertTrue(again["duplicate"])
        with self.assertRaises(OrderError):
            self.acc.perp.close_position("u1", "BTC", "close-000002")

    def test_leverage_cap_and_mode(self):
        with self.assertRaises(OrderError) as c:
            self.perp(notional=100, leverage=6)
        self.assertEqual(c.exception.code, "LEVERAGE_TOO_HIGH")
        with self.assertRaises(OrderError):
            self.perp(notional=100, leverage=2, margin_mode="hedge")
        self.perp(notional=100, leverage=2)
        with self.assertRaises(OrderError) as c:
            self.acc.perp.set_leverage("u1", "BTC", 3, "cross")
        self.assertEqual(c.exception.code, "POSITION_OPEN")

    def test_caps(self):
        with self.assertRaises(OrderError) as c:
            self.perp(notional=12_000, leverage=5)  # margin 2400 > 2000
        self.assertEqual(c.exception.code, "ORDER_CAP")
        with self.assertRaises(OrderError) as c:
            self.perp(type="limit", notional=100)
        self.assertEqual(c.exception.code, "BAD_ORDER")

    def test_flip_and_reduce_only(self):
        self.perp(qty=0.01, leverage=2)
        o = self.perp(side="sell", qty=0.03)
        self.assertEqual(o["kind"], "flip")
        p = self.snap()["perp"]["positions"][0]
        self.assertEqual(p["side"], "short")
        self.assertAlmostEqual(p["qty"], 0.02)
        with self.assertRaises(OrderError):
            self.perp(side="sell", qty=0.01, reduce_only=True)

    def test_isolated_liquidation_via_bar(self):
        self.perp(notional=5000, leverage=5, margin_mode="isolated")
        lp = self.snap()["perp"]["positions"][0]["liqPrice"]
        self.clock.t += 120_000
        self.bars.append({"ts": self.clock.t // 60_000 * 60_000 - 60_000, "open": 99_000, "high": 99_000, "low": lp - 1, "close": 99_000})
        s = self.snap()
        self.assertEqual(s["perp"]["positions"], [])
        liq = [o for o in s["perp"]["orders"] if o["kind"] == "liquidation"]
        self.assertEqual(len(liq), 1)
        self.assertAlmostEqual(s["cash"], 10_000 - 1000 - 2.5, places=6)  # margin lost, nothing else

    def test_cross_liq_farther_than_isolated(self):
        self.perp(notional=5000, leverage=5, margin_mode="cross")
        cross = self.snap()["perp"]["positions"][0]["liqPrice"]
        self.assertLess(cross, liq_price("long", 100_000, 0.05, 1000, 0.005))

    def test_funding_long_pays_short_receives(self):
        self.perp(notional=1000, leverage=1, margin_mode="isolated")
        self.perp(symbol="ETH", side="sell", notional=1000, leverage=1)
        self.clock.t = (self.clock.t // FUNDING_MS + 1) * FUNDING_MS + 1
        s = self.acc.snapshot("u1")
        by = {p["symbol"]: p for p in s["perp"]["positions"]}
        self.assertAlmostEqual(by["BTC"]["funding"], -0.1, places=6)
        self.assertAlmostEqual(by["BTC"]["margin"], 1000 - 0.1, places=6)
        self.assertAlmostEqual(by["ETH"]["funding"], 0.1, places=6)

    def test_slippage_applied(self):
        self.acc.close()
        self.acc = self._make(CostModel(slippage={"BTC": 0.001}))
        o = self.perp(qty=0.01)
        self.assertAlmostEqual(o["fillPrice"], 100_100)
        o = self.perp(side="sell", qty=0.01)
        self.assertAlmostEqual(o["fillPrice"], 99_900)

    def test_spot_unchanged_and_still_long_only(self):
        self.acc.place("u1", {"client_order_id": "spot-000001", "symbol": "BTC", "side": "buy", "notional": 1000})
        with self.assertRaises(OrderError) as c:
            self.clock.t += 5000
            self.acc.place("u1", {"client_order_id": "spot-000002", "symbol": "BTC", "side": "sell", "qty": 0.015})
        self.assertEqual(c.exception.code, "INSUFFICIENT_POSITION")

    def test_additive_migration_keeps_old_rows(self):
        self.acc.close()
        old = Path(self.tmp.name) / "old.sqlite"
        db = sqlite3.connect(old)
        db.executescript("""CREATE TABLE accounts (user_id TEXT PRIMARY KEY, cash REAL NOT NULL, start_cash REAL NOT NULL, created_ms INTEGER NOT NULL);
        INSERT INTO accounts VALUES ('u1', 9465.77, 10000, 1);""")
        db.commit()
        db.close()
        self.path = old
        self.acc = self._make()
        s = self.acc.snapshot("u1")
        self.assertAlmostEqual(s["cash"], 9465.77)
        self.assertEqual(s["perp"]["positions"], [])


if __name__ == "__main__":
    unittest.main()
