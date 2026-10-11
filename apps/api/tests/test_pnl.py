"""盈亏 view of the manual paper account: server-side figures, today's P&L, stale prices, curve,
login + own-data only, and no writes to the paper ledger."""
from __future__ import annotations

import hashlib
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paper.mainstream_account import Limits, PaperAccounts, reset_accounts
from app.paper.pnl import PnlView, bj_midnight, reset_pnl
from tests.test_mainstream_paper import NOW, Clock, Market

H = 3_600_000
MID = bj_midnight(NOW)


class History:
    """Hourly closes; price(sym, t) piecewise constant."""

    def __init__(self):
        self.series: dict[str, list[tuple[int, float]]] = {}

    def set(self, sym, pts):
        self.series[sym] = sorted(pts)

    def __call__(self, sym, tf, limit):
        step = {"1h": H, "4h": 4 * H, "1d": 24 * H}[tf]
        pts = self.series.get(sym, [])
        if not pts:
            return []
        out = []
        t = (pts[0][0] // step) * step
        while t < NOW:
            px = [p for ts, p in pts if ts <= t + step - 1]
            if px:
                out.append({"ts": t, "close": px[-1]})
            t += step
        return out[-limit:]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(MID - 30 * H)  # account opened ~1.25 days before "today"
        self.mkt = Market(self.clock)
        self.acc = PaperAccounts(Path(self.tmp.name) / "p.sqlite", price_fn=self.mkt.price, candles_fn=self.mkt.candles,
                                 symbols=lambda: ["BTC", "ETH"], limits=Limits(), now_ms=self.clock)
        self.hist = History()
        self.view = PnlView(self.acc, history_fn=self.hist, now_ms=self.clock)
        self.n = 0

    def tearDown(self):
        self.acc.close()
        self.tmp.cleanup()

    def buy(self, sym, usdt, uid="u1"):
        self.n += 1
        return self.acc.place(uid, {"client_order_id": f"pnl-order-{self.n:06d}", "symbol": sym, "side": "buy", "type": "market", "notional": usdt})

    def sell(self, sym, qty, uid="u1"):
        self.n += 1
        return self.acc.place(uid, {"client_order_id": f"pnl-order-{self.n:06d}", "symbol": sym, "side": "sell", "type": "market", "qty": qty})


class SummaryTests(_Base):
    def test_figures_and_identity(self):
        self.buy("BTC", 1000)
        self.buy("ETH", 500)
        self.clock.t = NOW
        self.mkt.px["BTC"] = 110_000.0
        self.mkt.px["ETH"] = 3_600.0
        s = self.view.summary("u1")
        btc = next(p for p in s["positions"] if p["symbol"] == "BTC")
        self.assertAlmostEqual(btc["unrealized"], (110_000 - btc["avgPrice"]) * btc["qty"])
        self.assertAlmostEqual(btc["unrealizedPct"], 110_000 / btc["avgPrice"] - 1)
        self.assertGreater(btc["unrealized"], 0)
        eth = next(p for p in s["positions"] if p["symbol"] == "ETH")
        self.assertLess(eth["unrealized"], 0)
        self.assertAlmostEqual(s["equity"], s["cash"] + btc["value"] + eth["value"])
        # equity - start = unrealized + realized (fees are booked as negative realized)
        self.assertAlmostEqual(s["equity"] - s["startCash"], s["unrealized"] + s["realized"], places=6)
        self.assertAlmostEqual(s["totalReturn"], s["equity"] / 10_000 - 1)
        self.assertFalse(s["stale"])
        self.assertEqual([p["symbol"] for p in s["positions"]], ["BTC", "ETH"])  # by value

    def test_realized_after_sell(self):
        o = self.buy("BTC", 1000)
        self.mkt.px["BTC"] = 120_000.0
        self.sell("BTC", o["qty"] if "qty" in o else self.acc.snapshot("u1")["positions"][0]["qty"])
        s = self.view.summary("u1")
        self.assertEqual(s["positions"], [])
        self.assertAlmostEqual(s["unrealized"], 0)
        self.assertGreater(s["realized"], 150)
        self.assertAlmostEqual(s["equity"] - 10_000, s["realized"], places=6)

    def test_stale_and_missing_price_flagged(self):
        self.buy("BTC", 1000)
        self.buy("ETH", 500)
        self.clock.t += 10 * 60_000
        self.mkt.ts["BTC"] = self.clock.t - 5 * 60_000  # 5 min old
        self.mkt.ts["ETH"] = self.clock.t - 10_000
        s = self.view.summary("u1")
        by = {p["symbol"]: p for p in s["positions"]}
        self.assertTrue(by["BTC"]["stale"])
        self.assertFalse(by["ETH"]["stale"])
        self.assertTrue(s["stale"])
        del self.mkt.px["ETH"]
        s = self.view.summary("u1")
        e = next(p for p in s["positions"] if p["symbol"] == "ETH")
        self.assertTrue(e["priceMissing"] and e["stale"])
        self.assertEqual(e["last"], e["avgPrice"])  # valued at cost, flagged
        self.assertAlmostEqual(e["unrealized"], 0)

    def test_today_vs_midnight(self):
        self.hist.set("BTC", [(MID - 40 * H, 100_000.0), (MID - 2 * H, 105_000.0), (MID + 1 * H, 108_000.0)])
        self.buy("BTC", 1000)  # at MID-30h, px 100k
        qty = self.acc.snapshot("u1")["positions"][0]["qty"]
        self.clock.t = NOW
        self.mkt.px["BTC"] = 108_000.0
        s = self.view.summary("u1")
        cash = s["cash"]
        self.assertAlmostEqual(s["todayBase"], cash + qty * 105_000.0, places=4)
        self.assertAlmostEqual(s["today"], qty * 3_000.0, places=4)
        self.assertAlmostEqual(s["todayPct"], s["today"] / s["todayBase"])

    def test_new_account_today_uses_start_cash(self):
        self.clock.t = NOW
        s = self.view.summary("fresh")
        self.assertEqual((s["equity"], s["today"], s["totalReturn"], s["positions"]), (10_000.0, 0.0, 0.0, []))

    def test_read_only_no_ledger_change(self):
        self.buy("BTC", 1000)
        self.acc._db.commit()
        rows = lambda: hashlib.sha256(repr([list(r) for t in ("accounts", "positions", "orders")  # noqa: E731
                                            for r in self.acc._db.execute(f"SELECT * FROM {t} ORDER BY 1")]).encode()).hexdigest()
        before = rows()
        self.view.summary("u1")
        self.view.curve("u1", "7d")
        self.view.curve("u1", "all")
        self.assertEqual(before, rows())


class CurveTests(_Base):
    def test_curve_replays_fills(self):
        self.hist.set("BTC", [(MID - 40 * H, 100_000.0), (MID - 10 * H, 90_000.0)])
        self.buy("BTC", 2000)
        qty = self.acc.snapshot("u1")["positions"][0]["qty"]
        cash = self.acc.snapshot("u1")["cash"]
        self.clock.t = NOW
        self.mkt.px["BTC"] = 90_000.0
        c = self.view.curve("u1", "7d")
        self.assertEqual(c["tf"], "1h")
        self.assertEqual(c["points"][0]["equity"], 10_000.0)  # account start
        late = [p for p in c["points"] if MID - 8 * H <= p["ts"] < NOW]
        self.assertTrue(late)
        for p in late:
            self.assertAlmostEqual(p["equity"], cash + qty * 90_000.0, places=4)
        self.assertTrue(c["points"][-1]["live"])
        self.assertAlmostEqual(c["end"], self.view.summary("u1")["equity"])
        ts = [p["ts"] for p in c["points"]]
        self.assertEqual(ts, sorted(ts))
        for r in ("30d", "all", "bogus"):
            self.assertTrue(self.view.curve("u1", r)["points"])
        self.assertEqual(self.view.curve("u1", "bogus")["range"], "7d")

    def test_missing_history_marks_approx(self):
        self.buy("BTC", 1000)
        self.clock.t = NOW
        c = self.view.curve("u1", "7d")
        self.assertTrue(c["approx"])

    def test_point_cap(self):
        self.clock.t = NOW - 900 * 86_400_000
        self.buy("BTC", 1000)
        self.clock.t = NOW
        self.assertLessEqual(len(self.view.curve("u1", "all")["points"]), 402)


class ApiTests(_Base):
    def setUp(self):
        super().setUp()
        from app.auth.accounts import reset_accounts as reset_users

        root = Path(self.tmp.name)
        self._env = patch.dict(os.environ, {"AUU_AUTH": "on", "AUU_ALLOW_SIGNUP": "on", "AUU_USER_STORE": str(root / "users.json"),
                                            "AUU_USER_JOURNAL_DIR": str(root / "journals"), "AUU_AUTH_RATE_MAX": "1000",
                                            "AUU_LEGACY_PUMP": "off"})
        self._env.start()
        os.environ.pop("AUU_INVITE_CODE", None)
        os.environ.pop("AUU_ADMIN_USER", None)
        reset_users()
        reset_accounts(self.acc)
        reset_pnl(self.view)
        self._sock = patch.object(socket.socket, "connect", side_effect=AssertionError("network access in test"))
        self._sock.start()
        from fastapi.testclient import TestClient
        from app.main import create_app

        self.app = create_app(legacy=False)
        self.TC = TestClient

    def tearDown(self):
        from app.auth.accounts import reset_accounts as reset_users

        self._sock.stop()
        reset_accounts(None)
        reset_pnl(None)
        self._env.stop()
        reset_users()
        super().tearDown()

    def client(self, name):
        c = self.TC(self.app, headers={"user-agent": "UnitTest/1.0"})
        r = c.post("/api/v1/auth/register", json={"name": name, "password": "password123", "password_confirm": "password123"})
        self.assertEqual(r.status_code, 200, r.text)
        return c, r.json()["data"]["user"]["id"]

    def test_login_required_and_own_data_only(self):
        anon = self.TC(self.app, headers={"user-agent": "UnitTest/1.0"})
        self.assertEqual(anon.get("/api/v1/pnl/summary").status_code, 401)
        self.assertEqual(anon.get("/api/v1/pnl/curve").status_code, 401)
        a, aid = self.client("alice")
        b, _ = self.client("bob")
        self.buy("BTC", 1000, uid=aid)
        sa = a.get("/api/v1/pnl/summary").json()["data"]
        sb = b.get("/api/v1/pnl/summary").json()["data"]
        self.assertEqual(len(sa["positions"]), 1)
        self.assertEqual(sb["positions"], [])
        # no way to ask for someone else's id
        sb2 = b.get(f"/api/v1/pnl/summary?user_id={aid}&uid={aid}").json()["data"]
        self.assertEqual(sb2["positions"], [])
        c = a.get("/api/v1/pnl/curve?range=30d").json()["data"]
        self.assertEqual(c["range"], "30d")
        self.assertEqual(a.get("/api/v1/pnl/curve?range=../x").json()["data"]["range"], "7d")


if __name__ == "__main__":
    unittest.main()
