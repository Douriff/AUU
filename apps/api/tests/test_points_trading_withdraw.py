"""Points trading ledger + withdrawal freeze/complete + feature flags (paper/top-up untouched)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.points.ledger import PointsError, PointsLedger, reset_points_ledger
from app.points import trading, withdraw
from app.points.trading import PointsTradingAccounts, usdt_to_points
from app.paper.mainstream_account import Limits, OrderError

NOW = 1_790_000_000_000
K = "k" * 20


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Market:
    def __init__(self, clock):
        self.px = {"BTC": 100_000.0, "ETH": 4_000.0}
        self.ts = {}
        self.bars = {"BTC": [], "ETH": []}
        self.clock = clock

    def price(self, sym):
        return (self.px[sym], self.ts.get(sym, self.clock())) if sym in self.px else None

    def candles(self, sym, since):
        return [b for b in self.bars.get(sym, []) if b["ts"] >= since // 60_000 * 60_000]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "AUU_POINTS_STORE": str(Path(self.tmp.name) / "points.sqlite"),
            "AUU_POINTS_TRADING": "on",
            "AUU_WITHDRAW_ENABLED": "on",
            "AUU_TRC20_RATE": "100",
            "AUU_WITHDRAW_SKIP_CHAIN": "on",
        }, clear=False)
        self.env.start()
        reset_points_ledger(None)
        self.led = PointsLedger(Path(self.tmp.name) / "points.sqlite")
        reset_points_ledger(self.led)
        self.clock = Clock()
        self.mkt = Market(self.clock)
        self.acc = PointsTradingAccounts(
            Path(self.tmp.name) / "pt.sqlite",
            price_fn=self.mkt.price,
            candles_fn=self.mkt.candles,
            symbols=lambda: ["BTC", "ETH"],
            limits=Limits(start_cash=0, min_order_usdt=10, max_order_usdt=5000, max_position_usdt=10000),
            now_ms=self.clock,
            points_rate=100,
        )
        trading.reset_points_accounts(self.acc)
        self.n = 0
        # seed points
        self.led.append(user_id="u1", kind="topup", amount=500_000, reason="seed topup", operator="admin:t",
                        idem_key="seed-topup-u1xxxxxx", fingerprint="seed1")

    def tearDown(self):
        self.acc.close()
        reset_points_ledger(None)
        trading.reset_points_accounts(None)
        self.env.stop()
        self.tmp.cleanup()

    def order(self, **kw):
        self.n += 1
        self.clock.t += 5_000
        body = {"client_order_id": f"cid-{self.n:06d}", "symbol": "BTC", "side": "buy", "type": "market"}
        body.update(kw)
        return self.acc.place("u1", body)


class TradingLedgerTests(_Base):
    def test_flag_default_off_blocks(self):
        with patch.dict(os.environ, {"AUU_POINTS_TRADING": "off"}):
            with self.assertRaises(OrderError) as cm:
                self.order(notional=100)
            self.assertEqual(cm.exception.code, "POINTS_TRADING_OFF")

    def test_buy_sell_writes_ledger_and_pnl(self):
        before = self.led.balance("u1")
        o = self.order(notional=100)
        self.assertEqual(o["status"], "filled")
        self.assertEqual(o["mode"], "points")
        mid = self.led.balance("u1")
        self.assertLess(mid, before)
        kinds = [e["kind"] for e in self.led.entries("u1", limit=10)]
        self.assertIn("points_trade_buy", kinds)
        self.assertIn("points_trade_fee", kinds)
        snap = self.acc.snapshot("u1", "BTC")
        self.assertEqual(snap["mode"], "points")
        self.assertGreater(snap["positions"][0]["qty"], 0)
        # sell all
        qty = snap["positions"][0]["qty"]
        self.mkt.px["BTC"] = 110_000.0
        s = self.order(side="sell", qty=qty)
        self.assertEqual(s["status"], "filled")
        after = self.led.balance("u1")
        self.assertGreater(after, mid)  # sale returns points
        self.assertTrue(self.led.verify()["ok"])
        # paper ledger hash untouched — we never opened mainstream_paper.sqlite here

    def test_insufficient_points(self):
        # drain almost all
        bal = self.led.balance("u1")
        self.led.append(user_id="u1", kind="spend", amount=-(bal - 50), reason="drain almost", operator="admin:t",
                        idem_key="drain-almost-u1xxxxx", fingerprint="drain")
        with self.assertRaises(OrderError) as cm:
            self.order(notional=1000)
        self.assertEqual(cm.exception.code, "INSUFFICIENT_POINTS")

    def test_admin_only_gate(self):
        self.assertTrue(trading.enabled_for(True))
        with patch.dict(os.environ, {"AUU_POINTS_TRADING_ADMIN_ONLY": "on"}):
            self.assertFalse(trading.enabled_for(False))
            self.assertTrue(trading.enabled_for(True))


class WithdrawTests(_Base):
    def test_apply_freeze_reject_unfreeze(self):
        before = self.led.balance("u1")
        # need a valid-looking TRC20 address (checksummed). Use a well-known Tron address format.
        # Generate via trc20 helpers from a known body.
        from app.points.trc20 import b58encode_check
        addr = b58encode_check(bytes.fromhex("41" + "11" * 20))
        row, rep = withdraw.apply("u1", 10, addr, "idem-withdraw-apply01", is_admin=True)
        self.assertFalse(rep)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.led.balance("u1"), before - 10 * 100)
        kinds = [e["kind"] for e in self.led.entries("u1", limit=5)]
        self.assertIn("withdraw_freeze", kinds)
        out = withdraw.reject("admin1", row["id"], "test reject reason")
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(self.led.balance("u1"), before)
        self.assertTrue(any(e["kind"] == "withdraw_unfreeze" for e in self.led.entries("u1", limit=5)))
        self.assertTrue(self.led.verify()["ok"])

    def test_complete_with_skip_chain(self):
        from app.points.trc20 import b58encode_check
        addr = b58encode_check(bytes.fromhex("41" + "22" * 20))
        before = self.led.balance("u1")
        row, _ = withdraw.apply("u1", 10, addr, "idem-withdraw-complete1", is_admin=True)
        tx = "ab" * 32
        done = withdraw.complete("admin1", row["id"], tx, "paid from tokenpocket", skip_chain=True)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["tx_hash"], tx)
        # points stay deducted (freeze already took them); complete is zero-amount marker
        self.assertEqual(self.led.balance("u1"), before - 1000)
        kinds = [e["kind"] for e in self.led.entries("u1", limit=10)]
        self.assertIn("withdraw_complete", kinds)
        self.assertTrue(self.led.verify()["ok"])

    def test_withdraw_off(self):
        with patch.dict(os.environ, {"AUU_WITHDRAW_ENABLED": "off"}):
            from app.points.trc20 import b58encode_check
            addr = b58encode_check(bytes.fromhex("41" + "33" * 20))
            with self.assertRaises(PointsError) as cm:
                withdraw.apply("u1", 10, addr, "idem-withdraw-off00001", is_admin=True)
            self.assertEqual(str(cm.exception), "WITHDRAW_OFF")

    def test_limits_and_bad_address(self):
        with self.assertRaises(PointsError) as cm:
            withdraw.apply("u1", 10, "not-an-address", "idem-withdraw-badaddr01", is_admin=True)
        self.assertEqual(str(cm.exception), "WITHDRAW_BAD_ADDRESS")
        from app.points.trc20 import b58encode_check
        addr = b58encode_check(bytes.fromhex("41" + "44" * 20))
        with self.assertRaises(PointsError):
            withdraw.apply("u1", 1, addr, "idem-withdraw-too-small1", is_admin=True)  # below min 10


class FlagTests(unittest.TestCase):
    def test_defaults_off(self):
        with patch.dict(os.environ, {"AUU_POINTS_TRADING": "", "AUU_WITHDRAW_ENABLED": ""}, clear=False):
            # empty -> off via _on default
            self.assertFalse(trading.enabled())
            self.assertFalse(withdraw.enabled())

    def test_rate_reuse(self):
        with patch.dict(os.environ, {"AUU_TRC20_RATE": "100"}):
            self.assertEqual(trading.rate(), 100)
            self.assertEqual(withdraw.rate(), 100)
            self.assertEqual(usdt_to_points(10.5), 1050)


class MigrationTests(unittest.TestCase):
    def test_old_db_migrates_kinds(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "old.sqlite"
        # create with OLD schema manually
        import sqlite3
        db = sqlite3.connect(path)
        db.executescript("""
        CREATE TABLE points_ledger (
          id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, ts INTEGER NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('topup','spend','adjust','refund')),
          amount INTEGER NOT NULL CHECK (amount <> 0 AND abs(amount) <= 10000000),
          balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
          reason TEXT NOT NULL, operator TEXT NOT NULL, provider TEXT, ref TEXT,
          idem_key TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
          prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
        );
        """)
        db.close()
        led = PointsLedger(path)
        led.append(user_id="u", kind="topup", amount=50, reason="old", operator="a",
                   idem_key="old-migrate-keyxxxx", fingerprint="fm")
        led.append(user_id="u", kind="points_trade_buy", amount=-10, reason="buy", operator="a",
                   idem_key="old-migrate-buyxxxxx", fingerprint="fb")
        self.assertEqual(led.balance("u"), 40)
        self.assertTrue(led.verify()["ok"])
        led.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
